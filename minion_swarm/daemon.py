from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .config import SwarmConfig

MAX_CONSOLE_STREAM_CHARS = 12_000

POLL_SCRIPT = Path.home() / ".minion-comms" / "poll.sh"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AgentRunResult:
    exit_code: int
    timed_out: bool
    compaction_detected: bool
    command_name: str


class RollingBuffer:
    def __init__(self, max_tokens: int) -> None:
        self.max_chars = max_tokens * 4
        self._chunks: deque[str] = deque()
        self._total_chars = 0

    def append(self, text: str) -> None:
        if not text:
            return
        self._chunks.append(text)
        self._total_chars += len(text)
        while self._total_chars > self.max_chars and self._chunks:
            removed = self._chunks.popleft()
            self._total_chars -= len(removed)

    def snapshot(self) -> str:
        return "".join(self._chunks)

    def __len__(self) -> int:
        return self._total_chars


class AgentDaemon:
    def __init__(self, config: SwarmConfig, agent_name: str) -> None:
        if agent_name not in config.agents:
            raise KeyError(f"Unknown agent '{agent_name}' in config")

        self.config = config
        self.agent_cfg = config.agents[agent_name]
        self.agent_name = agent_name

        self.buffer = RollingBuffer(self.agent_cfg.max_history_tokens)

        self.inject_history_next_turn = False
        self.consecutive_failures = 0
        self.last_error: Optional[str] = None

        self._stop_event = threading.Event()

        self.state_path = self.config.state_dir / f"{self.agent_name}.json"
        self.resume_ready = self._load_resume_ready()

        # dead-drop mode still uses the watcher for backward compat
        self._use_poll = self._comms_name() == "minion-comms"
        self._watcher: Any = None

    def _get_watcher(self) -> Any:
        """Lazy-init watcher for dead-drop mode only."""
        if self._watcher is None:
            from .watcher import CommsWatcher
            self._watcher = CommsWatcher(self.agent_name, self.config.comms_db)
        return self._watcher

    def run(self) -> None:
        self.config.ensure_runtime_dirs()

        if self._use_poll:
            self._run_poll_mode()
        else:
            self._run_watcher_mode()

    def _run_poll_mode(self) -> None:
        """minion-comms mode: poll.sh + claude invocations. No direct DB access."""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        self._log(f"starting daemon for {self.agent_name}")
        self._log(f"provider: {self.agent_cfg.provider} (resume_ready={self.resume_ready})")
        self._log(f"mode: poll ({POLL_SCRIPT})")
        self._write_state("idle")

        if not POLL_SCRIPT.exists():
            self._log(f"ERROR: poll.sh not found at {POLL_SCRIPT}")
            self._log("Run: curl -sSL https://raw.githubusercontent.com/ai-janitor/minion-comms/main/scripts/install.sh | bash")
            return

        # Boot: invoke claude directly to run ON STARTUP instructions
        self._log("boot: invoking agent for ON STARTUP")
        self._write_state("working")
        boot_prompt = self._build_boot_prompt()
        result = self._run_agent(boot_prompt)
        if result.exit_code == 0:
            self.resume_ready = True
            self._log("boot: complete")
        else:
            self._log(f"boot: failed (exit {result.exit_code})")

        self._write_state("idle")

        try:
            while not self._stop_event.is_set():
                # Block until poll.sh says there are messages
                self._log("polling for messages...")
                has_messages = self._poll_inbox()

                if self._stop_event.is_set():
                    break

                if not has_messages:
                    continue

                # Messages available — invoke claude to process via MCP
                self._write_state("working")
                self._log("messages detected, invoking agent")
                prompt = self._build_inbox_prompt()
                ok = self._process_prompt(prompt)

                if ok:
                    self.consecutive_failures = 0
                    self.last_error = None
                    self._write_state("idle")
                else:
                    self.consecutive_failures += 1
                    self._write_state(
                        "error",
                        failures=self.consecutive_failures,
                        last_error=self.last_error,
                    )
                    backoff = min(
                        self.agent_cfg.retry_backoff_sec * (2 ** (self.consecutive_failures - 1)),
                        self.agent_cfg.retry_backoff_max_sec,
                    )
                    self._log(f"failure #{self.consecutive_failures}; backing off {backoff}s ({self.last_error or 'unknown'})")
                    self._stop_event.wait(timeout=float(backoff))
        finally:
            self._write_state("stopped")
            self._log("daemon stopped")

    def _poll_inbox(self) -> bool:
        """Run poll.sh as a subprocess. Returns True if messages found."""
        try:
            proc = subprocess.run(
                ["bash", str(POLL_SCRIPT), self.agent_name, "--interval", "5", "--timeout", "30"],
                capture_output=True,
                text=True,
            )
            return proc.returncode == 0
        except Exception as exc:
            self._log(f"poll.sh error: {exc}")
            self._stop_event.wait(timeout=5.0)
            return False

    def _build_boot_prompt(self) -> str:
        """Prompt for the first invocation — agent registers and sets up."""
        system_section = self.agent_cfg.system.strip()
        protocol_section = self._build_protocol_section()
        rules_section = self._build_rules_section()
        boot_section = "BOOT: You just started. Execute your ON STARTUP instructions now."
        return "\n\n".join([system_section, protocol_section, rules_section, boot_section])

    def _build_inbox_prompt(self) -> str:
        """Prompt to check inbox and process messages via MCP."""
        # Strip ON STARTUP block from system prompt — boot already ran it.
        # Including it causes the agent to re-register and re-check_inbox
        # on every invocation, which can create message loops.
        system_section = self._strip_on_startup(self.agent_cfg.system.strip())
        protocol_section = self._build_protocol_section()
        rules_section = self._build_rules_section()

        sections: List[str] = [system_section, protocol_section]

        if self.inject_history_next_turn and len(self.buffer) > 0:
            sections.append(self._build_history_block(self.buffer.snapshot()))
            self.inject_history_next_turn = False

        inbox_section = (
            "You have new messages. Check your minion-comms inbox "
            "(check_inbox), read and process all messages, then send "
            "results via minion-comms send when done. "
            "Do NOT re-register — you are already registered."
        )
        sections.extend([rules_section, inbox_section])
        return "\n\n".join(s for s in sections if s.strip())

    @staticmethod
    def _strip_on_startup(text: str) -> str:
        """Remove ON STARTUP block from system prompt for subsequent invocations."""
        import re
        # Match "ON STARTUP ..." through to the next blank line or end of string
        return re.sub(
            r"ON STARTUP[^\n]*\n(?:[ \t]+\d+\..*\n)*(?:[ \t]+Then .*\n?)?",
            "",
            text,
        ).strip()

    def _process_prompt(self, prompt: str) -> bool:
        """Run the agent with a prompt and handle the result."""
        result = self._run_agent(prompt)

        if result.compaction_detected:
            self.inject_history_next_turn = True
            self._log("detected context compaction marker; history will be re-injected next cycle")

        if result.timed_out:
            self.last_error = f"{self.agent_cfg.provider} produced no output for {self.agent_cfg.no_output_timeout_sec}s"
            return False

        if result.exit_code != 0:
            self.last_error = f"{result.command_name} exited with code {result.exit_code}"
            return False

        self.resume_ready = True
        return True

    # ── dead-drop backward compat ──────────────────────────────────────────

    def _run_watcher_mode(self) -> None:
        """Legacy dead-drop mode: direct DB watcher."""
        watcher = self._get_watcher()
        watcher.start()
        watcher.register_agent(
            role=self.agent_cfg.role,
            description=f"minion-swarm daemon agent ({self.agent_cfg.zone})",
            status="online",
        )

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        self._log(f"starting daemon for {self.agent_name}")
        self._log(f"provider: {self.agent_cfg.provider} (resume_ready={self.resume_ready})")
        self._log(f"mode: watcher (dead-drop DB: {self.config.comms_db})")
        self._write_state("idle")

        try:
            while not self._stop_event.is_set():
                message = watcher.pop_next_message()

                if message is None:
                    watcher.set_agent_status("idle")
                    self._write_state("idle")
                    watcher.wait_for_update(timeout=5.0)
                    continue

                watcher.set_agent_status("working")
                self._write_state(
                    "working",
                    current_message_id=message.id,
                    from_agent=message.from_agent,
                    received_at=message.timestamp,
                )
                self._log(f"processing message {message.id} from {message.from_agent}")

                prompt = self._build_watcher_prompt(message)
                ok = self._process_prompt(prompt)

                if ok:
                    watcher.set_agent_status("online")
                    self._write_state("idle", last_message_id=message.id)
                    continue

                self._write_state(
                    "error",
                    failures=self.consecutive_failures,
                    last_error=self.last_error,
                    failed_message_id=message.id,
                )

                backoff = min(
                    self.agent_cfg.retry_backoff_sec * (2 ** (self.consecutive_failures - 1)),
                    self.agent_cfg.retry_backoff_max_sec,
                )
                self._log(f"failure #{self.consecutive_failures}; backing off {backoff}s ({self.last_error or 'unknown'})")

                if self.consecutive_failures >= 3:
                    self._alert_lead_watcher(watcher)

                self._stop_event.wait(timeout=float(backoff))

        finally:
            watcher.set_agent_status("offline")
            self._write_state("stopped")
            watcher.stop()
            self._log("daemon stopped")

    def _build_watcher_prompt(self, message: Any) -> str:
        """Build prompt with message content baked in (dead-drop mode)."""
        max_prompt_chars = self.agent_cfg.max_prompt_chars
        system_section = self.agent_cfg.system.strip()
        protocol_section = self._build_protocol_section()
        rules_section = self._build_rules_section()
        incoming_section = self._build_incoming_section(message)

        sections: List[str] = [system_section, protocol_section]

        if self.inject_history_next_turn and len(self.buffer) > 0:
            sections.append(self._build_history_block(self.buffer.snapshot()))
            self.inject_history_next_turn = False

        sections.extend([rules_section, incoming_section])
        prompt = "\n\n".join(s for s in sections if s.strip())

        if len(prompt) > max_prompt_chars:
            prompt = prompt[:max_prompt_chars]
            self._log("hard-truncated prompt to max_prompt_chars")

        return prompt

    def _build_incoming_section(self, message: Any) -> str:
        return "\n".join(
            [
                "Incoming message:",
                f"- id: {message.id}",
                f"- from: {message.from_agent}",
                f"- timestamp: {message.timestamp}",
                f"- broadcast: {message.is_broadcast}",
                "",
                message.content,
            ]
        )

    def _alert_lead_watcher(self, watcher: Any) -> None:
        lead = watcher.find_lead_agent() or "lead"
        content = (
            f"minion-swarm alert: agent {self.agent_name} has {self.consecutive_failures} "
            f"consecutive failures. Last error: {self.last_error or 'unknown'}."
        )
        try:
            watcher.send_message(self.agent_name, lead, content)
            self._log(f"alerted lead '{lead}' about repeated failures")
        except Exception as exc:
            self._log(f"failed to alert lead '{lead}': {exc}")

    # ── shared ─────────────────────────────────────────────────────────────

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        self._log(f"received signal {signum}, shutting down")
        self._stop_event.set()

    def _comms_name(self) -> str:
        if "minion-comms" in str(self.config.comms_db):
            return "minion-comms"
        return "dead-drop"

    def _build_rules_section(self) -> str:
        comms = self._comms_name()
        lines = [
            "Autonomous daemon rules:",
            "- Do not use AskUserQuestion.",
            f"- Route questions to lead via {comms} send.",
            "- Execute exactly the incoming task.",
            "- Send one summary message when done.",
            "- Task governance: lead manages task queue and assignment ownership.",
        ]

        if self.agent_cfg.role == "lead":
            lines.extend(
                [
                    "- As lead: create and maintain tasks.",
                    "- As lead: define scope and acceptance criteria.",
                    "- As lead: ask domain owners to update technical details based on direct work.",
                    "- As lead: after a task completes, review and assign the next task.",
                ]
            )
        else:
            lines.extend(
                [
                    "- Non-lead agents: execute assigned tasks, report results.",
                    "- If you discover new ideas, send them to lead.",
                ]
            )

        return "\n".join(lines)

    def _build_protocol_section(self) -> str:
        comms = self._comms_name()
        return "\n".join(
            [
                "Mandatory pre-task protocol (all agents):",
                f"- Use {comms} for all inter-agent communication.",
                f"- Check inbox via {comms} check_inbox before starting work.",
                f"- Send results via {comms} send when done.",
            ]
        )

    def _build_history_block(self, history_snapshot: str) -> str:
        return "\n".join(
            [
                "════════════════════ RECENT HISTORY (rolling buffer) ════════════════════",
                "The following is your captured stream-json history from before compaction.",
                "Use it to restore recent context and avoid redoing completed work.",
                "══════════════════════════════════════════════════════════════════════════",
                history_snapshot,
                "═══════════════════════ END RECENT HISTORY ═════════════════════════════",
            ]
        )

    def _truncate_tail(self, text: str, max_chars: int, prefix: str) -> str:
        if max_chars <= 0:
            return ""
        if len(text) <= max_chars:
            return text
        if len(prefix) >= max_chars:
            return prefix[:max_chars]
        keep = max_chars - len(prefix)
        return f"{prefix}{text[-keep:]}"

    def _run_agent(self, prompt: str) -> AgentRunResult:
        provider = self.agent_cfg.provider
        if provider == "claude":
            return self._run_command(self._build_claude_command(prompt))
        if provider == "codex":
            return self._run_with_optional_resume(
                resume_cmd=self._build_codex_command(prompt, use_resume=True),
                fresh_cmd=self._build_codex_command(prompt, use_resume=False),
                resume_label="codex resume --last",
            )
        if provider == "opencode":
            return self._run_with_optional_resume(
                resume_cmd=self._build_opencode_command(prompt, use_resume=True),
                fresh_cmd=self._build_opencode_command(prompt, use_resume=False),
                resume_label="opencode --continue",
            )
        if provider == "gemini":
            return self._run_with_optional_resume(
                resume_cmd=self._build_gemini_command(prompt, use_resume=True),
                fresh_cmd=self._build_gemini_command(prompt, use_resume=False),
                resume_label="gemini --resume latest",
            )
        raise ValueError(f"Unsupported provider: {provider}")

    def _run_with_optional_resume(self, resume_cmd: List[str], fresh_cmd: List[str], resume_label: str) -> AgentRunResult:
        if self.resume_ready:
            resumed = self._run_command(resume_cmd)
            if resumed.timed_out or resumed.exit_code == 0:
                return resumed
            self.resume_ready = False
            self._log(f"{resume_label} failed with exit {resumed.exit_code}; retrying without resume")

        return self._run_command(fresh_cmd)

    def _build_claude_command(self, prompt: str) -> List[str]:
        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        # Only use --continue in watcher mode (single agent per session).
        # In poll mode, multiple agents share the project dir so --continue
        # would resume the wrong agent's session.
        if not self._use_poll:
            cmd.append("--continue")
        if self.agent_cfg.allowed_tools:
            cmd.extend(["--allowed-tools", self.agent_cfg.allowed_tools])
        if self.agent_cfg.permission_mode:
            cmd.extend(["--permission-mode", self.agent_cfg.permission_mode])
        if self.agent_cfg.model:
            cmd.extend(["--model", self.agent_cfg.model])
        return cmd

    def _build_codex_command(self, prompt: str, use_resume: bool) -> List[str]:
        cmd = ["codex", "exec"]
        if use_resume:
            cmd.extend(["resume", "--last"])
        cmd.append("--json")
        if self.agent_cfg.model:
            cmd.extend(["--model", self.agent_cfg.model])
        cmd.append(prompt)
        return cmd

    def _build_opencode_command(self, prompt: str, use_resume: bool) -> List[str]:
        cmd = ["opencode", "run", "--format", "json"]
        if use_resume:
            cmd.append("--continue")
        if self.agent_cfg.model:
            cmd.extend(["--model", self.agent_cfg.model])
        cmd.append(prompt)
        return cmd

    def _build_gemini_command(self, prompt: str, use_resume: bool) -> List[str]:
        cmd = ["gemini", "--prompt", prompt, "--output-format", "stream-json"]
        if use_resume:
            cmd.extend(["--resume", "latest"])
        if self.agent_cfg.model:
            cmd.extend(["--model", self.agent_cfg.model])
        return cmd

    def _run_command(self, cmd: List[str]) -> AgentRunResult:
        self._log(f"exec: {cmd[0]} ({self.agent_cfg.provider})")
        self._print_stream_start(cmd[0])

        # Strip CLAUDECODE env var so nested claude sessions don't refuse to start
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.config.project_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
        except FileNotFoundError:
            self._log(f"command not found: {cmd[0]}")
            return AgentRunResult(exit_code=127, timed_out=False, compaction_detected=False, command_name=cmd[0])
        except Exception as exc:
            self._log(f"failed to launch {cmd[0]}: {exc}")
            return AgentRunResult(exit_code=127, timed_out=False, compaction_detected=False, command_name=cmd[0])

        q: "queue.Queue[Optional[str]]" = queue.Queue()

        def _reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                q.put(line)
            q.put(None)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()

        timed_out = False
        compaction_detected = False
        last_output_at = time.monotonic()
        displayed_chars = 0
        hidden_chars = 0

        while True:
            try:
                line = q.get(timeout=1.0)
            except queue.Empty:
                if proc.poll() is not None and q.empty():
                    break
                if time.monotonic() - last_output_at > self.agent_cfg.no_output_timeout_sec:
                    timed_out = True
                    proc.terminate()
                    break
                continue

            if line is None:
                break

            last_output_at = time.monotonic()
            self.buffer.append(line)
            rendered, has_compaction = self._render_stream_line(line)
            if rendered:
                remaining = MAX_CONSOLE_STREAM_CHARS - displayed_chars
                if remaining > 0:
                    chunk = rendered[:remaining]
                    print(chunk, end="", flush=True)
                    displayed_chars += len(chunk)
                else:
                    chunk = ""
                hidden_chars += len(rendered) - len(chunk)
            if has_compaction:
                compaction_detected = True

        if timed_out and proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        try:
            exit_code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            exit_code = proc.wait(timeout=5)

        self._print_stream_end(cmd[0], displayed_chars=displayed_chars, hidden_chars=hidden_chars)
        return AgentRunResult(
            exit_code=exit_code,
            timed_out=timed_out,
            compaction_detected=compaction_detected,
            command_name=cmd[0],
        )

    def _render_stream_line(self, line: str) -> Tuple[str, bool]:
        raw = line.rstrip("\n")
        if not raw:
            return "", False

        compaction = self._contains_compaction_marker(raw)

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw + "\n", compaction

        fragments = self._extract_text_fragments(payload)
        rendered = "".join(fragments)

        if not rendered:
            event_type = payload.get("type") if isinstance(payload, dict) else None
            if event_type in {"error", "warning"}:
                rendered = f"[{event_type}] {payload.get('message', '')}\n"

        if self._contains_compaction_marker(rendered):
            compaction = True

        if isinstance(payload, dict) and self._contains_compaction_marker(json.dumps(payload).lower()):
            compaction = True

        return rendered, compaction

    def _extract_text_fragments(self, payload: Any) -> List[str]:
        out: List[str] = []
        text_keys = {"text", "content", "delta", "output_text"}

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in text_keys and isinstance(value, str):
                        out.append(value)
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)
        return out

    def _contains_compaction_marker(self, text: str) -> bool:
        low = text.lower()
        markers = (
            "compaction",
            "compacted",
            "context window",
            "summarized prior",
            "summarised prior",
            "auto-compact",
        )
        return any(marker in low for marker in markers)

    def _print_stream_start(self, command_name: str) -> None:
        print(
            f"\n=== model-stream start: agent={self.agent_name} cmd={command_name} ===",
            flush=True,
        )

    def _print_stream_end(self, command_name: str, displayed_chars: int, hidden_chars: int) -> None:
        if hidden_chars > 0:
            print(f"\n[model-stream abbreviated: {hidden_chars} chars hidden]", flush=True)
        print(
            f"=== model-stream end: agent={self.agent_name} cmd={command_name} shown={displayed_chars} chars ===",
            flush=True,
        )

    def _load_resume_ready(self) -> bool:
        if not self.state_path.exists():
            return False
        try:
            payload = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            return False
        return bool(payload.get("resume_ready", False))

    def _write_state(self, status: str, **extra: Any) -> None:
        payload = {
            "agent": self.agent_name,
            "provider": self.agent_cfg.provider,
            "pid": os.getpid(),
            "status": status,
            "updated_at": utc_now_iso(),
            "consecutive_failures": self.consecutive_failures,
            "resume_ready": self.resume_ready,
        }
        payload.update(extra)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(payload, indent=2))

    def _log(self, message: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] [{self.agent_name}] {message}", flush=True)

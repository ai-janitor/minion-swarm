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

# Claude Code system prompt + tool definitions token costs (approximate).
# Each tool's JSON schema + description consumes context tokens.
# These are injected by Claude Code before the agent's prompt.
CLAUDE_CODE_SYSTEM_TOKENS = 3_500   # Base system prompt (instructions, rules, formatting)
CLAUDE_CODE_TOOL_TOKENS: dict[str, int] = {
    "Bash":             400,
    "Read":             350,
    "Write":            250,
    "Edit":             400,
    "Glob":             200,
    "Grep":             500,
    "WebFetch":         300,
    "WebSearch":        250,
    "Task":             2_500,  # Largest — includes all agent type descriptions
    "NotebookEdit":     300,
    "AskUserQuestion":  500,
    "EnterPlanMode":    800,
    "ExitPlanMode":     300,
    "TaskCreate":       500,
    "TaskUpdate":       500,
    "TaskList":         300,
    "TaskGet":          200,
    "TeamCreate":       1_500,
    "TeamDelete":       100,
    "SendMessage":      800,
    "Skill":            300,
    "TaskOutput":       200,
    "TaskStop":         100,
}
# Total with all tools: ~3500 + ~10550 ≈ 14k. With MCP tools, add per-tool.
# Claude Code also injects CLAUDE.md, rules, MEMORY.md — varies per project.
CLAUDE_CODE_PROJECT_OVERHEAD = 4_000  # Rough estimate for CLAUDE.md + rules


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AgentRunResult:
    exit_code: int
    timed_out: bool
    compaction_detected: bool
    command_name: str
    input_tokens: int = 0
    output_tokens: int = 0


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
        self._invocation = 0
        self._session_input_tokens = 0
        self._session_output_tokens = 0
        self._tool_overhead_tokens = 0  # Claude Code system prompt/tools overhead, measured at boot
        self._context_window = 0        # Set from modelUsage.contextWindow in stream-json

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

        # Reset stale HP from previous session
        self._update_hp(0, 0, turn_input=0, turn_output=0)

        # Boot: invoke claude directly to run ON STARTUP instructions
        self._log("boot: invoking agent for ON STARTUP")
        self._write_state("working")
        boot_prompt = self._build_boot_prompt()
        result = self._run_agent(boot_prompt)
        if result.exit_code == 0:
            self.resume_ready = True
            if result.input_tokens > 0:
                # input_tokens now includes cache tokens — real context consumed
                prompt_tokens = len(boot_prompt) // 4  # rough chars-to-tokens
                self._tool_overhead_tokens = max(0, result.input_tokens - prompt_tokens)
                ctx = self._context_window if self._context_window > 0 else 200_000
                self._log(f"boot HP: {result.input_tokens // 1000}k/{ctx // 1000}k context, overhead≈{self._tool_overhead_tokens // 1000}k, prompt≈{prompt_tokens} tokens")
                self._session_input_tokens += result.input_tokens
                self._session_output_tokens += result.output_tokens
                self._update_hp(
                    self._session_input_tokens, self._session_output_tokens,
                    turn_input=result.input_tokens, turn_output=result.output_tokens,
                )
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
        """Run poll.sh as a subprocess. Returns True if messages found.
        Sets stop_event if stand_down detected (exit code 3).
        """
        try:
            proc = subprocess.run(
                ["bash", str(POLL_SCRIPT), self.agent_name, "--interval", "5", "--timeout", "30"],
                capture_output=True,
                text=True,
            )
            if proc.returncode == 3:
                self._log("stand_down detected — leader dismissed the party")
                self._stop_event.set()
                return False
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
        role = self.agent_cfg.role or "coder"
        boot_section = "\n".join([
            "BOOT: You just started. Run these commands via the Bash tool:",
            f"  minion --compact register --name {self.agent_name} --class {role} --transport daemon",
            f"  minion set-context --agent {self.agent_name} --context 'just started'",
            f"  minion check-inbox --agent {self.agent_name}",
            f"  minion set-status --agent {self.agent_name} --status 'ready for orders'",
            "",
            "IMPORTANT: You are a daemon agent managed by minion-swarm.",
            "Do NOT run poll.sh — minion-swarm handles polling for you.",
            "Do NOT use AskUserQuestion — it blocks in headless mode.",
        ])
        return "\n\n".join([system_section, protocol_section, rules_section, boot_section])

    def _build_inbox_prompt(self) -> str:
        """Prompt to check inbox and process messages via CLI."""
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

        inbox_section = "\n".join([
            "You have new messages. Run via Bash tool:",
            f"  minion check-inbox --agent {self.agent_name}",
            "Read and process all messages, then send results:",
            f"  minion send --from {self.agent_name} --to <recipient> --message '...'",
            "Do NOT re-register — you are already registered.",
        ])
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

        # Track session-cumulative HP and write to DB
        if result.input_tokens > 0 or result.output_tokens > 0:
            self._session_input_tokens += result.input_tokens
            self._session_output_tokens += result.output_tokens
            self._update_hp(
                self._session_input_tokens, self._session_output_tokens,
                turn_input=result.input_tokens, turn_output=result.output_tokens,
            )

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
        lines = [
            "Autonomous daemon rules:",
            "- Do not use AskUserQuestion — it blocks in headless mode.",
            f"- Route questions to lead via Bash: minion send --from {self.agent_name} --to lead --message '...'",
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
        name = self.agent_name
        return "\n".join(
            [
                "Communication protocol — use the `minion` CLI via Bash tool:",
                f"- Check inbox: minion check-inbox --agent {name}",
                f"- Send message: minion send --from {name} --to <recipient> --message '...'",
                f"- Set status: minion set-status --agent {name} --status '...'",
                f"- Set context: minion set-context --agent {name} --context '...'",
                f"- View agents: minion who",
                "- All minion commands output JSON. Use Bash tool to run them.",
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
        env["MINION_CLASS"] = self.agent_cfg.role or "coder"

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.config.project_dir),
                stdin=subprocess.DEVNULL,
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

        # Raw stream log — full stream-json for context inspection
        stream_log = self.config.logs_dir / f"{self.agent_name}.stream.jsonl"
        stream_fp = open(stream_log, "a")

        timed_out = False
        compaction_detected = False
        last_output_at = time.monotonic()
        displayed_chars = 0
        hidden_chars = 0
        total_input_tokens = 0
        total_output_tokens = 0

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
            stream_fp.write(line)
            stream_fp.flush()
            rendered, has_compaction = self._render_stream_line(line)

            # Extract token usage from stream-json (last value wins —
            # result event comes last with full totals including cache)
            inp, out = self._extract_usage(line)
            if inp > 0:
                total_input_tokens = inp
            if out > 0:
                total_output_tokens = out

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

        stream_fp.close()

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
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
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

    def _extract_usage(self, line: str) -> Tuple[int, int]:
        """Extract token usage from a stream-json line. Returns (input_tokens, output_tokens).

        Claude Code stream-json reports tokens split across fields:
        - input_tokens: non-cached prompt tokens (often tiny)
        - cache_creation_input_tokens: system prompt tokens being cached
        - cache_read_input_tokens: system prompt tokens read from cache
        Total context consumed = input + cache_creation + cache_read.

        The 'result' event also has modelUsage with contextWindow — we extract
        that to set the HP limit accurately.
        """
        raw = line.strip()
        if not raw or "tokens" not in raw:
            return 0, 0
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return 0, 0
        if not isinstance(data, dict):
            return 0, 0

        # Prefer modelUsage from result event — it has contextWindow too
        if data.get("type") == "result":
            model_usage = data.get("modelUsage")
            if isinstance(model_usage, dict):
                for model_info in model_usage.values():
                    if isinstance(model_info, dict):
                        inp = (model_info.get("inputTokens", 0) or 0) + \
                              (model_info.get("cacheCreationInputTokens", 0) or 0) + \
                              (model_info.get("cacheReadInputTokens", 0) or 0)
                        out = model_info.get("outputTokens", 0) or 0
                        # Extract context window for accurate HP limit
                        ctx_window = model_info.get("contextWindow", 0)
                        if ctx_window > 0:
                            self._context_window = ctx_window
                        return inp, out

        # Fall back to usage dict in assistant/message events
        usage = self._find_usage_dict(data)
        if not usage:
            return 0, 0
        inp = (usage.get("input_tokens", 0) or 0) + \
              (usage.get("cache_creation_input_tokens", 0) or 0) + \
              (usage.get("cache_read_input_tokens", 0) or 0)
        out = usage.get("output_tokens", 0) or 0
        return inp, out

    def _find_usage_dict(self, obj: Any) -> Optional[dict]:
        """Recursively find a dict containing 'input_tokens' in a JSON structure."""
        if not isinstance(obj, dict):
            return None
        if "input_tokens" in obj:
            return obj
        for v in obj.values():
            if isinstance(v, dict):
                found = self._find_usage_dict(v)
                if found:
                    return found
        return None

    def _estimate_tool_overhead(self) -> int:
        """Estimate Claude Code system prompt + tool definition token overhead."""
        total = CLAUDE_CODE_SYSTEM_TOKENS + CLAUDE_CODE_PROJECT_OVERHEAD

        allowed = self.agent_cfg.allowed_tools
        if allowed:
            # Parse allowed tools list — e.g. "Bash Edit Read Glob Grep"
            tool_names = [t.split("(")[0].strip() for t in allowed.replace(",", " ").split()]
            for name in tool_names:
                total += CLAUDE_CODE_TOOL_TOKENS.get(name, 300)  # 300 default for unknown tools
        else:
            # All tools enabled — sum everything
            total += sum(CLAUDE_CODE_TOOL_TOKENS.values())

        return total

    def _update_hp(
        self, input_tokens: int, output_tokens: int,
        turn_input: int | None = None, turn_output: int | None = None,
    ) -> None:
        """Call minion update-hp to write observed HP to SQLite."""
        # Use API-reported context window, fall back to 200k default
        limit = self._context_window if self._context_window > 0 else 200_000
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env["MINION_CLASS"] = "lead"  # Daemon has permission to write HP
        cmd = [
            "minion", "update-hp",
            "--agent", self.agent_name,
            "--input-tokens", str(input_tokens),
            "--output-tokens", str(output_tokens),
            "--limit", str(limit),
        ]
        if turn_input is not None:
            cmd.extend(["--turn-input", str(turn_input)])
        if turn_output is not None:
            cmd.extend(["--turn-output", str(turn_output)])
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=env)
        except Exception as exc:
            self._log(f"update-hp failed: {exc}")

    def _print_stream_start(self, command_name: str) -> None:
        self._invocation += 1
        ts = datetime.now().strftime("%H:%M:%S")
        print(
            f"\n=== model-stream start: agent={self.agent_name} cmd={command_name} v={self._invocation} ts={ts} ===",
            flush=True,
        )

    def _print_stream_end(self, command_name: str, displayed_chars: int, hidden_chars: int) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        if hidden_chars > 0:
            print(f"\n[model-stream abbreviated: {hidden_chars} chars hidden]", flush=True)
        print(
            f"=== model-stream end: agent={self.agent_name} cmd={command_name} v={self._invocation} ts={ts} shown={displayed_chars} chars ===",
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

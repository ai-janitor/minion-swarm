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
from .watcher import DeadDropMessage, DeadDropWatcher


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

        self.watcher = DeadDropWatcher(agent_name, config.dead_drop_db)
        self.buffer = RollingBuffer(self.agent_cfg.max_history_tokens)

        self.inject_history_next_turn = False
        self.consecutive_failures = 0
        self.last_error: Optional[str] = None

        self._stop_event = threading.Event()

        self.state_path = self.config.state_dir / f"{self.agent_name}.json"
        self.resume_ready = self._load_resume_ready()

    def run(self) -> None:
        self.config.ensure_runtime_dirs()
        self.watcher.start()
        self.watcher.register_agent(
            role=self.agent_cfg.role,
            description=f"claude-swarm daemon agent ({self.agent_cfg.zone})",
            status="online",
        )

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        self._log(f"starting daemon for {self.agent_name}")
        self._log(f"provider: {self.agent_cfg.provider} (resume_ready={self.resume_ready})")
        self._log(f"watching dead-drop DB: {self.config.dead_drop_db}")
        self._write_state("idle")

        try:
            while not self._stop_event.is_set():
                message = self.watcher.pop_next_message()

                if message is None:
                    self.watcher.set_agent_status("idle")
                    self._write_state("idle")
                    self.watcher.wait_for_update(timeout=5.0)
                    continue

                self.watcher.set_agent_status("working")
                self._write_state(
                    "working",
                    current_message_id=message.id,
                    from_agent=message.from_agent,
                    received_at=message.timestamp,
                )
                self._log(f"processing message {message.id} from {message.from_agent}")

                ok = self._process_message(message)
                if ok:
                    self.consecutive_failures = 0
                    self.last_error = None
                    self.watcher.set_agent_status("online")
                    self._write_state("idle", last_message_id=message.id)
                    continue

                self.consecutive_failures += 1
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
                self._log(
                    f"failure #{self.consecutive_failures}; "
                    f"backing off {backoff}s ({self.last_error or 'unknown error'})"
                )

                if self.consecutive_failures >= 3:
                    self._alert_lead()

                self._stop_event.wait(timeout=float(backoff))

        finally:
            self.watcher.set_agent_status("offline")
            self._write_state("stopped")
            self.watcher.stop()
            self._log("daemon stopped")

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        self._log(f"received signal {signum}, shutting down")
        self._stop_event.set()

    def _process_message(self, message: DeadDropMessage) -> bool:
        prompt = self._build_prompt(message)
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

    def _build_prompt(self, message: DeadDropMessage) -> str:
        sections: List[str] = [self.agent_cfg.system.strip()]

        if self.inject_history_next_turn and len(self.buffer) > 0:
            sections.append(
                "\n".join(
                    [
                        "════════════════════ RECENT HISTORY (rolling buffer) ════════════════════",
                        "The following is your captured stream-json history from before compaction.",
                        "Use it to restore recent context and avoid redoing completed work.",
                        "══════════════════════════════════════════════════════════════════════════",
                        self.buffer.snapshot(),
                        "═══════════════════════ END RECENT HISTORY ═════════════════════════════",
                    ]
                )
            )
            self.inject_history_next_turn = False

        sections.append(
            "\n".join(
                [
                    "Autonomous daemon rules:",
                    "- Do not use AskUserQuestion.",
                    "- Route questions to lead via dead-drop send.",
                    "- Execute exactly the incoming task.",
                    "- Send one summary message when done.",
                ]
            )
        )

        sections.append(
            "\n".join(
                [
                    "Incoming dead-drop message:",
                    f"- id: {message.id}",
                    f"- from: {message.from_agent}",
                    f"- timestamp: {message.timestamp}",
                    f"- broadcast: {message.is_broadcast}",
                    "",
                    message.content,
                ]
            )
        )

        return "\n\n".join(s for s in sections if s.strip())

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
            "--continue",
        ]
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

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.config.project_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            self._log(f"command not found: {cmd[0]}")
            return AgentRunResult(
                exit_code=127,
                timed_out=False,
                compaction_detected=False,
                command_name=cmd[0],
            )
        except Exception as exc:
            self._log(f"failed to launch {cmd[0]}: {exc}")
            return AgentRunResult(
                exit_code=127,
                timed_out=False,
                compaction_detected=False,
                command_name=cmd[0],
            )

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
                print(rendered, end="", flush=True)
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

    def _alert_lead(self) -> None:
        lead = self.watcher.find_lead_agent() or "lead"
        content = (
            f"claude-swarm alert: agent {self.agent_name} has {self.consecutive_failures} "
            f"consecutive failures. Last error: {self.last_error or 'unknown'}."
        )
        try:
            self.watcher.send_message(self.agent_name, lead, content)
            self._log(f"alerted lead '{lead}' about repeated failures")
        except Exception as exc:  # pragma: no cover
            self._log(f"failed to alert lead '{lead}': {exc}")

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

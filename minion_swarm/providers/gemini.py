from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional

from .base import BaseProvider


class GeminiProvider(BaseProvider):
    """Gemini CLI provider."""

    def build_command(self, prompt: str, use_resume: bool = False) -> List[str]:
        cmd = ["gemini", "--prompt", prompt, "--output-format", "stream-json"]
        if use_resume:
            cmd.extend(["--resume", "latest"])
        if self.agent_cfg.permission_mode:
            mode_map = {"bypassPermissions": "yolo", "acceptEdits": "auto_edit", "plan": "plan"}
            gemini_mode = mode_map.get(self.agent_cfg.permission_mode, self.agent_cfg.permission_mode)
            cmd.extend(["--approval-mode", gemini_mode])
        if self.agent_cfg.allowed_tools:
            for tool in self.agent_cfg.allowed_tools.replace(",", " ").split():
                cmd.extend(["--allowed-tools", tool])
        if self.agent_cfg.model:
            cmd.extend(["--model", self.agent_cfg.model])
        return cmd

    def prompt_guardrails(self) -> str:
        name = self.agent_name
        return "\n".join([
            f"CRITICAL IDENTITY: You are {name}. Not gemini-benchmarker, not any other name. You are {name}.",
            f"When running minion commands, always use --name {name} or --agent {name}. Never substitute another name.",
            "",
            "EXECUTION DISCIPLINE:",
            "- Run ONLY the commands listed. Do not explore, search, or investigate on your own.",
            "- After completing the listed commands, STOP. Do not look for tasks, read files, or take initiative.",
            "- Wait for messages to arrive via the daemon polling loop. You will be invoked again when there is work.",
            "- One response = one task. No chaining, no speculative exploration.",
        ])

    def filter_log_line(self, line: str, error_log: Path) -> str:
        stripped = line.rstrip("\n")
        if not stripped or len(stripped) <= 500:
            return line

        summary = self._classify_gemini_error(stripped)
        if summary:
            self._append_error_log(error_log, stripped)
            return f"[{self.agent_name}] {summary}. Full error: {error_log}\n"
        return line

    @property
    def supports_resume(self) -> bool:
        return True

    @property
    def resume_label(self) -> str:
        return "gemini --resume latest"

    def _classify_gemini_error(self, line: str) -> Optional[str]:
        """Extract error code and short message from Gemini's verbose error output."""
        # Try JSON parse first
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                err = data.get("error", {})
                if isinstance(err, dict):
                    code = err.get("code", "")
                    status = err.get("status", "")
                    msg = err.get("message", "")[:120]
                    if code or status:
                        return f"{status or 'ERROR'} ({code}) — {msg}"
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

        # Pattern match for HTTP error codes in raw text
        m = re.search(r'"code"\s*:\s*(\d{3})', line)
        status_m = re.search(r'"status"\s*:\s*"([^"]+)"', line)
        if m:
            code = m.group(1)
            status = status_m.group(1) if status_m else "ERROR"
            msg_m = re.search(r'"message"\s*:\s*"([^"]{1,120})', line)
            msg = msg_m.group(1) if msg_m else ""
            return f"{status} ({code}) — {msg}"

        # Generic large output
        summary = self._extract_error_summary(line)
        return summary

    @staticmethod
    def _append_error_log(error_log: Path, content: str) -> None:
        from datetime import datetime
        try:
            error_log.parent.mkdir(parents=True, exist_ok=True)
            with open(error_log, "a") as f:
                f.write(f"\n--- {datetime.now().isoformat()} ---\n")
                f.write(content)
                f.write("\n")
        except OSError:
            pass

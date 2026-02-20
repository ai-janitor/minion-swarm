from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional

from .base import BaseProvider


class CodexProvider(BaseProvider):
    """OpenAI Codex CLI provider."""

    def build_command(self, prompt: str, use_resume: bool = False) -> List[str]:
        cmd = ["codex", "exec"]
        if use_resume:
            cmd.extend(["resume", "--last"])
        cmd.append("--json")
        if self.agent_cfg.permission_mode == "bypassPermissions":
            cmd.extend(["-c", 'sandbox_permissions=["disk-full-read-access"]'])
        if self.agent_cfg.model:
            cmd.extend(["--model", self.agent_cfg.model])
        cmd.append(prompt)
        return cmd

    def prompt_guardrails(self) -> str:
        name = self.agent_name
        return "\n".join([
            f"You are {name}. Run only the commands listed, then stop.",
            "Do not explore the codebase or take initiative beyond the task.",
        ])

    def filter_log_line(self, line: str, error_log: Path) -> str:
        stripped = line.rstrip("\n")
        if not stripped or len(stripped) <= 500:
            return line

        summary = self._classify_codex_error(stripped)
        if summary:
            self._append_error_log(error_log, stripped)
            return f"[{self.agent_name}] {summary}. Full error: {error_log}\n"
        return line

    @property
    def supports_resume(self) -> bool:
        return True

    @property
    def resume_label(self) -> str:
        return "codex resume --last"

    def _classify_codex_error(self, line: str) -> Optional[str]:
        """Extract short error summary from Codex verbose output."""
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                err_msg = data.get("error") or data.get("message") or ""
                if isinstance(err_msg, str) and err_msg:
                    return f"CODEX_ERROR — {err_msg[:120]}"
                if isinstance(err_msg, dict):
                    return f"CODEX_ERROR — {err_msg.get('message', '')[:120]}"
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

        # Pattern: "capacity exhausted", "rate limit", etc.
        m = re.search(r'(capacity\s+exhausted|rate\s*limit|overloaded)', line, re.IGNORECASE)
        if m:
            return f"CODEX_ERROR — {m.group(1)}"

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

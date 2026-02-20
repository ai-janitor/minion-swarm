from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional


class BaseProvider(ABC):
    """Common interface for all agent CLI providers (claude, gemini, codex, opencode)."""

    def __init__(self, agent_name: str, agent_cfg, use_poll: bool) -> None:
        self.agent_name = agent_name
        self.agent_cfg = agent_cfg
        self.use_poll = use_poll

    @abstractmethod
    def build_command(self, prompt: str, use_resume: bool = False) -> List[str]:
        ...

    @abstractmethod
    def prompt_guardrails(self) -> str:
        ...

    def filter_log_line(self, line: str, error_log: Path) -> str:
        """Parse raw output line, return cleaned version for tmux pane.

        Default: return as-is. Override for providers that dump verbose errors.
        """
        return line

    @property
    def supports_resume(self) -> bool:
        return True

    @property
    def resume_label(self) -> str:
        return ""

    # shared helpers

    @staticmethod
    def _extract_error_summary(line: str, max_normal: int = 500) -> Optional[str]:
        """If line exceeds max_normal chars, try to extract a short error summary."""
        if len(line) <= max_normal:
            return None
        # Try JSON error extraction
        try:
            import json
            data = json.loads(line)
            if isinstance(data, dict):
                code = data.get("error", {}).get("code") or data.get("code") or data.get("status")
                msg = data.get("error", {}).get("message") or data.get("message") or ""
                if code or msg:
                    return f"{code or 'ERROR'}: {msg[:120]}"
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
        # Try HTTP status code pattern
        m = re.search(r'\b([45]\d{2})\b', line[:200])
        if m:
            return f"HTTP {m.group(1)} (response truncated, {len(line)} chars)"
        return f"Large output ({len(line)} chars)"

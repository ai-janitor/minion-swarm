from __future__ import annotations

from typing import List

from .base import BaseProvider


class ClaudeProvider(BaseProvider):
    """Claude Code CLI provider."""

    def build_command(self, prompt: str, use_resume: bool = False) -> List[str]:
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
        if not self.use_poll:
            cmd.append("--continue")
        if self.agent_cfg.allowed_tools:
            cmd.extend(["--allowed-tools", self.agent_cfg.allowed_tools])
        if self.agent_cfg.permission_mode:
            cmd.extend(["--permission-mode", self.agent_cfg.permission_mode])
        if self.agent_cfg.model:
            cmd.extend(["--model", self.agent_cfg.model])
        return cmd

    def prompt_guardrails(self) -> str:
        # Claude follows instructions well — minimal guardrails needed
        return ""

    @property
    def supports_resume(self) -> bool:
        return False

    @property
    def resume_label(self) -> str:
        return ""

"""Provider registry — maps provider name to concrete BaseProvider subclass."""
from __future__ import annotations

from .base import BaseProvider
from .claude import ClaudeProvider
from .codex import CodexProvider
from .gemini import GeminiProvider
from .opencode import OpencodeProvider

__all__ = [
    "BaseProvider",
    "ClaudeProvider",
    "CodexProvider",
    "GeminiProvider",
    "OpencodeProvider",
    "get_provider",
]

_REGISTRY: dict[str, type[BaseProvider]] = {
    "claude": ClaudeProvider,
    "codex": CodexProvider,
    "gemini": GeminiProvider,
    "opencode": OpencodeProvider,
}


def get_provider(name: str, agent_name: str, agent_cfg, use_poll: bool) -> BaseProvider:
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown provider '{name}'. Available: {sorted(_REGISTRY)}")
    return cls(agent_name=agent_name, agent_cfg=agent_cfg, use_poll=use_poll)

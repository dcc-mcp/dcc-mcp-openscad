from __future__ import annotations

from typing import Any, Callable

from dcc_mcp_core.skill import skill_entry, skill_success

from .bridge import get_bridge


def bridge_main(method: str, message: str) -> Callable[..., dict[str, Any]]:
    """Create one declarative skill entry point for a typed CLI method."""

    @skill_entry
    def main(**kwargs: Any) -> dict[str, Any]:
        result = getattr(get_bridge(), method)(**kwargs)
        checks = result.pop("verified", None)
        if isinstance(checks, list):
            return skill_success(
                message, verified=bool(checks), verification_checks=checks, **result
            )
        return skill_success(message, **result)

    return main

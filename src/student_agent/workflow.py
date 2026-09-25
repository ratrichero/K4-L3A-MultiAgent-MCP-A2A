from __future__ import annotations

from typing import Any

from .config import Settings
from .llm import LLMClient
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """Get or initialize singleton LLMClient configured with multi-tier fallback."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient(Settings.load())
    return _llm_client


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Implement the L3A coordinator and specialist-agent workflow here.

    The starter kit intentionally does not generate a fallback answer: submitting an
    invented answer or evidence reference would violate the competition contract.
    """
    del case, gateway, trace
    raise NotImplementedError("Implement the L3A multi-agent workflow in solve_case()")

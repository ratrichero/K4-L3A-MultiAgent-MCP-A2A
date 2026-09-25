from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..mcp_gateway import EvidenceGateway
from .ledger import EvidenceLedger

logger = logging.getLogger(__name__)


class ScopedEvidenceGateway:
    """Application-level wrapper around EvidenceGateway enforcing least privilege.

    Responsibilities:
      1. Verifies tool_name against agent's specific allowed_tools.
      2. Ingests all returned evidence into the case's EvidenceLedger.
      3. Implements 1-retry with backoff for transient network/timeout errors.
    """

    def __init__(
        self,
        gateway: EvidenceGateway,
        ledger: EvidenceLedger,
        agent_name: str,
        allowed_tools: set[str],
    ) -> None:
        self._gateway = gateway
        self._ledger = ledger
        self.agent_name = agent_name
        self.allowed_tools = allowed_tools

    async def call(
        self,
        tool_name: str,
        *,
        case_id: str,
        max_retries: int = 1,
        backoff_seconds: float = 1.0,
        **arguments: str,
    ) -> dict[str, Any]:
        """Call MCP tool with permission check, retry, and ledger ingest."""
        if tool_name not in self.allowed_tools:
            raise PermissionError(
                f"Agent '{self.agent_name}' is not authorized to call MCP tool '{tool_name}'. "
                f"Allowed tools: {sorted(self.allowed_tools)}"
            )

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                evidence = await self._gateway.call(tool_name, case_id=case_id, **arguments)
                # Ingest into ledger
                self._ledger.record(evidence)
                return evidence
            except Exception as exc:
                last_error = exc
                if attempt < max_retries:
                    logger.warning(
                        f"MCP tool '{tool_name}' failed on attempt {attempt + 1}: {exc}. "
                        f"Retrying in {backoff_seconds}s..."
                    )
                    await asyncio.sleep(backoff_seconds)
                else:
                    if max_retries > 0:
                        logger.warning(
                            f"MCP tool '{tool_name}' failed after {max_retries + 1} attempts: {exc}"
                        )
                    else:
                        logger.debug(f"MCP tool '{tool_name}' returned error: {exc}")

        raise RuntimeError(
            f"MCP call '{tool_name}' failed for agent '{self.agent_name}': {last_error}"
        ) from last_error

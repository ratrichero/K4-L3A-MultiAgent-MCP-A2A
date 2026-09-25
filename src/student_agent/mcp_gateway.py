from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts

# Transient failures only — business not-found / schema errors are not retried here.
_RETRY_BACKOFF_SECONDS = (0.5, 1.0, 2.0)
_MAX_ATTEMPTS = 1 + len(_RETRY_BACKOFF_SECONDS)


def _result_is_error(result: Any) -> bool:
    flag = getattr(result, "is_error", None)
    if flag is None:
        flag = getattr(result, "isError", None)
    return bool(flag)


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timeout" in text:
        return True
    if "connect" in name or "readerror" in name or "read error" in text:
        return True
    if "temporarily" in text or "503" in text or "502" in text or "429" in text:
        return True
    return False


class EvidenceGateway:
    """MCP Evidence Gateway client.

    Hard rules enforced here:
    1. Every call includes the caller-supplied ``case_id`` (server rejects otherwise).
    2. ``evidence_ref`` is returned verbatim from MCP — never invented or rewritten.
    3. Responses are schema-validated before use.
    """

    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._discovered: list[str] | None = None

    async def list_tools(self) -> list[str]:
        response = await self._session.list_tools()
        self._discovered = sorted(tool.name for tool in response.tools)
        return list(self._discovered)

    async def ensure_tools(self) -> list[str]:
        if self._discovered is None:
            return await self.list_tools()
        return list(self._discovered)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        if not case_id:
            raise ValueError("case_id is required for every MCP call")
        payload = {"case_id": case_id, **arguments}
        result = await self._session.call_tool(tool_name, arguments=payload)
        if _result_is_error(result):
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = getattr(result, "structured_content", None)
        if evidence is None:
            evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        # Never mutate evidence_ref — return MCP payload as-is.
        return evidence

    async def call_with_retry(
        self, tool_name: str, *, case_id: str, **arguments: str
    ) -> dict[str, Any]:
        """Idempotent retry for transient transport / MCP errors (max 3 retries)."""
        last_error: BaseException | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                return await self.call(tool_name, case_id=case_id, **arguments)
            except Exception as exc:  # noqa: BLE001 — classify then re-raise
                last_error = exc
                if attempt >= len(_RETRY_BACKOFF_SECONDS) or not _is_transient(exc):
                    raise
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS[attempt])
        assert last_error is not None
        raise last_error


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)

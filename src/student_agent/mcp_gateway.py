from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


@dataclass(frozen=True)
class ToolDefinition:
    """The discovery metadata needed to call an MCP tool safely."""

    name: str
    description: str
    input_schema: dict[str, Any]


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._tools: dict[str, ToolDefinition] | None = None

    async def list_tool_definitions(self) -> tuple[ToolDefinition, ...]:
        if self._tools is not None:
            return tuple(sorted(self._tools.values(), key=lambda tool: tool.name))
        response = await self._session.list_tools()
        definitions = []
        for tool in response.tools:
            schema = getattr(tool, "inputSchema", None)
            if schema is None:
                schema = getattr(tool, "input_schema", None)
            definitions.append(
                ToolDefinition(
                    name=tool.name,
                    description=getattr(tool, "description", "") or "",
                    input_schema=deepcopy(schema) if isinstance(schema, dict) else {},
                )
            )
        self._tools = {tool.name: tool for tool in definitions}
        return tuple(sorted(definitions, key=lambda tool: tool.name))

    async def list_tools(self) -> list[str]:
        return [tool.name for tool in await self.list_tool_definitions()]

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        if not case_id:
            raise ValueError("case_id is required for every MCP call")
        if self._tools is not None and tool_name not in self._tools:
            raise ValueError(f"MCP tool was not discovered: {tool_name}")
        payload = {"case_id": case_id, **arguments}
        result = await self._session.call_tool(tool_name, arguments=payload)
        is_error = getattr(result, "is_error", None)
        if is_error is None:
            is_error = getattr(result, "isError", False)
        if is_error:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        # Isolate the validated server envelope from MCP model internals.  The workflow
        # treats the returned evidence_ref as opaque and never derives or rewrites it.
        return deepcopy(evidence)


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

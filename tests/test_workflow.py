from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway, ToolDefinition
from student_agent.trace import TraceWriter
from student_agent.workflow import EvidenceLedger, _verify, solve_case


def evidence(ref_suffix: str, domain: str, data: Any) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": f"ev_{ref_suffix:0<20}",
        "result_hash": f"sha256:{'a' * 64}",
        "domain": domain,
        "data": data,
    }


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        schema = lambda field: {  # noqa: E731
            "type": "object",
            "properties": {"case_id": {"type": "string"}, field: {"type": "string"}},
            "required": ["case_id", field],
        }
        self.tools = (
            # Discovery is alphabetical, so dependent tools may appear before get_order.
            ToolDefinition(
                "get_customer_history", "customer history", schema("customer_unique_id")
            ),
            ToolDefinition("get_order", "order record", schema("order_id")),
            ToolDefinition("get_order_items", "item records", schema("order_id")),
            ToolDefinition("get_order_payments", "payment records", schema("order_id")),
            ToolDefinition("get_policy", "policy rule", schema("policy_version")),
        )
        self.responses = {
            "get_order": evidence(
                "order",
                "order",
                {"order_status": "canceled", "customer_unique_id": "CUSTOMER-1"},
            ),
            "get_order_items": evidence(
                "items", "item", [{"item_id": "ITEM-1", "price": 100, "freight_value": 10}]
            ),
            "get_order_payments": evidence(
                "payment", "payment", [{"payment_reference": "PAY-1", "payment_value": 110}]
            ),
            "get_customer_history": evidence(
                "customer", "customer", {"customer_unique_id": "CUSTOMER-1"}
            ),
            "get_policy": evidence("policy", "policy", {"refund_required": True}),
        }

    async def list_tool_definitions(self) -> tuple[ToolDefinition, ...]:
        return self.tools

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        assert case_id == "L3A_CASE_001"
        self.calls.append((tool_name, case_id, arguments))
        return self.responses[tool_name]


def contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


def test_specialists_only_submit_gateway_refs_and_emit_consumption(tmp_path: Path) -> None:
    gateway = FakeGateway()
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts())
    result = asyncio.run(
        solve_case(
            {
                "case_id": "L3A_CASE_001",
                "customer_request": {
                    "language": "vi",
                    "message": "Kiểm tra đơn hàng",
                    "claimed_order_id": "ORDER-1",
                    "claims": [{"claim_id": "claim-001-a", "topic": "canceled_order_paid"}],
                },
                "policy_version": "EC_POLICY_V1",
            },
            gateway,  # type: ignore[arg-type]
            trace,
        )
    )

    contracts().validate_output(result, "result")
    assert result["assessment"]["primary_issue"] == "canceled_order_paid"
    assert result["financial_resolution"]["recommended_refund_brl"] == 110.0
    assert set(result["evidence_refs"]) == {
        gateway.responses[name]["evidence_ref"]
        for name in ("get_order", "get_order_payments", "get_policy")
    }
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    consumed = [event for event in events if event["event_type"] == "tool_result_consumed"]
    assert set(result["evidence_refs"]).issubset({event["evidence_refs"][0] for event in consumed})
    assert result["claim_assessments"][0]["claim_id"] == "claim-001-a"
    assert all(call[1] == "L3A_CASE_001" for call in gateway.calls)
    arguments = {tool_name: values for tool_name, _, values in gateway.calls}
    assert arguments["get_order"] == {"order_id": "ORDER-1"}
    assert arguments["get_customer_history"] == {"customer_unique_id": "CUSTOMER-1"}
    assert arguments["get_policy"] == {"policy_version": "EC_POLICY_V1"}
    assert any(event["event_type"] == "policy_decided" for event in events)


def test_verifier_rejects_invented_evidence_ref() -> None:
    ledger = EvidenceLedger("CASE_001")
    with pytest.raises(ValueError, match="not returned"):
        _verify(
            {
                "evidence_refs": ["ev_this_reference_was_invented"],
                "claim_assessments": [],
                "financial_resolution": {
                    "recommended_refund_brl": 0,
                    "refund_lines": [],
                },
            },
            ledger,
        )


def test_trace_writer_requires_tool_and_evidence_for_consumption(tmp_path: Path) -> None:
    invalid = {
        "schema_version": "day09-trace-event-v1",
        "event_id": "evt_123456789012",
        "case_id": "CASE_001",
        "event_type": "tool_result_consumed",
        "occurred_at": "2026-09-25T00:00:00Z",
        "actor": "order-item-agent",
    }
    # The public contract remains unchanged; the stricter invariant belongs to the client.
    contracts().validate_trace(invalid, "public-contract-valid event")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts())
    with pytest.raises(ValueError, match="requires tool_name"):
        trace.emit(
            case_id="CASE_001",
            event_type="tool_result_consumed",
            actor="order-item-agent",
        )


def test_evidence_gateway_preserves_ref_and_injects_case_id() -> None:
    class Session:
        def __init__(self) -> None:
            self.arguments: dict[str, Any] = {}

        async def list_tools(self) -> Any:
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="get_order",
                        description="order",
                        inputSchema={"type": "object"},
                    )
                ]
            )

        async def call_tool(self, _name: str, *, arguments: dict[str, Any]) -> Any:
            self.arguments = arguments
            return SimpleNamespace(
                is_error=False,
                structuredContent=evidence("opaque-server-ref", "order", {"ok": True}),
                content=[],
            )

    session = Session()
    gateway = EvidenceGateway(session, contracts())  # type: ignore[arg-type]

    async def call() -> dict[str, Any]:
        await gateway.list_tool_definitions()
        return await gateway.call("get_order", case_id="CASE_001", order_id="ORDER-1")

    result = asyncio.run(call())
    assert session.arguments == {"case_id": "CASE_001", "order_id": "ORDER-1"}
    assert result["evidence_ref"] == evidence("opaque-server-ref", "order", {})["evidence_ref"]

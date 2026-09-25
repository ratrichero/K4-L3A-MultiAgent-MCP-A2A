from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

# Least-privilege tool allowlists (intersected with gateway.list_tools at runtime).
TOOL_ALLOWLIST: dict[str, frozenset[str]] = {
    "order-agent": frozenset(
        {
            "get_order",
            "get_order_items",
            "get_sellers",
            "get_product_context",
            "get_customer_history",
        }
    ),
    "payment-agent": frozenset(
        {
            "get_order_payments",
            "get_payment_timeline",
            "get_refund_timeline",
        }
    ),
    "shipment-agent": frozenset({"get_shipment_summary"}),
    "policy-agent": frozenset({"get_policy"}),
}


@dataclass
class EvidenceBundle:
    """Authoritative MCP evidence collected for one case. Refs are MCP-issued only."""

    case_id: str
    by_ref: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_tool: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: list[dict[str, str]] = field(default_factory=list)

    def add(self, tool_name: str, evidence: dict[str, Any]) -> str:
        ref = evidence["evidence_ref"]
        if not isinstance(ref, str) or not ref.startswith("ev_"):
            raise ValueError(f"invalid evidence_ref from MCP tool {tool_name}")
        # Store verbatim — never rewrite evidence_ref / result_hash.
        self.by_ref[ref] = evidence
        self.by_tool[tool_name] = evidence
        return ref

    @property
    def refs(self) -> list[str]:
        return list(self.by_ref.keys())

    def data(self, tool_name: str) -> Any | None:
        evidence = self.by_tool.get(tool_name)
        return None if evidence is None else evidence.get("data")


async def consume_tool(
    *,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    actor: str,
    tool_name: str,
    case_id: str,
    bundle: EvidenceBundle,
    **arguments: str,
) -> dict[str, Any] | None:
    """Call MCP within the actor allowlist, then emit ``tool_result_consumed``.

    Returns None on failure (recorded in ``bundle.failures``); never fabricates refs.
    """
    allowed = TOOL_ALLOWLIST.get(actor, frozenset())
    discovered = set(await gateway.ensure_tools())
    if tool_name not in allowed:
        bundle.failures.append(
            {"tool": tool_name, "actor": actor, "reason": "TOOL_NOT_IN_ALLOWLIST"}
        )
        return None
    if tool_name not in discovered:
        bundle.failures.append(
            {"tool": tool_name, "actor": actor, "reason": "TOOL_NOT_DISCOVERED"}
        )
        return None

    try:
        evidence = await gateway.call_with_retry(
            tool_name, case_id=case_id, **arguments
        )
    except Exception as exc:  # noqa: BLE001 — surface as audit failure, do not invent data
        bundle.failures.append(
            {
                "tool": tool_name,
                "actor": actor,
                "reason": type(exc).__name__,
                "message": str(exc)[:160],
            }
        )
        return None

    evidence_ref = bundle.add(tool_name, evidence)
    # Principle 4: only emit consume after a real MCP success with real ref.
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[evidence_ref],
    )
    return evidence


async def run_order_agent(
    *,
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    bundle: EvidenceBundle,
    order_id: str,
) -> None:
    actor = "order-agent"
    case_id = case["case_id"]
    order_ev = await consume_tool(
        gateway=gateway,
        trace=trace,
        actor=actor,
        tool_name="get_order",
        case_id=case_id,
        bundle=bundle,
        order_id=order_id,
    )
    await consume_tool(
        gateway=gateway,
        trace=trace,
        actor=actor,
        tool_name="get_order_items",
        case_id=case_id,
        bundle=bundle,
        order_id=order_id,
    )
    await consume_tool(
        gateway=gateway,
        trace=trace,
        actor=actor,
        tool_name="get_sellers",
        case_id=case_id,
        bundle=bundle,
        order_id=order_id,
    )
    await consume_tool(
        gateway=gateway,
        trace=trace,
        actor=actor,
        tool_name="get_product_context",
        case_id=case_id,
        bundle=bundle,
        order_id=order_id,
    )
    customer_id = None
    if isinstance(order_ev, dict):
        data = order_ev.get("data") or {}
        if isinstance(data, dict):
            customer_id = data.get("customer_id") or data.get("customer_unique_id")
    if isinstance(customer_id, str) and customer_id:
        await consume_tool(
            gateway=gateway,
            trace=trace,
            actor=actor,
            tool_name="get_customer_history",
            case_id=case_id,
            bundle=bundle,
            customer_unique_id=customer_id,
        )


async def run_payment_agent(
    *,
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    bundle: EvidenceBundle,
    order_id: str,
) -> None:
    actor = "payment-agent"
    case_id = case["case_id"]
    for tool_name in ("get_order_payments", "get_payment_timeline", "get_refund_timeline"):
        await consume_tool(
            gateway=gateway,
            trace=trace,
            actor=actor,
            tool_name=tool_name,
            case_id=case_id,
            bundle=bundle,
            order_id=order_id,
        )


async def run_shipment_agent(
    *,
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    bundle: EvidenceBundle,
    order_id: str,
) -> None:
    await consume_tool(
        gateway=gateway,
        trace=trace,
        actor="shipment-agent",
        tool_name="get_shipment_summary",
        case_id=case["case_id"],
        bundle=bundle,
        order_id=order_id,
    )


async def run_policy_agent(
    *,
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    bundle: EvidenceBundle,
) -> None:
    policy_version = case.get("policy_version") or "EC_POLICY_V1"
    evidence = await consume_tool(
        gateway=gateway,
        trace=trace,
        actor="policy-agent",
        tool_name="get_policy",
        case_id=case["case_id"],
        bundle=bundle,
        policy_version=str(policy_version),
    )
    decision = "POLICY_LOADED" if evidence is not None else "POLICY_UNAVAILABLE"
    trace.emit(
        case_id=case["case_id"],
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=decision,
        evidence_refs=[evidence["evidence_ref"]] if evidence else None,
        attributes={"policy_version": str(policy_version)},
    )

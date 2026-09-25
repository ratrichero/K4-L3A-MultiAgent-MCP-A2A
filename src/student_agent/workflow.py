from __future__ import annotations

from typing import Any

from .agents import (
    EvidenceBundle,
    run_order_agent,
    run_payment_agent,
    run_policy_agent,
    run_shipment_agent,
)
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

_PRIMARY_ISSUES = frozenset(
    {
        "canceled_order_paid",
        "unavailable_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "unsupported_claim",
        "insufficient_evidence",
    }
)

_SPECIALISTS = (
    ("order-agent", "NEED_ORDER_EVIDENCE", run_order_agent),
    ("payment-agent", "NEED_PAYMENT_EVIDENCE", run_payment_agent),
    ("shipment-agent", "NEED_SHIPMENT_EVIDENCE", run_shipment_agent),
)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _unique_strs(values: list[Any], *, limit: int = 20) -> list[str]:
    seen: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        if value not in seen:
            seen.append(value)
        if len(seen) >= limit:
            break
    return seen


def _money(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _claim_topic(case: dict[str, Any]) -> str | None:
    request = case.get("customer_request") or {}
    for claim in _as_list(request.get("claims")):
        if not isinstance(claim, dict):
            continue
        topic = claim.get("topic")
        if isinstance(topic, str) and topic in _PRIMARY_ISSUES:
            return topic
    return None


def _infer_primary_issue(bundle: EvidenceBundle, claimed: str | None) -> str:
    order = bundle.data("get_order") or {}
    status = str(order.get("order_status") or "").lower() if isinstance(order, dict) else ""

    shipment = bundle.data("get_shipment_summary") or {}
    events = _as_list(shipment.get("events") if isinstance(shipment, dict) else None)
    late_actors = {
        str(event.get("actor") or "").lower()
        for event in events
        if isinstance(event, dict)
        and str(event.get("event_type") or "").lower() in {"delivered_late", "late_delivery"}
    }

    refund = bundle.data("get_refund_timeline") or {}
    refund_events = _as_list(refund.get("events") if isinstance(refund, dict) else None)
    refund_types = {
        str(event.get("event_type") or "").lower()
        for event in refund_events
        if isinstance(event, dict)
    }

    payments = bundle.data("get_order_payments")
    payment_rows = _as_list(payments if isinstance(payments, list) else None)
    if not payment_rows and isinstance(bundle.data("get_payment_timeline"), dict):
        payment_rows = _as_list(bundle.data("get_payment_timeline").get("payments"))

    paid = any(_money(row.get("payment_value")) > 0 for row in payment_rows if isinstance(row, dict))

    if "refund_failed" in refund_types or "failed" in refund_types:
        return "refund_failed"
    if "refund_pending" in refund_types or "pending" in refund_types:
        return "refund_pending"
    if status in {"canceled", "cancelled"} and paid:
        return "canceled_order_paid"
    if status in {"unavailable", "canceled"} and claimed == "unavailable_order_paid":
        return "unavailable_order_paid"
    if "seller" in late_actors:
        return "late_delivery_seller"
    if late_actors & {"logistics", "logistics_provider", "carrier"}:
        return "late_delivery_logistics"
    if claimed in _PRIMARY_ISSUES:
        return claimed
    if not bundle.refs:
        return "insufficient_evidence"
    return "unsupported_claim"


def _supporting_refs(bundle: EvidenceBundle, primary_issue: str) -> list[str]:
    """Cite only tools that actually support the conclusion (relevance)."""
    preferred: list[str] = ["get_order", "get_policy"]
    if primary_issue in {
        "canceled_order_paid",
        "unavailable_order_paid",
        "payment_mismatch",
        "duplicate_charge",
        "valid_split_payment",
        "refund_pending",
        "refund_failed",
    }:
        preferred.extend(
            ["get_order_payments", "get_payment_timeline", "get_refund_timeline", "get_order_items"]
        )
    if primary_issue in {"late_delivery_seller", "late_delivery_logistics"}:
        preferred.extend(["get_shipment_summary", "get_order_items", "get_sellers"])
    if primary_issue == "unsupported_claim":
        preferred.extend(["get_order", "get_shipment_summary", "get_order_payments"])

    refs: list[str] = []
    for tool_name in preferred:
        evidence = bundle.by_tool.get(tool_name)
        if evidence is None:
            continue
        ref = evidence["evidence_ref"]
        if ref not in refs:
            refs.append(ref)
    if not refs:
        refs = list(bundle.refs)[:10]
    return refs[:30]


def _extract_entities(bundle: EvidenceBundle, order_id: str) -> dict[str, list[str]]:
    order_ids = [order_id] if order_id else []
    item_ids: list[str] = []
    seller_ids: list[str] = []
    payment_refs: list[str] = []
    shipment_ids: list[str] = []

    items = bundle.data("get_order_items")
    for row in _as_list(items if isinstance(items, list) else None):
        if not isinstance(row, dict):
            continue
        if row.get("order_item_id"):
            item_ids.append(str(row["order_item_id"]))
        if row.get("seller_id"):
            seller_ids.append(str(row["seller_id"]))

    sellers = bundle.data("get_sellers")
    for row in _as_list(sellers if isinstance(sellers, list) else None):
        if isinstance(row, dict) and row.get("seller_id"):
            seller_ids.append(str(row["seller_id"]))

    payments = bundle.data("get_order_payments")
    rows = _as_list(payments if isinstance(payments, list) else None)
    timeline = bundle.data("get_payment_timeline")
    if not rows and isinstance(timeline, dict):
        rows = _as_list(timeline.get("payments"))
    for row in rows:
        if not isinstance(row, dict):
            continue
        sequential = row.get("payment_sequential")
        ptype = row.get("payment_type")
        if sequential is not None:
            payment_refs.append(f"{order_id}:{sequential}:{ptype or 'payment'}")

    shipment = bundle.data("get_shipment_summary")
    if isinstance(shipment, dict):
        if shipment.get("shipment_id"):
            shipment_ids.append(str(shipment["shipment_id"]))
        elif order_id:
            shipment_ids.append(f"ship-{order_id[:12]}")

    return {
        "order_ids": _unique_strs(order_ids),
        "item_ids": _unique_strs(item_ids),
        "seller_ids": _unique_strs(seller_ids),
        "payment_references": _unique_strs(payment_refs),
        "shipment_ids": _unique_strs(shipment_ids),
    }


def _policy_rule(bundle: EvidenceBundle, primary_issue: str) -> dict[str, Any] | None:
    policy = bundle.data("get_policy")
    if not isinstance(policy, dict):
        return None
    rules = policy.get("rules")
    if not isinstance(rules, dict):
        return None
    rule = rules.get(primary_issue)
    return rule if isinstance(rule, dict) else None


def _build_output(case: dict[str, Any], bundle: EvidenceBundle, order_id: str) -> dict[str, Any]:
    claimed = _claim_topic(case)
    primary_issue = _infer_primary_issue(bundle, claimed)
    if not bundle.refs:
        primary_issue = "insufficient_evidence"

    rule = _policy_rule(bundle, primary_issue)
    refs = _supporting_refs(bundle, primary_issue)
    entities = _extract_entities(bundle, order_id)

    case_status = "needs_investigation"
    confidence = 0.35
    resolution_actions: list[str] = []
    responsible: list[dict[str, Any]] = [{"party_type": "unknown", "party_id": None}]
    refund_brl = 0.0
    refund_lines: list[dict[str, Any]] = []

    if rule:
        case_status = str(rule.get("case_status") or case_status)
        if case_status not in {"action_required", "no_action", "needs_investigation"}:
            case_status = "needs_investigation"
        action = rule.get("recommended_action")
        if isinstance(action, str) and action:
            resolution_actions = [action[:80]]
        refund_brl = max(0.0, _money(rule.get("refund_brl")))
        parties = rule.get("responsible_parties")
        if isinstance(parties, list) and parties:
            responsible = []
            for party in parties[:5]:
                if not isinstance(party, dict):
                    continue
                party_type = party.get("party_type") or "unknown"
                if party_type not in {
                    "seller",
                    "platform",
                    "logistics_provider",
                    "payment_provider",
                    "customer",
                    "unknown",
                }:
                    party_type = "unknown"
                responsible.append(
                    {
                        "party_type": party_type,
                        "party_id": party.get("party_id"),
                    }
                )
            if not responsible:
                responsible = [{"party_type": "unknown", "party_id": None}]
        confidence = 0.72 if claimed == primary_issue else 0.55
        if refund_brl > 0:
            refund_lines = [
                {
                    "reason_code": primary_issue.upper()[:80],
                    "amount_brl": refund_brl,
                    "entity_id": order_id or None,
                }
            ]
    elif primary_issue == "insufficient_evidence":
        case_status = "needs_investigation"
        confidence = 0.2
        resolution_actions = ["gather_additional_evidence"]
    elif primary_issue == "unsupported_claim":
        case_status = "no_action"
        confidence = 0.6
        resolution_actions = ["deny_unsupported_claim"]
        responsible = [{"party_type": "customer", "party_id": None}]
    else:
        case_status = "needs_investigation"
        confidence = 0.4
        resolution_actions = ["manual_review"]

    if case_status == "no_action":
        refund_brl = 0.0
        refund_lines = []

    claim_assessments: list[dict[str, Any]] = []
    request = case.get("customer_request") or {}
    for claim in _as_list(request.get("claims")):
        if not isinstance(claim, dict):
            continue
        claim_id = claim.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id:
            continue
        topic = claim.get("topic")
        if topic == primary_issue:
            verdict = "supported"
            claim_confidence = min(1.0, confidence + 0.05)
        elif topic == "requested_full_refund":
            if refund_brl > 0:
                verdict = "partially_supported" if case_status == "action_required" else "unsupported"
            else:
                verdict = "unsupported"
            claim_confidence = confidence
        elif topic in _PRIMARY_ISSUES:
            verdict = "unsupported"
            claim_confidence = max(0.2, confidence - 0.2)
        else:
            verdict = "insufficient_evidence"
            claim_confidence = 0.3
        claim_assessments.append(
            {
                "claim_id": claim_id[:64],
                "verdict": verdict,
                "confidence": round(claim_confidence, 4),
                "evidence_refs": refs[:30],
            }
        )
        if len(claim_assessments) >= 5:
            break

    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": round(confidence, 4),
        },
        "affected_entities": entities,
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_issue.upper()[:80], "rank": 1}],
            "responsible_parties": responsible,
        },
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_brl,
            "refund_lines": refund_lines,
        },
        "resolution_actions": resolution_actions[:8] or ["manual_review"],
    }


def _verify_output(
    output: dict[str, Any], bundle: EvidenceBundle, trace: TraceWriter, case_id: str
) -> dict[str, Any]:
    decision = "PASS"
    refs = set(bundle.refs)
    cited = list(output.get("evidence_refs") or [])
    # Drop any ref not actually consumed from MCP (defense in depth).
    output["evidence_refs"] = [ref for ref in cited if ref in refs][:30]
    for claim in output.get("claim_assessments") or []:
        if isinstance(claim, dict):
            claim["evidence_refs"] = [
                ref for ref in (claim.get("evidence_refs") or []) if ref in refs
            ][:30]

    finance = output.get("financial_resolution") or {}
    lines = finance.get("refund_lines") or []
    total = sum(_money(line.get("amount_brl")) for line in lines if isinstance(line, dict))
    recommended = _money(finance.get("recommended_refund_brl"))
    if lines and abs(total - recommended) > 1e-6:
        finance["recommended_refund_brl"] = round(total, 2)
        decision = "FAIL_INVARIANT"

    if not output["evidence_refs"]:
        output["assessment"]["primary_issue"] = "insufficient_evidence"
        output["assessment"]["case_status"] = "needs_investigation"
        output["assessment"]["confidence"] = min(
            float(output["assessment"].get("confidence") or 0.3), 0.25
        )
        decision = "DOWNGRADE_CONFIDENCE"

    assessment = output["assessment"]
    conf = float(assessment.get("confidence") or 0)
    assessment["confidence"] = max(0.0, min(1.0, conf))

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code=decision,
        evidence_refs=output["evidence_refs"][:20] or None,
    )
    return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator + specialist workflow with MCP evidence and audit traces."""
    case_id = case["case_id"]
    request = case.get("customer_request") or {}
    order_id = request.get("claimed_order_id")
    if not isinstance(order_id, str) or not order_id:
        order_id = ""

    bundle = EvidenceBundle(case_id=case_id)
    await gateway.ensure_tools()

    # Specialist evidence gathering (scoped tools + tool_result_consumed).
    for actor, decision_code, runner in _SPECIALISTS:
        if not order_id:
            break
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code=decision_code,
        )
        refs_before = set(bundle.refs)
        await runner(
            case=case,
            gateway=gateway,
            trace=trace,
            bundle=bundle,
            order_id=order_id,
        )
        new_refs = [ref for ref in bundle.refs if ref not in refs_before]
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=actor,
            target="coordinator",
            decision_code="EVIDENCE_READY" if new_refs else "EVIDENCE_NOT_FOUND",
            evidence_refs=new_refs[:20] or None,
        )

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="policy-agent",
        decision_code="NEED_POLICY",
    )
    await run_policy_agent(case=case, gateway=gateway, trace=trace, bundle=bundle)
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy-agent",
        target="coordinator",
        decision_code="POLICY_HANDOFF",
        evidence_refs=[bundle.by_tool["get_policy"]["evidence_ref"]]
        if "get_policy" in bundle.by_tool
        else None,
    )

    draft = _build_output(case, bundle, order_id)
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="verifier",
        decision_code="READY_FOR_VERIFICATION",
        evidence_refs=draft.get("evidence_refs") or None,
    )
    return _verify_output(draft, bundle, trace, case_id)

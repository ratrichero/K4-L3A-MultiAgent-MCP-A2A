from __future__ import annotations

from typing import Any

from ..core.ledger import EvidenceLedger
from ..core.models import DraftResolution
from .finance import round_brl, to_decimal, to_float_brl


def enforce_invariants_and_reconcile(
    draft: DraftResolution,
    ledger: EvidenceLedger,
    case_id: str,
) -> None:
    """Enforce strict domain invariants in place before mapping to L3A output."""
    # 1. Decimal calculation of refund lines
    total_lines = sum(
        (round_brl(to_decimal(line.amount_brl)) for line in draft.refund_lines), to_decimal("0.00")
    )
    draft.recommended_refund_brl = round_brl(to_decimal(draft.recommended_refund_brl))

    # Auto-reconcile totals if lines exist
    if draft.refund_lines:
        draft.recommended_refund_brl = total_lines

    # 2. Status & Refund consistency
    if draft.case_status == "no_action":
        draft.recommended_refund_brl = to_decimal("0.00")
        draft.refund_lines = []
    elif draft.recommended_refund_brl > to_decimal("0.00"):
        draft.case_status = "action_required"

    # 3. Responsibility & Primary Issue alignment
    party_types = {p.get("party_type") for p in draft.responsible_parties}
    if draft.primary_issue == "late_delivery_seller" and "seller" not in party_types:
        seller_id = draft.seller_ids[0] if draft.seller_ids else None
        draft.responsible_parties.append({"party_type": "seller", "party_id": seller_id})
    elif (
        draft.primary_issue == "late_delivery_logistics" and "logistics_provider" not in party_types
    ):
        draft.responsible_parties.append(
            {"party_type": "logistics_provider", "party_id": "carrier_partner"}
        )
    elif (
        draft.primary_issue in ("canceled_order_paid", "unavailable_order_paid")
        and not draft.responsible_parties
    ):
        draft.responsible_parties.append({"party_type": "platform", "party_id": "marketplace"})

    # 4. Action uniqueness & non-empty
    unique_actions: list[str] = []
    for action in draft.resolution_actions:
        cleaned = action.strip()
        if cleaned and cleaned not in unique_actions:
            unique_actions.append(cleaned[:80])
    if not unique_actions:
        unique_actions = (
            ["review_case_details"] if draft.case_status != "no_action" else ["close_inquiry"]
        )
    draft.resolution_actions = unique_actions[:8]

    # 5. Evidence refs ownership verification
    valid_root_refs = [r for r in draft.evidence_refs if ledger.contains(r)]
    # If no root refs provided, populate with all consumed refs from ledger
    if not valid_root_refs:
        valid_root_refs = ledger.consumed_refs()
    draft.evidence_refs = sorted(set(valid_root_refs))[:30]

    # Align claim evidence refs to be subset of root refs
    for claim in draft.claim_assessments:
        claim.evidence_refs = [r for r in claim.evidence_refs if r in draft.evidence_refs]


def map_draft_to_l3a_output(
    draft: DraftResolution,
    ledger: EvidenceLedger,
    case_id: str,
) -> dict[str, Any]:
    """Map internal DraftResolution into Day09 L3A public output schema."""
    enforce_invariants_and_reconcile(draft, ledger, case_id)

    # Clean affected entities sets
    entities = {
        "order_ids": sorted(set(filter(None, draft.order_ids)))[:20],
        "item_ids": sorted(set(filter(None, draft.item_ids)))[:20],
        "seller_ids": sorted(set(filter(None, draft.seller_ids)))[:20],
        "payment_references": sorted(set(filter(None, draft.payment_references)))[:20],
        "shipment_ids": sorted(set(filter(None, draft.shipment_ids)))[:20],
    }

    # Clean refund lines
    refund_lines: list[dict[str, Any]] = [
        {
            "reason_code": line.reason_code[:80],
            "amount_brl": to_float_brl(to_decimal(line.amount_brl)),
            "entity_id": line.entity_id[:128] if line.entity_id else None,
        }
        for line in draft.refund_lines
    ][:10]

    financial_resolution: dict[str, Any] = {
        "currency": "BRL",
        "recommended_refund_brl": to_float_brl(draft.recommended_refund_brl),
        "refund_lines": refund_lines,
    }

    # Clean claim assessments
    claim_assessments: list[dict[str, Any]] = [
        {
            "claim_id": ca.claim_id[:64],
            "verdict": ca.verdict,
            "confidence": ca.confidence,
            "evidence_refs": ca.evidence_refs[:20],
        }
        for ca in draft.claim_assessments
    ][:5]

    output: dict[str, Any] = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": draft.primary_issue,
            "case_status": draft.case_status,
            "confidence": draft.confidence,
        },
        "affected_entities": entities,
        "root_cause_analysis": {
            "ranked_causes": draft.ranked_causes[:5],
            "responsible_parties": draft.responsible_parties[:5],
        },
        "evidence_refs": draft.evidence_refs,
        "data_conflicts": draft.data_conflicts[:5],
        "financial_resolution": financial_resolution,
        "resolution_actions": draft.resolution_actions,
    }

    if claim_assessments:
        output["claim_assessments"] = claim_assessments

    return output

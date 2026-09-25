from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from ..core.ledger import EvidenceLedger
from ..core.models import (
    ClaimAssessment,
    DraftResolution,
    InvestigationPlan,
    OrderFinding,
    PaymentFinding,
    RefundLine,
    ShipmentFinding,
)
from ..core.scoped_gateway import ScopedEvidenceGateway
from ..trace import TraceWriter
from ..utils.calibration import calculate_confidence
from ..utils.finance import round_brl

logger = logging.getLogger(__name__)

ALLOWED_TOOLS = {"get_policy"}


class PolicySpecialistAgent:
    """Specialist agent for Policy evaluation, claim assessment and financial resolution."""

    def __init__(
        self,
        gateway: ScopedEvidenceGateway,
        ledger: EvidenceLedger,
        trace: TraceWriter,
    ) -> None:
        self.gateway = gateway
        self.ledger = ledger
        self.trace = trace

    async def evaluate(
        self,
        plan: InvestigationPlan,
        order: OrderFinding,
        payment: PaymentFinding,
        shipment: ShipmentFinding,
    ) -> DraftResolution:
        case_id = plan.case_id
        policy_version = plan.policy_version

        # 1. Fetch authoritative policy
        policy_ref: str | None = None
        try:
            ev_pol = await self.gateway.call(
                "get_policy", case_id=case_id, policy_version=policy_version
            )
            policy_ref = ev_pol["evidence_ref"]
            self.ledger.mark_consumed(policy_ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="policy_specialist",
                tool_name="get_policy",
                evidence_refs=[policy_ref],
                attributes={"policy_version": policy_version},
            )
        except Exception as exc:
            logger.warning(f"[{case_id}] get_policy failed: {exc}")

        # 2. Determine Primary Issue and Root Causes
        primary_issue = "unsupported_claim"
        case_status = "no_action"
        ranked_causes: list[dict[str, Any]] = []
        responsible_parties: list[dict[str, Any]] = []
        recommended_refund = Decimal("0.00")
        refund_lines: list[RefundLine] = []
        resolution_actions: list[str] = []
        data_conflicts: list[dict[str, Any]] = []

        seller_id = (
            order.seller_ids[0]
            if order.seller_ids
            else (shipment.seller_ids[0] if shipment.seller_ids else None)
        )
        order_id = order.order_id or plan.claimed_order_id

        if not order.found:
            primary_issue = "insufficient_evidence"
            case_status = "needs_investigation"
            ranked_causes.append({"cause_code": "ENTITY_NOT_FOUND", "rank": 1})
            responsible_parties.append({"party_type": "unknown", "party_id": None})
            resolution_actions.append("request_additional_order_info")

        elif order.status == "canceled" and payment.total_paid_brl > Decimal("0.00"):
            primary_issue = "canceled_order_paid"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "ORDER_CANCELED_BEFORE_FULFILLMENT", "rank": 1})
            responsible_parties.append({"party_type": "platform", "party_id": "marketplace"})
            recommended_refund = payment.total_paid_brl
            refund_lines.append(
                RefundLine("order_canceled_refund", payment.total_paid_brl, order_id)
            )
            resolution_actions.extend(["process_full_refund", "notify_customer"])

        elif order.status == "unavailable" and payment.total_paid_brl > Decimal("0.00"):
            primary_issue = "unavailable_order_paid"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "INVENTORY_OUT_OF_STOCK", "rank": 1})
            responsible_parties.append({"party_type": "seller", "party_id": seller_id})
            recommended_refund = payment.total_paid_brl
            refund_lines.append(
                RefundLine("inventory_unavailable_refund", payment.total_paid_brl, order_id)
            )
            resolution_actions.extend(["process_full_refund", "notify_seller_inventory"])

        elif payment.has_duplicate_charge:
            primary_issue = "duplicate_charge"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "PAYMENT_GATEWAY_DUPLICATION", "rank": 1})
            responsible_parties.append(
                {"party_type": "payment_provider", "party_id": "payment_processor"}
            )
            recommended_refund = payment.duplicate_amount_brl
            refund_lines.append(
                RefundLine("duplicate_charge_reversal", payment.duplicate_amount_brl, order_id)
            )
            resolution_actions.extend(["reverse_duplicate_charge", "notify_customer"])

        elif payment.refund_status == "failed":
            primary_issue = "refund_failed"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "REFUND_GATEWAY_REJECTED", "rank": 1})
            responsible_parties.append({"party_type": "payment_provider", "party_id": "acquirer"})
            refund_val = payment.refund_amount_brl or payment.total_paid_brl
            recommended_refund = refund_val
            refund_lines.append(RefundLine("retry_failed_refund", refund_val, order_id))
            resolution_actions.extend(["retry_refund_transaction", "notify_customer"])

        elif payment.refund_status == "pending":
            primary_issue = "refund_pending"
            case_status = "no_action"
            ranked_causes.append({"cause_code": "REFUND_PROCESSING_SLA", "rank": 1})
            responsible_parties.append({"party_type": "platform", "party_id": "marketplace"})
            resolution_actions.append("advise_customer_banking_sla")

        elif shipment.is_late_delivery:
            if shipment.late_responsible_party == "seller":
                primary_issue = "late_delivery_seller"
                ranked_causes.append({"cause_code": "SELLER_HANDOFF_DELAY", "rank": 1})
                responsible_parties.append({"party_type": "seller", "party_id": seller_id})
                case_status = "action_required"
                resolution_actions.extend(["issue_seller_warning", "notify_customer"])
            else:
                primary_issue = "late_delivery_logistics"
                ranked_causes.append({"cause_code": "LOGISTICS_TRANSIT_DELAY", "rank": 1})
                responsible_parties.append(
                    {"party_type": "logistics_provider", "party_id": "carrier_partner"}
                )
                case_status = "no_action" if shipment.is_delivered else "action_required"
                resolution_actions.extend(["log_carrier_service_breach", "notify_customer"])

        elif payment.has_payment_records and abs(
            payment.total_paid_brl - order.total_order_value_brl
        ) > Decimal("0.05"):
            primary_issue = "payment_mismatch"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "ORDER_PAYMENT_MISMATCH", "rank": 1})
            responsible_parties.append({"party_type": "platform", "party_id": "checkout_service"})
            resolution_actions.extend(["reconcile_order_ledger", "notify_support"])

        elif payment.is_split_payment:
            primary_issue = "valid_split_payment"
            case_status = "no_action"
            ranked_causes.append({"cause_code": "CUSTOMER_SPLIT_PAYMENT_PLAN", "rank": 1})
            responsible_parties.append({"party_type": "customer", "party_id": None})
            resolution_actions.append("explain_payment_breakdown")

        else:
            primary_issue = "unsupported_claim"
            case_status = "no_action"
            ranked_causes.append({"cause_code": "ORDER_FULFILLED_ACCORDING_TO_SLA", "rank": 1})
            responsible_parties.append({"party_type": "customer", "party_id": None})
            resolution_actions.append("confirm_order_status_satisfactory")

        # 3. Assess Claims
        claim_assessments: list[ClaimAssessment] = []
        for cl in plan.claims:
            cid = cl.get("claim_id", f"claim_{case_id}")
            topic = cl.get("topic", "")

            # Check verdict alignment
            if topic == primary_issue:
                verdict = "supported"
            elif topic == "requested_full_refund":
                if recommended_refund > Decimal("0.00") and (
                    payment.total_paid_brl == Decimal("0.00")
                    or recommended_refund >= payment.total_paid_brl
                ):
                    verdict = "supported"
                elif recommended_refund > Decimal("0.00"):
                    verdict = "partially_supported"
                else:
                    verdict = "unsupported"
            elif not order.found:
                verdict = "insufficient_evidence"
            elif primary_issue == "unsupported_claim":
                verdict = "unsupported"
            elif (
                ("late_delivery" in topic and "late_delivery" in primary_issue)
                or ("refund" in topic and "refund" in primary_issue)
            ):
                verdict = "partially_supported"
            else:
                verdict = "unsupported"

            claim_refs = [
                r
                for r in (order.evidence_refs + payment.evidence_refs + shipment.evidence_refs)
                if r
            ]
            if policy_ref:
                claim_refs.append(policy_ref)

            claim_assessments.append(
                ClaimAssessment(
                    claim_id=cid,
                    verdict=verdict,
                    confidence=0.90 if verdict in ("supported", "unsupported") else 0.70,
                    evidence_refs=sorted(set(claim_refs)),
                )
            )

        # 4. Gather all evidence refs
        all_refs = set(order.evidence_refs + payment.evidence_refs + shipment.evidence_refs)
        if policy_ref:
            all_refs.add(policy_ref)

        # 5. Compute dynamic confidence
        confidence = calculate_confidence(
            primary_issue=primary_issue,
            entity_found=order.found,
            evidence_count=len(all_refs),
            has_data_conflict=bool(data_conflicts),
        )

        all_order_ids = [order_id] if order_id else []
        all_seller_ids = sorted(set(order.seller_ids + shipment.seller_ids))

        draft = DraftResolution(
            primary_issue=primary_issue,
            case_status=case_status,
            confidence=confidence,
            order_ids=all_order_ids,
            item_ids=order.item_ids,
            seller_ids=all_seller_ids,
            payment_references=payment.payment_references,
            shipment_ids=shipment.shipment_ids,
            ranked_causes=ranked_causes,
            responsible_parties=responsible_parties,
            recommended_refund_brl=round_brl(recommended_refund),
            refund_lines=refund_lines,
            resolution_actions=resolution_actions,
            data_conflicts=data_conflicts,
            claim_assessments=claim_assessments,
            evidence_refs=sorted(all_refs),
        )

        self.trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="policy_specialist",
            decision_code=f"ISSUE_{primary_issue.upper()}",
            evidence_refs=draft.evidence_refs,
            attributes={
                "primary_issue": primary_issue,
                "case_status": case_status,
                "recommended_refund_brl": float(draft.recommended_refund_brl),
            },
        )
        return draft

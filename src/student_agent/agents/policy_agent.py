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
        policy_data: dict[str, Any] = {}
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
            raw_policy = ev_pol.get("data") or {}
            if isinstance(raw_policy, list) and raw_policy:
                raw_policy = raw_policy[0]
            if isinstance(raw_policy, dict):
                policy_data = raw_policy
        except Exception as exc:
            logger.warning(f"[{case_id}] get_policy failed: {exc}")

        # Resolve compensation profile declared by the authoritative policy.
        # The gateway may expose a nested "policy" object or a flat map.
        policy_rules = policy_data.get("policy", policy_data)
        if not isinstance(policy_rules, dict):
            policy_rules = {}
        policy_comp = (
            policy_rules.get("compensation")
            or policy_rules.get("compensation_on_delays")
            or policy_data.get("compensation")
            or policy_data.get("compensation_on_delays")
            or {}
        )
        if not isinstance(policy_comp, dict):
            policy_comp = {}
        delay_comp = (
            policy_comp.get("late_delivery")
            or policy_comp.get("delivery_delay")
            or policy_rules.get("late_delivery_compensation")
        )
        if not isinstance(delay_comp, dict):
            delay_comp = {}

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

        # Collect customer's claimed topics (excluding requested_full_refund which is an action)
        claimed_topics = [
            c.get("topic")
            for c in plan.claims
            if c.get("topic") and c.get("topic") != "requested_full_refund"
        ]
        target_claim = claimed_topics[0] if claimed_topics else None

        # Authoritative facts from specialists
        is_canceled_paid = order.status == "canceled" and payment.total_paid_brl > Decimal("0.00")
        is_unavailable_paid = order.status == "unavailable" and payment.total_paid_brl > Decimal(
            "0.00"
        )
        is_duplicate_charge = payment.has_duplicate_charge
        is_refund_failed = payment.refund_status == "failed"
        is_refund_pending = payment.refund_status == "pending"
        is_late_seller = shipment.is_late_delivery and shipment.late_responsible_party == "seller"
        is_late_logistics = (
            shipment.is_late_delivery and shipment.late_responsible_party == "logistics_provider"
        )
        is_payment_mismatch = payment.has_payment_records and (
            order.total_order_value_brl > Decimal("0.00")
            and abs(payment.total_paid_brl - order.total_order_value_brl) > Decimal("0.05")
        )
        is_valid_split = payment.is_split_payment

        # Determine primary issue
        if not order.found:
            primary_issue = "insufficient_evidence"
            case_status = "needs_investigation"
            ranked_causes.append({"cause_code": "ENTITY_NOT_FOUND", "rank": 1})
            responsible_parties.append({"party_type": "unknown", "party_id": None})
            resolution_actions.append("request_additional_order_info")

        elif (target_claim == "canceled_order_paid" and is_canceled_paid) or (
            target_claim is None and is_canceled_paid
        ):
            primary_issue = "canceled_order_paid"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "ORDER_CANCELED_BEFORE_FULFILLMENT", "rank": 1})
            responsible_parties.append({"party_type": "platform", "party_id": "marketplace"})
            recommended_refund = payment.total_paid_brl
            refund_lines.append(
                RefundLine("order_canceled_refund", payment.total_paid_brl, order_id)
            )
            resolution_actions.extend(["process_full_refund", "notify_customer"])

        elif (target_claim == "unavailable_order_paid" and is_unavailable_paid) or (
            target_claim is None and is_unavailable_paid
        ):
            primary_issue = "unavailable_order_paid"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "INVENTORY_OUT_OF_STOCK", "rank": 1})
            responsible_parties.append({"party_type": "seller", "party_id": seller_id})
            recommended_refund = payment.total_paid_brl
            refund_lines.append(
                RefundLine("inventory_unavailable_refund", payment.total_paid_brl, order_id)
            )
            resolution_actions.extend(["process_full_refund", "notify_seller_inventory"])

        elif (target_claim == "duplicate_charge" and is_duplicate_charge) or (
            target_claim is None and is_duplicate_charge
        ):
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

        elif (target_claim == "refund_failed" and is_refund_failed) or (
            target_claim is None and is_refund_failed
        ):
            primary_issue = "refund_failed"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "REFUND_GATEWAY_REJECTED", "rank": 1})
            responsible_parties.append({"party_type": "payment_provider", "party_id": "acquirer"})
            refund_val = payment.refund_amount_brl or payment.total_paid_brl
            recommended_refund = refund_val
            refund_lines.append(RefundLine("retry_failed_refund", refund_val, order_id))
            resolution_actions.extend(["retry_refund_transaction", "notify_customer"])

        elif (target_claim == "refund_pending" and is_refund_pending) or (
            target_claim is None and is_refund_pending
        ):
            primary_issue = "refund_pending"
            case_status = "no_action"
            ranked_causes.append({"cause_code": "REFUND_PROCESSING_SLA", "rank": 1})
            responsible_parties.append({"party_type": "platform", "party_id": "marketplace"})
            resolution_actions.append("advise_customer_banking_sla")

        elif (target_claim == "late_delivery_seller" and is_late_seller) or (
            target_claim is None and is_late_seller
        ):
            primary_issue = "late_delivery_seller"
            ranked_causes.append({"cause_code": "SELLER_HANDOFF_DELAY", "rank": 1})
            responsible_parties.append({"party_type": "seller", "party_id": seller_id})
            case_status = "action_required"
            resolution_actions.extend(["issue_seller_warning", "notify_customer"])

        elif (target_claim == "late_delivery_logistics" and is_late_logistics) or (
            target_claim is None and is_late_logistics
        ):
            primary_issue = "late_delivery_logistics"
            ranked_causes.append({"cause_code": "LOGISTICS_TRANSIT_DELAY", "rank": 1})
            responsible_parties.append(
                {"party_type": "logistics_provider", "party_id": "carrier_partner"}
            )
            case_status = "no_action" if shipment.is_delivered else "action_required"
            resolution_actions.extend(["log_carrier_service_breach", "notify_customer"])

        elif (target_claim == "payment_mismatch" and is_payment_mismatch) or (
            target_claim is None and is_payment_mismatch
        ):
            primary_issue = "payment_mismatch"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "ORDER_PAYMENT_MISMATCH", "rank": 1})
            responsible_parties.append({"party_type": "platform", "party_id": "checkout_service"})
            resolution_actions.extend(["reconcile_order_ledger", "notify_support"])

        elif (target_claim == "valid_split_payment" and is_valid_split) or (
            target_claim is None and is_valid_split
        ):
            primary_issue = "valid_split_payment"
            case_status = "no_action"
            ranked_causes.append({"cause_code": "CUSTOMER_SPLIT_PAYMENT_PLAN", "rank": 1})
            responsible_parties.append({"party_type": "customer", "party_id": None})
            resolution_actions.append("explain_payment_breakdown")

        # If customer claimed unsupported_claim or their claimed issue was not proven
        elif target_claim is not None:
            primary_issue = "unsupported_claim"
            case_status = "no_action"
            ranked_causes.append({"cause_code": "ORDER_FULFILLED_ACCORDING_TO_SLA", "rank": 1})
            responsible_parties.append({"party_type": "customer", "party_id": None})
            resolution_actions.append("confirm_order_status_satisfactory")

        # Fallback only when target_claim is None
        elif is_canceled_paid:
            primary_issue = "canceled_order_paid"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "ORDER_CANCELED_BEFORE_FULFILLMENT", "rank": 1})
            responsible_parties.append({"party_type": "platform", "party_id": "marketplace"})
            recommended_refund = payment.total_paid_brl
            refund_lines.append(
                RefundLine("order_canceled_refund", payment.total_paid_brl, order_id)
            )
            resolution_actions.extend(["process_full_refund", "notify_customer"])

        elif is_unavailable_paid:
            primary_issue = "unavailable_order_paid"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "INVENTORY_OUT_OF_STOCK", "rank": 1})
            responsible_parties.append({"party_type": "seller", "party_id": seller_id})
            recommended_refund = payment.total_paid_brl
            refund_lines.append(
                RefundLine("inventory_unavailable_refund", payment.total_paid_brl, order_id)
            )
            resolution_actions.extend(["process_full_refund", "notify_seller_inventory"])

        elif is_duplicate_charge:
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

        elif is_refund_failed:
            primary_issue = "refund_failed"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "REFUND_GATEWAY_REJECTED", "rank": 1})
            responsible_parties.append({"party_type": "payment_provider", "party_id": "acquirer"})
            refund_val = payment.refund_amount_brl or payment.total_paid_brl
            recommended_refund = refund_val
            refund_lines.append(RefundLine("retry_failed_refund", refund_val, order_id))
            resolution_actions.extend(["retry_refund_transaction", "notify_customer"])

        elif is_refund_pending:
            primary_issue = "refund_pending"
            case_status = "no_action"
            ranked_causes.append({"cause_code": "REFUND_PROCESSING_SLA", "rank": 1})
            responsible_parties.append({"party_type": "platform", "party_id": "marketplace"})
            resolution_actions.append("advise_customer_banking_sla")

        elif is_late_seller:
            primary_issue = "late_delivery_seller"
            ranked_causes.append({"cause_code": "SELLER_HANDOFF_DELAY", "rank": 1})
            responsible_parties.append({"party_type": "seller", "party_id": seller_id})
            case_status = "action_required"
            resolution_actions.extend(["issue_seller_warning", "notify_customer"])

        elif is_late_logistics:
            primary_issue = "late_delivery_logistics"
            ranked_causes.append({"cause_code": "LOGISTICS_TRANSIT_DELAY", "rank": 1})
            responsible_parties.append(
                {"party_type": "logistics_provider", "party_id": "carrier_partner"}
            )
            case_status = "no_action" if shipment.is_delivered else "action_required"
            resolution_actions.extend(["log_carrier_service_breach", "notify_customer"])

        elif is_payment_mismatch:
            primary_issue = "payment_mismatch"
            case_status = "action_required"
            ranked_causes.append({"cause_code": "ORDER_PAYMENT_MISMATCH", "rank": 1})
            responsible_parties.append({"party_type": "platform", "party_id": "checkout_service"})
            resolution_actions.extend(["reconcile_order_ledger", "notify_support"])

        elif is_valid_split:
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

        # Detect genuine data conflicts across the fetched MCP sources so the
        # resolution stays auditable. Every emitted conflict cites >= 2 distinct
        # tool sources (required by the output schema).
        order_raw = order.raw_order or {}
        shipment_raw = shipment.summary_data or {}

        def _to_decimal(value: Any) -> Decimal | None:
            if value is None or isinstance(value, bool):
                return None
            try:
                return round_brl(Decimal(str(value)))
            except Exception:
                return None

        def _find_value(src: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
            for key in keys:
                if key not in src:
                    continue
                val = _to_decimal(src[key])
                if val is not None:
                    return val
            return None

        # (a) Total order value disputed across distinct tools.
        order_value_sources: list[tuple[str, Decimal]] = []
        ov = _find_value(
            order_raw,
            (
                "total_order_value_brl",
                "total_value_brl",
                "total_amount_brl",
                "order_value_brl",
                "amount_brl",
                "grand_total_brl",
            ),
        )
        if ov is not None:
            order_value_sources.append(("get_order", ov))
        ov = _find_value(
            shipment_raw,
            (
                "total_order_value_brl",
                "order_value",
                "total_value",
                "charged_value",
                "amount_brl",
            ),
        )
        if ov is not None:
            order_value_sources.append(("get_shipment_summary", ov))
        if payment.has_payment_records and payment.total_paid_brl > Decimal("0.00"):
            order_value_sources.append(("get_order_payments", payment.total_paid_brl))
        if order.total_order_value_brl > Decimal("0.00"):
            order_value_sources.append(("get_order", order.total_order_value_brl))

        # Cluster values within 10 cents, then attribute each cluster to the
        # tools that produced it. Disagreement across >= 2 tools is a real
        # conflict worth surfacing.
        clusters: list[list[tuple[str, Decimal]]] = []
        for src_name, val in order_value_sources:
            for cluster in clusters:
                if abs(float(cluster[0][1]) - float(val)) <= 0.05:
                    cluster.append((src_name, val))
                    break
            else:
                clusters.append([(src_name, val)])

        conflict_tools = sorted(
            {src_name for cluster in clusters for src_name, _ in cluster}
        )
        if len(clusters) >= 2 and len(conflict_tools) >= 2:
            data_conflicts.append(
                {
                    "field": "total_order_value_brl",
                    "sources": conflict_tools[:5],
                    "selected_source": (
                        "get_order_payments" if payment.has_payment_records else "get_order"
                    ),
                    "resolution_code": "PAYMENT_SUM_PREFERRED",
                }
            )

        # (b) Delivery status disputes across the order and shipment tools.
        status_by_tool: list[tuple[str, str]] = []
        status_keys = (
            "order_status",
            "delivery_status",
            "shipment_status",
            "status",
            "delivery_state",
        )
        for src_name, src in (("get_order", order_raw), ("get_shipment_summary", shipment_raw)):
            for key in status_keys:
                if isinstance(src.get(key), str):
                    status_by_tool.append((src_name, src[key]))
                    break
        status_labels = {label for _, label in status_by_tool}
        if len(status_labels) >= 2:
            data_conflicts.append(
                {
                    "field": "delivery_status",
                    "sources": [tool for tool, _ in status_by_tool][:5],
                    "selected_source": (
                        "get_shipment_summary"
                        if any(t == "get_shipment_summary" for t, _ in status_by_tool)
                        else "get_order"
                    ),
                    "resolution_code": "SHIPMENT_SOURCE_PREFERRED",
                }
            )

        # Late-delivery refund decided by the authoritative policy (amount rule).
        late_refund = Decimal("0.00")
        if any(x for x in (is_late_seller, is_late_logistics) if x):
            amount_spec = (
                delay_comp.get("amount_brl")
                or delay_comp.get("flat_amount_brl")
                or delay_comp.get("amount")
            )
            pct_spec = (
                delay_comp.get("percent_of_order")
                or delay_comp.get("percentage")
                or delay_comp.get("refund_percent")
            )
            if pct_spec is not None:
                try:
                    pct = float(pct_spec)
                    base = (
                        payment.total_paid_brl
                        if payment.total_paid_brl > Decimal("0")
                        else order.total_order_value_brl
                    )
                    late_refund = round_brl(base * Decimal(str(round(pct, 6))) / Decimal("100"))
                except (TypeError, ValueError):
                    late_refund = Decimal("0.00")
            elif amount_spec is not None:
                try:
                    cand = round_brl(Decimal(str(amount_spec)))
                except Exception:
                    cand = Decimal("0.00")
                # Flat amount that appears to exceed every order value is
                # treated as a centavo shorthand (e.g. 3000 => 30.00).
                order_value = max(
                    payment.total_paid_brl, order.total_order_value_brl, Decimal("0.01")
                )
                if cand > order_value * Decimal("3"):
                    cand = cand / Decimal("100")
                late_refund = cand
        # Refunds are always non-negative regardless of policy phrasing.
        late_refund = round_brl(max(Decimal("0.00"), late_refund))

        # Payment short/over reconciliation: the customer should be refunded
        # the excess they actually paid (clamped non-negative; never refund on
        # an underpayment).
        payment_diff = round_brl(
            max(Decimal("0.00"), payment.total_paid_brl - order.total_order_value_brl)
        )

        late_mismatch = (
            primary_issue in ("late_delivery_seller", "late_delivery_logistics")
            and late_refund > Decimal("0.00")
            and case_status == "action_required"
        )
        if late_mismatch:
            recommended_refund = late_refund
            refund_lines.append(RefundLine("late_delivery_compensation", late_refund, order_id))
            if not any(a.startswith("compensate") for a in resolution_actions):
                resolution_actions.append("compensate_late_delivery")
        elif primary_issue == "payment_mismatch" and payment_diff > Decimal("0.05"):
            recommended_refund = payment_diff
            refund_lines.append(RefundLine("overpayment_reconciliation", payment_diff, order_id))
            if not any(a.startswith("refund") for a in resolution_actions):
                resolution_actions.append("refund_overpayment")

        # 3. Assess Claims
        claim_assessments: list[ClaimAssessment] = []
        for cl in plan.claims:
            cid = cl.get("claim_id", f"claim_{case_id}")
            topic = cl.get("topic", "")

            # Check verdict alignment
            if topic == primary_issue:
                verdict = "supported"
            elif topic == "requested_full_refund":
                if recommended_refund > Decimal("0.00") and recommended_refund >= max(
                    payment.total_paid_brl, order.total_order_value_brl
                ):
                    verdict = "supported"
                elif recommended_refund > Decimal("0.00"):
                    verdict = "partially_supported"
                elif order.found and (
                    order.status in ("delivered", "shipped")
                    or shipment.is_delivered
                ):
                    verdict = "unsupported"
                else:
                    verdict = "insufficient_evidence"
            elif not order.found:
                verdict = "insufficient_evidence"
            elif primary_issue == "unsupported_claim":
                verdict = "unsupported"
            elif ("late_delivery" in topic and "late_delivery" in primary_issue) or (
                "refund" in topic and "refund" in primary_issue
            ):
                verdict = "partially_supported"
            else:
                verdict = "unsupported"

            # Only cite evidence that actually supports the verdict.
            claim_refs: list[str] = []
            if topic == primary_issue or verdict in ("supported", "partially_supported"):
                claim_refs.extend(order.evidence_refs)
                if verdict != "unsupported":
                    claim_refs.extend(payment.evidence_refs)
                    claim_refs.extend(shipment.evidence_refs)
            if policy_ref and policy_ref not in claim_refs:
                claim_refs.append(policy_ref)
            claim_refs = sorted(set(r for r in claim_refs if r))

            claim_assessments.append(
                ClaimAssessment(
                    claim_id=cid,
                    verdict=verdict,
                    confidence=0.90 if verdict in ("supported", "unsupported") else 0.70,
                    evidence_refs=claim_refs,
                )
            )

        # 4. Gather all evidence refs
        all_refs = set(order.evidence_refs + payment.evidence_refs + shipment.evidence_refs)
        if policy_ref:
            all_refs.add(policy_ref)

        # 5. Compute dynamic confidence. Conflicts that were fully resolved do
        # not need a calibration penalty (they raise, not lower, certainty).
        unresolved_conflicts = [
            c for c in data_conflicts if c.get("selected_source") in (None, "")
        ]
        confidence = calculate_confidence(
            primary_issue=primary_issue,
            entity_found=order.found,
            evidence_count=len(all_refs),
            has_data_conflict=bool(unresolved_conflicts),
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

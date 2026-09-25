from __future__ import annotations

import logging
from decimal import Decimal

from ..core.ledger import EvidenceLedger
from ..core.models import PaymentFinding
from ..core.scoped_gateway import ScopedEvidenceGateway
from ..trace import TraceWriter
from ..utils.finance import round_brl, to_decimal

logger = logging.getLogger(__name__)

ALLOWED_TOOLS = {
    "get_order_payments",
    "get_payment_timeline",
    "get_refund_timeline",
}


class PaymentSpecialistAgent:
    """Specialist agent for Payment reconciliation and refund timelines."""

    def __init__(
        self,
        gateway: ScopedEvidenceGateway,
        ledger: EvidenceLedger,
        trace: TraceWriter,
    ) -> None:
        self.gateway = gateway
        self.ledger = ledger
        self.trace = trace

    async def investigate(self, case_id: str, order_id: str | None) -> PaymentFinding:
        finding = PaymentFinding()
        if not order_id:
            return finding

        # 1. Look up base payments
        try:
            ev_pay = await self.gateway.call(
                "get_order_payments", case_id=case_id, order_id=order_id
            )
            pay_ref = ev_pay["evidence_ref"]
            self.ledger.mark_consumed(pay_ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="payment_specialist",
                tool_name="get_order_payments",
                evidence_refs=[pay_ref],
                attributes={"order_id": order_id},
            )

            pay_rows = ev_pay.get("data", [])
            if isinstance(pay_rows, dict):
                pay_rows = pay_rows.get("payments", [])

            if pay_rows:
                finding.has_payment_records = True
                finding.payment_rows = pay_rows
                total_paid = Decimal("0.00")
                types: set[str] = set()
                max_inst = 1
                refs: list[str] = []

                # Group by value and type to detect duplicates
                seen_values: dict[tuple[str, Decimal], int] = {}

                for row in pay_rows:
                    val = to_decimal(row.get("payment_value", 0))
                    p_type = row.get("payment_type", "unknown")
                    inst = int(row.get("payment_installments", 1) or 1)
                    seq = row.get("payment_sequential", 1)

                    total_paid += val
                    types.add(p_type)
                    if inst > max_inst:
                        max_inst = inst

                    ref_id = f"pay_{order_id}_{seq}"
                    refs.append(ref_id)

                    key = (p_type, round_brl(val))
                    seen_values[key] = seen_values.get(key, 0) + 1

                finding.total_paid_brl = round_brl(total_paid)
                finding.payment_types = sorted(types)
                finding.payment_installments_max = max_inst
                finding.payment_references = refs
                finding.is_split_payment = len(pay_rows) > 1 or len(types) > 1
                finding.evidence_refs.append(pay_ref)

                # Check duplicates (same type & non-zero amount appearing multiple times)
                for (ptype, amt), count in seen_values.items():
                    if (
                        count >= 2
                        and amt > Decimal("0.00")
                        and ptype in ("credit_card", "debit_card")
                    ):
                        finding.has_duplicate_charge = True
                        finding.duplicate_amount_brl = amt
                        break

        except Exception as exc:
            logger.warning(f"[{case_id}] get_order_payments failed: {exc}")

        # 2. Look up refund timeline
        try:
            ev_refund = await self.gateway.call(
                "get_refund_timeline", case_id=case_id, order_id=order_id, max_retries=0
            )
            refund_ref = ev_refund["evidence_ref"]
            self.ledger.mark_consumed(refund_ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="payment_specialist",
                tool_name="get_refund_timeline",
                evidence_refs=[refund_ref],
                attributes={"order_id": order_id},
            )
            finding.evidence_refs.append(refund_ref)

            refund_data = ev_refund.get("data", {})
            refund_events = []
            if isinstance(refund_data, dict):
                refund_events = refund_data.get("events") or refund_data.get("refunds", [])
            elif isinstance(refund_data, list):
                refund_events = refund_data

            if refund_events:
                # Find latest refund status
                latest = refund_events[-1]
                st = latest.get("status") or latest.get("refund_status")
                finding.refund_status = str(st).lower() if st else None
                finding.refund_amount_brl = to_decimal(
                    latest.get("amount_brl") or latest.get("amount", 0)
                )

        except Exception as exc:
            logger.debug(f"[{case_id}] get_refund_timeline skipped/failed: {exc}")

        # 3. Look up payment timeline
        try:
            ev_time = await self.gateway.call(
                "get_payment_timeline", case_id=case_id, order_id=order_id
            )
            time_ref = ev_time["evidence_ref"]
            self.ledger.mark_consumed(time_ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="payment_specialist",
                tool_name="get_payment_timeline",
                evidence_refs=[time_ref],
                attributes={"order_id": order_id},
            )
            finding.evidence_refs.append(time_ref)
            finding.timeline_events = ev_time.get("data", [])
        except Exception as exc:
            logger.debug(f"[{case_id}] get_payment_timeline skipped/failed: {exc}")

        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="payment_specialist",
            target="shipment_specialist",
            decision_code="PAYMENTS_RECONCILED",
            evidence_refs=finding.evidence_refs,
        )
        return finding

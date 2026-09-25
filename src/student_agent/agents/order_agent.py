from __future__ import annotations

import logging
from decimal import Decimal

from ..core.ledger import EvidenceLedger
from ..core.models import InvestigationPlan, OrderFinding
from ..core.scoped_gateway import ScopedEvidenceGateway
from ..trace import TraceWriter
from ..utils.finance import round_brl, to_decimal

logger = logging.getLogger(__name__)

ALLOWED_TOOLS = {
    "get_order",
    "get_order_items",
    "get_product_context",
    "get_customer_history",
}


class OrderSpecialistAgent:
    """Specialist agent for Order and Product investigation."""

    def __init__(
        self,
        gateway: ScopedEvidenceGateway,
        ledger: EvidenceLedger,
        trace: TraceWriter,
    ) -> None:
        self.gateway = gateway
        self.ledger = ledger
        self.trace = trace

    async def investigate(self, plan: InvestigationPlan) -> OrderFinding:
        case_id = plan.case_id
        order_id = plan.claimed_order_id

        finding = OrderFinding()
        if not order_id:
            logger.info(f"[{case_id}] No claimed_order_id provided")
            self.trace.emit(
                case_id=case_id,
                event_type="handoff",
                actor="order_specialist",
                target="payment_specialist",
                decision_code="NO_ORDER_CLAIMED",
            )
            return finding

        # 1. Look up authoritative order
        try:
            ev_order = await self.gateway.call("get_order", case_id=case_id, order_id=order_id)
            order_ref = ev_order["evidence_ref"]
            self.ledger.mark_consumed(order_ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="order_specialist",
                tool_name="get_order",
                evidence_refs=[order_ref],
                attributes={"order_id": order_id},
            )

            order_data = ev_order.get("data", {})
            if isinstance(order_data, list) and order_data:
                order_data = order_data[0]

            finding.found = True
            finding.order_id = order_id
            finding.status = order_data.get("order_status")
            finding.customer_id = order_data.get("customer_id")
            finding.order_purchase_timestamp = order_data.get("order_purchase_timestamp")
            finding.order_delivered_customer_date = order_data.get("order_delivered_customer_date")
            finding.order_estimated_delivery_date = order_data.get("order_estimated_delivery_date")
            finding.evidence_refs.append(order_ref)
            finding.raw_order = order_data

        except Exception as exc:
            logger.warning(f"[{case_id}] get_order failed or not found: {exc}")
            self.trace.emit(
                case_id=case_id,
                event_type="handoff",
                actor="order_specialist",
                target="payment_specialist",
                decision_code="ORDER_LOOKUP_FAILED",
            )
            return finding

        # 2. Look up items & sellers
        try:
            ev_items = await self.gateway.call(
                "get_order_items", case_id=case_id, order_id=order_id
            )
            items_ref = ev_items["evidence_ref"]
            self.ledger.mark_consumed(items_ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="order_specialist",
                tool_name="get_order_items",
                evidence_refs=[items_ref],
                attributes={"order_id": order_id},
            )

            items_data = ev_items.get("data", [])
            if isinstance(items_data, dict):
                items_data = items_data.get("items", [])

            item_ids: list[str] = []
            seller_ids: list[str] = []
            total_item_val = Decimal("0.00")
            total_freight_val = Decimal("0.00")

            for item in items_data:
                product_id = item.get("product_id")
                seller_id = item.get("seller_id")
                if product_id:
                    item_ids.append(product_id)
                if seller_id:
                    seller_ids.append(seller_id)

                total_item_val += to_decimal(item.get("price", 0))
                total_freight_val += to_decimal(item.get("freight_value", 0))

            finding.item_ids = sorted(set(item_ids))
            finding.seller_ids = sorted(set(seller_ids))
            finding.total_item_value_brl = round_brl(total_item_val)
            finding.total_freight_value_brl = round_brl(total_freight_val)
            finding.total_order_value_brl = round_brl(total_item_val + total_freight_val)
            finding.evidence_refs.append(items_ref)
            finding.raw_items = items_data

        except Exception as exc:
            logger.warning(f"[{case_id}] get_order_items failed: {exc}")

        # 3. Product context (categories)
        try:
            ev_prod = await self.gateway.call(
                "get_product_context", case_id=case_id, order_id=order_id
            )
            prod_ref = ev_prod["evidence_ref"]
            self.ledger.mark_consumed(prod_ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="order_specialist",
                tool_name="get_product_context",
                evidence_refs=[prod_ref],
                attributes={"order_id": order_id},
            )
            prod_data = ev_prod.get("data", [])
            categories = [
                p.get("product_category_name") for p in prod_data if p.get("product_category_name")
            ]
            finding.product_categories = sorted(set(categories))
            finding.evidence_refs.append(prod_ref)
        except Exception as exc:
            logger.debug(f"[{case_id}] get_product_context skipped/failed: {exc}")

        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="order_specialist",
            target="payment_specialist",
            decision_code="ORDER_VERIFIED",
            evidence_refs=finding.evidence_refs,
        )
        return finding

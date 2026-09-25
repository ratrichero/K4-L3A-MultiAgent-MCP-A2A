from __future__ import annotations

import logging
from datetime import datetime

from ..core.ledger import EvidenceLedger
from ..core.models import ShipmentFinding
from ..core.scoped_gateway import ScopedEvidenceGateway
from ..trace import TraceWriter

logger = logging.getLogger(__name__)

ALLOWED_TOOLS = {
    "get_shipment_summary",
    "get_sellers",
}


def _parse_iso(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        # Standardize ISO format
        clean = date_str.replace("Z", "+00:00")
        return datetime.fromisoformat(clean)
    except Exception:
        return None


class ShipmentSpecialistAgent:
    """Specialist agent for Shipment and Carrier timeline investigation."""

    def __init__(
        self,
        gateway: ScopedEvidenceGateway,
        ledger: EvidenceLedger,
        trace: TraceWriter,
    ) -> None:
        self.gateway = gateway
        self.ledger = ledger
        self.trace = trace

    async def investigate(self, case_id: str, order_id: str | None) -> ShipmentFinding:
        finding = ShipmentFinding()
        if not order_id:
            return finding

        # 1. Get shipment summary
        try:
            ev_ship = await self.gateway.call(
                "get_shipment_summary", case_id=case_id, order_id=order_id
            )
            ship_ref = ev_ship["evidence_ref"]
            self.ledger.mark_consumed(ship_ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="shipment_specialist",
                tool_name="get_shipment_summary",
                evidence_refs=[ship_ref],
                attributes={"order_id": order_id},
            )
            finding.evidence_refs.append(ship_ref)

            ship_data = ev_ship.get("data", {})
            if isinstance(ship_data, list) and ship_data:
                ship_data = ship_data[0]

            finding.has_shipment_records = True
            finding.summary_data = ship_data
            finding.order_status = ship_data.get("order_status")

            shipping_limits = ship_data.get("shipping_limits", [])
            shipping_limit = ship_data.get("shipping_limit_at") or ship_data.get(
                "shipping_limit_date"
            )
            if not shipping_limit and shipping_limits:
                shipping_limit = shipping_limits[0].get("shipping_limit_at")

            carrier_pickup = ship_data.get("delivered_carrier_at") or ship_data.get(
                "order_delivered_carrier_date"
            )
            estimated_del = ship_data.get("estimated_delivery_at") or ship_data.get(
                "order_estimated_delivery_date"
            )
            delivered_cust = ship_data.get("delivered_customer_at") or ship_data.get(
                "order_delivered_customer_date"
            )

            finding.shipping_limit_date = shipping_limit
            finding.carrier_delivered_date = carrier_pickup
            finding.estimated_delivery_date = estimated_del
            finding.delivered_customer_date = delivered_cust

            # Extract sellers from shipping limits if present
            s_ids = [s.get("seller_id") for s in shipping_limits if s.get("seller_id")]
            if s_ids:
                finding.seller_ids = sorted(set(finding.seller_ids + s_ids))

            dt_limit = _parse_iso(shipping_limit)
            dt_carrier = _parse_iso(carrier_pickup)
            dt_est = _parse_iso(estimated_del)
            dt_del = _parse_iso(delivered_cust)

            if dt_del:
                finding.is_delivered = True
                if dt_est and dt_del > dt_est:
                    finding.is_late_delivery = True
                    # Check explicit events for responsible actor
                    events = ship_data.get("events", [])
                    for ev in events:
                        if ev.get("event_type") == "delivered_late":
                            actor = ev.get("actor")
                            if actor in ("seller", "logistics_provider"):
                                finding.late_responsible_party = actor
                                break

                    if not finding.late_responsible_party:
                        # Check if seller was late in preparing
                        if dt_limit and dt_carrier and dt_carrier > dt_limit:
                            finding.late_responsible_party = "seller"
                        else:
                            finding.late_responsible_party = "logistics_provider"

            # Prefer authoritative shipment identifiers from the evidence and
            # only fall back to a synthetic key when the gateway exposes none.
            ship_ids: list[str] = []
            for key in ("shipment_id", "shipment_ref", "tracking_code", "id"):
                value = ship_data.get(key)
                if isinstance(value, str) and value.strip():
                    ship_ids.append(value.strip())
            shipments = ship_data.get("shipments")
            if isinstance(shipments, list):
                for entry in shipments:
                    if not isinstance(entry, dict):
                        continue
                    for key in ("shipment_id", "shipment_ref", "tracking_code", "id"):
                        value = entry.get(key)
                        if isinstance(value, str) and value.strip():
                            ship_ids.append(value.strip())
            finding.shipment_ids = sorted(set(ship_ids)) or [f"ship_{order_id}"]

        except Exception as exc:
            logger.warning(f"[{case_id}] get_shipment_summary failed: {exc}")

        # 2. Get seller profile
        try:
            ev_sellers = await self.gateway.call("get_sellers", case_id=case_id, order_id=order_id)
            sellers_ref = ev_sellers["evidence_ref"]
            self.ledger.mark_consumed(sellers_ref)
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="shipment_specialist",
                tool_name="get_sellers",
                evidence_refs=[sellers_ref],
                attributes={"order_id": order_id},
            )
            finding.evidence_refs.append(sellers_ref)
            sellers_data = ev_sellers.get("data", [])
            s_ids = [s.get("seller_id") for s in sellers_data if s.get("seller_id")]
            finding.seller_ids = sorted(set(s_ids))
        except Exception as exc:
            logger.debug(f"[{case_id}] get_sellers skipped/failed: {exc}")

        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="shipment_specialist",
            target="policy_specialist",
            decision_code="SHIPMENT_VERIFIED",
            evidence_refs=finding.evidence_refs,
        )
        return finding

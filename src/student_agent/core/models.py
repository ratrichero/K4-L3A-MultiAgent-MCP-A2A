from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class InvestigationPlan:
    """Plan constructed by Coordinator based on actual case input."""

    case_id: str
    claimed_order_id: str | None
    customer_message: str
    claims: list[dict[str, str]] = field(default_factory=list)
    policy_version: str = "EC_POLICY_V1"
    opened_at: str | None = None


@dataclass
class OrderFinding:
    """Findings produced by Order & Entity Specialist."""

    found: bool = False
    order_id: str | None = None
    status: str | None = None
    customer_id: str | None = None
    customer_unique_id: str | None = None
    order_purchase_timestamp: str | None = None
    order_delivered_customer_date: str | None = None
    order_estimated_delivery_date: str | None = None
    item_ids: list[str] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    total_item_value_brl: Decimal = Decimal("0.00")
    total_freight_value_brl: Decimal = Decimal("0.00")
    total_order_value_brl: Decimal = Decimal("0.00")
    product_categories: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    raw_order: dict[str, Any] = field(default_factory=dict)
    raw_items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PaymentFinding:
    """Findings produced by Payment Specialist."""

    has_payment_records: bool = False
    total_paid_brl: Decimal = Decimal("0.00")
    payment_types: list[str] = field(default_factory=list)
    payment_installments_max: int = 1
    payment_references: list[str] = field(default_factory=list)
    has_duplicate_charge: bool = False
    duplicate_amount_brl: Decimal = Decimal("0.00")
    is_split_payment: bool = False
    refund_status: str | None = None  # "pending", "failed", "completed", "none"
    refund_amount_brl: Decimal = Decimal("0.00")
    evidence_refs: list[str] = field(default_factory=list)
    payment_rows: list[dict[str, Any]] = field(default_factory=list)
    timeline_events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ShipmentFinding:
    """Findings produced by Logistics & Shipment Specialist."""

    has_shipment_records: bool = False
    shipment_ids: list[str] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    order_status: str | None = None
    shipping_limit_date: str | None = None
    carrier_delivered_date: str | None = None
    estimated_delivery_date: str | None = None
    delivered_customer_date: str | None = None
    is_delivered: bool = False
    is_late_delivery: bool = False
    late_responsible_party: str = "none"  # "seller", "logistics_provider", "none"
    evidence_refs: list[str] = field(default_factory=list)
    summary_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClaimAssessment:
    """Assessment of one specific customer claim."""

    claim_id: str
    verdict: str  # "supported", "unsupported", "partially_supported", "insufficient_evidence"
    confidence: float
    evidence_refs: list[str] = field(default_factory=list)


@dataclass
class RefundLine:
    """Single refund line item."""

    reason_code: str
    amount_brl: Decimal
    entity_id: str | None = None


@dataclass
class DraftResolution:
    """Reconciled resolution before invariant checking."""

    primary_issue: str
    case_status: str  # "action_required", "no_action", "needs_investigation"
    confidence: float
    order_ids: list[str] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    payment_references: list[str] = field(default_factory=list)
    shipment_ids: list[str] = field(default_factory=list)
    ranked_causes: list[dict[str, Any]] = field(default_factory=list)
    responsible_parties: list[dict[str, Any]] = field(default_factory=list)
    recommended_refund_brl: Decimal = Decimal("0.00")
    refund_lines: list[RefundLine] = field(default_factory=list)
    resolution_actions: list[str] = field(default_factory=list)
    data_conflicts: list[dict[str, Any]] = field(default_factory=list)
    claim_assessments: list[ClaimAssessment] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)

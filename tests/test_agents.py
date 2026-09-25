from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from student_agent.agents.coordinator import CoordinatorAgent
from student_agent.agents.order_agent import OrderSpecialistAgent
from student_agent.agents.payment_agent import PaymentSpecialistAgent
from student_agent.agents.policy_agent import PolicySpecialistAgent
from student_agent.agents.verifier import VerifierAgent
from student_agent.contracts import Contracts
from student_agent.core.ledger import EvidenceLedger
from student_agent.core.models import (
    ClaimAssessment,
    DraftResolution,
    InvestigationPlan,
    OrderFinding,
    PaymentFinding,
    RefundLine,
    ShipmentFinding,
)
from student_agent.core.scoped_gateway import ScopedEvidenceGateway
from student_agent.trace import TraceWriter


@pytest.fixture
def contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


@pytest.fixture
def trace(tmp_path: Path, contracts: Contracts) -> TraceWriter:
    return TraceWriter(tmp_path / "trace.jsonl", contracts)


def test_coordinator_plan(trace: TraceWriter) -> None:
    coordinator = CoordinatorAgent(trace)
    case = {
        "case_id": "L3A_CASE_999",
        "opened_at": "2018-01-01T00:00:00Z",
        "customer_request": {
            "message": "Kiểm tra đơn hàng",
            "claimed_order_id": "order_123",
            "claims": [{"claim_id": "claim_1", "topic": "canceled_order_paid"}],
        },
        "policy_version": "EC_POLICY_V1",
    }
    plan = coordinator.plan(case)
    assert plan.case_id == "L3A_CASE_999"
    assert plan.claimed_order_id == "order_123"
    assert len(plan.claims) == 1


@pytest.mark.anyio
async def test_order_specialist_investigate(trace: TraceWriter) -> None:
    ledger = EvidenceLedger("L3A_CASE_001")
    mock_gw = AsyncMock()
    mock_gw.call.side_effect = [
        {
            "evidence_ref": "ev_order_test_ref_1234567890",
            "data": [{"order_status": "delivered", "customer_id": "cust_1"}],
        },
        {
            "evidence_ref": "ev_items_test_ref_1234567890",
            "data": [
                {
                    "product_id": "prod_1",
                    "seller_id": "seller_1",
                    "price": 100.50,
                    "freight_value": 15.00,
                }
            ],
        },
        {
            "evidence_ref": "ev_prod_test_ref_1234567890",
            "data": [{"product_category_name": "eletronicos"}],
        },
    ]

    scoped_gw = ScopedEvidenceGateway(
        mock_gw,
        ledger,
        "order_specialist",
        {"get_order", "get_order_items", "get_product_context"},
    )
    order_agent = OrderSpecialistAgent(scoped_gw, ledger, trace)

    plan = InvestigationPlan(
        case_id="L3A_CASE_001",
        claimed_order_id="order_test_123",
        customer_message="Check order",
    )

    finding = await order_agent.investigate(plan)
    assert finding.found is True
    assert finding.order_id == "order_test_123"
    assert finding.status == "delivered"
    assert finding.item_ids == ["prod_1"]
    assert finding.seller_ids == ["seller_1"]
    assert finding.total_order_value_brl == Decimal("115.50")
    assert len(finding.evidence_refs) == 3


@pytest.mark.anyio
async def test_payment_specialist_duplicate_charge(trace: TraceWriter) -> None:
    ledger = EvidenceLedger("L3A_CASE_002")
    mock_gw = AsyncMock()
    mock_gw.call.side_effect = [
        {
            "evidence_ref": "ev_pay_test_ref_123456789012",
            "data": [
                {
                    "payment_sequential": 1,
                    "payment_type": "credit_card",
                    "payment_value": 50.00,
                },
                {
                    "payment_sequential": 2,
                    "payment_type": "credit_card",
                    "payment_value": 50.00,
                },
            ],
        },
        {"evidence_ref": "ev_ref_test_ref_123456789012", "data": []},
        {"evidence_ref": "ev_time_test_ref_12345678901", "data": []},
    ]

    scoped_gw = ScopedEvidenceGateway(
        mock_gw,
        ledger,
        "payment_specialist",
        {"get_order_payments", "get_refund_timeline", "get_payment_timeline"},
    )
    payment_agent = PaymentSpecialistAgent(scoped_gw, ledger, trace)

    finding = await payment_agent.investigate("L3A_CASE_002", "order_test_dup")
    assert finding.has_payment_records is True
    assert finding.total_paid_brl == Decimal("100.00")
    assert finding.has_duplicate_charge is True
    assert finding.duplicate_amount_brl == Decimal("50.00")


@pytest.mark.anyio
async def test_policy_specialist_canceled_order(trace: TraceWriter) -> None:
    ledger = EvidenceLedger("L3A_CASE_003")
    mock_gw = AsyncMock()
    mock_gw.call.return_value = {
        "evidence_ref": "ev_pol_test_ref_123456789012",
        "data": {"policy_name": "EC_POLICY_V1"},
    }

    scoped_gw = ScopedEvidenceGateway(mock_gw, ledger, "policy_specialist", {"get_policy"})
    policy_agent = PolicySpecialistAgent(scoped_gw, ledger, trace)

    plan = InvestigationPlan(
        case_id="L3A_CASE_003",
        claimed_order_id="order_canceled_1",
        customer_message="Đơn bị huỷ",
        claims=[{"claim_id": "cl_1", "topic": "canceled_order_paid"}],
    )
    order = OrderFinding(
        found=True, order_id="order_canceled_1", status="canceled", seller_ids=["seller_1"]
    )
    payment = PaymentFinding(
        has_payment_records=True,
        total_paid_brl=Decimal("89.90"),
        evidence_refs=["ev_pay_ref_12345678901234"],
    )
    shipment = ShipmentFinding()

    draft = await policy_agent.evaluate(plan, order, payment, shipment)
    assert draft.primary_issue == "canceled_order_paid"
    assert draft.case_status == "action_required"
    assert draft.recommended_refund_brl == Decimal("89.90")
    assert len(draft.refund_lines) == 1
    assert draft.refund_lines[0].amount_brl == Decimal("89.90")


def test_verifier_and_mapper_contract(contracts: Contracts, trace: TraceWriter) -> None:
    case_id = "L3A_CASE_004"
    ledger = EvidenceLedger(case_id)
    ev_ref1 = "ev_test_ref_1111111111111111"
    ev_ref2 = "ev_test_ref_2222222222222222"
    ledger.record({"evidence_ref": ev_ref1, "data": {}})
    ledger.record({"evidence_ref": ev_ref2, "data": {}})

    draft = DraftResolution(
        primary_issue="canceled_order_paid",
        case_status="action_required",
        confidence=0.92,
        order_ids=["order_4"],
        item_ids=["item_4"],
        seller_ids=["seller_4"],
        payment_references=["pay_order_4_1"],
        shipment_ids=["ship_order_4"],
        ranked_causes=[{"cause_code": "ORDER_CANCELED_BEFORE_FULFILLMENT", "rank": 1}],
        responsible_parties=[{"party_type": "platform", "party_id": "marketplace"}],
        recommended_refund_brl=Decimal("150.00"),
        refund_lines=[RefundLine("order_canceled_refund", Decimal("150.00"), "order_4")],
        resolution_actions=["process_full_refund", "notify_customer"],
        claim_assessments=[
            ClaimAssessment("cl_1", "supported", 0.90, [ev_ref1]),
        ],
        evidence_refs=[ev_ref1, ev_ref2],
    )

    verifier = VerifierAgent(contracts, trace)
    output = verifier.verify_and_finalize(draft, ledger, case_id)

    # Must pass schema validation
    contracts.validate_output(output, "test_output")
    assert output["schema_version"] == "day09-l3a-output-v2"
    assert output["case_id"] == case_id
    assert output["financial_resolution"]["recommended_refund_brl"] == 150.0

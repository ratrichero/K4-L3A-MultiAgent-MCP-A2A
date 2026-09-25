from __future__ import annotations

import logging
from typing import Any

from .agents.coordinator import CoordinatorAgent
from .agents.order_agent import ALLOWED_TOOLS as ALLOWED_TOOLS_ORDER
from .agents.order_agent import OrderSpecialistAgent
from .agents.payment_agent import ALLOWED_TOOLS as ALLOWED_TOOLS_PAYMENT
from .agents.payment_agent import PaymentSpecialistAgent
from .agents.policy_agent import ALLOWED_TOOLS as ALLOWED_TOOLS_POLICY
from .agents.policy_agent import PolicySpecialistAgent
from .agents.shipment_agent import ALLOWED_TOOLS as ALLOWED_TOOLS_SHIPMENT
from .agents.shipment_agent import ShipmentSpecialistAgent
from .agents.verifier import VerifierAgent
from .config import Settings
from .core.ledger import EvidenceLedger
from .core.models import PaymentFinding, ShipmentFinding
from .core.scoped_gateway import ScopedEvidenceGateway
from .llm import LLMClient
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

logger = logging.getLogger(__name__)

_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """Get or initialize singleton LLMClient configured with multi-tier fallback."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient(Settings.load())
    return _llm_client


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the full L3A Multi-Agent Workflow for one case."""
    case_id = case["case_id"]
    ledger = EvidenceLedger(case_id)

    # 1. Initialize scoped gateways (least privilege at app-level)
    order_gw = ScopedEvidenceGateway(gateway, ledger, "order_specialist", ALLOWED_TOOLS_ORDER)
    payment_gw = ScopedEvidenceGateway(gateway, ledger, "payment_specialist", ALLOWED_TOOLS_PAYMENT)
    shipment_gw = ScopedEvidenceGateway(
        gateway, ledger, "shipment_specialist", ALLOWED_TOOLS_SHIPMENT
    )
    policy_gw = ScopedEvidenceGateway(gateway, ledger, "policy_specialist", ALLOWED_TOOLS_POLICY)

    # 2. Instantiate agents
    coordinator = CoordinatorAgent(trace)
    order_agent = OrderSpecialistAgent(order_gw, ledger, trace)
    payment_agent = PaymentSpecialistAgent(payment_gw, ledger, trace)
    shipment_agent = ShipmentSpecialistAgent(shipment_gw, ledger, trace)
    policy_agent = PolicySpecialistAgent(policy_gw, ledger, trace)
    verifier = VerifierAgent(trace.contracts, trace)

    # 3. Stage 1: Ingestion & Planning
    plan = coordinator.plan(case)

    # 4. Stage 2: Order & Entity Verification
    order_finding = await order_agent.investigate(plan)

    # 5. Stage 3: Parallel Domain Specialists (Payment & Shipment)
    if order_finding.found and order_finding.order_id:
        payment_finding = await payment_agent.investigate(case_id, order_finding.order_id)
        shipment_finding = await shipment_agent.investigate(case_id, order_finding.order_id)
    else:
        payment_finding = PaymentFinding()
        shipment_finding = ShipmentFinding()

    # 6. Stage 4: Policy Evaluation & Resolution
    draft = await policy_agent.evaluate(plan, order_finding, payment_finding, shipment_finding)

    # 7. Stage 5: Invariants Verification & L3A Output Mapping
    output = verifier.verify_and_finalize(draft, ledger, case_id)

    return output

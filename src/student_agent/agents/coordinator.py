from __future__ import annotations

import logging
from typing import Any

from ..core.models import InvestigationPlan
from ..trace import TraceWriter

logger = logging.getLogger(__name__)


class CoordinatorAgent:
    """Coordinator Agent responsible for case ingestion and planning."""

    def __init__(self, trace: TraceWriter) -> None:
        self.trace = trace

    def plan(self, case: dict[str, Any]) -> InvestigationPlan:
        """Parse raw case input and construct an InvestigationPlan."""
        case_id = case["case_id"]
        customer_request = case.get("customer_request", {})
        customer_message = customer_request.get("message", "")
        claimed_order_id = customer_request.get("claimed_order_id")
        claims = customer_request.get("claims", [])
        policy_version = case.get("policy_version", "EC_POLICY_V1")
        opened_at = case.get("opened_at")

        # Emit task_assigned to initiate the specialist pipeline
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="order_specialist",
            attributes={
                "claimed_order_id": claimed_order_id,
                "claims_count": len(claims),
            },
        )

        return InvestigationPlan(
            case_id=case_id,
            claimed_order_id=claimed_order_id,
            customer_message=customer_message,
            claims=claims,
            policy_version=policy_version,
            opened_at=opened_at,
        )

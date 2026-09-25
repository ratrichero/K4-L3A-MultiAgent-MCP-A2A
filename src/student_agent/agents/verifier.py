from __future__ import annotations

import logging
from typing import Any

from ..contracts import Contracts
from ..core.ledger import EvidenceLedger
from ..core.models import DraftResolution
from ..trace import TraceWriter
from ..utils.invariants import map_draft_to_l3a_output

logger = logging.getLogger(__name__)


class VerifierAgent:
    """Verifier Agent acting as the deterministic quality and invariants gate."""

    def __init__(self, contracts: Contracts, trace: TraceWriter) -> None:
        self.contracts = contracts
        self.trace = trace

    def verify_and_finalize(
        self,
        draft: DraftResolution,
        ledger: EvidenceLedger,
        case_id: str,
    ) -> dict[str, Any]:
        """Verify all domain invariants and schema compliance deterministically."""
        # 1. Map to L3A public output schema & reconcile invariants
        output = map_draft_to_l3a_output(draft, ledger, case_id)

        # 2. Strict public contract validation
        self.contracts.validate_output(output, f"verifier for case {case_id}")

        # 3. Emit verification_completed trace event
        self.trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier",
            decision_code="INVARIANTS_PASSED",
            evidence_refs=output.get("evidence_refs"),
            attributes={
                "primary_issue": output["assessment"]["primary_issue"],
                "case_status": output["assessment"]["case_status"],
                "refund_brl": output["financial_resolution"]["recommended_refund_brl"],
            },
        )
        return output

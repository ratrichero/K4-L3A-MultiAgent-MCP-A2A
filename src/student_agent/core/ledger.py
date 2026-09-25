from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class EvidenceLedger:
    """Isolated evidence ledger for a single case.

    Guarantees:
      1. No cross-case leakage (scoped to one case_id).
      2. Tracks which evidence_refs have actually been consumed by specialists.
      3. Validates evidence_ref provenance before output construction.
    """

    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        self._store: dict[str, dict[str, Any]] = {}
        self._consumed_refs: set[str] = set()

    def record(self, evidence: dict[str, Any]) -> str:
        """Record an authoritative evidence object returned by the MCP gateway."""
        ref = evidence.get("evidence_ref")
        if not ref or not isinstance(ref, str):
            raise ValueError(f"Evidence response missing valid 'evidence_ref': {evidence}")
        self._store[ref] = evidence
        return ref

    def mark_consumed(self, evidence_ref: str) -> None:
        """Mark an evidence_ref as actively consumed in agent findings."""
        if evidence_ref in self._store:
            self._consumed_refs.add(evidence_ref)
        else:
            logger.warning(
                "Attempted to mark unknown evidence_ref '%s' as consumed in case %s",
                evidence_ref,
                self.case_id,
            )

    def get(self, evidence_ref: str) -> dict[str, Any] | None:
        """Retrieve evidence payload by ref."""
        return self._store.get(evidence_ref)

    def contains(self, evidence_ref: str) -> bool:
        """Check if ref exists in this case's ledger."""
        return evidence_ref in self._store

    def all_recorded_refs(self) -> list[str]:
        """All recorded evidence_refs sorted for determinism."""
        return sorted(self._store.keys())

    def consumed_refs(self) -> list[str]:
        """Refs that have been marked as consumed by at least one finding."""
        return sorted(self._consumed_refs)

    def filter_valid_refs(self, refs: list[str]) -> list[str]:
        """Filter a list of refs to keep only those present in this ledger."""
        return [r for r in refs if r in self._store]

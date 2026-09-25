from __future__ import annotations


def calculate_confidence(
    *,
    primary_issue: str,
    entity_found: bool,
    evidence_count: int,
    has_data_conflict: bool,
    issue_certainty: float = 0.90,
) -> float:
    """Calculate calibrated confidence score dynamically based on evidence and conflicts."""
    if primary_issue == "insufficient_evidence":
        # Missing evidence means lower confidence by definition
        return round(min(0.55, max(0.40, 0.45 + 0.02 * evidence_count)), 2)

    if not entity_found:
        # Entity could not be resolved from authoritative records
        return round(0.70 if primary_issue == "unsupported_claim" else 0.50, 2)

    base = issue_certainty
    # Penalty for unresolved or active conflicts
    conflict_penalty = 0.12 if has_data_conflict else 0.0

    # Coverage bonus for having multiple corroborating evidence items
    coverage_bonus = min(0.06, 0.02 * max(0, evidence_count - 1))

    score = base - conflict_penalty + coverage_bonus
    # Bound between 0.10 and 0.96 (never 1.0 to avoid overconfidence penalty)
    return round(min(0.96, max(0.20, score)), 2)

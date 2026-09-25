from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .mcp_gateway import EvidenceGateway, ToolDefinition
from .trace import TraceWriter

ENTITY_KEYS = {
    "order_ids": {"order_id", "order_ids", "claimed_order_id"},
    "item_ids": {"item_id", "item_ids", "order_item_id", "order_item_ids"},
    "seller_ids": {"seller_id", "seller_ids"},
    "payment_references": {
        "payment_id",
        "payment_ids",
        "payment_ref",
        "payment_refs",
        "payment_reference",
        "payment_references",
    },
    "shipment_ids": {"shipment_id", "shipment_ids", "tracking_id", "tracking_ids"},
}

LOOKUP_KEYS = {
    **ENTITY_KEYS,
    "customer_unique_ids": {"customer_unique_id", "customer_unique_ids"},
    "policy_versions": {"policy_version"},
}

ARGUMENT_ENTITY = {
    "order_id": "order_ids",
    "order_ids": "order_ids",
    "item_id": "item_ids",
    "item_ids": "item_ids",
    "order_item_id": "item_ids",
    "seller_id": "seller_ids",
    "seller_ids": "seller_ids",
    "payment_id": "payment_references",
    "payment_ref": "payment_references",
    "payment_reference": "payment_references",
    "payment_references": "payment_references",
    "shipment_id": "shipment_ids",
    "shipment_ids": "shipment_ids",
    "tracking_id": "shipment_ids",
    "customer_unique_id": "customer_unique_ids",
    "customer_unique_ids": "customer_unique_ids",
    "policy_version": "policy_versions",
}

DOMAIN_ACTORS = {
    "order-item-agent": frozenset({"order", "item", "product", "seller", "customer"}),
    "payment-agent": frozenset({"payment", "refund"}),
    "shipment-agent": frozenset({"shipment"}),
    "policy-agent": frozenset({"policy"}),
}


def _walk(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key).lower(), child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str) and value.strip():
        yield value.strip()
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield str(value)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _unique(values: Iterable[str], limit: int = 20) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
        if len(result) == limit:
            break
    return result


def _entity_context(value: Any) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {name: [] for name in LOOKUP_KEYS}
    for key, child in _walk(value):
        for entity_name, aliases in LOOKUP_KEYS.items():
            if key in aliases:
                found[entity_name].extend(_strings(child))
    return {name: _unique(values) for name, values in found.items()}


def _merge_context(target: dict[str, list[str]], value: Any) -> None:
    for name, values in _entity_context(value).items():
        target[name] = _unique([*target[name], *values])


def _tool_domain(tool: ToolDefinition) -> str | None:
    text = f"{tool.name} {tool.description}".lower()
    for domain in (
        "refund",
        "payment",
        "shipment",
        "policy",
        "seller",
        "product",
        "item",
        "customer",
        "order",
    ):
        if domain in text:
            return domain
    return None


def _tool_arguments(
    tool: ToolDefinition,
    context: Mapping[str, list[str]],
    issue: str | None = None,
) -> dict[str, Any] | None:
    schema = tool.input_schema
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    properties = properties if isinstance(properties, dict) else {}
    required = schema.get("required", []) if isinstance(schema, dict) else []
    required = required if isinstance(required, list) else []
    arguments: dict[str, Any] = {}
    for name in properties:
        if name == "case_id":
            continue
        entity_name = ARGUMENT_ENTITY.get(name)
        if entity_name and context.get(entity_name):
            property_schema = properties.get(name, {})
            arguments[name] = (
                list(context[entity_name])
                if isinstance(property_schema, dict) and property_schema.get("type") == "array"
                else context[entity_name][0]
            )
        elif (
            name
            in {
                "issue",
                "issue_type",
                "topic",
                "policy_type",
                "policy_code",
                "policy_topic",
            }
            and issue
        ):
            arguments[name] = issue
    if any(name != "case_id" and name not in arguments for name in required):
        return None
    # Never make an unscoped bulk data call. Policy lookup is the only safe exception.
    if _tool_domain(tool) != "policy" and not arguments:
        return None
    return arguments


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_ref: str
    result_hash: str
    domain: str
    data: Any
    tool_name: str
    actor: str


@dataclass
class EvidenceLedger:
    case_id: str
    records: list[EvidenceRecord] = field(default_factory=list)
    refs: set[str] = field(default_factory=set)

    def consume(
        self,
        evidence: Mapping[str, Any],
        *,
        tool_name: str,
        actor: str,
        allowed_domains: frozenset[str],
    ) -> EvidenceRecord:
        evidence_ref = evidence["evidence_ref"]
        domain = evidence["domain"]
        if domain not in allowed_domains:
            raise ValueError(f"{actor} cannot consume {domain!r} evidence from {tool_name}")
        if evidence_ref in self.refs:
            raise ValueError(f"duplicate evidence_ref returned by gateway: {evidence_ref}")
        record = EvidenceRecord(
            evidence_ref=evidence_ref,
            result_hash=evidence["result_hash"],
            domain=domain,
            data=deepcopy(evidence["data"]),
            tool_name=tool_name,
            actor=actor,
        )
        self.records.append(record)
        self.refs.add(evidence_ref)
        return record

    def refs_for(self, domains: set[str]) -> list[str]:
        return [record.evidence_ref for record in self.records if record.domain in domains]


@dataclass
class SpecialistAgent:
    name: str
    domains: frozenset[str]

    async def investigate(
        self,
        *,
        case_id: str,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        tools: Iterable[ToolDefinition],
        context: dict[str, list[str]],
        ledger: EvidenceLedger,
        issue: str | None = None,
    ) -> None:
        candidates = [tool for tool in tools if _tool_domain(tool) in self.domains]
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=self.name,
            decision_code="DOMAIN_EVIDENCE_REQUESTED",
            attributes={"candidate_tools": len(candidates)},
        )
        consumed = skipped = failures = 0
        consumed_refs: list[str] = []
        pending = candidates
        while pending:
            deferred: list[ToolDefinition] = []
            attempted = 0
            for tool in pending:
                arguments = _tool_arguments(tool, context, issue)
                if arguments is None:
                    deferred.append(tool)
                    continue
                attempted += 1
                try:
                    evidence = await gateway.call(tool.name, case_id=case_id, **arguments)
                except RuntimeError as exc:
                    message = str(exc).lower()
                    if any(token in message for token in ("403", "forbidden", "unauthorized")):
                        raise
                    failures += 1
                    continue
                record = ledger.consume(
                    evidence,
                    tool_name=tool.name,
                    actor=self.name,
                    allowed_domains=self.domains,
                )
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=self.name,
                    tool_name=tool.name,
                    evidence_refs=[record.evidence_ref],
                    attributes={"domain": record.domain},
                )
                _merge_context(context, record.data)
                consumed_refs.append(record.evidence_ref)
                consumed += 1
            if attempted == 0:
                skipped += len(deferred)
                break
            pending = deferred
        if self.name == "policy-agent" and consumed_refs:
            trace.emit(
                case_id=case_id,
                event_type="policy_decided",
                actor=self.name,
                target="coordinator",
                decision_code="POLICY_EVALUATED",
                evidence_refs=consumed_refs,
            )
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=self.name,
            target="coordinator",
            decision_code="EVIDENCE_HANDOFF",
            attributes={"consumed": consumed, "skipped": skipped, "failures": failures},
        )


def _domain_values(ledger: EvidenceLedger, domains: set[str], keys: set[str]) -> list[Any]:
    values: list[Any] = []
    for record in ledger.records:
        if record.domain in domains:
            values.extend(
                value
                for key, value in _walk(record.data)
                if key in keys and not isinstance(value, (dict, list))
            )
    return values


def _first_text(ledger: EvidenceLedger, domains: set[str], keys: set[str]) -> str | None:
    for value in _domain_values(ledger, domains, keys):
        if value is not None:
            return str(value).strip().lower()
    return None


def _decimals(ledger: EvidenceLedger, domains: set[str], keys: set[str]) -> list[Decimal]:
    result: list[Decimal] = []
    for value in _domain_values(ledger, domains, keys):
        if isinstance(value, bool) or value is None:
            continue
        try:
            result.append(Decimal(str(value)))
        except InvalidOperation:
            continue
    return result


def _total(ledger: EvidenceLedger, domains: set[str], keys: set[str]) -> Decimal:
    return sum(_decimals(ledger, domains, keys), Decimal("0"))


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _explicit_duplicate(ledger: EvidenceLedger) -> bool:
    values = _domain_values(
        ledger, {"payment"}, {"duplicate", "is_duplicate", "duplicate_charge", "duplicate_of"}
    )
    return any(
        value is True or str(value).strip().lower() in {"true", "yes", "duplicate"}
        for value in values
    )


@dataclass(frozen=True)
class Assessment:
    issue: str
    status: str
    confidence: float
    cause: str
    party_type: str
    relevant_domains: set[str]
    refund: Decimal = Decimal("0")


def _assess(ledger: EvidenceLedger) -> Assessment:
    order_status = _first_text(ledger, {"order"}, {"order_status", "status"})
    payment_total = _total(ledger, {"payment"}, {"payment_value", "amount_brl"})
    item_total = _total(ledger, {"item"}, {"price", "freight_value"})
    refund_status = _first_text(ledger, {"refund"}, {"refund_status", "status"})
    refund_amount = _total(ledger, {"refund"}, {"refund_amount", "amount_brl", "amount"})
    if refund_status in {"failed", "rejected", "error"}:
        return Assessment(
            "refund_failed",
            "action_required",
            0.95,
            "REFUND_FAILED",
            "payment_provider",
            {"refund", "payment", "policy"},
            refund_amount,
        )
    if refund_status in {"pending", "processing", "requested"}:
        return Assessment(
            "refund_pending",
            "needs_investigation",
            0.85,
            "REFUND_PENDING",
            "payment_provider",
            {"refund", "payment", "policy"},
            refund_amount,
        )
    if order_status in {"canceled", "cancelled"} and payment_total > 0:
        return Assessment(
            "canceled_order_paid",
            "action_required",
            0.98,
            "PAID_ORDER_CANCELED",
            "platform",
            {"order", "payment", "policy"},
            payment_total,
        )
    if order_status == "unavailable" and payment_total > 0:
        return Assessment(
            "unavailable_order_paid",
            "action_required",
            0.98,
            "PAID_ORDER_UNAVAILABLE",
            "platform",
            {"order", "payment", "policy"},
            payment_total,
        )
    if _explicit_duplicate(ledger):
        refund = max(payment_total - item_total, Decimal("0"))
        return Assessment(
            "duplicate_charge",
            "action_required",
            0.95,
            "DUPLICATE_PAYMENT",
            "payment_provider",
            {"payment", "order", "item", "policy"},
            refund,
        )
    delivered = _date(
        _first_text(
            ledger, {"order", "shipment"}, {"delivered_at", "order_delivered_customer_date"}
        )
    )
    estimated = _date(
        _first_text(
            ledger,
            {"order", "shipment"},
            {"estimated_delivery", "estimated_delivery_date", "order_estimated_delivery_date"},
        )
    )
    if delivered and estimated and delivered > estimated:
        carrier = _date(
            _first_text(
                ledger, {"order", "shipment"}, {"shipped_at", "order_delivered_carrier_date"}
            )
        )
        limits = [
            parsed
            for value in _domain_values(ledger, {"item"}, {"shipping_limit_date"})
            if (parsed := _date(str(value))) is not None
        ]
        if carrier and limits and carrier > max(limits):
            return Assessment(
                "late_delivery_seller",
                "action_required",
                0.9,
                "SELLER_DISPATCH_LATE",
                "seller",
                {"order", "item", "shipment", "seller", "policy"},
            )
        return Assessment(
            "late_delivery_logistics",
            "action_required",
            0.9,
            "LOGISTICS_DELIVERY_LATE",
            "logistics_provider",
            {"order", "shipment", "policy"},
        )
    payment_rows = _domain_values(
        ledger, {"payment"}, {"payment_sequential", "payment_id", "payment_reference"}
    )
    if item_total > 0 and payment_total > 0:
        if abs(payment_total - item_total) > Decimal("0.01"):
            refund = max(payment_total - item_total, Decimal("0"))
            return Assessment(
                "payment_mismatch",
                "action_required",
                0.92,
                "PAYMENT_TOTAL_MISMATCH",
                "payment_provider",
                {"order", "item", "payment", "policy"},
                refund,
            )
        if len(payment_rows) > 1:
            return Assessment(
                "valid_split_payment",
                "no_action",
                0.95,
                "VALID_SPLIT_PAYMENT",
                "customer",
                {"order", "item", "payment", "policy"},
            )
    if ledger.records:
        return Assessment(
            "unsupported_claim",
            "no_action",
            0.75,
            "CLAIM_NOT_CORROBORATED",
            "unknown",
            {record.domain for record in ledger.records},
        )
    return Assessment(
        "insufficient_evidence",
        "needs_investigation",
        0.0,
        "AUTHORITATIVE_EVIDENCE_MISSING",
        "unknown",
        set(),
    )


def _claim_ids(case: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    customer_request = case.get("customer_request", {})
    claims = customer_request.get("claims", []) if isinstance(customer_request, Mapping) else []
    if isinstance(claims, list):
        for index, claim in enumerate(claims, 1):
            if isinstance(claim, Mapping):
                value = claim.get("claim_id", claim.get("id", f"claim_{index}"))
            else:
                value = f"claim_{index}"
            values.append(str(value)[:64])
    return _unique(values, 5)


def _resolution_actions(issue: str) -> list[str]:
    return {
        "canceled_order_paid": ["INITIATE_REFUND", "NOTIFY_CUSTOMER"],
        "unavailable_order_paid": ["INITIATE_REFUND", "NOTIFY_CUSTOMER"],
        "duplicate_charge": ["REFUND_DUPLICATE_CHARGE", "NOTIFY_CUSTOMER"],
        "payment_mismatch": ["RECONCILE_PAYMENT", "REFUND_OVERPAYMENT_IF_CONFIRMED"],
        "refund_pending": ["FOLLOW_UP_REFUND", "NOTIFY_CUSTOMER"],
        "refund_failed": ["RETRY_OR_ESCALATE_REFUND", "NOTIFY_CUSTOMER"],
        "late_delivery_seller": ["REVIEW_SELLER_SLA", "NOTIFY_CUSTOMER"],
        "late_delivery_logistics": ["REVIEW_LOGISTICS_SLA", "NOTIFY_CUSTOMER"],
        "valid_split_payment": ["NO_FINANCIAL_ACTION"],
        "unsupported_claim": ["NO_ACTION_EVIDENCE_DOES_NOT_SUPPORT_CLAIM"],
        "insufficient_evidence": ["REQUEST_MANUAL_INVESTIGATION"],
    }[issue]


def _build_output(
    case: Mapping[str, Any],
    context: Mapping[str, list[str]],
    ledger: EvidenceLedger,
    assessment: Assessment,
) -> dict[str, Any]:
    evidence_refs = ledger.refs_for(assessment.relevant_domains)
    party_id = (
        context["seller_ids"][0]
        if assessment.party_type == "seller" and context["seller_ids"]
        else None
    )
    refund = assessment.refund.quantize(Decimal("0.01"))
    output: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": str(case["case_id"]),
        "assessment": {
            "primary_issue": assessment.issue,
            "case_status": assessment.status,
            "confidence": assessment.confidence,
        },
        "affected_entities": {name: list(context.get(name, [])) for name in ENTITY_KEYS},
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": assessment.cause, "rank": 1}],
            "responsible_parties": [{"party_type": assessment.party_type, "party_id": party_id}],
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(refund),
            "refund_lines": (
                [
                    {
                        "reason_code": assessment.cause,
                        "amount_brl": float(refund),
                        "entity_id": context["order_ids"][0] if context["order_ids"] else None,
                    }
                ]
                if refund > 0
                else []
            ),
        },
        "resolution_actions": _resolution_actions(assessment.issue),
    }
    claims = _claim_ids(case)
    if claims:
        verdict = (
            "insufficient_evidence"
            if assessment.issue == "insufficient_evidence"
            else "unsupported"
            if assessment.issue == "unsupported_claim"
            else "supported"
        )
        output["claim_assessments"] = [
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": assessment.confidence,
                "evidence_refs": evidence_refs,
            }
            for claim_id in claims
        ]
    return output


def _verify(output: Mapping[str, Any], ledger: EvidenceLedger) -> None:
    output_refs = output.get("evidence_refs", [])
    if not isinstance(output_refs, list) or not set(output_refs).issubset(ledger.refs):
        raise ValueError("output contains an evidence_ref not returned for this case")
    for claim in output.get("claim_assessments", []):
        if not set(claim["evidence_refs"]).issubset(set(output_refs)):
            raise ValueError("claim evidence_refs must exist in top-level evidence_refs")
    financial = output["financial_resolution"]
    line_total = round(sum(line["amount_brl"] for line in financial["refund_lines"]), 2)
    if line_total != round(financial["recommended_refund_brl"], 2):
        raise ValueError("refund lines do not match recommended_refund_brl")


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinate scoped specialists and return an evidence-backed result."""
    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case must contain a non-empty case_id")
    tools = await gateway.list_tool_definitions()
    if not tools:
        raise RuntimeError("MCP Gateway returned no tool definitions")
    context = _entity_context(case)
    ledger = EvidenceLedger(case_id)
    for name, domains in DOMAIN_ACTORS.items():
        if name == "policy-agent":
            continue
        await SpecialistAgent(name, domains).investigate(
            case_id=case_id,
            gateway=gateway,
            trace=trace,
            tools=tools,
            context=context,
            ledger=ledger,
        )
    preliminary = _assess(ledger)
    await SpecialistAgent("policy-agent", DOMAIN_ACTORS["policy-agent"]).investigate(
        case_id=case_id,
        gateway=gateway,
        trace=trace,
        tools=tools,
        context=context,
        ledger=ledger,
        issue=preliminary.issue,
    )
    assessment = _assess(ledger)
    output = _build_output(case, context, ledger, assessment)
    _verify(output, ledger)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="VERIFIED",
        evidence_refs=output["evidence_refs"],
        attributes={"evidence_count": len(output["evidence_refs"])},
    )
    return output

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2

from student_agent import OUTPUT_SCHEMA_VERSION, VARIANT_ID, cli
from student_agent.cases import CaseSet
from student_agent.config import Settings
from student_agent.contracts import Contracts


def valid_output(case_id: str) -> dict[str, Any]:
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0,
        },
        "affected_entities": {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "root_cause_analysis": {
            "ranked_causes": [],
            "responsible_parties": [],
        },
        "evidence_refs": [],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0,
            "refund_lines": [],
        },
        "resolution_actions": ["REQUEST_MANUAL_INVESTIGATION"],
    }


def install_run_dependencies(
    monkeypatch: Any,
    tmp_path: Path,
    case_set: CaseSet,
    contracts: Contracts,
) -> None:
    settings = Settings("http://competition", "sk-team-1234567890abcdef", "http://mcp", tmp_path)
    monkeypatch.setattr(cli.Settings, "load", lambda _root: settings)
    monkeypatch.setattr(cli, "load_case_set", lambda _root: case_set)
    monkeypatch.setattr(cli, "Contracts", lambda _root: contracts)


def test_run_resumes_without_deleting_valid_outputs_or_trace(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    contracts_root = Path(__file__).resolve().parents[1] / "contracts" / "schemas"
    contracts = Contracts(contracts_root)
    case_ids = ("L3A_CASE_001", "L3A_CASE_002")
    case_set = CaseSet(
        "test-v1",
        VARIANT_ID,
        case_ids,
        {case_id: {"case_id": case_id} for case_id in case_ids},
    )
    install_run_dependencies(monkeypatch, tmp_path, case_set, contracts)

    output_root = tmp_path / "outputs"
    output_root.mkdir()
    existing_path = output_root / "L3A_CASE_001.json"
    existing_bytes = json.dumps(valid_output("L3A_CASE_001")).encode()
    existing_path.write_bytes(existing_bytes)
    trace_path = tmp_path / "traces" / "trace.jsonl"
    trace_path.parent.mkdir()
    trace_path.write_text("existing-trace-line\n", encoding="utf-8")

    connections: list[object] = []
    solved: list[str] = []

    class Gateway:
        async def list_tools(self) -> list[str]:
            return ["get_order"]

    @asynccontextmanager
    async def connect(*_args: Any) -> Any:
        gateway = Gateway()
        connections.append(gateway)
        yield gateway

    async def solve(case: dict[str, Any], _gateway: Any, _trace: Any) -> dict[str, Any]:
        solved.append(case["case_id"])
        return valid_output(case["case_id"])

    monkeypatch.setattr(cli, "connect_gateway", connect)
    monkeypatch.setattr(cli, "solve_case", solve)
    asyncio.run(cli._run(tmp_path))

    assert existing_path.read_bytes() == existing_bytes
    assert solved == ["L3A_CASE_002"]
    assert len(connections) == 1
    assert trace_path.read_text(encoding="utf-8").startswith("existing-trace-line\n")
    output = capsys.readouterr().out
    assert "[1/2] Processing L3A_CASE_001." in output
    assert "[2/2] Processing L3A_CASE_002." in output


def test_run_retries_network_errors_with_new_connections(tmp_path: Path, monkeypatch: Any) -> None:
    contracts_root = Path(__file__).resolve().parents[1] / "contracts" / "schemas"
    contracts = Contracts(contracts_root)
    case_id = "L3A_CASE_021"
    case_set = CaseSet("test-v1", VARIANT_ID, (case_id,), {case_id: {"case_id": case_id}})
    install_run_dependencies(monkeypatch, tmp_path, case_set, contracts)

    connections: list[object] = []
    attempts = 0
    delays: list[int] = []

    class Gateway:
        async def list_tools(self) -> list[str]:
            return ["get_order"]

    @asynccontextmanager
    async def connect(*_args: Any) -> Any:
        gateway = Gateway()
        connections.append(gateway)
        yield gateway

    async def solve(case: dict[str, Any], _gateway: Any, _trace: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ExceptionGroup(
                "MCP task group failed",
                [httpx2.ReadError("wrapped MCP read failure")],
            )
        if attempts <= 3:
            raise httpx2.ReadError("temporary MCP read failure")
        return valid_output(case["case_id"])

    async def no_wait(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr(cli, "connect_gateway", connect)
    monkeypatch.setattr(cli, "solve_case", solve)
    monkeypatch.setattr(cli.asyncio, "sleep", no_wait)
    asyncio.run(cli._run(tmp_path))

    assert attempts == 4
    assert len(connections) == 4
    assert len({id(connection) for connection in connections}) == 4
    assert delays == [2, 4, 8]
    assert (tmp_path / "outputs" / f"{case_id}.json").is_file()

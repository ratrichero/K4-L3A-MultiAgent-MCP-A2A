from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx2

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


RETRY_DELAYS = (2, 4, 8)
RETRYABLE_MCP_ERRORS = (
    httpx2.TimeoutException,
    httpx2.NetworkError,
    ConnectionError,
    TimeoutError,
)


def _is_retryable_mcp_error(error: BaseException) -> bool:
    if isinstance(error, RETRYABLE_MCP_ERRORS):
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(_is_retryable_mcp_error(child) for child in error.exceptions)
    return False


def _valid_existing_output(path: Path, case_id: str, contracts: Contracts) -> bool:
    if not path.is_file():
        return False
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
        contracts.validate_output(value, f"outputs/{case_id}.json")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    return isinstance(value, dict) and value.get("case_id") == case_id


def _write_output(path: Path, output: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


async def _process_case_once(
    *,
    case: dict[str, Any],
    settings: Settings,
    contracts: Contracts,
    trace: TraceWriter,
    target: Path,
) -> None:
    case_id = case["case_id"]
    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        output = await solve_case(case, gateway, trace)
    contracts.validate_output(output, f"outputs/{case_id}.json")
    if output.get("case_id") != case_id:
        raise ValueError(f"solver returned a mismatched case_id for {case_id}")
    # A valid output on disk is the resume checkpoint, so finalize the trace first.
    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
    _write_output(target, output)


async def _process_case_with_retry(
    *,
    case: dict[str, Any],
    settings: Settings,
    contracts: Contracts,
    trace: TraceWriter,
    target: Path,
) -> None:
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            await _process_case_once(
                case=case,
                settings=settings,
                contracts=contracts,
                trace=trace,
                target=target,
            )
            return
        except Exception as exc:
            if not _is_retryable_mcp_error(exc) or attempt == len(RETRY_DELAYS):
                raise
            delay = RETRY_DELAYS[attempt]
            print(
                f"MCP network error for {case['case_id']}; retrying in {delay}s "
                f"({attempt + 1}/{len(RETRY_DELAYS)}).",
                file=sys.stderr,
                flush=True,
            )
            await asyncio.sleep(delay)


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _run(root: Path) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace = TraceWriter(trace_path, contracts)
    total = len(case_set.case_ids)
    for index, case_id in enumerate(case_set.case_ids, 1):
        print(f"[{index}/{total}] Processing {case_id}.", flush=True)
        target = output_root / f"{case_id}.json"
        if _valid_existing_output(target, case_id, contracts):
            print(f"[{index}/{total}] Skipping {case_id}: valid output exists.", flush=True)
            continue
        await _process_case_with_retry(
            case=case_set.cases[case_id],
            settings=settings,
            contracts=contracts,
            trace=trace,
            target=target,
        )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    commands.add_parser("run", help="run the implemented workflow for all cases")
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

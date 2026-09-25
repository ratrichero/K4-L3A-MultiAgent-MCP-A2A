from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _run(
    root: Path,
    resume: bool = False,
    start_from: str | None = None,
    single_case: str | None = None,
) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)

    is_resume_mode = resume or (start_from is not None) or (single_case is not None)

    all_case_ids = list(case_set.case_ids)
    if single_case:
        if single_case not in case_set.cases:
            raise ValueError(f"Unknown case_id: {single_case}")
        target_case_ids = [single_case]
    elif start_from:
        if start_from not in case_set.cases:
            raise ValueError(f"Unknown start-from case_id: {start_from}")
        start_idx = all_case_ids.index(start_from)
        target_case_ids = all_case_ids[start_idx:]
    else:
        target_case_ids = all_case_ids

    # In fresh run, clear stale output and trace files
    if not is_resume_mode:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
    else:
        # In resume mode, clean up any partial trace events for cases we are about to re-run
        target_set = set(target_case_ids)
        if trace_path.exists():
            valid_lines: list[str] = []
            for line in trace_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                    if ev.get("case_id") not in target_set:
                        valid_lines.append(line)
                except Exception:
                    continue
            trace_path.write_text(
                "\n".join(valid_lines) + ("\n" if valid_lines else ""), encoding="utf-8"
            )

    trace = TraceWriter(trace_path, contracts)

    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gw_check:
        discovered_tools = await gw_check.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        print(f"Connected to MCP Gateway. Discovered {len(discovered_tools)} tools.")

    total_all = len(all_case_ids)
    mode_desc = "Resume" if is_resume_mode else "Fresh run"
    print(f"Workflow active: {len(target_case_ids)} cases to execute (Mode: {mode_desc}).\n")

    for case_id in target_case_ids:
        idx = all_case_ids.index(case_id) + 1
        target_file = output_root / f"{case_id}.json"

        # If --resume flag was used without start_from/single_case,
        # skip if valid output already exists
        if resume and not start_from and not single_case and target_file.exists():
            try:
                existing_data = json.loads(target_file.read_text(encoding="utf-8"))
                contracts.validate_output(existing_data, f"outputs/{case_id}.json")
                issue = existing_data.get("assessment", {}).get("primary_issue")
                refund = existing_data.get("financial_resolution", {}).get(
                    "recommended_refund_brl", 0.0
                )
                print(
                    f"[{idx:>3}/{total_all}] Skipping {case_id} (completed: {issue}, {refund} BRL)"
                )
                continue
            except Exception:
                pass

        case = case_set.cases[case_id]
        print(f"[{idx:>3}/{total_all}] Processing {case_id}...", end="", flush=True)
        trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")

        output = None
        for attempt in range(3):
            try:
                async with connect_gateway(
                    settings.mcp_endpoint, settings.team_api_key, contracts
                ) as gateway:
                    output = await solve_case(case, gateway, trace)
                    break
            except Exception as exc:
                if attempt < 2:
                    print(
                        f"\n      [Retry {attempt + 1}/3] {case_id}: {exc}. Retrying in 2s...",
                        flush=True,
                    )
                    await asyncio.sleep(2.0)
                else:
                    raise

        contracts.validate_output(output, f"outputs/{case_id}.json")
        if output.get("case_id") != case_id:
            raise ValueError(f"solver returned a mismatched case_id for {case_id}")
        target = output_root / f"{case_id}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(target)
        trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")

        issue = output.get("assessment", {}).get("primary_issue")
        refund = output.get("financial_resolution", {}).get("recommended_refund_brl", 0.0)
        refs = len(output.get("evidence_refs", []))
        print(f" -> {issue} | refund: {refund} BRL | {refs} refs | OK", flush=True)
        await asyncio.sleep(0.3)

    print(
        f"\nExecution finished! Total outputs: {len(list(output_root.glob('*.json')))}/{total_all}"
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run_cmd = commands.add_parser("run", help="run the implemented workflow for all cases")
    run_cmd.add_argument(
        "--resume", action="store_true", help="resume execution, skipping existing valid outputs"
    )
    run_cmd.add_argument(
        "--start-from", help="start execution from a specific case_id (e.g. L3A_CASE_017)"
    )
    run_cmd.add_argument("--case", help="run only a single specific case_id (e.g. L3A_CASE_017)")
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
            asyncio.run(
                _run(
                    root,
                    resume=args.resume,
                    start_from=args.start_from,
                    single_case=args.case,
                )
            )
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

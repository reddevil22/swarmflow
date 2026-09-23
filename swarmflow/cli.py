"""swarmflow command line interface.

Argument parsing, process exit codes and output formatting; the wave pipeline lives in
``swarmflow/pipeline.py``.
"""

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import yaml

from .audit import audit, freeze
from .config import (DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG_PATH,
                     LEGACY_CONFIG_PATH, load_config, resolve_paths)
from .frontier import FrontierError, build_backend
from .ledger import Ledger
from .trace import scan_trace
from .workers import WorkerRunner
from . import pipeline, runstate


def _frontier(config) -> object:
    return build_backend(config["frontier"])


def _runner(config, ledger, project_root: str) -> WorkerRunner:
    return WorkerRunner(config, ledger, project_root)


def cmd_init(args) -> int:
    existing = DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.exists() else (
        LEGACY_CONFIG_PATH if LEGACY_CONFIG_PATH.exists() else None)
    if existing:
        print(f"config already exists: {existing}")
        return 0
    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXAMPLE_CONFIG_PATH, DEFAULT_CONFIG_PATH)
    print(f"wrote {DEFAULT_CONFIG_PATH}")
    print("edit worker.model (your local model id) and any CLI paths, then run:")
    print("  swarmflow smoke-frontier && swarmflow smoke-worker")
    return 0


def cmd_smoke_frontier(args, config) -> int:
    try:
        client = _frontier(config)
    except (FileNotFoundError, FrontierError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"FRONTIER FAIL: {exc}")
        return 1
    try:
        result = client.complete("Reply with exactly: frontier-ok")
    except FrontierError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"FRONTIER FAIL: {exc}")
        return 1
    ok = result["ok"] and "frontier-ok" in result["final_text"]
    role = (config.get("role_providers") or {}).get("frontier") or "-"
    model = config["frontier"].get("model") or "-"
    endpoint = config["frontier"].get("base_url") or ""
    if args.json:
        print(json.dumps({"ok": ok, "backend": result.get("backend"),
                          "provider": role, "model": model, "base_url": endpoint,
                          "subtype": result["subtype"], "usage": result["usage"],
                          "text": result["final_text"]}))
        return 0 if ok else 1
    print(f"FRONTIER {'OK' if ok else 'FAIL'} backend={result.get('backend')} "
          f"provider={role} model={model} {'endpoint=' + endpoint + ' ' if endpoint else ''}"
          f"subtype={result['subtype']} exit={result['exit_code']} "
          f"usage={result['usage']} text={result['final_text']!r}")
    return 0 if ok else 1


def cmd_smoke_worker(args, config) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="swarmflow_worker_smoke_"))
    ledger = Ledger(str(workdir / "state" / "ledger.db"))
    try:
        runner = _runner(config, ledger, str(workdir))
        result = runner.run_inline("Reply with exactly: worker-ok")
    except (ValueError, FileNotFoundError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"WORKER FAIL: {exc}")
        ledger.close()
        return 1
    ok = "worker-ok" in result["scan"].get("last_text", "")
    role = (config.get("role_providers") or {}).get("worker") or "-"
    model = config["worker"].get("model") or "-"
    if args.json:
        print(json.dumps({"ok": ok, "outcome": result["outcome"],
                          "provider": role, "model": model,
                          "turns": result["scan"]["turns"],
                          "trace_dir": str(runner.logs_dir)}))
    else:
        print(f"WORKER {'OK' if ok else 'FAIL'} provider={role} model={model} "
              f"outcome={result['outcome']} "
              f"turns={result['scan']['turns']} trace_dir={runner.logs_dir}")
    ledger.close()
    return 0 if ok else 1


def cmd_recon(args, config) -> int:
    from .recon import digest, recon
    override = args.regression_command or config["regression"].get("command") or ""
    info = recon(args.project, regression_command=override)
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        print(digest(info))
    return 0


def cmd_evidence(args, config) -> int:
    from .evidence import write_bundle
    ledger = Ledger(config["paths"]["ledger"])
    path = write_bundle(str(runstate.resolve_project(args.project)), ledger,
                        ignores=config["audit"]["ignore_extra"])
    ledger.close()
    if args.json:
        print(json.dumps({"bundle": str(path), "bytes": path.stat().st_size}))
    else:
        print(f"evidence bundle written: {path} ({path.stat().st_size} bytes)")
    return 0


def cmd_trace(args, config) -> int:
    scan = scan_trace(args.file, config["worker"]["max_output_tokens"])
    print(json.dumps(scan, indent=2))
    return 0


def cmd_plan_load(args, config) -> int:
    return pipeline.plan_load(args.plan, args.project, config,
                              allow_dirty=args.allow_dirty, kill_stale=args.kill_stale,
                              json_output=args.json, worktree=args.worktree,
                              worktree_path=args.worktree_path)


def cmd_plan(args, config) -> int:
    """Frontier planning: PRD (+ recon digest) -> validated plan.yaml."""
    from .roles import RoleError, plan_from_prd
    if getattr(args, "worktree", False) and not getattr(args, "load", False):
        print("--worktree applies to a loaded run; add --load (or drop --worktree)")
        return 2
    prd_path = Path(args.prd)
    if not prd_path.exists():
        print(f"PRD not found: {prd_path}")
        return 2
    project_root = Path(args.project).resolve()
    out = Path(args.out) if args.out else project_root / ".swarmflow" / "plan.yaml"
    if out.exists() and not args.force:
        print(f"{out} already exists (pass --force to overwrite)")
        return 2
    prd_text = prd_path.read_text(encoding="utf-8")
    try:
        result = plan_from_prd(config, prd_text, str(project_root), args.mode)
    except RoleError as exc:
        print(f"PLAN FAILED: {exc}")
        return exc.code
    except FrontierError as exc:
        print(f"PLAN FAILED (frontier): {exc}")
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(result["plan"], sort_keys=False), encoding="utf-8")
    prd_store = project_root / ".swarmflow" / "PRD.md"
    prd_store.parent.mkdir(parents=True, exist_ok=True)
    prd_store.write_text(prd_text, encoding="utf-8")
    data = runstate.load_run(str(project_root))
    data["prd_path"] = str(prd_store)
    data["prd_sha256"] = hashlib.sha256(prd_text.encode("utf-8")).hexdigest()
    runstate.persist_run(str(project_root), data)
    if args.json:
        print(json.dumps({"plan": str(out), "tasks": result["tasks"],
                          "waves": result["waves"], "retried": result["retried"],
                          "artifact": result["artifact"]}))
    else:
        print(f"plan written: {out} ({result['tasks']} tasks, "
              f"waves {result['waves']}){' [retried once]' if result['retried'] else ''}")
        print(f"raw model output: {result['artifact']}")
    if args.load:
        return pipeline.plan_load(str(out), str(project_root), config,
                                  worktree=getattr(args, "worktree", False),
                                  worktree_path=getattr(args, "worktree_path", None))
    return 0


def cmd_verify(args, config) -> int:
    """Frontier verification of one task against its spec."""
    from .roles import RoleError, verify_task
    with Ledger(config["paths"]["ledger"]) as ledger:
        task = ledger.get(args.task)
        if not task:
            print(f"unknown task: {args.task}")
            return 2
        project_root = str(runstate.resolve_project(args.project or task["project"]))
        try:
            result = verify_task(config, project_root, ledger, args.task)
        except RoleError as exc:
            print(f"VERIFY FAILED: {exc}")
            return exc.code
        except FrontierError as exc:
            print(f"VERIFY FAILED (frontier): {exc}")
            return 2
        if args.json:
            print(json.dumps(result))
        else:
            print(f"verify {result['task_id']}: {result['verdict']} -> {result['status']}")
            for finding in result["findings"][:10]:
                print(f"  - [{finding.get('severity')}] {finding.get('summary')}")
            for item in result["uncovered"][:10]:
                print(f"  uncovered: {item}")
            print(f"  artifact: {result['artifact']}")
        return 0 if result["verdict"] == "pass" else 1


def cmd_retry(args, config) -> int:
    """Re-queue a needs_fix/failed task; the next dispatch injects the findings."""
    from .prompt import latest_verdict, worker_outcome
    with Ledger(config["paths"]["ledger"]) as ledger:
        task = ledger.get(args.task)
        if not task:
            print(f"unknown task: {args.task}")
            return 2
        if task["status"] not in ("needs_fix", "failed"):
            print(f"task {task['id']} is {task['status']}; retry takes a needs_fix or "
                  "failed task")
            return 2
        if task["attempts"] >= 3:
            print(f"task {task['id']} already used {task['attempts']} attempts; "
                  "escalate or re-plan instead of retrying")
            return 2
        project_root = str(runstate.resolve_project(args.project or task["project"]))
        verdict = latest_verdict(project_root, task["id"])
        findings = []
        if verdict and str(verdict.get("verdict", "")).lower() != "pass":
            findings = verdict.get("findings") or []
        outcome = worker_outcome(task)
        ledger.set_status(task["id"], "queued")
        ledger.record_event(task["id"], "retry", json.dumps({
            "attempts": task["attempts"], "findings": len(findings),
            "outcome": outcome})[:500])
        wave = task["wave"]
    next_step = f"swarmflow wave-run --wave {wave}"
    if args.json:
        print(json.dumps({"task": task["id"], "status": "queued",
                          "attempts": task["attempts"], "findings": findings,
                          "outcome": outcome, "next": next_step}))
    else:
        print(f"retry {task['id']}: queued (attempt {task['attempts'] + 1} receives "
              f"{len(findings)} finding(s) in its brief)")
        for finding in findings[:10]:
            print(f"  - [{finding.get('severity')}] {finding.get('summary')}")
        if not findings:
            print(f"  no verify findings; plain re-dispatch"
                  + (f" (previous outcome: {outcome})" if outcome else ""))
        print(f"next: {next_step} (re-runs that wave's gates and any other queued task)")
    return 0


def cmd_accept(args, config) -> int:
    """Frontier acceptance over the frozen criteria and the evidence bundle."""
    from .roles import RoleError, accept_run
    with Ledger(config["paths"]["ledger"]) as ledger:
        try:
            result = accept_run(config, str(runstate.resolve_project(args.project)),
                                ledger)
        except RoleError as exc:
            print(f"ACCEPT FAILED: {exc}")
            return exc.code
        except FrontierError as exc:
            print(f"ACCEPT FAILED (frontier): {exc}")
            return 2
        if args.json:
            print(json.dumps(result))
        else:
            print(f"acceptance: {result['verdict']} ({len(result['criteria'])} criteria, "
                  f"{len(result['gaps'])} gaps)")
            for gap in result["gaps"][:10]:
                print(f"  gap: {gap.get('criterion')} - {gap.get('why')}")
            for risk in result["residual_risks"][:10]:
                print(f"  risk: {risk}")
            if result["accepted_tasks"]:
                print(f"  accepted tasks: {', '.join(result['accepted_tasks'])}")
            print(f"  artifact: {result['artifact']}")
        return 0 if result["verdict"] == "accepted" else 1


def cmd_wave_run(args, config) -> int:
    with Ledger(config["paths"]["ledger"]) as ledger:
        rc, _ = pipeline.run_wave(
            ledger, config, wave=args.wave, concurrency=args.concurrency,
            verify=getattr(args, "verify", False), skip_regression=args.skip_regression,
            strict=args.strict, rebaseline=args.rebaseline, quiet=args.json)
    return rc


def cmd_status(args, config) -> int:
    ledger = Ledger(config["paths"]["ledger"])
    counts = ledger.counts()
    tasks = ledger.list_tasks()
    ledger.close()
    if args.json:
        print(json.dumps({"counts": counts, "tasks": tasks}))
        return 0
    print("counts:", counts)
    for task in tasks:
        print(f"  {task['id']:<20} wave={task['wave']} status={task['status']:<12} "
              f"attempts={task['attempts']} module={task['module']}")
    return 0


def cmd_freeze(args, config) -> int:
    ledger = Ledger(config["paths"]["ledger"])
    project_root = str(runstate.resolve_project(args.project))
    tasks = [task for task in ledger.list_tasks() if task["project"] == project_root]
    ledger.close()
    if not tasks:
        print(f"no tasks registered for {project_root}")
        return 2
    run_state = runstate.load_run(project_root)
    mode = args.mode or run_state.get("mode") or "greenfield"
    owners = {task["id"]: task["owner_files"] for task in tasks}
    info = freeze(project_root, owners, ignores=config["audit"]["ignore_extra"],
                  mode=mode)
    if mode == "brownfield":
        run_state["frozen_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        run_state.setdefault("mode", mode)
        runstate.persist_run(project_root, run_state)
    if args.json:
        print(json.dumps(info))
    else:
        print(f"frozen {info['frozen_files']} baseline entries "
              f"({info['owned']} owned paths) -> {info['baseline']}")
        print("re-balanced every tracked file (carried=0) and kept previously sealed "
              "entries whose files still exist; strays are not absorbed - this clears "
              "any pending modified/deleted_frozen findings for this project")
    return 0


def cmd_audit(args, config) -> int:
    project_root = str(runstate.resolve_project(args.project))
    run_state = runstate.load_run(project_root)
    ignores = list(config["audit"]["ignore_extra"]) \
        + list(run_state.get("audit_ignores") or [])
    result = audit(project_root, ignores=ignores)
    kinds = {violation["kind"] for violation in result["violations"]}
    if args.json:
        print(json.dumps(result))
    elif result["ok"]:
        print("AUDIT OK: no violations")
    elif kinds == {"no_baseline"}:
        print("no frozen baseline; run freeze first")
    else:
        print(f"AUDIT FAIL: {len(result['violations'])} violation(s)")
        for violation in result["violations"]:
            print(f"  {violation['kind']}: {violation['path']}")
    if result["ok"]:
        return 0
    return 2 if kinds == {"no_baseline"} else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swarmflow")
    parser.add_argument("--config", default=None, help="path to swarmflow.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write config/swarmflow.yaml from the example")
    init.set_defaults(func=cmd_init)

    smoke_f = sub.add_parser("smoke-frontier", help="verify frontier model reachability")
    smoke_f.add_argument("--json", action="store_true")
    smoke_f.set_defaults(func=cmd_smoke_frontier)

    smoke_w = sub.add_parser("smoke-worker", help="verify a local Pi worker session")
    smoke_w.add_argument("--json", action="store_true")
    smoke_w.set_defaults(func=cmd_smoke_worker)

    trace = sub.add_parser("trace", help="analyze a session trace file")
    trace.add_argument("file")
    trace.set_defaults(func=cmd_trace)

    recon_p = sub.add_parser("recon", help="survey an existing repository")
    recon_p.add_argument("--project", required=True)
    recon_p.add_argument("--regression-command", default="")
    recon_p.add_argument("--json", action="store_true")
    recon_p.set_defaults(func=cmd_recon)

    plan_load = sub.add_parser("plan-load", help="validate + scaffold + enqueue a plan")
    plan_load.add_argument("--plan", required=True)
    plan_load.add_argument("--project", default=None)
    plan_load.add_argument("--allow-dirty", action="store_true",
                           help="brownfield: allow modified tracked files")
    plan_load.add_argument("--kill-stale", action="store_true",
                           help="brownfield: terminate pre-existing project listeners")
    plan_load.add_argument("--json", action="store_true")
    plan_load.add_argument("--worktree", action="store_true",
                           help="run in an isolated git worktree on the run branch; the "
                                "project checkout keeps its branch and its uncommitted "
                                "work")
    plan_load.add_argument("--worktree-path", default=None,
                           help="where the run worktree goes "
                                "(default: <project>/.swarmflow/worktrees/<name>)")
    plan_load.set_defaults(func=cmd_plan_load)

    wave = sub.add_parser("wave-run", help="run one wave of queued tasks")
    wave.add_argument("--wave", type=int, default=1)
    wave.add_argument("--concurrency", type=int, default=None)
    wave.add_argument("--skip-regression", action="store_true",
                      help="do not run/compare the project regression suite")
    wave.add_argument("--rebaseline", action="store_true",
                      help="re-record the regression baseline before this wave "
                           "(skips its comparison)")
    wave.add_argument("--strict", action="store_true",
                      help="force fail-closed regression comparison for this wave")
    wave.add_argument("--verify", action="store_true",
                      help="frontier-verify this wave's delivered tasks after the gates")
    wave.add_argument("--json", action="store_true")
    wave.set_defaults(func=cmd_wave_run)

    plan_p = sub.add_parser("plan", help="ask the frontier planner for a plan (PRD in)")
    plan_p.add_argument("--prd", required=True, help="path to the PRD/spec text")
    plan_p.add_argument("--project", required=True)
    plan_p.add_argument("--mode", default="greenfield",
                        choices=["greenfield", "brownfield"])
    plan_p.add_argument("--out", default=None,
                        help="default: <project>/.swarmflow/plan.yaml")
    plan_p.add_argument("--load", action="store_true",
                        help="plan-load the resulting file immediately")
    plan_p.add_argument("--worktree", action="store_true",
                        help="with --load: run in an isolated git worktree on the run "
                             "branch, leaving the project checkout untouched")
    plan_p.add_argument("--worktree-path", default=None,
                        help="with --load: where the run worktree goes")
    plan_p.add_argument("--force", action="store_true", help="overwrite an existing --out")
    plan_p.add_argument("--json", action="store_true")
    plan_p.set_defaults(func=cmd_plan)

    verify_p = sub.add_parser("verify", help="frontier verification of one task")
    verify_p.add_argument("--task", required=True)
    verify_p.add_argument("--project", default=None,
                          help="defaults to the task's recorded project")
    verify_p.add_argument("--json", action="store_true")
    verify_p.set_defaults(func=cmd_verify)

    retry_p = sub.add_parser("retry", help="re-queue a needs_fix/failed task (findings "
                                           "are injected on the next dispatch)")
    retry_p.add_argument("--task", required=True)
    retry_p.add_argument("--project", default=None,
                         help="defaults to the task's recorded project")
    retry_p.add_argument("--json", action="store_true")
    retry_p.set_defaults(func=cmd_retry)

    accept_p = sub.add_parser("accept", help="frontier acceptance over the evidence bundle")
    accept_p.add_argument("--project", required=True)
    accept_p.add_argument("--json", action="store_true")
    accept_p.set_defaults(func=cmd_accept)

    status = sub.add_parser("status", help="show ledger state")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    freeze_p = sub.add_parser("freeze", help="snapshot the frozen baseline for a project")
    freeze_p.add_argument("--project", required=True)
    freeze_p.add_argument("--mode", default=None,
                          choices=["greenfield", "brownfield"],
                          help="defaults to the run's recorded mode")
    freeze_p.add_argument("--json", action="store_true")
    freeze_p.set_defaults(func=cmd_freeze)

    audit_p = sub.add_parser("audit", help="check a project against the frozen baseline")
    audit_p.add_argument("--project", required=True)
    audit_p.add_argument("--json", action="store_true")
    audit_p.set_defaults(func=cmd_audit)

    evidence_p = sub.add_parser("evidence", help="assemble the evidence bundle for a project")
    evidence_p.add_argument("--project", required=True)
    evidence_p.add_argument("--json", action="store_true")
    evidence_p.set_defaults(func=cmd_evidence)

    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    config = resolve_paths(load_config(args.config))
    runstate.set_state_root(config["paths"]["state_dir"])
    return args.func(args, config)


if __name__ == "__main__":
    sys.exit(main())

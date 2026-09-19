"""swarmflow command line interface."""

import argparse
import json
import sys
import tempfile
from pathlib import Path

from .audit import audit, freeze
from .config import REPO_ROOT, load_config
from .frontier import FrontierClient, FrontierError
from .ledger import Ledger
from .plan import enqueue_plan, load_plan, scaffold, validate_plan
from .workers import WorkerRunner, scan_trace


def _frontier(config) -> FrontierClient:
    frontier = config["frontier"]
    return FrontierClient(
        cmd_path=frontier["cmd_path"],
        model=frontier["model"],
        effort=frontier.get("effort"),
        timeout_s=float(frontier["timeout_s"]),
    )


def _runner(config, ledger, project_root: str) -> WorkerRunner:
    return WorkerRunner(config, ledger, project_root, REPO_ROOT)


def cmd_smoke_frontier(args, config) -> int:
    client = _frontier(config)
    try:
        result = client.call("Reply with exactly: frontier-ok")
    except FrontierError as exc:
        print(f"FRONTIER FAIL: {exc}")
        return 1
    ok = result["ok"] and "frontier-ok" in result["final_text"]
    print(f"FRONTIER {'OK' if ok else 'FAIL'} subtype={result['subtype']} "
          f"exit={result['exit_code']} usage={result['usage']} "
          f"text={result['final_text']!r}")
    return 0 if ok else 1


def cmd_smoke_worker(args, config) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="swarmflow_worker_smoke_"))
    ledger = Ledger(str(workdir / "state" / "ledger.db"))
    runner = _runner(config, ledger, str(workdir))
    result = runner.run_inline("Reply with exactly: worker-ok")
    ok = "worker-ok" in result["scan"].get("last_text", "")
    print(f"WORKER {'OK' if ok else 'FAIL'} outcome={result['outcome']} "
          f"turns={result['scan']['turns']} trace_dir={runner.logs_dir}")
    ledger.close()
    return 0 if ok else 1


def cmd_trace(args, config) -> int:
    scan = scan_trace(args.file, config["worker"]["max_output_tokens"])
    print(json.dumps(scan, indent=2)[:4000])
    return 0


def cmd_plan_load(args, config) -> int:
    plan = load_plan(args.plan)
    errors = validate_plan(plan)
    if errors:
        print("PLAN INVALID:")
        for error in errors:
            print(f"  - {error}")
        return 2
    raw_root = args.project or plan.get("project")
    if not raw_root:
        print("no project root given (use --project or project: in the plan)")
        return 2
    project_root = Path(raw_root).resolve()
    info = scaffold(plan, project_root, REPO_ROOT)
    ledger = Ledger(str(REPO_ROOT / config["paths"]["ledger"]))
    counts = enqueue_plan(ledger, plan, project_root, info["spec_paths"])
    ledger.close()
    print(f"scaffolded {project_root} (git: {info['git']}); enqueued "
          f"{counts['inserted']}/{counts['total']} tasks")
    return 0


def cmd_wave_run(args, config) -> int:
    ledger = Ledger(str(REPO_ROOT / config["paths"]["ledger"]))
    tasks = ledger.list_tasks(status="queued", wave=args.wave)
    if not tasks:
        print(f"no queued tasks in wave {args.wave}")
        ledger.close()
        return 0
    project_root = tasks[0]["project"]
    runner = _runner(config, ledger, project_root)
    results = runner.run_wave(args.wave, args.concurrency)
    print(f"wave {args.wave}: {len(results)} session(s)")
    failures = 0
    for result in results:
        task = ledger.get(result["task_id"])
        status = task["status"] if task else "?"
        if status != "delivered":
            failures += 1
        print(f"  {result['task_id']:<20} {result['outcome']:<18} status={status} "
              f"turns={result['scan']['turns']} out_tokens={result['scan']['out_tokens']}")
    print("ledger:", ledger.counts())
    audit_state = _run_audit(project_root, ledger, tasks[0]["id"])
    ledger.close()
    return 0 if failures == 0 and audit_state != "fail" else 1


def cmd_status(args, config) -> int:
    ledger = Ledger(str(REPO_ROOT / config["paths"]["ledger"]))
    print("counts:", ledger.counts())
    for task in ledger.list_tasks():
        print(f"  {task['id']:<20} wave={task['wave']} status={task['status']:<12} "
              f"attempts={task['attempts']} module={task['module']}")
    ledger.close()
    return 0


def _run_audit(project_root: str, ledger, task_id: str) -> str:
    """Run the scope audit after a wave. Returns 'ok', 'skipped', or 'fail'."""
    result = audit(project_root)
    kinds = {violation["kind"] for violation in result["violations"]}
    if kinds == {"no_baseline"}:
        print("SCOPE AUDIT skipped (no frozen baseline; run `freeze --project ...` first)")
        return "skipped"
    if result["ok"]:
        print("SCOPE AUDIT OK")
        return "ok"
    print(f"SCOPE AUDIT FAIL: {len(result['violations'])} violation(s)")
    for violation in result["violations"]:
        print(f"  {violation['kind']}: {violation['path']}")
    ledger.record_event(task_id, "scope-violation",
                        json.dumps(result["violations"])[:500])
    return "fail"


def cmd_freeze(args, config) -> int:
    ledger = Ledger(str(REPO_ROOT / config["paths"]["ledger"]))
    project_root = str(Path(args.project).resolve())
    tasks = [task for task in ledger.list_tasks() if task["project"] == project_root]
    ledger.close()
    if not tasks:
        print(f"no tasks registered for {project_root}")
        return 2
    owners = {task["id"]: task["owner_files"] for task in tasks}
    info = freeze(project_root, owners)
    print(f"frozen {info['frozen_files']} files ({info['owned']} owned paths) "
          f"-> {info['baseline']}")
    return 0


def cmd_audit(args, config) -> int:
    project_root = str(Path(args.project).resolve())
    result = audit(project_root)
    kinds = {violation["kind"] for violation in result["violations"]}
    if result["ok"]:
        print("AUDIT OK: no violations")
        return 0
    if kinds == {"no_baseline"}:
        print("no frozen baseline; run freeze first")
        return 2
    print(f"AUDIT FAIL: {len(result['violations'])} violation(s)")
    for violation in result["violations"]:
        print(f"  {violation['kind']}: {violation['path']}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swarmflow")
    parser.add_argument("--config", default=None, help="path to swarmflow.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    smoke_f = sub.add_parser("smoke-frontier", help="verify deepseek-flash reachability")
    smoke_f.set_defaults(func=cmd_smoke_frontier)

    smoke_w = sub.add_parser("smoke-worker", help="verify a local Pi worker session")
    smoke_w.set_defaults(func=cmd_smoke_worker)

    trace = sub.add_parser("trace", help="analyze a session trace file")
    trace.add_argument("file")
    trace.set_defaults(func=cmd_trace)

    plan_load = sub.add_parser("plan-load", help="validate + scaffold + enqueue a plan")
    plan_load.add_argument("--plan", required=True)
    plan_load.add_argument("--project", default=None)
    plan_load.set_defaults(func=cmd_plan_load)

    wave = sub.add_parser("wave-run", help="run one wave of queued tasks")
    wave.add_argument("--wave", type=int, default=1)
    wave.add_argument("--concurrency", type=int, default=None)
    wave.set_defaults(func=cmd_wave_run)

    status = sub.add_parser("status", help="show ledger state")
    status.set_defaults(func=cmd_status)

    freeze_p = sub.add_parser("freeze", help="snapshot the frozen baseline for a project")
    freeze_p.add_argument("--project", required=True)
    freeze_p.set_defaults(func=cmd_freeze)

    audit_p = sub.add_parser("audit", help="check a project against the frozen baseline")
    audit_p.add_argument("--project", required=True)
    audit_p.set_defaults(func=cmd_audit)

    args = parser.parse_args(argv)
    config = load_config(args.config)
    return args.func(args, config)


if __name__ == "__main__":
    sys.exit(main())

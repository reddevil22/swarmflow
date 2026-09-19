"""swarmflow command line interface."""

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from .audit import audit, freeze
from .config import DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG_PATH, REPO_ROOT, load_config
from .frontier import FrontierError, build_backend
from .ledger import Ledger
from .plan import enqueue_plan, load_plan, scaffold, validate_plan
from .regression import compare, run_regression
from .workers import WaveAborted, WorkerRunner, scan_trace


def _frontier(config) -> object:
    return build_backend(config["frontier"])


def _runner(config, ledger, project_root: str) -> WorkerRunner:
    return WorkerRunner(config, ledger, project_root, REPO_ROOT)


def cmd_init(args) -> int:
    if DEFAULT_CONFIG_PATH.exists():
        print(f"config already exists: {DEFAULT_CONFIG_PATH}")
        return 0
    shutil.copyfile(EXAMPLE_CONFIG_PATH, DEFAULT_CONFIG_PATH)
    print(f"wrote {DEFAULT_CONFIG_PATH}")
    print("edit worker.model (your local model id) and any CLI paths, then run:")
    print("  swarmflow smoke-frontier && swarmflow smoke-worker")
    return 0


def cmd_smoke_frontier(args, config) -> int:
    try:
        client = _frontier(config)
    except (FileNotFoundError, FrontierError) as exc:
        print(f"FRONTIER FAIL: {exc}")
        return 1
    try:
        result = client.complete("Reply with exactly: frontier-ok")
    except FrontierError as exc:
        print(f"FRONTIER FAIL: {exc}")
        return 1
    ok = result["ok"] and "frontier-ok" in result["final_text"]
    if args.json:
        print(json.dumps({"ok": ok, "backend": result.get("backend"),
                          "subtype": result["subtype"], "usage": result["usage"],
                          "text": result["final_text"]}))
        return 0 if ok else 1
    print(f"FRONTIER {'OK' if ok else 'FAIL'} backend={result.get('backend')} "
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
        print(f"WORKER FAIL: {exc}")
        ledger.close()
        return 1
    ok = "worker-ok" in result["scan"].get("last_text", "")
    if args.json:
        print(json.dumps({"ok": ok, "outcome": result["outcome"],
                          "turns": result["scan"]["turns"],
                          "trace_dir": str(runner.logs_dir)}))
    else:
        print(f"WORKER {'OK' if ok else 'FAIL'} outcome={result['outcome']} "
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
    ledger = Ledger(str(REPO_ROOT / config["paths"]["ledger"]))
    path = write_bundle(str(Path(args.project).resolve()), ledger)
    ledger.close()
    print(f"evidence bundle written: {path} ({path.stat().st_size} bytes)")
    return 0


def cmd_trace(args, config) -> int:
    scan = scan_trace(args.file, config["worker"]["max_output_tokens"])
    print(json.dumps(scan, indent=2)[:4000])
    return 0


def _brownfield_preflight(plan: dict, project_root: Path, args, config) -> int:
    """Git-required preflight: clean tree, ignore entries, recon, branch, run.json."""
    from .recon import branch_ensure, ensure_gitignore_entries, git_state, recon as run_recon
    state = git_state(project_root)
    if not state.get("is_git"):
        print("brownfield mode requires a git repository; run `git init` first")
        return 2
    if state.get("dirty_tracked") and not args.allow_dirty:
        print("working tree has modified tracked files; commit or stash them first "
              "(or pass --allow-dirty):")
        for line in (state["dirty_tracked"] or [])[:10]:
            print(f"  {line}")
        return 2
    added = ensure_gitignore_entries(project_root, [".swarmflow/", "logs/"])
    if added:
        print(f"added to .gitignore: {', '.join(added)}")
    run_recon(str(project_root),
              regression_command=config["regression"].get("command") or "")
    branch = config["git"]["branch_prefix"] + (plan.get("project_name") or "run")
    action = branch_ensure(str(project_root), branch)
    run_path = project_root / ".swarmflow" / "run.json"
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if run_path.exists():
        try:
            data = json.loads(run_path.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
        data["updated_at"] = now
        run_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    else:
        data = {"plan_path": str(Path(args.plan).resolve()), "mode": "brownfield",
                "branch": branch, "base_sha": state.get("head", ""),
                "created_at": now, "updated_at": now}
        run_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"brownfield preflight ok: branch {branch} ({action}), "
          f"base {str(state.get('head') or '')[:10]}, recon written")
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
    mode = plan.get("mode", "greenfield")
    if mode == "brownfield":
        rc = _brownfield_preflight(plan, project_root, args, config)
        if rc:
            return rc
    info = scaffold(plan, project_root, REPO_ROOT, mode=mode)
    ledger = Ledger(str(REPO_ROOT / config["paths"]["ledger"]))
    counts = enqueue_plan(ledger, plan, project_root, info["spec_paths"])
    ledger.close()
    print(f"[{mode}] scaffolded {project_root} (git: {info['git']}); enqueued "
          f"{counts['inserted']}/{counts['total']} tasks")
    return 0


def _load_run_state(project_root: str) -> dict:
    path = Path(project_root) / ".swarmflow" / "run.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _save_regression_baseline(project_root: str, baseline: dict) -> None:
    path = Path(project_root) / ".swarmflow" / "run.json"
    data = _load_run_state(project_root)
    data.setdefault("regression", {})["baseline"] = baseline
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _recon_regression_command(project_root: str) -> str:
    from .recon import load_recon
    entry = ((load_recon(project_root).get("commands") or {}).get("regression") or {})
    return entry.get("command", "")


def cmd_wave_run(args, config) -> int:
    ledger = Ledger(str(REPO_ROOT / config["paths"]["ledger"]))
    tasks = ledger.list_tasks(status="queued", wave=args.wave)
    if not tasks:
        if args.json:
            print(json.dumps({"wave": args.wave, "results": [], "ledger": ledger.counts()}))
        else:
            print(f"no queued tasks in wave {args.wave}")
        ledger.close()
        return 0
    projects = {task["project"] for task in tasks}
    if len(projects) > 1:
        print(f"refusing to run a wave mixing projects: {sorted(projects)}")
        ledger.close()
        return 2
    project_root = tasks[0]["project"]
    run_state = _load_run_state(project_root)
    brownfield = run_state.get("mode") == "brownfield"
    if brownfield:
        owners = {task["id"]: task["owner_files"] for task in tasks}
        info = freeze(project_root, owners, ignores=config["audit"]["ignore_extra"],
                      mode="brownfield")
        print(f"per-wave freeze: {info['frozen_files']} tracked file(s), "
              f"{info['owned']} owned path(s)")
    regression_enabled = bool(config["regression"].get("enabled", True)) \
        and not args.skip_regression
    regression_command = config["regression"].get("command") \
        or _recon_regression_command(project_root)
    baseline = (run_state.get("regression") or {}).get("baseline")
    if regression_enabled and regression_command and baseline is None:
        baseline = run_regression(
            project_root, regression_command,
            timeout_s=float(config["regression"]["timeout_s"]),
            evidence_path=str(Path(project_root) / ".swarmflow" / "evidence" / "baseline.txt"))
        _save_regression_baseline(project_root, baseline)
        print(f"regression baseline recorded: rc={baseline.get('rc')} "
              f"failures={baseline.get('failures')}")
    elif regression_enabled and not regression_command:
        print("regression gate skipped: no command detected (run `swarmflow recon` "
              "or set regression.command)")
    runner = _runner(config, ledger, project_root)
    try:
        results = runner.run_wave(args.wave, args.concurrency)
    except WaveAborted as exc:
        print(f"wave aborted: {exc}")
        ledger.close()
        return 1
    report = {"wave": args.wave, "results": []}
    failures = 0
    for result in results:
        task = ledger.get(result["task_id"])
        status = task["status"] if task else "?"
        if status != "delivered":
            failures += 1
        entry = {"task_id": result["task_id"], "outcome": result["outcome"],
                 "status": status, "turns": result["scan"]["turns"],
                 "out_tokens": result["scan"]["out_tokens"]}
        report["results"].append(entry)
        if not args.json:
            print(f"  {entry['task_id']:<20} {entry['outcome']:<18} status={status} "
                  f"turns={entry['turns']} out_tokens={entry['out_tokens']}")
    if regression_enabled and regression_command:
        current = run_regression(
            project_root, regression_command,
            timeout_s=float(config["regression"]["timeout_s"]),
            evidence_path=str(Path(project_root) / ".swarmflow" / "evidence"
                              / f"wave{args.wave}.txt"))
        comparison = compare(baseline, current)
        report["regression"] = comparison
        if comparison["regressed"]:
            failures += 1
            reason = "; ".join(comparison["reasons"])
            ledger.record_event(tasks[0]["id"], "regression", reason[:500])
            print(f"REGRESSION DETECTED: {reason}")
            print("  fix the regression before continuing (or --skip-regression at "
                  "your own risk)")
        elif baseline is not None:
            print("regression gate: no new failures")
    report["ledger"] = ledger.counts()
    audit_state, audit_result = _run_audit(project_root, ledger, tasks[0]["id"],
                                           quiet=args.json,
                                           ignores=config["audit"]["ignore_extra"])
    report["audit"] = {"state": audit_state, "result": audit_result}
    ledger.close()
    if args.json:
        print(json.dumps(report))
    else:
        print("ledger:", report["ledger"])
    return 0 if failures == 0 and audit_state != "fail" else 1


def cmd_status(args, config) -> int:
    ledger = Ledger(str(REPO_ROOT / config["paths"]["ledger"]))
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


def _run_audit(project_root: str, ledger, task_id: str,
               quiet: bool = False, ignores: list | None = None) -> tuple:
    """Run the scope audit after a wave. Returns (state, result-or-None)."""
    result = audit(project_root, ignores=ignores)
    kinds = {violation["kind"] for violation in result["violations"]}
    if kinds == {"no_baseline"}:
        if not quiet:
            print("SCOPE AUDIT skipped (no frozen baseline; run `freeze --project ...` first)")
        return "skipped", None
    if result["ok"]:
        if not quiet:
            print("SCOPE AUDIT OK")
        return "ok", result
    if not quiet:
        print(f"SCOPE AUDIT FAIL: {len(result['violations'])} violation(s)")
        for violation in result["violations"]:
            print(f"  {violation['kind']}: {violation['path']}")
    ledger.record_event(task_id, "scope-violation",
                        json.dumps(result["violations"])[:500])
    return "fail", result


def cmd_freeze(args, config) -> int:
    ledger = Ledger(str(REPO_ROOT / config["paths"]["ledger"]))
    project_root = str(Path(args.project).resolve())
    tasks = [task for task in ledger.list_tasks() if task["project"] == project_root]
    ledger.close()
    if not tasks:
        print(f"no tasks registered for {project_root}")
        return 2
    owners = {task["id"]: task["owner_files"] for task in tasks}
    info = freeze(project_root, owners, ignores=config["audit"]["ignore_extra"],
                  mode=args.mode)
    if args.json:
        print(json.dumps(info))
    else:
        print(f"frozen {info['frozen_files']} files ({info['owned']} owned paths) "
              f"-> {info['baseline']}")
    return 0


def cmd_audit(args, config) -> int:
    project_root = str(Path(args.project).resolve())
    result = audit(project_root, ignores=config["audit"]["ignore_extra"])
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
    plan_load.set_defaults(func=cmd_plan_load)

    wave = sub.add_parser("wave-run", help="run one wave of queued tasks")
    wave.add_argument("--wave", type=int, default=1)
    wave.add_argument("--concurrency", type=int, default=None)
    wave.add_argument("--skip-regression", action="store_true",
                      help="do not run/compare the project regression suite")
    wave.add_argument("--json", action="store_true")
    wave.set_defaults(func=cmd_wave_run)

    status = sub.add_parser("status", help="show ledger state")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    freeze_p = sub.add_parser("freeze", help="snapshot the frozen baseline for a project")
    freeze_p.add_argument("--project", required=True)
    freeze_p.add_argument("--mode", default="greenfield",
                          choices=["greenfield", "brownfield"])
    freeze_p.add_argument("--json", action="store_true")
    freeze_p.set_defaults(func=cmd_freeze)

    audit_p = sub.add_parser("audit", help="check a project against the frozen baseline")
    audit_p.add_argument("--project", required=True)
    audit_p.add_argument("--json", action="store_true")
    audit_p.set_defaults(func=cmd_audit)

    evidence_p = sub.add_parser("evidence", help="assemble the evidence bundle for a project")
    evidence_p.add_argument("--project", required=True)
    evidence_p.set_defaults(func=cmd_evidence)

    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args)
    config = load_config(args.config)
    return args.func(args, config)


if __name__ == "__main__":
    sys.exit(main())

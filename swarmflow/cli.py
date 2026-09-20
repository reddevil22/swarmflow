"""swarmflow command line interface."""

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import yaml

from .audit import audit, freeze, seal
from .config import DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG_PATH, REPO_ROOT, load_config
from .frontier import FrontierError, build_backend
from .ledger import Ledger
from .plan import enqueue_plan, load_plan, scaffold, validate_plan
from .procs import kill_tree, snapshot as process_snapshot
from .regression import compare, run_regression
from .sweep import find_stale, run_sweep
from .workers import WaveAborted, WorkerRunner, scan_trace
from . import runstate


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
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
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
    path = write_bundle(str(Path(args.project).resolve()), ledger,
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


def _brownfield_preflight(plan: dict, project_root: Path, plan_path: str,
                          allow_dirty: bool, kill_stale: bool, config) -> int:
    """Git-required preflight: clean tree, ignore entries, recon, branch, control state."""
    from .recon import (branch_ensure, ensure_gitignore_entries, git_state,
                        recon as run_recon)
    state = git_state(project_root)
    if not state.get("is_git"):
        print("brownfield mode requires a git repository; run `git init` first")
        return 2
    if state.get("dirty_tracked") and not allow_dirty:
        print("working tree has modified tracked files; commit or stash them first "
              "(or pass --allow-dirty):")
        for line in (state["dirty_tracked"] or [])[:10]:
            print(f"  {line}")
        return 2
    adopted = runstate.adopt_legacy(str(project_root))
    if adopted:
        print("adopted legacy .swarmflow/run.json into the control-plane state store "
              f"({', '.join(adopted)}); worker-writable regression/audit fields were "
              "not imported")
    gitignore_existed = (project_root / ".gitignore").exists()
    added = ensure_gitignore_entries(project_root, [".swarmflow/", "logs/"])
    audit_ignores = [] if gitignore_existed else [".gitignore"]
    if added:
        print(f"added to .gitignore: {', '.join(added)}")
    override = config["regression"].get("command") or ""
    run_recon(str(project_root), regression_command=override)
    detected = _recon_regression_command(str(project_root))
    branch = config["git"]["branch_prefix"] + (plan.get("project_name") or "run")
    action = branch_ensure(str(project_root), branch)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    data = runstate.load_run(str(project_root))
    data.update({"plan_path": str(Path(plan_path).resolve()), "mode": "brownfield",
                 "project": str(project_root.resolve()), "branch": branch,
                 "base_sha": data.get("base_sha") or state.get("head", ""),
                 "updated_at": now})
    data.setdefault("created_at", now)
    data.setdefault("audit_ignores", audit_ignores)
    command = override or detected or data.get("regression_command", "")
    if command:
        data["regression_command"] = command
        if override:
            data["regression_source"] = "config override"
        elif detected:
            data["regression_source"] = "recon"
        else:
            data.setdefault("regression_source", "previous run")
    runstate.save_run(str(project_root), data)
    runstate.write_mirror(str(project_root), data)
    print(f"brownfield preflight ok: branch {branch} ({action}), "
          f"base {str(state.get('head') or '')[:10]}, recon written, gate command frozen")
    stale = find_stale(str(project_root), process_snapshot(), config.get("sweep") or {})
    if stale:
        for record in stale:
            ports = ",".join(str(port) for port in record["ports"]) or "-"
            print(f"warning: project-attributed listener already running: pid "
                  f"{record['pid']} {record['name']} (ports: {ports})")
        if kill_stale:
            killed = [record["pid"] for record in stale if kill_tree(record["pid"])]
            print(f"  --kill-stale terminated {len(killed)} process(es)")
        else:
            print("  stale listeners can serve stale code to probes; use --kill-stale "
                  "or stop them manually")
    return 0


def cmd_plan_load(args, config) -> int:
    return _plan_load(args.plan, args.project, config, allow_dirty=args.allow_dirty,
                      kill_stale=args.kill_stale, json_output=args.json)


def _plan_load(plan_path: str, project: str | None, config, allow_dirty: bool = False,
               kill_stale: bool = False, json_output: bool = False) -> int:
    plan = load_plan(plan_path)
    errors = validate_plan(plan)
    if errors:
        print("PLAN INVALID:")
        for error in errors:
            print(f"  - {error}")
        return 2
    raw_root = project or plan.get("project")
    if not raw_root:
        print("no project root given (use --project or project: in the plan)")
        return 2
    project_root = Path(raw_root).resolve()
    mode = plan.get("mode", "greenfield")
    if mode == "brownfield":
        rc = _brownfield_preflight(plan, project_root, plan_path, allow_dirty,
                                   kill_stale, config)
        if rc:
            return rc
    info = scaffold(plan, project_root, REPO_ROOT, mode=mode)
    if mode != "brownfield":
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        data = runstate.load_run(str(project_root))
        data.update({"plan_path": str(Path(plan_path).resolve()), "mode": mode,
                     "project": str(project_root.resolve()), "updated_at": now})
        data.setdefault("created_at", now)
        override = config["regression"].get("command") or ""
        command = override or _recon_regression_command(str(project_root))
        if command:
            data["regression_command"] = command
            data.setdefault("regression_source",
                            "config override" if override else "recon")
        runstate.save_run(str(project_root), data)
        runstate.write_mirror(str(project_root), data)
    ledger = Ledger(str(REPO_ROOT / config["paths"]["ledger"]))
    counts = enqueue_plan(ledger, plan, project_root, info["spec_paths"])
    ledger.close()
    if json_output:
        print(json.dumps({"mode": mode, "project": str(project_root),
                          "tasks": counts, "git": info["git"],
                          "spec_paths": len(info["spec_paths"])}))
    else:
        print(f"[{mode}] scaffolded {project_root} (git: {info['git']}); enqueued "
              f"{counts['inserted']}/{counts['total']} tasks")
    return 0


def cmd_plan(args, config) -> int:
    """Frontier planning: PRD (+ recon digest) -> validated plan.yaml."""
    from .roles import RoleError, plan_from_prd
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
    runstate.save_run(str(project_root), data)
    if args.json:
        print(json.dumps({"plan": str(out), "tasks": result["tasks"],
                          "waves": result["waves"], "retried": result["retried"],
                          "artifact": result["artifact"]}))
    else:
        print(f"plan written: {out} ({result['tasks']} tasks, "
              f"waves {result['waves']}){' [retried once]' if result['retried'] else ''}")
        print(f"raw model output: {result['artifact']}")
    if args.load:
        return _plan_load(str(out), str(project_root), config)
    return 0


def cmd_verify(args, config) -> int:
    """Frontier verification of one task against its spec."""
    from .roles import RoleError, verify_task
    with Ledger(str(REPO_ROOT / config["paths"]["ledger"])) as ledger:
        task = ledger.get(args.task)
        if not task:
            print(f"unknown task: {args.task}")
            return 2
        project_root = args.project or task["project"]
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


def cmd_accept(args, config) -> int:
    """Frontier acceptance over the frozen criteria and the evidence bundle."""
    from .roles import RoleError, accept_run
    with Ledger(str(REPO_ROOT / config["paths"]["ledger"])) as ledger:
        try:
            result = accept_run(config, str(Path(args.project).resolve()), ledger)
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


def _save_regression_baseline(project_root: str, baseline: dict) -> None:
    data = runstate.load_run(project_root)
    stored = {key: value for key, value in baseline.items() if key != "output"}
    data.setdefault("regression", {})["baseline"] = stored
    runstate.save_run(project_root, data)
    runstate.write_mirror(project_root, data)


def _slim_comparison(comparison: dict) -> dict:
    """Comparison without the raw captured outputs (used for reports and compare files)."""
    slim = {key: value for key, value in comparison.items()
            if key not in ("baseline", "current")}
    slim["version"] = 1
    for side in ("baseline", "current"):
        value = comparison.get(side)
        slim[side] = ({key: item for key, item in value.items() if key != "output"}
                      if isinstance(value, dict) else value)
    return slim


def _write_comparison(project_root: str, wave: int, comparison: dict) -> Path:
    path = Path(project_root) / ".swarmflow" / "evidence" / f"wave{wave}.compare.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_slim_comparison(comparison), indent=2), encoding="utf-8")
    return path


def _recon_regression_command(project_root: str) -> str:
    from .recon import load_recon
    entry = ((load_recon(project_root).get("commands") or {}).get("regression") or {})
    return entry.get("command", "")


def cmd_wave_run(args, config) -> int:
    with Ledger(str(REPO_ROOT / config["paths"]["ledger"])) as ledger:
        tasks = ledger.list_tasks(status="queued", wave=args.wave)
        if not tasks:
            if getattr(args, "verify", False):
                delivered = [task for task in ledger.list_tasks(wave=args.wave)
                             if task["status"] == "delivered"]
                if delivered:
                    results = _run_verify_stage(delivered[0]["project"], delivered,
                                                config, ledger, quiet=args.json)
                    return 0 if all(item["verdict"] == "pass" for item in results) else 1
            if args.json:
                print(json.dumps({"wave": args.wave, "results": [],
                                  "ledger": ledger.counts()}))
            else:
                print(f"no queued tasks in wave {args.wave}")
            return 0
        projects = {task["project"] for task in tasks}
        if len(projects) > 1:
            print(f"refusing to run a wave mixing projects: {sorted(projects)}")
            return 2
        project_root = tasks[0]["project"]
        run_state = runstate.load_run(project_root)
        brownfield = run_state.get("mode") == "brownfield"
        freeze_info = None
        if brownfield:
            if run_state.get("frozen_at") and runstate.load_frozen(project_root) is None:
                print("REFUSING: this run froze a baseline earlier but the control-plane "
                      "store no longer has it; re-baseline explicitly with "
                      "`swarmflow freeze --project <path> --mode brownfield`")
                return 2
            owners = {task["id"]: task["owner_files"] for task in tasks}
            freeze_info = freeze(project_root, owners,
                                 ignores=config["audit"]["ignore_extra"],
                                 mode="brownfield", carry_over=True)
            run_state["frozen_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            runstate.save_run(project_root, run_state)
            ledger.record_event(tasks[0]["id"], "freeze", json.dumps({
                "entries": freeze_info["entries"], "carried": freeze_info["carried"],
                "refreshed": freeze_info["refreshed"]}))
            print(f"per-wave freeze: {freeze_info['entries']} baseline entries "
                  f"({freeze_info['carried']} carried, {freeze_info['refreshed']} "
                  f"refreshed), {freeze_info['owned']} owned path(s)")
        regression_enabled = bool(config["regression"].get("enabled", True)) \
            and not args.skip_regression
        regression_timeout = float(config["regression"]["timeout_s"])
        strict = bool(config["regression"].get("strict", True)) or args.strict
        tolerance = int(config["regression"].get("shrink_tolerance", 0))
        regression_command = _gate_command(project_root, run_state, config)
        baseline = (run_state.get("regression") or {}).get("baseline")
        if regression_enabled and regression_command and args.rebaseline:
            baseline = run_regression(
                project_root, regression_command, timeout_s=regression_timeout,
                evidence_path=str(Path(project_root) / ".swarmflow" / "evidence" / "baseline.txt"))
            _save_regression_baseline(project_root, baseline)
            ledger.record_event(tasks[0]["id"], "rebaseline",
                                "operator re-recorded the regression baseline")
            print(f"regression baseline re-recorded: rc={baseline.get('rc')} "
                  f"failures={baseline.get('failures')} tests_ran={baseline.get('tests_ran')}")
        elif regression_enabled and regression_command and baseline is None:
            baseline = run_regression(
                project_root, regression_command, timeout_s=regression_timeout,
                evidence_path=str(Path(project_root) / ".swarmflow" / "evidence" / "baseline.txt"))
            _save_regression_baseline(project_root, baseline)
            print(f"regression baseline recorded: rc={baseline.get('rc')} "
                  f"failures={baseline.get('failures')} tests_ran={baseline.get('tests_ran')}")
        elif regression_enabled and not regression_command:
            print("regression gate skipped: no command frozen at plan-load (set "
                  "regression.command and re-run plan-load)")
        sweep_before = process_snapshot() if config["sweep"].get("enabled", True) else None
        wave_start = time.time()
        runner = _runner(config, ledger, project_root)
        try:
            results = runner.run_wave(args.wave, args.concurrency)
        except WaveAborted as exc:
            print(f"wave aborted: {exc}")
            return 1
        sweep_after = process_snapshot() if sweep_before is not None else None
        report = {"wave": args.wave, "results": []}
        failures = 0
        for result in results:
            task = ledger.get(result["task_id"])
            status = task["status"] if task else "?"
            if status != "delivered":
                failures += 1
            entry = {"task_id": result["task_id"], "outcome": result["outcome"],
                     "status": status, "turns": result["scan"]["turns"],
                     "out_tokens": result["scan"]["out_tokens"],
                     "server_launches": len(result.get("server_launches") or [])}
            report["results"].append(entry)
            if not args.json:
                print(f"  {entry['task_id']:<20} {entry['outcome']:<18} status={status} "
                      f"turns={entry['turns']} out_tokens={entry['out_tokens']}")
                if entry["server_launches"]:
                    print(f"    note: {entry['server_launches']} server/watcher "
                          f"command(s) observed in the trace")
        report["sweep"] = _run_sweep(project_root, args.wave, sweep_before, sweep_after,
                                     wave_start, config, ledger, tasks[0]["id"],
                                     quiet=args.json)
        if regression_enabled and regression_command and not args.rebaseline:
            current = run_regression(
                project_root, regression_command, timeout_s=regression_timeout,
                evidence_path=str(Path(project_root) / ".swarmflow" / "evidence"
                                  / f"wave{args.wave}.txt"))
            comparison = compare(baseline, current, strict=strict,
                                 shrink_tolerance=tolerance)
            report["regression"] = _slim_comparison(comparison)
            compare_path = _write_comparison(project_root, args.wave, comparison)
            if comparison["regressed"]:
                failures += 1
                reason = "; ".join(comparison["reasons"])
                ledger.record_event(tasks[0]["id"], "regression", reason[:500])
                print(f"REGRESSION DETECTED: {reason}")
                for name in comparison["new_failures"][:10]:
                    print(f"  new failing test: {name}")
                if comparison["fixed_failures"]:
                    print(f"  fixed: {len(comparison['fixed_failures'])} known-failing "
                          f"test(s)")
                print(f"  comparison: {compare_path}")
                print("  fix the regression before continuing (or --skip-regression at "
                      "your own risk)")
            elif comparison["indeterminate"]:
                print(f"REGRESSION GATE WARNING: {'; '.join(comparison['reasons'])}")
                print("  the results could not be compared; re-record the baseline with "
                      "--rebaseline once the current state is known-good")
            elif baseline is not None:
                detail = "; ".join(comparison["reasons"]) or "green -> green"
                print(f"regression gate: no new failures ({detail})")
        disc_state, disc_result = _run_discrimination(
            project_root, args.wave, tasks, config, ledger,
            dict(run_state, regression={"baseline": baseline}), quiet=args.json)
        report["discrimination"] = disc_result
        if disc_state == "fail":
            failures += 1
        audit_ignores = list(config["audit"]["ignore_extra"]) \
            + list(run_state.get("audit_ignores") or [])
        audit_state, audit_result = _run_audit(project_root, ledger, tasks[0]["id"],
                                               quiet=args.json, ignores=audit_ignores,
                                               brownfield=brownfield)
        report["audit"] = {"state": audit_state, "result": audit_result}
        if freeze_info is not None:
            report["freeze"] = {"entries": freeze_info["entries"],
                                "carried": freeze_info["carried"],
                                "refreshed": freeze_info["refreshed"]}
        if brownfield:
            try:
                report["seal"] = seal(project_root,
                                      {task["id"]: task["owner_files"] for task in tasks})
            except Exception as exc:              # never break a wave on sealing
                print(f"seal failed, baseline left unsealed: {exc}")
        verify_results = []
        verify_wanted = bool(getattr(args, "verify", False)) \
            or bool((config.get("verify") or {}).get("enabled", False))
        if verify_wanted:
            explicit = bool(getattr(args, "verify", False))
            if failures > 0 or audit_state == "fail":
                if explicit:
                    print("verify skipped: the wave's gates failed")
            else:
                verify_results = _run_verify_stage(project_root, tasks, config, ledger,
                                                   quiet=args.json)
                errors = [item for item in verify_results if item["verdict"] == "error"]
                if errors and len(errors) == len(verify_results) \
                        and all(item.get("code") == 2 for item in errors):
                    if explicit:
                        print("verify failed: frontier unavailable")
                        return 2
                    print("verify skipped: frontier not configured")
                elif any(item["verdict"] != "pass" for item in verify_results):
                    failures += 1
        report["verify"] = verify_results
        report["ledger"] = ledger.counts()
        if args.json:
            print(json.dumps(report))
        else:
            print("ledger:", report["ledger"])
        return 0 if failures == 0 and audit_state != "fail" else 1


def _gate_command(project_root: str, run_state: dict, config: dict) -> str:
    """The regression command for this wave: config override > frozen store value.

    Never re-derived from the worker-writable project after dispatch; the one exception
    is an adopted pre-upgrade run, which resolves once and persists.
    """
    override = config["regression"].get("command") or ""
    stored = run_state.get("regression_command") or ""
    if override:
        if stored and stored != override:
            print(f"regression command overridden by config: {override!r} "
                  f"(frozen: {stored!r})")
        return override
    if stored:
        return stored
    if run_state.get("adopted"):
        adopted_command = _recon_regression_command(project_root)
        if adopted_command:
            run_state["regression_command"] = adopted_command
            run_state["regression_source"] = "recon (adopted run)"
            runstate.save_run(project_root, run_state)
            print("adopted run: persisted the regression command from recon.json "
                  "(one-time; not re-read afterwards)")
        return adopted_command
    return ""


def _run_discrimination(project_root: str, wave: int, tasks: list, config: dict,
                        ledger, run_state: dict, quiet: bool = False) -> tuple:
    """Run this wave's owned test files against the parent state. Returns (state, result)."""
    from .discrimination import run_check
    path = Path(project_root) / ".swarmflow" / "evidence" / f"wave{wave}.discrimination.json"
    try:
        result = run_check(project_root, wave, tasks, config, run_state)
    except Exception as exc:                      # a check bug must never break a wave
        result = {"version": 1, "wave": wave, "indeterminate": True,
                  "reason": f"check crashed: {exc}", "counts": {}, "files": []}
    slim = {key: value for key, value in result.items() if key != "output"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(slim, indent=2), encoding="utf-8")
    if result.get("skipped"):
        if not quiet:
            print(f"discrimination check skipped: {result['skipped']}")
        return "skipped", slim
    counts = result.get("counts") or {}
    ledger.record_event(tasks[0]["id"], "discrimination", json.dumps(counts)[:500])
    enforce = str(config["discrimination"].get("mode", "warn")).lower() == "enforce"
    non_discriminating = counts.get("passes_at_parent", 0) + counts.get("deleted_in_wave", 0)
    unattributable = result.get("unattributable_files") or []
    state = "ok"
    if result.get("indeterminate"):
        state = "fail" if enforce else "warn"
        if not quiet:
            print(f"discrimination INDETERMINATE: {result.get('reason') or 'run failed'}")
    elif unattributable:
        state = "fail" if enforce else "warn"
        if not quiet:
            print(f"discrimination: {len(unattributable)} file(s) could not be attributed "
                  f"at the parent state (path-less runner output) - discrimination not "
                  f"demonstrated"
                  f"{' (enforce -> failing the wave)' if enforce else ' (inconclusive)'}")
            for path in unattributable[:10]:
                print(f"  not_observed: {path}")
    elif non_discriminating:
        state = "fail" if enforce else "warn"
        if not quiet:
            print(f"discrimination: {counts.get('fails_at_parent', 0)}/"
                  f"{result.get('tested', 0)} changed test file(s) fail at base; "
                  f"{non_discriminating} non-discriminating"
                  f"{' (enforce)' if enforce else ' (evidence only)'}")
    elif not quiet:
        print(f"discrimination: {counts.get('fails_at_parent', 0)}/"
              f"{result.get('tested', 0)} changed test file(s) fail at base")
    if not quiet and not unattributable:
        for entry in result.get("files") or []:
            if entry["verdict"] in ("passes_at_parent", "deleted_in_wave", "error_at_parent"):
                print(f"  {entry['verdict']}: {entry['path']}")
    return state, slim


def _run_verify_stage(project_root: str, tasks: list, config: dict, ledger,
                      quiet: bool = False) -> list:
    """Frontier-verify this wave's delivered tasks (unique ids, capped)."""
    from .roles import RoleError, verify_task
    limit = int((config.get("verify") or {}).get("max_tasks", 5))
    seen = []
    for task in tasks:
        if task["id"] not in seen:
            seen.append(task["id"])
    results = []
    skipped = 0
    for task_id in seen:
        task = ledger.get(task_id)
        if not task or task["status"] != "delivered":
            continue
        if len(results) >= limit:
            skipped += 1
            continue
        try:
            outcome = verify_task(config, project_root, ledger, task_id)
        except RoleError as exc:
            outcome = {"task_id": task_id, "verdict": "error", "error": str(exc),
                       "code": exc.code, "findings": []}
        except FrontierError as exc:
            outcome = {"task_id": task_id, "verdict": "error", "error": str(exc),
                       "code": 2, "findings": []}
        results.append(outcome)
        if not quiet:
            if outcome["verdict"] == "pass":
                print(f"  verify {task_id}: pass")
            elif outcome["verdict"] == "error":
                print(f"  verify {task_id}: ERROR {outcome['error']}")
            else:
                print(f"  verify {task_id}: {outcome['verdict']}")
                for finding in (outcome.get("findings") or [])[:5]:
                    print(f"    - [{finding.get('severity')}] {finding.get('summary')}")
    if not quiet and results:
        passed = sum(1 for item in results if item["verdict"] == "pass")
        print(f"verified {passed} of {len(results)} ({skipped} skipped over max_tasks)")
    return results


def _run_sweep(project_root: str, wave: int, before: dict, after: dict, wave_start: float,
               config: dict, ledger, task_id: str, quiet: bool = False) -> dict:
    """Post-wave process sweep. Returns the result (also written to disk)."""
    path = Path(project_root) / ".swarmflow" / "evidence" / f"wave{wave}.sweep.json"
    try:
        result = run_sweep(project_root, wave, before, after, wave_start, config)
    except Exception as exc:                      # a sweep bug must never break a wave
        result = {"version": 1, "wave": wave, "indeterminate": True,
                  "reason": f"sweep crashed: {exc}", "counts": {}, "orphans": [],
                  "pre_existing": [], "killed": []}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if result.get("skipped"):
        if not quiet:
            print(f"process sweep skipped: {result['skipped']}")
        return result
    counts = result.get("counts") or {}
    if result.get("indeterminate"):
        if not quiet:
            print(f"process sweep INDETERMINATE: {result.get('reason')}")
        return result
    ledger.record_event(task_id, "sweep", json.dumps(counts)[:500])
    if quiet:
        return result
    mode = result.get("mode", "warn")
    killed = set(result.get("killed") or [])
    if counts.get("orphans"):
        print(f"process sweep: {counts['orphans']} new project process(es) from this wave "
              f"(mode={mode})")
        for record in result.get("orphans") or []:
            ports = ",".join(str(port) for port in record["ports"]) or "-"
            suffix = " [killed]" if record["pid"] in killed else ""
            print(f"  pid {record['pid']} {record['name']}: {record['cmd'][:80]} "
                  f"(ports: {ports}){suffix}")
        if mode != "kill":
            print("  set sweep.mode: kill to terminate them automatically")
    elif counts.get("pre_existing"):
        print(f"process sweep: no new processes; {counts['pre_existing']} pre-existing "
              f"project listener(s)")
    else:
        print("process sweep: clean")
    for record in result.get("pre_existing") or []:
        ports = ",".join(str(port) for port in record["ports"]) or "-"
        print(f"  pre-existing listener: pid {record['pid']} {record['name']} "
              f"(ports: {ports}) - left untouched")
    return result


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


def _run_audit(project_root: str, ledger, task_id: str, quiet: bool = False,
               ignores: list | None = None, brownfield: bool = False) -> tuple:
    """Run the scope audit after a wave. Returns (state, result-or-None)."""
    try:
        result = audit(project_root, ignores=ignores)
    except Exception as exc:                      # a gate bug must never break a wave
        if not quiet:
            print(f"SCOPE AUDIT ERROR: {exc}")
        ledger.record_event(task_id, "audit-error", str(exc)[:500])
        return "fail", None
    kinds = {violation["kind"] for violation in result["violations"]}
    if kinds == {"no_baseline"}:
        if brownfield:
            if not quiet:
                print("SCOPE AUDIT FAIL: no frozen baseline for a brownfield run "
                      "(control state missing?)")
            ledger.record_event(task_id, "scope-violation",
                                json.dumps(result["violations"])[:500])
            return "fail", result
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
    run_state = runstate.load_run(project_root)
    mode = args.mode or run_state.get("mode") or "greenfield"
    owners = {task["id"]: task["owner_files"] for task in tasks}
    info = freeze(project_root, owners, ignores=config["audit"]["ignore_extra"],
                  mode=mode)
    if mode == "brownfield":
        run_state["frozen_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        run_state.setdefault("mode", mode)
        runstate.save_run(project_root, run_state)
    if args.json:
        print(json.dumps(info))
    else:
        print(f"frozen {info['frozen_files']} baseline entries "
              f"({info['owned']} owned paths) -> {info['baseline']}")
        print("re-balanced every tracked file (carried=0): this clears any pending "
              "modified/deleted_frozen findings for this project")
    return 0


def cmd_audit(args, config) -> int:
    project_root = str(Path(args.project).resolve())
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
    plan_p.add_argument("--force", action="store_true", help="overwrite an existing --out")
    plan_p.add_argument("--json", action="store_true")
    plan_p.set_defaults(func=cmd_plan)

    verify_p = sub.add_parser("verify", help="frontier verification of one task")
    verify_p.add_argument("--task", required=True)
    verify_p.add_argument("--project", default=None,
                          help="defaults to the task's recorded project")
    verify_p.add_argument("--json", action="store_true")
    verify_p.set_defaults(func=cmd_verify)

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
    config = load_config(args.config)
    state_dir = Path(str(config["paths"].get("state_dir") or "state"))
    if not state_dir.is_absolute():
        state_dir = REPO_ROOT / state_dir
    runstate.set_state_root(state_dir)
    return args.func(args, config)


if __name__ == "__main__":
    sys.exit(main())

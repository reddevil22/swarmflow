"""Wave pipeline policy: everything between argument parsing and the gate modules.

``cli.py`` owns argparse and exit codes; this module owns preflight, freeze, dispatch,
gates, verification and the wave report, so an alternate front-end can reuse the pipeline
without re-implementing it. Functions print operator progress (or honour ``quiet``), and
none of them read argparse namespaces.
"""

import json
import time
from pathlib import Path

from . import runstate, sweep as sweep_mod
from .audit import audit, freeze, seal
from .config import REPO_ROOT
from .frontier import FrontierError
from .ledger import Ledger
from .plan import enqueue_plan, load_plan, scaffold, validate_plan
from .procs import format_ports, kill_tree, snapshot as process_snapshot
from .regression import compare, run_regression
from .sweep import find_stale
from .workers import WaveAborted, WorkerRunner


def _runner(config, ledger, project_root: str) -> WorkerRunner:
    """Worker-runner factory: the seam tests and alternate front-ends replace."""
    return WorkerRunner(config, ledger, project_root, REPO_ROOT)


def recon_regression_command(project_root: str) -> str:
    """The regression command recorded by the last recon, if any."""
    from .recon import load_recon
    entry = ((load_recon(project_root).get("commands") or {}).get("regression") or {})
    return entry.get("command", "")


def brownfield_preflight(plan: dict, project_root: Path, plan_path: str,
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
    detected = recon_regression_command(str(project_root))
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
    data.setdefault("untracked_baseline", state.get("untracked_files") or [])
    command = override or detected or data.get("regression_command", "")
    if command:
        data["regression_command"] = command
        if override:
            data["regression_source"] = "config override"
        elif detected:
            data["regression_source"] = "recon"
        else:
            data.setdefault("regression_source", "previous run")
    runstate.persist_run(str(project_root), data, mirror=True)
    print(f"brownfield preflight ok: branch {branch} ({action}), "
          f"base {str(state.get('head') or '')[:10]}, recon written, gate command frozen")
    exempted = data.get("untracked_baseline") or []
    if exempted:
        preview = ", ".join(exempted[:3]) + (" ..." if len(exempted) > 3 else "")
        print(f"pre-existing untracked file(s) exempt from the scope audit: "
              f"{len(exempted)} ({preview})")
    stale = find_stale(str(project_root), process_snapshot(), config.get("sweep") or {})
    if stale:
        for record in stale:
            ports = format_ports(record["ports"])
            print(f"warning: project-attributed listener already running: pid "
                  f"{record['pid']} {record['name']} (ports: {ports})")
        if kill_stale:
            killed = [record["pid"] for record in stale if kill_tree(record["pid"])]
            print(f"  --kill-stale terminated {len(killed)} process(es)")
        else:
            print("  stale listeners can serve stale code to probes; use --kill-stale "
                  "or stop them manually")
    return 0


def plan_load(plan_path: str, project: str | None, config, allow_dirty: bool = False,
              kill_stale: bool = False, json_output: bool = False) -> int:
    """Validate, scaffold, freeze gate inputs and enqueue a plan."""
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
        rc = brownfield_preflight(plan, project_root, plan_path, allow_dirty,
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
        command = override or recon_regression_command(str(project_root))
        if command:
            data["regression_command"] = command
            data.setdefault("regression_source",
                            "config override" if override else "recon")
        runstate.persist_run(str(project_root), data, mirror=True)
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


def save_regression_baseline(project_root: str, baseline: dict) -> None:
    data = runstate.load_run(project_root)
    stored = {key: value for key, value in baseline.items() if key != "output"}
    data.setdefault("regression", {})["baseline"] = stored
    runstate.persist_run(project_root, data, mirror=True)


def slim_comparison(comparison: dict) -> dict:
    """Comparison without the raw captured outputs (used for reports and compare files)."""
    slim = {key: value for key, value in comparison.items()
            if key not in ("baseline", "current")}
    slim["version"] = 1
    for side in ("baseline", "current"):
        value = comparison.get(side)
        slim[side] = ({key: item for key, item in value.items() if key != "output"}
                      if isinstance(value, dict) else value)
    return slim


def write_comparison(project_root: str, wave: int, comparison: dict) -> Path:
    path = Path(project_root) / ".swarmflow" / "evidence" / f"wave{wave}.compare.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(slim_comparison(comparison), indent=2), encoding="utf-8")
    return path


def gate_command(project_root: str, run_state: dict, config: dict) -> str:
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
        adopted_command = recon_regression_command(project_root)
        if adopted_command:
            run_state["regression_command"] = adopted_command
            run_state["regression_source"] = "recon (adopted run)"
            runstate.save_run(project_root, run_state)
            print("adopted run: persisted the regression command from recon.json "
                  "(one-time; not re-read afterwards)")
        return adopted_command
    return ""


def run_discrimination(project_root: str, wave: int, tasks: list, config: dict,
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


def run_verify_stage(project_root: str, tasks: list, config: dict, ledger,
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


def run_sweep(project_root: str, wave: int, before: dict, after: dict, wave_start: float,
              config: dict, ledger, task_id: str, quiet: bool = False) -> dict:
    """Post-wave process sweep. Returns the result (also written to disk)."""
    path = Path(project_root) / ".swarmflow" / "evidence" / f"wave{wave}.sweep.json"
    try:
        result = sweep_mod.run_sweep(project_root, wave, before, after, wave_start, config)
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
            ports = format_ports(record["ports"])
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
        ports = format_ports(record["ports"])
        print(f"  pre-existing listener: pid {record['pid']} {record['name']} "
              f"(ports: {ports}) - left untouched")
    return result


def run_audit(project_root: str, ledger, task_id: str, quiet: bool = False,
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


def run_wave(ledger, config, wave: int, concurrency: int | None = None,
             verify: bool = False, skip_regression: bool = False, strict: bool = False,
             rebaseline: bool = False, quiet: bool = False) -> tuple:
    """Freeze, dispatch one wave, run every gate, optionally verify. (rc, report)."""
    tasks = ledger.list_tasks(status="queued", wave=wave)
    if not tasks:
        if verify:
            delivered = [task for task in ledger.list_tasks(wave=wave)
                         if task["status"] == "delivered"]
            if delivered:
                results = run_verify_stage(delivered[0]["project"], delivered,
                                           config, ledger, quiet=quiet)
                return (0 if all(item["verdict"] == "pass" for item in results) else 1), None
        if quiet:
            print(json.dumps({"wave": wave, "results": [],
                              "ledger": ledger.counts()}))
        else:
            print(f"no queued tasks in wave {wave}")
        return 0, None
    projects = {task["project"] for task in tasks}
    if len(projects) > 1:
        print(f"refusing to run a wave mixing projects: {sorted(projects)}")
        return 2, None
    project_root = tasks[0]["project"]
    run_state = runstate.load_run(project_root)
    brownfield = run_state.get("mode") == "brownfield"
    freeze_info = None
    if brownfield:
        if run_state.get("frozen_at") and runstate.load_frozen(project_root) is None:
            print("REFUSING: this run froze a baseline earlier but the control-plane "
                  "store no longer has it; re-baseline explicitly with "
                  "`swarmflow freeze --project <path> --mode brownfield`")
            return 2, None
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
        and not skip_regression
    regression_timeout = float(config["regression"]["timeout_s"])
    strict = bool(config["regression"].get("strict", True)) or strict
    tolerance = int(config["regression"].get("shrink_tolerance", 0))
    regression_command = gate_command(project_root, run_state, config)
    baseline = (run_state.get("regression") or {}).get("baseline")
    if regression_enabled and regression_command and rebaseline:
        baseline = run_regression(
            project_root, regression_command, timeout_s=regression_timeout,
            evidence_path=str(Path(project_root) / ".swarmflow" / "evidence" / "baseline.txt"))
        save_regression_baseline(project_root, baseline)
        ledger.record_event(tasks[0]["id"], "rebaseline",
                            "operator re-recorded the regression baseline")
        print(f"regression baseline re-recorded: rc={baseline.get('rc')} "
              f"failures={baseline.get('failures')} tests_ran={baseline.get('tests_ran')}")
    elif regression_enabled and regression_command and baseline is None:
        baseline = run_regression(
            project_root, regression_command, timeout_s=regression_timeout,
            evidence_path=str(Path(project_root) / ".swarmflow" / "evidence" / "baseline.txt"))
        save_regression_baseline(project_root, baseline)
        print(f"regression baseline recorded: rc={baseline.get('rc')} "
              f"failures={baseline.get('failures')} tests_ran={baseline.get('tests_ran')}")
    elif regression_enabled and not regression_command:
        print("regression gate skipped: no command frozen at plan-load (set "
              "regression.command and re-run plan-load)")
    sweep_before = process_snapshot() if config["sweep"].get("enabled", True) else None
    wave_start = time.time()
    runner = _runner(config, ledger, project_root)
    try:
        results = runner.run_wave(wave, concurrency)
    except WaveAborted as exc:
        print(f"wave aborted: {exc}")
        return 1, None
    sweep_after = process_snapshot() if sweep_before is not None else None
    report = {"wave": wave, "results": []}
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
        if not quiet:
            print(f"  {entry['task_id']:<20} {entry['outcome']:<18} status={status} "
                  f"turns={entry['turns']} out_tokens={entry['out_tokens']}")
            if entry["server_launches"]:
                print(f"    note: {entry['server_launches']} server/watcher "
                      f"command(s) observed in the trace")
    report["sweep"] = run_sweep(project_root, wave, sweep_before, sweep_after,
                                wave_start, config, ledger, tasks[0]["id"], quiet=quiet)
    if regression_enabled and regression_command and not rebaseline:
        current = run_regression(
            project_root, regression_command, timeout_s=regression_timeout,
            evidence_path=str(Path(project_root) / ".swarmflow" / "evidence"
                              / f"wave{wave}.txt"))
        comparison = compare(baseline, current, strict=strict,
                             shrink_tolerance=tolerance)
        report["regression"] = slim_comparison(comparison)
        compare_path = write_comparison(project_root, wave, comparison)
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
    disc_state, disc_result = run_discrimination(
        project_root, wave, tasks, config, ledger,
        dict(run_state, regression={"baseline": baseline}), quiet=quiet)
    report["discrimination"] = disc_result
    if disc_state == "fail":
        failures += 1
    audit_ignores = list(config["audit"]["ignore_extra"]) \
        + list(run_state.get("audit_ignores") or [])
    audit_state, audit_result = run_audit(project_root, ledger, tasks[0]["id"],
                                          quiet=quiet, ignores=audit_ignores,
                                          brownfield=brownfield)
    report["audit"] = {"state": audit_state, "result": audit_result}
    if freeze_info is not None:
        report["freeze"] = {"entries": freeze_info["entries"],
                            "carried": freeze_info["carried"],
                            "refreshed": freeze_info["refreshed"]}
    if brownfield:
        try:
            seal_result = seal(project_root,
                               {task["id"]: task["owner_files"] for task in tasks})
            report["seal"] = seal_result
            ledger.record_event(tasks[0]["id"], "seal",
                                json.dumps(seal_result)[:500])
            if not quiet and any(seal_result.get(key)
                                 for key in ("added", "sealed", "dropped", "capped")):
                detail = (f"seal: {seal_result['added']} new file(s) baselined, "
                          f"{seal_result['sealed']} refreshed, "
                          f"{seal_result['dropped']} dropped")
                if seal_result.get("capped"):
                    detail += (f", {seal_result['capped']} over the cap "
                                  "(left unprotected)")
                print(detail)
        except Exception as exc:                  # never break a wave on sealing
            print(f"seal failed, baseline left unsealed: {exc}")
    verify_results = []
    verify_wanted = verify or bool((config.get("verify") or {}).get("enabled", False))
    if verify_wanted:
        if failures > 0 or audit_state == "fail":
            if verify:
                print("verify skipped: the wave's gates failed")
        else:
            verify_results = run_verify_stage(project_root, tasks, config, ledger,
                                              quiet=quiet)
            errors = [item for item in verify_results if item["verdict"] == "error"]
            if errors and len(errors) == len(verify_results) \
                    and all(item.get("code") == 2 for item in errors):
                if verify:
                    print("verify failed: frontier unavailable")
                    return 2, None
                print("verify skipped: frontier not configured")
            elif any(item["verdict"] != "pass" for item in verify_results):
                failures += 1
    report["verify"] = verify_results
    report["ledger"] = ledger.counts()
    if quiet:
        print(json.dumps(report))
    else:
        print("ledger:", report["ledger"])
    return (0 if failures == 0 and audit_state != "fail" else 1), report

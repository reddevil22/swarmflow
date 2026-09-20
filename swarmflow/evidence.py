"""Evidence bundle assembly for verifier and acceptance passes.

Deterministic, no LLM calls: recon digest, task ledger, worker reports (extracted from
session traces), audit state, regression results, and a bounded git diff against the
run base (with sensitive paths - tests and dependency manifests - diffed in full).
"""

import json
import subprocess
from pathlib import Path

from .audit import audit
from .recon import digest as recon_digest, load_recon
from .workers import scan_trace
from . import runstate

MANIFEST_PATHS = ["package.json", "package-lock.json", "pyproject.toml",
                  "requirements.txt", "go.mod", "go.sum", "Cargo.toml", "Cargo.lock"]

MAX_DIFF_CHARS = 20000
MAX_REPORT_CHARS = 4000


def _git(project_root: str, *args: str, timeout: int = 60):
    try:
        proc = subprocess.run(["git", *args], cwd=project_root, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
        return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        return False, ""


def _run_state(project_root: str) -> dict:
    return runstate.load_run(project_root)


def bundle(project_root: str, ledger, ignores: list | None = None) -> str:
    """Assemble the evidence bundle as markdown."""
    root = Path(project_root)
    state = _run_state(str(root))
    recon = load_recon(str(root))
    lines = [f"# Evidence bundle - {root.name}", "",
             f"- mode: {state.get('mode', 'greenfield')}",
             f"- branch: {state.get('branch', '(none)')}",
             f"- base: {(state.get('base_sha') or '')[:10]}",
             "- note: worker reports are agent narratives and may describe intermediate "
             "failures; the Regression, Audit and Git diff sections reflect the final state",
             ""]

    lines.append("## Recon digest")
    lines.append(recon_digest(recon) if recon else "(no recon recorded)")
    lines.append("")

    lines.append("## Tasks")
    for task in ledger.list_tasks():
        verdict = (task.get("verdict") or "")[:200]
        lines.append(f"- **{task['id']}** wave={task['wave']} status={task['status']} "
                     f"attempts={task['attempts']} verdict={verdict}")
        if task.get("spec_path"):
            lines.append(f"  - spec: {task['spec_path']}")
        if task.get("acceptance"):
            lines.append(f"  - acceptance: {'; '.join(task['acceptance'])}")
    lines.append("")

    lines.append("## Worker reports")
    run_started = state.get("created_at", "")
    for task in ledger.list_tasks():
        trace = task.get("worker_trace")
        if not trace:
            continue
        if run_started and (task.get("created_at") or "") < run_started:
            lines.append(f"### {task['id']} (earlier run - report omitted)")
            lines.append("")
            continue
        spec_path = task.get("spec_path")
        if spec_path and Path(spec_path).exists():
            spec_text = Path(spec_path).read_text(encoding="utf-8", errors="replace")
            lines.append(f"### {task['id']} - spec (frozen)")
            lines.append(spec_text[:1500])
            lines.append("")
        scan = scan_trace(trace)
        text = (scan.get("last_text") or "(no final report found)")[:MAX_REPORT_CHARS]
        lines.append(f"### {task['id']} - report")
        lines.append(text.strip())
        lines.append("")

    lines.append("## Audit")
    try:
        result = audit(str(root), ignores=(state.get("audit_ignores") or [])
                       + list(ignores or []))
    except Exception as exc:                      # the bundle must still render
        result = {"ok": False, "violations": [
            {"kind": "audit-error", "path": "", "detail": str(exc)[:300]}]}
    lines.append(f"- ok: {result['ok']}")
    untracked_baseline = state.get("untracked_baseline") or []
    if untracked_baseline:
        preview = ", ".join(untracked_baseline[:3]) + \
            (" ..." if len(untracked_baseline) > 3 else "")
        lines.append(f"- pre-existing untracked files exempt from the audit: "
                     f"{len(untracked_baseline)} ({preview})")
    lines.append("- note: .gitignore additions (.swarmflow/, logs/) are made by the "
                 "brownfield preflight, before the first freeze")
    for violation in result.get("violations", [])[:50]:
        lines.append(f"- {violation['kind']}: {violation['path']}")
    lines.append("")

    lines.append("## Regression")
    baseline = (state.get("regression") or {}).get("baseline")
    if baseline:
        lines.append(f"- baseline: rc={baseline.get('rc')} "
                     f"failures={baseline.get('failures')} "
                     f"tests_ran={baseline.get('tests_ran')} "
                     f"command=`{baseline.get('command')}`")
        known_failing = baseline.get("fingerprints") or []
        if known_failing:
            lines.append(f"- baseline failing tests ({len(known_failing)}): "
                         + ", ".join(known_failing[:10]))
    else:
        lines.append("- baseline: (none recorded)")
    evidence_dir = root / ".swarmflow" / "evidence"
    for path in sorted(evidence_dir.glob("*.txt")):
        lines.append(f"- evidence file: {path.relative_to(root)}")
    compare_files = sorted(evidence_dir.glob("wave*.compare.json"),
                           key=lambda item: item.stat().st_mtime)
    if compare_files:
        latest = compare_files[-1]
        try:
            comparison = json.loads(latest.read_text(encoding="utf-8"))
        except ValueError:
            comparison = {}
        lines.append(f"- latest comparison ({latest.name}): "
                     f"regressed={comparison.get('regressed')} "
                     f"indeterminate={comparison.get('indeterminate')}")
        for reason in comparison.get("reasons") or []:
            lines.append(f"  - {reason}")
        for name in (comparison.get("new_failures") or [])[:10]:
            lines.append(f"  - new failing test: {name}")
        for name in (comparison.get("fixed_failures") or [])[:10]:
            lines.append(f"  - fixed: {name}")
    wave_files = sorted(evidence_dir.glob("wave*.txt"))
    if wave_files:
        tail = wave_files[-1].read_text(encoding="utf-8", errors="replace")[-2000:]
        lines.append(f"- latest regression evidence ({wave_files[-1].name}) tail:")
        lines.append("```")
        lines.append(tail)
        lines.append("```")
    lines.append("")

    lines.append("## Discrimination (wave tests at the parent state)")
    disc_files = sorted(evidence_dir.glob("wave*.discrimination.json"),
                        key=lambda item: item.stat().st_mtime)
    if not disc_files:
        lines.append("- (none recorded)")
    else:
        latest = disc_files[-1]
        try:
            check = json.loads(latest.read_text(encoding="utf-8"))
        except ValueError:
            check = {}
        if check.get("skipped"):
            lines.append(f"- skipped: {check['skipped']}")
        else:
            counts = check.get("counts") or {}
            probe = check.get("python_probe") or {}
            if probe.get("attempted"):
                lines.append(f"- python probe: candidates={len(probe.get('candidates') or [])} "
                             f"launched={probe.get('launched')} "
                             f"escaped={len(probe.get('escaped') or [])}")
            lines.append(f"- {latest.name}: base {(check.get('base_sha_short') or '?')}, "
                         f"copied {len(check.get('copied') or [])} file(s), "
                         f"red_parent={check.get('red_parent')}, "
                         f"indeterminate={check.get('indeterminate')}")
            lines.append("- verdicts: " + ", ".join(
                f"{verdict}={counts.get(verdict, 0)}" for verdict in (
                    "fails_at_parent", "passes_at_parent", "error_at_parent",
                    "preexisting_at_parent", "deleted_in_wave", "not_observed")))
            if check.get("reason"):
                lines.append(f"- notes: {check['reason']}")
            for entry in check.get("files") or []:
                detail = f" ({', '.join(entry['evidence'][:2])})" if entry.get("evidence") else ""
                lines.append(f"  - {entry['verdict']}: {entry['path']}{detail}")
    lines.append("")

    lines.append("## Processes (post-wave sweep)")
    sweep_files = sorted(evidence_dir.glob("wave*.sweep.json"),
                         key=lambda item: item.stat().st_mtime)
    if not sweep_files:
        lines.append("- (none recorded)")
    else:
        latest = sweep_files[-1]
        try:
            sweep_check = json.loads(latest.read_text(encoding="utf-8"))
        except ValueError:
            sweep_check = {}
        if sweep_check.get("skipped"):
            lines.append(f"- skipped: {sweep_check['skipped']}")
        elif sweep_check.get("indeterminate"):
            lines.append(f"- indeterminate: {sweep_check.get('reason')}")
        else:
            counts = sweep_check.get("counts") or {}
            lines.append(f"- {latest.name}: orphans={counts.get('orphans', 0)} "
                         f"pre_existing={counts.get('pre_existing', 0)} "
                         f"killed={counts.get('killed', 0)} "
                         f"mode={sweep_check.get('mode', 'warn')}")
            for record in sweep_check.get("orphans") or []:
                ports = ", ".join(str(port) for port in record["ports"]) or "-"
                lines.append(f"  - leaked: pid {record['pid']} {record['name']} "
                             f"(ports: {ports}) `{record['cmd'][:120]}`")
            for record in sweep_check.get("pre_existing") or []:
                ports = ", ".join(str(port) for port in record["ports"]) or "-"
                lines.append(f"  - pre-existing listener (untouched): pid {record['pid']} "
                             f"{record['name']} (ports: {ports})")
    lines.append("")

    lines.append("## Git diff vs base")
    base = state.get("base_sha", "")
    if base:
        _, stat = _git(str(root), "diff", "--stat", base)
        lines.append(stat.strip() or "(no diff)")
        paths = list((recon.get("tests") or {}).get("files") or []) + MANIFEST_PATHS
        if paths:
            _, diff = _git(str(root), "diff", base, "--", *paths)
            if diff.strip():
                lines.append("")
                lines.append("### Sensitive paths diff (tests + manifests)")
                lines.append(diff[:MAX_DIFF_CHARS])
                if len(diff) > MAX_DIFF_CHARS:
                    lines.append("... (diff truncated)")
    else:
        lines.append("(no base sha recorded)")
    return "\n".join(lines) + "\n"


def write_bundle(project_root: str, ledger, ignores: list | None = None) -> Path:
    text = bundle(project_root, ledger, ignores=ignores)
    out = Path(project_root) / ".swarmflow" / "evidence" / "bundle.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out

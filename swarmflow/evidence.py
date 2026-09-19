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
    path = Path(project_root) / ".swarmflow" / "run.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def bundle(project_root: str, ledger) -> str:
    """Assemble the evidence bundle as markdown."""
    root = Path(project_root)
    state = _run_state(str(root))
    recon = load_recon(str(root))
    lines = [f"# Evidence bundle - {root.name}", "",
             f"- mode: {state.get('mode', 'greenfield')}",
             f"- branch: {state.get('branch', '(none)')}",
             f"- base: {(state.get('base_sha') or '')[:10]}",
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
        scan = scan_trace(trace)
        text = (scan.get("last_text") or "(no final report found)")[:MAX_REPORT_CHARS]
        lines.append(f"### {task['id']}")
        lines.append(text.strip())
        lines.append("")

    lines.append("## Audit")
    result = audit(str(root))
    lines.append(f"- ok: {result['ok']}")
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
                     f"command=`{baseline.get('command')}`")
    else:
        lines.append("- baseline: (none recorded)")
    evidence_dir = root / ".swarmflow" / "evidence"
    for path in sorted(evidence_dir.glob("*.txt")):
        lines.append(f"- evidence file: {path.relative_to(root)}")
    wave_files = sorted(evidence_dir.glob("wave*.txt"))
    if wave_files:
        tail = wave_files[-1].read_text(encoding="utf-8", errors="replace")[-2000:]
        lines.append(f"- latest regression evidence ({wave_files[-1].name}) tail:")
        lines.append("```")
        lines.append(tail)
        lines.append("```")
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


def write_bundle(project_root: str, ledger) -> Path:
    text = bundle(project_root, ledger)
    out = Path(project_root) / ".swarmflow" / "evidence" / "bundle.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out

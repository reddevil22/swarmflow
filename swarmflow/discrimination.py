"""Discrimination check: do this wave's tests actually fail on the parent state?

Runs the test files a wave's tasks own against the run's base commit in an isolated git
worktree (parent sources + the wave's tests). A test that passes there pins nothing about
the change; verdicts are recorded as evidence. The live working tree is never modified:
cleanup removes linked directories itself and only then deletes the worktree.
"""

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from .audit import _hash_file
from .gitutil import git
from .recon import TEST_PATTERNS
from .regression import run_regression

TEST_ADJACENT = ("fixture", "helper", "mock", "conftest", "jest.config", "jest-e2e",
                 "playwright.config", "vitest.config", "test-setup", "setup-tests")
UNSUPPORTED_FAMILIES = ("go", "cargo")
LAUNCH_FAILED_MARKERS = ("is not recognized as an internal or external command",
                         "command not found", "No such file or directory")
VERDICTS = ("fails_at_parent", "passes_at_parent", "error_at_parent",
            "preexisting_at_parent", "deleted_in_wave", "not_observed")


def _is_test_shaped(rel: str) -> bool:
    name = Path(rel.replace("\\", "/")).name
    return any(pattern in name for pattern in TEST_PATTERNS)


def _is_test_adjacent(rel: str) -> bool:
    lowered = rel.replace("\\", "/").lower()
    if any(token in lowered for token in TEST_ADJACENT):
        return True
    return lowered.startswith(("tests/", "test/")) or "/tests/" in lowered


def _skipped(wave: int, reason: str) -> dict:
    return {"version": 1, "wave": wave, "skipped": reason, "indeterminate": False,
            "files": [], "copied": [], "runs": [], "counts": {}}


CMD_UNSAFE_RE = re.compile(r'[&|^<>%" ]')


def _link_dir(source: Path, target: Path) -> bool:
    try:
        if os.name == "nt":
            if CMD_UNSAFE_RE.search(str(source)) or CMD_UNSAFE_RE.search(str(target)):
                return False   # cmd.exe would re-parse these paths
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(target), str(source)],
                capture_output=True, text=True, timeout=60)
            return proc.returncode == 0
        os.symlink(str(source), str(target), target_is_directory=True)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _unlink_dir(path: Path) -> bool:
    """Remove a directory link without touching its target."""
    try:
        if os.path.islink(path):
            os.unlink(path)
        else:
            os.rmdir(path)   # Windows junction: removes the link, not the contents
        return True
    except OSError:
        return False


def _launch_failed(run: dict) -> bool:
    """A shell that cannot find the command still exits with a code on Windows."""
    if run.get("rc") is None and not run.get("timeout"):
        return True
    output = run.get("output") or ""
    return any(marker in output for marker in LAUNCH_FAILED_MARKERS)


def _unique_name_hit(fingerprint: str, texts: dict) -> str | None:
    """The single copied file whose content carries the fingerprint as a test name.

    A hit is a line equal to the name, or the name inside quotes/backticks (how test
    frameworks declare it). Ambiguity (zero or several files) attributes nothing."""
    needle = (fingerprint or "").strip()
    if not needle:
        return None
    quoted = re.compile(r"['\"`]" + re.escape(needle) + r"['\"`]")
    hits = []
    for rel, text in texts.items():
        for line in text.splitlines():
            if line.strip() == needle or quoted.search(line):
                hits.append(rel)
                break
    return hits[0] if len(hits) == 1 else None


PYTHON_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg")
PYTHON_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PYTHON_NAME_EXCLUDE = {"tests", "test", "docs", "node_modules", "__pycache__",
                       "venv", ".venv", "build", "dist", "site-packages"}
PROBE_LINE = ("import importlib.util; s=importlib.util.find_spec('{name}'); "
              "print(s.origin if s and s.origin else '')")


def _under(path_text: str, root: Path) -> bool:
    try:
        return str(Path(path_text).resolve()).lower().startswith(
            str(root.resolve()).lower() + os.sep)
    except OSError:
        return False


def _python_prepare(project: Path, wt: Path, settings: dict) -> tuple:
    """PYTHONPATH override + candidate package names for a python-looking project."""
    roots = [str(root) for root in (settings.get("python_paths") or ["src"])]
    looks_python = any((project / marker).exists() for marker in PYTHON_MARKERS)
    prepend = []
    names = []
    for root in roots:
        base = wt / root
        if not base.is_dir():
            continue
        prepend.append(str(base))
        if not looks_python and any(base.rglob("*.py")):
            looks_python = True
        for child in sorted(base.iterdir()):
            if not child.is_dir() or child.name in PYTHON_NAME_EXCLUDE \
                    or not PYTHON_NAME_RE.match(child.name):
                continue
            if any(child.rglob("*.py")) and child.name not in names:
                names.append(child.name)
    if not looks_python:
        return None, [], []
    if not prepend:
        return None, names[:5], []
    existing = os.environ.get("PYTHONPATH", "")
    value = os.pathsep.join(prepend + ([existing] if existing else []))
    return {"PYTHONPATH": value}, names[:5], prepend


def _probe_interpreter(project: Path, command: str) -> str:
    lowered = (command or "").strip().lower()
    if lowered.startswith("poetry "):
        return "poetry run python"
    if lowered.startswith("uv "):
        return "uv run python"
    for name in (".venv", "venv"):
        for candidate in ((project / name / "Scripts" / "python.exe"),
                          (project / name / "bin" / "python")):
            if candidate.exists():
                return f'"{candidate}"'
    return "python"


def _probe_packages(project: Path, wt: Path, names: list, command: str,
                    env_override: dict | None, timeout_s: float) -> dict:
    """Verify that the project's own packages import from inside the worktree.

    PYTHONPATH wins over path-entry editable installs; a front-inserted meta_path finder
    bypasses sys.path entirely, so this probe - not the prepend - is the real guard."""
    probe = {"attempted": bool(names), "launched": False, "candidates": names,
             "escaped": []}
    if not names:
        return probe
    interpreter = _probe_interpreter(project, command)
    for name in names:
        probe_cmd = f"{interpreter} -c \"{PROBE_LINE.format(name=name)}\""
        result = run_regression(str(wt), probe_cmd,
                                timeout_s=min(120.0, timeout_s or 120.0),
                                env=env_override)
        if result.get("rc") is None or _launch_failed(result):
            return probe                       # launched stays False
        probe["launched"] = True
        lines = [line.strip() for line in (result.get("output") or "").splitlines()
                 if line.strip()]
        origin = lines[-1] if lines else ""
        if origin and not _under(origin, wt) and (_under(origin, project)
                                                 or "site-packages" in origin
                                                 or "dist-packages" in origin):
            probe["escaped"].append({"name": name, "origin": origin})
    return probe


def _remove_tree(wt: Path, project_root: Path, linked: list) -> list:
    """Delete the worktree safely: links first (never recursively), then the tree."""
    problems = []
    for name in linked:
        path = wt / name
        if path.exists() or os.path.islink(path):
            if not _unlink_dir(path):
                problems.append(name)
                continue
        if path.exists() or os.path.islink(path):
            problems.append(name)
    if not problems and wt.exists():
        shutil.rmtree(wt)
    git(project_root, "worktree", "prune")
    return problems


def run_check(project_root: str, wave: int, tasks: list, config: dict,
              run_state: dict) -> dict:
    """Run this wave's owned test files against the run's base commit in a worktree."""
    settings = config.get("discrimination") or {}
    if not settings.get("enabled", True):
        return _skipped(wave, "disabled in config")
    base_sha = (run_state or {}).get("base_sha") or ""
    if not base_sha:
        return _skipped(wave, "no base_sha (greenfield run)")
    project = Path(project_root)
    skip_patterns = settings.get("skip_patterns") or []
    owned = sorted({rel for task in tasks for rel in (task.get("owner_files") or [])})

    def excluded(rel):
        return any(fnmatch.fnmatch(rel, pattern) for pattern in skip_patterns)

    on_disk = [rel for rel in owned
               if (project / rel).is_file() and _is_test_shaped(rel) and not excluded(rel)]
    ok, listing = git(project, "ls-tree", "-r", "--name-only", base_sha)
    at_base = {line.strip() for line in listing.splitlines() if line.strip()} if ok else set()
    deleted = sorted(rel for rel in owned
                     if _is_test_shaped(rel) and rel in at_base
                     and not (project / rel).exists() and not excluded(rel))
    if not on_disk and not deleted:
        return _skipped(wave, "no owned test-shaped files")
    on_disk = on_disk[:int(settings.get("max_test_files", 20))]
    copy_set = sorted({rel for rel in owned
                       if (project / rel).is_file() and not excluded(rel)
                       and (_is_test_shaped(rel) or _is_test_adjacent(rel))})
    commands = []
    for task in tasks:
        command = (task.get("test_command") or "").strip()
        if command and command not in commands:
            commands.append(command)
    if not commands:
        return _skipped(wave, "no test commands on this wave's tasks")

    wt = project / ".swarmflow" / "discrimination" / f"wt-wave{wave}"
    notes = []
    git(project, "worktree", "prune")
    linked_names = settings.get("link_dirs") or []
    if wt.exists():
        leftovers = _remove_tree(wt, project, linked_names)
        notes.append("removed a leftover worktree from a previous run"
                     if not leftovers else f"leftover worktree not fully removed: {leftovers}")
    wt.parent.mkdir(parents=True, exist_ok=True)
    ok, output = git(project, "worktree", "add", "--detach", str(wt), base_sha)
    if not ok:
        return {"version": 1, "wave": wave, "indeterminate": True,
                "reason": f"worktree add failed: {output.strip()[:300]}",
                "files": [], "copied": [], "runs": [], "counts": {},
                "base_sha": base_sha, "parent": "base_sha"}

    linked = []
    copied = []
    full_runs = []
    problems = []
    try:
        for name in linked_names:
            source = project / name
            if not source.is_dir():
                continue
            if _link_dir(source, wt / name):
                linked.append(name)
            else:
                notes.append(f"could not link {name}")
        for rel in copy_set:
            source = project / rel
            target = wt / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append({"path": rel, "sha256": _hash_file(source)})
        env_override, candidates, _prepend = _python_prepare(project, wt, settings)
        probe = _probe_packages(project, wt, candidates, commands[0], env_override,
                                float(settings.get("timeout_s", 900)))
        early = {"version": 1, "wave": wave, "base_sha": base_sha,
                 "base_sha_short": base_sha[:10], "parent": "base_sha",
                 "worktree": str(wt), "indeterminate": True, "unattributable": False,
                 "unattributable_files": [], "red_parent": False, "tested": 0,
                 "copied": copied, "files": [], "counts": {}, "runs": [],
                 "python_probe": probe}
        if candidates and not probe["launched"]:
            return dict(early, reason="python probe could not run; parent-state "
                                      "isolation unverified")
        if probe["escaped"]:
            escaped = probe["escaped"][0]
            return dict(early, reason=f"package {escaped['name']} imports from outside "
                                      f"the worktree ({escaped['origin']})")
        for index, command in enumerate(commands[:3], start=1):
            evidence = (project / ".swarmflow" / "evidence"
                        / f"wave{wave}.discrimination.parent{index}.txt")
            result = run_regression(str(wt), command,
                                    timeout_s=float(settings.get("timeout_s", 900)),
                                    evidence_path=str(evidence), env=env_override)
            full_runs.append(result)
    finally:
        problems = _remove_tree(wt, project, linked)
        if problems:
            notes.append(f"cleanup incomplete, still linked: {', '.join(problems)}")
        if len(commands) > 3:
            notes.append(f"ran the first 3 of {len(commands)} test commands")

    launched = [run for run in full_runs if not _launch_failed(run)]
    indeterminate = not launched or any(run.get("timeout") or _launch_failed(run)
                                        for run in full_runs) \
        or (len(launched) < len(full_runs))
    families = {run.get("family") for run in full_runs}
    baseline = ((run_state.get("regression") or {}).get("baseline") or {})
    baseline_fp = set(baseline.get("fingerprints") or [])
    green = bool(full_runs) and all(run.get("rc") == 0 for run in full_runs) and not indeterminate
    collected_nothing = any(run.get("rc") == 5 or "No tests found" in (run.get("output") or "")
                            for run in full_runs)
    # A red run only exonerates a file when its failures name files; TAP-style runners
    # report bare test names, so absence from their failure list proves nothing.
    red_runs = [run for run in full_runs
                if not run.get("timeout") and run.get("rc") not in (0, None)]
    blind_red_runs = [run for run in red_runs
                      if not any(("/" in entry or "\\" in entry)
                                 for entry in (run.get("fingerprints") or [])
                                 + (run.get("errors") or []))]
    texts = {}
    for rel in on_disk:
        path = project / rel
        try:
            if path.stat().st_size <= 200_000:
                texts[rel] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

    files = []
    for rel in sorted(set(on_disk) | set(deleted)):
        entry = {"path": rel, "verdict": "not_observed", "evidence": [],
                 "by_task": [task["id"] for task in tasks
                             if rel in (task.get("owner_files") or [])]}
        if rel in deleted:
            entry["verdict"] = "deleted_in_wave"
            files.append(entry)
            continue
        failures = set()
        errors = set()
        seen = False
        for run in full_runs:
            failures.update(name for name in (run.get("fingerprints") or []) if rel in name)
            errors.update(name for name in (run.get("errors") or []) if rel in name)
            seen = seen or rel in (run.get("output") or "")
        test_failures = failures - errors
        if test_failures:
            entry["evidence"] = sorted(test_failures)[:5]
            entry["verdict"] = "preexisting_at_parent" \
                if test_failures <= baseline_fp else "fails_at_parent"
        elif errors:
            entry["verdict"] = "error_at_parent"
            entry["evidence"] = sorted(errors)[:5]
        elif green:
            entry["verdict"] = "passes_at_parent"
            entry["basis"] = "suite_green"
        elif blind_red_runs and not collected_nothing and not indeterminate:
            # path-less runner: a failure name that appears as a whole line in exactly
            # one copied file attributes that failure to it.
            hits = set()
            for run in blind_red_runs:
                for fingerprint in run.get("fingerprints") or []:
                    if _unique_name_hit(fingerprint, texts) == rel:
                        hits.add(fingerprint)
            if hits:
                entry["verdict"] = "preexisting_at_parent" \
                    if hits <= baseline_fp else "fails_at_parent"
                entry["basis"] = "name_found_in_copied_file"
                entry["evidence"] = sorted(hits)[:5]
            else:
                entry["reason"] = "unattributable_failures"
        elif not collected_nothing and not indeterminate:
            # Path-bearing runners name failing files; absence from their failures of a
            # completed run means the copied file passed (or was skipped).
            entry["verdict"] = "passes_at_parent"
            entry["basis"] = "absent_from_failures"
        elif not seen:
            entry["reason"] = "file_not_executed"
        files.append(entry)

    if families and families <= set(UNSUPPORTED_FAMILIES):
        return _skipped(wave, f"unsupported test family: {', '.join(sorted(families))}")

    counts = {verdict: sum(1 for entry in files if entry["verdict"] == verdict)
              for verdict in VERDICTS}
    unattributable = [entry["path"] for entry in files
                      if entry.get("reason") == "unattributable_failures"]
    result = {"version": 1, "wave": wave, "base_sha": base_sha,
              "base_sha_short": base_sha[:10], "parent": "base_sha",
              "worktree": str(wt), "indeterminate": indeterminate,
              "unattributable": bool(unattributable),
              "unattributable_files": unattributable,
              "python_probe": probe,
              "red_parent": bool(baseline and baseline.get("rc") != 0),
              "reason": "; ".join(notes), "tested": len(files),
              "copied": copied, "files": files, "counts": counts,
              "runs": [{"command": run.get("command"), "rc": run.get("rc"),
                        "timeout": run.get("timeout"), "duration_s": run.get("duration_s")}
                       for run in full_runs]}
    return result

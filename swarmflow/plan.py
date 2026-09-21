"""Plan loading, validation, scaffolding and enqueueing."""

import re
import shutil
import subprocess
from pathlib import Path

import yaml

from .config import asset

REQUIRED_TASK_KEYS = {"id", "module", "owner_files"}
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}


def _path_problem(value) -> str | None:
    """Return an error when a value is not a safe project-relative path."""
    if not isinstance(value, str) or not value.strip():
        return "must be a non-empty string"
    text = value.replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return "must be relative to the project root"
    if ".." in text.split("/"):
        return "must not contain '..' segments"
    return None


def load_plan(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def validate_plan(plan: dict) -> list[str]:
    """Return a list of problems; empty list means the plan is usable."""
    errors = []
    mode = plan.get("mode", "greenfield")
    if mode not in ("greenfield", "brownfield"):
        return [f"mode must be 'greenfield' or 'brownfield', got {mode!r}"]
    tasks = plan.get("tasks")
    if not tasks:
        return ["plan has no tasks"]
    project_name = plan.get("project_name")
    if project_name is not None and not ID_RE.match(str(project_name)):
        errors.append(f"project_name must match {ID_RE.pattern} (it names the run branch)")
    seen_ids = set()
    owner_map: dict[str, str] = {}
    for task in tasks:
        task_id = task.get("id", "<missing id>")
        missing = REQUIRED_TASK_KEYS - set(task)
        if missing:
            errors.append(f"{task_id}: missing keys {sorted(missing)}")
            continue
        if not ID_RE.match(str(task_id)) or ".." in str(task_id):
            errors.append(f"{task_id}: id must match {ID_RE.pattern} without '..'")
        if task_id in seen_ids:
            errors.append(f"{task_id}: duplicate id")
        seen_ids.add(task_id)
        if "test_command" in task and not isinstance(task["test_command"], str):
            errors.append(f"{task_id}: test_command must be a string")
        thinking = task.get("thinking")
        if thinking is not None and thinking not in THINKING_LEVELS:
            errors.append(f"{task_id}: thinking must be one of {sorted(THINKING_LEVELS)}")
        module = task.get("module")
        if module is not None and not isinstance(module, str):
            errors.append(f"{task_id}: module must be a string")
        acceptance = task.get("acceptance")
        if acceptance is not None and (
                not isinstance(acceptance, list)
                or not all(isinstance(item, str) for item in acceptance)):
            errors.append(f"{task_id}: acceptance must be a list of strings")
        files_to_read = task.get("files_to_read")
        if files_to_read is not None and (
                not isinstance(files_to_read, list)
                or not all(isinstance(item, str) for item in files_to_read)):
            errors.append(f"{task_id}: files_to_read must be a list of strings")
        for file_name in files_to_read or []:
            problem = _path_problem(file_name)
            if problem:
                errors.append(f"{task_id}: files_to_read entry {file_name!r} {problem}")
        owners = task.get("owner_files") or []
        if not owners:
            errors.append(f"{task_id}: owner_files is empty (shared files belong to integration)")
        for file_name in owners:
            problem = _path_problem(file_name)
            if problem:
                errors.append(f"{task_id}: owner_files entry {file_name!r} {problem}")
                continue
            if file_name in owner_map and owner_map[file_name] != task_id:
                errors.append(
                    f"ownership overlap: {file_name} claimed by {owner_map[file_name]} and {task_id}"
                )
            owner_map[file_name] = task_id
    return errors


def write_specs(plan: dict, specs_dir: Path) -> dict:
    """Write each task's spec text to <specs_dir>/<id>.md. Returns id -> path."""
    specs_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for task in plan.get("tasks", []):
        path = specs_dir / f"{task['id']}.md"
        text = task.get("spec") or "(no spec text provided)"
        path.write_text(f"# Task {task['id']}\n\n{text}\n", encoding="utf-8")
        paths[task["id"]] = str(path)
    return paths


def render_spec_md(plan: dict) -> str:
    lines = [f"# {plan.get('project_name', 'project')} - SPEC", ""]
    scope = plan.get("mvp_scope") or {}
    if scope:
        lines.append("## MVP-1 scope")
        lines += [f"- in: {item}" for item in scope.get("in", [])]
        lines += [f"- out: {item}" for item in scope.get("out", [])]
        lines.append("")
    contracts = plan.get("contracts") or []
    if contracts:
        lines.append("## Interfaces (frozen)")
        for contract in contracts:
            lines.append(f"### {contract.get('name', '?')}")
            lines.append(contract.get("definition", ""))
        lines.append("")
    lines.append("## Tasks")
    for task in plan.get("tasks", []):
        lines.append(f"- **{task['id']}** ({task.get('module')}) -> files: {', '.join(task.get('owner_files', []))}")
        for check in task.get("acceptance") or []:
            lines.append(f"    - acceptance: {check}")
    lines.append("")
    lines.append("## Acceptance criteria (MVP-1)")
    for criterion in plan.get("acceptance_criteria") or []:
        lines.append(f"- {criterion.get('id')}: {criterion.get('criterion')} [{criterion.get('check')}]")
    return "\n".join(lines) + "\n"


def scaffold(plan: dict, project_root: Path,
             mode: str = "greenfield") -> dict:
    """Create the project skeleton for the given mode.

    greenfield: owns the repo - writes AGENTS.md, root SPEC.md, specs/, git-inits.
    brownfield: never touches user files - artifacts live under `.swarmflow/`,
    no AGENTS.md copy, no git init.
    """
    project_root.mkdir(parents=True, exist_ok=True)
    artifacts_dir = ""
    if mode == "brownfield":
        artifacts = project_root / ".swarmflow"
        artifacts.mkdir(parents=True, exist_ok=True)
        artifacts_dir = str(artifacts)
        spec_md = artifacts / "SPEC.md"
        spec_paths = write_specs(plan, artifacts / "specs")
        git_info = "existing" if (project_root / ".git").exists() else "missing"
    else:
        shutil.copyfile(asset("AGENTS.worker.md"), project_root / "AGENTS.md")
        spec_md = project_root / "SPEC.md"
        spec_paths = write_specs(plan, project_root / "specs")
        git_info = "existing"
        if not (project_root / ".git").exists():
            try:
                subprocess.run(["git", "init"], cwd=project_root, capture_output=True,
                               text=True, check=True)
                git_info = "initialized"
            except (OSError, subprocess.CalledProcessError):
                git_info = "unavailable"
    spec_md.write_text(render_spec_md(plan), encoding="utf-8")
    for task in plan.get("tasks", []):
        for file_name in task.get("owner_files", []):
            parent = (project_root / file_name).parent
            if parent and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
    return {"spec_paths": spec_paths, "git": git_info, "artifacts_dir": artifacts_dir}


def enqueue_plan(ledger, plan: dict, project_root: Path, spec_paths: dict) -> dict:
    inserted = 0
    for task in plan.get("tasks", []):
        if ledger.add_task(
            task_id=task["id"],
            project=str(project_root),
            wave=int(task.get("wave", 1)),
            module=task.get("module", ""),
            owner_files=task.get("owner_files", []),
            acceptance=task.get("acceptance", []),
            spec_path=spec_paths.get(task["id"], ""),
            thinking=task.get("thinking", "high"),
            test_command=task.get("test_command", ""),
            files_to_read=task.get("files_to_read", []),
        ):
            inserted += 1
    return {"inserted": inserted, "total": len(plan.get("tasks", []))}

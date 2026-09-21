"""Repository reconnaissance for brownfield runs.

Deterministic, no LLM, no network. Produces `<project>/.swarmflow/recon.json` plus a
bounded markdown digest that planners and verifiers consume. Every detected command
carries an evidence string so nothing is guessed silently.
"""

import json
import os
import time
from collections import Counter
from pathlib import Path

from .gitutil import git

ARTIFACT_DIRS = {
    ".git", "node_modules", ".venv", "venv", "target", "dist", "build", "coverage",
    ".next", ".tox", ".mypy_cache", ".ruff_cache", "__pycache__", ".pytest_cache",
    ".swarmflow", "logs", "state",
}

CODE_EXTS = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs",
             ".java", ".rb", ".c", ".h", ".cpp", ".cs", ".php", ".swift", ".kt"}

TEST_PATTERNS = ("test_", "_test.", ".spec.", ".test.")

CONVENTION_FILES = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md")

MANIFESTS = [
    ("node", "package.json"),
    ("python", "pyproject.toml"),
    ("python", "requirements.txt"),
    ("go", "go.mod"),
    ("rust", "Cargo.toml"),
]

MAX_SCAN_FILES = 3000
MAX_FILE_BYTES = 400_000
DIGEST_LIMIT = 6000


def _git(project_root: Path, *args: str, timeout: int = 30):
    """Recon-level git with its own shorter timeout; see ``gitutil.git``."""
    return git(project_root, *args, timeout=timeout)


MAX_UNTRACKED = 2000


def untracked_paths(project_root, cap: int = MAX_UNTRACKED) -> list:
    """File-level untracked paths (`-uall`, so a new directory lists its files).

    Capped for very large trees; anything beyond the cap simply keeps violating the
    scope audit (the conservative direction)."""
    ok, porcelain = _git(Path(project_root), "status", "--porcelain", "-uall")
    if not ok:
        return []
    paths = []
    for line in porcelain.splitlines():
        if not line.startswith("?? "):
            continue
        rel = line[3:].strip()
        if rel.startswith('"') and rel.endswith('"'):
            rel = rel[1:-1].encode("latin-1", "replace").decode("unicode_escape")
        paths.append(rel.replace("\\", "/"))
        if len(paths) >= cap:
            break
    return paths


def git_state(project_root) -> dict:
    """Git facts. `dirty_tracked` counts only tracked modifications (not untracked)."""
    root = Path(project_root)
    ok, _ = _git(root, "rev-parse", "--is-inside-work-tree")
    if not ok:
        return {"is_git": False}
    ok_head, head = _git(root, "rev-parse", "HEAD")
    ok_branch, branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    _, porcelain = _git(root, "status", "--porcelain")
    ok_count, commits = _git(root, "rev-list", "--count", "HEAD")
    lines = [line for line in porcelain.splitlines() if line.strip()]
    dirty = [line for line in lines if not line.startswith("??")]
    untracked = untracked_paths(root)
    return {
        "is_git": True,
        "head": head if ok_head else "",
        "branch": branch if ok_branch else "",
        "commits": int(commits) if ok_count and commits.isdigit() else 0,
        "dirty_tracked": dirty[:50],
        "untracked_count": len(untracked),
        "untracked_files": untracked,
    }


def detect_stacks(project_root) -> list:
    root = Path(project_root)
    stacks = []
    seen = set()
    for stack, manifest in MANIFESTS:
        if (root / manifest).exists() and stack not in seen:
            stacks.append({"stack": stack, "evidence": manifest})
            seen.add(stack)
    return stacks


def _has_python_tests(root: Path) -> str:
    if (root / "tests").is_dir():
        return "tests/ directory present"
    for pattern in ("test_*.py", "*_test.py"):
        for match in root.glob(pattern):
            return f"{match.name} present"
    return ""


def _node_scripts(root: Path) -> dict:
    try:
        payload = json.loads((root / "package.json").read_text(encoding="utf-8"))
        return payload.get("scripts") or {}
    except (OSError, ValueError):
        return {}


def detect_commands(project_root, stacks: list) -> dict:
    """Candidate regression/build/lint commands with evidence. First stack wins."""
    root = Path(project_root)
    commands = {"regression": None, "build": None, "lint": None}

    def set_if_empty(key, command, evidence):
        if commands[key] is None:
            commands[key] = {"command": command, "evidence": evidence}

    for entry in stacks:
        stack = entry["stack"]
        if stack == "node":
            scripts = _node_scripts(root)
            if scripts.get("test"):
                set_if_empty("regression", "npm test --silent",
                             "package.json scripts.test")
            if scripts.get("build"):
                set_if_empty("build", "npm run build --silent",
                             "package.json scripts.build")
            if scripts.get("lint"):
                set_if_empty("lint", "npm run lint --silent",
                             "package.json scripts.lint")
        elif stack == "python":
            evidence = _has_python_tests(root)
            if evidence:
                set_if_empty("regression", "python -m pytest -q", evidence)
        elif stack == "go":
            set_if_empty("regression", "go test ./...", "go.mod")
            set_if_empty("build", "go build ./...", "go.mod")
            set_if_empty("lint", "go vet ./...", "go.mod")
        elif stack == "rust":
            set_if_empty("regression", "cargo test", "Cargo.toml")
            set_if_empty("build", "cargo build", "Cargo.toml")
            set_if_empty("lint", "cargo clippy", "Cargo.toml")
    return commands


def test_inventory(project_root, limit: int = 30) -> dict:
    root = Path(project_root)
    found = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ARTIFACT_DIRS]
        for name in filenames:
            scanned += 1
            if scanned > MAX_SCAN_FILES:
                break
            if any(token in name for token in TEST_PATTERNS):
                rel = (Path(dirpath) / name).relative_to(root).as_posix()
                found.append(rel)
                if len(found) >= limit:
                    break
        if len(found) >= limit or scanned > MAX_SCAN_FILES:
            break
    return {"count_sampled": len(found), "files": sorted(found)}


def file_inventory(project_root) -> dict:
    root = Path(project_root)
    by_ext: Counter = Counter()
    top_dirs: Counter = Counter()
    total_files = 0
    total_loc = 0
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ARTIFACT_DIRS]
        for name in filenames:
            scanned += 1
            if scanned > MAX_SCAN_FILES:
                break
            full = Path(dirpath) / name
            rel = full.relative_to(root)
            total_files += 1
            by_ext[full.suffix.lower() or "<none>"] += 1
            if len(rel.parts) > 1:
                top_dirs[rel.parts[0]] += 1
            if full.suffix.lower() in CODE_EXTS:
                try:
                    if full.stat().st_size <= MAX_FILE_BYTES:
                        with full.open(encoding="utf-8", errors="replace") as handle:
                            total_loc += sum(1 for _ in handle)
                except OSError:
                    pass
        if scanned > MAX_SCAN_FILES:
            break
    return {
        "total_files": total_files,
        "approx_loc": total_loc,
        "by_extension": dict(by_ext.most_common(12)),
        "top_dirs": dict(top_dirs.most_common(8)),
        "truncated": scanned > MAX_SCAN_FILES,
    }


def conventions_files(project_root) -> list:
    root = Path(project_root)
    return [name for name in CONVENTION_FILES if (root / name).exists()]


def recon(project_root, regression_command: str = "") -> dict:
    """Run reconnaissance and persist `.swarmflow/recon.json`."""
    root = Path(project_root).resolve()
    stacks = detect_stacks(root)
    info = {
        "project_root": str(root),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "git": git_state(root),
        "env": {"NODE_ENV": os.environ.get("NODE_ENV", "")},
        "stacks": stacks,
        "commands": detect_commands(root, stacks),
        "conventions": conventions_files(root),
        "tests": test_inventory(root),
        "inventory": file_inventory(root),
    }
    if regression_command:
        info["commands"]["regression"] = {
            "command": regression_command, "evidence": "config/CLI override"}
    out_dir = root / ".swarmflow"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "recon.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info


def load_recon(project_root) -> dict:
    path = Path(project_root).resolve() / ".swarmflow" / "recon.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def digest(info: dict, limit: int = DIGEST_LIMIT) -> str:
    """Bounded markdown digest of a recon result, for planner/verifier prompts."""
    lines = ["# Repository recon", ""]
    git = info.get("git") or {}
    if git.get("is_git"):
        lines.append(f"- git: branch `{git.get('branch')}`, head `{(git.get('head') or '')[:10]}`, "
                     f"{git.get('commits', 0)} commits, "
                     f"{len(git.get('dirty_tracked') or [])} dirty tracked file(s)")
    else:
        lines.append("- git: NOT a git repository (brownfield mode requires git)")
    stacks = info.get("stacks") or []
    lines.append("- stacks: " + (", ".join(f"{s['stack']} ({s['evidence']})"
                                           for s in stacks) or "none detected"))
    env = info.get("env") or {}
    if env.get("NODE_ENV"):
        lines.append(f"- ambient env: NODE_ENV={env['NODE_ENV']} (node stacks can behave "
                     "differently under it: production conditions may hide dev-only APIs)")
    commands = info.get("commands") or {}
    for key in ("regression", "build", "lint"):
        entry = commands.get(key)
        if entry:
            lines.append(f"- {key} command: `{entry['command']}` (evidence: {entry['evidence']})")
    conventions = info.get("conventions") or []
    if conventions:
        lines.append("- conventions files: " + ", ".join(conventions))
    tests = info.get("tests") or {}
    lines.append(f"- existing tests sampled: {tests.get('count_sampled', 0)}")
    for name in (tests.get("files") or [])[:12]:
        lines.append(f"  - {name}")
    inventory = info.get("inventory") or {}
    if inventory:
        lines.append(f"- inventory: {inventory.get('total_files', 0)} files, "
                     f"~{inventory.get('approx_loc', 0)} LOC")
        top = inventory.get("by_extension") or {}
        lines.append("- extensions: " + ", ".join(f"{ext}:{count}" for ext, count in top.items()))
        dirs = inventory.get("top_dirs") or {}
        lines.append("- top dirs: " + ", ".join(f"{d}:{count}" for d, count in dirs.items()))
    text = "\n".join(lines) + "\n"
    if len(text) > limit:
        text = text[:limit] + "\n... (digest truncated)\n"
    return text


def ensure_gitignore_entries(project_root, entries: list) -> list:
    """Append missing entries to the project .gitignore (once). Returns added entries."""
    path = Path(project_root).resolve() / ".gitignore"
    existing = []
    if path.exists():
        existing = path.read_text(encoding="utf-8").splitlines()
    stripped = {line.strip() for line in existing}
    added = [entry for entry in entries if entry not in stripped]
    if added:
        with path.open("a", encoding="utf-8") as handle:
            if existing and existing[-1].strip():
                handle.write("\n")
            handle.write("# swarmflow artifacts\n")
            for entry in added:
                handle.write(entry + "\n")
    return added


def branch_ensure(project_root, branch: str) -> str:
    """Check out `branch`, creating it if needed. Returns 'created' or 'reused'."""
    root = Path(project_root)
    ok, _ = _git(root, "rev-parse", "--verify", branch)
    if ok:
        _git(root, "checkout", branch)
        return "reused"
    _git(root, "checkout", "-b", branch)
    return "created"

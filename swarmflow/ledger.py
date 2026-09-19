"""SQLite-backed task ledger: the single source of truth for swarm state."""

import json
import sqlite3
import time
from pathlib import Path

STATUSES = [
    "queued",
    "running",
    "delivered",
    "verified",
    "needs_fix",
    "failed",
    "escalated",
    "awaiting_pr_review",
    "integrated",
    "accepted",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    wave INTEGER NOT NULL DEFAULT 1,
    module TEXT,
    owner_files TEXT,
    acceptance TEXT,
    spec_path TEXT,
    test_command TEXT DEFAULT '',
    files_to_read TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    thinking TEXT NOT NULL DEFAULT 'high',
    worker_trace TEXT,
    report_path TEXT,
    artifacts TEXT,
    verdict TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    task_id TEXT,
    kind TEXT,
    detail TEXT
);
"""

_UPDATABLE = {"worker_trace", "report_path", "artifacts", "verdict", "thinking", "module"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Ledger:
    def __init__(self, path: str):
        db_path = Path(path)
        if db_path.parent and not db_path.parent.exists():
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        try:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN test_command TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN files_to_read TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def add_task(self, task_id: str, project: str, wave: int = 1, module: str = "",
                 owner_files: list | None = None, acceptance: list | None = None,
                 spec_path: str = "", thinking: str = "high",
                 test_command: str = "", files_to_read: list | None = None) -> bool:
        """Insert a task if it does not exist. Returns True when inserted."""
        now = _now()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO tasks (id, project, wave, module, owner_files, acceptance,"
            " spec_path, test_command, files_to_read, status, attempts, thinking,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?)",
            (task_id, project, wave, module, json.dumps(owner_files or []),
             json.dumps(acceptance or []), spec_path, test_command,
             json.dumps(files_to_read or []), thinking, now, now),
        )
        self.conn.commit()
        if cur.rowcount:
            self.record_event(task_id, "enqueued", f"wave={wave} module={module}")
            return True
        return False

    def get(self, task_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def set_status(self, task_id: str, status: str, **fields) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status: {status}")
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        sets = ["status = ?", "updated_at = ?"]
        values: list = [status, _now()]
        for key, value in fields.items():
            if key == "artifacts" and not isinstance(value, str):
                value = json.dumps(value)
            sets.append(f"{key} = ?")
            values.append(value)
        values.append(task_id)
        self.conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", values)
        self.conn.commit()
        self.record_event(task_id, f"status:{status}", json.dumps(fields)[:500])

    def bump_attempts(self, task_id: str) -> int:
        self.conn.execute(
            "UPDATE tasks SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (_now(), task_id),
        )
        self.conn.commit()
        task = self.get(task_id)
        return task["attempts"] if task else 0

    def record_event(self, task_id: str, kind: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO events (ts, task_id, kind, detail) VALUES (?, ?, ?, ?)",
            (_now(), task_id, kind, detail),
        )
        self.conn.commit()

    def list_tasks(self, status: str | None = None, wave: int | None = None) -> list[dict]:
        query = "SELECT * FROM tasks"
        clauses, values = [], []
        if status:
            clauses.append("status = ?")
            values.append(status)
        if wave is not None:
            clauses.append("wave = ?")
            values.append(wave)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY wave, id"
        return [self._row_to_dict(r) for r in self.conn.execute(query, values).fetchall()]

    def counts(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status ORDER BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def events(self, task_id: str | None = None, limit: int = 50) -> list[dict]:
        if task_id:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE task_id = ? ORDER BY seq DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        task = dict(row)
        for key in ("owner_files", "acceptance", "artifacts", "files_to_read"):
            if task.get(key):
                try:
                    task[key] = json.loads(task[key])
                except (TypeError, ValueError):
                    pass
        return task

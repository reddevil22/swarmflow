"""CLI surface: --json output parses, and the light commands behave."""

import json

import yaml

from swarmflow import cli
from swarmflow.ledger import Ledger


def _config(tmp_path, **extra):
    config = {"paths": {"ledger": str(tmp_path / "ledger.db"),
                        "state_dir": str(tmp_path / "state")}}
    config.update(extra)
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def test_plan_rejects_worktree_without_load(tmp_path, capsys):
    """--worktree isolates a loaded run; alone it would silently do nothing."""
    project = tmp_path / "proj"
    project.mkdir()
    prd = tmp_path / "PRD.md"
    prd.write_text("# Requirements\n", encoding="utf-8")

    rc = cli.main(["--config", str(_config(tmp_path)), "plan", "--prd", str(prd),
                   "--project", str(project), "--worktree"])

    assert rc == 2
    assert "--load" in capsys.readouterr().out


def test_status_json(tmp_path, capsys):
    assert cli.main(["--config", str(_config(tmp_path)), "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"] == []
    assert payload["counts"] == {}


def test_recon_json(tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (project / "tests").mkdir()
    (project / "tests" / "test_x.py").write_text("def test_ok():\n    assert True\n",
                                                 encoding="utf-8")
    assert cli.main(["--config", str(_config(tmp_path)), "recon",
                     "--project", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(stack["stack"] == "python" for stack in payload["stacks"])
    assert payload["commands"]["regression"]["command"]


def test_trace_output_is_always_valid_json(tmp_path, capsys):
    trace = tmp_path / "t.jsonl"
    trace.write_text(json.dumps({
        "type": "message_end",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "x" * 20000}],
                    "usage": {"output": 5}},
    }) + "\n", encoding="utf-8")
    assert cli.main(["--config", str(_config(tmp_path)), "trace", str(trace)]) == 0
    payload = json.loads(capsys.readouterr().out)     # long text must not truncate JSON
    assert payload["exists"] is True
    assert len(payload["last_text"]) == 12000         # capped field, valid document


def test_freeze_and_audit_json(tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    config = _config(tmp_path)
    ledger = Ledger(str(tmp_path / "ledger.db"))
    ledger.add_task("T1", str(project.resolve()), owner_files=["a.py"])
    ledger.close()
    assert cli.main(["--config", str(config), "freeze", "--project", str(project),
                     "--json"]) == 0
    frozen = json.loads(capsys.readouterr().out)
    assert frozen["frozen_files"] == 1
    assert frozen["entries"] == 1
    assert frozen["carried"] == 0            # a manual freeze re-balances everything
    assert cli.main(["--config", str(config), "audit", "--project", str(project),
                     "--json"]) == 0
    audited = json.loads(capsys.readouterr().out)
    assert audited["ok"] is True


def test_wave_run_json_without_tasks(tmp_path, capsys):
    assert cli.main(["--config", str(_config(tmp_path)), "wave-run", "--wave", "1",
                     "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"] == []
    assert payload["ledger"] == {}


def test_evidence_json(tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    assert cli.main(["--config", str(_config(tmp_path)), "evidence",
                     "--project", str(project), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["bundle"].endswith("bundle.md")
    assert payload["bytes"] > 0


def test_plan_load_json(tmp_path, capsys):
    project = tmp_path / "proj"
    plan = {"project_name": "demo", "project": str(project),
            "tasks": [{"id": "T1", "module": "app.py", "owner_files": ["app.py"],
                       "spec": "do", "wave": 1}]}
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(yaml.safe_dump(plan), encoding="utf-8")
    assert cli.main(["--config", str(_config(tmp_path)), "plan-load",
                     "--plan", str(plan_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "greenfield"
    assert payload["tasks"]["inserted"] == 1


def test_smoke_frontier_json_reports_the_error(tmp_path, monkeypatch, capsys):
    # inject the failure: a real connect to a closed loopback port blocks (rather than
    # refusing) on some Linux environments, which used to hang CI for the full timeout
    def boom(*args, **kwargs):
        raise OSError("connection refused (test)")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    config = _config(tmp_path, frontier={"backend": "openai",
                                         "base_url": "http://127.0.0.1:1",
                                         "timeout_s": 1})
    assert cli.main(["--config", str(config), "smoke-frontier", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "error" in payload


def test_smoke_worker_json_reports_the_error(tmp_path, capsys):
    assert cli.main(["--config", str(_config(tmp_path)), "smoke-worker",
                     "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "worker.model" in payload["error"]


def test_init_writes_the_config(tmp_path, monkeypatch, capsys):
    target = tmp_path / "swarmflow.yaml"
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATH", target)
    monkeypatch.setattr(cli, "LEGACY_CONFIG_PATH", tmp_path / "absent.yaml")
    assert cli.main(["init"]) == 0
    assert target.exists()
    assert "paths" in target.read_text(encoding="utf-8")

"""Offline synthetic freeze CLI and artifact boundary checks."""

import json
import os
import subprocess
import sys

import pytest

from eval.goal5c.__main__ import main
from eval.goal5c.contracts import canonical_json_bytes
from eval.goal5c.freeze import freeze_registry
from test_goal5c_freeze import synthetic_registry, synthetic_plan


def _inputs(tmp_path):
    registry = synthetic_registry()
    plan = synthetic_plan(registry)
    registry_path = tmp_path / "registry.json"
    plan_path = tmp_path / "plan.json"
    registry_path.write_bytes(canonical_json_bytes(registry))
    plan_path.write_bytes(canonical_json_bytes(plan))
    return registry, plan, registry_path, plan_path


def test_freeze_cli_is_deterministic_across_hash_seeds(tmp_path):
    registry, plan, registry_path, plan_path = _inputs(tmp_path)
    before = registry_path.read_bytes(), plan_path.read_bytes()
    outputs = []
    for seed in ("1", "97"):
        output = tmp_path / f"freeze-{seed}.json"
        args = [sys.executable, "-m", "eval.goal5c", "freeze-synthetic", "--registry", str(registry_path),
                "--plan", str(plan_path), "--output", str(output)]
        result = subprocess.run(args, env={**os.environ, "PYTHONHASHSEED": seed}, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        outputs.append(output.read_bytes())
        repeat = subprocess.run(args, capture_output=True, text=True)
        assert repeat.returncode == 2
        assert output.read_bytes() == outputs[-1]
    assert outputs[0] == outputs[1] == canonical_json_bytes(freeze_registry(registry, plan))
    assert (registry_path.read_bytes(), plan_path.read_bytes()) == before


def test_freeze_cli_refuses_git_output(tmp_path, capsys):
    _, _, registry_path, plan_path = _inputs(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: synthetic", encoding="utf-8")
    output = repo / "roster.json"
    assert main(["freeze-synthetic", "--registry", str(registry_path),
                 "--plan", str(plan_path), "--output", str(output)]) == 2
    assert not output.exists()
    assert "input rejected" in capsys.readouterr().out


@pytest.mark.parametrize("mutation", ["real_data", "outcomes", "approval_override"])
def test_freeze_cli_rejects_non_synthetic_or_authorizing_plan(tmp_path, capsys, mutation):
    registry, plan, registry_path, plan_path = _inputs(tmp_path)
    if mutation == "real_data":
        registry["data_class"] = "private"
        registry_path.write_bytes(canonical_json_bytes(registry))
    elif mutation == "outcomes":
        plan["outcomes_observed"] = True
    else:
        plan["real_execution_authorized"] = True
    plan_path.write_bytes(canonical_json_bytes(plan))
    output = tmp_path / "rejected.json"
    assert main(["freeze-synthetic", "--registry", str(registry_path),
                 "--plan", str(plan_path), "--output", str(output)]) == 2
    assert not output.exists()
    assert registry["queries"][0]["original_query"] not in capsys.readouterr().out

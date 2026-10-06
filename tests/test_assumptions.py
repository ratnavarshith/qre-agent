from pathlib import Path

import yaml

from qre_agent import load_assumptions
from qre_agent.assumptions import DEFAULT_PATH, REPO_ROOT


def test_default_yaml_has_no_machine_specific_paths():
    raw = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))["bicycle"]
    assert not Path(raw["compiler_dir"]).is_absolute()
    assert not Path(raw["cache_dir"]).is_absolute()


def test_relative_compiler_dir_resolves_against_the_repo_root(monkeypatch):
    monkeypatch.delenv("QRE_COMPILER_DIR", raising=False)
    assert Path(load_assumptions().compiler_dir) == REPO_ROOT / "bicycle-compiler/target/release"


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("QRE_COMPILER_DIR", str(tmp_path))
    assert Path(load_assumptions().compiler_dir) == tmp_path


def test_recorded_assumptions_leave_out_machine_paths():
    recorded = load_assumptions().as_dict()
    assert "compiler_dir" not in recorded and "cache_dir" not in recorded

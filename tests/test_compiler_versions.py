import hashlib
from types import SimpleNamespace

import pytest

from qre_agent import compiler

LOCK = '[[package]]\nname = "rsgridsynth"\nversion = "0.2.2"\n'
SHA = "bed7e8aba4cc0fb86311b7fa6dad6217c0fce7cb"


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A compiler tree as the Docker image lays it out: <root>/target/release and <root>/Cargo.lock,
    with no git checkout."""
    (tmp_path / "target" / "release").mkdir(parents=True)
    (tmp_path / "Cargo.lock").write_bytes(LOCK.encode())
    monkeypatch.setattr(
        compiler, "run", lambda a, name, args, stdin=None: "bicycle_compiler 0.2.1\n"
    )
    monkeypatch.setattr(compiler, "_git", lambda *args: None)
    return tmp_path


def versions(root):
    return compiler.compiler_versions(
        SimpleNamespace(compiler_dir=str(root / "target" / "release"))
    )


def test_a_tree_without_git_names_its_commit_in_a_commit_file(root):
    (root / "COMMIT").write_text(SHA + "\n", encoding="utf-8")
    assert versions(root) == {
        "bicycle_compiler": "0.2.1",
        "bicycle_compiler_commit": SHA,
        "rsgridsynth": "0.2.2",
        "cargo_lock_sha256": hashlib.sha256(LOCK.encode()).hexdigest(),
    }


def test_a_tree_with_neither_git_nor_a_commit_file_has_no_commit(root):
    assert "bicycle_compiler_commit" not in versions(root)


def test_git_wins_over_the_commit_file(root, monkeypatch):
    (root / "COMMIT").write_text("0" * 40, encoding="utf-8")
    monkeypatch.setattr(
        compiler, "_git", lambda root, *args: "abc" if args[0] == "rev-parse" else ""
    )
    assert versions(root)["bicycle_compiler_commit"] == "abc"


def test_a_machine_without_git_still_gets_every_other_version(root, monkeypatch):
    """The image has no git: that is no commit from git, not a failure of the whole record."""

    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.undo()  # the fixture's _git stub would hide the real one
    monkeypatch.setattr(
        compiler, "run", lambda a, name, args, stdin=None: "bicycle_compiler 0.2.1\n"
    )
    monkeypatch.setattr(compiler.subprocess, "run", no_git)
    (root / "COMMIT").write_text(SHA, encoding="utf-8")
    got = versions(root)
    assert got["bicycle_compiler"] == "0.2.1" and got["bicycle_compiler_commit"] == SHA
    assert got["rsgridsynth"] == "0.2.2"

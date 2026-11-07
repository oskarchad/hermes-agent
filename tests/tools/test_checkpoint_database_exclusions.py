"""Database snapshots belong to the backup owner, never the code checkpoint store."""

import hashlib
import subprocess

from tools import checkpoint_manager as cm


def _git(store, project, *args):
    return subprocess.run(
        ["git", f"--git-dir={store}", *args],
        capture_output=True, text=True, check=False, env=cm._git_env(store, str(project)),
    )


def test_database_files_never_enter_the_object_store(tmp_path, monkeypatch):
    base = tmp_path / "checkpoints"
    project = tmp_path / "project"
    data = project / "data"
    data.mkdir(parents=True)
    (project / "source.py").write_text("print('checkpoint me')\n")
    monkeypatch.setattr(cm, "CHECKPOINT_BASE", base)
    contents = []
    for suffix in (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3"):
        payload = f"private database payload {suffix}\n".encode()
        (data / f"app{suffix}").write_bytes(payload)
        contents.append(payload)

    manager = cm.CheckpointManager(enabled=True)
    assert manager.ensure_checkpoint(str(project), "before edit")
    store = cm._store_path(base)
    ref = cm._ref_name(cm._project_hash(str(project)))
    tree = _git(store, project, "ls-tree", "-r", "--name-only", ref)
    assert tree.returncode == 0, tree.stderr
    assert tree.stdout.splitlines() == ["source.py"]
    # Removing a blob from the index after git add is too late: it still costs disk.
    for payload in contents:
        blob = hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()
        assert _git(store, project, "cat-file", "-e", blob).returncode != 0


def test_upgrading_existing_store_preserves_database_exclusion(tmp_path, monkeypatch):
    base = tmp_path / "checkpoints"
    project = tmp_path / "project"
    project.mkdir()
    (project / "source.py").write_text("version = 1\n")
    monkeypatch.setattr(cm, "CHECKPOINT_BASE", base)
    manager = cm.CheckpointManager(enabled=True)
    assert manager.ensure_checkpoint(str(project), "old installation")
    store = cm._store_path(base)
    # The deployed fork already writes this rule into each store.
    exclude = store / "info" / "exclude"
    exclude.write_text(exclude.read_text() + "\n*.db\n")
    (project / "app.db").write_text("database created since the last checkpoint")
    (project / "source.py").write_text("version = 2\n")
    manager.new_turn()
    assert manager.ensure_checkpoint(str(project), "new installation")
    ref = cm._ref_name(cm._project_hash(str(project)))
    tree = _git(store, project, "ls-tree", "-r", "--name-only", ref)
    assert tree.returncode == 0, tree.stderr
    assert tree.stdout.splitlines() == ["source.py"]

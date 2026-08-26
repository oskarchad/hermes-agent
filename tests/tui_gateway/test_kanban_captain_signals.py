"""Contracts for profile-scoped Captain report persistence."""

from hermes_cli import kanban_db as kb


def test_connect_installs_captain_persistence_schema(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        names = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert {
        "kanban_captain_registry",
        "kanban_captain_inbox",
        "kanban_captain_receivers",
    } <= names

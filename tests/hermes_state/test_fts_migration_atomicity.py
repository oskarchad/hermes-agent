"""Interrupted FTS startup publication must remain retryable (HER-338)."""

import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_schema import _FTS_BASE_TRIGGERS, _FTS_TRIGRAM_TRIGGERS


def _seed(path, count):
    db = SessionDB(path)
    if not db._fts_enabled:
        db.close()
        pytest.skip("SQLite FTS5 unavailable")
    db.create_session("session", source="cli")
    for i in range(count):
        db.append_message("session", "user", f"historicalneedle {i}")
    db.close()


def _assert_complete(path, count, table="messages_fts"):
    db = SessionDB(path)
    try:
        assert db._conn.execute("SELECT count(*) FROM messages").fetchone()[0] == count
        assert db._conn.execute(
            f"SELECT count(*) FROM {table} WHERE {table} MATCH 'historicalneedle'"
        ).fetchone()[0] == count
        db._conn.execute(f"INSERT INTO {table}({table}, rank) VALUES('integrity-check', 1)")
        db._conn.commit()
        db.append_message("session", "user", "subsequentneedle")
        assert db._conn.execute(
            f"SELECT count(*) FROM {table} WHERE {table} MATCH 'subsequentneedle'"
        ).fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.parametrize("count", [0, 2])
def test_interrupted_alignment_retries_without_losing_history(tmp_path, monkeypatch, count):
    path = tmp_path / "state.db"
    _seed(path, count)
    with sqlite3.connect(path) as raw:
        raw.execute("DROP TABLE messages_fts")
        raw.execute(
            "CREATE VIRTUAL TABLE messages_fts USING fts5("
            "content, tool_name, tool_calls, content='messages', content_rowid='id')"
        )
        raw.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        raw.execute("INSERT OR REPLACE INTO state_meta(key,value) VALUES('fts_storage_version','2')")

    ensure = SessionDB._ensure_fts_schema

    def interrupt_after_ddl(self, cursor, table_name, ddl):
        result = ensure(self, cursor, table_name, ddl)
        if table_name == "messages_fts":
            raise RuntimeError("injected alignment interruption")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(SessionDB, "_ensure_fts_schema", interrupt_after_ddl)
        with pytest.raises(RuntimeError, match="injected alignment interruption"):
            SessionDB(path)

    _assert_complete(path, count)


@pytest.mark.parametrize("table,triggers", [
    ("messages_fts", _FTS_BASE_TRIGGERS),
    ("messages_fts_trigram", _FTS_TRIGRAM_TRIGGERS),
])
def test_interrupted_trigger_repair_does_not_publish_unrebuilt_index(
    tmp_path, monkeypatch, table, triggers,
):
    path = tmp_path / "state.db"
    _seed(path, 1)
    with sqlite3.connect(path) as raw:
        if not raw.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            pytest.skip("SQLite trigram unavailable")
        for trigger in triggers:
            raw.execute(f"DROP TRIGGER {trigger}")
        raw.execute(
            "INSERT INTO messages(session_id,role,content,timestamp,active) "
            "VALUES('session','user','historicalneedle gap',0,1)"
        )

    def interrupt_rebuild(*args, **kwargs):
        raise RuntimeError("injected repair interruption")

    with monkeypatch.context() as patch:
        patch.setattr(SessionDB, "_rebuild_fts_indexes", staticmethod(interrupt_rebuild))
        with pytest.raises(RuntimeError, match="injected repair interruption"):
            SessionDB(path)

    _assert_complete(path, 2, table)

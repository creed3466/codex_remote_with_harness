"""SessionStore unit tests."""

from __future__ import annotations

from pathlib import Path

from codex_rc.session_store import SessionStore


def test_register_then_get(tmp_path: Path) -> None:
    s = SessionStore(tmp_path / "sessions.db")
    sess = s.register(channel_id="C1", project_path="/p")
    assert sess.channel_id == "C1"
    assert sess.state == "starting"
    assert sess.sandbox == "workspace-write"
    assert sess.approval == "on-request"
    assert sess.codex_thread_id is None
    got = s.get("C1")
    assert got == sess


def test_register_replaces_existing(tmp_path: Path) -> None:
    s = SessionStore(tmp_path / "sessions.db")
    s.register(channel_id="C1", project_path="/old")
    s.update_thread("C1", "thread-1")
    fresh = s.register(channel_id="C1", project_path="/new")
    assert fresh.project_path == "/new"
    assert fresh.codex_thread_id is None  # cleared
    assert fresh.state == "starting"  # reset


def test_state_and_thread_updates(tmp_path: Path) -> None:
    s = SessionStore(tmp_path / "sessions.db")
    s.register(channel_id="C1", project_path="/p")
    s.update_state("C1", "running")
    s.update_thread("C1", "t-42")
    sess = s.get("C1")
    assert sess and sess.state == "running" and sess.codex_thread_id == "t-42"


def test_list_active_excludes_stopped(tmp_path: Path) -> None:
    s = SessionStore(tmp_path / "sessions.db")
    s.register(channel_id="A", project_path="/a")
    s.register(channel_id="B", project_path="/b")
    s.register(channel_id="C", project_path="/c")
    s.update_state("A", "running")
    s.update_state("B", "stopped")
    s.update_state("C", "error")
    active = {x.channel_id for x in s.list_active()}
    assert active == {"A", "C"}


def test_remove(tmp_path: Path) -> None:
    s = SessionStore(tmp_path / "sessions.db")
    s.register(channel_id="X", project_path="/x")
    s.remove("X")
    assert s.get("X") is None


def test_creates_parent_dir(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "deep" / "memory.json"
    SessionStore(db)
    assert db.parent.is_dir()


def test_persists_across_instances(tmp_path: Path) -> None:
    """A second SessionStore opened on the same path sees prior writes."""
    path = tmp_path / "memory.json"
    a = SessionStore(path)
    a.register(channel_id="C1", project_path="/p")
    a.update_thread("C1", "t-1")
    a.record_thread(
        codex_thread_id="t-1", channel_id="C1", project_path="/p"
    )
    a.touch_thread("t-1", preview_addition="hello", turn_increment=2)

    b = SessionStore(path)
    sess = b.get("C1")
    assert sess and sess.codex_thread_id == "t-1"
    threads = b.list_threads("C1")
    assert len(threads) == 1
    assert threads[0].preview == "hello"
    assert threads[0].turn_count == 2


def test_memory_file_is_human_readable_json(tmp_path: Path) -> None:
    """The point of the JSON backend is that the operator can read it."""
    import json

    path = tmp_path / "memory.json"
    s = SessionStore(path)
    s.register(channel_id="C1", project_path="/p")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert "C1" in data["sessions"]
    assert data["sessions"]["C1"]["project_path"] == "/p"


def test_handoff_record_persists_and_updates_session(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    s = SessionStore(path)
    s.register(channel_id="C1", project_path="/p")
    rec = s.record_handoff(
        handoff_id="handoff-1",
        channel_id="C1",
        project_path="/p",
        sandbox="workspace-write",
        approval="on-request",
        source_codex_thread_id="thread-old",
        body="# Codex Handoff\n\nContinue from here.",
    )

    assert Path(rec.file_path).is_file()
    assert s.read_handoff_body(rec).startswith("# Codex Handoff")
    sess = s.get("C1")
    assert sess and sess.handoff_id == "handoff-1"

    reopened = SessionStore(path)
    got = reopened.latest_handoff("C1")
    assert got and got.source_codex_thread_id == "thread-old"
    assert got.preview == "Codex Handoff"


def test_handoffs_are_pruned_to_latest_ten(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    s = SessionStore(path)
    s.register(channel_id="C1", project_path="/p")
    for i in range(12):
        s.record_handoff(
            handoff_id=f"handoff-{i}",
            channel_id="C1",
            project_path="/p",
            sandbox="workspace-write",
            approval="on-request",
            source_codex_thread_id=f"thread-{i}",
            body=f"# Handoff {i}\n",
        )

    handoffs = s.list_handoffs("C1", limit=20)
    assert len(handoffs) == 10
    ids = {h.handoff_id for h in handoffs}
    assert "handoff-0" not in ids
    assert "handoff-1" not in ids
    assert "handoff-11" in ids
    assert not (tmp_path / "handoffs" / "handoff-0.md").exists()


def test_sessions_are_pruned_to_latest_ten(tmp_path: Path) -> None:
    s = SessionStore(tmp_path / "memory.json")
    for i in range(12):
        s.register(channel_id=f"C{i}", project_path=f"/p{i}")

    sessions = s.list_all()
    assert len(sessions) == 10
    ids = {sess.channel_id for sess in sessions}
    assert "C0" not in ids
    assert "C1" not in ids
    assert "C11" in ids


def test_threads_are_pruned_to_configured_max_and_keep_active(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    s = SessionStore(path, max_threads=0)
    s.register(channel_id="C1", project_path="/p")
    for i in range(5):
        thread_id = f"t-{i}"
        s.record_thread(
            codex_thread_id=thread_id,
            channel_id="C1",
            project_path="/p",
        )
        s._data["threads"][thread_id]["last_used_at"] = float(i)  # noqa: SLF001
    s.update_thread("C1", "t-0")
    s._flush()  # noqa: SLF001

    limited = SessionStore(path, max_threads=3)
    limited.update_state("C1", "running")

    ids = {t.codex_thread_id for t in limited.list_threads("C1", limit=10)}
    assert ids == {"t-0", "t-3", "t-4"}


def test_migrates_from_legacy_sqlite_when_memory_is_empty(tmp_path: Path) -> None:
    """If a sessions.db sits next to a fresh memory.json, its rows are
    pulled in so `/codex continue` keeps working across the migration."""
    import sqlite3

    sqlite_path = tmp_path / "sessions.db"
    with sqlite3.connect(str(sqlite_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                channel_id TEXT PRIMARY KEY, project_path TEXT, sandbox TEXT,
                approval TEXT, state TEXT, codex_thread_id TEXT,
                created_at REAL, updated_at REAL
            );
            CREATE TABLE thread_history (
                codex_thread_id TEXT PRIMARY KEY, channel_id TEXT,
                project_path TEXT, started_at REAL, last_used_at REAL,
                preview TEXT, turn_count INTEGER
            );
            INSERT INTO sessions VALUES
              ('C1', '/p1', 'workspace-write', 'on-request', 'running',
               't-1', 100.0, 200.0);
            INSERT INTO thread_history VALUES
              ('t-1', 'C1', '/p1', 100.0, 200.0, 'hello', 3);
            """
        )

    s = SessionStore(tmp_path / "memory.json")
    sess = s.get("C1")
    assert sess and sess.project_path == "/p1"
    assert sess.codex_thread_id == "t-1"
    threads = s.list_threads("C1")
    assert len(threads) == 1 and threads[0].preview == "hello"
    assert threads[0].turn_count == 3


def test_migration_skipped_when_memory_already_populated(tmp_path: Path) -> None:
    """Once memory.json has content, a later-added sessions.db is ignored
    so the operator can't accidentally clobber live state by leaving an
    old DB file around."""
    import sqlite3

    # Bot 1: clean start, memory.json populated via normal API.
    s1 = SessionStore(tmp_path / "memory.json")
    s1.register(channel_id="C1", project_path="/p")

    # An old sessions.db appears later (e.g. backup restored next to us).
    sqlite_path = tmp_path / "sessions.db"
    with sqlite3.connect(str(sqlite_path)) as conn:
        conn.executescript(
            "CREATE TABLE sessions (channel_id TEXT PRIMARY KEY, project_path TEXT, "
            "sandbox TEXT, approval TEXT, state TEXT, codex_thread_id TEXT, "
            "created_at REAL, updated_at REAL);"
            "INSERT INTO sessions VALUES ('LEGACY', '/old', 'workspace-write', "
            "'on-request', 'running', NULL, 0.0, 0.0);"
        )

    # Bot 2: memory.json is non-empty → migration is skipped.
    s2 = SessionStore(tmp_path / "memory.json")
    assert s2.get("LEGACY") is None
    assert s2.get("C1") is not None

"""Regression tests for thread-safe SQLite access.

The app used to keep one ``sqlite3.Connection`` per store and share it across the
HTTP thread pool. Under parallel load that raised ``sqlite3.InterfaceError``
("bad parameter or other API misuse" / "cannot commit - no transaction is
active") and surfaced as 500s. These tests drive the stores from many threads at
once and assert the results stay consistent and error-free.
"""

import sqlite3
import threading

import pytest

from chatbot.admin_store import AdminStore
from chatbot.sqlite_support import SqliteDatabase, is_write_statement
from chatbot.users import UserStore

SECRET = "test-secret"


def _run_threads(fn, count):
    """Run ``fn(0..count-1)`` on real threads; return whatever they raised."""
    errors = []

    def wrap(n):
        try:
            fn(n)
        except BaseException as exc:  # noqa: BLE001 - surface anything raised
            errors.append(exc)

    threads = [threading.Thread(target=wrap, args=(n,)) for n in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


class TestIsWriteStatement:
    def test_classifies_statements(self):
        assert is_write_statement("INSERT INTO t VALUES (1)")
        assert is_write_statement("  update t set x = 1")
        assert is_write_statement("CREATE TABLE t (id)")
        assert is_write_statement("DELETE FROM t")
        assert not is_write_statement("SELECT * FROM t")
        assert not is_write_statement("PRAGMA table_info(t)")
        assert not is_write_statement("")


class TestSqliteDatabase:
    def test_file_database_uses_wal(self, tmp_path):
        db = SqliteDatabase(str(tmp_path / "w.sqlite3"))
        try:
            assert db.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            db.close()

    def test_concurrent_writes_are_serialized(self, tmp_path):
        db = SqliteDatabase(str(tmp_path / "c.sqlite3"))
        db.connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
        db.connection.commit()

        def worker(n):
            for i in range(25):
                conn = db.connection
                conn.execute("INSERT INTO t (v) VALUES (?)", (n * 1000 + i,))
                conn.commit()

        try:
            assert _run_threads(worker, 40) == []
            assert db.connection.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1000
        finally:
            db.close()

    def test_memory_database_is_shared_across_threads(self):
        db = SqliteDatabase(":memory:")
        db.connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
        db.connection.commit()

        def worker(n):
            for _ in range(15):
                conn = db.connection
                conn.execute("INSERT INTO t (v) VALUES (?)", (n,))
                conn.commit()

        try:
            assert _run_threads(worker, 20) == []
            assert db.connection.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 300
        finally:
            db.close()

    def test_rollback_after_error_keeps_database_usable(self):
        db = SqliteDatabase(":memory:")
        try:
            conn = db.connection
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER UNIQUE)")
            conn.commit()
            conn.execute("INSERT INTO t (v) VALUES (1)")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO t (v) VALUES (1)")
            conn.rollback()
            assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
            conn.execute("INSERT INTO t (v) VALUES (2)")
            conn.commit()
            assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
        finally:
            db.close()


class TestStoreConcurrency:
    def test_user_store_parallel_reads_and_writes(self, tmp_path):
        store = UserStore(db_path=str(tmp_path / "users.sqlite3"), secret=SECRET)
        user = store.create_user("owner@example.com", "password123", "Owner", "finance")

        def worker(n):
            for _ in range(10):
                assert store.get_by_email("owner@example.com") is not None
                store.add_workspace(user["id"], "finance", "member")

        try:
            assert _run_threads(worker, 24) == []
        finally:
            store.close()

    def test_admin_store_parallel_audit_writes(self, tmp_path):
        store = AdminStore(str(tmp_path / "admin.sqlite3"))

        def worker(n):
            for i in range(40):
                store.log_event("system", "test.event", "object", str(i), "detail")

        try:
            assert _run_threads(worker, 16) == []
            assert store.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 640
        finally:
            store.close()

    def test_memory_stores_share_state_across_threads(self):
        store = UserStore(db_path=":memory:", secret=SECRET)
        user = store.create_user("mem@example.com", "password123", "Mem", "finance")

        def worker(n):
            assert store.get_by_email("mem@example.com")["id"] == user["id"]

        try:
            assert _run_threads(worker, 12) == []
        finally:
            store.close()

"""Thread-safe SQLite access shared by the app's stores.

One process owns one SQLite file per store, and the HTTP layer serves requests
from a thread pool. A ``sqlite3.Connection`` must not be used from more than one
thread at a time: doing so raised ``sqlite3.InterfaceError`` ("bad parameter or
other API misuse", "cannot commit - no transaction is active") under load and
turned into 500s.

This module gives each thread its own connection (SQLite's recommended model),
puts the file in WAL mode so readers never block the writer, and runs each write
inside an explicit ``BEGIN IMMEDIATE`` transaction behind a per-database lock.
Writes therefore serialize in-process instead of racing at the file level; reads
stay lock-free. ``:memory:`` databases (used by tests) are shared across threads
through a named in-memory database, since a plain ``:memory:`` connection is
private to one thread.
"""

from __future__ import annotations

import itertools
import os
import re
import sqlite3
import threading

# First keyword of a statement that mutates the database or its schema. These
# are the statements that must open a write transaction.
_WRITE_KEYWORDS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "REPLACE",
    "CREATE",
    "DROP",
    "ALTER",
    "VACUUM",
    "REINDEX",
    "ANALYZE",
)
_WRITE_RE = re.compile(
    r"^\s*(?:" + "|".join(_WRITE_KEYWORDS) + r")\b", re.IGNORECASE
)

_memory_counter = itertools.count(1)


def is_write_statement(sql):
    """True when ``sql`` starts with a statement that changes the database."""
    return bool(_WRITE_RE.match(sql or ""))


class _ConnectionProxy:
    """A ``sqlite3.Connection`` look-alike bound to a single thread.

    Reads run in autocommit; the first write of a logical operation opens a
    ``BEGIN IMMEDIATE`` transaction (taking the database's write lock) and the
    caller's ``commit()`` closes it. The proxy exists so the stores keep writing
    ``self.conn.execute(...)`` / ``self.conn.commit()`` unchanged while gaining
    per-thread connections and explicit write transactions.
    """

    def __init__(self, db):
        # Set before any attribute access so __getattr__ can rely on it.
        self._conn = db._new_connection()
        self._db = db
        self._in_transaction = False

    # -- statement dispatch --------------------------------------------------

    def execute(self, sql, parameters=()):
        self._begin_write(sql)
        try:
            return self._conn.execute(sql, parameters)
        except BaseException:
            self._abort_write(sql)
            raise

    def executemany(self, sql, seq_of_parameters):
        self._begin_write(sql)
        try:
            return self._conn.executemany(sql, seq_of_parameters)
        except BaseException:
            self._abort_write(sql)
            raise

    # -- transaction control -------------------------------------------------

    def commit(self):
        if not self._in_transaction:
            return
        try:
            self._conn.execute("COMMIT")
        except BaseException:
            self._safe_rollback()
            raise
        finally:
            self._release()

    def rollback(self):
        if not self._in_transaction:
            return
        try:
            self._safe_rollback()
        finally:
            self._release()

    def close(self):
        if self._in_transaction:
            try:
                self._safe_rollback()
            finally:
                self._release()
        try:
            self._conn.close()
        finally:
            self._db._forget(self)

    # -- internals -----------------------------------------------------------

    def _begin_write(self, sql):
        if self._in_transaction or not is_write_statement(sql):
            return
        # Guard against a half-finished transaction from a previous statement:
        # trust the connection's own state over our bookkeeping.
        if self._conn.in_transaction:
            self._safe_rollback()
        self._db._write_lock.acquire()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            self._db._write_lock.release()
            raise
        self._in_transaction = True

    def _abort_write(self, sql):
        if self._in_transaction and is_write_statement(sql):
            try:
                self._safe_rollback()
            finally:
                self._release()

    def _safe_rollback(self):
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _release(self):
        self._in_transaction = False
        try:
            self._db._write_lock.release()
        except RuntimeError:
            pass

    # -- attribute passthrough (row_factory, enable_load_extension, ...) -----

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    def __getattr__(self, name):
        conn = self.__dict__.get("_conn")
        if conn is None:
            raise AttributeError(name)
        return getattr(conn, name)


class SqliteDatabase:
    """Owns the connections to one SQLite database.

    ``connection`` returns this thread's connection proxy. ``close`` closes
    every connection the database has handed out (used at shutdown/tests).
    """

    def __init__(
        self,
        db_path,
        *,
        row_factory=None,
        setup=None,
        busy_timeout_ms=15000,
    ):
        self.db_path = db_path
        self._row_factory = row_factory
        self._setup = setup
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._write_lock = threading.RLock()
        self._local = threading.local()
        self._registry_lock = threading.Lock()
        self._proxies: list = []
        self._keeper = None

        if db_path == ":memory:":
            # A plain :memory: database belongs to one connection. A named,
            # shared-cache in-memory database lets every thread's connection see
            # the same data; the keeper below keeps it alive.
            name = f"memdb_{os.getpid()}_{next(_memory_counter)}"
            self._uri = f"file:{name}?mode=memory&cache=shared"
            self._uri_mode = True
            self._is_file = False
            self._keeper = sqlite3.connect(
                self._uri, uri=True, check_same_thread=False
            )
            self._keeper.row_factory = self._row_factory
        else:
            parent = os.path.dirname(os.path.abspath(db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._uri = db_path
            self._uri_mode = False
            self._is_file = True
            self._enable_wal()

    @property
    def connection(self):
        proxy = getattr(self._local, "proxy", None)
        if proxy is None:
            proxy = _ConnectionProxy(self)
            self._local.proxy = proxy
            with self._registry_lock:
                self._proxies.append(proxy)
        return proxy

    def close(self):
        with self._registry_lock:
            proxies = list(self._proxies)
            self._proxies.clear()
        for proxy in proxies:
            try:
                proxy.close()
            except sqlite3.Error:
                pass
        if self._keeper is not None:
            try:
                self._keeper.close()
            except sqlite3.Error:
                pass
            self._keeper = None

    # -- internals -----------------------------------------------------------

    def _new_connection(self):
        conn = sqlite3.connect(
            self._uri,
            timeout=self._busy_timeout_ms / 1000.0,
            isolation_level=None,  # autocommit; transactions are explicit
            check_same_thread=False,  # thread-local, but closed at shutdown
            uri=self._uri_mode,
        )
        if self._row_factory is not None:
            conn.row_factory = self._row_factory
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        if self._is_file:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        if self._setup is not None:
            self._setup(conn)
        return conn

    def _enable_wal(self):
        """Turn on WAL once at startup so it persists in the file header.

        If the filesystem cannot support WAL (some network mounts), the pragma
        leaves the mode unchanged instead of failing; the in-process write lock
        and busy timeout still keep concurrent access correct, just with less
        reader/writer parallelism.
        """
        conn = sqlite3.connect(self._uri, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    def _forget(self, proxy):
        with self._registry_lock:
            try:
                self._proxies.remove(proxy)
            except ValueError:
                pass

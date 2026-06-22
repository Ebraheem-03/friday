"""SQLite (WAL) knowledge-graph: entities, observations, relations + migrations.

Schema
------
- entities(id, name UNIQUE, type, created_at)
- observations(id, entity_id FK, content, created_at)
- relations(id, from_entity FK, to_entity FK, kind, created_at)

Connection model
----------------
A single ``sqlite3.Connection`` is opened in the constructor and reused for
all operations.  Access is protected by a ``threading.Lock`` so the store is
safe to call from multiple threads (e.g., the main async event loop running
``asyncio.to_thread``).  Each public method acquires the lock, executes its
query, and releases immediately — no long-held locks.

WAL mode means readers never block writers and writes never block readers
(within the same process), so concurrent async-to-sync bridging is safe.

Migration strategy
------------------
``PRAGMA user_version`` tracks the schema revision.  ``_migrate()`` is called
once at open time; each numbered step is idempotent (guarded by IF NOT EXISTS
or equivalent).  Adding a new step increments ``_SCHEMA_VERSION`` and appends
to ``_MIGRATION_STEPS``.

Security
--------
All SQL uses parameterised queries (``?`` placeholders).  No string
interpolation is used anywhere in SQL paths.  Observation *content* is never
logged at INFO; only entity names and counts appear in DEBUG logs.

Default DB path: ``~/.local/share/friday/memory.db``.  The path is injectable
for tests (pass ``:memory:`` or a ``tmp_path``-derived path).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema version — bump when adding a migration step below
# ---------------------------------------------------------------------------
_SCHEMA_VERSION: int = 1

# ---------------------------------------------------------------------------
# Migration steps — each entry is applied once (idempotent).
# Index 0 -> user_version 0->1, index 1 -> user_version 1->2, etc.
# ---------------------------------------------------------------------------
_MIGRATION_STEPS: list[str] = [
    # Step 0 -> 1: initial schema
    """
    CREATE TABLE IF NOT EXISTS entities (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT    NOT NULL UNIQUE,
        type       TEXT    NOT NULL DEFAULT 'generic',
        created_at TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS observations (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        content    TEXT    NOT NULL,
        created_at TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS relations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        from_entity INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        to_entity   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        kind        TEXT    NOT NULL,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_entities_name
        ON entities(name);

    CREATE INDEX IF NOT EXISTS idx_observations_entity_id
        ON observations(entity_id);

    CREATE INDEX IF NOT EXISTS idx_relations_from
        ON relations(from_entity);

    CREATE INDEX IF NOT EXISTS idx_relations_to
        ON relations(to_entity);
    """
]


# ---------------------------------------------------------------------------
# Result type for search
# ---------------------------------------------------------------------------

class SearchResult(TypedDict):
    """A single match returned by ``Store.search``."""

    entity_name: str
    entity_type: str
    content: str  # the observation text that matched (or entity name for direct hits)
    match_kind: str  # "entity" | "observation"


# ---------------------------------------------------------------------------
# Default DB path
# ---------------------------------------------------------------------------

def _default_db_path() -> Path:
    """Return ``~/.local/share/friday/memory.db``, creating parent dirs."""
    path = Path.home() / ".local" / "share" / "friday" / "memory.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class Store:
    """Thread-safe synchronous SQLite knowledge-graph store.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite file, or ``':memory:'`` for an in-process
        in-memory database (useful for tests).  If ``None``, defaults to
        ``~/.local/share/friday/memory.db``.

    Example
    -------
    >>> store = Store(db_path=':memory:')
    >>> store.add_entity('Alice', type='person')
    >>> store.add_observation('Alice', 'Prefers dark mode')
    >>> results = store.search('dark mode')
    >>> results[0]['entity_name']
    'Alice'
    >>> store.close()
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        resolved = Path(db_path) if db_path and str(db_path) != ":memory:" else db_path
        if resolved is None:
            resolved = _default_db_path()

        self._db_path = str(resolved)
        self._lock = threading.Lock()

        self._conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,  # we guard with _lock ourselves
            isolation_level=None,     # autocommit; we manage transactions explicitly
        )
        self._conn.row_factory = sqlite3.Row

        # Enable WAL mode and safety pragmas once at open time.
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")  # ms; avoids SQLITE_BUSY spins

        logger.debug("Store opened: path=%r", self._db_path)
        self._migrate()

    # ------------------------------------------------------------------
    # Migration
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        """Apply any pending migration steps idempotently.

        Reads ``PRAGMA user_version``, runs each un-applied step in order,
        and updates the version after each step.  Safe to call on an already
        up-to-date database — it becomes a no-op.
        """
        with self._lock:
            current: int = self._conn.execute("PRAGMA user_version").fetchone()[0]
            logger.debug(
                "Store migration: current_version=%d target_version=%d",
                current,
                _SCHEMA_VERSION,
            )
            while current < _SCHEMA_VERSION:
                step_sql = _MIGRATION_STEPS[current]
                self._conn.executescript(step_sql)
                current += 1
                # executescript commits implicitly; update version inside its own exec.
                self._conn.execute(f"PRAGMA user_version={current}")  # noqa: S608 — safe: int literal only
                logger.debug("Store migration: applied step -> user_version=%d", current)

    # ------------------------------------------------------------------
    # Entity operations
    # ------------------------------------------------------------------

    def add_entity(self, name: str, type: str = "generic") -> int:  # noqa: A002
        """Insert an entity or return the existing one's id.

        Parameters
        ----------
        name:
            Unique entity label (e.g. ``'user_name'``, ``'Alice'``).
        type:
            Semantic category (e.g. ``'person'``, ``'preference'``).
            Defaults to ``'generic'``.

        Returns
        -------
        int
            The ``id`` of the (new or existing) entity row.
        """
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO entities (name, type) VALUES (?, ?)",
                    (name, type),
                )
                row = self._conn.execute(
                    "SELECT id FROM entities WHERE name = ?", (name,)
                ).fetchone()
                self._conn.execute("COMMIT")
                entity_id: int = row["id"]
                logger.debug("add_entity: name=%r id=%d", name, entity_id)
                return entity_id
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_entity(self, name: str) -> sqlite3.Row | None:
        """Return the entity row for *name*, or ``None`` if not found.

        Parameters
        ----------
        name:
            Exact entity name to look up.

        Returns
        -------
        sqlite3.Row | None
            Row with ``id``, ``name``, ``type``, ``created_at``, or ``None``.
        """
        with self._lock:
            return self._conn.execute(
                "SELECT id, name, type, created_at FROM entities WHERE name = ?",
                (name,),
            ).fetchone()

    # ------------------------------------------------------------------
    # Observation operations
    # ------------------------------------------------------------------

    def add_observation(self, entity_name: str, content: str) -> int:
        """Append an observation about *entity_name*.

        Creates the entity (as ``'generic'`` type) if it does not exist yet.

        Parameters
        ----------
        entity_name:
            The entity this observation is about.
        content:
            Free-text observation to store.  **Not logged at INFO** (may
            contain sensitive user data).

        Returns
        -------
        int
            The ``id`` of the new observation row.
        """
        entity_id = self.add_entity(entity_name)
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                cursor = self._conn.execute(
                    "INSERT INTO observations (entity_id, content) VALUES (?, ?)",
                    (entity_id, content),
                )
                self._conn.execute("COMMIT")
                obs_id: int = cursor.lastrowid  # type: ignore[assignment]
                # Log metadata only — never log content at INFO.
                logger.debug(
                    "add_observation: entity=%r obs_id=%d", entity_name, obs_id
                )
                return obs_id
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_observations(self, entity_name: str) -> list[sqlite3.Row]:
        """Return all observations for *entity_name*, oldest first.

        Parameters
        ----------
        entity_name:
            The entity to retrieve observations for.

        Returns
        -------
        list[sqlite3.Row]
            Each row has ``id``, ``entity_id``, ``content``, ``created_at``.
            Empty list if the entity has no observations or does not exist.
        """
        with self._lock:
            return self._conn.execute(
                """
                SELECT o.id, o.entity_id, o.content, o.created_at
                FROM observations o
                JOIN entities e ON e.id = o.entity_id
                WHERE e.name = ?
                ORDER BY o.created_at ASC
                """,
                (entity_name,),
            ).fetchall()

    # ------------------------------------------------------------------
    # Relation operations
    # ------------------------------------------------------------------

    def add_relation(self, a: str, b: str, kind: str) -> int:
        """Create a directed relation ``a -[kind]-> b``.

        Both entities are created (as ``'generic'``) if they do not exist.

        Parameters
        ----------
        a:
            Name of the source entity.
        b:
            Name of the target entity.
        kind:
            Relation label (e.g. ``'knows'``, ``'uses'``, ``'owns'``).

        Returns
        -------
        int
            The ``id`` of the new relation row.
        """
        from_id = self.add_entity(a)
        to_id = self.add_entity(b)
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                cursor = self._conn.execute(
                    "INSERT INTO relations (from_entity, to_entity, kind) VALUES (?, ?, ?)",
                    (from_id, to_id, kind),
                )
                self._conn.execute("COMMIT")
                rel_id: int = cursor.lastrowid  # type: ignore[assignment]
                logger.debug(
                    "add_relation: %r -[%s]-> %r rel_id=%d", a, kind, b, rel_id
                )
                return rel_id
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str) -> list[SearchResult]:
        """Full-text search across entity names and observation content.

        Matches any entity whose ``name`` contains *query* (case-insensitive),
        and any observation whose ``content`` contains *query*
        (case-insensitive).  Uses SQL ``LIKE`` with ``%`` wildcards — no FTS5
        extension required (stays within stdlib sqlite3 with no extra deps).

        Parameters
        ----------
        query:
            The search string.  Wildcards (``%``, ``_``) in *query* are
            treated as literals (escaped before interpolation into LIKE).

        Returns
        -------
        list[SearchResult]
            Ordered by entity name then match kind (entity before observation).
            Each entry has ``entity_name``, ``entity_type``, ``content``,
            and ``match_kind`` (``'entity'`` or ``'observation'``).
        """
        # Escape LIKE wildcards in caller-supplied query so it is treated as a
        # literal substring search, not a pattern.
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"

        results: list[SearchResult] = []

        with self._lock:
            # --- entity name matches ---
            entity_rows = self._conn.execute(
                """
                SELECT name, type
                FROM entities
                WHERE name LIKE ? ESCAPE '\\'
                ORDER BY name
                """,
                (pattern,),
            ).fetchall()
            for row in entity_rows:
                results.append(
                    SearchResult(
                        entity_name=row["name"],
                        entity_type=row["type"],
                        content=row["name"],
                        match_kind="entity",
                    )
                )

            # --- observation content matches ---
            obs_rows = self._conn.execute(
                """
                SELECT e.name AS entity_name, e.type AS entity_type, o.content
                FROM observations o
                JOIN entities e ON e.id = o.entity_id
                WHERE o.content LIKE ? ESCAPE '\\'
                ORDER BY e.name, o.created_at
                """,
                (pattern,),
            ).fetchall()
            for row in obs_rows:
                results.append(
                    SearchResult(
                        entity_name=row["entity_name"],
                        entity_type=row["entity_type"],
                        content=row["content"],
                        match_kind="observation",
                    )
                )

        logger.debug(
            "search: query=%r hits=%d", query, len(results)
        )
        return results

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying SQLite connection.

        Safe to call more than once; subsequent calls are no-ops.
        """
        with self._lock:
            try:
                self._conn.close()
                logger.debug("Store closed: path=%r", self._db_path)
            except Exception:
                pass  # already closed

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

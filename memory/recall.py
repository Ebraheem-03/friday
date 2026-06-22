"""Recall API: write nodes, query by entity/relation, sub-ms local lookup.

Public surface
--------------
``Memory`` is the single class exposed by this module.  It wraps a ``Store``
and provides two layers of API:

1. **Sync recall API** — used by any non-brain code that needs to read/write
   the knowledge graph directly:
   - ``write(entity, content)`` — add an observation about an entity.
   - ``query(entity)`` — return all observations for an entity as a string.
   - ``relate(a, b, kind)`` — record a directed relation between two entities.

2. **Async ``MemoryTool`` adapter** — the two async methods the brain's
   ``MemoryTool`` Protocol requires:
   - ``remember(key, value)`` — maps to ``store.add_observation(key, value)``
     (creates the entity if absent) and returns a short confirmation string.
   - ``recall(query)`` — runs ``store.search(query)`` and formats results as a
     concise, human-readable text summary.

Threading / async model
-----------------------
``sqlite3`` is synchronous.  The ``Memory`` async methods call the sync
``Store`` methods via ``asyncio.to_thread`` so the event loop is never blocked.
This is the conservative, explicit choice: hot-path recall is sub-millisecond
locally but offloading to a thread keeps the async contract clean and avoids
blocking the main loop during any unexpected I/O stall (e.g., WAL checkpoint
flush).  The thread-pool overhead is negligible vs. any downstream TTS or STT
latency.

Protocol compatibility
----------------------
``Memory`` satisfies ``core.brain.MemoryTool`` (a ``@runtime_checkable``
Protocol) because it implements exactly:
    async def remember(self, key: str, value: str) -> str
    async def recall(self, query: str) -> str

Security
--------
Observation *content* is never logged at INFO.  Only entity names, hit counts,
and result counts appear in DEBUG logs.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from memory.store import SearchResult, Store

logger = logging.getLogger(__name__)


class Memory:
    """Synchronous KG store + async MemoryTool adapter for the brain.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file, or ``':memory:'`` for an in-process
        in-memory database (useful for tests).  Passed directly to ``Store``.
        If ``None``, ``Store`` defaults to ``~/.local/share/friday/memory.db``.

    store:
        Optional pre-constructed ``Store`` instance.  Useful in tests where the
        caller wants to control the store directly.  If provided, ``db_path``
        is ignored.

    Example
    -------
    >>> import asyncio
    >>> mem = Memory(db_path=':memory:')
    >>> asyncio.run(mem.remember('user_name', 'Alice'))
    "Remembered 'user_name'."
    >>> asyncio.run(mem.recall('Alice'))
    'user_name: Alice'
    >>> mem.close()
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        store: Store | None = None,
    ) -> None:
        self._store: Store = store if store is not None else Store(db_path=db_path)

    # ------------------------------------------------------------------
    # Synchronous recall / write API (for non-brain callers)
    # ------------------------------------------------------------------

    def write(self, entity: str, content: str) -> None:
        """Append *content* as an observation about *entity*.

        Creates the entity automatically if it does not exist.

        Parameters
        ----------
        entity:
            Entity name (used as the knowledge-graph node label).
        content:
            Free-text observation.  Not logged at INFO.
        """
        self._store.add_observation(entity, content)
        logger.debug("Memory.write: entity=%r", entity)

    def query(self, entity: str) -> str:
        """Return all observations for *entity* as a formatted string.

        Parameters
        ----------
        entity:
            Exact entity name to retrieve observations for.

        Returns
        -------
        str
            Newline-separated observation content strings, or a friendly
            "no memories" message when the entity has no observations.
        """
        rows = self._store.get_observations(entity)
        if not rows:
            logger.debug("Memory.query: entity=%r no observations", entity)
            return f"Nothing remembered about '{entity}'."
        lines = [row["content"] for row in rows]
        logger.debug("Memory.query: entity=%r count=%d", entity, len(lines))
        return "\n".join(lines)

    def relate(self, a: str, b: str, kind: str) -> None:
        """Record a directed relation ``a -[kind]-> b`` in the graph.

        Parameters
        ----------
        a:
            Source entity name.
        b:
            Target entity name.
        kind:
            Relation label (e.g. ``'knows'``, ``'uses'``).
        """
        self._store.add_relation(a, b, kind)
        logger.debug("Memory.relate: %r -[%s]-> %r", a, kind, b)

    # ------------------------------------------------------------------
    # Async MemoryTool adapter (brain Protocol)
    # ------------------------------------------------------------------

    async def remember(self, key: str, value: str) -> str:
        """Store *value* as an observation about entity *key*.

        Satisfies ``core.brain.MemoryTool.remember``.

        The entity is created automatically if it does not exist.  The sync
        ``Store.add_observation`` call is offloaded via ``asyncio.to_thread``
        to avoid blocking the event loop.

        Parameters
        ----------
        key:
            Short entity label (e.g. ``'user_name'``, ``'preferred_editor'``).
        value:
            The content to store.

        Returns
        -------
        str
            Short confirmation string (e.g. ``"Remembered 'user_name'."``).
        """
        await asyncio.to_thread(self._store.add_observation, key, value)
        logger.debug("Memory.remember: key=%r", key)
        return f"Remembered '{key}'."

    async def recall(self, query: str) -> str:
        """Search the knowledge graph for *query* and return a text summary.

        Satisfies ``core.brain.MemoryTool.recall``.

        Runs ``Store.search`` via ``asyncio.to_thread``.  Formats results as a
        concise human-readable string the brain can include verbatim in its
        response.

        Parameters
        ----------
        query:
            Natural-language search string.

        Returns
        -------
        str
            Formatted summary of matching observations, or a friendly
            "nothing remembered" message when no matches are found.
        """
        results: list[SearchResult] = await asyncio.to_thread(
            self._store.search, query
        )
        if not results:
            logger.debug("Memory.recall: query=%r no results", query)
            return f"Nothing remembered about '{query}'."

        # Format: group observation matches by entity, dedup entity-name hits.
        lines: list[str] = []
        seen_entities: set[str] = set()
        for hit in results:
            if hit["match_kind"] == "observation":
                lines.append(f"{hit['entity_name']}: {hit['content']}")
                seen_entities.add(hit["entity_name"])
            elif hit["entity_name"] not in seen_entities:
                # Entity-name match with no observation content yet — mention it.
                lines.append(f"{hit['entity_name']} (entity)")
                seen_entities.add(hit["entity_name"])

        logger.debug("Memory.recall: query=%r result_lines=%d", query, len(lines))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying store.  Safe to call more than once."""
        self._store.close()

    def __enter__(self) -> "Memory":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

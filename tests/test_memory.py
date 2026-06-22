"""Tests for memory/store.py and memory/recall.py.

Coverage
--------
- Schema creation: all three tables + indexes present after open.
- WAL pragma: journal_mode=WAL confirmed after open.
- add_entity / get_entity round-trip.
- add_observation + get_observations round-trip.
- add_relation round-trip.
- search: entity name match, observation content match, no match.
- search: LIKE wildcard escaping (% in query treated as literal).
- Migration idempotency: open the same DB twice; schema is unchanged, no error.
- Async adapter: remember → recall returns the stored value.
- Persistence across "restart": write with one Store, close, open a fresh
  Store on the same path, search returns the data.
- Memory Protocol compatibility: Memory satisfies MemoryTool at runtime.
- Memory.query / Memory.write / Memory.relate sync API.
- Memory.recall returns friendly message when empty.
- Memory.remember returns confirmation string.
- context-manager (Store / Memory __enter__ / __exit__).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memory.store import Store
from memory.recall import Memory
from core.brain import MemoryTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_store(tmp_path: Path, name: str = "test.db") -> Store:
    """Open a Store backed by a temp-file DB (not :memory: so we can reopen)."""
    return Store(db_path=tmp_path / name)


# ---------------------------------------------------------------------------
# Store: schema creation
# ---------------------------------------------------------------------------

class TestStoreSchema:
    def test_tables_created(self, tmp_path: Path) -> None:
        """All three tables are present after opening a fresh DB."""
        with _open_store(tmp_path) as store:
            conn = store._conn
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {"entities", "observations", "relations"}.issubset(tables)

    def test_indexes_created(self, tmp_path: Path) -> None:
        """Expected indexes exist."""
        with _open_store(tmp_path) as store:
            conn = store._conn
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
        assert "idx_entities_name" in indexes
        assert "idx_observations_entity_id" in indexes
        assert "idx_relations_from" in indexes
        assert "idx_relations_to" in indexes

    def test_wal_mode(self, tmp_path: Path) -> None:
        """WAL journal mode is active after open."""
        with _open_store(tmp_path) as store:
            mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_foreign_keys_on(self, tmp_path: Path) -> None:
        """Foreign key enforcement is enabled."""
        with _open_store(tmp_path) as store:
            fk = store._conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1

    def test_schema_version_set(self, tmp_path: Path) -> None:
        """user_version is set to _SCHEMA_VERSION after open."""
        from memory.store import _SCHEMA_VERSION
        with _open_store(tmp_path) as store:
            ver = store._conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver == _SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Store: entity operations
# ---------------------------------------------------------------------------

class TestStoreEntity:
    def test_add_entity_returns_id(self, tmp_path: Path) -> None:
        with _open_store(tmp_path) as store:
            eid = store.add_entity("Alice", type="person")
        assert isinstance(eid, int)
        assert eid > 0

    def test_add_entity_idempotent(self, tmp_path: Path) -> None:
        """Calling add_entity twice for the same name returns the same id."""
        with _open_store(tmp_path) as store:
            id1 = store.add_entity("Bob")
            id2 = store.add_entity("Bob")
        assert id1 == id2

    def test_get_entity_returns_row(self, tmp_path: Path) -> None:
        with _open_store(tmp_path) as store:
            store.add_entity("Carol", type="person")
            row = store.get_entity("Carol")
        assert row is not None
        assert row["name"] == "Carol"
        assert row["type"] == "person"

    def test_get_entity_missing_returns_none(self, tmp_path: Path) -> None:
        with _open_store(tmp_path) as store:
            row = store.get_entity("Nonexistent")
        assert row is None

    def test_add_entity_default_type(self, tmp_path: Path) -> None:
        with _open_store(tmp_path) as store:
            store.add_entity("thing")
            row = store.get_entity("thing")
        assert row["type"] == "generic"


# ---------------------------------------------------------------------------
# Store: observation operations
# ---------------------------------------------------------------------------

class TestStoreObservation:
    def test_add_observation_returns_id(self, tmp_path: Path) -> None:
        with _open_store(tmp_path) as store:
            oid = store.add_observation("Dave", "Likes coffee")
        assert isinstance(oid, int)
        assert oid > 0

    def test_add_observation_creates_entity(self, tmp_path: Path) -> None:
        """add_observation creates the entity if it does not exist."""
        with _open_store(tmp_path) as store:
            store.add_observation("Eve", "New entity")
            row = store.get_entity("Eve")
        assert row is not None

    def test_get_observations_round_trip(self, tmp_path: Path) -> None:
        with _open_store(tmp_path) as store:
            store.add_observation("Frank", "First note")
            store.add_observation("Frank", "Second note")
            rows = store.get_observations("Frank")
        assert len(rows) == 2
        contents = [r["content"] for r in rows]
        assert "First note" in contents
        assert "Second note" in contents

    def test_get_observations_empty(self, tmp_path: Path) -> None:
        """Entity exists but has no observations — returns empty list."""
        with _open_store(tmp_path) as store:
            store.add_entity("Grace")
            rows = store.get_observations("Grace")
        assert rows == []

    def test_get_observations_missing_entity(self, tmp_path: Path) -> None:
        """Non-existent entity returns empty list (no error)."""
        with _open_store(tmp_path) as store:
            rows = store.get_observations("Nobody")
        assert rows == []


# ---------------------------------------------------------------------------
# Store: relation operations
# ---------------------------------------------------------------------------

class TestStoreRelation:
    def test_add_relation_returns_id(self, tmp_path: Path) -> None:
        with _open_store(tmp_path) as store:
            rid = store.add_relation("Alice", "Bob", "knows")
        assert isinstance(rid, int)
        assert rid > 0

    def test_add_relation_creates_entities(self, tmp_path: Path) -> None:
        """add_relation creates both entities if they don't exist."""
        with _open_store(tmp_path) as store:
            store.add_relation("Hen", "Ivy", "related_to")
            assert store.get_entity("Hen") is not None
            assert store.get_entity("Ivy") is not None

    def test_add_relation_stored_in_db(self, tmp_path: Path) -> None:
        with _open_store(tmp_path) as store:
            store.add_relation("Jack", "Kara", "uses")
            rows = store._conn.execute(
                """
                SELECT r.kind, ef.name AS from_name, et.name AS to_name
                FROM relations r
                JOIN entities ef ON ef.id = r.from_entity
                JOIN entities et ON et.id = r.to_entity
                WHERE ef.name = 'Jack'
                """
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["kind"] == "uses"
        assert rows[0]["to_name"] == "Kara"


# ---------------------------------------------------------------------------
# Store: search
# ---------------------------------------------------------------------------

class TestStoreSearch:
    def test_search_entity_name_match(self, tmp_path: Path) -> None:
        with _open_store(tmp_path) as store:
            store.add_entity("quantum_computer", type="topic")
            results = store.search("quantum")
        assert len(results) >= 1
        names = [r["entity_name"] for r in results]
        assert "quantum_computer" in names

    def test_search_observation_content_match(self, tmp_path: Path) -> None:
        with _open_store(tmp_path) as store:
            store.add_observation("Leo", "Prefers dark mode editor")
            results = store.search("dark mode")
        assert len(results) >= 1
        obs_hits = [r for r in results if r["match_kind"] == "observation"]
        assert any("dark mode" in h["content"] for h in obs_hits)

    def test_search_no_match(self, tmp_path: Path) -> None:
        with _open_store(tmp_path) as store:
            store.add_entity("Mars")
            results = store.search("zzz_not_found_xyz")
        assert results == []

    def test_search_case_insensitive(self, tmp_path: Path) -> None:
        with _open_store(tmp_path) as store:
            store.add_observation("Mia", "Uses Python for data science")
            results = store.search("python")
        assert any(r["entity_name"] == "Mia" for r in results)

    def test_search_wildcard_escaping(self, tmp_path: Path) -> None:
        """A literal '%' in the query must not match all rows."""
        with _open_store(tmp_path) as store:
            store.add_entity("irrelevant_entity")
            store.add_observation("percent_entity", "discount: 10%")
            results_percent = store.search("10%")
            results_irrelevant = store.search("irrelevant%")
        # '10%' should match the observation but NOT 'irrelevant_entity'
        percent_names = [r["entity_name"] for r in results_percent]
        assert "percent_entity" in percent_names
        assert "irrelevant_entity" not in percent_names
        # 'irrelevant%' should match nothing (the % is escaped to a literal)
        irr_names = [r["entity_name"] for r in results_irrelevant]
        assert "irrelevant_entity" not in irr_names

    def test_search_result_fields(self, tmp_path: Path) -> None:
        """Each SearchResult has all expected keys."""
        with _open_store(tmp_path) as store:
            store.add_observation("Ned", "Like hiking")
            results = store.search("hiking")
        assert len(results) >= 1
        r = results[0]
        assert "entity_name" in r
        assert "entity_type" in r
        assert "content" in r
        assert "match_kind" in r


# ---------------------------------------------------------------------------
# Store: migration idempotency
# ---------------------------------------------------------------------------

class TestStoreMigration:
    def test_open_twice_is_noop(self, tmp_path: Path) -> None:
        """Opening the same DB file a second time does not raise or corrupt."""
        db = tmp_path / "migrate_test.db"
        with Store(db_path=db) as s1:
            s1.add_entity("Olga")
        # Second open — migration should detect version == _SCHEMA_VERSION and skip.
        with Store(db_path=db) as s2:
            row = s2.get_entity("Olga")
        assert row is not None
        assert row["name"] == "Olga"

    def test_schema_version_stable_after_reopen(self, tmp_path: Path) -> None:
        from memory.store import _SCHEMA_VERSION
        db = tmp_path / "ver_test.db"
        with Store(db_path=db) as s:
            v1 = s._conn.execute("PRAGMA user_version").fetchone()[0]
        with Store(db_path=db) as s:
            v2 = s._conn.execute("PRAGMA user_version").fetchone()[0]
        assert v1 == v2 == _SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Store: persistence across "restart"
# ---------------------------------------------------------------------------

class TestStorePersistence:
    def test_data_survives_close_and_reopen(self, tmp_path: Path) -> None:
        """Write with one Store instance, close, reopen fresh, data still there."""
        db = tmp_path / "persist.db"
        # Write
        s1 = Store(db_path=db)
        s1.add_observation("Pete", "Prefers Vim")
        s1.close()

        # Open a brand-new Store instance on the same file
        s2 = Store(db_path=db)
        results = s2.search("Vim")
        s2.close()

        assert len(results) >= 1
        assert any(r["entity_name"] == "Pete" for r in results)

    def test_multiple_observations_survive_reopen(self, tmp_path: Path) -> None:
        db = tmp_path / "multi.db"
        s1 = Store(db_path=db)
        s1.add_observation("Quinn", "Likes jazz")
        s1.add_observation("Quinn", "Dislikes mornings")
        s1.close()

        s2 = Store(db_path=db)
        rows = s2.get_observations("Quinn")
        s2.close()

        contents = [r["content"] for r in rows]
        assert "Likes jazz" in contents
        assert "Dislikes mornings" in contents


# ---------------------------------------------------------------------------
# Memory: sync API
# ---------------------------------------------------------------------------

class TestMemorySync:
    def test_write_and_query_round_trip(self, tmp_path: Path) -> None:
        with Memory(db_path=tmp_path / "sync.db") as mem:
            mem.write("user_name", "Alice")
            result = mem.query("user_name")
        assert "Alice" in result

    def test_query_empty_returns_friendly_message(self, tmp_path: Path) -> None:
        with Memory(db_path=tmp_path / "empty.db") as mem:
            result = mem.query("nonexistent_key")
        assert "Nothing remembered" in result

    def test_relate_creates_entities(self, tmp_path: Path) -> None:
        with Memory(db_path=tmp_path / "rel.db") as mem:
            mem.relate("FRIDAY", "Python", "written_in")
            row = mem._store.get_entity("FRIDAY")
        assert row is not None

    def test_write_multiple_observations(self, tmp_path: Path) -> None:
        with Memory(db_path=tmp_path / "multi.db") as mem:
            mem.write("hobby", "reading")
            mem.write("hobby", "cycling")
            result = mem.query("hobby")
        assert "reading" in result
        assert "cycling" in result


# ---------------------------------------------------------------------------
# Memory: async adapter (MemoryTool Protocol)
# ---------------------------------------------------------------------------

class TestMemoryAsync:
    @pytest.mark.asyncio
    async def test_remember_returns_confirmation(self, tmp_path: Path) -> None:
        mem = Memory(db_path=tmp_path / "async.db")
        result = await mem.remember("user_name", "Bob")
        mem.close()
        assert isinstance(result, str)
        assert len(result) > 0
        assert "user_name" in result

    @pytest.mark.asyncio
    async def test_recall_returns_stored_value(self, tmp_path: Path) -> None:
        """remember → recall round-trip returns the stored value."""
        mem = Memory(db_path=tmp_path / "rt.db")
        await mem.remember("favourite_colour", "indigo")
        result = await mem.recall("indigo")
        mem.close()
        assert "indigo" in result

    @pytest.mark.asyncio
    async def test_recall_empty_returns_friendly_message(self, tmp_path: Path) -> None:
        mem = Memory(db_path=tmp_path / "empty_async.db")
        result = await mem.recall("totally_unknown_xyz")
        mem.close()
        assert "Nothing remembered" in result

    @pytest.mark.asyncio
    async def test_recall_matches_entity_name(self, tmp_path: Path) -> None:
        """recall finds a stored entity by key name."""
        mem = Memory(db_path=tmp_path / "entity_async.db")
        await mem.remember("project_name", "FRIDAY")
        result = await mem.recall("project_name")
        mem.close()
        assert "project_name" in result

    @pytest.mark.asyncio
    async def test_remember_multiple_then_recall(self, tmp_path: Path) -> None:
        mem = Memory(db_path=tmp_path / "multi_async.db")
        await mem.remember("pet", "has a cat named Luna")
        await mem.remember("pet", "also has a dog named Oreo")
        result = await mem.recall("Luna")
        mem.close()
        assert "Luna" in result

    @pytest.mark.asyncio
    async def test_async_persistence_across_restart(self, tmp_path: Path) -> None:
        """Write with one Memory, close, reopen fresh, async recall returns data."""
        db = tmp_path / "async_persist.db"

        mem1 = Memory(db_path=db)
        await mem1.remember("api_key_hint", "starts with sk-")
        mem1.close()

        mem2 = Memory(db_path=db)
        result = await mem2.recall("starts with sk-")
        mem2.close()

        assert "starts with sk-" in result


# ---------------------------------------------------------------------------
# Memory: Protocol compatibility
# ---------------------------------------------------------------------------

class TestMemoryProtocol:
    def test_memory_satisfies_memory_tool_protocol(self, tmp_path: Path) -> None:
        """Memory is isinstance-compatible with MemoryTool at runtime."""
        mem = Memory(db_path=tmp_path / "proto.db")
        try:
            assert isinstance(mem, MemoryTool)
        finally:
            mem.close()

    def test_memory_has_remember_method(self, tmp_path: Path) -> None:
        mem = Memory(db_path=tmp_path / "method.db")
        mem.close()
        assert callable(getattr(mem, "remember", None))
        assert callable(getattr(mem, "recall", None))


# ---------------------------------------------------------------------------
# Store: context manager
# ---------------------------------------------------------------------------

class TestStoreContextManager:
    def test_context_manager_closes_connection(self, tmp_path: Path) -> None:
        db = tmp_path / "ctx.db"
        with Store(db_path=db) as store:
            store.add_entity("Vera")
        # After __exit__, re-opening must work (proves prior conn was closed cleanly)
        with Store(db_path=db) as store2:
            row = store2.get_entity("Vera")
        assert row is not None


# ---------------------------------------------------------------------------
# Memory: context manager
# ---------------------------------------------------------------------------

class TestMemoryContextManager:
    def test_context_manager_works(self, tmp_path: Path) -> None:
        db = tmp_path / "memctx.db"
        with Memory(db_path=db) as mem:
            mem.write("ctx_key", "ctx_value")
        # Reopen and verify
        with Memory(db_path=db) as mem2:
            result = mem2.query("ctx_key")
        assert "ctx_value" in result

import hashlib
import os
import re
import sqlite3
from pathlib import Path
from typing import Callable

ENTITY_KEY_VERSION = "v2"


def normalize_identifier(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def entity_key(entity_type: str, source_value: str) -> str:
    normalized = normalize_identifier(source_value)
    return hashlib.sha256(
        f"{ENTITY_KEY_VERSION}\0{entity_type}\0{normalized}".encode("utf-8")
    ).hexdigest()


class EntityGraph:
    """Persistent pseudonym registry with hashed source keys and relationship edges."""

    def __init__(self, path: str | Path | None = None):
        configured_path = path or os.getenv("ENTITY_GRAPH_PATH") or "data/processed/entity_graph.sqlite"
        self.path = Path(configured_path)
        if not self.path.is_absolute():
            self.path = Path(__file__).resolve().parent.parent / self.path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS entities (
                    entity_id TEXT PRIMARY KEY,
                    entity_type TEXT NOT NULL,
                    replacement TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS relationships (
                    source_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    PRIMARY KEY (source_id, relation, target_id)
                )"""
            )

    def resolve(
        self,
        entity_type: str,
        source_value: str,
        make_replacement: Callable[[], str],
    ) -> str:
        identifier = entity_key(entity_type, source_value)
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT replacement FROM entities WHERE entity_id = ?", (identifier,)
            ).fetchone()
            if row is not None:
                return str(row[0])
            replacement = make_replacement()
            connection.execute(
                "INSERT OR IGNORE INTO entities VALUES (?, ?, ?)",
                (identifier, entity_type, replacement),
            )
        return replacement

    def relate(
        self,
        source_type: str,
        source_value: str,
        relation: str,
        target_type: str,
        target_value: str,
    ) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO relationships VALUES (?, ?, ?)",
                (
                    entity_key(source_type, source_value),
                    relation,
                    entity_key(target_type, target_value),
                ),
            )

    def update_replacement(
        self, entity_type: str, source_value: str, replacement: str
    ) -> None:
        self.update_replacements(
            [{"entity_type": entity_type, "source_value": source_value, "replacement": replacement}]
        )

    def update_replacements(self, updates: list[dict[str, str]]) -> None:
        with sqlite3.connect(self.path) as connection:
            for update in updates:
                entity_type = update["entity_type"]
                source_value = update["source_value"]
                replacement = update["replacement"]
                identifier = entity_key(entity_type, source_value)
                cursor = connection.execute(
                    "UPDATE entities SET replacement = ? WHERE entity_id = ?",
                    (replacement, identifier),
                )
                if cursor.rowcount == 0:
                    connection.execute(
                        "INSERT INTO entities VALUES (?, ?, ?)",
                        (identifier, entity_type, replacement),
                    )

    def synthetic_email_domains(self) -> set[str]:
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                "SELECT replacement FROM entities WHERE entity_type = 'synthetic_email_v3'"
            ).fetchall()
        return {
            str(row[0]).rsplit("@", 1)[1].casefold()
            for row in rows
            if "@" in str(row[0])
        }

    def relationship_count(self) -> int:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute("SELECT COUNT(*) FROM relationships").fetchone()
        return int(row[0]) if row else 0
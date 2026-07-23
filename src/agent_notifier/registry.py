"""Persistent external-session to agent-thread mappings."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SessionMapping:
    adapter: str
    project: str
    external_key: str
    thread_id: str
    cwd: str


class SessionRegistry:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_mappings (
                adapter TEXT NOT NULL,
                project TEXT NOT NULL,
                external_key TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                cwd TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (adapter, project, external_key)
            )
            """
        )
        self._conn.commit()

    def bind(
        self,
        adapter: str,
        project: str,
        external_key: str,
        thread_id: str,
        cwd: str,
    ) -> SessionMapping:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO session_mappings
                    (adapter, project, external_key, thread_id, cwd)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(adapter, project, external_key) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    cwd = excluded.cwd,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (adapter, project, external_key, thread_id, cwd),
            )
            row = self._conn.execute(
                """
                SELECT adapter, project, external_key, thread_id, cwd
                FROM session_mappings
                WHERE adapter = ? AND project = ? AND external_key = ?
                """,
                (adapter, project, external_key),
            ).fetchone()
        return SessionMapping(*row)

    def get(self, adapter: str, project: str, external_key: str) -> SessionMapping | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT adapter, project, external_key, thread_id, cwd
                FROM session_mappings
                WHERE adapter = ? AND project = ? AND external_key = ?
                """,
                (adapter, project, external_key),
            ).fetchone()
        return SessionMapping(*row) if row else None

    def find_by_thread(self, thread_id: str) -> SessionMapping | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT adapter, project, external_key, thread_id, cwd
                FROM session_mappings
                WHERE thread_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
        return SessionMapping(*row) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

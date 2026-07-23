"""Persistent active-session routes and notification subscriptions."""

from __future__ import annotations

import json
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

    @property
    def session_label(self) -> str:
        return Path(self.cwd).name or self.project.removesuffix("-codex")

    @property
    def short_thread_id(self) -> str:
        return self.thread_id[:8]


@dataclass(frozen=True)
class ActiveAgent:
    project: str
    external_key: str
    provider: str


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
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_subscriptions (
                adapter TEXT NOT NULL,
                project TEXT NOT NULL,
                external_key TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                cwd TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (adapter, project, external_key, thread_id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_agents (
                project TEXT NOT NULL,
                external_key TEXT NOT NULL,
                provider TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (project, external_key)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thread_token_usage (
                thread_id TEXT PRIMARY KEY,
                token_usage_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Existing installations used session_mappings for both purposes.
        # Preserve every current active route as its first notification subscription.
        self._conn.execute(
            """
            INSERT OR IGNORE INTO notification_subscriptions
                (adapter, project, external_key, thread_id, cwd, updated_at)
            SELECT adapter, project, external_key, thread_id, cwd, updated_at
            FROM session_mappings
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
            self._subscribe_locked(
                adapter, project, external_key, thread_id, cwd
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

    def subscribe(
        self,
        adapter: str,
        project: str,
        external_key: str,
        thread_id: str,
        cwd: str,
    ) -> SessionMapping:
        """Subscribe a thread to notifications without changing the active route."""
        with self._lock, self._conn:
            self._subscribe_locked(
                adapter, project, external_key, thread_id, cwd
            )
        return SessionMapping(adapter, project, external_key, thread_id, cwd)

    def _subscribe_locked(
        self,
        adapter: str,
        project: str,
        external_key: str,
        thread_id: str,
        cwd: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO notification_subscriptions
                (adapter, project, external_key, thread_id, cwd)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(adapter, project, external_key, thread_id) DO UPDATE SET
                cwd = excluded.cwd,
                updated_at = CURRENT_TIMESTAMP
            """,
            (adapter, project, external_key, thread_id, cwd),
        )

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

    def set_active_agent(
        self, project: str, external_key: str, provider: str
    ) -> ActiveAgent:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO active_agents (project, external_key, provider)
                VALUES (?, ?, ?)
                ON CONFLICT(project, external_key) DO UPDATE SET
                    provider = excluded.provider,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (project, external_key, provider),
            )
        return ActiveAgent(project, external_key, provider)

    def get_active_agent(
        self, project: str, external_key: str
    ) -> ActiveAgent | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT project, external_key, provider
                FROM active_agents
                WHERE project = ? AND external_key = ?
                """,
                (project, external_key),
            ).fetchone()
        return ActiveAgent(*row) if row else None

    def update_thread_token_usage(
        self, thread_id: str, token_usage: dict
    ) -> None:
        payload = json.dumps(token_usage, ensure_ascii=False, sort_keys=True)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO thread_token_usage (thread_id, token_usage_json)
                VALUES (?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    token_usage_json = excluded.token_usage_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (thread_id, payload),
            )

    def get_thread_token_usage(self, thread_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT token_usage_json
                FROM thread_token_usage
                WHERE thread_id = ?
                """,
                (thread_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def find_by_thread(self, thread_id: str) -> SessionMapping | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT adapter, project, external_key, thread_id, cwd
                FROM notification_subscriptions
                WHERE thread_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
        return SessionMapping(*row) if row else None

    def list_subscriptions(
        self,
        adapter: str,
        project: str | None = None,
        external_key: str | None = None,
    ) -> list[SessionMapping]:
        clauses = ["adapter = ?"]
        values: list[str] = [adapter]
        if project is not None:
            clauses.append("project = ?")
            values.append(project)
        if external_key is not None:
            clauses.append("external_key = ?")
            values.append(external_key)
        query = f"""
            SELECT adapter, project, external_key, thread_id, cwd
            FROM notification_subscriptions
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC
        """
        with self._lock:
            rows = self._conn.execute(query, values).fetchall()
        return [SessionMapping(*row) for row in rows]

    def list_subscriptions_by_thread(
        self, thread_id: str, project: str | None = None
    ) -> list[SessionMapping]:
        clauses = ["thread_id = ?"]
        values: list[str] = [thread_id]
        if project is not None:
            clauses.append("project = ?")
            values.append(project)
        query = f"""
            SELECT adapter, project, external_key, thread_id, cwd
            FROM notification_subscriptions
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC
        """
        with self._lock:
            rows = self._conn.execute(query, values).fetchall()
        return [SessionMapping(*row) for row in rows]

    def list_routes(
        self, adapter: str, project: str | None = None
    ) -> list[SessionMapping]:
        with self._lock:
            if project is None:
                rows = self._conn.execute(
                    """
                    SELECT adapter, project, external_key, thread_id, cwd
                    FROM session_mappings
                    WHERE adapter = ?
                    ORDER BY updated_at DESC
                    """,
                    (adapter,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT adapter, project, external_key, thread_id, cwd
                    FROM session_mappings
                    WHERE adapter = ? AND project = ?
                    ORDER BY updated_at DESC
                    """,
                    (adapter, project),
                ).fetchall()
        return [SessionMapping(*row) for row in rows]

    def prune_missing_threads(
        self,
        adapter: str,
        valid_thread_ids: set[str],
        project: str | None = None,
        external_key: str | None = None,
    ) -> tuple[int, int]:
        clauses = ["adapter = ?"]
        values: list[str] = [adapter]
        if project is not None:
            clauses.append("project = ?")
            values.append(project)
        if external_key is not None:
            clauses.append("external_key = ?")
            values.append(external_key)
        where = " AND ".join(clauses)

        with self._lock, self._conn:
            subscriptions = self._conn.execute(
                f"""
                SELECT adapter, project, external_key, thread_id, updated_at
                FROM notification_subscriptions
                WHERE {where}
                """,
                values,
            ).fetchall()
            mappings = self._conn.execute(
                f"""
                SELECT adapter, project, external_key, thread_id, updated_at
                FROM session_mappings
                WHERE {where}
                """,
                values,
            ).fetchall()

            def is_stale(row) -> bool:
                return row[3] not in valid_thread_ids

            stale_subscriptions = [
                row[:4] for row in subscriptions if is_stale(row)
            ]
            stale_mappings = [
                row[:4] for row in mappings if is_stale(row)
            ]
            self._conn.executemany(
                """
                DELETE FROM notification_subscriptions
                WHERE adapter = ? AND project = ? AND external_key = ?
                  AND thread_id = ?
                """,
                stale_subscriptions,
            )
            self._conn.executemany(
                """
                DELETE FROM session_mappings
                WHERE adapter = ? AND project = ? AND external_key = ?
                  AND thread_id = ?
                """,
                stale_mappings,
            )
        return len(stale_subscriptions), len(stale_mappings)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

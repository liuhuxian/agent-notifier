"""Persistent first-responder-wins approval arbitration."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path


class ApprovalAlreadyResolved(RuntimeError):
    pass


@dataclass(frozen=True)
class ApprovalResolution:
    approval_id: str
    decision: str
    resolved_by: str


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    thread_id: str
    feishu_message_id: str | None
    payload: dict
    origin: str | None = None
    decision: str | None = None


class ApprovalStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                decision TEXT,
                resolved_by TEXT,
                resolved_at TEXT,
                origin TEXT
            )
            """
        )
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(approvals)")
        }
        if "feishu_message_id" not in columns:
            self._conn.execute(
                "ALTER TABLE approvals ADD COLUMN feishu_message_id TEXT"
            )
        if "origin" not in columns:
            self._conn.execute("ALTER TABLE approvals ADD COLUMN origin TEXT")
        self._conn.commit()

    def register(
        self,
        approval_id: str,
        thread_id: str,
        kind: str,
        payload: dict,
        origin: str | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO approvals
                    (approval_id, thread_id, kind, payload_json, origin)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    thread_id,
                    kind,
                    json.dumps(payload, sort_keys=True),
                    origin,
                ),
            )

    def expire_non_terminal(self) -> list[ApprovalRecord]:
        """Expire notifier-managed approvals while preserving terminal ones."""
        with self._lock, self._conn:
            rows = self._conn.execute(
                """
                SELECT approval_id, thread_id, feishu_message_id,
                       payload_json, origin
                FROM approvals
                WHERE decision IS NULL
                  AND origin IS NOT NULL
                  AND origin != 'terminal'
                """
            ).fetchall()
            if rows:
                self._conn.execute(
                    """
                    UPDATE approvals
                    SET decision = 'expired',
                        resolved_by = 'service_restart',
                        resolved_at = CURRENT_TIMESTAMP
                    WHERE decision IS NULL
                      AND origin IS NOT NULL
                      AND origin != 'terminal'
                    """
                )
        return [
            ApprovalRecord(
                approval_id,
                thread_id,
                message_id,
                json.loads(payload_json),
                origin,
                "expired",
            )
            for approval_id, thread_id, message_id, payload_json, origin in rows
        ]

    def resolve(self, approval_id: str, decision: str, resolved_by: str) -> ApprovalResolution:
        if decision not in {"allow", "deny"}:
            raise ValueError(f"invalid approval decision: {decision}")
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE approvals
                SET decision = ?, resolved_by = ?, resolved_at = CURRENT_TIMESTAMP
                WHERE approval_id = ? AND decision IS NULL
                """,
                (decision, resolved_by, approval_id),
            )
            if cursor.rowcount != 1:
                raise ApprovalAlreadyResolved(approval_id)
        return ApprovalResolution(approval_id, decision, resolved_by)

    def get_resolution(self, approval_id: str) -> ApprovalResolution | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT approval_id, decision, resolved_by
                FROM approvals
                WHERE approval_id = ? AND decision IS NOT NULL
                """,
                (approval_id,),
            ).fetchone()
        return ApprovalResolution(*row) if row else None

    def set_feishu_message_id(
        self, approval_id: str, feishu_message_id: str
    ) -> None:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE approvals
                SET feishu_message_id = ?
                WHERE approval_id = ?
                """,
                (feishu_message_id, approval_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(approval_id)

    def find_by_prefix(self, approval_id: str) -> ApprovalRecord | None:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT approval_id, thread_id, feishu_message_id, payload_json
                       , origin, decision
                FROM approvals
                WHERE approval_id = ?
                   OR approval_id LIKE ?
                ORDER BY rowid DESC
                """,
                (approval_id, f"agent-notifier-approval:{approval_id}%"),
            ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise ValueError(f"ambiguous approval id: {approval_id}")
        approval_id, thread_id, message_id, payload_json, origin, decision = rows[0]
        return ApprovalRecord(
            approval_id,
            thread_id,
            message_id,
            json.loads(payload_json),
            origin,
            decision,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

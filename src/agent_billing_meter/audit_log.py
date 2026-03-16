"""SQLite audit log for debit operations."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from agent_billing_meter.models import DebitResult

DEFAULT_DB = Path.home() / ".agent-billing-meter.db"


class AuditLog:
    """Persistent SQLite log of all debit results."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = str(db_path or DEFAULT_DB)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS debits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_user_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    amount_debited INTEGER NOT NULL,
                    success INTEGER NOT NULL,
                    balance_before INTEGER,
                    balance_after INTEGER,
                    error TEXT,
                    metadata TEXT,
                    timestamp REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_user ON debits(app_user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON debits(timestamp)")
            conn.commit()

    def store(self, result: DebitResult, metadata: dict[str, object] | None = None) -> int:
        """Persist a DebitResult. Returns the row id."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO debits
                    (app_user_id, operation, amount_debited, success,
                     balance_before, balance_after, error, metadata, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.app_user_id,
                    result.operation,
                    result.amount_debited,
                    int(result.success),
                    result.balance_before,
                    result.balance_after,
                    result.error,
                    json.dumps(metadata) if metadata else None,
                    result.timestamp,
                ),
            )
            conn.commit()
            return cursor.lastrowid or 0

    def query(
        self,
        app_user_id: str | None = None,
        operation: str | None = None,
        since: float | None = None,
        success_only: bool = False,
        limit: int = 100,
    ) -> list[DebitResult]:
        """Return matching debit results, newest first."""
        clauses: list[str] = []
        params: list[object] = []

        if app_user_id:
            clauses.append("app_user_id = ?")
            params.append(app_user_id)
        if operation:
            clauses.append("operation = ?")
            params.append(operation)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)
        if success_only:
            clauses.append("success = 1")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM debits {where} ORDER BY timestamp DESC LIMIT ?", params
            ).fetchall()

        return [self._row_to_result(r) for r in rows]

    def total_debited(
        self,
        app_user_id: str | None = None,
        operation: str | None = None,
    ) -> int:
        """Sum of amount_debited for successful debits matching filters."""
        clauses = ["success = 1"]
        params: list[object] = []

        if app_user_id:
            clauses.append("app_user_id = ?")
            params.append(app_user_id)
        if operation:
            clauses.append("operation = ?")
            params.append(operation)

        where = "WHERE " + " AND ".join(clauses)

        with self._connect() as conn:
            result = conn.execute(
                f"SELECT COALESCE(SUM(amount_debited), 0) FROM debits {where}", params
            ).fetchone()
        return int(result[0]) if result else 0

    def unique_users(self) -> list[str]:
        """List all distinct app_user_ids in the log."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT app_user_id FROM debits ORDER BY app_user_id"
            ).fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def _row_to_result(row: sqlite3.Row) -> DebitResult:
        return DebitResult(
            success=bool(row["success"]),
            app_user_id=row["app_user_id"],
            operation=row["operation"],
            amount_debited=row["amount_debited"],
            timestamp=row["timestamp"],
            balance_before=row["balance_before"],
            balance_after=row["balance_after"],
            error=row["error"],
        )

    def since_epoch(self, days: int = 1) -> float:
        """Helper: timestamp for N days ago."""
        return time.time() - days * 86400

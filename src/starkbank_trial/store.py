import sqlite3
import time
import uuid
from pathlib import Path


class Store:
    LEASE_SECONDS = 120

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS invoices (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    invoice_id TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims (
                    invoice_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    amount INTEGER,
                    stark_id TEXT,
                    created_at TEXT NOT NULL,
                    lease_until INTEGER,
                    lease_token TEXT
                );
                CREATE TABLE IF NOT EXISTS invoice_requests (
                    request_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    invoice_id TEXT,
                    created_at TEXT NOT NULL,
                    lease_until INTEGER,
                    lease_token TEXT
                );
                """
            )
            self._add_column_if_missing(db, "claims", "lease_until", "INTEGER")
            self._add_column_if_missing(db, "claims", "lease_token", "TEXT")

    @staticmethod
    def _add_column_if_missing(db, table, column, definition):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _db(self):
        return sqlite3.connect(self.path, timeout=30)

    def save_invoice(self, invoice_id, created_at):
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO invoices VALUES(?, ?)",
                (invoice_id, created_at),
            )

    def save_event(self, event_id, invoice_id, received_at):
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO events VALUES(?, ?, ?)",
                (event_id, invoice_id, received_at),
            )

    def claim(self, invoice_id, created_at):
        now = int(time.time())
        lease_token = str(uuid.uuid4())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status, lease_until FROM claims WHERE invoice_id = ?",
                (invoice_id,),
            ).fetchone()
            if row and row[0] == "completed":
                return {"claimed": False, "status": "completed"}
            if row and row[1] and row[1] > now:
                return {"claimed": False, "status": row[0]}
            db.execute(
                """
                INSERT INTO claims(invoice_id, status, created_at, lease_until, lease_token)
                VALUES(?, 'processing', ?, ?, ?)
                ON CONFLICT(invoice_id) DO UPDATE SET
                    status = 'processing',
                    created_at = excluded.created_at,
                    lease_until = excluded.lease_until,
                    lease_token = excluded.lease_token
                """,
                (invoice_id, created_at, now + self.LEASE_SECONDS, lease_token),
            )
        return {
            "claimed": True,
            "status": "processing",
            "lease_token": lease_token,
        }

    def mark_retryable(self, invoice_id, lease_token):
        with self._db() as db:
            cursor = db.execute(
                """
                UPDATE claims
                SET status = 'retryable', lease_until = ?
                WHERE invoice_id = ? AND lease_token = ?
                """,
                (int(time.time()) + self.LEASE_SECONDS, invoice_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("transfer lease is no longer owned")

    def complete(self, invoice_id, amount, stark_id, lease_token):
        with self._db() as db:
            cursor = db.execute(
                """
                UPDATE claims
                SET status = 'completed', amount = ?, stark_id = ?, lease_until = 0
                WHERE invoice_id = ? AND lease_token = ?
                """,
                (amount, stark_id, invoice_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("transfer lease is no longer owned")

    def claim_invoice_creation(self, request_key, created_at):
        now = int(time.time())
        lease_token = str(uuid.uuid4())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status, invoice_id, lease_until FROM invoice_requests "
                "WHERE request_key = ?",
                (request_key,),
            ).fetchone()
            if row and row[0] == "completed":
                return {
                    "claimed": False,
                    "status": "completed",
                    "invoice_id": row[1],
                }
            if row and row[2] and row[2] > now:
                return {"claimed": False, "status": row[0]}
            db.execute(
                """
                INSERT INTO invoice_requests(request_key, status, created_at, lease_until, lease_token)
                VALUES(?, 'processing', ?, ?, ?)
                ON CONFLICT(request_key) DO UPDATE SET
                    status = 'processing',
                    created_at = excluded.created_at,
                    lease_until = excluded.lease_until,
                    lease_token = excluded.lease_token
                """,
                (request_key, created_at, now + self.LEASE_SECONDS, lease_token),
            )
        return {
            "claimed": True,
            "status": "processing",
            "lease_token": lease_token,
        }

    def complete_invoice_creation(self, request_key, invoice_id, lease_token):
        with self._db() as db:
            cursor = db.execute(
                """
                UPDATE invoice_requests
                SET status = 'completed', invoice_id = ?, lease_until = 0
                WHERE request_key = ? AND lease_token = ?
                """,
                (invoice_id, request_key, lease_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("invoice creation lease is no longer owned")

import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS invoices(id TEXT PRIMARY KEY,created_at TEXT NOT NULL); CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,invoice_id TEXT NOT NULL,received_at TEXT NOT NULL); CREATE TABLE IF NOT EXISTS claims(invoice_id TEXT PRIMARY KEY,status TEXT NOT NULL,amount INTEGER,stark_id TEXT,created_at TEXT NOT NULL);"
            )

    def _db(self):
        return sqlite3.connect(self.path, timeout=30)

    def save_invoice(self, id, created):
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO invoices VALUES(?,?)", (id, created))

    def save_event(self, id, invoice, received):
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO events VALUES(?,?,?)", (id, invoice, received)
            )

    def claim(self, invoice, created):
        with self._db() as db:
            return (
                db.execute(
                    "INSERT OR IGNORE INTO claims(invoice_id,status,created_at) VALUES(?,'processing',?)",
                    (invoice, created),
                ).rowcount
                == 1
            )

    def release(self, invoice):
        with self._db() as db:
            db.execute(
                "DELETE FROM claims WHERE invoice_id=? AND status='processing'",
                (invoice,),
            )

    def complete(self, invoice, amount, stark_id):
        with self._db() as db:
            db.execute(
                "UPDATE claims SET status='completed',amount=?,stark_id=? WHERE invoice_id=?",
                (amount, stark_id, invoice),
            )

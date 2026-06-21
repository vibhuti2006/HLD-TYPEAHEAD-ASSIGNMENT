"""
store.py — SQLite primary data store (the durable source of truth).

Why SQLite: it is a single local file, needs zero setup, and gives us real SQL
so we can honestly count database reads and writes for the performance report.

Table schema:
    queries(
        query         TEXT PRIMARY KEY,   -- the search string (lowercased)
        count         INTEGER,            -- all-time popularity (from the AOL log)
        recent_count  REAL                -- time-decayed recent activity (for trending)
    )

The in-memory trie is built FROM this table at startup. On a batch flush we
write the aggregated counts back here so data survives a restart.
"""

import csv
import os
import sqlite3
import threading

from app.config import CSV_PATH, DB_PATH
from app.metrics import metrics


class Store:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        # check_same_thread=False: FastAPI runs sync endpoints in a thread pool,
        # so the connection is touched from several threads. A single SQLite
        # connection is NOT safe for concurrent use, so we guard every DB call
        # with this lock — only one thread touches the connection at a time.
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")  # better concurrent read/write
        self._create_table()

    def _create_table(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS queries (
                query        TEXT PRIMARY KEY,
                count        INTEGER NOT NULL,
                recent_count REAL    NOT NULL DEFAULT 0
            )
            """
        )
        self.conn.commit()

    def is_empty(self):
        cur = self.conn.execute("SELECT COUNT(*) FROM queries")
        metrics.add_db_reads(1)
        return cur.fetchone()[0] == 0

    def load_csv(self, csv_path=CSV_PATH):
        """Bulk-load the cleaned (query,count) CSV produced by ingest.py.

        Done in ONE transaction so 200k rows load fast and count as one write.
        """
        if not os.path.exists(csv_path):
            raise SystemExit(f"{csv_path} not found. Run: python3 ingest.py")
        rows = []
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader)  # skip header "query,count"
            for query, count in reader:
                rows.append((query, int(count)))
        self.conn.executemany(
            "INSERT OR REPLACE INTO queries (query, count, recent_count) VALUES (?, ?, 0)",
            rows,
        )
        self.conn.commit()
        metrics.add_db_writes(1)  # one bulk transaction
        return len(rows)

    def load_all(self):
        """Read every (query, count, recent_count) row to build the in-memory index."""
        cur = self.conn.execute("SELECT query, count, recent_count FROM queries")
        metrics.add_db_reads(1)
        return cur.fetchall()

    def batch_upsert(self, counter):
        """Apply a batch of aggregated search increments in ONE transaction.

        `counter` is {query: how_many_times_searched_in_this_batch}.
        - existing query  -> count += n,  recent_count += n
        - new query       -> inserted with count = n, recent_count = n

        Returns the number of distinct queries written (for the metrics report).
        """
        if not counter:
            return 0
        rows = [(q, n, float(n)) for q, n in counter.items()]
        with self._lock:
            self.conn.executemany(
                """
                INSERT INTO queries (query, count, recent_count)
                VALUES (?, ?, ?)
                ON CONFLICT(query) DO UPDATE SET
                    count        = count + excluded.count,
                    recent_count = recent_count + excluded.recent_count
                """,
                rows,
            )
            self.conn.commit()
        metrics.add_db_writes(1)  # the whole batch is ONE write statement
        return len(rows)

    def decay_recent(self, factor):
        """Multiply every query's recent_count by `factor` in ONE statement.

        This is how recent activity fades over time so a brief spike does not
        rank highly forever.
        """
        with self._lock:
            self.conn.execute(
                "UPDATE queries SET recent_count = recent_count * ?", (factor,)
            )
            self.conn.commit()
        metrics.add_db_writes(1)

    def close(self):
        self.conn.close()

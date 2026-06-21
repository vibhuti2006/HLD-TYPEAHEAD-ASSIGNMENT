"""
batch_writer.py — buffer search submissions and flush them in batches.

The requirement: do NOT write to the DB synchronously on every search.

How it works (straight from the class notes' "batching" idea):
  - POST /search just appends the query to an in-memory buffer and returns
    immediately. No DB write on the request path.
  - A background loop flushes the buffer when EITHER:
        the buffer reaches BATCH_SIZE,  OR  FLUSH_INTERVAL_SECONDS have passed.
  - On flush we AGGREGATE duplicates (Counter), so 50 searches for "iphone"
    become a single  count += 50  instead of 50 separate writes.
  - The whole aggregated batch is written in ONE SQL transaction.

Effect on the metrics report: searches >> db_writes  (the write reduction).

Failure trade-off (we DISCUSS this, we don't add durability code):
  The buffer lives only in memory. If the app crashes before a flush, the
  buffered (unflushed) searches are LOST — at most BATCH_SIZE or one interval's
  worth. This is the classic batching trade-off: lower write load and latency,
  at the cost of a small window of possible data loss. A production system would
  first append each search to a write-ahead log (WAL) and replay it on restart;
  for this assignment that durability is described in the README, not coded.
"""

import asyncio
import threading
from collections import Counter

from app.config import BATCH_SIZE, FLUSH_INTERVAL_SECONDS
from app.metrics import metrics


class BatchWriter:
    def __init__(self, store, trie, cache):
        self.store = store
        self.trie = trie
        self.cache = cache
        self._buffer = []                 # list of raw query strings
        self._lock = threading.Lock()     # guards the buffer (request thread vs flush)
        self._task = None
        self._running = False

    def submit(self, query):
        """Called by POST /search. Cheap: just append and count the search."""
        with self._lock:
            self._buffer.append(query)
            size = len(self._buffer)
        metrics.record_search()
        # If we already hit the batch size, flush now instead of waiting.
        if size >= BATCH_SIZE:
            self.flush()

    def flush(self):
        """Aggregate the buffer and write it to the store + trie in one batch.

        Returns the number of distinct queries written (0 if nothing buffered).
        """
        with self._lock:
            if not self._buffer:
                return 0
            batch = self._buffer
            self._buffer = []             # swap out the buffer under the lock

        # Aggregate duplicates: {query: times_searched_in_this_batch}
        counter = Counter(batch)

        # 1) durable write to the primary store (ONE transaction)
        written = self.store.batch_upsert(counter)

        # 2) update the in-memory trie so suggestions reflect the new counts
        for query, delta in counter.items():
            self.trie.bump(query, delta)

        # 3) invalidate cached suggestions for every prefix of every changed
        #    query, so the next /suggest recomputes fresh results from the trie.
        for query in counter:
            self._invalidate_prefixes(query)

        return written

    def _invalidate_prefixes(self, query):
        """A changed query affects the suggestion list of all of its prefixes
        (e.g. "iphone" affects "i", "ip", "iph", ...). Invalidate them in both
        ranking modes so basic and trending stay correct."""
        for i in range(1, len(query) + 1):
            prefix = query[:i]
            self.cache.invalidate(f"basic:{prefix}")
            self.cache.invalidate(f"trending:{prefix}")

    # ---- background loop ---------------------------------------------------
    async def _run(self):
        self._running = True
        while self._running:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            self.flush()

    def start(self):
        """Launch the periodic flush loop on the running event loop."""
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        """Flush whatever is left and stop the loop (clean shutdown)."""
        self._running = False
        if self._task:
            self._task.cancel()
        self.flush()

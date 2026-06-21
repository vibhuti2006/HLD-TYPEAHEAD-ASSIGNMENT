"""
metrics.py — counters and latency tracking for the performance report.

The assignment's non-functional section asks us to report:
  - p95 latency of /suggest
  - cache hit rate
  - database read/write counts
  - evidence that batch writes reduce DB writes

This module is the single place that records all of that. It is deliberately
tiny and dependency-free so it is easy to explain.
"""

from collections import deque

from app.config import LATENCY_SAMPLE_SIZE


class Metrics:
    def __init__(self):
        # /suggest traffic
        self.suggest_calls = 0
        self.cache_hits = 0
        self.cache_misses = 0
        # /search traffic and the DB write reduction story
        self.searches = 0          # how many POST /search the user made
        self.db_writes = 0         # how many DB write statements we actually ran
        self.db_reads = 0          # how many DB read statements we ran
        # rolling window of recent /suggest latencies (milliseconds)
        self.latencies_ms = deque(maxlen=LATENCY_SAMPLE_SIZE)

    # ---- recording helpers (called from the rest of the app) -------------
    def record_suggest(self, latency_ms, hit):
        self.suggest_calls += 1
        self.latencies_ms.append(latency_ms)
        if hit:
            self.cache_hits += 1
        else:
            self.cache_misses += 1

    def record_search(self):
        self.searches += 1

    def add_db_writes(self, n):
        self.db_writes += n

    def add_db_reads(self, n):
        self.db_reads += n

    # ---- derived numbers (called by /stats) -------------------------------
    def _percentile(self, pct):
        if not self.latencies_ms:
            return 0.0
        data = sorted(self.latencies_ms)
        # nearest-rank percentile: simple and easy to defend
        idx = min(len(data) - 1, int(round((pct / 100.0) * len(data) + 0.5)) - 1)
        return round(data[idx], 3)

    def snapshot(self):
        total_lookups = self.cache_hits + self.cache_misses
        hit_rate = (self.cache_hits / total_lookups) if total_lookups else 0.0
        # write reduction: searches the user made vs DB writes we actually did
        write_reduction = (1 - self.db_writes / self.searches) if self.searches else 0.0
        return {
            "suggest_calls": self.suggest_calls,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_hit_rate": round(hit_rate, 4),
            "latency_ms_p50": self._percentile(50),
            "latency_ms_p95": self._percentile(95),
            "searches": self.searches,
            "db_writes": self.db_writes,
            "db_reads": self.db_reads,
            "write_reduction_ratio": round(write_reduction, 4),
        }


# a single shared instance used across the app
metrics = Metrics()

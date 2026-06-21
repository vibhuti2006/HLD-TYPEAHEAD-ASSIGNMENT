"""
config.py — every tunable knob in one place.

Keeping all constants here (instead of scattered "magic numbers") means in the
viva you can point to ONE file and explain every parameter and its trade-off.
"""

# ---- Dataset / store ------------------------------------------------------
CSV_PATH = "data/queries.csv"     # output of ingest.py: header "query,count"
DB_PATH = "data/typeahead.db"     # SQLite primary store (durable source of truth)

# ---- Suggestions ----------------------------------------------------------
TOP_K = 10                        # requirement: return at most 10 suggestions
NEW_QUERY_INITIAL_COUNT = 1       # count given to a brand-new searched query

# ---- Distributed cache + consistent hashing -------------------------------
CACHE_NODES = 3                   # number of LOGICAL cache nodes
VIRTUAL_NODES = 150               # virtual nodes per real node on the hash ring
                                  # (more vnodes => smoother, more even key spread)
CACHE_TTL_SECONDS = 60            # per-entry expiry so stale suggestions don't live forever

# ---- Batch writes ---------------------------------------------------------
BATCH_SIZE = 50                   # flush when the buffer reaches this many searches
FLUSH_INTERVAL_SECONDS = 2.0      # ...or at least this often, whichever comes first

# ---- Recency / trending ---------------------------------------------------
# Enhanced ranking score = count + RECENCY_BOOST * recent_count
# recent_count is decayed by DECAY_FACTOR every DECAY_INTERVAL seconds, so a
# short-lived spike fades instead of ranking high forever.
RECENCY_BOOST = 500               # weight of recent activity vs all-time popularity
DECAY_FACTOR = 0.9                # "lose 10% of recent activity each interval" (from class notes)
DECAY_INTERVAL_SECONDS = 30       # how often the global decay job runs

# ---- Metrics --------------------------------------------------------------
LATENCY_SAMPLE_SIZE = 5000        # how many recent /suggest latencies we keep for p50/p95

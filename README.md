# Search Typeahead System

A search typeahead (autocomplete) system: it suggests popular search queries as
you type, records submitted searches, ranks suggestions by popularity **or** by
recency, serves reads from a **distributed cache routed with consistent hashing**,
and reduces database load with **batched writes**.

Built for HLD101 Assignment SST-2028.

---

## 1. Quick start (run locally)

```bash
# 1. (one time) build the dataset from the raw AOL search log
python3 ingest.py                 # -> data/queries.csv  (200,000 rows: query,count)

# 2. install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. run the app (loads the CSV into SQLite + builds the trie at startup)
.venv/bin/uvicorn app.main:app --port 8099

# 4. open the UI
open http://localhost:8099
```

First start loads 200k queries into SQLite and builds the in-memory trie
(~5 seconds). Later starts reuse `data/typeahead.db`.

---

## 2. Dataset

- **Source:** AOL user search query log (`data/user-ct-test-collection-02.txt`),
  a real log of `~3.6 million` search events.
- **Derived counts:** the raw log has no count column, so `ingest.py` aggregates
  `GROUP BY query -> COUNT(*)`. **The count of a query is how many times real
  users actually searched it** — a genuine popularity score. (The assignment
  explicitly allows deriving counts by aggregation.)
- **Cleaning:** lowercased + trimmed; dropped empty queries, AOL's `-`
  placeholder, and single characters.
- **Content filtering:** the AOL log is real user data and contains a lot of
  adult/explicit queries. `app/content_filter.py` drops them at ingestion
  (~102k explicit search events removed), so they never reach `queries.csv`, the
  database, or the suggestions. Terms are matched as word stems with a word
  boundary (`\bsex` matches "sex"/"sexy" but **not** "essex"/"sussex"), which
  avoids false positives. To extend the list, edit `_BLOCKED_STEMS` and re-run
  `python3 ingest.py`.
- **Size:** top **200,000** unique queries kept (well over the 100k minimum;
  there are ~1.24M unique queries available — tune with `python3 ingest.py --top N`).

Expected `data/queries.csv` format:

```
query,count
google,32396
yahoo,13344
ebay,12949
```

---

## 3. Architecture

```
   Browser (static/)
   search box ──debounced──▶ GET /suggest ─▶ DistributedCache ──hit──▶ top-10
       │                                          │ miss
       │                                          ▼
       │                                      Trie index ── ranked by ──▶ scorer
       │                                       (built from SQLite)        (basic | trending)
       │
       └── POST /search ─▶ BatchWriter buffer ──flush (size OR time)──▶ SQLite (durable)
                                                                          + Trie update
                                                                          + cache invalidate
   Background: decay job fades recent_count every 30s  (so spikes don't rank forever)
```

**Read path** (`/suggest`): cache → on miss, the trie → fill cache.
**Write path** (`/search`): buffer → periodic batch flush → SQLite + trie + cache invalidation.

| Layer | File | Responsibility |
|-------|------|----------------|
| Primary store | `app/store.py` | SQLite — durable source of truth, real read/write counts |
| Prefix index | `app/index_trie.py` | trie: find all queries starting with a prefix |
| Ranking | `app/ranking.py` | basic (count) vs trending (count + recency) scorers |
| Distributed cache | `app/cache.py` | N logical nodes + consistent hash ring + TTL |
| Batch writer | `app/batch_writer.py` | buffer searches, aggregate, flush in one transaction |
| Metrics | `app/metrics.py` | latency p50/p95, cache hit rate, DB read/write counts |
| API + wiring | `app/main.py` | endpoints, startup load, background jobs |
| Frontend | `static/` | search box, dropdown, trending, keyboard nav, states |
| Tunables | `app/config.py` | every parameter in one place |

---

## 4. API documentation

### `GET /suggest?q=<prefix>&mode=<basic|trending>`
Up to 10 suggestions that **start with** the prefix, ranked by `mode`
(default `basic`). Handles empty/missing/mixed-case/no-match input gracefully.

```bash
curl "localhost:8099/suggest?q=goo"
# {"prefix":"goo","mode":"basic","suggestions":[{"query":"google","count":32396}, ...],"source":"trie"}
```
`source` is `cache` on a hit, `trie` on a miss.

### `POST /search`   body `{"query": "..."}`
Records the search (into the batch buffer) and returns the dummy response.
New queries are created on flush; existing queries have their count incremented.
```bash
curl -X POST localhost:8099/search -H "Content-Type: application/json" -d '{"query":"iphone"}'
# {"message":"Searched"}
```

### `GET /cache/debug?prefix=<prefix>&mode=<basic|trending>`
Shows which cache node owns the prefix key, whether it is a hit/miss right now,
and per-node key counts — the live evidence of consistent hashing.
```bash
curl "localhost:8099/cache/debug?prefix=goo"
# {"key":"basic:goo","owner_node":"cache-node-2","status":"hit","ring_position":539318957,"nodes":{...}}
```

### `GET /trending`
Top queries by recent (decayed) activity — powers the UI Trending section.

### `GET /stats`
Performance numbers: `cache_hit_rate`, `latency_ms_p50/p95`, `searches`,
`db_writes`, `db_reads`, `write_reduction_ratio`.

---

## 5. Design choices & trade-offs (the viva answers)

### Why a trie for suggestions?
Every suggestion must **start with** the typed prefix. A trie walks the prefix in
`O(len(prefix))` and every query in the subtree below that node is a match. This
is exactly the structure from the course notes (each node = children + isTerminal
+ count). The alternative — a SQL `LIKE 'goo%'` scan + sort on every keystroke —
is `O(matches·log)` per call and slow for short prefixes.

### Why a cache in front of the trie, and what does it store?
The course notes point out that, effectively, *we are just caching the top-K
results for each prefix*. So the cache stores `prefix -> top-10 suggestions`. A
cache hit is `O(1)` and never touches the trie. The trie is the **authoritative
fallback**: it can produce correct top-K for **any** prefix, including ones that
were never cached or whose cache entry expired.

### Why consistent hashing (not `hash(key) % N`)?
With `hash(key) % N`, changing `N` (adding/removing a cache node) changes the
result for **almost every key**, so the entire cache misses at once — a
catastrophic cold start. With a hash ring, adding/removing a node only remaps the
keys in **one arc** of the ring — about `1/N` of keys. **Virtual nodes** (150 per
real node) place each node at many points around the ring so load stays even
(verified: 14 keys → 5/5/4 across 3 nodes).

### Cache freshness — expiry and invalidation
- **TTL (60s):** every entry expires, so stale suggestions can't live forever.
- **Invalidation:** when a batch flush changes a query's count, we delete the
  cache entries for **all of that query's prefixes** (in both modes), so the next
  `/suggest` recomputes fresh results from the trie.

### Trending — how recency works and why a spike doesn't win forever
- Each query has a `recent_count`. A submitted search adds to it.
- Trending score = `count + RECENCY_BOOST · recent_count`, so recent activity is
  pushed up while all-time `count` still provides a baseline and breaks ties.
- A background job multiplies every `recent_count` by `0.9` every 30s
  (the notes' "decay 10%"). A query that spiked once and then goes quiet stops
  getting increments, so its `recent_count` **decays back toward 0** and it
  falls back to its plain popularity rank. **Only sustained recent activity
  stays on top.** Same `/suggest` endpoint serves both modes via `?mode=`.

### Batch writes — how and why
- `POST /search` only appends the query to an in-memory buffer and returns; it
  does **no** DB write.
- A background loop flushes when the buffer hits `BATCH_SIZE (50)` **or** every
  `FLUSH_INTERVAL (2s)`, whichever comes first.
- On flush we **aggregate duplicates** (`Counter`) so 50 searches for `iphone`
  become a single `count += 50`, and the whole batch is **one SQL transaction**.
- **Measured:** 101 searches → **5 DB writes** → `write_reduction_ratio ≈ 0.95`
  (95% fewer writes).

### Batch writes — failure trade-off (asked in the rubric)
The buffer is in memory. **If the app crashes before a flush, the buffered
(unflushed) searches are lost** — at most one batch / one interval's worth. This
is the fundamental batching trade-off: lower write load + faster responses, at
the cost of a small window of possible loss. A production system would first
append each search to a **write-ahead log (WAL)** and replay it on restart; we
describe that here rather than implementing it (per the assignment's scope).

### Alternative we did not use: sampling
The notes mention sampling (process ~0.1% of searches, ignore the rest) as
another write-reduction technique. We chose **batching** because it keeps counts
**exact** (better for a small demo dataset), whereas sampling only approximates
counts. Sampling scales further at the cost of accuracy.

---

## 6. Performance report

Run the app, exercise it (or use the load snippet below), then `curl localhost:8099/stats`.

Representative numbers from a local run:

| Metric | Value | Meaning |
|--------|-------|---------|
| `latency_ms_p95` | < 1 ms | `/suggest` is fast (cache hit O(1); miss = trie walk) |
| `cache_hit_rate` | rises toward 1.0 with repeated prefixes | cache effectiveness |
| `searches` vs `db_writes` | 101 vs 5 | batching cut DB writes ~95% |
| consistent hashing | 14 keys → 5/5/4 over 3 nodes | even key distribution |

Generate load:
```bash
for i in $(seq 1 60); do curl -s -X POST localhost:8099/search \
  -H "Content-Type: application/json" -d '{"query":"banana"}' >/dev/null; done
curl localhost:8099/stats
```

---

## 7. Project layout

```
ingest.py              # raw AOL log  ->  data/queries.csv  (aggregate + top-N)
data/queries.csv       # cleaned dataset (query,count)
requirements.txt
app/
  config.py            # all tunables
  store.py             # SQLite primary store
  index_trie.py        # trie prefix index
  ranking.py           # basic + trending scorers
  cache.py             # CacheNode, ConsistentHashRing, DistributedCache
  batch_writer.py      # buffer + periodic flush
  metrics.py           # latency / hit-rate / read-write counters
  main.py              # FastAPI app, routes, startup, background jobs
static/                # index.html, app.js, styles.css
docs/architecture.md   # architecture write-up
```

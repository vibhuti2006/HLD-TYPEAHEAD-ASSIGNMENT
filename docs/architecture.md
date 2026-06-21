# Architecture

## Component diagram

```
                         ┌───────────────────────────────────────────────────────────┐
                         │                      FastAPI app (app/main.py)             │
  ┌──────────────┐       │                                                            │
  │   Browser     │      │   GET /suggest?q=&mode=                                     │
  │  static/      │──────┼────────────────▶ ┌──────────────────────┐                  │
  │               │      │                  │   DistributedCache     │  (app/cache.py) │
  │ search box    │◀─────┼── top-10 ────────│  3 logical nodes       │                 │
  │ dropdown      │      │      ▲ hit        │  consistent hash ring  │                 │
  │ trending      │      │      │            │  TTL 60s + invalidate  │                 │
  │ mode toggle   │      │      │ miss        └──────────┬───────────┘                  │
  │               │      │      │                        │ lookup(prefix, k, scorer)   │
  │               │      │      │            ┌───────────▼───────────┐                  │
  │               │      │      └────────────│   Trie prefix index    │ (app/index_trie)│
  │               │      │  fill cache       │  built from SQLite     │                 │
  │               │      │                   └───────────▲───────────┘                  │
  │               │      │                                │ build at startup            │
  │               │      │   POST /search                 │                             │
  │               │──────┼──▶ ┌──────────────┐  flush     │      ┌──────────────────┐   │
  │               │      │    │ BatchWriter   │───────────┼─────▶│  SQLite store     │   │
  │               │      │    │ buffer (RAM)  │ size OR 2s │      │ (app/store.py)    │   │
  └──────────────┘       │    └──────────────┘            │      │ query,count,recent│   │
                         │        │ invalidate prefixes    │      └──────────────────┘   │
                         │        └────────────────────────┘                             │
                         │                                                               │
                         │   Background decay job: recent_count *= 0.9 every 30s         │
                         │   Metrics: latency p50/p95, hit rate, db reads/writes         │
                         └───────────────────────────────────────────────────────────────┘
```

## Read path: `GET /suggest`
1. Normalize prefix (trim + lowercase). Empty → `[]`.
2. Build cache key `"<mode>:<prefix>"` and route it through the consistent hash
   ring to its owning cache node.
3. **Hit** → return cached top-10 (`O(1)`).
4. **Miss** → walk the trie to the prefix node, gather all queries in the
   subtree, rank top-10 with the mode's scorer, store in cache, return.

## Write path: `POST /search`
1. Normalize query, append to the in-memory buffer, return `{"message":"Searched"}`.
   No DB write on the request path.
2. The batch loop flushes when the buffer hits `BATCH_SIZE` **or** every
   `FLUSH_INTERVAL` seconds:
   - aggregate duplicates into a `Counter`,
   - one SQL `INSERT … ON CONFLICT … count = count + n` (durable),
   - update the in-memory trie (`count += n`, `recent += n`, create if new),
   - invalidate the cache entries for every prefix of every changed query.

## Background: recency decay
Every `DECAY_INTERVAL` seconds a job multiplies every query's `recent_count` by
`DECAY_FACTOR` (0.9) in both the trie (in-memory) and SQLite (one `UPDATE`). This
makes recent activity fade so a brief spike does not stay on top forever.

## Data structures
- **Trie** (`index_trie.py`): node = `{children, word, count, recent}`. A flat
  `terminals` list of all word-nodes makes global ops (decay, trending) a simple
  loop instead of a full tree walk.
- **Consistent hash ring** (`cache.py`): `md5(key)` → 32-bit point; each node is
  placed at 150 virtual points; `bisect` finds the next point clockwise. md5
  (not Python's salted `hash()`) keeps routing stable across runs.
- **SQLite** (`store.py`): `queries(query PK, count, recent_count)`.

## Ranking
- **basic**: `score = count` (all-time popularity — the 60% requirement).
- **trending**: `score = count + RECENCY_BOOST · recent_count` (recency-aware —
  the +20% requirement). Same `/suggest` endpoint, selected by `?mode=`.

## Where each rubric item lives
| Rubric item | Where |
|-------------|-------|
| Dataset ingestion | `ingest.py`, `store.load_csv` |
| Suggestions API | `main.suggest`, `index_trie.lookup` |
| Search API + count update | `main.search`, `batch_writer`, `store.batch_upsert` |
| Distributed cache + consistent hashing | `cache.py`, `main.cache_debug` |
| Trending (recency) | `ranking.trending_scorer`, `main` decay loop, `main.trending` |
| Batch writes | `batch_writer.py` |
| Performance reporting | `metrics.py`, `main.stats` |
```

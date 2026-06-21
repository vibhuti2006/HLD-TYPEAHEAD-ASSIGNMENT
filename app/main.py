"""
main.py — the FastAPI application: startup, the APIs, and the background jobs.

APIs (exactly the ones the assignment lists, plus the two the UI/report need):

  Required by the assignment:
    GET  /suggest?q=<prefix>&mode=<basic|trending>   top-10 prefix suggestions
    POST /search        body {"query": "..."}        records the search, returns "Searched"
    GET  /cache/debug?prefix=<prefix>                which cache node owns it + hit/miss

  Needed to satisfy other stated requirements:
    GET  /trending      -> UI "Trending searches section"
    GET  /stats         -> performance report (latency p95, hit rate, read/write counts)

Read path  (/suggest):  cache  ->  (miss) trie  ->  fill cache
Write path (/search) :  buffer ->  periodic batch flush -> store + trie + cache invalidate
"""

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.batch_writer import BatchWriter
from app.cache import DistributedCache
from app.config import (DECAY_FACTOR, DECAY_INTERVAL_SECONDS,
                        NEW_QUERY_INITIAL_COUNT, TOP_K)
from app.index_trie import Trie
from app.metrics import metrics
from app.ranking import get_scorer, trending_scorer
from app.store import Store

# These are populated during startup (lifespan) and shared by all requests.
state = {}


async def _decay_loop():
    """Background job: every DECAY_INTERVAL, fade recent activity by DECAY_FACTOR
    in both the in-memory trie and the durable store. This is what stops a brief
    spike from ranking highly forever."""
    import asyncio
    while True:
        await asyncio.sleep(DECAY_INTERVAL_SECONDS)
        # routed through the batch writer so decay and flush share one lock
        state["batch"].decay(DECAY_FACTOR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- STARTUP ----
    store = Store()
    if store.is_empty():
        n = store.load_csv()
        print(f"[startup] loaded {n:,} queries from CSV into SQLite")

    # Build the in-memory trie from the durable store.
    trie = Trie()
    rows = store.load_all()
    for query, count, recent in rows:
        trie.insert(query, count, recent)
    print(f"[startup] built trie with {len(trie.terminals):,} queries")

    cache = DistributedCache()
    batch = BatchWriter(store, trie, cache)

    state.update(store=store, trie=trie, cache=cache, batch=batch)
    batch.start()                       # start periodic flush loop
    import asyncio
    decay_task = asyncio.create_task(_decay_loop())

    yield                               # ---- app runs here ----

    # ---- SHUTDOWN ----
    decay_task.cancel()
    await batch.stop()                  # flush any buffered searches
    store.close()


app = FastAPI(title="Search Typeahead", lifespan=lifespan)


# --------------------------------------------------------------------------
# GET /suggest?q=<prefix>&mode=<basic|trending>
# --------------------------------------------------------------------------
@app.get("/suggest")
def suggest(q: str = Query(default=""), mode: str = Query(default="basic")):
    """Return at most TOP_K suggestions that start with `q`, ranked by `mode`.

    Edge cases (all required by the assignment):
      - empty / missing q     -> []
      - mixed-case q          -> lowercased before matching
      - prefix with no matches -> []
    """
    started = time.perf_counter()
    prefix = (q or "").strip().lower()

    if not prefix:                                   # empty / missing input
        metrics.record_suggest((time.perf_counter() - started) * 1000, hit=True)
        return {"prefix": prefix, "mode": mode, "suggestions": []}

    cache = state["cache"]
    cache_key = f"{mode}:{prefix}"

    cached = cache.get(cache_key)
    if cached is not None:                           # CACHE HIT
        metrics.record_suggest((time.perf_counter() - started) * 1000, hit=True)
        return {"prefix": prefix, "mode": mode, "suggestions": cached, "source": "cache"}

    # CACHE MISS -> fall back to the trie (the primary in-memory index)
    scorer = get_scorer(mode)
    results = state["trie"].lookup(prefix, TOP_K, scorer)
    suggestions = [{"query": w, "count": c} for (w, c, _r) in results]
    cache.set(cache_key, suggestions)                # populate cache for next time

    metrics.record_suggest((time.perf_counter() - started) * 1000, hit=False)
    return {"prefix": prefix, "mode": mode, "suggestions": suggestions, "source": "trie"}


# --------------------------------------------------------------------------
# POST /search   body: {"query": "..."}
# --------------------------------------------------------------------------
class SearchBody(BaseModel):
    query: str


@app.post("/search")
def search(body: SearchBody):
    """Dummy search endpoint. Records the query (via the batch buffer) and
    returns the required dummy response. New queries are created on flush with
    an initial count; existing queries have their count incremented."""
    query = (body.query or "").strip().lower()
    if query:
        state["batch"].submit(query)                 # buffered, NOT written now
    return {"message": "Searched"}


# --------------------------------------------------------------------------
# GET /cache/debug?prefix=<prefix>
# --------------------------------------------------------------------------
@app.get("/cache/debug")
def cache_debug(prefix: str = Query(default=""), mode: str = Query(default="basic")):
    """Show which cache node owns this prefix key and whether it is a hit/miss.
    This is the live evidence that consistent hashing routes keys to nodes."""
    prefix = (prefix or "").strip().lower()
    cache_key = f"{mode}:{prefix}"
    return state["cache"].debug(cache_key)


# --------------------------------------------------------------------------
# GET /trending   (powers the UI "Trending searches" section)
# --------------------------------------------------------------------------
@app.get("/trending")
def trending():
    """Top queries by recent (decayed) activity. Computed from the in-memory
    trie, so it needs no DB read."""
    results = state["trie"].top_recent(TOP_K, trending_scorer)
    # Only show queries that actually have recent activity (recent > 0).
    items = [{"query": w, "count": c, "recent": round(r, 2)}
             for (w, c, r) in results if r > 0]
    return {"trending": items}


# --------------------------------------------------------------------------
# GET /stats   (performance report numbers)
# --------------------------------------------------------------------------
@app.get("/stats")
def stats():
    return JSONResponse(metrics.snapshot())


# --------------------------------------------------------------------------
# Serve the frontend
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")

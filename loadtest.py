"""
loadtest.py — measure the system and write PERFORMANCE_REPORT.md.

This produces the three numbers the assignment's performance report asks for:
  1. latency of /suggest (p50 / p95), measured client-side under concurrency
  2. cache hit rate (from the server's /stats, measured as a delta for this run)
  3. write reduction from batching (searches issued vs DB writes actually done)
Plus a bonus: consistent-hashing key distribution across the cache nodes.

It uses ONLY the Python standard library (urllib), so there is nothing extra to
install. Run it against an already-running server:

    # terminal 1
    .venv/bin/uvicorn app.main:app --port 8099
    # terminal 2
    python3 loadtest.py                       # defaults to http://localhost:8099

The script reads /stats BEFORE and AFTER each phase and reports the DELTA, so the
numbers are correct for this run even if the server already served other traffic.
"""

import argparse
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# A spread of prefixes that exist in the AOL dataset. The set is reused many
# times during the run, which is what lets the cache register hits.
PREFIXES = [
    "go", "goo", "goog", "google", "ya", "yah", "yahoo", "eb", "ebay",
    "map", "mapq", "my", "mys", "myspace", "am", "ama", "amazon", "we",
    "wea", "weather", "new", "news", "car", "card", "ho", "hot", "hotel",
    "fa", "fac", "fl", "flo", "flower", "jo", "job", "jobs", "ho", "home",
    "ban", "bank", "cr", "craig", "craigslist", "wal", "walmart", "dis",
    "disney", "es", "espn", "mo", "movie", "movies", "re", "recipe",
]


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read())


def post(base, path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def percentile(values, pct):
    if not values:
        return 0.0
    data = sorted(values)
    idx = min(len(data) - 1, int(round((pct / 100.0) * len(data) + 0.5)) - 1)
    return data[idx]


# ---- Phase 1: latency + cache hit rate ------------------------------------
def measure_suggest(base, total_requests, concurrency):
    print(f"[phase 1] {total_requests} /suggest requests "
          f"(concurrency {concurrency})...")
    before = get(base, "/stats")

    # build the request list (cycle through PREFIXES, both ranking modes)
    reqs = []
    for i in range(total_requests):
        prefix = PREFIXES[i % len(PREFIXES)]
        mode = "trending" if i % 5 == 0 else "basic"
        reqs.append(f"/suggest?q={prefix}&mode={mode}")

    latencies_ms = []

    def one(path):
        t0 = time.perf_counter()
        get(base, path)
        return (time.perf_counter() - t0) * 1000

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for ms in pool.map(one, reqs):
            latencies_ms.append(ms)

    after = get(base, "/stats")
    hits = after["cache_hits"] - before["cache_hits"]
    misses = after["cache_misses"] - before["cache_misses"]
    hit_rate = hits / (hits + misses) if (hits + misses) else 0.0

    return {
        "requests": total_requests,
        "concurrency": concurrency,
        "client_p50_ms": round(percentile(latencies_ms, 50), 3),
        "client_p95_ms": round(percentile(latencies_ms, 95), 3),
        "client_max_ms": round(max(latencies_ms), 3),
        "cache_hits": hits,
        "cache_misses": misses,
        "cache_hit_rate": round(hit_rate, 4),
    }


# ---- Phase 2: write reduction from batching -------------------------------
def measure_writes(base, total_searches, concurrency):
    print(f"[phase 2] {total_searches} POST /search requests "
          f"(concurrency {concurrency})...")
    before = get(base, "/stats")

    # Spread searches over a small set of queries so aggregation has an effect.
    queries = ["iphone", "laptop", "headphones", "coffee maker", "running shoes"]
    bodies = [{"query": queries[i % len(queries)]} for i in range(total_searches)]

    def one(body):
        post(base, "/search", body)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(one, bodies))

    # give the batch writer time to flush the final buffer
    time.sleep(3)
    after = get(base, "/stats")

    searches = after["searches"] - before["searches"]
    flushes = after["search_flushes"] - before["search_flushes"]
    reduction = (1 - flushes / searches) if searches else 0.0
    return {
        "searches_issued": searches,
        "db_writes_performed": flushes,
        "write_reduction_ratio": round(reduction, 4),
        "writes_avoided": searches - flushes,
    }


# ---- Phase 3: consistent-hashing key distribution -------------------------
def measure_hashing(base):
    print("[phase 3] checking consistent-hashing key distribution...")
    tally = {}
    # use many DISTINCT keys so we can see how evenly the ring spreads them
    keys = [f"{p}{i}" for i in range(6) for p in PREFIXES]  # ~300 distinct keys
    for key in keys:
        d = get(base, f"/cache/debug?prefix={key}")
        owner = d["owner_node"]
        tally[owner] = tally.get(owner, 0) + 1
    return {"total_keys": len(keys), "distribution": tally}


def render_report(base, lat, writes, hashing, final_stats):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    dist_rows = "\n".join(
        f"| {node} | {count} | {count / hashing['total_keys']:.1%} |"
        for node, count in sorted(hashing["distribution"].items())
    )
    return f"""# Performance Report

Generated by `loadtest.py` on {now} against `{base}`.

All numbers are measured for this run (deltas of the server's `/stats`), not
hand-picked. Re-run `python3 loadtest.py` to reproduce.

## 1. Suggestion latency (`GET /suggest`)

{lat['requests']} requests at concurrency {lat['concurrency']}.

| Metric | Value |
|--------|-------|
| p50 latency | **{lat['client_p50_ms']} ms** |
| p95 latency | **{lat['client_p95_ms']} ms** |
| max latency | {lat['client_max_ms']} ms |

Latency is low because a cache hit is an O(1) dictionary lookup and a cache miss
is an O(len(prefix)) trie walk; no per-request database read is on the path.

## 2. Cache hit rate

Measured over the same {lat['requests']} suggestion requests (the prefix set is
reused, so repeats hit the cache):

| Metric | Value |
|--------|-------|
| cache hits | {lat['cache_hits']} |
| cache misses | {lat['cache_misses']} |
| **hit rate** | **{lat['cache_hit_rate']:.1%}** |

The first time a prefix is seen it misses and is computed from the trie, then
cached (TTL 60s); subsequent requests for the same prefix hit the cache.

## 3. Write reduction from batching

{writes['searches_issued']} searches were submitted; the batch writer aggregated
them and flushed to SQLite in far fewer transactions.

| Metric | Value |
|--------|-------|
| searches issued | {writes['searches_issued']} |
| DB writes performed | {writes['db_writes_performed']} |
| writes avoided | {writes['writes_avoided']} |
| **write reduction** | **{writes['write_reduction_ratio']:.1%}** |

Every search would normally be one DB write. Batching collapses many searches
(including duplicates of the same query) into a single transaction per flush.

## 4. Consistent-hashing key distribution

{hashing['total_keys']} distinct keys routed through the hash ring:

| Cache node | Keys owned | Share |
|------------|-----------|-------|
{dist_rows}

An even split (≈ 1/N each) shows the ring + virtual nodes distribute keys
evenly across the cache nodes.

## Raw final `/stats` snapshot

```json
{json.dumps(final_stats, indent=2)}
```
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8099")
    ap.add_argument("--suggest-requests", type=int, default=5000)
    ap.add_argument("--search-requests", type=int, default=2000)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--out", default="PERFORMANCE_REPORT.md")
    args = ap.parse_args()

    # fail early if the server isn't up
    try:
        get(args.base, "/stats")
    except Exception as e:
        raise SystemExit(
            f"Could not reach {args.base}. Start the server first:\n"
            f"  .venv/bin/uvicorn app.main:app --port 8099\n({e})"
        )

    lat = measure_suggest(args.base, args.suggest_requests, args.concurrency)
    writes = measure_writes(args.base, args.search_requests, args.concurrency)
    hashing = measure_hashing(args.base)
    final_stats = get(args.base, "/stats")

    report = render_report(args.base, lat, writes, hashing, final_stats)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)

    print("\n=== summary ===")
    print(f"  p50 / p95 latency : {lat['client_p50_ms']} / {lat['client_p95_ms']} ms")
    print(f"  cache hit rate    : {lat['cache_hit_rate']:.1%}")
    print(f"  write reduction   : {writes['write_reduction_ratio']:.1%} "
          f"({writes['searches_issued']} searches -> {writes['db_writes_performed']} writes)")
    print(f"  hash distribution : {hashing['distribution']}")
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()

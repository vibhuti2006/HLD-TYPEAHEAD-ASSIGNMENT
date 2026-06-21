"""
ingest.py — Turn raw AOL search logs into a clean (query, count) dataset.

Raw AOL files are tab-separated event logs with 5 columns:
    AnonID   Query   QueryTime   ItemRank   ClickURL
Each row is ONE search event. The same query appears many times, so the
number of times a query is repeated == its popularity. We therefore:

    1. Stream the file line by line (never load 216 MB into memory at once).
    2. Keep only the Query column, lowercased + trimmed.
    3. Drop junk: empty, the literal "-" AOL uses for "no query", 1-char queries.
    4. Drop explicit / adult queries (see app/content_filter.py) so they never
       enter the dataset, database, or suggestions.
    5. Aggregate: GROUP BY query -> COUNT(*).
    6. Keep the top-N most popular queries.
    7. Write data/queries.csv with header "query,count".

Usage:
    python3 ingest.py
    python3 ingest.py --top 200000 --min-count 1
"""

import argparse
import csv
import glob
import os
from collections import Counter

from app.content_filter import is_explicit

RAW_GLOB = "data/user-ct-test-collection-*.txt"
OUT_PATH = "data/queries.csv"


def iter_queries(paths):
    """Yield one normalized query string per raw event row."""
    for path in paths:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            header = f.readline()  # skip the "AnonID\tQuery\t..." header line
            for line in f:
                cols = line.split("\t")
                if len(cols) < 2:
                    continue
                q = cols[1].strip().lower()
                yield q


def is_valid(q):
    if not q:
        return False
    if q == "-":          # AOL's placeholder for "no query"
        return False
    if len(q) < 2:        # single chars aren't useful for typeahead
        return False
    if is_explicit(q):    # drop adult / explicit queries
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=200_000,
                    help="keep the top-N queries by count (default 200000)")
    ap.add_argument("--min-count", type=int, default=1,
                    help="drop queries seen fewer than this many times")
    args = ap.parse_args()

    paths = sorted(glob.glob(RAW_GLOB))
    if not paths:
        raise SystemExit(f"No raw files matched {RAW_GLOB!r}. "
                         f"Put the AOL .txt files in data/")

    print(f"Reading {len(paths)} file(s):")
    for p in paths:
        print(f"  - {p}")

    counts = Counter()
    rows_seen = 0
    explicit_dropped = 0
    for q in iter_queries(paths):
        rows_seen += 1
        if q and q != "-" and len(q) >= 2 and is_explicit(q):
            explicit_dropped += 1
        if is_valid(q):
            counts[q] += 1
        if rows_seen % 1_000_000 == 0:
            print(f"  ...processed {rows_seen:,} rows, "
                  f"{len(counts):,} unique queries so far")

    print(f"\nTotal event rows read   : {rows_seen:,}")
    print(f"Explicit rows filtered  : {explicit_dropped:,}")
    print(f"Unique valid queries    : {len(counts):,}")

    # apply min-count filter, then take the top-N by count
    items = [(q, c) for q, c in counts.items() if c >= args.min_count]
    items.sort(key=lambda x: x[1], reverse=True)
    items = items[: args.top]

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["query", "count"])
        w.writerows(items)

    out_size = os.path.getsize(OUT_PATH) / (1024 * 1024)
    print(f"\nWrote {len(items):,} rows to {OUT_PATH} ({out_size:.1f} MB)")
    print("\nTop 10 queries:")
    for q, c in items[:10]:
        print(f"  {c:>8,}  {q}")


if __name__ == "__main__":
    main()

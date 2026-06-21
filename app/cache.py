"""
cache.py — distributed suggestion cache with consistent hashing.

The class notes describe two views of the same idea:
  1. augment the trie with the top-K at each node, OR
  2. "effectively, we're just caching the top-K results for each prefix."
This file is view #2: a key-value cache of  prefix -> top-K suggestions.

It is "distributed" across several LOGICAL cache nodes (in one process for the
demo, but the routing logic is identical to real Redis shards). A consistent
hash ring decides which node owns each prefix key.

WHY CONSISTENT HASHING (the exam question):
  With plain  hash(key) % N , changing N (adding/removing a cache node) remaps
  almost EVERY key, so the whole cache misses at once. With a hash ring, adding
  or removing a node only remaps the keys between two adjacent points on the
  ring — about 1/N of keys. Virtual nodes spread each real node over many ring
  points so the load is even.
"""

import bisect
import hashlib
import time

from app.config import CACHE_NODES, CACHE_TTL_SECONDS, VIRTUAL_NODES


def _hash(key):
    """Stable 32-bit hash. We use md5 (not Python's built-in hash()) because
    built-in hash() of strings is randomised per process, which would make the
    ring — and the /cache/debug output — different on every run."""
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)   # first 32 bits is plenty for the ring


class CacheNode:
    """One logical cache shard: an in-memory dict with per-entry TTL."""

    def __init__(self, node_id):
        self.node_id = node_id
        self.store = {}            # key -> (value, expiry_epoch)
        self.hits = 0
        self.misses = 0

    def get(self, key):
        entry = self.store.get(key)
        if entry is None:
            self.misses += 1
            return None
        value, expiry = entry
        if time.time() >= expiry:  # expired -> treat as a miss and evict
            del self.store[key]
            self.misses += 1
            return None
        self.hits += 1
        return value

    def set(self, key, value, ttl):
        self.store[key] = (value, time.time() + ttl)

    def delete(self, key):
        self.store.pop(key, None)

    def contains_fresh(self, key):
        """True if the key is present AND not expired — used by /cache/debug
        to report hit/miss WITHOUT mutating hit/miss counters."""
        entry = self.store.get(key)
        return entry is not None and time.time() < entry[1]


class ConsistentHashRing:
    """Maps any key to one node via a sorted ring of (hash -> node) points."""

    def __init__(self, node_ids, virtual_nodes=VIRTUAL_NODES):
        self.virtual_nodes = virtual_nodes
        self._ring = {}            # ring point hash -> node_id
        self._sorted_points = []   # sorted ring point hashes (for bisect)
        for node_id in node_ids:
            self.add_node(node_id)

    def add_node(self, node_id):
        # Place `virtual_nodes` points for this node around the ring.
        for v in range(self.virtual_nodes):
            point = _hash(f"{node_id}#{v}")
            self._ring[point] = node_id
        self._sorted_points = sorted(self._ring)

    def remove_node(self, node_id):
        self._ring = {p: n for p, n in self._ring.items() if n != node_id}
        self._sorted_points = sorted(self._ring)

    def get_node(self, key):
        """Walk clockwise from hash(key) to the next ring point and return its node."""
        if not self._sorted_points:
            return None
        h = _hash(key)
        idx = bisect.bisect(self._sorted_points, h)
        if idx == len(self._sorted_points):
            idx = 0                # wrap around the ring
        point = self._sorted_points[idx]
        return self._ring[point]


class DistributedCache:
    """The cache the rest of the app talks to. Routes every key through the ring."""

    def __init__(self, num_nodes=CACHE_NODES, ttl=CACHE_TTL_SECONDS):
        self.ttl = ttl
        self.node_ids = [f"cache-node-{i}" for i in range(num_nodes)]
        self.nodes = {nid: CacheNode(nid) for nid in self.node_ids}
        self.ring = ConsistentHashRing(self.node_ids)

    def _node_for(self, key):
        return self.nodes[self.ring.get_node(key)]

    def get(self, key):
        return self._node_for(key).get(key)

    def set(self, key, value):
        self._node_for(key).set(key, value, self.ttl)

    def invalidate(self, key):
        """Drop a key so the next read recomputes fresh suggestions from the trie."""
        self._node_for(key).delete(key)

    def debug(self, key):
        """Power the GET /cache/debug endpoint: which node owns this key, is it
        a hit or miss right now, and how full each node is."""
        node = self._node_for(key)
        return {
            "key": key,
            "owner_node": node.node_id,
            "status": "hit" if node.contains_fresh(key) else "miss",
            "ring_position": _hash(key),
            "nodes": {
                nid: {"keys_stored": len(n.store), "hits": n.hits, "misses": n.misses}
                for nid, n in self.nodes.items()
            },
        }

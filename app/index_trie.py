"""
index_trie.py — the prefix index that answers "which queries start with X?".

This is exactly the structure from the class notes:
  "each trie node will store children (the subsequent letters), isTerminal,
   and count (only if terminal — how many times this query was searched)."

A trie (prefix tree) is the natural fit for typeahead because every suggestion
must START WITH the typed prefix. Walking the prefix is O(len(prefix)); the
matching queries are everything in the subtree below that node.

Ranking (sort by count, or by recency) is NOT done here — the trie just finds
the candidate queries and hands them to a scorer function. This keeps the data
structure and the ranking policy separate and easy to explain.
"""

import heapq


class TrieNode:
    # __slots__ keeps each node small (we create a lot of them).
    __slots__ = ("children", "word", "count", "recent")

    def __init__(self):
        self.children = {}     # char -> TrieNode
        self.word = None       # the full query string, set only on a terminal node
        self.count = 0         # all-time popularity (meaningful only if terminal)
        self.recent = 0.0      # decayed recent activity (for trending)


class Trie:
    def __init__(self):
        self.root = TrieNode()
        # Flat list of every terminal node, so global operations (decay, trending)
        # are a simple loop instead of a full tree walk.
        self.terminals = []

    def insert(self, word, count, recent=0.0):
        """Add a query with its starting count. Used when loading the dataset."""
        node = self.root
        for ch in word:
            nxt = node.children.get(ch)
            if nxt is None:
                nxt = TrieNode()
                node.children[ch] = nxt
            node = nxt
        if node.word is None:            # first time we see this exact word
            node.word = word
            self.terminals.append(node)
        node.count = count
        node.recent = recent

    def bump(self, word, delta):
        """Apply a batch increment: count += delta, recent += delta.

        Creates the query if it did not exist (a brand-new search).
        """
        node = self.root
        for ch in word:
            nxt = node.children.get(ch)
            if nxt is None:
                nxt = TrieNode()
                node.children[ch] = nxt
            node = nxt
        if node.word is None:
            node.word = word
            self.terminals.append(node)
        node.count += delta
        node.recent += delta

    def decay_all(self, factor):
        """Fade every query's recent activity (recent *= factor).

        Iterates a snapshot (list(...)) because a concurrent search can append
        a brand-new terminal node; iterating the live list could otherwise raise
        'list changed size during iteration'."""
        for node in list(self.terminals):
            node.recent *= factor

    def _node_for_prefix(self, prefix):
        """Walk down to the node representing `prefix`, or None if no match."""
        node = self.root
        for ch in prefix:
            node = node.children.get(ch)
            if node is None:
                return None
        return node

    def _collect(self, node, out):
        """Depth-first gather of every terminal node in this subtree."""
        if node.word is not None:
            out.append(node)
        for child in node.children.values():
            self._collect(child, out)

    def lookup(self, prefix, k, scorer):
        """Return up to k (query, count, recent) tuples under `prefix`,
        ranked highest-first by `scorer(node)`.

        Handles the edge cases the assignment lists:
          - empty / missing prefix -> []  (handled by the caller, but safe here too)
          - prefix with no matches  -> []
        """
        if not prefix:
            return []
        start = self._node_for_prefix(prefix)
        if start is None:
            return []                      # no query starts with this prefix
        candidates = []
        self._collect(start, candidates)
        # heapq.nlargest gives the top-k by score without sorting everything.
        top = heapq.nlargest(k, candidates, key=scorer)
        return [(n.word, n.count, n.recent) for n in top]

    def top_recent(self, k, scorer):
        """Global top-k by the given scorer — used for the Trending section."""
        top = heapq.nlargest(k, self.terminals, key=scorer)
        return [(n.word, n.count, n.recent) for n in top]

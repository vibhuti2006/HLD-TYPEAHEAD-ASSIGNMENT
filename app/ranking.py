"""
ranking.py — how suggestions are ordered. Two modes, one /suggest endpoint.

BASIC (the 60% requirement):
    score = count
    => historically popular queries appear first.

TRENDING / recency-aware (the +20% requirement):
    score = count + RECENCY_BOOST * recent_count
    => recently searched queries are pushed up, but all-time count still
       provides a baseline and breaks ties.

Why this avoids permanently over-ranking a brief spike:
    `recent_count` is multiplied by DECAY_FACTOR (0.9) every DECAY_INTERVAL by a
    background job. A query that spiked once and then went quiet stops getting
    increments, so its recent_count decays back toward 0 and it falls back to
    its plain popularity rank. Only *sustained* recent activity stays on top.

A "scorer" is just a function node -> number. The trie ranks candidates with it.
"""

from app.config import RECENCY_BOOST

# Each trie node exposes .count and .recent (see index_trie.TrieNode).

def basic_scorer(node):
    return node.count


def trending_scorer(node):
    return node.count + RECENCY_BOOST * node.recent


def get_scorer(mode):
    """Pick the scorer for the request. Unknown modes fall back to basic."""
    return trending_scorer if mode == "trending" else basic_scorer

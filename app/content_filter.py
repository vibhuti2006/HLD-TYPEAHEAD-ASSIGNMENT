"""
content_filter.py — drop explicit / adult queries from the dataset.

The raw AOL log is real user search data and contains a lot of adult content.
We filter it out at INGESTION time so it never reaches queries.csv, the
database, or the trie — the system simply never knows about these queries.

How the match works:
  Each blocked term is matched as a WORD STEM with a word boundary in front
  (regex \bterm). This blocks "sex", "sexy", "sexual" but NOT innocent words
  that merely contain the letters, e.g. "essex", "sussex", "middlesex",
  "scunthorpe". That word-boundary detail is the thing to mention in the viva
  if asked "how do you avoid false positives?".
"""

import re

# Explicit / adult stems. Matched at a word boundary, so "sex" also catches
# "sexy"/"sexual" but not "essex". Keep this list in one place so it's easy to
# extend or explain.
_BLOCKED_STEMS = [
    "sex", "porn", "porno", "xxx", "nude", "naked", "boob", "tit", "tits",
    "pussy", "penis", "vagina", "dick", "cock", "cum", "orgasm", "masturbat",
    "blowjob", "handjob", "anal", "dildo", "vibrator", "escort", "hooker",
    "prostitut", "hentai", "milf", "incest", "rape", "bestiality", "fetish",
    "erotic", "lingerie", "stripper", "playboy", "hustler", "redtube",
    "pornhub", "xvideos", "xhamster", "youporn", "fuck", "shit", "bitch",
    "whore", "slut", "horny", "gangbang", "threesome", "bdsm", "creampie",
    "deepthroat", "cumshot", "bukkake", "upskirt", "voyeur", "camgirl",
    "webcam girl", "adult video", "adult dvd", "free porn", "teen porn",
    "child porn", "pedophil", "lolita",
]

# Pre-compile one regex: \b(sex|porn|xxx|...) — case-insensitive.
_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _BLOCKED_STEMS) + r")",
    re.IGNORECASE,
)


def is_explicit(query):
    """Return True if the query contains a blocked adult/explicit term."""
    return bool(_PATTERN.search(query))

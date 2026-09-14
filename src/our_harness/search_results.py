"""Conservative search-result screening, not a factual relevance judgment."""
from __future__ import annotations

import re
from urllib.parse import urlsplit, unquote

_GENERIC = frozenset("the a an and or for of in to with from about official websites website tools tool popular current latest best features documentation logo logos svg brand assets source sources images image 2026".split())


def usable(query: str, results: list[dict]) -> list[dict]:
    # Multiple positive site operators are treated as alternatives. Never use
    # suffix-only matching: example.org.attacker.test is not example.org.
    sites = [s.lower().rstrip('.') for s in re.findall(r'(?<!\S)site:([\w.-]+)', query, re.I)]
    text = re.sub(r'(?<!\S)site:\S+', '', query, flags=re.I)
    terms = {t.lower() for t in re.findall(r'[A-Za-z][A-Za-z0-9]+', text)} - _GENERIC
    # Only broad multi-topic queries offer enough lexical evidence to reject
    # a completely disjoint batch. Short synonym queries and unknown scripts
    # are not reliably screened. Explicit hostname constraints still apply.
    lexical = len(terms) >= 6 and text.isascii()
    kept, seen = [], set()
    for item in results:
        url = str(item.get('url') or '')
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        host = (parsed.hostname or '').lower().rstrip('.')
        if parsed.scheme not in {'http', 'https'} or not host:
            continue
        if sites and not any(host == s or host.endswith('.' + s) for s in sites):
            continue
        words = set(re.findall(r'[a-z][a-z0-9]+', (str(item.get('title') or '') + ' ' + unquote(url)).lower()))
        if lexical and not terms.intersection(words):
            continue
        if url not in seen:
            kept.append(item)
            seen.add(url)
    return kept

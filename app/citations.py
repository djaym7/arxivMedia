"""External citation lookups (Semantic Scholar Graph API).

Pure I/O + parsing. This module never touches the DB and never raises; the
orchestration (when to fetch, how to pace, what to write) lives in ingest.py so
the request path never calls Semantic Scholar.
"""
import os

import httpx

CITATION_TIMEOUT = 10.0
CITATION_SLEEP_SECS = 1.1


def arxiv_id_from_source(source: str | None) -> str | None:
    """Strip the 'arxiv:' prefix from a posts.source value.

    'arxiv:2406.01234' -> '2406.01234'. Returns None if source is falsy or does
    not start with 'arxiv:'.
    """
    if not source or not source.startswith("arxiv:"):
        return None
    bare = source[len("arxiv:"):].strip()
    return bare or None


def fetch_citation_count(arxiv_id: str) -> tuple[int, str] | None:
    """Look up an arXiv paper's citation count on Semantic Scholar.

    arxiv_id: bare id, e.g. '2406.01234' (NO 'arxiv:' prefix, NO version suffix).
    Returns (citation_count, citation_url) on success, or None on 404 / 429 /
    timeout / network error / malformed response. NEVER raises.
    """
    try:
        url = (
            f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}"
            "?fields=citationCount,influentialCitationCount,url"
        )
        headers = {}
        api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
        if api_key:
            headers["x-api-key"] = api_key
        resp = httpx.get(url, timeout=CITATION_TIMEOUT, headers=headers,
                         follow_redirects=True)
        if resp.status_code != 200:
            return None
        data = resp.json()
        count = data.get("citationCount")
        if count is None:
            return None
        cite_url = data.get("url") or f"https://arxiv.org/abs/{arxiv_id}"
        return int(count), cite_url
    except Exception:
        return None

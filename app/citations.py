"""External citation lookups (OpenAlex primary, Semantic Scholar fallback).

Pure I/O + parsing. This module never touches the DB and never raises; the
orchestration (when to fetch, how to pace, what to write) lives in ingest.py so
the request path never calls an external citation API.

Lookup strategy for an arXiv id:
  1. OpenAlex (free, no API key) — uses the "polite pool" via mailto. arXiv
     works are indexed with landing_page_url http://arxiv.org/abs/<id> and/or a
     DOI 10.48550/arXiv.<id>. We query by landing URL and validate the match.
  2. Semantic Scholar Graph API as a fallback when OpenAlex has no answer.
     Keyless callers are aggressively 429-throttled, so OpenAlex is primary.

We distinguish a *definitive* answer ("found: N" or "indexed but genuinely 0",
and "not found anywhere") from a *transient* failure (HTTP 429/5xx/timeout/
network error on the source). On a transient failure on BOTH sources we return
TRANSIENT so the caller can avoid stamping citation_checked_at and retry later.
"""
import os
import re

import httpx

CITATION_TIMEOUT = 10.0
CITATION_SLEEP_SECS = 1.1

OPENALEX_BASE = "https://api.openalex.org/works"
OPENALEX_MAILTO = os.environ.get("ARXIVMEDIA_CONTACT_EMAIL", "jmd.desai08@gmail.com")
USER_AGENT = (
    "arxivMedia/0.3 (+https://github.com/djaym7/arxivMedia; "
    f"mailto:{OPENALEX_MAILTO})"
)

# Sentinel returned when every source failed *transiently* (429/5xx/timeout),
# as opposed to a definitive "not found" (which is None). The caller uses this
# to decide whether to stamp citation_checked_at.
TRANSIENT = "transient"


def arxiv_id_from_source(source: str | None) -> str | None:
    """Strip the 'arxiv:' prefix from a posts.source value.

    'arxiv:2406.01234' -> '2406.01234'. Returns None if source is falsy or does
    not start with 'arxiv:'.
    """
    if not source or not source.startswith("arxiv:"):
        return None
    bare = source[len("arxiv:"):].strip()
    return bare or None


def _title_tokens(title: str | None) -> set[str]:
    return set(re.sub(r"[^a-z0-9 ]", "", (title or "").lower()).split())


def _title_similarity(a: str | None, b: str | None) -> float:
    """Jaccard token overlap of two titles in [0, 1]."""
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# Min title overlap to trust an OpenAlex record when we have the arXiv title.
TITLE_MATCH_THRESHOLD = 0.5


def _openalex_candidate_matches(work: dict, arxiv_id: str,
                                expected_title: str | None) -> bool:
    """Does this OpenAlex work genuinely correspond to this arXiv id?

    OpenAlex occasionally accretes unrelated landing URLs onto a single record,
    so a raw landing_page_url filter can return the wrong paper (e.g. a record
    titled 'AI-Assisted Pipeline...' coming back for the BERT arXiv id). When we
    know the real arXiv title we require a title-token overlap; this is the most
    reliable discriminator. Without a title we fall back to id-based checks.
    """
    if expected_title:
        return _title_similarity(expected_title, work.get("title")) >= TITLE_MATCH_THRESHOLD
    # No title to compare: trust only if the arXiv id is this work's primary id.
    primary = (work.get("primary_location") or {}).get("landing_page_url") or ""
    if f"/abs/{arxiv_id}" in primary:
        return True
    doi = ((work.get("ids") or {}).get("doi") or "").lower()
    return f"10.48550/arxiv.{arxiv_id}".lower() in doi


def fetch_openalex_citation(arxiv_id: str,
                            expected_title: str | None = None) -> tuple[int, str] | None | str:
    """Look up an arXiv paper's citation count on OpenAlex.

    Returns:
      (count, url)  on a definitive, validated hit,
      None          on a definitive miss (not indexed / no trustworthy match),
      TRANSIENT     on a transient failure (HTTP 429/5xx, timeout, network).
    NEVER raises. expected_title (the arXiv title) is used to reject OpenAlex
    records that have wrongly accreted this arXiv id.
    """
    headers = {"User-Agent": USER_AGENT}
    params = {
        "filter": f"locations.landing_page_url:http://arxiv.org/abs/{arxiv_id}",
        "per_page": "5",
        "mailto": OPENALEX_MAILTO,
    }
    try:
        resp = httpx.get(OPENALEX_BASE, params=params, headers=headers,
                         timeout=CITATION_TIMEOUT, follow_redirects=True)
    except Exception:
        return TRANSIENT
    if resp.status_code == 429 or resp.status_code >= 500:
        return TRANSIENT
    if resp.status_code != 200:
        return None  # definitive (e.g. 400/404)
    try:
        data = resp.json()
        results = data.get("results") or []
    except Exception:
        return None
    if not results:
        return None  # definitively not indexed under this arXiv id

    chosen = next(
        (w for w in results if _openalex_candidate_matches(w, arxiv_id, expected_title)),
        None)
    if chosen is None:
        # No trustworthy match. Only accept a lone result when we had NO title to
        # validate against (best-effort); never accept an unvalidated lone result
        # when a title was provided (that's the polluted-record case).
        if len(results) == 1 and not expected_title:
            chosen = results[0]
        else:
            return None
    count = chosen.get("cited_by_count")
    if count is None:
        return None
    cite_url = f"https://arxiv.org/abs/{arxiv_id}"
    try:
        return int(count), cite_url
    except (TypeError, ValueError):
        return None


def fetch_semantic_scholar_citation(arxiv_id: str) -> tuple[int, str] | None | str:
    """Look up an arXiv paper's citation count on Semantic Scholar.

    Returns (count, url) on a hit, None on a definitive miss, or TRANSIENT on a
    transient failure (429/5xx/timeout/network). NEVER raises. Honors the
    optional SEMANTIC_SCHOLAR_API_KEY for higher rate limits.
    """
    url = (
        f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{arxiv_id}"
        "?fields=citationCount,influentialCitationCount,url"
    )
    headers = {"User-Agent": USER_AGENT}
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    try:
        resp = httpx.get(url, timeout=CITATION_TIMEOUT, headers=headers,
                         follow_redirects=True)
    except Exception:
        return TRANSIENT
    if resp.status_code == 429 or resp.status_code >= 500:
        return TRANSIENT
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    count = data.get("citationCount")
    if count is None:
        return None
    cite_url = data.get("url") or f"https://arxiv.org/abs/{arxiv_id}"
    try:
        return int(count), cite_url
    except (TypeError, ValueError):
        return None


def fetch_citation_count(arxiv_id: str,
                         expected_title: str | None = None) -> tuple[int, str] | None | str:
    """Look up an arXiv paper's citation count (OpenAlex first, S2 fallback).

    arxiv_id: bare id, e.g. '2406.01234' (NO 'arxiv:' prefix, NO version suffix).
    expected_title: the arXiv title, used to validate the OpenAlex match.

    Returns:
      (citation_count, citation_url)  on a definitive hit,
      None                            on a definitive miss from every source,
      TRANSIENT                       when EVERY source failed transiently
                                      (429/5xx/timeout) and no definitive answer
                                      was obtained — the caller should NOT stamp
                                      citation_checked_at and should retry later.
    NEVER raises.
    """
    saw_transient = False

    primary = fetch_openalex_citation(arxiv_id, expected_title)
    if isinstance(primary, tuple):
        return primary
    if primary == TRANSIENT:
        saw_transient = True

    fallback = fetch_semantic_scholar_citation(arxiv_id)
    if isinstance(fallback, tuple):
        return fallback
    if fallback == TRANSIENT:
        saw_transient = True

    # Both sources answered. If either gave a definitive "not found" (None) and
    # neither was transient, it's a definitive miss. If we only ever saw
    # transient failures, report TRANSIENT so the caller retries next cycle.
    return TRANSIENT if saw_transient else None

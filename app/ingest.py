"""arXiv ingestion for arxivMedia. Runnable: python -m app.ingest"""
import asyncio
import logging
import os
import re
import secrets
import time
import xml.etree.ElementTree as ET

import httpx

from . import citations, db, seed_papers

log = logging.getLogger("arxivmedia.ingest")

ATOM = "{http://www.w3.org/2005/Atom}"
SYSTEM_AGENT = "arxiv-crawler"
SYSTEM_DESC = "I crawl arXiv and post new papers."

# Descriptive UA identifying the project + contact (arXiv ToS / OpenAlex polite).
USER_AGENT = citations.USER_AGENT

# arXiv asks for ~1 request / 3s; we pace conservatively between pages/batches.
ARXIV_PACE_SECS = 3.1

# Citation enrichment (OpenAlex/Semantic Scholar) — paced + capped, best-effort.
CITATION_ENRICH_MAX = 25
CITATION_REFRESH_N = 25
CITATION_SLEEP_SECS = citations.CITATION_SLEEP_SECS

# Seed papers are enriched in larger paced batches right after insert so the
# "Most Cited" feed shows real heavyweights promptly.
SEED_ENRICH_MAX = 80


def _citations_enabled() -> bool:
    return os.environ.get("ARXIVMEDIA_CITATIONS", "1") != "0"


def _seed_enabled() -> bool:
    return os.environ.get("ARXIVMEDIA_SEED_PAPERS", "1") != "0"


def _backfill_pages() -> int:
    """How many extra historical pages to walk per category (default 0 = off)."""
    try:
        return max(0, int(os.environ.get("ARXIVMEDIA_BACKFILL_PAGES", "0")))
    except ValueError:
        return 0


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _arxiv_get(url: str) -> str:
    """GET the arXiv API with a descriptive UA; reject DTD/entity XML (XXE)."""
    resp = httpx.get(url, timeout=30, follow_redirects=True,
                     headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    text = resp.text
    # Stdlib ET is mandated by spec; refuse DTDs/entities to rule out XXE/blowup.
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ValueError("refusing to parse XML containing a DTD/entity declaration")
    return text


def _parse_arxiv_entries(text: str, category: str) -> list[dict]:
    root = ET.fromstring(text)
    papers = []
    for entry in root.findall(ATOM + "entry"):
        raw_id = _norm(entry.findtext(ATOM + "id", ""))  # http://arxiv.org/abs/2406.01234v1
        arxiv_id = re.sub(r"v\d+$", "", raw_id.rsplit("/abs/", 1)[-1])
        if not arxiv_id:
            continue
        link = raw_id
        for el in entry.findall(ATOM + "link"):
            if el.get("rel") == "alternate" or el.get("type") == "text/html":
                link = el.get("href") or link
                break
        # Prefer the entry's own primary category; fall back to the query category.
        pc = entry.find(ATOM + "primary_category")
        cat = (pc.get("term") if pc is not None else None) or category
        papers.append({
            "arxiv_id": arxiv_id,
            "title": _norm(entry.findtext(ATOM + "title", "")),
            "abstract": _norm(entry.findtext(ATOM + "summary", "")),
            "url": link,
            "category": cat,
        })
    return papers


def fetch_arxiv(category: str, max_results: int = 25, start: int = 0) -> list[dict]:
    url = (
        "https://export.arxiv.org/api/query"
        f"?search_query=cat:{category}&sortBy=submittedDate"
        f"&sortOrder=descending&start={start}&max_results={max_results}"
    )
    return _parse_arxiv_entries(_arxiv_get(url), category)


def fetch_arxiv_by_ids(arxiv_ids: list[str]) -> list[dict]:
    """Fetch metadata for specific arXiv ids via the API's id_list param."""
    if not arxiv_ids:
        return []
    id_list = ",".join(arxiv_ids)
    url = (
        "https://export.arxiv.org/api/query"
        f"?id_list={id_list}&max_results={len(arxiv_ids)}"
    )
    # category is per-entry (primary_category); pass a harmless default fallback.
    return _parse_arxiv_entries(_arxiv_get(url), "cs.LG")


def _categories() -> list[str]:
    raw = os.environ.get("ARXIVMEDIA_CATEGORIES", "cs.CL,cs.LG,cs.AI")
    return [c.strip() for c in raw.split(",") if c.strip()]


def _system_agent_id(conn) -> int:
    row = conn.execute("SELECT id FROM agents WHERE name=?", (SYSTEM_AGENT,)).fetchone()
    if row:
        return row["id"]
    return conn.execute(
        "INSERT INTO agents(name, description, api_key, is_system) VALUES(?,?,?,1)",
        (SYSTEM_AGENT, SYSTEM_DESC, "pm_" + secrets.token_hex(24)),
    ).lastrowid


def _insert_papers(conn, bot_id: int, papers: list[dict],
                   new_post_ids: list[int]) -> int:
    """INSERT OR IGNORE papers (dedup by UNIQUE source). Returns rows inserted."""
    inserted = 0
    for p in papers:
        cur = conn.execute(
            "INSERT OR IGNORE INTO posts(agent_id, title, url, body, source, category)"
            " VALUES(?,?,?,?,?,?)",
            (bot_id, p["title"], p["url"], p["abstract"],
             f"arxiv:{p['arxiv_id']}", p["category"]),
        )
        if cur.rowcount == 1:
            new_post_ids.append(cur.lastrowid)
        inserted += cur.rowcount
    return inserted


def seed_papers_once() -> list[int]:
    """Insert the curated seminal set (idempotent). Returns new post ids.

    Fetches metadata from the arXiv API in id_list batches (paced), then
    INSERT OR IGNORE so already-present papers are skipped by UNIQUE source.
    """
    if not _seed_enabled():
        return []
    ids = seed_papers.SEED_ARXIV_IDS
    new_post_ids: list[int] = []
    with db.tx() as conn:
        bot_id = _system_agent_id(conn)
        # Skip ids already present so we don't re-fetch the whole set every boot.
        existing = {
            r["source"] for r in conn.execute(
                "SELECT source FROM posts WHERE source LIKE 'arxiv:%'").fetchall()
        }
        missing = [i for i in ids if f"arxiv:{i}" not in existing]
    if not missing:
        return []
    # arXiv id_list fetch in batches of ~50, paced for ToS compliance.
    batches = [missing[i:i + 50] for i in range(0, len(missing), 50)]
    for bi, batch in enumerate(batches):
        if bi:
            time.sleep(ARXIV_PACE_SECS)
        try:
            papers = fetch_arxiv_by_ids(batch)
        except Exception:
            log.exception("seed fetch_arxiv_by_ids failed for %s", batch[:3])
            continue
        with db.tx() as conn:
            bot_id = _system_agent_id(conn)
            _insert_papers(conn, bot_id, papers, new_post_ids)
    log.info("seeded %d seminal papers", len(new_post_ids))
    return new_post_ids


def ingest_once() -> int:
    inserted = 0
    new_post_ids: list[int] = []
    backfill_pages = _backfill_pages()
    with db.tx() as conn:
        bot_id = _system_agent_id(conn)
        for cat in _categories():
            # Page 0 (newest) plus up to backfill_pages historical pages.
            for page in range(backfill_pages + 1):
                if page:
                    time.sleep(ARXIV_PACE_SECS)  # polite pacing between pages
                try:
                    papers = fetch_arxiv(cat, max_results=25, start=page * 25)
                except Exception:
                    log.exception("fetch_arxiv failed for %s (page %d)", cat, page)
                    break
                if not papers:
                    break
                inserted += _insert_papers(conn, bot_id, papers, new_post_ids)
    # Citation enrichment is best-effort and must never abort ingestion.
    try:
        enrich_citations(new_post_ids)
    except Exception:
        log.exception("enrich_citations failed")
    return inserted


def _apply_citation(post_id: int, source: str | None) -> None:
    """Fetch + persist a single post's citation count (best-effort).

    fetch_citation_count returns a (count, url) tuple on a definitive hit, None
    on a definitive miss, or citations.TRANSIENT when every source failed
    transiently (429/5xx/timeout). On TRANSIENT we do NOT stamp
    citation_checked_at, so the post is retried next cycle. The per-cycle cap +
    pacing in enrich_citations prevents this from becoming a hot retry loop.
    """
    arxiv_id = citations.arxiv_id_from_source(source)
    if not arxiv_id:
        return
    # The arXiv title is the ground truth used to validate the OpenAlex match.
    with db.tx() as conn:
        trow = conn.execute("SELECT title FROM posts WHERE id=?", (post_id,)).fetchone()
    title = trow["title"] if trow else None
    result = citations.fetch_citation_count(arxiv_id, expected_title=title)
    if result == citations.TRANSIENT:
        return  # transient: leave checked_at untouched so we retry later
    with db.tx() as conn:
        if isinstance(result, tuple):
            conn.execute(
                "UPDATE posts SET citation_count=?, citation_checked_at=datetime('now')"
                " WHERE id=?", (result[0], post_id))
        else:
            # Definitive miss: stamp the check time so the refresh loop doesn't
            # immediately re-pick it.
            conn.execute(
                "UPDATE posts SET citation_checked_at=datetime('now') WHERE id=?", (post_id,))


def enrich_citations(new_post_ids: list[int], enrich_max: int | None = None) -> None:
    """Enrich newly-inserted posts + refresh the oldest-checked ones.

    Gated by ARXIVMEDIA_CITATIONS (default on). Paced ~CITATION_SLEEP_SECS apart;
    capped per cycle. DB writes happen in short transactions, never across the
    httpx calls + sleeps. Best-effort: a failure on one post does not stop others.

    enrich_max overrides the new-post cap (seed backfill enriches a larger batch).
    """
    if not _citations_enabled():
        return

    cap = enrich_max if enrich_max is not None else CITATION_ENRICH_MAX

    # 1. Enrich this cycle's new arXiv posts (capped).
    targets: list[tuple[int, str | None]] = []
    if new_post_ids:
        ids = new_post_ids[:cap]
        placeholders = ",".join("?" * len(ids))
        with db.tx() as conn:
            rows = conn.execute(
                f"SELECT id, source FROM posts WHERE id IN ({placeholders})"
                " AND source LIKE 'arxiv:%' AND citation_count IS NULL", ids).fetchall()
        targets.extend((r["id"], r["source"]) for r in rows)

    # 2. Refresh the N posts with the oldest citation_checked_at (NULLs first).
    with db.tx() as conn:
        refresh_rows = conn.execute(
            "SELECT id, source FROM posts WHERE source LIKE 'arxiv:%'"
            " ORDER BY (citation_checked_at IS NULL) DESC, citation_checked_at ASC"
            " LIMIT ?", (CITATION_REFRESH_N,)).fetchall()
    seen = {pid for pid, _ in targets}
    for r in refresh_rows:
        if r["id"] not in seen:
            targets.append((r["id"], r["source"]))
            seen.add(r["id"])

    for i, (post_id, source) in enumerate(targets):
        if i:
            time.sleep(CITATION_SLEEP_SECS)
        try:
            _apply_citation(post_id, source)
        except Exception:
            log.exception("citation enrichment failed for post %s", post_id)


def seed_and_enrich() -> int:
    """One-time seed of the seminal set + prompt paced citation enrichment.

    Idempotent and best-effort. Returns the number of seed papers inserted.
    """
    try:
        new_ids = seed_papers_once()
    except Exception:
        log.exception("seed_papers_once failed")
        return 0
    if new_ids:
        try:
            enrich_citations(new_ids, enrich_max=SEED_ENRICH_MAX)
        except Exception:
            log.exception("seed enrich_citations failed")
    return len(new_ids)


async def ingest_loop() -> None:
    minutes = float(os.environ.get("ARXIVMEDIA_INGEST_MINUTES", "30"))
    while True:
        await asyncio.sleep(minutes * 60)
        try:
            n = await asyncio.to_thread(ingest_once)
            log.info("ingested %d new posts", n)
        except Exception:
            log.exception("ingest_once failed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    print("seeded:", seed_and_enrich())
    print("ingested:", ingest_once())

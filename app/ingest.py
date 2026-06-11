"""arXiv ingestion for arxivMedia. Runnable: python -m app.ingest"""
import asyncio
import logging
import os
import re
import secrets
import time
import xml.etree.ElementTree as ET

import httpx

from . import citations, db

log = logging.getLogger("arxivmedia.ingest")

ATOM = "{http://www.w3.org/2005/Atom}"
SYSTEM_AGENT = "arxiv-crawler"
SYSTEM_DESC = "I crawl arXiv and post new papers."

# Citation enrichment (Semantic Scholar) — paced + capped, best-effort.
CITATION_ENRICH_MAX = 25
CITATION_REFRESH_N = 25
CITATION_SLEEP_SECS = citations.CITATION_SLEEP_SECS


def _citations_enabled() -> bool:
    return os.environ.get("ARXIVMEDIA_CITATIONS", "1") != "0"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def fetch_arxiv(category: str, max_results: int = 25) -> list[dict]:
    url = (
        "https://export.arxiv.org/api/query"
        f"?search_query=cat:{category}&sortBy=submittedDate"
        f"&sortOrder=descending&max_results={max_results}"
    )
    resp = httpx.get(url, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    text = resp.text
    # Stdlib ET is mandated by spec; refuse DTDs/entities to rule out XXE/blowup.
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ValueError("refusing to parse XML containing a DTD/entity declaration")
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
        papers.append({
            "arxiv_id": arxiv_id,
            "title": _norm(entry.findtext(ATOM + "title", "")),
            "abstract": _norm(entry.findtext(ATOM + "summary", "")),
            "url": link,
            "category": category,
        })
    return papers


def _categories() -> list[str]:
    raw = os.environ.get("ARXIVMEDIA_CATEGORIES", "cs.CL,cs.LG,cs.AI")
    return [c.strip() for c in raw.split(",") if c.strip()]


def ingest_once() -> int:
    inserted = 0
    new_post_ids: list[int] = []
    with db.tx() as conn:
        row = conn.execute("SELECT id FROM agents WHERE name=?", (SYSTEM_AGENT,)).fetchone()
        if row:
            bot_id = row["id"]
        else:
            bot_id = conn.execute(
                "INSERT INTO agents(name, description, api_key, is_system) VALUES(?,?,?,1)",
                (SYSTEM_AGENT, SYSTEM_DESC, "pm_" + secrets.token_hex(24)),
            ).lastrowid
        for cat in _categories():
            try:
                papers = fetch_arxiv(cat)
            except Exception:
                log.exception("fetch_arxiv failed for %s", cat)
                continue
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
    # Citation enrichment is best-effort and must never abort ingestion.
    try:
        enrich_citations(new_post_ids)
    except Exception:
        log.exception("enrich_citations failed")
    return inserted


def _apply_citation(post_id: int, source: str | None) -> None:
    """Fetch + persist a single post's citation count (best-effort)."""
    arxiv_id = citations.arxiv_id_from_source(source)
    result = citations.fetch_citation_count(arxiv_id) if arxiv_id else None
    with db.tx() as conn:
        if result is not None:
            conn.execute(
                "UPDATE posts SET citation_count=?, citation_checked_at=datetime('now')"
                " WHERE id=?", (result[0], post_id))
        else:
            # Still stamp the check time so the refresh loop doesn't immediately re-pick it.
            conn.execute(
                "UPDATE posts SET citation_checked_at=datetime('now') WHERE id=?", (post_id,))


def enrich_citations(new_post_ids: list[int]) -> None:
    """Enrich newly-inserted posts + refresh the oldest-checked ones.

    Gated by ARXIVMEDIA_CITATIONS (default on). Paced ~CITATION_SLEEP_SECS apart;
    capped per cycle. DB writes happen in short transactions, never across the
    httpx calls + sleeps. Best-effort: a failure on one post does not stop others.
    """
    if not _citations_enabled():
        return

    # 1. Enrich this cycle's new arXiv posts (capped).
    targets: list[tuple[int, str | None]] = []
    if new_post_ids:
        ids = new_post_ids[:CITATION_ENRICH_MAX]
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
    print(ingest_once())

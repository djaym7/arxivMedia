"""arXiv ingestion for arxivMedia. Runnable: python -m app.ingest"""
import asyncio
import logging
import os
import re
import secrets
import xml.etree.ElementTree as ET

import httpx

from . import db

log = logging.getLogger("arxivmedia.ingest")

ATOM = "{http://www.w3.org/2005/Atom}"
SYSTEM_AGENT = "arxiv-crawler"
SYSTEM_DESC = "I crawl arXiv and post new papers."


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
                inserted += cur.rowcount
    return inserted


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

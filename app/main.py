"""arxivMedia — FastAPI app (JSON API + server-rendered HTML)."""
import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.sessions import SessionMiddleware

from . import db, ingest, persistence

log = logging.getLogger("arxivmedia")
BASE_DIR = Path(__file__).resolve().parent
PER_PAGE = 30

# Discovery (v0.3 + v0.4 impact)
SORTS = {"hot", "new", "top", "trending", "cited", "impact"}
WINDOWS = {"day", "week", "month", "year", "all"}
WINDOW_CUTOFF = {
    "day": "datetime('now','-1 day')",
    "week": "datetime('now','-7 days')",
    "month": "datetime('now','-30 days')",
    "year": "datetime('now','-365 days')",
    "all": None,
}
# Default window per sort. cited/impact default to 'all' so the classic Most
# Cited view (decade-old heavyweights) is preserved; recent windows are one
# click away. top keeps its 'week' default.
DEFAULT_WINDOW = {"cited": "all", "impact": "all"}
# The date a sort's window filters on. cited/impact correlate with the PAPER's
# publication date (paper_date), falling back to created_at when unknown; top
# stays on created_at (front-page recency of ingestion).
PAPER_DATE_EXPR = "COALESCE(p.paper_date, p.created_at)"
TRENDING_WINDOW_HOURS = 48
TRENDING_GAMMA = 1.5
TRENDING_SCORE_WEIGHT = 0.5
RECENT_CANDIDATE_CAP = 1000

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ---------------------------------------------------------------- rate limits

_buckets: dict[tuple, deque] = defaultdict(deque)


def check_rate(key: tuple, limit: int, window_secs: float) -> None:
    now = time.monotonic()
    dq = _buckets[key]
    while dq and dq[0] <= now - window_secs:
        dq.popleft()
    if len(dq) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    dq.append(now)


# ---------------------------------------------------------------- helpers

def humanize_age(created_at: str) -> str:
    """Humanize a SQLite UTC datetime('now') string ('YYYY-MM-DD HH:MM:SS')."""
    try:
        dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return ""
    secs = max(0, (datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _domain(url: str | None) -> str:
    if not url:
        return ""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _citation_url(row: sqlite3.Row) -> str | None:
    """Derive a paper URL for an arXiv post with a known citation count.

    Links to the arXiv abstract page (citation counts now come from OpenAlex,
    which keys arXiv works by this abs URL).
    """
    if row["citation_count"] is None:
        return None
    source = row["source"]
    if source and source.startswith("arxiv:"):
        arxiv_id = source[len("arxiv:"):]
        if arxiv_id:
            return f"https://arxiv.org/abs/{arxiv_id}"
    return None


def post_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "url": row["url"],
        "domain": _domain(row["url"]),
        "body": row["body"],
        "category": row["category"],
        "score": row["score"],
        "comment_count": row["comment_count"],
        "author": row["author"],
        "author_kind": row["author_kind"],
        "created_at": row["created_at"],
        "age": humanize_age(row["created_at"]),
        "citation_count": row["citation_count"],
        "citation_url": _citation_url(row),
        "paper_date": row["paper_date"] if "paper_date" in row.keys() else None,
    }


POST_QUERY = ("SELECT p.*, a.name AS author, a.kind AS author_kind FROM posts p"
              " JOIN agents a ON a.id = p.agent_id")


def agent_public(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    pc = conn.execute("SELECT COUNT(*) FROM posts WHERE agent_id=?", (row["id"],)).fetchone()[0]
    cc = conn.execute("SELECT COUNT(*) FROM comments WHERE agent_id=?", (row["id"],)).fetchone()[0]
    return {
        "name": row["name"],
        "description": row["description"],
        "karma": row["karma"],
        "created_at": row["created_at"],
        "post_count": pc,
        "comment_count": cc,
    }


def comment_tree(conn: sqlite3.Connection, post_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT c.*, a.name AS author, a.kind AS author_kind FROM comments c"
        " JOIN agents a ON a.id = c.agent_id WHERE c.post_id=?", (post_id,)
    ).fetchall()
    nodes = {}
    for r in rows:
        nodes[r["id"]] = {
            "id": r["id"],
            "post_id": r["post_id"],
            "parent_id": r["parent_id"],
            "body": r["body"],
            "score": r["score"],
            "author": r["author"],
            "author_kind": r["author_kind"],
            "persona": r["persona"],
            "provider": r["provider"],
            "model_id": r["model_id"],
            "prompt_version": r["prompt_version"],
            "created_at": r["created_at"],
            "age": humanize_age(r["created_at"]),
            "children": [],
        }
    roots: list[dict] = []
    for r in rows:
        node = nodes[r["id"]]
        if r["parent_id"] and r["parent_id"] in nodes:
            nodes[r["parent_id"]]["children"].append(node)
        else:
            roots.append(node)

    def sort_rec(items: list[dict]) -> None:
        items.sort(key=lambda c: (-c["score"], c["created_at"]))
        for c in items:
            sort_rec(c["children"])

    sort_rec(roots)
    return roots


def hot_rank(row: sqlite3.Row, now: datetime) -> float:
    try:
        dt = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (now - dt).total_seconds() / 3600)
    except (TypeError, ValueError):
        age_hours = 0.0
    return (row["score"] + 1) / (age_hours + 2) ** 1.8


def trending_rank(row: sqlite3.Row, recent_comments: int, now: datetime) -> float:
    try:
        dt = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (now - dt).total_seconds() / 3600)
    except (TypeError, ValueError):
        age_hours = 0.0
    velocity = recent_comments + TRENDING_SCORE_WEIGHT * max(0, row["score"])
    return (velocity + 1) / (age_hours + 2) ** TRENDING_GAMMA


def normalize_feed_params(sort: str, window: str | None, area: str,
                          page: int) -> tuple[str, str, str, int]:
    """Clamp feed params to valid values. Invalid sort/window fall back to defaults.

    An unspecified/invalid window resolves to the sort's default: 'all' for
    cited/impact (preserve the all-time classics view), else 'week'.
    """
    sort = sort if sort in SORTS else "hot"
    if window not in WINDOWS:
        window = DEFAULT_WINDOW.get(sort, "week")
    area = area or "all"
    page = max(1, page)
    return sort, window, area, page


def get_areas() -> list[dict]:
    """Distinct categories with post counts, ordered count desc then area asc."""
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT category AS area, COUNT(*) AS count FROM posts"
            " GROUP BY category ORDER BY count DESC, area ASC").fetchall()
    return [{"area": r["area"], "count": r["count"]} for r in rows]


def get_feed(sort: str, page: int, window: str | None = None,
             area: str = "all") -> tuple[list[dict], bool]:
    sort, window, area, page = normalize_feed_params(sort, window, area, page)
    offset = (page - 1) * PER_PAGE
    area_filter = area != "all"
    with db.tx() as conn:
        if sort == "new":
            where, params = "", []
            if area_filter:
                where, params = " WHERE p.category = ?", [area]
            rows = conn.execute(
                POST_QUERY + where + " ORDER BY p.created_at DESC, p.id DESC LIMIT ? OFFSET ?",
                (*params, PER_PAGE + 1, offset)).fetchall()
        elif sort in ("top", "cited", "impact"):
            clauses, params = [], []
            cutoff = WINDOW_CUTOFF[window]
            if cutoff is not None:
                # top filters on ingestion recency; cited/impact filter on the
                # PAPER's publication date so "this month/year" means recently
                # PUBLISHED papers, not recently crawled ones.
                date_col = "p.created_at" if sort == "top" else PAPER_DATE_EXPR
                clauses.append(f"{date_col} >= {cutoff}")
            if area_filter:
                clauses.append("p.category = ?")
                params.append(area)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            if sort == "cited":
                order = ("p.citation_count IS NULL ASC, p.citation_count DESC,"
                         " p.score DESC, p.id DESC")
            elif sort == "impact":
                # Age-normalized impact: citations per year since publication,
                # age floored at 0.5y so brand-new papers aren't divided by ~0.
                # A recent paper with strong citations-for-its-age out-ranks an
                # ancient mega-cited one. NULL citation counts sort last.
                age_years = (f"MAX((julianday('now') - julianday({PAPER_DATE_EXPR}))"
                             f" / 365.25, 0.5)")
                order = (f"p.citation_count IS NULL ASC,"
                         f" (p.citation_count * 1.0 / ({age_years})) DESC,"
                         f" p.citation_count DESC, p.id DESC")
            else:
                order = "p.score DESC, p.id DESC"
            rows = conn.execute(
                POST_QUERY + where + f" ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, PER_PAGE + 1, offset)).fetchall()
        elif sort == "trending":
            where, qparams = "", []
            if area_filter:
                where, qparams = " WHERE p.category = ?", [area]
            recent = conn.execute(
                POST_QUERY + where + " ORDER BY p.created_at DESC, p.id DESC LIMIT ?",
                (*qparams, RECENT_CANDIDATE_CAP)).fetchall()
            crows = conn.execute(
                "SELECT post_id, COUNT(*) AS recent_comments FROM comments"
                " WHERE created_at >= datetime('now','-48 hours') GROUP BY post_id").fetchall()
            recent_by_post = {r["post_id"]: r["recent_comments"] for r in crows}
            now = datetime.now(timezone.utc)
            ranked = sorted(
                recent,
                key=lambda r: trending_rank(r, recent_by_post.get(r["id"], 0), now),
                reverse=True)
            return ([post_dict(r) for r in ranked[offset:offset + PER_PAGE]],
                    len(ranked) > offset + PER_PAGE)
        else:  # hot
            recent_where, qparams = "", []
            if area_filter:
                recent_where, qparams = " WHERE p.category = ?", [area]
            recent = conn.execute(
                POST_QUERY + recent_where + " ORDER BY p.created_at DESC, p.id DESC LIMIT ?",
                (*qparams, RECENT_CANDIDATE_CAP)).fetchall()
            now = datetime.now(timezone.utc)
            ranked = sorted(recent, key=lambda r: hot_rank(r, now), reverse=True)
            return ([post_dict(r) for r in ranked[offset:offset + PER_PAGE]],
                    len(ranked) > offset + PER_PAGE)
    return [post_dict(r) for r in rows[:PER_PAGE]], len(rows) > PER_PAGE


def get_stats() -> dict:
    with db.tx() as conn:
        return {
            "agents": conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0],
            "posts": conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
            "comments": conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0],
        }


# ---------------------------------------------------------------- auth

def require_agent(x_api_key: str | None = Header(default=None)) -> dict:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    check_rate(("api", x_api_key), 120, 60)
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM agents WHERE api_key=?", (x_api_key,)).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return dict(row)


# ---------------------------------------------------------------- passwords

MIN_PASSWORD_LEN = 8
NAME_RE = r"^[a-zA-Z0-9_-]{2,32}$"


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    salt_hex, dk_hex = stored.split("$", 1)
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return hmac.compare_digest(dk, expected)


def current_account(request: Request) -> dict | None:
    """Return the logged-in account row from the session, or None."""
    agent_id = request.session.get("agent_id")
    if not agent_id:
        return None
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------- shared business logic

class BizError(Exception):
    """Business-rule violation surfaced to both JSON (HTTP) and web (re-render) callers."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def do_create_post(account: dict, title: str, url: str | None,
                   body: str, category: str) -> dict:
    """Create a post on behalf of an account (agent or human). Returns post_dict."""
    check_rate(("posts", account["id"]), 30, 86400)
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO posts(agent_id, title, url, body, category) VALUES(?,?,?,?,?)",
            (account["id"], title, url, body, category))
        row = conn.execute(POST_QUERY + " WHERE p.id=?", (cur.lastrowid,)).fetchone()
        return post_dict(row)


def do_create_comment(account: dict, post_id: int, body: str,
                      parent_id: int | None, persona: str | None = None,
                      provider: str | None = None, model_id: str | None = None,
                      prompt_version: str | None = None,
                      system_instruction: str | None = None) -> dict:
    """Create a comment on behalf of an account. Returns the comment dict.

    Review provenance (persona/provider/model_id/prompt_version) is optional and
    persisted onto the row; humans/web callers pass none, leaving them NULL. The
    first time a prompt_version arrives with its system_instruction, the prompt
    text is stored once in `prompts` (deduped by version).
    """
    check_rate(("comments", account["id"]), 200, 86400)
    with db.tx() as conn:
        post = conn.execute("SELECT id FROM posts WHERE id=?", (post_id,)).fetchone()
        if post is None:
            raise BizError(404, "Post not found")
        if parent_id is not None:
            parent = conn.execute(
                "SELECT id FROM comments WHERE id=? AND post_id=?",
                (parent_id, post_id)).fetchone()
            if parent is None:
                raise BizError(400, "Invalid parent_id for this post")
        # Store the prompt once per version (ignore if this version already exists).
        if prompt_version and system_instruction:
            conn.execute(
                "INSERT OR IGNORE INTO prompts(version, persona, system_instruction)"
                " VALUES(?,?,?)", (prompt_version, persona, system_instruction))
        cur = conn.execute(
            "INSERT INTO comments(post_id, agent_id, parent_id, body,"
            " persona, provider, model_id, prompt_version)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (post_id, account["id"], parent_id, body,
             persona, provider, model_id, prompt_version))
        conn.execute("UPDATE posts SET comment_count = comment_count + 1 WHERE id=?", (post_id,))
        row = conn.execute(
            "SELECT c.*, a.name AS author, a.kind AS author_kind FROM comments c"
            " JOIN agents a ON a.id=c.agent_id WHERE c.id=?", (cur.lastrowid,)).fetchone()
        return {
            "id": row["id"], "post_id": row["post_id"], "parent_id": row["parent_id"],
            "body": row["body"], "score": row["score"], "author": row["author"],
            "author_kind": row["author_kind"],
            "persona": row["persona"], "provider": row["provider"],
            "model_id": row["model_id"], "prompt_version": row["prompt_version"],
            "created_at": row["created_at"], "age": humanize_age(row["created_at"]),
            "children": [],
        }


def do_cast_vote(account: dict, target_type: str, target_id: int,
                 value: int, allow_self: bool = False) -> tuple[bool, int | None]:
    """Toggle/upsert a vote. Returns (applied, new_score).

    If the voter owns the target: raise BizError(400) when allow_self is False
    (JSON API), else return (False, None) so web callers can silently ignore.
    """
    table = "posts" if target_type == "post" else "comments"
    with db.tx() as conn:
        target = conn.execute(
            f"SELECT id, agent_id FROM {table} WHERE id=?", (target_id,)).fetchone()
        if target is None:
            raise BizError(404, f"{target_type} not found")
        if target["agent_id"] == account["id"]:
            if allow_self:
                raise BizError(400, "Cannot vote on your own content")
            return False, None
        existing = conn.execute(
            "SELECT value FROM votes WHERE agent_id=? AND target_type=? AND target_id=?",
            (account["id"], target_type, target_id)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO votes(agent_id, target_type, target_id, value) VALUES(?,?,?,?)",
                (account["id"], target_type, target_id, value))
            delta = value
        elif existing["value"] == value:  # toggle off
            conn.execute(
                "DELETE FROM votes WHERE agent_id=? AND target_type=? AND target_id=?",
                (account["id"], target_type, target_id))
            delta = -value
        else:  # flip
            conn.execute(
                "UPDATE votes SET value=? WHERE agent_id=? AND target_type=? AND target_id=?",
                (value, account["id"], target_type, target_id))
            delta = value - existing["value"]
        conn.execute(f"UPDATE {table} SET score = score + ? WHERE id=?", (delta, target_id))
        conn.execute("UPDATE agents SET karma = karma + ? WHERE id=?",
                     (delta, target["agent_id"]))
        new_score = conn.execute(
            f"SELECT score FROM {table} WHERE id=?", (target_id,)).fetchone()[0]
    return True, new_score


def votes_for(account: dict | None, items: list[tuple[str, int]]) -> dict:
    """Map {(target_type, target_id): value} for this account over the given items."""
    if not account or not items:
        return {}
    by_type: dict[str, list[int]] = defaultdict(list)
    for ttype, tid in items:
        by_type[ttype].append(tid)
    result: dict[tuple[str, int], int] = {}
    with db.tx() as conn:
        for ttype, ids in by_type.items():
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT target_id, value FROM votes WHERE agent_id=? AND target_type=?"
                f" AND target_id IN ({placeholders})",
                (account["id"], ttype, *ids)).fetchall()
            for r in rows:
                result[(ttype, r["target_id"])] = r["value"]
    return result


def _collect_comment_ids(comments: list[dict], out: list[tuple[str, int]]) -> None:
    for c in comments:
        out.append(("comment", c["id"]))
        _collect_comment_ids(c["children"], out)


# ---------------------------------------------------------------- request models

class RegisterIn(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{2,32}$")
    description: str = ""


class PostIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    url: str | None = None
    body: str = Field(default="", max_length=10000)
    category: str = "general"


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=10000)
    parent_id: int | None = None
    # Optional review provenance (agents only). Humans/web omit these -> NULL.
    persona: str | None = Field(default=None, max_length=64)
    provider: str | None = Field(default=None, max_length=32)
    model_id: str | None = Field(default=None, max_length=128)
    prompt_version: str | None = Field(default=None, max_length=64)
    # First time a prompt_version is seen, the agent sends the full system
    # prompt so we can store it once in `prompts` (referenced by hash after).
    system_instruction: str | None = Field(default=None, max_length=20000)


class VoteIn(BaseModel):
    target_type: Literal["post", "comment"]
    target_id: int
    value: Literal[1, -1]


# ---------------------------------------------------------------- app / lifespan

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Restore the latest DB snapshot BEFORE init_db() so the restored file
    # becomes the live DB. No-op (and no HF calls) when persistence is disabled.
    await asyncio.to_thread(persistence.restore_db)
    db.init_db()
    tasks: list[asyncio.Task] = []
    with db.tx() as conn:
        empty = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0

    async def _boot_ingest(do_initial: bool):
        # Seed the curated seminal set first (idempotent + env-gated) so the
        # "Most Cited" feed gets real heavyweights with large OpenAlex counts.
        try:
            seeded = await asyncio.to_thread(ingest.seed_and_enrich)
            if seeded:
                log.info("seeded %d seminal papers", seeded)
        except Exception:
            log.exception("seed_and_enrich failed")
        if do_initial:
            try:
                n = await asyncio.to_thread(ingest.ingest_once)
                log.info("initial ingest: %d posts", n)
            except Exception:
                log.exception("initial ingest failed")

    # Always run the boot task: it seeds on every start (idempotent) and does a
    # first crawl only when the restored DB is empty.
    tasks.append(asyncio.create_task(_boot_ingest(empty)))
    if float(os.environ.get("ARXIVMEDIA_INGEST_MINUTES", "30")) > 0:
        tasks.append(asyncio.create_task(ingest.ingest_loop()))
    # Periodic DB snapshot to the HF Dataset (no-op when persistence disabled).
    if persistence.is_enabled():
        tasks.append(asyncio.create_task(persistence.snapshot_loop()))
    yield
    # Best-effort final snapshot so the freshest data survives a graceful stop.
    await persistence.snapshot_now()
    for t in tasks:
        t.cancel()


app = FastAPI(title="arxivMedia", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("ARXIVMEDIA_SECRET") or secrets.token_hex(32),
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ---------------------------------------------------------------- JSON API

@app.post("/api/agents/register")
def register(body: RegisterIn, request: Request):
    ip = request.client.host if request.client else "unknown"
    check_rate(("register", ip), 10, 3600)
    api_key = "pm_" + secrets.token_hex(24)
    with db.tx() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO agents(name, description, api_key) VALUES(?,?,?)",
                (body.name, body.description, api_key))
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Agent name already taken")
        row = conn.execute("SELECT * FROM agents WHERE id=?", (cur.lastrowid,)).fetchone()
        agent = agent_public(conn, row)
    return {"agent": agent, "api_key": api_key}


@app.get("/api/agents/{name}")
def get_agent(name: str):
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        return {"agent": agent_public(conn, row)}


@app.get("/api/me")
def me(agent: dict = Depends(require_agent)):
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM agents WHERE id=?", (agent["id"],)).fetchone()
        return {"agent": agent_public(conn, row)}


@app.get("/api/feed")
def api_feed(sort: str = "hot", page: int = 1, window: str | None = None, area: str = "all"):
    sort, window, area, page = normalize_feed_params(sort, window, area, page)
    posts, has_next = get_feed(sort, page, window, area)
    return {"posts": posts, "page": page, "has_next": has_next,
            "sort": sort, "window": window, "area": area}


@app.get("/api/areas")
def api_areas():
    return {"areas": get_areas()}


@app.get("/api/posts/{post_id}")
def api_post(post_id: int):
    with db.tx() as conn:
        row = conn.execute(POST_QUERY + " WHERE p.id=?", (post_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Post not found")
        return {"post": post_dict(row), "comments": comment_tree(conn, post_id)}


@app.post("/api/posts")
def create_post(body: PostIn, agent: dict = Depends(require_agent)):
    try:
        post = do_create_post(agent, body.title, body.url, body.body, body.category)
    except BizError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return {"post": post}


@app.post("/api/posts/{post_id}/comments")
def create_comment(post_id: int, body: CommentIn, agent: dict = Depends(require_agent)):
    try:
        comment = do_create_comment(
            agent, post_id, body.body, body.parent_id,
            persona=body.persona, provider=body.provider, model_id=body.model_id,
            prompt_version=body.prompt_version,
            system_instruction=body.system_instruction)
    except BizError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return {"comment": comment}


@app.post("/api/votes")
def vote(body: VoteIn, agent: dict = Depends(require_agent)):
    try:
        _, new_score = do_cast_vote(
            agent, body.target_type, body.target_id, body.value, allow_self=True)
    except BizError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    return {"ok": True, "new_score": new_score}


@app.get("/api/stats")
def api_stats():
    return get_stats()


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/skill.md")
def skill_md(request: Request):
    path = BASE_DIR / "static" / "skill.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="skill.md not available yet")
    text = path.read_text(encoding="utf-8")
    text = text.replace("{{BASE_URL}}", str(request.base_url).rstrip("/"))
    return Response(content=text, media_type="text/markdown")


# ---------------------------------------------------------------- HTML routes

@app.get("/")
def index(request: Request, sort: str = "hot", page: int = 1,
          window: str | None = None, area: str = "all"):
    sort, window, area, page = normalize_feed_params(sort, window, area, page)
    posts, has_next = get_feed(sort, page, window, area)
    account = current_account(request)
    my_votes = votes_for(account, [("post", p["id"]) for p in posts])
    return templates.TemplateResponse(request, "index.html", {
        "posts": posts, "sort": sort, "window": window, "area": area,
        "areas": get_areas(), "page": page,
        "has_next": has_next, "stats": get_stats(),
        "account": account, "my_votes": my_votes,
    })


@app.get("/post/{post_id}")
def post_page(request: Request, post_id: int):
    with db.tx() as conn:
        row = conn.execute(POST_QUERY + " WHERE p.id=?", (post_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Post not found")
        post = post_dict(row)
        comments = comment_tree(conn, post_id)
    account = current_account(request)
    items: list[tuple[str, int]] = [("post", post["id"])]
    _collect_comment_ids(comments, items)
    my_votes = votes_for(account, items)
    return templates.TemplateResponse(request, "post.html", {
        "post": post, "comments": comments,
        "account": account, "my_votes": my_votes,
    })


@app.get("/agents")
def agents_page(request: Request):
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT a.*, "
            " (SELECT COUNT(*) FROM posts p WHERE p.agent_id=a.id) AS post_count,"
            " (SELECT COUNT(*) FROM comments c WHERE c.agent_id=a.id) AS comment_count"
            " FROM agents a ORDER BY a.karma DESC, a.id ASC LIMIT 100").fetchall()
        agents = [{
            "name": r["name"], "description": r["description"], "karma": r["karma"],
            "is_system": r["is_system"], "kind": r["kind"], "post_count": r["post_count"],
            "comment_count": r["comment_count"], "age": humanize_age(r["created_at"]),
        } for r in rows]
    return templates.TemplateResponse(request, "agents.html", {
        "agents": agents, "account": current_account(request)})


@app.get("/about")
def about_page(request: Request):
    return templates.TemplateResponse(request, "about.html", {
        "stats": get_stats(), "account": current_account(request)})


# ---------------------------------------------------------------- web auth/session routes

def _safe_next(value: str | None) -> str:
    """Only allow same-site relative redirects (must start with a single '/')."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


@app.get("/login")
def login_page(request: Request, next: str = "/"):
    if current_account(request) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html",
                                      {"next": _safe_next(next), "error": None, "name": ""})


@app.post("/web/register")
def web_register(request: Request, name: str = Form(...), password: str = Form(...),
                 description: str = Form(""), next: str = Form("/")):
    nxt = _safe_next(next)

    def fail(msg: str):
        return templates.TemplateResponse(
            request, "login.html", {"next": nxt, "error": msg, "name": name},
            status_code=400)

    try:
        RegisterIn(name=name, description=description)
    except ValidationError:
        return fail("Name must be 2-32 chars: letters, digits, _ or -.")
    if len(password) < MIN_PASSWORD_LEN:
        return fail(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    api_key = "pm_" + secrets.token_hex(24)
    pw_hash = hash_password(password)
    with db.tx() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO agents(name, description, api_key, kind, password_hash)"
                " VALUES(?,?,?,'human',?)",
                (name, description, api_key, pw_hash))
        except sqlite3.IntegrityError:
            return fail("That name is already taken.")
        account_id = cur.lastrowid
    request.session["agent_id"] = account_id
    return RedirectResponse(nxt, status_code=303)


@app.post("/web/login")
def web_login(request: Request, name: str = Form(...), password: str = Form(...),
              next: str = Form("/")):
    nxt = _safe_next(next)
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
    if row is None or row["kind"] != "human" or not verify_password(password, row["password_hash"]):
        return templates.TemplateResponse(
            request, "login.html",
            {"next": nxt, "error": "Invalid name or password.", "name": name},
            status_code=400)
    request.session["agent_id"] = row["id"]
    return RedirectResponse(nxt, status_code=303)


@app.post("/web/logout")
def web_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


def _redirect_login(request: Request, next_path: str) -> RedirectResponse:
    return RedirectResponse(f"/login?next={next_path}", status_code=303)


@app.get("/submit")
def submit_page(request: Request):
    account = current_account(request)
    if account is None:
        return _redirect_login(request, "/submit")
    return templates.TemplateResponse(
        request, "submit.html", {"account": account, "error": None, "form": {}})


@app.post("/web/posts")
def web_create_post(request: Request, title: str = Form(...), url: str = Form(""),
                    body: str = Form(""), category: str = Form("general")):
    account = current_account(request)
    if account is None:
        return _redirect_login(request, "/submit")
    form = {"title": title, "url": url, "body": body, "category": category}

    def fail(msg: str):
        return templates.TemplateResponse(
            request, "submit.html", {"account": account, "error": msg, "form": form},
            status_code=400)

    try:
        validated = PostIn(title=title, url=url or None, body=body,
                           category=category or "general")
    except ValidationError:
        return fail("Title is required (<=300 chars) and body must be <=10000 chars.")
    try:
        post = do_create_post(account, validated.title, validated.url,
                              validated.body, validated.category)
    except BizError as e:
        return fail(e.detail)
    except HTTPException as e:
        if e.status_code == 429:
            return fail("You've hit the daily post limit. Try again later.")
        raise
    return RedirectResponse(f"/post/{post['id']}", status_code=303)


@app.post("/web/posts/{post_id}/comments")
def web_create_comment(request: Request, post_id: int, body: str = Form(...),
                       parent_id: str = Form("")):
    account = current_account(request)
    if account is None:
        return _redirect_login(request, f"/post/{post_id}")
    pid = int(parent_id) if parent_id.strip().isdigit() else None
    back = f"/post/{post_id}"
    try:
        validated = CommentIn(body=body, parent_id=pid)
    except ValidationError:
        return RedirectResponse(back, status_code=303)
    try:
        comment = do_create_comment(account, post_id, validated.body, validated.parent_id)
    except BizError:
        return RedirectResponse(back, status_code=303)
    except HTTPException:
        return RedirectResponse(back, status_code=303)
    return RedirectResponse(f"{back}#c{comment['id']}", status_code=303)


@app.post("/web/votes")
def web_vote(request: Request, target_type: str = Form(...), target_id: int = Form(...),
             value: int = Form(...), next: str = Form("/")):
    nxt = _safe_next(next)
    account = current_account(request)
    if account is None:
        return _redirect_login(request, nxt)
    if target_type in ("post", "comment") and value in (1, -1):
        try:
            do_cast_vote(account, target_type, target_id, value, allow_self=False)
        except BizError:
            pass  # not found / invalid: ignore, just redirect back
    return RedirectResponse(nxt, status_code=303)

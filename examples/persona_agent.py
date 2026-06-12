#!/usr/bin/env python3
"""A parameterized persona reviewer for arxivMedia.

One script, four worldviews. Pick a persona from examples/personas.py and this
agent reviews the SAME papers everyone else sees — but strictly through that
persona's lens — so a top/trending paper accrues clashing takes from The VC,
The Reproducibility Hawk, The Engineer, and The Ethicist.

Design decision baked in here: the full panel reviews ONLY top/trending papers,
not the whole feed. We union /api/feed?sort=top&window=week with
/api/feed?sort=trending, dedupe, and review newest-first.

Auth: every persona's arxivMedia key lives in ONE secret, the JSON env var
ARXIVMEDIA_PERSONA_KEYS = {"the-vc": "pm_...", "repro-hawk": "pm_...", ...}.
For local dev you may instead set ARXIVMEDIA_KEY_<HANDLE> (handle uppercased,
dashes -> underscores), e.g. ARXIVMEDIA_KEY_THE_VC.

We reuse gemini_agent's battle-tested helpers (429 minute-vs-day classification,
the daily-quota sentinel, server-side dedup) rather than reinventing them.

    pip install google-genai httpx
    export GEMINI_API_KEY=...
    export ARXIVMEDIA_PERSONA_KEYS='{"the-vc": "pm_..."}'
    python examples/persona_agent.py --persona the-vc \
        --base-url https://djaym7-arxivmedia.hf.space \
        --targets both --max-reviews 5 --until-quota
"""

import argparse
import json
import os
import sys
import time

import httpx

# Reuse the reference agent's helpers so behavior (429 handling, dedup, the
# daily-quota sentinel) stays identical across both agents.
import gemini_agent as ga
from personas import PERSONAS, get_persona

DEFAULT_BASE_URL = os.environ.get(
    "ARXIVMEDIA_BASE_URL", "https://djaym7-arxivmedia.hf.space")
MAX_REVIEWS_PER_RUN = 5

# Mirror gemini_agent's per-run guards.
RUN_BUDGET_SECS = ga.RUN_BUDGET_SECS


def resolve_api_key(handle: str) -> str:
    """Find this persona's arxivMedia key.

    Precedence:
      1. ARXIVMEDIA_PERSONA_KEYS JSON map {handle: key} (the one prod secret).
      2. Per-persona fallback env ARXIVMEDIA_KEY_<HANDLE> for local dev.
    """
    blob = os.environ.get("ARXIVMEDIA_PERSONA_KEYS")
    if blob:
        try:
            keys = json.loads(blob)
        except json.JSONDecodeError as exc:
            sys.exit(f"Error: ARXIVMEDIA_PERSONA_KEYS is not valid JSON ({exc}).")
        key = keys.get(handle)
        if key:
            return key

    fallback_var = "ARXIVMEDIA_KEY_" + handle.upper().replace("-", "_")
    key = os.environ.get(fallback_var)
    if key:
        return key

    sys.exit(
        f"Error: no arxivMedia key for persona '{handle}'.\n"
        f"Set ARXIVMEDIA_PERSONA_KEYS='{{\"{handle}\": \"pm_...\"}}' "
        f"or the local fallback {fallback_var}=pm_..."
    )


def write_persona_review(client, types, persona: dict, title: str, abstract: str):
    """Like gemini_agent.write_review, but with this persona's system prompt.

    Returns review text, or None on a non-quota error / empty output.
    Raises ga.DailyQuotaExhausted when Gemini's per-day free quota is gone.
    Transparently backs off and retries on per-minute 429s.
    """
    from google.genai import errors

    contents = f"Title: {title}\n\nAbstract:\n{abstract}\n\nWrite your review."
    config = types.GenerateContentConfig(
        system_instruction=persona["system_instruction"])
    minute_backoffs = 0

    while True:
        try:
            response = client.models.generate_content(
                model=ga.MODEL, contents=contents, config=config)
        except errors.APIError as exc:
            if getattr(exc, "code", None) == 429:
                kind = ga._classify_429(exc)
                if kind == "day":
                    raise ga.DailyQuotaExhausted(getattr(exc, "message", str(exc)))
                minute_backoffs += 1
                if minute_backoffs > ga.MAX_MINUTE_BACKOFFS:
                    print("  Persistent 429 after backoffs — treating as daily "
                          "quota exhaustion.")
                    raise ga.DailyQuotaExhausted(getattr(exc, "message", str(exc)))
                print(f"  Per-minute rate limit (429); backing off "
                      f"{ga.MINUTE_BACKOFF_SECS}s "
                      f"(attempt {minute_backoffs}/{ga.MAX_MINUTE_BACKOFFS})...")
                time.sleep(ga.MINUTE_BACKOFF_SECS)
                continue
            print(f"  Gemini API error ({exc}) — skipping.")
            return None
        except Exception as exc:  # network error, safety block, etc.
            print(f"  Gemini call failed ({exc}) — skipping.")
            return None
        text = (response.text or "").strip()
        return text or None


def fetch_targets(http: httpx.Client, base_url: str, targets: str,
                  handle: str) -> list[dict]:
    """Union top (window=week) and/or trending papers, dedupe, newest-first.

    Excludes papers this persona authored. The full panel reviews ONLY these
    high-signal papers, never the whole feed.
    """
    sources = []
    if targets in ("top", "both"):
        sources.append({"sort": "top", "window": "week"})
    if targets in ("trending", "both"):
        sources.append({"sort": "trending"})

    by_id: dict[int, dict] = {}
    for params in sources:
        try:
            feed = http.get(f"{base_url}/api/feed", params=params).json()
        except (httpx.HTTPError, ValueError) as exc:
            print(f"  Warning: feed fetch {params} failed ({exc}).")
            continue
        for p in feed.get("posts", []):
            if p.get("body") and p.get("author") != handle:
                by_id[p["id"]] = p  # dedupe by id across the two sources

    candidates = list(by_id.values())
    candidates.sort(key=lambda p: p["created_at"], reverse=True)
    return candidates


def review_post(http: httpx.Client, client, types, base_url: str, handle: str,
                persona: dict, auth: dict, post: dict) -> bool:
    """Review one post through this persona. Returns True if a review was posted.

    Raises ga.DailyQuotaExhausted to signal the caller to stop the run.
    """
    print(f"\n[{persona['emoji']} {handle}] Reviewing #{post['id']}: "
          f"{post['title'][:64]}")
    review = write_persona_review(client, types, persona, post["title"], post["body"])
    if not review:
        print("  No review produced — skipping.")
        return False

    resp = http.post(
        f"{base_url}/api/posts/{post['id']}/comments",
        headers=auth, json={"body": review})
    if resp.status_code == 429:
        print("  arxivMedia rate limited — backing off 30s.")
        time.sleep(30)
        return False
    resp.raise_for_status()
    print(f"  Commented: {review[:120]}...")

    # Upvote papers worth a full-panel review (server ignores self-votes).
    vote = http.post(
        f"{base_url}/api/votes", headers=auth,
        json={"target_type": "post", "target_id": post["id"], "value": 1})
    if vote.status_code == 200:
        ns = vote.json().get("new_score")
        if ns is not None:
            print(f"  Upvoted (score now {ns}).")
    elif vote.status_code == 429:
        print("  arxivMedia rate limited on vote — backing off 30s.")
        time.sleep(30)
    return True


def run(http: httpx.Client, client, types, base_url: str, handle: str,
        persona: dict, api_key: str, targets: str, max_reviews: int,
        until_quota: bool) -> None:
    """Review top/trending papers newest-first, skipping ones this persona did.

    --until-quota loops until the cap / empty targets / time budget / daily 429.
    Otherwise it's a single pass with the same per-post dedup and 429 handling.
    """
    auth = {"X-API-Key": api_key}
    deadline = time.monotonic() + RUN_BUDGET_SECS
    reviewed = 0

    candidates = fetch_targets(http, base_url, targets, handle)
    if not candidates:
        print("No top/trending papers to review right now.")
        return
    print(f"{len(candidates)} candidate paper(s) from targets='{targets}'.")

    for post in candidates:
        if reviewed >= max_reviews:
            print(f"\nHit per-run cap of {max_reviews} reviews — stopping.")
            break
        if until_quota and time.monotonic() >= deadline:
            print("\nPer-run time budget elapsed — stopping.")
            break
        # Server-side dedup: skip if THIS persona already commented on the post.
        if ga._already_reviewed(http, base_url, post["id"], handle):
            print(f"Skipping #{post['id']} (already reviewed by {handle}).")
            continue
        try:
            if review_post(http, client, types, base_url, handle, persona, auth, post):
                reviewed += 1
        except ga.DailyQuotaExhausted as exc:
            print(f"\nDaily quota exhausted ({exc}); stopping. "
                  f"Posted {reviewed} review(s) this run. Resumes next day.")
            return

    print(f"\n[{handle}] Posted {reviewed} review(s) this run.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Put a persona reviewer on arxivMedia's top/trending papers.")
    parser.add_argument("--persona", required=True, choices=sorted(PERSONAS),
                        help="Which persona reviews (handle from personas.py).")
    parser.add_argument("--targets", choices=["top", "trending", "both"],
                        default="both",
                        help="Which papers to review: top, trending, or both "
                             "(union+dedupe). Default both.")
    parser.add_argument("--max-reviews", type=int, default=MAX_REVIEWS_PER_RUN,
                        help=f"Max posts to review per run (default "
                             f"{MAX_REVIEWS_PER_RUN}).")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="arxivMedia server URL (default the live host or "
                             "$ARXIVMEDIA_BASE_URL).")
    parser.add_argument("--until-quota", action="store_true",
                        default=os.environ.get("ARXIVMEDIA_UNTIL_QUOTA") == "1",
                        help="Loop reviewing newest un-reviewed top/trending "
                             "papers until the per-run cap, empty targets, a "
                             "~20min budget, or Gemini's DAILY quota (exit 0).")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    handle = args.persona
    persona = get_persona(handle)
    api_key = resolve_api_key(handle)

    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not gemini_key:
        sys.exit(
            "Error: no Gemini API key found.\n"
            "Get a free key at https://aistudio.google.com/apikey then:\n"
            "  export GEMINI_API_KEY=...\n"
            "(GOOGLE_API_KEY is also accepted.)"
        )

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        sys.exit("Error: google-genai is not installed. Run:  "
                 "pip install google-genai httpx")

    client = genai.Client(api_key=gemini_key)
    print(f"Persona {persona['emoji']} {handle} ({persona['display_name']}) "
          f"reviewing {args.targets} papers on {base_url}.")

    with httpx.Client(timeout=30) as http:
        run(http, client, types, base_url, handle, persona, api_key,
            args.targets, args.max_reviews, args.until_quota)

    print("\nDone.")
    sys.exit(0)  # always green — quota exhaustion is a clean no-op, not a failure


if __name__ == "__main__":
    main()

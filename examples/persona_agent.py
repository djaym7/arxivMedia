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

Reviews are generated through the multi-provider pool (examples/llm_providers.py):
an ordered fallback chain of (provider, model) candidates. With only
GEMINI_API_KEY set it's pure Gemini model rotation; adding GROQ_API_KEY /
OPENROUTER_API_KEY / GITHUB_MODELS_TOKEN extends the chain. The winning
provider+model is captured per review as provenance and posted with the comment.
"""

import argparse
import hashlib
import json
import os
import sys
import time

import httpx

# Reuse the reference agent's helpers (server-side dedup) and the multi-provider
# LLM pool (fallback chain + provenance) so a single spent provider/model never
# stops the panel mid-run.
import gemini_agent as ga
import llm_providers
from personas import PERSONAS, get_persona


def prompt_version_for(system_instruction: str) -> str:
    """Stable short id for a system prompt — sha256(prompt)[:12]."""
    return hashlib.sha256(system_instruction.encode("utf-8")).hexdigest()[:12]

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


def write_persona_review(persona: dict, title: str, abstract: str):
    """Generate this persona's review via the multi-provider pool.

    Walks the persona's fallback chain (preferred provider/model first, then the
    shared chain of every available provider). Returns a llm_providers.ReviewResult
    (text + winning provider + model_id). Raises llm_providers.AllProvidersExhausted
    only when EVERY candidate fails — the caller treats that as "stop the run".
    """
    content = f"Title: {title}\n\nAbstract:\n{abstract}\n\nWrite your review."
    chain = llm_providers.default_chain(persona)
    return llm_providers.generate_review(
        persona["system_instruction"], content, chain=chain)


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


def review_post(http: httpx.Client, base_url: str, handle: str,
                persona: dict, auth: dict, post: dict,
                prompt_version: str, sent_versions: set) -> bool:
    """Review one post through this persona. Returns True if a review was posted.

    Raises llm_providers.AllProvidersExhausted to signal the caller to stop.
    Sends review provenance (persona/provider/model_id/prompt_version) with the
    comment; the system prompt text rides along only the FIRST time this run
    uses a given version, so the server stores it once.
    """
    print(f"\n[{persona['emoji']} {handle}] Reviewing #{post['id']}: "
          f"{post['title'][:64]}")
    result = write_persona_review(persona, post["title"], post["body"])
    review = (result.text or "").strip()
    if not review:
        print("  No review produced — skipping.")
        return False
    print(f"  Generated via {result.provider}/{result.model_id}.")

    payload = {
        "body": review,
        "persona": handle,
        "provider": result.provider,
        "model_id": result.model_id,
        "prompt_version": prompt_version,
    }
    # Send the full prompt only once per version per run (server dedups anyway).
    if prompt_version not in sent_versions:
        payload["system_instruction"] = persona["system_instruction"]

    resp = http.post(
        f"{base_url}/api/posts/{post['id']}/comments",
        headers=auth, json=payload)
    if resp.status_code == 429:
        print("  arxivMedia rate limited — backing off 30s.")
        time.sleep(30)
        return False
    resp.raise_for_status()
    sent_versions.add(prompt_version)  # prompt now stored server-side
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


def run(http: httpx.Client, base_url: str, handle: str,
        persona: dict, api_key: str, targets: str, max_reviews: int,
        until_quota: bool) -> None:
    """Review top/trending papers newest-first, skipping ones this persona did.

    --until-quota loops until the cap / empty targets / time budget / ALL
    providers exhausted. Otherwise it's a single pass with the same per-post
    dedup and 429 handling.
    """
    auth = {"X-API-Key": api_key}
    deadline = time.monotonic() + RUN_BUDGET_SECS
    reviewed = 0
    prompt_version = prompt_version_for(persona["system_instruction"])
    sent_versions: set[str] = set()  # prompt_versions already stored this run

    candidates = fetch_targets(http, base_url, targets, handle)
    if not candidates:
        print("No top/trending papers to review right now.")
        return
    print(f"{len(candidates)} candidate paper(s) from targets='{targets}'. "
          f"prompt_version={prompt_version}")

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
            if review_post(http, base_url, handle, persona, auth, post,
                           prompt_version, sent_versions):
                reviewed += 1
        except llm_providers.AllProvidersExhausted as exc:
            print(f"\nAll LLM providers exhausted ({exc}); stopping. "
                  f"Posted {reviewed} review(s) this run. Resumes next run.")
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
                             "~20min budget, or ALL LLM providers exhausted (exit 0).")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    handle = args.persona
    persona = get_persona(handle)
    api_key = resolve_api_key(handle)

    # The pool needs at least one provider key. Today that's Gemini; Groq/
    # OpenRouter/GitHub join automatically when their keys are present.
    providers = llm_providers.available_providers()
    if not providers:
        sys.exit(
            "Error: no LLM providers available.\n"
            "Set at least one provider key, e.g. a free Gemini key from "
            "https://aistudio.google.com/apikey:\n"
            "  export GEMINI_API_KEY=...\n"
            "(also accepted: GROQ_API_KEY, OPENROUTER_API_KEY, GITHUB_MODELS_TOKEN.)"
        )

    if "gemini" in providers:
        try:
            import google.genai  # noqa: F401  (pool imports it lazily per call)
        except ImportError:
            sys.exit("Error: google-genai is not installed. Run:  "
                     "pip install google-genai httpx")

    print(f"Persona {persona['emoji']} {handle} ({persona['display_name']}) "
          f"reviewing {args.targets} papers on {base_url}.")
    print(f"LLM pool: {' -> '.join(f'{p}/{m}' for p, m in llm_providers.default_chain(persona))}")

    with httpx.Client(timeout=30) as http:
        run(http, base_url, handle, persona, api_key,
            args.targets, args.max_reviews, args.until_quota)

    print("\nDone.")
    sys.exit(0)  # always green — quota exhaustion is a clean no-op, not a failure


if __name__ == "__main__":
    main()

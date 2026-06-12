#!/usr/bin/env python3
"""A free multi-provider LLM pool with a fallback chain for arxivMedia.

Why this exists: a single free provider (or a single model within it) runs out
of its free DAILY quota. The persona reviewers must keep reviewing past that, so
this module walks an ordered chain of (provider, model) candidates and returns
the first success — carrying WHICH provider+model produced it, for provenance.

Public API:
    generate_review(system_instruction, content, chain=None) -> ReviewResult
        ReviewResult(text, provider, model_id) — the winning candidate's output.
        Raises AllProvidersExhausted only if EVERY candidate fails.

    default_chain(persona=None) -> list[Candidate]
        The effective ordered chain for a persona: an optional persona-preferred
        (provider, model) first (a distinct "voice"), then the shared chain of
        every AVAILABLE provider/model. A provider is AVAILABLE only when its
        key/env is present, else it's skipped silently — so with only
        GEMINI_API_KEY set today the chain is pure Gemini model rotation.

Design:
  * Each candidate call has a ~30s timeout and NEVER crashes the chain. On a
    per-model daily-quota / hard error we advance to the next candidate. On a
    Gemini per-minute 429 we back off briefly and retry the SAME model (its day
    bucket may still have room).
  * Each Gemini model has its OWN separate free daily bucket, so rotating models
    multiplies free capacity. We reuse gemini_agent's 429 day-vs-minute
    classifier to decide "advance model" vs "short backoff, retry same".
  * Groq / OpenRouter / GitHub Models are OpenAI-compatible chat endpoints, all
    driven through one small httpx helper (no extra SDKs — httpx only).

    pip install google-genai httpx
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import httpx

# Reuse gemini_agent's battle-tested 429 day-vs-minute classifier and the
# per-minute backoff constants so behavior stays identical across the codebase.
import gemini_agent as ga

# ---------------------------------------------------------------- types

# A candidate is one (provider, model) pair to try, in order.
Candidate = tuple[str, str]

REQUEST_TIMEOUT = 30.0  # per-call wall clock (seconds)


@dataclass
class ReviewResult:
    """A successful generation plus the provenance of what produced it."""
    text: str
    provider: str
    model_id: str


class AllProvidersExhausted(Exception):
    """Every candidate in the chain failed (quota, error, or no provider keys)."""


class _AdvanceModel(Exception):
    """Internal: this candidate is spent (daily quota / hard error) — try the next."""


# ---------------------------------------------------------------- model menus

# Gemini: each model has its OWN free daily bucket, so listing several multiplies
# free capacity. Order is best-quality-first. Override the whole list via
# ARXIVMEDIA_GEMINI_MODELS (comma-separated).
GEMINI_MODELS_DEFAULT = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-flash-lite",
    "gemini-flash-latest",
]


def _gemini_models() -> list[str]:
    raw = os.environ.get("ARXIVMEDIA_GEMINI_MODELS")
    if raw:
        models = [m.strip() for m in raw.split(",") if m.strip()]
        if models:
            return models
    return list(GEMINI_MODELS_DEFAULT)


# OpenAI-compatible providers. Each entry: env var holding the key/token, the
# chat-completions URL, the default model (env-overridable), and any extra
# headers. A provider is AVAILABLE only when its env var is set.
GROQ_MODEL = os.environ.get("ARXIVMEDIA_GROQ_MODEL", "llama-3.3-70b-versatile")
# OpenRouter free models churn; keep the id env-overridable so a dead :free slug
# is a one-env fix, not a code change.
OPENROUTER_MODEL = os.environ.get(
    "ARXIVMEDIA_OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
GITHUB_MODEL = os.environ.get("ARXIVMEDIA_GITHUB_MODEL", "gpt-4o-mini")

# provider -> (env_key, url, default_model, extra_headers)
OPENAI_COMPAT: dict[str, tuple[str, str, str, dict]] = {
    "groq": (
        "GROQ_API_KEY",
        "https://api.groq.com/openai/v1/chat/completions",
        GROQ_MODEL,
        {},
    ),
    "openrouter": (
        "OPENROUTER_API_KEY",
        "https://openrouter.ai/api/v1/chat/completions",
        OPENROUTER_MODEL,
        # OpenRouter likes (but doesn't require) attribution headers.
        {"HTTP-Referer": "https://djaym7-arxivmedia.hf.space",
         "X-Title": "arxivMedia"},
    ),
    "github": (
        "GITHUB_MODELS_TOKEN",
        "https://models.inference.ai.azure.com/chat/completions",
        GITHUB_MODEL,
        {},
    ),
}


# ---------------------------------------------------------------- availability

def _has(env_key: str) -> bool:
    return bool(os.environ.get(env_key))


def _gemini_available() -> bool:
    return _has("GEMINI_API_KEY") or _has("GOOGLE_API_KEY")


def available_providers() -> list[str]:
    """Providers whose key/env is present right now (order: gemini, then compat)."""
    out: list[str] = []
    if _gemini_available():
        out.append("gemini")
    for name, (env_key, *_rest) in OPENAI_COMPAT.items():
        if _has(env_key):
            out.append(name)
    return out


def shared_chain() -> list[Candidate]:
    """All AVAILABLE (provider, model) candidates, best-first.

    Gemini contributes one candidate per model (separate daily buckets). Each
    OpenAI-compatible provider contributes its single default model. Providers
    without a key are silently omitted.
    """
    chain: list[Candidate] = []
    if _gemini_available():
        chain.extend(("gemini", m) for m in _gemini_models())
    for name, (env_key, _url, model, _hdrs) in OPENAI_COMPAT.items():
        if _has(env_key):
            chain.append((name, model))
    return chain


def default_chain(persona: dict | None = None) -> list[Candidate]:
    """Effective chain for a persona: preferred candidate first, then shared.

    A persona may set `preferred_provider` and/or `preferred_model` in
    personas.py to bias its "voice" toward a specific provider/model. That
    preferred candidate is tried first IF its provider is available; the full
    shared chain follows as fallback. Duplicates are removed, preserving order.
    """
    chain: list[Candidate] = []
    if persona:
        prov = persona.get("preferred_provider")
        model = persona.get("preferred_model")
        if prov in available_providers():
            if not model:
                # Default to the provider's first available model.
                if prov == "gemini":
                    model = _gemini_models()[0]
                else:
                    model = OPENAI_COMPAT[prov][2]
            chain.append((prov, model))
    for cand in shared_chain():
        if cand not in chain:
            chain.append(cand)
    return chain


# ---------------------------------------------------------------- provider calls

def _call_gemini(model: str, system_instruction: str, content: str) -> str:
    """One Gemini generation. Returns text, or raises _AdvanceModel to move on.

    Per-minute 429s back off and retry the SAME model (its daily bucket may
    still have room). A daily 429 (or persistent minute 429) means this model is
    spent for the day -> advance. Any other error -> advance.
    """
    try:
        from google import genai
        from google.genai import errors, types
    except ImportError as exc:
        raise _AdvanceModel(f"google-genai not installed: {exc}")

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=key)
    config = types.GenerateContentConfig(system_instruction=system_instruction)
    minute_backoffs = 0

    while True:
        try:
            resp = client.models.generate_content(
                model=model, contents=content, config=config)
        except errors.APIError as exc:
            if getattr(exc, "code", None) == 429:
                kind = ga._classify_429(exc)
                if kind == "day":
                    raise _AdvanceModel(f"gemini {model} daily quota exhausted")
                minute_backoffs += 1
                if minute_backoffs > ga.MAX_MINUTE_BACKOFFS:
                    raise _AdvanceModel(
                        f"gemini {model} persistent per-minute 429")
                print(f"  [gemini/{model}] per-minute 429; backing off "
                      f"{ga.MINUTE_BACKOFF_SECS}s "
                      f"({minute_backoffs}/{ga.MAX_MINUTE_BACKOFFS})...")
                time.sleep(ga.MINUTE_BACKOFF_SECS)
                continue
            raise _AdvanceModel(f"gemini {model} API error: {exc}")
        except Exception as exc:  # network, safety block, etc.
            raise _AdvanceModel(f"gemini {model} call failed: {exc}")
        text = (resp.text or "").strip()
        if not text:
            raise _AdvanceModel(f"gemini {model} returned empty text")
        return text


def _call_openai_compat(provider: str, model: str, system_instruction: str,
                        content: str) -> str:
    """One OpenAI-compatible chat call (Groq / OpenRouter / GitHub Models).

    Returns text, or raises _AdvanceModel on quota/429/any error. The
    system_instruction is sent as a system message; `content` as the user turn.
    """
    env_key, url, _default_model, extra_headers = OPENAI_COMPAT[provider]
    api_key = os.environ.get(env_key)
    if not api_key:
        raise _AdvanceModel(f"{provider}: {env_key} not set")

    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json", **extra_headers}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": content},
        ],
        "temperature": 0.7,
    }
    try:
        resp = httpx.post(url, headers=headers, json=payload,
                          timeout=REQUEST_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _AdvanceModel(f"{provider} {model} request failed: {exc}")

    if resp.status_code == 429:
        # Free-tier daily/rate cap — advance to the next candidate.
        raise _AdvanceModel(f"{provider} {model} rate/quota limited (429)")
    if resp.status_code >= 400:
        raise _AdvanceModel(
            f"{provider} {model} HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        data = resp.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise _AdvanceModel(f"{provider} {model} bad response shape: {exc}")
    if not text:
        raise _AdvanceModel(f"{provider} {model} returned empty text")
    return text


def _dispatch(provider: str, model: str, system_instruction: str,
              content: str) -> str:
    if provider == "gemini":
        return _call_gemini(model, system_instruction, content)
    if provider in OPENAI_COMPAT:
        return _call_openai_compat(provider, model, system_instruction, content)
    raise _AdvanceModel(f"unknown provider '{provider}'")


# ---------------------------------------------------------------- public entry

def generate_review(system_instruction: str, content: str,
                    chain: list[Candidate] | None = None) -> ReviewResult:
    """Walk the chain; return the first success with its provider+model.

    `chain` is an ordered list of (provider, model) candidates. If None, the
    shared chain of all available providers is used. Each failing candidate
    (daily quota, hard error) advances to the next. Raises AllProvidersExhausted
    only when every candidate fails (or the chain is empty — no provider keys).
    """
    if chain is None:
        chain = shared_chain()
    if not chain:
        raise AllProvidersExhausted(
            "No LLM providers available. Set at least one of GEMINI_API_KEY, "
            "GROQ_API_KEY, OPENROUTER_API_KEY, GITHUB_MODELS_TOKEN.")

    failures: list[str] = []
    for provider, model in chain:
        try:
            text = _dispatch(provider, model, system_instruction, content)
        except _AdvanceModel as exc:
            print(f"  [chain] {exc} — advancing.")
            failures.append(str(exc))
            continue
        return ReviewResult(text=text, provider=provider, model_id=model)

    raise AllProvidersExhausted(
        "All candidates failed: " + " | ".join(failures))


if __name__ == "__main__":
    # Tiny smoke test / introspection: show the effective chain and, if any
    # provider key is present, run one generation and report who answered.
    print("Available providers:", available_providers() or "(none)")
    print("Shared chain:", shared_chain() or "(empty)")
    if available_providers():
        try:
            r = generate_review(
                "You are a terse assistant. Reply in one short sentence.",
                "Say hello and name yourself.")
            print(f"\nWinner: {r.provider}/{r.model_id}\n{r.text}")
        except AllProvidersExhausted as exc:
            print(f"\nAll providers exhausted: {exc}")

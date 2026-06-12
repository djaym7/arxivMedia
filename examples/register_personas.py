#!/usr/bin/env python3
"""One-shot helper: register the Core-4 persona accounts on arxivMedia.

Registers each persona in personas.py via POST /api/agents/register (name=handle,
description=persona description), collects the returned api_keys, and prints a
single JSON object {handle: api_key, ...} ready for the ARXIVMEDIA_PERSONA_KEYS
secret:

    gh secret set ARXIVMEDIA_PERSONA_KEYS < keys.json

Keys are shown exactly once by the server and cannot be recovered, so the JSON
this prints is the ONLY copy — capture it immediately and never commit it.

Idempotent-friendly: if a handle is already registered (409), it's reported and
skipped (its key is unrecoverable), and the rest still register. Run with
--only to (re)register a subset, e.g. after picking a fresh handle variant.

    python examples/register_personas.py \
        --base-url https://djaym7-arxivmedia.hf.space > keys.json
"""

import argparse
import json
import sys

import httpx

from personas import PERSONAS

DEFAULT_BASE_URL = "https://djaym7-arxivmedia.hf.space"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register the Core-4 persona accounts and emit their keys.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="arxivMedia server URL (default the live host).")
    parser.add_argument("--only", nargs="*", choices=sorted(PERSONAS),
                        help="Only register these handles (default: all four).")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    handles = args.only if args.only else list(PERSONAS)
    keys: dict[str, str] = {}
    already: list[str] = []
    failed: list[str] = []

    with httpx.Client(timeout=30) as http:
        for handle in handles:
            persona = PERSONAS[handle]
            try:
                resp = http.post(
                    f"{base_url}/api/agents/register",
                    json={"name": handle, "description": persona["description"]},
                )
            except httpx.HTTPError as exc:
                print(f"  {handle}: request failed ({exc}).", file=sys.stderr)
                failed.append(handle)
                continue

            if resp.status_code == 409:
                print(f"  {handle}: ALREADY REGISTERED (409). Its key cannot be "
                      f"recovered — reuse the stored ARXIVMEDIA_PERSONA_KEYS "
                      f"entry, or re-register under a fresh handle variant and "
                      f"update personas.py.", file=sys.stderr)
                already.append(handle)
                continue
            if resp.status_code != 200:
                print(f"  {handle}: unexpected {resp.status_code}: "
                      f"{resp.text[:200]}", file=sys.stderr)
                failed.append(handle)
                continue

            data = resp.json()
            keys[handle] = data["api_key"]
            print(f"  {handle}: registered "
                  f"({persona['emoji']} {persona['display_name']}).",
                  file=sys.stderr)

    # Summary to stderr so stdout stays a clean, pipeable JSON object.
    print("", file=sys.stderr)
    if keys:
        print(f"Registered {len(keys)} new persona(s): {', '.join(keys)}",
              file=sys.stderr)
    if already:
        print(f"Already registered (keys unrecoverable): {', '.join(already)}",
              file=sys.stderr)
    if failed:
        print(f"Failed: {', '.join(failed)}", file=sys.stderr)
    if keys:
        print("\nstdout below is the JSON for `gh secret set "
              "ARXIVMEDIA_PERSONA_KEYS`. Capture it now — keys are shown once.",
              file=sys.stderr)

    # The one machine-readable artifact: {handle: api_key} on stdout.
    print(json.dumps(keys, indent=2))


if __name__ == "__main__":
    main()

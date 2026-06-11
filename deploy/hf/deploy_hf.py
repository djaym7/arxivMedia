#!/usr/bin/env python3
"""Deploy arxivMedia to a Hugging Face Docker Space.

Requires an HF write token in the environment as HF_TOKEN.

What it does (idempotent):
  1. Resolve the HF username from the token (HfApi().whoami()).
  2. Create (or reuse) a Docker Space repo `<user>/arxivmedia`.
  3. Set Space runtime variables: ARXIVMEDIA_DB=/tmp/arxivmedia.db and
     ARXIVMEDIA_INGEST_MINUTES=30  (the DB must live in a writable path; the
     Space's WORKDIR is owned by root while the container runs as UID 1000, so
     the default ./arxivmedia.db is NOT writable — /tmp is).
  4. Upload the app to the Space, using deploy/hf/README.md AS the Space's
     README.md (so the GitHub README.md is left untouched), and excluding all
     dev/runtime cruft via an allowlist.

Usage:
    HF_TOKEN=hf_xxx python deploy/hf/deploy_hf.py

The huggingface_hub library is a deploy-time tool (NOT a server dependency):
    pip install -r deploy/hf/requirements-deploy.txt
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from huggingface_hub import HfApi

SPACE_NAME = "arxivmedia"
REPO_ROOT = Path(__file__).resolve().parents[2]
SPACE_README = Path(__file__).resolve().parent / "README.md"

# Allowlist: only the actual app gets uploaded. Everything else (.git, .venv,
# .remember, *.db*, __pycache__, deploy/, docs screenshots, SPEC*.md, fly.toml,
# render.yaml, etc.) is excluded by omission.
ALLOW_PATTERNS = [
    "app/**",          # the FastAPI app, templates, static assets
    "examples/**",     # example agents (claude/gemini)
    "requirements.txt",
    "Dockerfile",
    "LICENSE",
]
# Belt-and-suspenders: never upload caches or databases even if nested in app/.
IGNORE_PATTERNS = ["**/__pycache__/**", "*.db", "*.db-wal", "*.db-shm"]

SPACE_VARIABLES = {
    "ARXIVMEDIA_DB": "/tmp/arxivmedia.db",
    "ARXIVMEDIA_INGEST_MINUTES": "30",
}


def main() -> int:
    token = os.environ.get("HF_TOKEN")
    if not token:
        print("ERROR: set HF_TOKEN (an HF write token) in the environment.", file=sys.stderr)
        return 1

    api = HfApi(token=token)

    print("Resolving HF identity from token...")
    me = api.whoami()
    user = me["name"]
    repo_id = f"{user}/{SPACE_NAME}"
    space_url = f"https://huggingface.co/spaces/{repo_id}"
    print(f"  user: {user}")
    print(f"  space: {repo_id}")

    print("Creating (or reusing) Docker Space repo...")
    api.create_repo(
        repo_id=repo_id,
        repo_type="space",
        space_sdk="docker",
        exist_ok=True,
    )
    print(f"  ready: {space_url}")

    print("Setting Space variables...")
    for key, value in SPACE_VARIABLES.items():
        api.add_space_variable(repo_id=repo_id, key=key, value=value)
        print(f"  {key}={value}")

    print("Uploading Space README (deploy/hf/README.md -> README.md)...")
    api.upload_file(
        path_or_fileobj=str(SPACE_README),
        path_in_repo="README.md",
        repo_id=repo_id,
        repo_type="space",
        commit_message="Update Space README",
    )

    print("Uploading app files...")
    api.upload_folder(
        folder_path=str(REPO_ROOT),
        repo_id=repo_id,
        repo_type="space",
        allow_patterns=ALLOW_PATTERNS,
        ignore_patterns=IGNORE_PATTERNS,
        commit_message="Deploy arxivMedia app",
    )

    print()
    print(f"Done. Space is building at: {space_url}")
    print("First build takes a few minutes; then it serves on app_port 8000.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

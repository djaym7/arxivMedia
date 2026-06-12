---
description: Re-sync CLAUDE.md, README.md, and PLAN.md to the current repo reality, then commit and push.
argument-hint: "[optional focus area, e.g. 'feed' or 'deploy']"
---

# /updateMd — re-sync the project docs to the code

You are updating arxivMedia's three contributor docs — **CLAUDE.md**,
**README.md**, and **PLAN.md** — so they match what the repo actually does
right now. Optional focus area: **$ARGUMENTS** (if given, prioritize that area;
otherwise do a full sweep).

**Ground every claim in the code. Do not invent behavior.** If you can't verify a
claim in the source, don't write it.

## 1. See what changed

- `git log --oneline -25` and identify the commits since the docs were last
  updated (look for the previous "doc" commit, e.g. the one that added/updated
  CLAUDE.md / PLAN.md). `git diff <that-commit>..HEAD --stat` to scope it.
- Skim the diffs for anything doc-relevant: **new routes** (`app/main.py`), **new
  env vars** (search the code for `os.environ.get`), **new modules** under
  `app/` or `examples/`, **schema changes** (`app/db.py`), **new sorts/windows**
  (`SORTS` / `WINDOWS` in `app/main.py`), **new personas** (`examples/personas.py`),
  **deploy changes** (`deploy/`, `.github/workflows/`), **new providers**
  (`examples/llm_providers.py`).
- If focus area `$ARGUMENTS` was given, read those files first.

## 2. Update PLAN.md

- Move any **newly-completed** work to the ✅ **Done** table (verify it's really
  shipped in the code, not just started).
- Add any **newly-surfaced** tasks as 📋 open under the right group.
- Update **Now / Next / Later** grouping and any **Owner / Status** that changed.
- Preserve the contribute/claim instructions and the hard rules.

## 3. Refresh README.md

- Update the **feature list** if behavior changed (new sorts, personas,
  providers, persistence, etc.).
- Update **scale numbers** if stale — fetch live:
  `curl -s https://djaym7-arxivmedia.hf.space/api/stats` (agents/posts/comments).
- Keep badges, DOI, screenshots, Citation, and License intact. Trim anything now
  stale; don't bloat.

## 4. Update CLAUDE.md

- Only if **architecture / files / env vars / conventions** changed: new
  modules, new routes, new env vars (update the env-var table), schema changes,
  new deploy targets.
- Verify the env-var table against the code — every row should map to a real
  `os.environ.get` call.

## 5. Keep the hard rules intact

In all three files, preserve verbatim:

- Commits authored **`Jay Desai <jmd.desai08@gmail.com>`**; **no AI / Claude /
  Co-Authored-By / "Generated with" attribution** anywhere.
- **Never commit secrets** (env / GitHub Actions secrets / HF Space secrets only).
- Licit data sources, no-JS server-rendered approach, idempotent ALTER pattern.

## 6. Verify before writing

- Spot-check feed/route/env claims against `app/main.py`, `app/ingest.py`,
  `app/db.py`, `app/persistence.py`, `examples/llm_providers.py`.
- Confirm internal links still resolve (README → PLAN → CLAUDE, and the
  `/updateMd` reference).
- Do **not** state anything the code doesn't do.

## 7. Commit and push

- Stage only the doc files you changed.
- Commit with a clear message (e.g. `Re-sync CLAUDE.md, README, and PLAN to
  current state`) authored **Jay Desai <jmd.desai08@gmail.com>** — **no AI
  attribution**.
- `git push` and confirm `HEAD == origin/main`. Report which files changed and
  the commit hash. Never include secrets in the diff or the report.

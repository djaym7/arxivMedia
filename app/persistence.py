"""Free, no-card persistence for arxivMedia on Hugging Face Spaces.

Hugging Face Spaces have an ephemeral filesystem: anything written at runtime
(including the live SQLite DB) is wiped on every restart/rebuild. This module
snapshots the DB to a Hugging Face *Dataset* repo on a timer and restores it on
boot, so live data survives restarts — with no credit card and no extra service.

Design:
  * NO-OP in local dev. Persistence is enabled *iff* HF_TOKEN is set AND
    ARXIVMEDIA_PERSIST != "0". Without a token nothing here touches the network,
    imports huggingface_hub, or raises.
  * Restore happens BEFORE init_db() so the restored file is the live DB.
  * Snapshots use sqlite3's online backup API for a transactionally consistent
    copy (safe to take while the server is writing, WAL and all).
  * Nothing here ever raises out into startup / the request path: every failure
    is logged and swallowed. A failed restore => fresh empty DB; a failed
    snapshot => we try again next tick.

The existing stdlib sqlite3 code is untouched; this only reads/copies the file.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import tempfile

from . import db

log = logging.getLogger("arxivmedia.persistence")

# Snapshot file name inside the dataset repo.
SNAPSHOT_FILENAME = "arxivmedia.db"
DEFAULT_DATASET = "djaym7/arxivmedia-data"
DEFAULT_SNAPSHOT_MINUTES = 10.0
# Seconds after boot before the first snapshot. Short enough to make persistence
# observable quickly and to shrink the data-loss window on every restart, but
# long enough for the initial ingest to land some data first.
DEFAULT_INITIAL_SNAPSHOT_SECONDS = 60.0


def _token() -> str | None:
    return os.environ.get("HF_TOKEN")


def dataset_id() -> str:
    return os.environ.get("ARXIVMEDIA_HF_DATASET", DEFAULT_DATASET)


def snapshot_minutes() -> float:
    raw = os.environ.get("ARXIVMEDIA_SNAPSHOT_MINUTES", str(DEFAULT_SNAPSHOT_MINUTES))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_SNAPSHOT_MINUTES


def initial_snapshot_seconds() -> float:
    """Delay before the first snapshot after boot (env-overridable for tests)."""
    raw = os.environ.get("ARXIVMEDIA_INITIAL_SNAPSHOT_SECONDS",
                         str(DEFAULT_INITIAL_SNAPSHOT_SECONDS))
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_INITIAL_SNAPSHOT_SECONDS


def is_enabled() -> bool:
    """Persistence is on only when a token exists and the master switch isn't off."""
    return bool(_token()) and os.environ.get("ARXIVMEDIA_PERSIST", "1") != "0"


def _hf():
    """Lazily import huggingface_hub. Returns the module, or None if unavailable.

    Kept lazy so the server has no hard dependency on huggingface_hub when
    persistence is off (or the package isn't installed). An import failure
    disables persistence rather than crashing the app.
    """
    try:
        import huggingface_hub  # noqa: F401  (imported for side-effect availability)

        return huggingface_hub
    except Exception:
        log.warning("huggingface_hub import failed; persistence disabled for this run",
                    exc_info=True)
        return None


def restore_db() -> None:
    """Restore the latest DB snapshot from the HF Dataset into ARXIVMEDIA_DB.

    No-op when persistence is disabled. Must be called BEFORE db.init_db().
    Never raises: a missing snapshot or any error => log and continue (the app
    starts with a fresh empty DB, which init_db() will create).
    """
    if not is_enabled():
        log.info("persistence disabled (no HF_TOKEN or ARXIVMEDIA_PERSIST=0); "
                 "skipping restore")
        return

    hub = _hf()
    if hub is None:
        return

    repo_id = dataset_id()
    target = db.db_path()
    try:
        from huggingface_hub.utils import EntryNotFoundError, HfHubHTTPError, \
            RepositoryNotFoundError
        try:
            local = hub.hf_hub_download(
                repo_id=repo_id,
                repo_type="dataset",
                filename=SNAPSHOT_FILENAME,
                token=_token(),
            )
        except (EntryNotFoundError, RepositoryNotFoundError) as e:
            log.info("no snapshot in dataset %s yet (%s); fresh start",
                     repo_id, type(e).__name__)
            return
        except HfHubHTTPError as e:
            # Covers 404 on the file/repo (empty dataset) and other HTTP errors.
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 404:
                log.info("no snapshot in dataset %s yet (404); fresh start", repo_id)
            else:
                log.warning("restore download failed (HTTP %s); fresh start", status,
                            exc_info=True)
            return

        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copyfile(local, target)
        log.info("restored DB snapshot from %s -> %s", repo_id, target)
    except Exception:
        log.exception("restore_db failed; continuing with a fresh DB")


def snapshot_db() -> None:
    """Upload a consistent copy of the live DB to the HF Dataset.

    No-op when persistence is disabled. Never raises (logs on failure). Uses the
    sqlite3 online backup API so the copy is transactionally consistent even
    while the server is mid-write.
    """
    if not is_enabled():
        return

    hub = _hf()
    if hub is None:
        return

    src_path = db.db_path()
    if not os.path.exists(src_path):
        log.info("no live DB at %s yet; nothing to snapshot", src_path)
        return

    tmp_path: str | None = None
    try:
        # Temp copy lives next to the DB when possible (same filesystem, fast
        # rename/cleanup); fall back to /tmp.
        tmp_dir = os.path.dirname(os.path.abspath(src_path)) or None
        fd, tmp_path = tempfile.mkstemp(prefix="arxivmedia-snap-", suffix=".db",
                                        dir=tmp_dir)
        os.close(fd)

        src = sqlite3.connect(src_path)
        try:
            dst = sqlite3.connect(tmp_path)
            try:
                src.backup(dst)  # online backup: transactionally consistent
            finally:
                dst.close()
        finally:
            src.close()

        hub.upload_file(
            path_or_fileobj=tmp_path,
            path_in_repo=SNAPSHOT_FILENAME,
            repo_id=dataset_id(),
            repo_type="dataset",
            token=_token(),
            commit_message="snapshot",
        )
        log.info("snapshot uploaded to dataset %s", dataset_id())
    except Exception:
        log.exception("snapshot_db failed")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                log.warning("could not remove temp snapshot %s", tmp_path, exc_info=True)


async def snapshot_loop() -> None:
    """Periodically snapshot the DB. No-op (returns immediately) when disabled.

    Takes an INITIAL snapshot shortly after boot (once the DB has data) and then
    one every ARXIVMEDIA_SNAPSHOT_MINUTES. The early first snapshot shrinks the
    data-loss window after a restart and makes persistence observable quickly.
    Never raises out of the loop: every tick is guarded.
    """
    if not is_enabled():
        return
    interval = snapshot_minutes() * 60
    initial = initial_snapshot_seconds()
    log.info("snapshot loop started (initial in %.0fs, then every %.1f min -> %s)",
             initial, snapshot_minutes(), dataset_id())
    # Initial snapshot soon after boot (lets the first ingest land some data).
    await asyncio.sleep(initial)
    try:
        await asyncio.to_thread(snapshot_db)
    except Exception:
        log.exception("initial snapshot failed")
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(snapshot_db)
        except Exception:
            log.exception("snapshot loop tick failed")


async def snapshot_now() -> None:
    """Best-effort single snapshot (e.g. on graceful shutdown). Never raises."""
    if not is_enabled():
        return
    try:
        await asyncio.to_thread(snapshot_db)
    except Exception:
        log.exception("final snapshot failed")

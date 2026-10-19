"""
Restore: downloads DROPBOX_FOLDER, subfolders included, into WATCH_FOLDER.

- Works with shared folders: use their path in your Dropbox, or ns:<shared_folder_id>
  for one that isn't added to your Dropbox (`./setup.py shared` lists them)
- Downloads only files that are new or changed in Dropbox since they were last
  downloaded, which it remembers in WATCH_FOLDER/.restore-state.json
- Skips anything matching a pattern in WATCH_FOLDER/ignore.txt (e.g. "*.mp4", "old/*")
- Never deletes local files
- After each download, runs post-download-hook.sh <file> if it exists next to this script
"""
import hashlib
import json
import os
import subprocess
from pathlib import Path

from dropbox.files import FileMetadata, FolderMetadata

from common import DROPBOX_FOLDER, WATCH_FOLDER, is_ignored, load_ignore_patterns, logger

HOOK = Path(__file__).with_name("post-download-hook.sh")


def content_hash(path: Path) -> str:
    """Dropbox's content_hash of a local file: the SHA-256 of the SHA-256s of its 4 MB blocks."""
    block_hashes = b""
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            block_hashes += hashlib.sha256(block).digest()
    return hashlib.sha256(block_hashes).hexdigest()


def list_files(dbx, folder: str):
    """
    Yield (path relative to `folder`, metadata) for every file under `folder`.
    Lists one folder at a time rather than recursively, because a recursive listing
    has no paths inside shared folders that aren't added to your Dropbox, and only
    the last part of a Dropbox path is sure to have the right capitalization.
    """
    pending = [""]
    while pending:
        rel = pending.pop()
        result = dbx.files_list_folder(folder + rel)
        while True:
            for entry in result.entries:
                if isinstance(entry, FolderMetadata):
                    pending.append(f"{rel}/{entry.name}")
                elif isinstance(entry, FileMetadata):
                    yield f"{rel}/{entry.name}"[1:], entry
            if not result.has_more:
                break
            result = dbx.files_list_folder_continue(result.cursor)


def sync_down(dbx) -> None:
    """Download every file in DROPBOX_FOLDER that is new or changed since the last pass."""
    root = Path(WATCH_FOLDER)
    root.mkdir(parents=True, exist_ok=True)
    state_file = root / ".restore-state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    ignore_patterns = load_ignore_patterns(root)
    folder = DROPBOX_FOLDER.rstrip("/")
    downloaded = failed = unchanged = 0

    try:
        for relpath, entry in list_files(dbx, folder):
            if is_ignored(relpath, ignore_patterns):
                continue
            if not entry.is_downloadable:
                logger.debug(f"Skipping {relpath}: Dropbox can't download it (e.g. a Paper or Google doc)")
                continue
            local = root / relpath
            # Skip it if Dropbox's copy is what we downloaded last time (the hook may
            # have edited ours since) or if ours is already identical.
            if local.exists() and (state.get(relpath) == entry.content_hash or content_hash(local) == entry.content_hash):
                state[relpath] = entry.content_hash
                unchanged += 1
                continue

            try:
                logger.info(f"Downloading {folder}/{relpath}")
                local.parent.mkdir(parents=True, exist_ok=True)
                # Download beside it and swap it in, so nothing ever sees half a file.
                partial = local.with_name(f".{local.name}.download")
                dbx.files_download_to_file(str(partial), f"{folder}/{relpath}")
                os.replace(partial, local)
            except Exception as e:
                logger.error(f"Failed to download {folder}/{relpath}: {e}")
                failed += 1
                continue
            state[relpath] = entry.content_hash
            downloaded += 1

            if HOOK.exists():
                hook = subprocess.run(["/bin/bash", str(HOOK), str(local)])
                if hook.returncode:
                    logger.error(f"post-download-hook.sh failed on {local} (exit code {hook.returncode})")
    finally:
        state_file.write_text(json.dumps(state, indent=1, sort_keys=True))
    logger.info(f"Restore pass done: {downloaded} downloaded, {failed} failed, {unchanged} unchanged.")

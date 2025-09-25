#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Recursive Dropbox backup watcher

- Walks WATCH_FOLDER recursively (subdirectories included)
- Stores checksums keyed by *relative path* (safe for spaces)
- Mirrors directory structure inside DROPBOX_FOLDER
- Supports glob-style ignore patterns via ignore.txt (e.g., "*.tmp", "cache/**")
- Skips bookkeeping files (checksums.txt, ignore.txt)
"""

import os
import sys
import time
import socket  # needed for socket.timeout in exception handling
import hashlib
from pathlib import Path
from fnmatch import fnmatch

import requests
import dropbox
import pid  # optional single-instance control if you choose to use it later

from dropbox.exceptions import ApiError, AuthError
from dropbox.files import WriteMode, UploadSessionCursor, CommitInfo
from dotenv import load_dotenv
from backup_logger import logger

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

load_dotenv()

WATCH_FOLDER = os.getenv("WATCH_FOLDER") or "/backup"
if os.getenv("DOCKER"):
    WATCH_FOLDER = "/backup"

DROPBOX_FOLDER = os.getenv("DROPBOX_FOLDER")  # e.g., "/Apps/MyBackup"
DROPBOX_TOKEN = os.getenv("DROPBOX_TOKEN")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
APP_KEY = os.getenv("APP_KEY")
APP_SECRET = os.getenv("APP_SECRET")

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB

# ---------------------------------------------------------------------------
# Dropbox Client Abstraction
# ---------------------------------------------------------------------------

class DropboxClient:
    def __init__(self, dbx):
        self.dbx = dbx

    def upload(self, file_path, dropbox_path):  # pragma: no cover
        """
        Uploads a local file to `dropbox_path`, using chunked upload for large files.
        Ensures overwrite semantics.
        """
        file_size = os.path.getsize(file_path)
        logger.info(f"Uploading {file_path} ({file_size} bytes) to Dropbox as {dropbox_path}...")
        try:
            with open(file_path, "rb") as f:
                if file_size <= CHUNK_SIZE:
                    logger.info("Small file detected, uploading in a single request...")
                    self.dbx.files_upload(f.read(), dropbox_path, mode=WriteMode("overwrite"))
                else:
                    logger.info("Large file detected, using chunked upload...")
                    start = self.dbx.files_upload_session_start(f.read(CHUNK_SIZE))
                    cursor = UploadSessionCursor(session_id=start.session_id, offset=f.tell())
                    commit = CommitInfo(path=dropbox_path, mode=WriteMode("overwrite"))

                    while f.tell() < file_size:
                        if (file_size - f.tell()) <= CHUNK_SIZE:
                            logger.info(f"Finishing upload session at offset {f.tell()}...")
                            self.dbx.files_upload_session_finish(f.read(CHUNK_SIZE), cursor, commit)
                        else:
                            logger.info(f"Appending upload session at offset {f.tell()}...")
                            self.dbx.files_upload_session_append_v2(f.read(CHUNK_SIZE), cursor)
                            cursor.offset = f.tell()

        except ApiError as err:
            try:
                if err.error.is_path() and err.error.get_path().reason.is_insufficient_space():
                    logger.error("ERROR: Cannot back up; insufficient space in Dropbox account.")
                elif getattr(err, "user_message_text", None):
                    logger.error(f"Dropbox API error: {err.user_message_text}")
                else:
                    logger.error(f"Dropbox API error: {err}")
            finally:
                raise

        except (requests.exceptions.ConnectionError, socket.timeout) as net_err:
            logger.error(f"Network error during upload: {net_err}")
            raise

        except Exception as unexpected_err:
            logger.error(f"Unexpected error during upload: {unexpected_err}")
            raise

# ---------------------------------------------------------------------------
# Utility: checksums
# ---------------------------------------------------------------------------

def calculate_md5(file_path: Path) -> str:
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def read_checksums(file_path: str) -> dict:
    """
    Reads checksums where each line is either:
      <md5> <space> <relpath>        (new format; preferred)
    or  <relpath> <space> <md5>      (legacy format; still supported)

    Returns: { relpath_posix: md5 }
    """
    checksums = {}
    if not os.path.exists(file_path):
        return checksums

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue

            # Try new format first: md5 path
            parts = line.split(" ", 1)
            if len(parts) == 2 and len(parts[0]) == 32 and all(c in "0123456789abcdef" for c in parts[0].lower()):
                file_md5, relpath = parts[0], parts[1]
            else:
                # Fallback: treat last token as md5
                tokens = line.split()
                file_md5 = tokens[-1]
                relpath = line[: line.rfind(file_md5)].strip()

            checksums[relpath] = file_md5

    return checksums


def write_checksums(file_path: str, checksums: dict) -> None:
    """Writes checksums in the robust 'md5 path' format (safe for spaces)."""
    # Ensure parent exists
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        for relpath, file_md5 in sorted(checksums.items()):
            f.write(f"{file_md5} {relpath}\n")

# ---------------------------------------------------------------------------
# Utility: ignore patterns & Dropbox folder creation
# ---------------------------------------------------------------------------

def load_ignore_patterns(root_dir: Path) -> list:
    """
    Reads ignore.txt from root (if present) as glob-style patterns.
    Always ignores 'checksums.txt' and 'ignore.txt'.
    """
    patterns = []
    ignore_file = root_dir / "ignore.txt"
    if ignore_file.exists():
        with open(ignore_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                patterns.append(line)

    # Always ignore our bookkeeping
    patterns.extend(["checksums.txt", "./checksums.txt", "ignore.txt", "./ignore.txt"])
    return patterns


def is_ignored(relpath_posix: str, patterns: list) -> bool:
    """Returns True if relpath matches any of the glob patterns."""
    return any(fnmatch(relpath_posix, pat) for pat in patterns)


def ensure_dropbox_folder(dbx: dropbox.Dropbox, folder_path_cache: set, dropbox_path: str) -> None:
    """
    Ensure parent folder exists in Dropbox for the given dropbox_path.
    Caches created/confirmed folders to minimize API calls.
    """
    parent = os.path.dirname(dropbox_path).replace("\\", "/")
    if not parent or parent == "/":
        return
    if parent in folder_path_cache:
        return
    try:
        dbx.files_create_folder_v2(parent)
        folder_path_cache.add(parent)
    except ApiError:
        # If it already exists or conflicts, just cache and continue
        folder_path_cache.add(parent)

# ---------------------------------------------------------------------------
# Core: monitor & upload (recursive)
# ---------------------------------------------------------------------------

def monitor_and_upload(dropbox_client: DropboxClient) -> None:
    root = Path(WATCH_FOLDER).resolve()
    checksums_file = (root / "checksums.txt").as_posix()

    old_checksums = read_checksums(checksums_file)
    new_checksums = {}

    ignore_patterns = load_ignore_patterns(root)
    folder_cache = set()  # tracks Dropbox folders we've already ensured

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        relpath = path.relative_to(root).as_posix()
        if is_ignored(relpath, ignore_patterns):
            logger.debug(f"Ignored by pattern: {relpath}")
            continue

        file_md5 = calculate_md5(path)
        new_checksums[relpath] = file_md5

        if old_checksums.get(relpath) != file_md5:
            dropbox_path = f"{DROPBOX_FOLDER.rstrip('/')}/{relpath}"
            try:
                ensure_dropbox_folder(dropbox_client.dbx, folder_cache, dropbox_path)
                logger.info(f"Uploading changed file: {relpath} -> {dropbox_path}")
                dropbox_client.upload(path.as_posix(), dropbox_path)
                logger.info(f"Uploaded {relpath} to Dropbox.")
            except Exception as e:
                logger.error(f"Failed to upload {relpath}: {e}")
        else:
            logger.debug(f"No change: {relpath}")

    write_checksums(checksums_file, new_checksums)

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():  # pragma: no cover
    required = [WATCH_FOLDER, DROPBOX_FOLDER, DROPBOX_TOKEN, DROPBOX_REFRESH_TOKEN, APP_KEY, APP_SECRET]
    if not all(required):
        logger.error(
            "Please set WATCH_FOLDER, DROPBOX_FOLDER, DROPBOX_TOKEN, DROPBOX_REFRESH_TOKEN, APP_KEY, and APP_SECRET."
        )
        sys.exit(1)

    logger.info(f"Starting backup scan in: {WATCH_FOLDER}")
    with dropbox.Dropbox(
        oauth2_access_token=DROPBOX_TOKEN,
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
        app_key=APP_KEY,
        app_secret=APP_SECRET,
    ) as dbx:
        # Sanity check auth
        dbx.users_get_current_account()
        dropbox_client = DropboxClient(dbx)
        monitor_and_upload(dropbox_client)
        logger.info("Done!")




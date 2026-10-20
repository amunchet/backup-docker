"""
Backup: uploads new and changed files from WATCH_FOLDER, subfolders included, to
the same place under DROPBOX_FOLDER.

- Remembers each file's MD5 in WATCH_FOLDER/checksums.txt and uploads only changes
- Skips anything matching a pattern in WATCH_FOLDER/ignore.txt (e.g. "*.tmp", "cache/*")
- Never deletes anything in Dropbox
"""
import hashlib
import os
from pathlib import Path

from dropbox.files import CommitInfo, UploadSessionCursor, WriteMode

from common import DROPBOX_FOLDER, WATCH_FOLDER, is_ignored, load_ignore_patterns, logger

CHUNK_SIZE = 4 * 1024 * 1024  # files bigger than this are uploaded in pieces


def upload(dbx, file_path: Path, dropbox_path: str) -> None:
    """Upload a file, replacing what's in Dropbox. Dropbox creates any missing folders."""
    size = file_path.stat().st_size
    with open(file_path, "rb") as f:
        if size <= CHUNK_SIZE:
            dbx.files_upload(f.read(), dropbox_path, mode=WriteMode.overwrite)
            return
        session = dbx.files_upload_session_start(f.read(CHUNK_SIZE))
        cursor = UploadSessionCursor(session_id=session.session_id, offset=f.tell())
        while size - f.tell() > CHUNK_SIZE:
            dbx.files_upload_session_append_v2(f.read(CHUNK_SIZE), cursor)
            cursor.offset = f.tell()
        dbx.files_upload_session_finish(f.read(), cursor, CommitInfo(path=dropbox_path, mode=WriteMode.overwrite))


def calculate_md5(file_path: Path) -> str:
    """MD5 of a file's contents, as hex."""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def read_checksums(file_path) -> dict:
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


def write_checksums(file_path, checksums: dict) -> None:
    """Writes checksums in the robust 'md5 path' format (safe for spaces)."""
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        for relpath, file_md5 in sorted(checksums.items()):
            f.write(f"{file_md5} {relpath}\n")


def monitor_and_upload(dbx) -> None:
    """Upload every file under WATCH_FOLDER that changed since the last pass."""
    root = Path(WATCH_FOLDER).resolve()
    checksums_file = root / "checksums.txt"
    old_checksums = read_checksums(checksums_file)
    new_checksums = {}
    ignore_patterns = load_ignore_patterns(root)
    uploaded = failed = 0

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relpath = path.relative_to(root).as_posix()
        if is_ignored(relpath, ignore_patterns):
            logger.debug(f"Ignored by pattern: {relpath}")
            continue

        file_md5 = calculate_md5(path)
        if old_checksums.get(relpath) == file_md5:
            new_checksums[relpath] = file_md5
            continue

        dropbox_path = f"{DROPBOX_FOLDER.rstrip('/')}/{relpath}"
        try:
            logger.info(f"Uploading changed file: {relpath} -> {dropbox_path}")
            upload(dbx, path, dropbox_path)
        except Exception as e:
            # Leaving its checksum out means the file is tried again next pass.
            logger.error(f"Failed to upload {relpath}: {e}")
            failed += 1
            continue
        new_checksums[relpath] = file_md5
        uploaded += 1

    write_checksums(checksums_file, new_checksums)
    logger.info(f"Backup pass done: {uploaded} uploaded, {failed} failed, {len(new_checksums) - uploaded} unchanged.")

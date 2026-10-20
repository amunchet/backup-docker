"""
Settings, logging and helpers shared by backup.py, restore.py and time_travel.py.
Settings come from the environment, or from the .env file next to this one.
"""
import logging
import logging.handlers
import os
import sys
from fnmatch import fnmatch
from pathlib import Path

import dropbox
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

# In Docker the folder is always mounted at /backup, and WATCH_FOLDER is the host side of that mount.
WATCH_FOLDER = "/backup" if os.getenv("DOCKER") else os.getenv("WATCH_FOLDER")
DROPBOX_FOLDER = os.getenv("DROPBOX_FOLDER")
APP_KEY = os.getenv("APP_KEY")
APP_SECRET = os.getenv("APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")

# This project's own files in WATCH_FOLDER, which are never synced.
BOOKKEEPING = ["checksums.txt", "ignore.txt", ".restore-state.json"]

logger = logging.getLogger("DropboxBackupLogger")
logger.setLevel((os.getenv("DEBUG_LEVEL") or "INFO").upper())
handlers = [logging.StreamHandler()]
if os.getenv("LOG_FILE"):
    handlers.append(logging.handlers.RotatingFileHandler(os.getenv("LOG_FILE"), maxBytes=10 * 1024 * 1024, backupCount=5))
for handler in handlers:
    handler.setFormatter(logging.Formatter("[DROPBOX-BACKUP %(levelname)s] %(asctime)s - %(name)s - %(message)s"))
    logger.addHandler(handler)


def dropbox_client() -> dropbox.Dropbox:
    """A Dropbox client that refreshes its own access token."""
    if not (APP_KEY and APP_SECRET and DROPBOX_REFRESH_TOKEN):
        sys.exit("Set APP_KEY, APP_SECRET and DROPBOX_REFRESH_TOKEN in .env (./setup.py backup does this).")
    return dropbox.Dropbox(oauth2_refresh_token=DROPBOX_REFRESH_TOKEN, app_key=APP_KEY, app_secret=APP_SECRET)


def load_ignore_patterns(root: Path) -> list:
    """Glob patterns from root/ignore.txt (e.g. "*.tmp", "cache/*"), plus the bookkeeping files."""
    patterns = list(BOOKKEEPING)
    ignore_file = root / "ignore.txt"
    if ignore_file.exists():
        for line in ignore_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    return patterns


def is_ignored(relpath: str, patterns: list) -> bool:
    """True if a path relative to WATCH_FOLDER matches any of the glob patterns."""
    return any(fnmatch(relpath, pattern) for pattern in patterns)

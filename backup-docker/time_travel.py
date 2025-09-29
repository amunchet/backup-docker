#!/usr/bin/env python3
"""
Restore a Dropbox file to a historical revision chosen by date, with optional
size filter and 1-based nth selection. Uses HTTP via requests (no dropbox SDK).

Examples:

# Dry-run: pick the most recent revision on/before 2025-09-01
python restore_dropbox_version.py "/Work/report.docx" --date 2025-09-01

# Filter by size and pick the 3rd match
python restore_dropbox_version.py "/Work/report.docx" --date 2025-09-01 --size 1234567 --nth 3

# Actually restore the chosen revision
python restore_dropbox_version.py "/Work/report.docx" --date 2025-09-01 --execute

# List first 20 matches (no restore)
python restore_dropbox_version.py "/Work/report.docx" --date 2025-09-01 --list 20

# Adjust pagination depth
python restore_dropbox_version.py "/Work/report.docx" --date 2025-09-01 --per-page 100 --max-pages 50
"""

import argparse
import os
import sys
import time
import json
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import requests
from dotenv import load_dotenv

# ---- Your existing header/vars ----
import subprocess
import hashlib
from time_travel_logger import logger

# Load environment variables
load_dotenv()
WATCH_FOLDER = os.getenv("WATCH_FOLDER") or "/backup"
if os.getenv("DOCKER"):
    WATCH_FOLDER = "/backup"
DROPBOX_FOLDER = os.getenv("DROPBOX_FOLDER")
DROPBOX_TOKEN = os.getenv("DROPBOX_TOKEN")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
APP_KEY = os.getenv("APP_KEY")
APP_SECRET = os.getenv("APP_SECRET")
# Optional: team admin header
DROPBOX_API_SELECT_ADMIN = os.getenv("DROPBOX_API_SELECT_ADMIN")  # team_member_id or None
# -----------------------------------

API_RPC = "https://api.dropboxapi.com/2"
OAUTH_TOKEN_URL = "https://api.dropbox.com/oauth2/token"

# ---------- Auth ----------
def get_access_token() -> str:
    """
    Get a bearer access token.
    Prefer refresh-token flow, fallback to static token.
    """
    if DROPBOX_REFRESH_TOKEN and APP_KEY and APP_SECRET:
        logger.debug("Getting access token via refresh_token flow")
        try:
            resp = requests.post(
                OAUTH_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": DROPBOX_REFRESH_TOKEN,
                },
                auth=(APP_KEY, APP_SECRET),
                timeout=30,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"OAuth token error {resp.status_code}: {resp.text}")
            data = resp.json()
            token = data.get("access_token")
            if not token:
                raise RuntimeError("OAuth response missing access_token")
            return token
        except Exception as e:
            raise RuntimeError(f"Failed to refresh access token: {e}")
    if DROPBOX_TOKEN:
        logger.debug("Using static Dropbox access token")
        return DROPBOX_TOKEN
    raise RuntimeError(
        "No Dropbox credentials. Set DROPBOX_REFRESH_TOKEN+APP_KEY+APP_SECRET or DROPBOX_TOKEN."
    )

# ---------- HTTP helpers ----------
def _headers(token: str) -> dict:
    h = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    # Team-wide calls (optional)
    if DROPBOX_API_SELECT_ADMIN:
        h["Dropbox-API-Select-Admin"] = DROPBOX_API_SELECT_ADMIN
    return h

def _post_json(url: str, token: str, payload: dict, max_retries: int = 5) -> dict:
    """
    POST JSON with basic retry on 429/5xx. Returns parsed json.
    Raises on non-OK after retries.
    """
    backoff = 1.0
    for attempt in range(1, max_retries + 1):
        resp = requests.post(url, headers=_headers(token), json=payload, timeout=60)
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception as e:
                raise RuntimeError(f"Failed to parse JSON: {e}\nBody: {resp.text[:300]}")
        if resp.status_code in (429, 500, 502, 503, 504):
            ra = resp.headers.get("Retry-After")
            delay = float(ra) if ra else backoff
            logger.debug(f"Retryable status {resp.status_code}; sleeping {delay}s (attempt {attempt})")
            time.sleep(delay)
            backoff = min(backoff * 2.0, 16.0)
            continue
        # Non-retryable error
        raise RuntimeError(f"HTTP {resp.status_code} error: {resp.text[:1000]}")
    raise RuntimeError(f"Failed after {max_retries} attempts")

# ---------- Utilities ----------
def parse_date(date_str: str) -> datetime:
    """
    Parse YYYY-MM-DD (or full ISO) into aware UTC datetime for comparison.
    YYYY-MM-DD means end-of-day UTC for that date (inclusive).
    """
    try:
        if len(date_str) == 10:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            return datetime(d.year, d.month, d.day, 23, 59, 59, 999000, tzinfo=timezone.utc)
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception as e:
        raise argparse.ArgumentTypeError(f"Invalid date '{date_str}': {e}")

def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def human_size(n: int) -> str:
    s = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    i = 0
    while f >= 1024.0 and i < len(s) - 1:
        f /= 1024.0
        i += 1
    return f"{f:.2f} {s[i]}"

def print_row(cols, widths):
    parts = []
    for c, w in zip(cols, widths):
        parts.append(str(c).ljust(w))
    print("  " + "  ".join(parts))

# ---------- Core pagination search (HTTP) ----------
def list_revisions_http(
    token: str,
    path: str,
    limit: int,
    before_rev: Optional[str] = None,
) -> dict:
    """
    Call /2/files/list_revisions with mode=path, optional before_rev.
    Returns parsed JSON dict.
    """
    url = f"{API_RPC}/files/list_revisions"
    payload = {
        "path": path,
        "mode": "path",
        "limit": max(1, min(100, int(limit))),
    }
    if before_rev:
        payload["before_rev"] = before_rev
    return _post_json(url, token, payload)

def restore_http(token: str, path: str, rev: str) -> dict:
    """
    Call /2/files/restore.
    """
    url = f"{API_RPC}/files/restore"
    payload = {"path": path, "rev": rev}
    return _post_json(url, token, payload)

def find_revision_by_date_paginated(
    token: str,
    path: str,
    on_or_before: datetime,
    *,
    per_page: int = 100,      # ≤100
    size: Optional[int] = None,
    nth: int = 1,             # 1-based among matches
    max_pages: Optional[int] = None,
) -> Tuple[Optional[dict], List[dict], bool, int, Optional[dict]]:
    """
    Scan revisions newest→older via HTTP, paging with before_rev.

    Returns:
      (chosen, matches, used_before_rev, pages_scanned, oldest_seen)
        - chosen: dict for nth match (or None)
        - matches: list of dicts meeting (server_modified <= date AND size if given)
        - used_before_rev: True if we paged
        - pages_scanned: count of API calls
        - oldest_seen: the last entry on the last page we saw (or None)
    """
    on_or_before = _to_utc(on_or_before)
    nth = max(1, nth)
    per_page = max(1, min(100, per_page))

    matches: List[dict] = []
    before_rev: Optional[str] = None
    used_before_rev = False
    pages = 0
    oldest_seen: Optional[dict] = None

    while True:
        res = list_revisions_http(token, path, per_page, before_rev=before_rev)
        entries = res.get("entries", [])
        has_more = bool(res.get("has_more"))
        pages += 1
        if before_rev:
            used_before_rev = True

        if entries:
            oldest_seen = entries[-1]

        # Collect matches
        for e in entries:
            logger.info(e)
            # server_modified is RFC3339/ISO8601, Z-terminated
            sm = e.get("server_modified")
            if not sm:
                continue
            try:
                sm_dt = datetime.fromisoformat(sm.replace("Z", "+00:00"))
            except Exception:
                continue
            sm_dt = _to_utc(sm_dt)
            sz = e.get("size")
            if sm_dt <= on_or_before and (size is None or sz == size):
                matches.append(e)
                if len(matches) >= nth:
                    return matches[nth - 1], matches, used_before_rev, pages, oldest_seen

        if not has_more or not entries:
            # Exhausted history
            return None, matches, used_before_rev, pages, oldest_seen

        # Prepare next page using oldest rev from current page
        before_rev = entries[-1].get("rev")
        if not before_rev:
            # Defensive: if no rev is present, we can't page further
            return None, matches, used_before_rev, pages, oldest_seen

        if max_pages and pages >= max_pages:
            return None, matches, used_before_rev, pages, oldest_seen

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(description="Restore a Dropbox file to a prior revision by date (HTTP).")
    ap.add_argument("path", help="Dropbox file path (e.g., /folder/file.ext)")
    ap.add_argument(
        "--date",
        required=True,
        type=parse_date,
        help="Target date (on/before). Format YYYY-MM-DD (end-of-day UTC) or full ISO.",
    )
    ap.add_argument(
        "--size",
        type=int,
        default=None,
        help="Optional byte size filter. Only consider revisions with this exact size.",
    )
    ap.add_argument(
        "--nth",
        type=int,
        default=1,
        help="1-based index among matches after filtering (default: 1 = first match).",
    )
    ap.add_argument(
        "--per-page",
        type=int,
        default=100,
        help="Revisions per page (max 100). Default: 100.",
    )
    ap.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional cap on how many pages to scan (each page up to --per-page).",
    )
    ap.add_argument(
        "--list",
        type=int,
        default=0,
        metavar="N",
        help="List the first N matching revisions (after filters) instead of restoring.",
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="Actually restore the chosen revision (default is dry-run).",
    )

    args = ap.parse_args()

    try:
        token = get_access_token()
    except Exception as e:
        logger.error(e)
        sys.exit(2)

    path = args.path
    on_or_before = args.date
    size = args.size
    nth = max(1, args.nth)
    per_page = max(1, min(100, args.per_page))
    max_pages = args.max_pages

    # Find revision via HTTP pagination
    try:
        chosen, matches, used_before_rev, pages, oldest_seen = find_revision_by_date_paginated(
            token,
            path,
            on_or_before,
            per_page=per_page,
            size=size,
            nth=nth,
            max_pages=max_pages,
        )
    except Exception as e:
        logger.error(f"Error listing revisions: {e}")
        sys.exit(2)

    # Summary
    print("\n== Dry-Run Summary ==" if not args.execute else "\n== Action Summary ==")
    print(f"Path:             {path}")
    print(f"Target date:      {on_or_before.isoformat()}")
    if size is not None:
        print(f"Size filter:      {size} bytes ({human_size(size)})")
    print(f"nth:              {nth}")
    print(f"Per page:         {per_page}")
    print(f"Pages scanned:    {pages}")
    print(f"Paged (before_rev): {'yes' if used_before_rev else 'no'}")

    # List option
    if args.list:
        to_show = min(args.list, len(matches))
        print(f"\n-- Matching revisions (newest → older) [showing {to_show} of {len(matches)}] --")
        if to_show:
            widths = (27, 18, 36, 10, 26)
            print_row(("server_modified (UTC)", "size", "rev", "hash", "path_display"), widths)
            print_row(("-" * 27, "-" * 18, "-" * 36, "-" * 10, "-" * 26), widths)
            for e in matches[:to_show]:
                sm = e.get("server_modified", "")
                # Normalize to UTC iso
                try:
                    sm_iso = _to_utc(datetime.fromisoformat(sm.replace("Z", "+00:00"))).isoformat()
                except Exception:
                    sm_iso = sm
                sz = e.get("size", 0)
                ch = e.get("content_hash", "") or ""
                pd = e.get("path_display", "") or ""
                print_row((sm_iso, f"{sz} ({human_size(sz)})", e.get("rev", ""), ch[:10] + ("…" if len(ch) > 10 else ""), pd[:26]), widths)
        else:
            print("  (no matches)")
        return

    if not chosen:
        print("\nNo matching revision to restore with the provided criteria.")
        if oldest_seen is not None:
            try:
                oldest_iso = _to_utc(datetime.fromisoformat(oldest_seen["server_modified"].replace("Z", "+00:00"))).isoformat()
            except Exception:
                oldest_iso = oldest_seen.get("server_modified", "")
            sz = oldest_seen.get("size", 0)
            print(f"- Oldest seen:    {oldest_iso} "
                  f"(rev={oldest_seen.get('rev','')}, size={sz} / {human_size(sz)})")
        else:
            print("- This file has no accessible revisions.")
        sys.exit(1)

    # Show chosen revision
    print("\n-- Chosen revision --")
    try:
        chosen_iso = _to_utc(datetime.fromisoformat(chosen["server_modified"].replace("Z", "+00:00"))).isoformat()
    except Exception:
        chosen_iso = chosen.get("server_modified", "")
    print(f"server_modified:   {chosen_iso}")
    print(f"rev:               {chosen.get('rev','')}")
    print(f"size:              {chosen.get('size',0)} bytes ({human_size(chosen.get('size',0))})")
    ch = chosen.get("content_hash", "")
    if ch:
        print(f"content_hash:      {ch}")

    if not args.execute:
        print("\nDry run only. Use --execute to restore this revision.")
        return

    # Execute restore
    try:
        restored = restore_http(token, path, chosen.get("rev", ""))
        # Pretty print important fields
        r_sm = restored.get("server_modified", "")
        try:
            r_sm_iso = _to_utc(datetime.fromisoformat(r_sm.replace("Z", "+00:00"))).isoformat()
        except Exception:
            r_sm_iso = r_sm
        r_size = restored.get("size", 0)
        print("\nRestored successfully.")
        print(f"Current metadata: size={r_size} ({human_size(r_size)}), "
              f"server_modified={r_sm_iso}, "
              f"rev={restored.get('rev','')}")
    except Exception as e:
        logger.error(f"Error during restore: {e}")
        sys.exit(2)

if __name__ == "__main__":
    main()


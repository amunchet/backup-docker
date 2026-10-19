#!/usr/bin/env python3
"""
Time travel: put a Dropbox file back to the version it had on a given date.
Nothing changes unless you add --execute.

In Docker, run it from the backup-docker folder as
`docker compose exec dropbox-backup python time_travel.py ...`. Examples:

# Show the newest version saved on or before 2025-09-01
python time_travel.py "/Work/report.docx" --date 2025-09-01

# List the 20 newest versions saved on or before that date
python time_travel.py "/Work/report.docx" --date 2025-09-01 --list 20

# Only consider versions of exactly 1234567 bytes, and pick the 3rd newest of those
python time_travel.py "/Work/report.docx" --date 2025-09-01 --size 1234567 --nth 3

# Actually restore the chosen version
python time_travel.py "/Work/report.docx" --date 2025-09-01 --execute
"""
import argparse
import sys
from datetime import datetime, timezone

from common import dropbox_client, logger


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
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception as e:
        raise argparse.ArgumentTypeError(f"Invalid date '{date_str}': {e}")


def human_size(n: int) -> str:
    """A byte count as e.g. "1.50 MB"."""
    s = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    i = 0
    while f >= 1024.0 and i < len(s) - 1:
        f /= 1024.0
        i += 1
    return f"{f:.2f} {s[i]}"


def utc(dt: datetime) -> datetime:
    """Dropbox's datetimes are naive UTC; make them aware."""
    return dt.replace(tzinfo=timezone.utc)


def find_revision(dbx, path, on_or_before, size=None, nth=1, per_page=100, max_pages=None):
    """
    Scan the file's revisions newest first, a page at a time, for ones saved on or
    before `on_or_before` (and of exactly `size` bytes, if given).

    Returns (the nth match or None, the matches found, pages scanned, oldest revision seen).
    """
    matches, pages, oldest_seen, before_rev = [], 0, None, None
    while True:
        result = dbx.files_list_revisions(path, limit=per_page, before_rev=before_rev)
        pages += 1
        if result.entries:
            oldest_seen = result.entries[-1]
        for entry in result.entries:
            logger.debug(f"{entry.rev} {entry.server_modified} {entry.size}")
            if utc(entry.server_modified) <= on_or_before and (size is None or entry.size == size):
                matches.append(entry)
                if len(matches) == nth:
                    return entry, matches, pages, oldest_seen
        if not result.has_more or not result.entries or (max_pages and pages >= max_pages):
            return None, matches, pages, oldest_seen
        before_rev = result.entries[-1].rev


def main():
    """Command line: find a revision by date, list or show it, and restore it with --execute."""
    ap = argparse.ArgumentParser(description="Restore a Dropbox file to the version it had on a date.")
    ap.add_argument("path", help="Dropbox file path, e.g. /folder/file.ext (or ns:<id>/file.ext in a shared folder)")
    ap.add_argument("--date", required=True, type=parse_date,
                    help="Target date (on/before). Format YYYY-MM-DD (end-of-day UTC) or full ISO.")
    ap.add_argument("--size", type=int, default=None,
                    help="Optional byte size filter. Only consider revisions with this exact size.")
    ap.add_argument("--nth", type=int, default=1,
                    help="1-based index among matches after filtering (default: 1 = newest match).")
    ap.add_argument("--per-page", type=int, default=100, help="Revisions per page (max 100). Default: 100.")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="Optional cap on how many pages to scan (each page up to --per-page).")
    ap.add_argument("--list", type=int, default=0, metavar="N",
                    help="List the first N matching revisions (after filters) instead of restoring.")
    ap.add_argument("--execute", action="store_true", help="Actually restore the chosen revision (default is dry-run).")
    args = ap.parse_args()

    nth = max(1, args.nth)
    dbx = dropbox_client()
    try:
        chosen, matches, pages, oldest_seen = find_revision(
            dbx, args.path, args.date, size=args.size, nth=max(nth, args.list),
            per_page=max(1, min(100, args.per_page)), max_pages=args.max_pages,
        )
    except Exception as e:
        logger.error(f"Error listing revisions: {e}")
        sys.exit(2)

    print("\n== Dry-Run Summary ==" if not args.execute else "\n== Action Summary ==")
    print(f"Path:             {args.path}")
    print(f"Target date:      {args.date.isoformat()}")
    if args.size is not None:
        print(f"Size filter:      {args.size} bytes ({human_size(args.size)})")
    print(f"nth:              {nth}")
    print(f"Pages scanned:    {pages}")

    if args.list:
        shown = matches[:args.list]
        print(f"\n-- Matching revisions (newest -> older) [showing {len(shown)}] --")
        for e in shown:
            print(f"  {utc(e.server_modified).isoformat():<27}  {e.size:>12} ({human_size(e.size):>10})  "
                  f"rev {e.rev}  hash {(e.content_hash or '')[:10]}  {e.path_display or ''}")
        if not shown:
            print("  (no matches)")
        return

    if not chosen:
        print("\nNo matching revision to restore with the provided criteria.")
        if oldest_seen is not None:
            print(f"- Oldest seen:    {utc(oldest_seen.server_modified).isoformat()} "
                  f"(rev={oldest_seen.rev}, size={oldest_seen.size} / {human_size(oldest_seen.size)})")
        else:
            print("- This file has no accessible revisions.")
        sys.exit(1)

    print("\n-- Chosen revision --")
    print(f"server_modified:   {utc(chosen.server_modified).isoformat()}")
    print(f"rev:               {chosen.rev}")
    print(f"size:              {chosen.size} bytes ({human_size(chosen.size)})")
    if chosen.content_hash:
        print(f"content_hash:      {chosen.content_hash}")

    if not args.execute:
        print("\nDry run only. Use --execute to restore this revision.")
        return

    try:
        restored = dbx.files_restore(args.path, chosen.rev)
    except Exception as e:
        logger.error(f"Error during restore: {e}")
        sys.exit(2)
    print("\nRestored successfully.")
    print(f"Current metadata: size={restored.size} ({human_size(restored.size)}), "
          f"server_modified={utc(restored.server_modified).isoformat()}, rev={restored.rev}")


if __name__ == "__main__":  # pragma: no cover
    main()

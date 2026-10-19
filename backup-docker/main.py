#!/usr/bin/env python3
"""
Runs a backup or restore pass (SYNC_DIRECTION) every SLEEP_TIME seconds.

    python main.py          run until stopped
    python main.py --once   run one pass and exit
"""
import os
import sys
import time
from pathlib import Path

import pid
from dropbox.exceptions import DropboxException

import backup
import restore
from common import DROPBOX_FOLDER, WATCH_FOLDER, dropbox_client, logger

SLEEP_TIME = int(os.getenv("SLEEP_TIME") or 60)
SYNC_DIRECTION = (os.getenv("SYNC_DIRECTION") or "BACKUP").upper()


def main():
    """Check the settings, then run passes until stopped."""
    if SYNC_DIRECTION not in ("BACKUP", "RESTORE"):
        sys.exit(f"SYNC_DIRECTION must be BACKUP or RESTORE, not {SYNC_DIRECTION}.")
    if not (WATCH_FOLDER and DROPBOX_FOLDER):
        sys.exit("Set WATCH_FOLDER and DROPBOX_FOLDER in .env (./setup.py backup does this).")
    if SYNC_DIRECTION == "BACKUP" and not os.path.isdir(WATCH_FOLDER):
        sys.exit(f"The folder to back up, {WATCH_FOLDER}, doesn't exist.")

    dbx = dropbox_client()
    try:
        account = dbx.users_get_current_account()
    except DropboxException as e:
        sys.exit(f"Dropbox refused the saved settings: {e}\nRun ./setup.py check in the project folder to see why.")
    arrow = "->" if SYNC_DIRECTION == "BACKUP" else "<-"
    logger.info(f"{SYNC_DIRECTION}: {WATCH_FOLDER} {arrow} Dropbox {DROPBOX_FOLDER} ({account.email}), every {SLEEP_TIME}s")
    sync = backup.monitor_and_upload if SYNC_DIRECTION == "BACKUP" else restore.sync_down

    if "--once" in sys.argv:
        sync(dbx)
        return
    while True:
        try:
            sync(dbx)
        except Exception:
            # e.g. the network was down; the next pass tries again.
            logger.exception(f"{SYNC_DIRECTION} pass failed")
        time.sleep(SLEEP_TIME)


if __name__ == "__main__":  # pragma: no cover
    if os.getenv("DOCKER"):
        main()
    else:
        # One instance per copy of this folder, so cron can start it regularly.
        with pid.PidFile(piddir=Path(__file__).resolve().parent):
            main()

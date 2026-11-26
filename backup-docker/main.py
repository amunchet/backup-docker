#!/usr/bin/env python3
"""
Runs a backup or restore pass (SYNC_DIRECTION) every SLEEP_TIME seconds.

    python main.py          run until stopped
    python main.py --once   run one pass and exit
"""
import fcntl
import os
import signal
import sys
import time
from pathlib import Path

from dropbox.exceptions import AuthError, BadInputError, InternalServerError
from requests.exceptions import RequestException

import backup
import restore
from common import DROPBOX_FOLDER, WATCH_FOLDER, dropbox_client, logger, rooted

SLEEP_TIME = int(os.getenv("SLEEP_TIME") or 60)
SYNC_DIRECTION = (os.getenv("SYNC_DIRECTION") or "BACKUP").upper()
# Failed passes are retried, e.g. while the network or DNS is down, for this long. Then main.py
# exits, so Docker or systemd starts it afresh, and the restart shows that something is wrong.
GIVE_UP_AFTER = 60 * 60


def main():
    """Check the settings, then run passes until stopped, riding out outages."""
    if SYNC_DIRECTION not in ("BACKUP", "RESTORE"):
        sys.exit(f"SYNC_DIRECTION must be BACKUP or RESTORE, not {SYNC_DIRECTION}.")
    if not (WATCH_FOLDER and DROPBOX_FOLDER):
        sys.exit("Set WATCH_FOLDER and DROPBOX_FOLDER in .env (./setup.py backup does this).")
    if SYNC_DIRECTION == "BACKUP" and not os.path.isdir(WATCH_FOLDER):
        sys.exit(f"The folder to back up, {WATCH_FOLDER}, doesn't exist.")

    dbx = dropbox_client()
    sync = backup.monitor_and_upload if SYNC_DIRECTION == "BACKUP" else restore.sync_down
    rooted_dbx, folder = rooted(dbx, DROPBOX_FOLDER.rstrip("/"))
    once = "--once" in sys.argv
    started = failing_since = None
    while True:
        try:
            # Each pass reaches Dropbox first, so even one with nothing to upload notices an
            # outage, or that Dropbox stopped accepting the settings.
            account = dbx.users_get_current_account()
            if not started:
                arrow = "->" if SYNC_DIRECTION == "BACKUP" else "<-"
                logger.info(f"{SYNC_DIRECTION}: {WATCH_FOLDER} {arrow} Dropbox {DROPBOX_FOLDER} ({account.email}), every {SLEEP_TIME}s")
                started = True
            sync(rooted_dbx, folder)
            failing_since = None
        except (AuthError, BadInputError) as e:
            # Waiting can't fix these, so stop now.
            sys.exit(f"Dropbox refused the saved settings: {e}\nRun ./setup.py check in the project folder to see why.")
        except Exception as e:
            if once:
                raise
            if failing_since is None:
                failing_since = time.monotonic()
            if time.monotonic() - failing_since >= GIVE_UP_AFTER:
                sys.exit(f"{SYNC_DIRECTION} passes have failed for {GIVE_UP_AFTER // 60} minutes, the last with: {e}\n"
                         "Exiting, so Docker or systemd starts it afresh.")
            # An outage (network or DNS down, Dropbox erring) needs no traceback; anything else may be a bug.
            logger.error(f"{SYNC_DIRECTION} pass failed, trying again in {SLEEP_TIME}s: {e}",
                         exc_info=not isinstance(e, (RequestException, InternalServerError)))
        if once:
            return
        time.sleep(SLEEP_TIME)


if __name__ == "__main__":  # pragma: no cover
    # Stop cleanly on `docker stop` or `systemctl stop`, saving what the pass has done so far.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit())
    # One copy at a time per folder, so a cron job or a manual run can't overlap the service. The
    # kernel drops the lock when the process ends, however it ends, so it never goes stale.
    with open(Path(__file__).with_name("main.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(f"{Path(__file__).resolve().parent.name} is already running, e.g. as a service or from cron.")
        main()

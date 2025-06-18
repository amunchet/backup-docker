# Main controller for backup/restorer system
import os
import time
import hashlib
import sys
import dropbox
import requests
import pid
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import WriteMode, UploadSessionCursor, CommitInfo
from dotenv import load_dotenv
from backup_logger import logger

from backup import main as backup_main
from restore import sync_down as restore_main

# Load environment variables from .env file
load_dotenv()

SLEEP_TIME = os.environ.get("SLEEP_TIME") or 60
SYNC_DIRECTION = os.environ.get("SYNC_DIRECTION") or "BACKUP"

def inner_main():
    """
    Runner function
    """
    while True:
        if SYNC_DIRECTION == "BACKUP":
            logger.info("Starting up BACKUP...")
            backup_main()
        else:
            logger.info("Starting up RESTORE...")
            restore_main()

        logger.info("Sleeping...")
        time.sleep(int(SLEEP_TIME))



if __name__ == "__main__": # pragma: no cover
    logger.info("Starting up up...")
    if os.getenv("DOCKER"):
        print("In a Docker, ignoring PID...")
        inner_main()
    else:
        print("Starting up with PID handler...")
        with pid.PidFile():
            inner_main()
 

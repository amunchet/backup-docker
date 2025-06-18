import os
import sys
import hashlib
import dropbox
from dropbox.files import FileMetadata
from dotenv import load_dotenv
from backup_logger import logger

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


def ensure_local_folder():
    if not os.path.exists(WATCH_FOLDER):
        os.makedirs(WATCH_FOLDER)


def calculate_md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def calculate_md5_file(path: str) -> str:
    hash_md5 = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def conditional_download(dbx, dropbox_path):
    local_path = os.path.join(WATCH_FOLDER, os.path.basename(dropbox_path))

    try:
        metadata, res = dbx.files_download(dropbox_path)
        remote_data = res.content
        remote_md5 = calculate_md5_bytes(remote_data)

        if os.path.exists(local_path):
            local_md5 = calculate_md5_file(local_path)
            if local_md5 == remote_md5:
                logger.info(f"No change for {local_path}, skipping.")
                return

        logger.info(f"Downloading updated file: {dropbox_path}")
        with open(local_path, "wb") as f:
            f.write(remote_data)

    except Exception as e:
        logger.error(f"Failed to download {dropbox_path}: {e}")


def sync_down():
    ensure_local_folder()

    with dropbox.Dropbox(
        oauth2_access_token=DROPBOX_TOKEN,
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
        app_key=APP_KEY,
        app_secret=APP_SECRET
    ) as dbx:
        try:
            dbx.users_get_current_account()
        except dropbox.exceptions.AuthError as err:
            logger.error(f"Dropbox authentication error: {err}")
            sys.exit(1)

        try:
            result = dbx.files_list_folder(DROPBOX_FOLDER)
            entries = result.entries

            while result.has_more:
                result = dbx.files_list_folder_continue(result.cursor)
                entries.extend(result.entries)

            for entry in entries:
                if isinstance(entry, FileMetadata):
                    conditional_download(dbx, entry.path_lower)

        except Exception as err:
            logger.error(f"Sync error: {err}")
            raise


if __name__ == "__main__":
    logger.info("Starting Dropbox -> Local sync (if changed)...")
    sync_down()
    logger.info("Done!")


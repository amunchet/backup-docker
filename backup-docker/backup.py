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


# Load environment variables from .env file
load_dotenv()

# Environment variables
WATCH_FOLDER = os.getenv("WATCH_FOLDER") or "/backup"
if os.getenv("DOCKER"):
    WATCH_FOLDER = "/backup"

DROPBOX_FOLDER = os.getenv("DROPBOX_FOLDER")
DROPBOX_TOKEN = os.getenv("DROPBOX_TOKEN")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
APP_KEY = os.getenv("APP_KEY")
APP_SECRET = os.getenv("APP_SECRET")

CHUNK_SIZE = 4 * 1024 * 1024  # 4MB chunks

# Dropbox client abstraction
class DropboxClient:
    def __init__(self, dbx):
        self.dbx = dbx

    def upload(self, file_path, dropbox_path):  # pragma: no cover
        file_size = os.path.getsize(file_path)
        logger.info(f"Uploading {file_path} ({file_size} bytes) to Dropbox as {dropbox_path}...")
        try:
            with open(file_path, 'rb') as f:
                if file_size <= CHUNK_SIZE:
                    logger.info(f"Small file detected, uploading normally...")
                    self.dbx.files_upload(f.read(), dropbox_path, mode=WriteMode('overwrite'))
                else:
                    logger.info(f"Large file detected, using chunked upload...")
                    upload_session_start_result = self.dbx.files_upload_session_start(f.read(CHUNK_SIZE))
                    cursor = UploadSessionCursor(session_id=upload_session_start_result.session_id, offset=f.tell())
                    commit = CommitInfo(path=dropbox_path, mode=WriteMode('overwrite'))

                    while f.tell() < file_size:
                        if (file_size - f.tell()) <= CHUNK_SIZE:
                            logger.info(f"Finishing upload session at offset {f.tell()}...")
                            self.dbx.files_upload_session_finish(f.read(CHUNK_SIZE), cursor, commit)
                        else:
                            logger.info(f"Appending upload session at offset {f.tell()}...")
                            self.dbx.files_upload_session_append_v2(f.read(CHUNK_SIZE), cursor)
                            cursor.offset = f.tell()

        except ApiError as err:
            if err.error.is_path() and err.error.get_path().reason.is_insufficient_space():
                logger.error("ERROR: Cannot back up; insufficient space in Dropbox account.")
                raise err
            elif err.user_message_text:
                logger.error(f"Dropbox API error: {err.user_message_text}")
                raise err
            else:
                logger.error(f"Dropbox API error: {err}")
                raise err

        except (requests.exceptions.ConnectionError, socket.timeout) as net_err:
            logger.error(f"Network error during upload: {net_err}")
            raise net_err

        except Exception as unexpected_err:
            logger.error(f"Unexpected error during upload: {unexpected_err}")
            raise unexpected_err
# Function to calculate the MD5 checksum of a file
def calculate_md5(file_path):
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

# Function to read checksums from a file
def read_checksums(file_path):
    checksums = {}
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            for line in f:
                splits = line.strip().split()
                file_md5 = splits[-1]
                file_name = line.replace(file_md5, "").strip()
                # file_name, file_md5 = line.strip().split()
                checksums[file_name] = file_md5
    return checksums

# Function to write checksums to a file
def write_checksums(file_path, checksums):
    with open(file_path, "w") as f:
        for file_name, file_md5 in checksums.items():
            f.write(f"{file_name} {file_md5}\n")

# Main function to monitor the folder and upload changed files
def monitor_and_upload(dropbox_client):
    checksums_file = os.path.join(WATCH_FOLDER, "checksums.txt")

    # Read existing checksums
    old_checksums = read_checksums(checksums_file)
    new_checksums = {}
    
    # Load ignore file
    ignore_file = os.path.join(WATCH_FOLDER, "ignore.txt")
    ignore = []
    if os.path.exists(ignore_file):
        with open(ignore_file) as f:
            ignore = [x.strip() for x in f.readlines()]


    # Calculate new checksums and compare with old ones
    for file_name in os.listdir(WATCH_FOLDER):
        file_path = os.path.join(WATCH_FOLDER, file_name)
        if os.path.isfile(file_path) and file_name not in ignore:
            file_md5 = calculate_md5(file_path)
            new_checksums[file_name] = file_md5
            if old_checksums.get(file_name) != file_md5:
                dropbox_path = os.path.join(DROPBOX_FOLDER, file_name)
                logger.info("Calling upload")
                logger.info(file_path)
                logger.info(dropbox_path)
                dropbox_client.upload(file_path, dropbox_path)
                logger.info(f"Uploaded {file_name} to Dropbox.")
            else:
                logger.debug(f"File stayed the same:{file_name}")

    # Write new checksums to file
    write_checksums(checksums_file, new_checksums)

def main(): # pragma: no cover
    if not WATCH_FOLDER or not DROPBOX_FOLDER or not DROPBOX_TOKEN or not DROPBOX_REFRESH_TOKEN or not APP_KEY or not APP_SECRET:
        logger.error("Please set WATCH_FOLDER, DROPBOX_FOLDER, DROPBOX_TOKEN, DROPBOX_REFRESH_TOKEN, APP_KEY, and APP_SECRET environment variables.")
        sys.exit(1)

    with dropbox.Dropbox(
        oauth2_access_token= DROPBOX_TOKEN,
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
        app_key = APP_KEY,
        app_secret = APP_SECRET
    ) as dbx:
        dbx.users_get_current_account()
        
        dropbox_client = DropboxClient(dbx)
        monitor_and_upload(dropbox_client)
        logger.info("Done!")

       

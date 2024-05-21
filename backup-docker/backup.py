import os
import time
import hashlib
import sys
import dropbox
import requests
import pid
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import WriteMode
from dotenv import load_dotenv
from backup_logger import logger


# Load environment variables from .env file
load_dotenv()

# Environment variables
WATCH_FOLDER = os.getenv("WATCH_FOLDER")
DROPBOX_FOLDER = os.getenv("DROPBOX_FOLDER")
DROPBOX_TOKEN = os.getenv("DROPBOX_TOKEN")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
APP_KEY = os.getenv("APP_KEY")
APP_SECRET = os.getenv("APP_SECRET")

# Function to refresh the Dropbox access token
def refresh_access_token():
    token_url = "https://api.dropbox.com/oauth2/token"
    logger.debug(DROPBOX_REFRESH_TOKEN)
    data = {
        "grant_type": "refresh_token",
        "refresh_token": DROPBOX_REFRESH_TOKEN,
        "client_id": APP_KEY,
        "client_secret": APP_SECRET,
    }

    response = requests.post(token_url, data=data)
    if response.status_code != 200:
        logger.error(response.text)
        sys.exit("ERROR: Unable to refresh access token.")

    tokens = response.json()
    new_access_token = tokens['access_token']

    # Update the environment variable and .env file
    os.environ["DROPBOX_TOKEN"] = new_access_token
    with open(".env", "a") as env_file:
        env_file.write(f"DROPBOX_TOKEN={new_access_token}\n")

    return new_access_token

# Dropbox client abstraction
class DropboxClient:
    def __init__(self, dbx):
        self.dbx = dbx

    def upload(self, file_path, dropbox_path):
        with open(file_path, 'rb') as f:
            logger.info(f"Uploading {file_path} to Dropbox as {dropbox_path}...")
            try:
                self.dbx.files_upload(f.read(), dropbox_path, mode=WriteMode('overwrite'))
            except ApiError as err:
                if err.error.is_path() and err.error.get_path().reason.is_insufficient_space():
                    sys.exit("ERROR: Cannot back up; insufficient space.")
                elif err.user_message_text:
                    logger.error(err.user_message_text)
                    sys.exit()
                else:
                    logger.error(err)
                    sys.exit()

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
                file_name, file_md5 = line.strip().split()
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

    # Calculate new checksums and compare with old ones
    for file_name in os.listdir(WATCH_FOLDER):
        file_path = os.path.join(WATCH_FOLDER, file_name)
        if os.path.isfile(file_path):
            file_md5 = calculate_md5(file_path)
            new_checksums[file_name] = file_md5
            if old_checksums.get(file_name) != file_md5:
                dropbox_path = os.path.join(DROPBOX_FOLDER, file_name)
                dropbox_client.upload(file_path, dropbox_path)
                logger.info(f"Uploaded {file_name} to Dropbox.")

    # Write new checksums to file
    write_checksums(checksums_file, new_checksums)

def main():
    if not WATCH_FOLDER or not DROPBOX_FOLDER or not DROPBOX_TOKEN or not DROPBOX_REFRESH_TOKEN or not APP_KEY or not APP_SECRET:
        logger.error("Please set WATCH_FOLDER, DROPBOX_FOLDER, DROPBOX_TOKEN, DROPBOX_REFRESH_TOKEN, APP_KEY, and APP_SECRET environment variables.")
        sys.exit(1)

    try:
        dbx = dropbox.Dropbox(DROPBOX_TOKEN)
        dbx.users_get_current_account()
    except AuthError:
        # Refresh the access token if it's expired
        new_token = refresh_access_token()
        dbx = dropbox.Dropbox(new_token)

    dropbox_client = DropboxClient(dbx)
    monitor_and_upload(dropbox_client)
    logger.info("Done!")

if __name__ == "__main__":
    logger.info("Starting up...")
    while True:
        with pid.PidFile():
            main()
            logger.info("Sleeping...")
            time.sleep(60)

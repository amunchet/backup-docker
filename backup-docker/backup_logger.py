import logging
import logging.handlers
import os

# Define the custom logger
logger = logging.getLogger('DropboxBackupLogger')
level = logging.INFO
logger.setLevel(level)

# Define the custom log format
formatter = logging.Formatter('[DROPBOX-BACKUP %(levelname)s] %(asctime)s - %(name)s - %(message)s')

# Define the stderr handler
stderr_handler = logging.StreamHandler()
stderr_handler.setLevel(level)
stderr_handler.setFormatter(formatter)

# Define the file handler
log_file = '/var/log/dropbox-backup.log'
file_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=10485760, backupCount=5)
file_handler.setLevel(level)
file_handler.setFormatter(formatter)

# Add handlers to the logger
logger.addHandler(stderr_handler)
logger.addHandler(file_handler)

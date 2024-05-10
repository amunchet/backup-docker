# Backup Docker
Backup Docker System

This project is a docker (or a standalone installation) and checks a specified folder (mounted volume in docker) for changes, then uploads those to a cloud provider (such as Dropbox or S3).

## Features
- Incremental uploads only.  Takes checksums and then updates a `checksum.txt` file
- Logs errors to syslog (or rsyslog)
- Intended to run as a CRON


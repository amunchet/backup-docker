#!/bin/bash
# Runs every test suite. Needs the packages from backup-docker/requirements.txt and
# dropbox-mcp/requirements.txt (see "Development" in README.md).
set -e
cd "$(dirname "$0")"
PYTHONPATH=. pytest -q tests
backup-docker/run_tests.sh -q
dropbox-mcp/run_tests.sh -q

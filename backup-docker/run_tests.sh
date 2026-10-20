#!/bin/bash
cd "$(dirname "$0")"
PYTHONPATH=. pytest --cov=. --cov-report=term-missing --cov-fail-under=95 tests "$@"

#!/bin/bash
cd "$(dirname "$0")"
PYTHONPATH=. pytest tests "$@"

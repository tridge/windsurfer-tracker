#!/bin/bash
set -e
cd "$(dirname "$0")/.."
python3 -m pytest test/ -v --tb=short "$@"

#!/bin/sh

python -m uvicorn lenny.app:app --host 0.0.0.0 --port 1337 --workers=${LENNY_WORKERS:-1} --log-level=${LENNY_LOG_LEVEL:-info} &

nginx &

python scripts/load_open_books.py

wait

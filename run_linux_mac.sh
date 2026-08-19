#!/usr/bin/env bash
set -e
if [ ! -d .venv ]; then python3 -m venv .venv; fi
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m webbrowser http://127.0.0.1:8000 >/dev/null 2>&1 || true
uvicorn app.main:app --host 127.0.0.1 --port 8000

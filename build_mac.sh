#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt
python3 -m PyInstaller \
  -y \
  --name "ERNI Live Clipper" \
  --windowed \
  --onedir \
  --add-data "HOTKEYS.md:." \
  app.py

echo "Built: dist/ERNI Live Clipper.app"

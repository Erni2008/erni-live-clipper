#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt

vendor_dir="vendor/macos"
mkdir -p "$vendor_dir"
deno_bin="$vendor_dir/deno"
if [ ! -x "$deno_bin" ]; then
  arch_name="$(uname -m)"
  if [ "$arch_name" = "arm64" ]; then
    deno_target="aarch64-apple-darwin"
  else
    deno_target="x86_64-apple-darwin"
  fi
  deno_zip="$vendor_dir/deno-${deno_target}.zip"
  deno_url="https://github.com/denoland/deno/releases/latest/download/deno-${deno_target}.zip"
  echo "Downloading deno for ${deno_target}..."
  curl -L --fail --retry 5 --retry-delay 2 -o "$deno_zip" "$deno_url"
  unzip -o "$deno_zip" -d "$vendor_dir"
  chmod +x "$deno_bin"
fi

python3 -m PyInstaller \
  -y \
  --name "ERNI Live Clipper" \
  --windowed \
  --onedir \
  --add-data "HOTKEYS.md:." \
  --add-binary "$deno_bin:." \
  app.py

echo "Built: dist/ERNI Live Clipper.app"

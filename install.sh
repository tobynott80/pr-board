#!/usr/bin/env bash
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$HOME/.config/pr-board/config.json"

mkdir -p "$HOME/.local/bin"
mkdir -p -m 700 "$(dirname "$CONFIG")"
ln -sf "$ROOT/bin/pr-board" "$HOME/.local/bin/pr-board"

if [ ! -f "$CONFIG" ]; then
  install -m 600 "$ROOT/config.example.json" "$CONFIG"
  echo "created $CONFIG, add your jira email and api token to it"
fi

echo "installed, run pr-board"

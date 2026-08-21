#!/bin/bash
# Build docs/slides/mec-cast-architecture.pptx from docs/slides/build-deck.js.
#
#   bash .claude/skills/doc-sync/scripts/build_deck.sh
#
# pptxgenjs is not vendored in the repo (it would add ~19 packages of
# node_modules to a repo that is otherwise Rust/Python/ROS). It lives in a
# scratch dir outside the tree; this script sets that up on first run.
set -euo pipefail

# nvm is one way to get node, not the only one. Guard the source so
# `set -e` does not abort on a machine where node came from a package
# manager instead.
if [ -s "$HOME/.nvm/nvm.sh" ]; then . "$HOME/.nvm/nvm.sh"; fi
command -v node >/dev/null || {
  echo "ERROR: node not on PATH (nvm is interactive-only in this shell)."
  exit 1
}

REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
BUILD_DIR="$HOME/.deckbuild"
SRC="$REPO/docs/slides/build-deck.js"
OUT="mec-cast-architecture.pptx"

[ -f "$SRC" ] || { echo "ERROR: generator missing: $SRC"; exit 1; }

mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"
# Not `npm init -y`: it derives the package name from the directory, and
# ".deckbuild" is an invalid npm name (leading dot), which newer npm rejects
# outright. A minimal manifest is all that is needed to host one dependency.
[ -f package.json ] || cat > package.json <<'JSON'
{ "name": "deckbuild", "version": "1.0.0", "private": true }
JSON
if ! node -e "require('pptxgenjs')" 2>/dev/null; then
  echo "==> installing pptxgenjs into $BUILD_DIR"
  npm install pptxgenjs --no-audit --no-fund 2>&1 | tail -2
fi

cp "$SRC" "$BUILD_DIR/build-deck.js"
node build-deck.js "$OUT"
cp "$OUT" "$REPO/docs/slides/$OUT"

echo
ls -la "$REPO/docs/slides/$OUT"
echo
echo "Now run the QA gate:"
echo "  python .claude/skills/doc-sync/scripts/qa_pptx.py docs/slides/$OUT"

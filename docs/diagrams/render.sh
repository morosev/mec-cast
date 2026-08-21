#!/bin/bash
# Render the Mermaid diagrams that need a rendered artifact.
#
#   bash docs/diagrams/render.sh          PNG only (the committed form)
#   bash docs/diagrams/render.sh --svg    also emit SVG, for print/papers
#
# Only the two detailed dataflow diagrams produce files. PNG is committed
# because it is what GitHub can display; SVG is generated on demand for a
# paper or poster and is gitignored — vector costs ~294 KB of binary for a
# use that may never arise, and it is ten seconds to regenerate.
#
# architecture-overview.mmd and lab-deployment.mmd deliberately produce
# NOTHING. Their source is embedded directly in README.md inside a ```mermaid
# fence, which GitHub renders natively — so a committed image would only be a
# second copy to keep in sync.
#
# Note: GitHub renders mermaid inside markdown fences, NOT standalone .mmd
# files. And a mermaid .svg uses <foreignObject> for its labels, which
# GitHub's sanitiser strips — which is why the GitHub-facing copies are PNG.
set -euo pipefail

export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
# Guarded: under `set -e` a bare `[ -s … ] && .` aborts the script on a
# machine where node came from a package manager rather than nvm.
if [ -s "$NVM_DIR/nvm.sh" ]; then . "$NVM_DIR/nvm.sh"; fi

DIR="$(cd "$(dirname "$0")" && pwd)"

MMDC="$(command -v mmdc || true)"
if [ -z "$MMDC" ] && [ -x "$HOME/.deckbuild/node_modules/.bin/mmdc" ]; then
  MMDC="$HOME/.deckbuild/node_modules/.bin/mmdc"
fi
if [ -z "$MMDC" ]; then
  echo "mermaid-cli not found. Install it with:"
  echo "  npm install -g @mermaid-js/mermaid-cli"
  echo "Or just edit the .mmd files — the overview diagrams render on GitHub"
  echo "from the fences in README.md without any toolchain."
  exit 1
fi

CFG="$(mktemp)"
trap 'rm -f "$CFG"' EXIT
cat > "$CFG" <<'JSON'
{ "args": ["--no-sandbox", "--disable-dev-shm-usage"] }
JSON

WANT_SVG=0
[ "${1:-}" = "--svg" ] && WANT_SVG=1

for f in "$DIR"/dataflow-*.mmd; do
  base="$(basename "$f" .mmd)"
  echo "==> $base"
  "$MMDC" -i "$f" -o "$DIR/$base.png" -b white -p "$CFG" -w 4800 --quiet
  if [ "$WANT_SVG" = 1 ]; then
    "$MMDC" -i "$f" -o "$DIR/$base.svg" -b white -p "$CFG" --quiet
    echo "    + $base.svg (gitignored — for print only)"
  fi
done

echo
echo "Source-only (rendered by GitHub from the fences in README.md):"
for f in "$DIR"/architecture-overview.mmd "$DIR"/lab-deployment.mmd; do
  echo "  $(basename "$f")"
done
echo
ls -la "$DIR"/dataflow-*.png "$DIR"/dataflow-*.svg 2>/dev/null \
  | awk '{printf "  %-58s %7.0f KB\n", $9, $5/1024}'
echo
echo "The one-picture hero is separate:"
echo "  bash .claude/skills/doc-sync/scripts/render_hero.sh"

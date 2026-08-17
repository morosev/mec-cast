#!/bin/bash
# Render docs/diagrams/system-hero.html to two PNGs.
#
#   bash .claude/skills/doc-sync/scripts/render_hero.sh [scale]
#
#   system-hero.png      full quality (default 1.5x = 2880x1620) — slides, print
#   system-hero-web.png  1600px wide, quantised — embedded in README.md
#
# Two files on purpose: the README is the most-loaded page in the repo and
# must not pull a multi-megabyte image. GitHub renders READMEs at roughly a
# 900px content width, so 1600px is already 2x retina there.
#
# No film grain. It was added to hide gradient banding on projectors — a
# problem never actually observed — and it cost +71% file size (4225 KB vs
# 2470 KB at 2x) because random noise defeats PNG compression.
set -euo pipefail

[ -s "$HOME/.nvm/nvm.sh" ] && . "$HOME/.nvm/nvm.sh"
command -v node >/dev/null || { echo "ERROR: node not on PATH (source nvm)"; exit 1; }

REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
BUILD_DIR="$HOME/.deckbuild"
SRC="$REPO/docs/diagrams/system-hero.html"
OUT="$REPO/docs/diagrams/system-hero.png"
WEB="$REPO/docs/diagrams/system-hero-web.png"
SCALE="${1:-1.5}"
WEB_WIDTH=1600

[ -f "$SRC" ] || { echo "ERROR: missing $SRC"; exit 1; }
[ -d "$BUILD_DIR/node_modules/puppeteer" ] || [ -d "$BUILD_DIR/node_modules/puppeteer-core" ] || {
  echo "ERROR: puppeteer not found in $BUILD_DIR"
  echo "  cd $BUILD_DIR && npm install @mermaid-js/mermaid-cli"
  exit 1; }

cd "$BUILD_DIR"
SRC="$SRC" OUT="$OUT" SCALE="$SCALE" node -e '
const puppeteer = require("puppeteer");
(async () => {
  const b = await puppeteer.launch({
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--force-color-profile=srgb"],
  });
  const p = await b.newPage();
  await p.setViewport({
    width: 1920, height: 1080,
    deviceScaleFactor: Number(process.env.SCALE),
  });
  await p.goto("file://" + process.env.SRC, { waitUntil: "networkidle0" });
  await p.evaluate(() => document.fonts.ready);   // Lato loaded before capture
  await p.screenshot({ path: process.env.OUT, type: "png" });
  await b.close();
  console.log("wrote " + process.env.OUT);
})().catch(e => { console.error(e); process.exit(1); });
'

# Web variant. Quantising to an adaptive 256-colour palette is what makes this
# small; the design uses few hues, so banding stays invisible at this size.
PY="$REPO/telemetry/python/.venv/bin/python"
if [ -x "$PY" ]; then
  OUT="$OUT" WEB="$WEB" WEB_WIDTH="$WEB_WIDTH" "$PY" - <<'PYEOF'
import os
from PIL import Image

src, dst = os.environ["OUT"], os.environ["WEB"]
width = int(os.environ["WEB_WIDTH"])
img = Image.open(src).convert("RGB")
h = round(img.height * width / img.width)
img = img.resize((width, h), Image.LANCZOS)
img.quantize(colors=256, method=Image.MEDIANCUT).save(dst, "PNG", optimize=True)
print(f"wrote {dst}  ({width}x{h}, {os.path.getsize(dst)/1024:.0f} KB)")
PYEOF
else
  echo "NOTE: python venv not found; skipped the web variant"
fi

ls -la "$OUT" "$WEB" 2>/dev/null | awk '{printf "  %-58s %7.0f KB\n", $9, $5/1024}'

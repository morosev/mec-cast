#!/bin/bash
# Cut a platform release: annotated tag + GitHub Release carrying the changelog.
#
#   bash scripts/tag-release.sh 0.2.0 "Version reporting and OCI image stamps"
#   bash scripts/tag-release.sh 0.2.0 "..." --dry-run
#
# Tags are `platform-vX.Y.Z`, a separate namespace from the legacy client's
# `vX.Y.Z` (scripts/release.sh). See RELEASING.md for what each MAJOR/MINOR/
# PATCH bump means here — the short version is that MAJOR marks a change that
# makes runs before and after it incomparable.
#
# No assets are attached. The artifacts are the images in GHCR and the source
# at this SHA; the Release exists to carry notes, which are worth having.
set -euo pipefail

cd "$(dirname "$0")/.."

VERSION=${1:-}
DESCRIPTION=${2:-}
DRY_RUN=${3:-}

if [ -z "$VERSION" ] || [ -z "$DESCRIPTION" ]; then
  sed -n '2,13p' "$0"
  exit 2
fi

if ! echo "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "ERROR: version must be X.Y.Z (no 'v', no 'platform-' — both are added)."
  exit 2
fi

TAG="platform-v${VERSION}"
DATE=$(date +%Y-%m-%d)

# ─── preflight ────────────────────────────────────────────────────────────
fail() { echo "ERROR: $1"; exit 1; }

git rev-parse --git-dir >/dev/null 2>&1 || fail "not a git checkout"
[ -z "$(git status --porcelain)" ] || fail "working tree is dirty — commit or stash first"

BRANCH=$(git rev-parse --abbrev-ref HEAD)
[ "$BRANCH" = "main" ] || fail "on branch '$BRANCH'; platform releases are cut from main"

git rev-parse "$TAG" >/dev/null 2>&1 && fail "tag $TAG already exists"

git fetch --quiet origin main 2>/dev/null || echo "WARNING: could not fetch origin"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main 2>/dev/null || echo "$LOCAL")
[ "$LOCAL" = "$REMOTE" ] || fail "local main differs from origin/main — push or pull first"

# The images for this SHA only exist if CI went green, and the tag is worth
# little if it points at a commit whose pipeline failed. Advisory, not fatal:
# gh may be unauthenticated, and a release should not be blocked by that.
if command -v gh >/dev/null 2>&1; then
  CI=$(gh run list --commit "$LOCAL" --limit 1 --json conclusion \
       --jq '.[0].conclusion' 2>/dev/null || echo "")
  case "$CI" in
    success) echo "  CI: green on $(git rev-parse --short HEAD)" ;;
    "")      echo "  CI: could not determine (gh unauthenticated or no run yet)" ;;
    *)       echo "  CI: ${BOLD:-}$CI${RST:-} on $(git rev-parse --short HEAD)"
             read -r -p "  Pipeline is not green. Tag anyway? [y/N] " ans
             [ "$ans" = "y" ] || exit 1 ;;
  esac
else
  fail "gh CLI not found — needed to create the Release"
fi

# ─── notes ────────────────────────────────────────────────────────────────
# Range is bounded by the previous PLATFORM tag. Using `git describe` unfiltered
# would anchor to v1.0.3 and produce a changelog spanning the whole restructure.
PREV_TAG=$(git describe --tags --match 'platform-v*' --abbrev=0 HEAD 2>/dev/null || echo "")

if [ -n "$PREV_TAG" ]; then
  RANGE="${PREV_TAG}..HEAD"
  RANGE_LABEL="Changes since ${PREV_TAG}"
  COMPARE="https://github.com/morosev/mec-cast/compare/${PREV_TAG}...${TAG}"
else
  # First platform tag. The restructure commit is the honest starting point —
  # everything before it belongs to the WebRTC-demo era, not this platform.
  FIRST=$(git log --diff-filter=A --format=%H -- telemetry/src/lib.rs | tail -1)
  RANGE="${FIRST}..HEAD"
  RANGE_LABEL="Changes since the platform restructure"
  COMPARE=""
fi

COMMIT_LOG=$(git --no-pager log "$RANGE" --oneline --no-decorate --no-merges | sed 's/^/- /')
SHORT=$(git rev-parse --short HEAD)

SUBMODULES=$(git submodule status | awk '{printf "| `%s` | `%s` |\n", $2, substr($1,2,12)}')

NOTES=$(cat <<EOF
${DESCRIPTION}

Container images for this release are already published; the tag names a
commit, it does not trigger a build.

### Deploying this version

\`\`\`bash
git fetch --tags && git checkout ${TAG}
bash deploy/lab/deploy.sh <role> <user@host>   # ue | edge | infra | gnb
\`\`\`

Then confirm what the host is actually running:

\`\`\`bash
make version
\`\`\`

It reports the role, the commit, and the commit each running container was
built from — and warns when those disagree, which is the failure that
silently detaches a measurement campaign from its source.

### Images

\`\`\`bash
docker pull ghcr.io/morosev/mec-cast-ros:sha-${SHORT}
docker pull ghcr.io/morosev/mec-cast-ran:sha-${SHORT}
\`\`\`

Pin the \`sha-\` tags for a campaign. \`:main\` moves.

### Reproducing a run from this version

Every \`runs/<id>/run.json\` records the repo SHA, dirty flag and submodule
pins, so a run is reproducible from its own metadata rather than from this
page. This release is \`${SHORT}\` with:

| Submodule | Commit |
|---|---|
${SUBMODULES}

### ${RANGE_LABEL}

${COMMIT_LOG}
$([ -n "$COMPARE" ] && printf '\n**Full diff**: %s\n' "$COMPARE")
EOF
)

# ─── act ──────────────────────────────────────────────────────────────────
if [ "$DRY_RUN" = "--dry-run" ]; then
  echo
  echo "=== DRY RUN — tag $TAG would carry these notes ==="
  echo
  echo "$NOTES"
  echo
  echo "=== nothing was tagged, pushed or created ==="
  exit 0
fi

echo "==> Tagging $TAG at $SHORT"
git tag -a "$TAG" -m "${TAG}: ${DESCRIPTION}"
git push origin "$TAG"

echo "==> Creating GitHub Release"
gh release create "$TAG" \
  --title "$TAG — $DESCRIPTION" \
  --notes "$NOTES"

echo
echo "=== $TAG released ($DATE) ==="
echo "https://github.com/morosev/mec-cast/releases/tag/${TAG}"

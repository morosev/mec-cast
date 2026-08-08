#!/bin/bash
# Idempotent developer environment setup for mec-cast.
# Safe to re-run; each step is skipped when already satisfied.
#
#   bash scripts/bootstrap-dev.sh
#
# Does NOT install ROS2 (containers only) or build libwebrtc (opt-in, hours).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

# --- Rust -----------------------------------------------------------------
say "Rust toolchain"
if have cargo; then
  echo "cargo present: $(cargo --version)"
else
  echo "Installing rustup..."
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
  # shellcheck disable=SC1091
  source "$HOME/.cargo/env"
fi

# --- Docker ---------------------------------------------------------------
say "Docker"
if have docker; then
  echo "docker present: $(docker --version)"
  if ! docker info >/dev/null 2>&1; then
    echo "WARNING: docker is installed but not usable by $(whoami)."
    echo "  Either add yourself to the docker group and re-login:"
    echo "    sudo usermod -aG docker \$USER"
    echo "  or run docker-dependent make targets with sudo."
  fi
else
  echo "Docker not found. Install it with:"
  echo "  curl -fsSL https://get.docker.com | sh"
  echo "  sudo usermod -aG docker \$USER   # then re-login"
fi

# --- Python venv + maturin ------------------------------------------------
say "Python venv (telemetry/python/.venv)"
VENV="$ROOT_DIR/telemetry/python/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet maturin pytest
echo "venv ready: $("$VENV/bin/python" --version)"

# --- Submodules -----------------------------------------------------------
say "Submodules"
# Small ones first; webrtc is ~20 GB and stays opt-in.
git submodule update --init services/logging third_party/str0m 2>/dev/null || \
  echo "NOTE: could not initialise services/logging or third_party/str0m."

if [ -d third_party/webrtc/src/.git ] || [ -f third_party/webrtc/src/DEPS ]; then
  echo "third_party/webrtc/src present."
else
  echo "third_party/webrtc/src not initialised (~20 GB). Only needed to"
  echo "  rebuild the legacy client — see docs/guides/building-libwebrtc.md."
fi

if [ -f services/logging/pyproject.toml ]; then
  echo "services/logging populated."
elif [ -f ../mec-cast-logging-service/pyproject.toml ]; then
  echo "services/logging empty; a sibling working tree was found and will be"
  echo "used automatically by the e2e suite as a fallback."
else
  echo "WARNING: no logging service source found; e2e tests will fail."
fi

# --- PyO3 wheel into the venv --------------------------------------------
say "Building telemetry wheel into the venv"
if have cargo; then
  (cd telemetry/python && "$VENV/bin/maturin" develop --release)
else
  echo "Skipped (cargo unavailable in this shell; re-run after sourcing ~/.cargo/env)."
fi

say "Done"
echo "Next: make test        # fast tests"
echo "      make test-all    # includes containers"

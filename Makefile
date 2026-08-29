# mec-cast umbrella task runner.
#
# This Makefile only DELEGATES to each component's native build tool
# (cargo, maturin, colcon, npm, gn/ninja). Build logic belongs in the
# component, never here.
#
# SCOPE: local development. CI does NOT call these targets — it repeats the
# underlying commands inline (.github/workflows/platform.yml), and the lab
# deploy path (deploy/lab/deploy.sh) drives compose over ssh directly. The
# one target meant for a lab host is `make version`.
#
# `lint` and `test` mirror CI's Rust and Python checks exactly, including the
# feature combinations — a gap there already cost one red build, and a check
# that lives anywhere other than the command people actually run is a note,
# not a gate. What is NOT mirrored is the docker tier (ROS image, e2e, RAN
# image); that is `test-all`.
#
#   make help          list targets
#   make bootstrap     one-time dev environment setup
#   make test          everything that runs without docker
#   make test-all      everything, including containers

SHELL := /bin/bash
.DEFAULT_GOAL := help

VENV    := telemetry/python/.venv
PYTHON  := $(VENV)/bin/python
PYTEST  := $(VENV)/bin/pytest

# The admin control plane ships its own package and venv; its tests do not run
# under the telemetry venv.
ADMIN_VENV   := services/admin/.venv
ADMIN_PYTEST := $(ADMIN_VENV)/bin/pytest

# Provenance stamps, exported so `docker compose --build` interpolates them
# into the build args too. Without the export, a compose build produces an
# image labelled `unknown`, which silently defeats `make version`'s
# mismatch detection and the admin's version-skew check.
VCS_REF := $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
VERSION := $(shell git describe --tags --match 'platform-v*' --always --dirty 2>/dev/null || echo unknown)
export VCS_REF
export VERSION

COMPOSE := docker compose -f deploy/compose/logging.yml -f deploy/compose/local.yml
# Control plane on top of the data plane. Separate so `up-local` keeps
# exercising the standalone env-RUN_ID path.
COMPOSE_ADMIN := $(COMPOSE) -f deploy/compose/admin.yml
# The return path: the edge sends its processed cloud back and a renderer at
# the UE draws it. Another overlay, for the same reason admin.yml is one —
# local.yml alone must keep producing comparable measurements.
COMPOSE_RENDER := $(COMPOSE) -f deploy/compose/render.yml
COMPOSE_RENDER_ADMIN := $(COMPOSE_ADMIN) -f deploy/compose/render.yml

# On macOS, ring's C objects are compiled against the SDK's deployment target
# while a bare `cc` link defaults to the host's — so ld warns once per object.
# Pin the link to the SDK to match. Expands to nothing on other platforms.
CC_VERSION_MIN := $(shell test "$$(uname -s)" = Darwin && \
                    printf -- '-mmacosx-version-min=%s' "$$(xcrun --show-sdk-version)")

# ─── setup ────────────────────────────────────────────────────────────────
.PHONY: bootstrap
bootstrap: ## One-time dev setup (rust, docker, venv, submodules)
	bash scripts/bootstrap-dev.sh

# ─── build ────────────────────────────────────────────────────────────────
.PHONY: build build-telemetry build-ran build-python build-ros2 build-ran-image build-client build-libwebrtc

# Stamp images with the commit they came from, so a container found running on
# a lab host is traceable without trusting whatever the checkout beside it says.
BUILD_LABELS = --build-arg VCS_REF=$(VCS_REF) --build-arg VERSION=$(VERSION)

build: build-telemetry build-ran build-python ## Build everything cheap (no docker, no libwebrtc)

build-telemetry: ## Rust telemetry crate
	cargo build --release -p mec-cast-telemetry

build-ran: ## srsRAN metrics collector
	cargo build --release -p ran-collector

build-python: ## PyO3 wheel into the dev venv
	cd telemetry/python && ../../$(VENV)/bin/maturin develop --release

build-ros2: ## ROS2 image (telemetry wheel + colcon workspace)
	docker build -f deploy/docker/ros.Dockerfile $(BUILD_LABELS) -t mec-cast-ros .

build-ran-image: ## srsRAN collector image
	docker build -f deploy/docker/ran.Dockerfile $(BUILD_LABELS) -t mec-cast-ran .

build-client: ## Legacy WebRTC native addon (needs libwebrtc.a)
	cd clients/webrtc_native && ./build.sh

build-libwebrtc: ## Forked libwebrtc — 20 GB, hours. Opt-in, never in CI.
	@echo "This builds the patched WebRTC fork. Expect hours and ~20 GB."
	@echo "See docs/guides/building-libwebrtc.md before running."
	cd third_party/webrtc/src && \
	  ninja -C out/release_x64 webrtc

# ─── test ─────────────────────────────────────────────────────────────────
.PHONY: test test-all test-rust test-python test-admin test-ros2 test-e2e test-legacy test-ffi

test: test-rust test-ffi test-python test-admin ## Fast tests (no docker)

# test-legacy is deliberately NOT here: it needs the libwebrtc addon, which
# is a ~20 GB opt-in build. Run it explicitly when working on Profile B.
test-all: test-rust test-ffi test-python test-admin test-ros2 test-e2e ## Everything, containers included

test-rust: ## Unit + property + integration tests across the workspace
	cargo test --workspace
# Again without default features. This is not extra coverage of the same code
# — it guards a stated architectural property: ran-collector must build
# without tungstenite and telemetry without ureq (see the comment at
# ran/collector/Cargo.toml). Cargo keeps a separate artifact directory per
# feature set, so alternating costs nothing after the first build of each.
	cargo test --workspace --no-default-features

test-python: ## PyO3 binding smoke tests
	$(PYTEST) telemetry/python/tests -v

# These run in CI's `admin` job. Mirrored here so a green local `make test`
# means the same thing as a green pipeline — otherwise the first sign of a
# break is a failed push.
test-admin: ## Admin control-plane tests + the shared ROS2 client
	@test -x $(ADMIN_PYTEST) || { \
	  echo "ERROR: $(ADMIN_VENV) missing. Run: make bootstrap"; exit 1; }
	$(ADMIN_PYTEST) services/admin/tests -q
	$(ADMIN_PYTEST) ros2/src/mec_cast_admin_client/test -q

# The legacy addon's video path needs a camera, so it cannot be exercised in
# CI or WSL. This covers the C boundary it depends on, from a C compiler.
test-ffi: ## C ABI smoke test against the telemetry staticlib
	cargo build --release -p mec-cast-telemetry
	cc -Wall -Wextra -Werror $(CC_VERSION_MIN) -o target/c_abi_smoke \
	  telemetry/tests/c_abi_smoke.c \
	  -Itelemetry/include \
	  target/release/libmec_cast_telemetry.a \
	  -lpthread -ldl -lm
	./target/c_abi_smoke

test-ros2: build-ros2 ## In-container colcon/launch_testing tier
	bash deploy/docker/run-ros-tests.sh

test-e2e: build-ros2 ## Full compose topology with netem impairment
	$(PYTEST) tests/e2e -v

# Profile B. Needs the native addon, so it is opt-in rather than part of
# test-all. DURATION sets how long the call streams (default 10s).
DURATION ?= 10
test-legacy: ## Legacy WebRTC e2e — needs the libwebrtc addon (opt-in)
	@test -f clients/webrtc_native/build/Release/webrtc_addon.node || { \
	  echo "ERROR: native addon not built."; \
	  echo "  make build-client   (requires third_party/webrtc/src —"; \
	  echo "                       see docs/guides/building-libwebrtc.md)"; \
	  exit 1; }
	@test -d edge/signaling/node_modules || { \
	  echo "ERROR: signaling server dependencies missing."; \
	  echo "  cd edge/signaling && npm install"; \
	  exit 1; }
	@# node comes from nvm, which only loads in interactive shells.
	@[ -s "$$HOME/.nvm/nvm.sh" ] && . "$$HOME/.nvm/nvm.sh"; \
	  command -v node >/dev/null || { \
	    echo "ERROR: node not on PATH (nvm is interactive-only in this shell)."; \
	    exit 1; }; \
	  bash tests/legacy/e2e_local.sh $(DURATION)

# ─── lint ─────────────────────────────────────────────────────────────────
.PHONY: lint fmt
lint: ## Rust (clippy + rustfmt) and Python (ruff) checks, as CI runs them
	cargo fmt --all --check
	cargo clippy --workspace --all-targets -- -D warnings
# The other two combinations CI runs. The first keeps the optional-dependency
# boundary honest; the second checks that the opt-in features still compile,
# which nothing else exercises because neither is on by default.
	cargo clippy --workspace --all-targets --no-default-features -- -D warnings
	cargo clippy -p mec-cast-telemetry --all-targets --features linux-ptp,pyo3 -- -D warnings
# Ruff too, because CI runs it and this target is what everyone checks
# before pushing. It used to cover only Rust, so a Python-only change could
# pass `make lint` and fail CI on formatting alone — which is exactly what
# happened. Skipped with a warning rather than failing when ruff is absent:
# the Rust half is still worth running on a machine without the admin venv.
	@if [ -x services/admin/.venv/bin/ruff ]; then \
		services/admin/.venv/bin/ruff check services/admin && \
		services/admin/.venv/bin/ruff format --check services/admin; \
	elif command -v ruff >/dev/null 2>&1; then \
		ruff check services/admin && ruff format --check services/admin; \
	else \
		echo "WARNING: ruff not found; skipped the Python checks CI will run."; \
		echo "  pip install -e 'services/admin[dev]'"; \
	fi

fmt: ## Apply rustfmt, and ruff's formatting where available
	cargo fmt --all
	@if [ -x services/admin/.venv/bin/ruff ]; then \
		services/admin/.venv/bin/ruff check services/admin --fix; \
		services/admin/.venv/bin/ruff format services/admin; \
	fi

# ─── run ──────────────────────────────────────────────────────────────────
.PHONY: up-local up-admin up-render up-render-admin up-logging down down-hard logs view

up-local: build-ros2 ## Bring up the full local topology
	RUN_ID=$${RUN_ID:-$$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid)} \
	  $(COMPOSE) up -d --build

up-admin: build-ros2 ## Local topology + the admin control plane (no RUN_ID needed)
	$(COMPOSE_ADMIN) up -d --build
	@echo "admin page: http://localhost:8099/admin"

# Printed by BOTH render targets. Two things it has to get right, because
# getting either wrong looks like a broken renderer rather than a default:
#
#   * Report the sink actually in force. `null` is the default at every layer
#     (here, render.yml, and the node itself), and under it the node measures
#     the round trip and draws nothing — healthy, but no viewer is served, so
#     the published ports accept nothing and the page never loads.
#   * Never print a bare `http://localhost:9876`. The node builds the real URL
#     from viewer_host and BOTH ports, and the page needs the `?url=` stream
#     parameter to have any data source at all. Point at the node's own log
#     instead of printing something that looks right and is not.
#
# A third trap, specific to the admin: the sink is built in start_run(), so
# under the control plane the node is IDLE until a run starts and no viewer
# exists yet — the URL is logged at run start, not at container start.
#
# $(1) is the compose invocation, $(2) the target name to suggest re-running,
# $(3) non-empty when the control plane owns the run lifecycle.
define render_hint
sink=$${RENDER_SINK:-null}; \
echo "render sink=$$sink"; \
if [ "$$sink" = rerun ]; then \
  if [ -n "$(3)" ]; then \
    echo "  the renderer is IDLE until a run starts — start one on the admin page."; \
    echo "  the viewer URL is logged at that point, not now:"; \
    echo "    $(1) logs render | grep -o 'http://[^ ]*proxy'"; \
  else \
    printf "  waiting for the viewer URL"; \
    url=""; \
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do \
      url=$$($(1) logs render 2>/dev/null | grep -o 'http://[^ ]*proxy' | tail -1); \
      [ -n "$$url" ] && break; \
      printf "."; sleep 1; \
    done; \
    echo; \
    if [ -n "$$url" ]; then \
      echo "  viewer: $$url"; \
    else \
      echo "  not logged yet — the node may still be starting. Check with:"; \
      echo "    $(1) logs render | grep -o 'http://[^ ]*proxy'"; \
    fi; \
  fi; \
else \
  echo "  measuring the round trip, drawing nothing."; \
  echo "  for a viewer:  RENDER_SINK=rerun make $(2)"; \
fi
endef

up-render: build-ros2 ## Local topology + the return path and a renderer at the UE
	$(COMPOSE_RENDER) up -d --build
	@$(call render_hint,$(COMPOSE_RENDER),up-render)

up-render-admin: build-ros2 ## Return path + renderer, driven by the control plane
	ADMIN_URL=ws://admin:8099/ws/node $(COMPOSE_RENDER_ADMIN) up -d --build
	@echo "admin page: http://localhost:8099/admin"
	@$(call render_hint,$(COMPOSE_RENDER_ADMIN),up-render-admin,admin)

up-logging: ## Logging service + postgres only
	docker compose -f deploy/compose/logging.yml up -d --build

# `down` removes containers and keeps the data. It used to pass -v, which
# also deleted the `pgdata` volume — every aggregated snapshot of every run
# the logging service had ever stored. The per-frame CSVs live in a bind
# mount and survived, so the loss was silent and only half the picture went
# missing, which is the worst shape for it. Accumulation is harmless by
# design: every query is scoped by trace_id = run_id.
#
# --remove-orphans means this tears down the admin and renderer too, even
# though their overlay files are not named here — so one `down` is enough
# whichever `up-*` you ran. There is deliberately no down-admin/down-render.
down: ## Stop the local topology, keeping the database
	$(COMPOSE) down --remove-orphans

down-hard: ## Stop it AND delete the database volume (destroys run history)
	@echo "This deletes the pgdata volume: every logged snapshot, every run."
	@echo "Per-frame CSVs under runs/ are untouched."
	$(COMPOSE) down -v --remove-orphans

# The full overlay, not $(COMPOSE): with the base files this showed six of
# the eight running services and silently omitted admin and render — the two
# you are most likely to be reading logs for. Compose is happy to be given
# files whose services are not running.
logs: ## Follow container logs (including admin and renderer when running)
	$(COMPOSE_RENDER_ADMIN) logs -f

# The viewer is a host application, not a container: it needs a display, and
# the ROS image has no X libraries. It lives on the UE — locally, this machine
# — and nowhere else, because nothing measured depends on it.
RRVIEWER ?= $(HOME)/.rrviewer/bin/rerun
RENDER_GRPC_PORT ?= 9877

view: ## Watch the live point cloud in the native rerun viewer (UE only)
	@test -x "$(RRVIEWER)" || { \
	  echo "ERROR: the rerun viewer is not installed at $(RRVIEWER)."; \
	  echo "  python3 -m venv ~/.rrviewer && ~/.rrviewer/bin/pip install 'rerun-sdk==0.36.3'"; \
	  echo "  Match the SDK pinned in deploy/docker/ros.Dockerfile (>=0.36,<0.37)."; \
	  exit 1; }
	@$(COMPOSE_RENDER) ps --services --filter status=running 2>/dev/null | grep -qx render || { \
	  echo "ERROR: no renderer is running, so there is no stream to attach to."; \
	  echo "  RUN_ID=\$$(uuidgen) RENDER_SINK=rerun NETEM_LOSS=0% make up-render"; \
	  exit 1; }
	@echo "attaching to rerun+http://localhost:$(RENDER_GRPC_PORT)/proxy"
	@echo "  (a window opens via WSLg or your X server; Ctrl-C here closes it)"
# --port auto is load-bearing: without it the viewer defaults to 9876, finds
# the render node's own web server already there, decides another viewer is
# running, streams its data to that instead, and exits looking like it did
# nothing at all.
	@$(RRVIEWER) --port auto "rerun+http://localhost:$(RENDER_GRPC_PORT)/proxy"

# ─── misc ─────────────────────────────────────────────────────────────────
.PHONY: clean help version

version: ## What is actually deployed on this host (role, commit, images, PTP)
	@bash scripts/version.sh

clean: ## Remove build artifacts (keeps runs/ and third_party/)
	cargo clean
	rm -rf telemetry/python/build telemetry/python/*.egg-info
	find . -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

help: ## Show this help
	@# [0-9] matters: without it test-ros2, test-e2e and build-ros2 vanish.
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

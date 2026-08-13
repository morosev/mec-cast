# mec-cast umbrella task runner.
#
# This Makefile only DELEGATES to each component's native build tool
# (cargo, maturin, colcon, npm, gn/ninja). Build logic belongs in the
# component, never here — that keeps local and CI from drifting, since
# CI calls these same targets.
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
COMPOSE := docker compose -f deploy/compose/logging.yml -f deploy/compose/local.yml

# ─── setup ────────────────────────────────────────────────────────────────
.PHONY: bootstrap
bootstrap: ## One-time dev setup (rust, docker, venv, submodules)
	bash scripts/bootstrap-dev.sh

# ─── build ────────────────────────────────────────────────────────────────
.PHONY: build build-telemetry build-ran build-python build-ros2 build-client build-libwebrtc

build: build-telemetry build-ran build-python ## Build everything cheap (no docker, no libwebrtc)

build-telemetry: ## Rust telemetry crate
	cargo build --release -p mec-cast-telemetry

build-ran: ## srsRAN metrics collector
	cargo build --release -p ran-collector

build-python: ## PyO3 wheel into the dev venv
	cd telemetry/python && ../../$(VENV)/bin/maturin develop --release

build-ros2: ## ROS2 image (telemetry wheel + colcon workspace)
	docker build -f deploy/docker/ros.Dockerfile -t mec-cast-ros .

build-client: ## Legacy WebRTC native addon (needs libwebrtc.a)
	cd clients/webrtc_native && ./build.sh

build-libwebrtc: ## Forked libwebrtc — 20 GB, hours. Opt-in, never in CI.
	@echo "This builds the patched WebRTC fork. Expect hours and ~20 GB."
	@echo "See docs/guides/building-libwebrtc.md before running."
	cd third_party/webrtc/src && \
	  ninja -C out/release_x64 webrtc

# ─── test ─────────────────────────────────────────────────────────────────
.PHONY: test test-all test-rust test-python test-ros2 test-e2e test-ffi

test: test-rust test-ffi test-python ## Fast tests (no docker)

test-all: test-rust test-ffi test-python test-ros2 test-e2e ## Everything, containers included

test-rust: ## Unit + property + integration tests across the workspace
	cargo test --workspace

test-python: ## PyO3 binding smoke tests
	$(PYTEST) telemetry/python/tests -v

# The legacy addon's video path needs a camera, so it cannot be exercised in
# CI or WSL. This covers the C boundary it depends on, from a C compiler.
test-ffi: ## C ABI smoke test against the telemetry staticlib
	cargo build --release -p mec-cast-telemetry
	cc -Wall -Wextra -Werror -o target/c_abi_smoke \
	  telemetry/tests/c_abi_smoke.c \
	  -Itelemetry/include \
	  target/release/libmec_cast_telemetry.a \
	  -lpthread -ldl -lm
	./target/c_abi_smoke

test-ros2: build-ros2 ## In-container colcon/launch_testing tier
	bash deploy/docker/run-ros-tests.sh

test-e2e: build-ros2 ## Full compose topology with netem impairment
	$(PYTEST) tests/e2e -v

# ─── lint ─────────────────────────────────────────────────────────────────
.PHONY: lint fmt
lint: ## Clippy + rustfmt check
	cargo fmt --all --check
	cargo clippy --workspace --all-targets -- -D warnings

fmt: ## Apply rustfmt
	cargo fmt --all

# ─── run ──────────────────────────────────────────────────────────────────
.PHONY: up-local up-logging down logs experiment

up-local: build-ros2 ## Bring up the full local topology
	RUN_ID=$${RUN_ID:-$$(uuidgen)} $(COMPOSE) up -d --build

up-logging: ## Logging service + postgres only
	docker compose -f deploy/compose/logging.yml up -d --build

down: ## Tear down local topology and volumes
	$(COMPOSE) down -v --remove-orphans

logs: ## Follow container logs
	$(COMPOSE) logs -f

experiment: ## Run one measured experiment (see scripts/run-experiment.sh)
	bash scripts/run-experiment.sh

# ─── misc ─────────────────────────────────────────────────────────────────
.PHONY: clean help

clean: ## Remove build artifacts (keeps runs/ and third_party/)
	cargo clean
	rm -rf telemetry/python/build telemetry/python/*.egg-info
	find . -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

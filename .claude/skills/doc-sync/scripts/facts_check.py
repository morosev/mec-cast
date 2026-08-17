#!/usr/bin/env python3
"""Check docs and code against docs/_facts.yml.

Two directions:

1. Does the CODE still match the facts file? (ports, env vars, service names)
2. Do the DOCS contradict it? (a doc naming a different port for a service)

Direction 1 matters most — the facts file is only useful while it is true.

    python .claude/skills/doc-sync/scripts/facts_check.py

Exit 0 clean, 1 on contradictions. Needs PyYAML.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML not installed. Install it or skip this gate:")
    print("  telemetry/python/.venv/bin/pip install pyyaml")
    sys.exit(2)

REPO = Path(__file__).resolve().parents[4]
FACTS = REPO / "docs" / "_facts.yml"
problems: list[str] = []
notes: list[str] = []


def tracked(*globs: str) -> list[Path]:
    args = ["git", "ls-files", *globs]
    out = subprocess.run(args, cwd=REPO, capture_output=True, text=True).stdout.split()
    return [
        REPO / p for p in out
        if not p.startswith(("third_party/", "services/logging/"))
    ]


def check_sha_reachable(facts: dict) -> None:
    sha = facts.get("meta", {}).get("last_synced_sha")
    if not sha:
        problems.append("meta.last_synced_sha is missing — /doc-sync cannot find a baseline")
        return
    r = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=REPO, capture_output=True,
    )
    if r.returncode != 0:
        problems.append(
            f"meta.last_synced_sha {sha[:12]} is unreachable "
            "(rebased or squashed?) — fall back to the last docs/ commit"
        )
        return
    behind = subprocess.run(
        ["git", "rev-list", "--count", f"{sha}..HEAD"],
        cwd=REPO, capture_output=True, text=True,
    ).stdout.strip()
    if behind and behind != "0":
        notes.append(f"{behind} commit(s) since last sync — Stage 1 has work to do")


def check_ports_in_code(facts: dict) -> None:
    """Each declared port should still appear somewhere in config."""
    config = tracked("deploy/**/*.yml", "deploy/**/*.yaml", "deploy/**/*.json5",
                     "Makefile", "*.toml")
    blob = "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in config if p.is_file()
    )
    for name, spec in (facts.get("ports") or {}).items():
        port = str(spec.get("port"))
        if port not in blob:
            notes.append(
                f"port {name}={port} declared in _facts.yml but not found in "
                "deploy config — verify it is still real"
            )


def check_env_in_code(facts: dict) -> None:
    """Each declared env var should still be read somewhere in source."""
    src = tracked("*.py", "*.rs", "*.js", "*.sh", "*.yml", "*.cc", "*.h", "Makefile")
    blob = "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in src if p.is_file()
    )
    for var in (facts.get("env") or {}):
        if var not in blob:
            problems.append(
                f"env var {var} is in _facts.yml but no source file references it"
            )


def check_services_in_code(facts: dict) -> None:
    src = tracked("*.py", "*.rs", "*.js", "*.cc")
    blob = "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in src if p.is_file()
    )
    for role, name in (facts.get("services") or {}).items():
        if name not in blob:
            notes.append(
                f"service identifier '{name}' ({role}) not found in source — "
                "unimplemented, or renamed?"
            )


def check_envelope(facts: dict) -> None:
    """The wire size is asserted in several places; they must agree."""
    want = str((facts.get("envelope") or {}).get("wire_bytes"))
    rs = REPO / "telemetry" / "src" / "envelope.rs"
    if rs.exists():
        m = re.search(r"ENVELOPE_WIRE_LEN\s*:\s*usize\s*=\s*(\d+)", rs.read_text(encoding="utf-8"))
        if m and m.group(1) != want:
            problems.append(
                f"envelope.wire_bytes={want} but ENVELOPE_WIRE_LEN={m.group(1)} in envelope.rs"
            )


def check_docs_contradictions(facts: dict) -> None:
    """A doc naming a different port for a known service is a contradiction."""
    docs = tracked("*.md")
    for name, spec in (facts.get("ports") or {}).items():
        port = str(spec.get("port"))
        label = name.replace("_", "[ -]?")
        for doc in docs:
            text = doc.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(rf"{label}\D{{0,40}}?:(\d{{4,5}})", text, re.I):
                found = m.group(1)
                if found != port and found not in {
                    str(s.get("port")) for s in (facts.get("ports") or {}).values()
                }:
                    problems.append(
                        f"{doc.relative_to(REPO)}: {name} shown as :{found}, "
                        f"_facts.yml says :{port}"
                    )


def main() -> int:
    if not FACTS.exists():
        print(f"missing {FACTS.relative_to(REPO)}")
        return 1
    facts = yaml.safe_load(FACTS.read_text(encoding="utf-8"))

    check_sha_reachable(facts)
    check_envelope(facts)
    check_env_in_code(facts)
    check_ports_in_code(facts)
    check_services_in_code(facts)
    check_docs_contradictions(facts)

    for n in notes:
        print(f"  note: {n}")
    for p in problems:
        print(f"  PROBLEM: {p}")

    if problems:
        print(f"\n{len(problems)} contradiction(s)")
        return 1
    print(f"\nPASS — _facts.yml agrees with the code ({len(notes)} note(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())

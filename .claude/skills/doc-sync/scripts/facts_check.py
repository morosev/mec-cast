#!/usr/bin/env python3
"""Check docs and code against docs/_facts.yml.

Two directions:

1. Does the CODE still match the facts file? (ports, env vars, service
   names, the clock model)
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
    """Each declared port should still appear somewhere in config.

    Not only under deploy/. The legacy WebRTC signalling server binds 8080 and
    has no compose file at all -- it is an opt-in local target, so searching
    only the deploy tree reported a real port as missing on every run. A gate
    finding that everyone has learned to ignore is worth less than no finding,
    so the search covers the component configs too.
    """
    config = tracked("deploy/**/*.yml", "deploy/**/*.yaml", "deploy/**/*.json5",
                     "Makefile", "*.toml", "clients/**/*.json", "*/*/client-config.json")
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


def check_clock_in_code(facts: dict) -> None:
    """The clock block, which was unchecked while every claim in it went stale.

    It is the block that decides whether a cross-host number means anything,
    and nothing validated it. Seven documents drifted to naming `phc2sys` as
    the mechanism -- with every gate green -- while the lab ran two hosts on
    chrony and a third on ptp_kvm, and `ptp.reliable` was hardcoded off in
    both bindings. A fact nobody checks is a comment.

    Only mechanically verifiable claims are asserted here. The prose fields
    (`same_root_required`, `alternatives`) carry the reasoning and cannot be
    checked; what CAN be checked is that the device default, the diagnostic
    command and the snapshot field still exist as described.
    """
    clock = facts.get("clock") or {}
    if not clock:
        problems.append("_facts.yml has no clock block")
        return

    # 1. The documented default must be the default the deployment actually
    #    uses. A compose file changed without the fact following it is exactly
    #    how /dev/ptp0 came to be stated as universal truth.
    default = str(clock.get("device_default", ""))
    if default:
        composes = tracked("deploy/**/compose*.yml")
        mapping = [
            f for f in composes
            if f.is_file() and "PTP_DEVICE" in f.read_text(encoding="utf-8", errors="replace")
        ]
        if not mapping:
            problems.append(
                "clock.device_default is declared but no compose file honours "
                "PTP_DEVICE — the device is hardcoded again"
            )
        for f in mapping:
            text = f.read_text(encoding="utf-8", errors="replace")
            if f"${{PTP_DEVICE:-{default}}}" not in text:
                problems.append(
                    f"{f.relative_to(REPO)}: PTP_DEVICE default does not match "
                    f"clock.device_default={default}"
                )

    # 2. The cross-host check is the only thing that catches two-roots skew.
    #    If the script or its flag goes away, the fact is a dead pointer to
    #    the one diagnostic that matters.
    cmd = str(clock.get("cross_host_check", ""))
    if cmd:
        script = REPO / cmd.split()[0]
        if not script.exists():
            problems.append(
                f"clock.cross_host_check names {cmd.split()[0]}, which does not exist"
            )
        else:
            body = script.read_text(encoding="utf-8", errors="replace")
            for flag in (w for w in cmd.split() if w.startswith("--")):
                if flag not in body:
                    problems.append(
                        f"clock.cross_host_check uses {flag} but "
                        f"{script.relative_to(REPO)} does not support it"
                    )

    # 3. The per-snapshot field must still be emitted under that name.
    #    `context.ptp.reliable` -> the recorder writes "reliable" inside a
    #    "ptp" object; renaming either half silently invalidates every doc
    #    that tells an operator to look for it.
    field = str(clock.get("per_snapshot_field", ""))
    if field:
        leaf = field.split(".")[-1]
        rec = REPO / "telemetry" / "src" / "recorder.rs"
        if rec.exists() and f'"{leaf}"' not in rec.read_text(encoding="utf-8"):
            problems.append(
                f"clock.per_snapshot_field={field} but recorder.rs emits no "
                f'"{leaf}" key'
            )


def check_docs_contradictions(facts: dict) -> None:
    """A doc naming a different port for a known service is a contradiction.

    `.mmd` counts as a doc. Diagram sources state ports, paths and service
    names exactly as prose does, and they were invisible to this check until
    three of them spent a milestone describing a layout the code had left
    behind — with every gate green, because everything scanned only `*.md`.
    """
    docs = tracked("*.md", "*.mmd")
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


def check_output_paths(facts: dict) -> None:
    """The per-frame CSV layout, wherever it is written out.

    `outputs.per_frame_csv` is the pinned shape. Diagrams and docs spell it
    out literally, so an instance suffix arriving in the code silently leaves
    them describing directories that no longer exist. Mermaid escapes the
    angle brackets, hence the two spellings.
    """
    pinned = str((facts.get("outputs") or {}).get("per_frame_csv") or "")
    if not pinned:
        return
    suffixed = "-<instance>" in pinned or "-&lt;instance&gt;" in pinned
    if not suffixed:
        return
    # Any runs/<RUN_ID>/<leaf>/samples.csv whose leaf carries no instance
    # suffix contradicts the pinned shape.
    bare = re.compile(
        r"runs/(?:<RUN_ID>|&lt;RUN_ID&gt;|\$RUN_ID|<run_id>)/"
        r"(pub|edge|render)/samples\.csv"
    )
    for doc in tracked("*.md", "*.mmd"):
        text = doc.read_text(encoding="utf-8", errors="replace")
        for m in bare.finditer(text):
            problems.append(
                f"{doc.relative_to(REPO)}: writes {m.group(0)}, but "
                f"outputs.per_frame_csv is {pinned} — the leaf needs its "
                "instance suffix (pub-0, edge-0, render-0)"
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
    check_clock_in_code(facts)
    check_docs_contradictions(facts)
    check_output_paths(facts)

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

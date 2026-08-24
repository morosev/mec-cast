#!/usr/bin/env python3
"""Validate docs against the repo: links, paths, make targets, orphans.

Catches the drift class that survives a restructure — a link or command that
silently stopped resolving. Run from the repo root.

    python .claude/skills/doc-sync/scripts/qa_docs.py [--quiet]

Exit 0 when clean, 1 when problems were found.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
# Submodules and vendored trees: we describe them, we do not own them.
EXCLUDE_DIRS = {"third_party", "node_modules", ".git", "target", "runs", ".venv"}
EXCLUDE_DOCS = {"services/logging", "third_party"}

# Paths that are correct but relative to a context this script cannot see.
# Keep this list SHORT and always with a reason — it is a suppression, and a
# suppression nobody can justify is how a gate rots. (doc, path) pairs.
KNOWN_CONTEXT_PATHS = {
    ("RELEASING.md", "tests/e2e_local.sh"):
        "path inside the release zip, whose layout differs from the repo",
    ("docs/guides/running-an-experiment.md", "edge/samples.csv"):
        "path inside runs/<RUN_ID>/",
    ("docs/guides/running-an-experiment.md", "ran/samples.csv"):
        "path inside runs/<RUN_ID>/",
}

problems: list[str] = []
quiet = "--quiet" in sys.argv


def owned_docs() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=REPO, capture_output=True, text=True
    ).stdout.split()
    return [
        REPO / p for p in out
        if not any(p.startswith(x) for x in EXCLUDE_DOCS)
    ]


def check_links(doc: Path) -> None:
    """Every relative markdown link must resolve."""
    text = doc.read_text(encoding="utf-8", errors="replace")
    for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", text):
        target = m.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path_part = target.split("#")[0]
        if not path_part:
            continue  # pure anchor
        resolved = (doc.parent / path_part).resolve()
        if not resolved.exists():
            problems.append(f"{doc.relative_to(REPO)}: broken link -> {target}")


def check_paths(doc: Path) -> None:
    """Backticked repo-ish paths should exist.

    Resolves against BOTH the repo root and the document's own directory —
    component READMEs legitimately write `tests/replay.rs` meaning a path
    relative to themselves. Checking only from the root reports those as
    missing, which is noise that trains you to ignore the gate.

    Deliberately conservative otherwise: only paths whose first segment is a
    real top-level directory are considered, so prose and URL fragments do not
    generate findings.
    """
    text = doc.read_text(encoding="utf-8", errors="replace")
    tops = {p.name for p in REPO.iterdir() if p.is_dir() and not p.name.startswith(".")}
    local_tops = {p.name for p in doc.parent.iterdir() if p.is_dir()} \
        if doc.parent.is_dir() else set()

    for m in re.finditer(r"`([A-Za-z0-9_./-]+/[A-Za-z0-9_./-]+)`", text):
        cand = m.group(1).rstrip("/")
        if any(ch in cand for ch in "<>*{}") or " " in cand:
            continue
        top = cand.split("/")[0]
        if top not in tops and top not in local_tops:
            continue
        if (REPO / cand).exists() or (doc.parent / cand).exists():
            continue
        rel = str(doc.relative_to(REPO)).replace("\\", "/")
        if (rel, cand) in KNOWN_CONTEXT_PATHS:
            continue
        problems.append(f"{doc.relative_to(REPO)}: path does not exist -> {cand}")


def check_make_targets(docs: list[Path]) -> None:
    """`make X` written *as a command* must be a real target.

    Only code counts — inline backticks and fenced blocks. In prose, "make"
    is an ordinary English verb, and matching it there produced a false
    positive on every run ("mixed traffic classes make head-of-line blocking
    worth addressing" in ADR-0006). The old defence was a stoplist of words
    that may follow "make", which cannot be completed: any noun in the
    language can. Scoping to code is the distinction that actually holds, and
    it is also where a reader would copy the command from.
    """
    makefile = REPO / "Makefile"
    if not makefile.exists():
        return
    targets = set(
        re.findall(r"^([a-zA-Z0-9_-]+):", makefile.read_text(encoding="utf-8"), re.M)
    )
    # Noun phrases that appear inside backticks without being invocations.
    prose = {"targets", "target", "commands"}
    for doc in docs:
        text = doc.read_text(encoding="utf-8", errors="replace")
        code = re.findall(r"```[a-z]*\n(.*?)```", text, re.S)
        code += re.findall(r"`([^`\n]+)`", text)
        for span in code:
            for m in re.finditer(r"\bmake ([a-z][a-z0-9_-]*)\b", span):
                t = m.group(1)
                if t in prose or t in targets:
                    continue
                problems.append(
                    f"{doc.relative_to(REPO)}: unknown make target -> make {t}"
                )


def check_orphans(docs: list[Path]) -> None:
    """A doc nothing links to is usually stale (RELEASING.md was, for months)."""
    linked: set[str] = set()
    for doc in docs:
        text = doc.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", text):
            t = m.group(1).split("#")[0].strip()
            if t and not t.startswith(("http", "mailto:")):
                r = (doc.parent / t).resolve()
                if r.exists():
                    linked.add(str(r))
    for doc in docs:
        if doc.name == "README.md":
            continue  # READMEs are entry points, reached by directory
        if str(doc.resolve()) not in linked:
            problems.append(f"{doc.relative_to(REPO)}: orphan — no doc links to it")


def main() -> int:
    docs = owned_docs()
    if not quiet:
        print(f"checking {len(docs)} owned documents\n")
    for doc in docs:
        check_links(doc)
        check_paths(doc)
    check_make_targets(docs)
    check_orphans(docs)

    if problems:
        for p in problems:
            print(f"  {p}")
        print(f"\n{len(problems)} problem(s)")
        return 1
    print("PASS — links, paths, make targets and cross-references all resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())

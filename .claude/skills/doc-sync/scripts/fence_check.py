"""Check the embedded mermaid fences against their .mmd sources.

They are intentionally not byte-identical (the fences drop the %%{init}
theme block, since GitHub applies its own), so compare the graph instead:
node ids and edges must match, or the rendered page lies about the source.
"""
import pathlib
import re

# Derive the repo root the way qa_docs.py and facts_check.py do, so this
# runs on any checkout rather than only the one it was written on.
D = pathlib.Path(__file__).resolve().parents[4] / "docs" / "diagrams"
readme = (D / "README.md").read_text(encoding="utf-8")

fences = re.findall(r"```mermaid\n(.*?)```", readme, re.S)
print(f"fences found: {len(fences)}")

# Fence order in README.md matches this list.
sources = ["architecture-overview.mmd", "lab-deployment.mmd", "mec-cast-nodes.mmd"]


def graph(text: str):
    """Node ids and edge pairs, ignoring comments, theme and styling."""
    body = "\n".join(
        ln for ln in text.splitlines()
        if not ln.strip().startswith("%%")
        and not ln.strip().startswith("classDef")
        and not ln.strip().startswith("class ")
    )
    nodes = set(re.findall(r"\b([A-Z][A-Z0-9_]*)\s*[\[\(\{]", body))
    edges = set()
    # Arrow forms in use across the three diagrams. Order matters within the
    # alternation: `-\.->` before `-\.-`, or the dotted arrow's `>` is left
    # dangling and the edge is missed. `<?` covers the bidirectional forms
    # (`<-->`, `<==>`) that mec-cast-nodes.mmd uses — without it those edges
    # are invisible to this comparison and the check passes vacuously.
    arrow = r"(?:<?-\.->|-\.-|<?-->|---|<?==>)"
    for m in re.finditer(
        rf"\b([A-Z][A-Z0-9_]*)\s*{arrow}\s*(?:\|[^|]*\|\s*)?([A-Z][A-Z0-9_]*)", body
    ):
        edges.add((m.group(1), m.group(2)))
    return nodes, edges


def labels(text: str) -> dict[str, str]:
    """Node id -> its label text.

    The graph comparison below catches a node or edge going missing, but not
    a LABEL changing — and a label is where the ports, paths and service
    names live. A fence spent a milestone naming a service the source had
    already renamed, with this check green the whole time.
    """
    out: dict[str, str] = {}
    for m in re.finditer(r"\b([A-Z][A-Z0-9_]*)\s*\[\s*\"?(.*?)\"?\s*\]", text, re.S):
        out[m.group(1)] = " ".join(m.group(2).split())
    return out


#: Mermaid diagram declarations. A fence whose first meaningful line is not
#: one of these is not a diagram at all.
DECLARATIONS = (
    "flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram",
    "erDiagram", "journey", "gantt", "pie", "gitGraph", "mindmap", "timeline",
)


def declares_a_diagram(text: str) -> str | None:
    """The declaration line, or None if the fence never gets to one.

    Cheap, and it catches the failure that actually happened: the lab-
    deployment fence was published with the OPENING line of its %%{init}%%
    block stripped and four lines of the body left behind, so it began
    mid-object. GitHub rendered `UnknownDiagramError` in place of the
    diagram for a whole release, and the graph comparison below could not
    see it — that strips %% lines before comparing, so a fence that is not a
    diagram at all still "matches" its source.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("%%"):
            continue
        return stripped if stripped.startswith(DECLARATIONS) else None
    return None


ok = True
for fence, name in zip(fences, sources):
    if declares_a_diagram(fence) is None:
        ok = False
        first = next(
            (ln.strip() for ln in fence.splitlines()
             if ln.strip() and not ln.strip().startswith("%%")),
            "(empty)",
        )
        print(f"\n{name}: NOT A DIAGRAM — the fence begins with")
        print(f"  {first[:90]}")
        print("  Mermaid needs a declaration (flowchart, graph, …) first.")
        print("  A partially-stripped %%{init}%% block is the usual cause.")
for fence, name in zip(fences, sources):
    src = (D / name).read_text(encoding="utf-8")
    fn, fe = graph(fence)
    sn, se = graph(src)
    fl, sl = labels(fence), labels(src)
    drifted = sorted(
        k for k in set(fl) & set(sl) if fl[k] != sl[k]
    )
    if drifted:
        ok = False
        print(f"\n{name}: LABEL DRIFT")
        for k in drifted:
            print(f"  {k}\n    fence:  {fl[k][:90]}\n    source: {sl[k][:90]}")
    missing_n, extra_n = sn - fn, fn - sn
    missing_e, extra_e = se - fe, fe - se
    status = "MATCH" if not (missing_n or extra_n or missing_e or extra_e) else "DRIFT"
    if status == "DRIFT":
        ok = False
    print(f"\n{name}: {status}  ({len(sn)} nodes, {len(se)} edges in source)")
    if missing_n:
        print(f"   in .mmd but not in fence: {sorted(missing_n)}")
    if extra_n:
        print(f"   in fence but not in .mmd: {sorted(extra_n)}")
    if missing_e:
        print(f"   edges missing from fence: {sorted(missing_e)}")
    if extra_e:
        print(f"   edges only in fence:      {sorted(extra_e)}")

print("\n" + ("PASS — embedded fences match their sources" if ok else "FAIL — fences drifted"))

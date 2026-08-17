"""Check the embedded mermaid fences against their .mmd sources.

They are intentionally not byte-identical (the fences drop the %%{init}
theme block, since GitHub applies its own), so compare the graph instead:
node ids and edges must match, or the rendered page lies about the source.
"""
import pathlib
import re

D = pathlib.Path("/home/morosev/mec-cast/docs/diagrams")
readme = (D / "README.md").read_text(encoding="utf-8")

fences = re.findall(r"```mermaid\n(.*?)```", readme, re.S)
print(f"fences found: {len(fences)}")

# Fence order in README.md matches this list.
sources = ["architecture-overview.mmd", "lab-deployment.mmd"]


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
    for m in re.finditer(r"\b([A-Z][A-Z0-9_]*)\s*(?:-\.->|-->|---|==>)\s*(?:\|[^|]*\|\s*)?([A-Z][A-Z0-9_]*)", body):
        edges.add((m.group(1), m.group(2)))
    return nodes, edges


ok = True
for fence, name in zip(fences, sources):
    src = (D / name).read_text(encoding="utf-8")
    fn, fe = graph(fence)
    sn, se = graph(src)
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

---
name: doc-sync
description: Sync mec-cast documentation with the code, then optionally regenerate the slide deck, the Mermaid diagrams, and the one-picture system image. Use when asked to update/sync/refresh docs, after a batch of code changes, before a release or review, or when asked to update the PPT, diagrams or hero image. Runs code→docs by default; the visual outputs only when confirmed.
---

# doc-sync

Keeps `docs/`, the component READMEs, the 10-slide deck, the Mermaid diagrams
and the one-picture system image truthful about the code.

```
code → docs/_facts.yml → docs + READMEs → PPT → MMD → hero image
```

Each output derives from the one before it, never from the code directly. If a
slide or a diagram needs something the docs do not say, that is a Stage 1 gap —
fix it there first.

## Ask this first, every run

> **How far should this sync go?**
> 1. **Docs only** — code → facts → docs and READMEs
> 2. **Docs + slides and diagrams** — 1, then the PPT and the `.mmd` files
> 3. **Everything** — 1 and 2, plus the one-picture system image

Then run stages 1..N for the chosen tier. Two exceptions to asking:

- The user already named the scope ("just the diagrams", "docs only",
  "full sync") — take them at their word and say which tier you inferred.
- Stage 1 runs in every tier and is skipped only on explicit request, because
  regenerating a slide from stale docs launders the staleness into a nicer
  format.

"Docs" means `docs/**` **and** the component READMEs (`telemetry/`, `ros2/`,
`clients/`, `edge/`, `ran/collector/`, `deploy/`, `services/`,
`services/admin/`, `third_party/`, `tools/`, and the root `README.md`).

`services/` now holds two different kinds of thing, and the distinction
matters here: `services/logging/` and `third_party/str0m/` are **submodules** —
never edit their contents; describe them from the outside. `services/admin/` is
**in-repo and owned**, so its README is a normal Stage 1 target like any other
component's.

---

## Stage 1 — code → docs

### 1.1 Establish the diff

Read `meta.last_synced_sha` from `docs/_facts.yml`, diff to HEAD:

```bash
git diff --stat <last_synced_sha>..HEAD -- . ':!third_party' ':!services/logging'
```

```bash
git log --oneline <last_synced_sha>..HEAD
```

No marker, or the SHA is unreachable after a rebase: fall back to the last
commit touching `docs/`, and say which baseline you used.

Nothing changed → say so and stop. Do not manufacture work.

### 1.2 Build a candidate pool; do not fix everything

Turn the diff into **candidates** — (doc, reason, evidence) triples — then
filter. Never edit a doc that has no candidate.

[`references/code-to-doc-map.md`](references/code-to-doc-map.md) maps each
source path to the docs it implicates, lists the change shapes that usually
warrant an ADR, and collects the phrasings that rot silently ("not yet",
"currently only", pinned versions, counts). Read it when turning a diff into
candidates — it is a starting point, not a substitute for reading the diff.

Two signals:

- **Code churn.** A file changed repeatedly in the window is still in flux;
  chasing its prose descriptions is low value. Its *interface* — paths, flags,
  ports, names — is still worth fixing.
- **Doc altitude.** The higher the doc, the more conservative:

| Altitude | Docs | Policy |
|---|---|---|
| Low | Component READMEs, `guides/` | Fix within the tiers below |
| Mid | `architecture/overview.md`, `operations/` | Fix facts; propose prose |
| High | `architecture/adr/`, `timing-model.md` | **Propose only, never auto-edit** |

Deep prose review of a high-altitude doc happens only when the user asks for
that doc by name.

### 1.3 Tiered edit policy

**Tier 1 — auto-apply.** Mechanically verifiable against the repo, and wrong
in a way a reader hits immediately:

- paths, filenames, directories that moved or vanished
- `make` targets, script names, CLI flags, command invocations
- ports, env var names, service identifiers, image names
- anything contradicting `docs/_facts.yml`
- broken relative links

Verify each against the working tree before editing (`test -e`, `make -n`,
`grep`). Never repair a path by guessing where it went.

**Tier 2 — propose with a diff.** Judgement required:

- prose describing behaviour that changed
- claims about *state* — "not yet wired", "currently only X", "needs an
  upstream push". These rot silently and no linter sees them. Check every one
  in a touched doc.
- tables of components, metrics, responsibilities
- mid-altitude content that is not a Tier-1 fact

**Tier 3 — flag, never touch.** ADR content, rationale, design arguments, any
statement about *why*. Report with evidence; draft only if asked.

### 1.4 ADR pass

Both directions, every run:

1. **Are existing ADRs still true?** Each asserts something checkable —
   ADR-0004 says percentiles are exact, not estimated. Verify against code. A
   contradicted ADR is a serious Tier-3 flag: either the code drifted from a
   decision, or the decision changed with no record.
2. **Any undocumented decisions?** Scan the diff for architectural choices: a
   new dependency at a boundary, a changed wire format, a swapped transport or
   protocol, a new persistent store, a reversal of an earlier ADR. Report as
   "looks like an undocumented decision" with commit and code as evidence.
   **Draft the ADR only on request** — ADRs record human intent, and invented
   rationale is worse than an honest gap.

### 1.5 Update the facts file

If code changed anything in `docs/_facts.yml`, update it **first**, then
propagate outward. Add a key only if the value appears in 3+ places or being
wrong is silently harmful — this file is not a system model. On completion set
`meta.last_synced_sha` to the synced HEAD and `meta.last_synced_date`.

### 1.6 Report

Baseline used · candidates found · Tier-1 applied · Tier-2 proposed · Tier-3
flagged · what you deliberately left alone and why.

---

## Stage 2 — PPT (tier 2+)

`docs/slides/mec-cast-architecture.pptx`, generated by
`docs/slides/build-deck.js` (pptxgenjs). **Edit the generator, never the
`.pptx`** — hand edits are lost on the next build.

Content comes from the docs, not from code. If a slide needs something the
docs do not say, that is a Stage-1 gap — fix it there first.

### Structure

| # | Slide | Sourced from |
|---|---|---|
| 1 | Architecture overview | `architecture/overview.md` |
| 2 | Deployment — local | `deploy/compose/local.yml`, `deploy/README.md` |
| 3 | Deployment — lab | `deploy/lab/`, `operations/lab-topology.md` |
| 4 | Edge services — logging and admin | `operations/logging-submodule.md`, `operations/admin-service.md`, `services/README.md` |
| 5 | ROS2 on the UE — client and renderer | `ros2/README.md`, `adr/0009-render-return-path.md` |
| 6 | Edge | `ros2/README.md`, `telemetry/README.md` |
| 7 | Zenoh | `adr/0001-zenoh-over-dds.md` |
| 8 | Profile B — current | `clients/webrtc_native/README.md` |
| 9 | Profile B — planned str0m | `architecture/str0m-profile.md` |
| 10 | Applications and future work | `research/README.md`, ADR-0005 |

**Pair related components on one slide; do not add an eleventh.** This has now
happened twice — slide 4 absorbed the admin service beside logging, slide 5 the
render node beside the lidar client — and both times the pairing held because
the two things genuinely share a location and a role. `qa_pptx.py` pins
`EXPECTED_SLIDES = 10` and `PROFILE_B_FIRST_SLIDE = 8`; a new slide inserted
before 8 shifts Profile B and forces both constants, which is a content
contract, not a formatting detail. Watch the word counts QA prints — slides 4
(232), 10 (234), 6 (200) and 5 (193) are the dense ones — and revisit only if
one visibly bursts.

**Slides 1–7 must not mention WebRTC, str0m, SFU, libwebrtc, or "Profile B".**
Profile B is introduced on slide 8 and nowhere earlier. `qa_pptx.py` enforces
this; do not weaken the check.

### Growth is your responsibility

The 10 slides are stable, not frozen. When the system outgrows them, **notice
it and argue the case** — never silently cram content in, never silently add a
slide. Signals:

- a component exists that no slide owns (a compression stage, a RIC xApp)
- one slide's word count climbs well past its neighbours (QA prints these)
- a slide describes something now retired — Profile B, once str0m lands
- two unrelated ideas share a slide because there was nowhere else

Bring the user: what changed, which slide is strained, what you propose (add /
re-scope / retire), and what it costs. Then do what they decide.

### Build and verify

```bash
bash .claude/skills/doc-sync/scripts/build_deck.sh
```

```bash
python .claude/skills/doc-sync/scripts/qa_pptx.py docs/slides/mec-cast-architecture.pptx
```

Geometry QA caught 25 real defects on first build, one a box entirely off the
slide. A deck that looks right in the generator source frequently is not.

---

## Stage 3 — MMD (tier 2+)

Four diagrams in `docs/diagrams/`. **`.mmd` is the source of truth**; any
`.svg`/`.png` is a rendered artifact.

| File | Mirrors | Artifacts | Update when |
|---|---|---|---|
| `architecture-overview.mmd` | PPT slide 1 | **none** — embedded as a fence in `docs/diagrams/README.md` | Components or top-level flow change |
| `lab-deployment.mmd` | PPT slide 3 | **none** — embedded as a fence | Roles, hosts, ports, start order change |
| `dataflow-measurement-lifecycle.mmd` | — | `.png` + `.svg` | Timestamps, threads, queues, metric definitions change |
| `dataflow-runtime-topology.mmd` | — | `.png` + `.svg` | Processes, ports, env vars, volumes change |

The first two must stay consistent with their slides — update both or neither.
They also exist **twice**: the `.mmd` file and a copy inside a ` ```mermaid `
fence in `docs/diagrams/README.md`. Edit both, or the rendered page drifts
from the source.

### What GitHub can actually display

This drives the format policy; do not "simplify" it away:

- GitHub renders Mermaid **inside a fence in a markdown file**. It does
  **not** render a standalone `.mmd` — that shows as plain text.
- A Mermaid `.svg` uses `<foreignObject>` for labels, which GitHub's
  sanitiser strips, so it often displays blank. **PNG is the GitHub-safe
  format**; SVG is for print and papers.

### Artifact policy

- Keep every `.mmd` — 13 KB in total, and the source.
- Overview diagrams ship **no** image; the fence is the rendered copy.
- Detailed diagrams ship **PNG** (GitHub) and **SVG** (print/papers).
- The hero ships **two** PNGs: `system-hero.png` (2880×1620, slides and
  print) and `system-hero-web.png` (1600×900, ~346 KB, palette-quantised),
  which is the one embedded in the root README. **Never put the full-size
  file in a README** — it is the most-loaded page in the repo.
- No film grain. It was added to hide gradient banding that was never
  actually observed, and cost +71% file size because noise defeats PNG
  compression.
- **Commit binaries only when the diagram meaningfully changed.** Git stores
  each revision whole; the hero was regenerated seven times in one afternoon,
  which would have added ~30 MB of history for one image. Regenerating is
  free; committing is not.

### Editing rules

- Palette and fonts live once per file, in the `%%{init: …}%%` block and the
  `classDef` lines. Change a hex there, never per-node.
- Grayscale only. Dark fill `#2B3136` means *timestamp* in the lifecycle
  diagram and *clock authority* elsewhere; preserve that meaning.
- Never leave a bare `%%` line — Mermaid can read it as a directive and
  swallow the lines that follow.
- Keep the two detailed diagrams separate. They answer different questions
  ("where does this number come from" vs "what runs where"); merged, both
  become unreadable.

### Render, then actually look

```bash
bash docs/diagrams/render.sh
```

**View every changed diagram as an image.** Parsing proves nothing, and
neither does grep. Two different failures, both real:

- A version of the lifecycle diagram parsed cleanly while missing the edge
  carrying the payload from the gNB to the edge host — the data flow was
  silently wrong.
- After ADR-0006 corrected the transport, a grep for `tcp/` cleaned the
  endpoint strings but left `"Zenoh over TCP, dialled OUT to the router"` — a
  prose label the pattern never matched. It surfaced only on looking at the
  render. **When a fact changes, search for the claim, not the syntax**: the
  diagrams hold their own copies of ports, schemes and env vars.

Check specifically: every logical connection present (trace the payload path
end to end); no cluster fill other than white (Mermaid's default is pale
yellow); text legible at full resolution — judge from the rendered file, not a
downscaled preview, which misrepresents colour.

---

## Stage 4 — hero image (tier 3 only)

One picture that explains the whole system to someone who will not read
anything else. Source `docs/diagrams/system-hero.html`, output
`docs/diagrams/system-hero.png` (3840×2160). **Edit the HTML, never the PNG.**

Rendered by headless Chromium (puppeteer, already present as a mermaid-cli
dependency), so full CSS is available: gradients, soft shadows, rounded
geometry, real typography.

```bash
bash .claude/skills/doc-sync/scripts/render_hero.sh
```

### The design contract

The reader is assumed to know O-RAN and ROS2 already. The picture's job is to
be **recognised**, not explained — so it uses the names that audience expects
and shows both where things run and what crosses each link.

**Spatial layer** — four sites left to right, each a dashed zone, with a real
air gap at Uu drawn as radiated arcs:

| Zone | Contains | Colour |
|---|---|---|
| UE site · robot | LiDAR, robotic arm, ROS 2 client, 5G modem | terracotta |
| O-RAN · srsRAN | O-RU, O-DU (MAC scheduler), O-CU, ran-collector | amber |
| 5G core · Open5GS | AMF/SMF, NRF/UDM/UDR, UPF | slate |
| MEC edge | Zenoh router, edge ingest, CSV, PostgreSQL | sage |

Links between zones carry the interface name above and the payload below
(`Uu`, `N3`/GTP-U, `N6`/tcp:7447).

**Functional layer** — a measurement axis beneath the zones, x-aligned to
them: four stamp points (`capture_ns`, `send_ns`, `recv_ns`,
`process_done_ns`) with spans showing `sender`, `network`, `processing`, and
`glass-to-glass` underneath. The `network` span visibly crosses the RAN and
core, which is the whole argument of the platform.

**Telemetry plane** — the dark band tying it together: PTP, the 64-byte
envelope, the metric definitions, the single `RUN_ID`.

Rules that keep it working:

- **Zones, not a row of cards.** It is a distributed system; draw it as one.
- **Use the reader's vocabulary.** `O-DU`, `UPF`, `PointCloud2`, `rmw_zenoh`,
  `MAC`, `HARQ`, `PRB` — not paraphrases. A specialist should locate the
  components without reading sentences.
- **Soft depth only** — one diffuse shadow and a faint top sheen. No bevels or
  isometric extrusion; they date badly and undercut a research context.
- **Say what is aspirational.** The robotic arm is drawn as intended
  deployment. The footer states what is real: results now return to the UE for
  the renderer (ADR-0009), but nothing on the UE is *commanded* — a return of
  results is not actuation. If that changes, update the tagline, the footer and
  the UE zone's chips together; leaving the tagline claiming a loop the picture
  does not show is how this drifted the first time.

### Traps, all already hit once

- **Gradients on a horizontal line need `gradientUnits="userSpaceOnUse"`.**
  A stroked horizontal path has a zero-height bounding box, and the default
  `objectBoundingBox` units degenerate to no paint at all — the shafts vanish
  while the solid arrowheads still render, which looks like a layout problem
  and is not.
- **`margin-top: auto` on the wrong child pushes content out of its box**,
  where it lands on the page background as pale unreadable text.
- **Vertical slack has to go somewhere deliberate.** A zone stretched to a tall
  row leaves a void; `flex:1` on every chip makes one-line chips as tall as
  four-line ones; `space-between` just spreads the void around. Size the row to
  its content and give the leftover to something that earns it — that space is
  where the measurement axis went.
- **Full-width Unicode digits look identical to ASCII in a hex colour**
  (`#C48F２C`) and silently kill the gradient. Scan the file for non-ASCII
  before rendering.

### AI image generators — deliberately not used

Considered and rejected for the diagram itself: generative models cannot
render `O-DU`, `UPF` or `rmw_zenoh` reliably, cannot guarantee that the right
box connects to the right box, and cannot be regenerated deterministically
after a doc change — which breaks the premise that this image tracks the docs.
If a stylised variant is ever wanted, produce it externally from this PNG and
keep it as a separate presentation asset, never the canonical one.

### Verify by looking

There is no automated gate for aesthetics. Render, then **view the PNG** and
check: no text outside a card, arrow shafts actually visible, nothing clipped
at the frame edge, and the four card colours still distinguishable in
greyscale (it will be printed and photocopied).

Downscale before viewing only to fit a preview; judge colour and legibility
from the full-resolution file.

---

## Style — hold constant across runs

Consistency between updates matters more than any single wording choice.

**Framing — get this right before anything else.** mec-cast is an
**experimentation testbed for industrial communication over private 5G**
(srsRAN + Open5GS). It is *not* a latency-measurement engine. Precise
timestamping is one instrument among several — it is what makes the other
findings trustworthy, not the reason the system exists.

Describe it by what it investigates, in this order:

1. large data transmission in industrial systems
2. minimal latency for teleoperation
3. fast communication between nearby peers and edge processing

The supporting capabilities — reproducible workloads, RAN observability,
transport comparison, controlled impairment, PTP timing — are *how* those
questions get answered. `docs/_facts.yml` holds the canonical wording under
`platform.purpose`, `research_aims` and `capabilities`; propagate from there.

Watch for the old framing resurfacing: any sentence leading with "measurement
platform", "the value is per-frame latency", or a title that makes timing the
subject rather than the method. Those predate this positioning and should be
rewritten when encountered, in any doc, slide or diagram.

**Brevity.** Keep only what a reader must have. The title carries the point;
the body supports it. Prefer a table to a paragraph, a sentence to a
paragraph, deletion to hedging. On slides especially — a slide is a visual
aid, not a document; the graph and its title do the explaining.

**Voice.** Plain and declarative. State what is true, then why it matters. No
marketing register, no "simply"/"just"/"easily", no exclamation marks.

**Honesty about limits.** Say what is untested, unverified or unimplemented —
plainly, once. `make test-legacy` covers signalling only; `ptp.reliable` is
false in local dev. These statements are load-bearing; never quietly drop one
to make a section read better.

**Rationale.** One clause on why, when the choice is non-obvious. Long-form
rationale belongs in an ADR — link it, don't restate it.

**Formatting.** Sentence case headings. Backticks for paths, commands,
identifiers. Relative markdown links. One command per fenced `bash` block.
Every table column earns its place.

**Do not write.** Changelogs in prose ("recently we added…"), future promises
beyond a linked plan, or a component README restated inside `docs/` — link
instead. Docs describe the current state.

---

## QA gates — required before reporting done

| Stage | Gate | Catches |
|---|---|---|
| 1 | `python .claude/skills/doc-sync/scripts/qa_docs.py` | broken links, dead paths, missing make targets |
| 1 | `python .claude/skills/doc-sync/scripts/facts_check.py` | docs contradicting `_facts.yml` |
| 2 | `python .claude/skills/doc-sync/scripts/qa_pptx.py <deck>` | overflow, off-slide, content and exclusion rules |
| 3 | `python .claude/skills/doc-sync/scripts/fence_check.py` | embedded mermaid fences drifting from their `.mmd` |
| 3 | `bash docs/diagrams/render.sh` + view each changed image | broken edges, wrong fills, unreadable text |
| 4 | `bash …/render_hero.sh` + view the PNG | text outside cards, invisible arrows, clipping |

`fence_check.py` exists because the two overview diagrams are stored twice —
as `.mmd` and as a fence in `docs/diagrams/README.md`. It compares the graphs
(node ids and edges) rather than the text, since the fences deliberately drop
the `%%{init}` theme block.

Run the gates for the stages you ran. Report failures you could not fix rather
than lowering the bar.

`qa_docs.py` carries a short allowlist of paths that are correct but relative
to a context it cannot see (inside a release zip, inside `runs/<RUN_ID>/`).
Every entry has a stated reason. Add to it only with one — a suppression
nobody can justify is how a gate rots.

Its `make X` check looks **only inside backticks and fenced blocks**. In prose
"make" is an ordinary verb, and matching it there flagged ADR-0006's "mixed
traffic classes make head-of-line blocking worth addressing" on every run for
three syncs. The old defence was a stoplist of words that may follow "make",
which cannot be completed — any noun in the language can. If a gate reports the
same false positive twice, fix the gate; a finding everyone has learned to
ignore is worth less than no finding at all.

## Environment

- Git and the toolchains live in WSL; run them there.
- Editing a `.sh` through the Windows mount strips its exec bit. Before
  committing: `chmod +x $(git ls-files '*.sh')`, then check
  `git diff --summary | grep mode`.
- **Windows Python rewrites line endings.** `pathlib.write_text()` run under
  Git Bash translates `\n` to CRLF, which rewrites every line of the file and
  turns a fifteen-line edit into a 345-line diff. Either run Python inside
  WSL, or pass `newline="\n"`. Check before staging:

  ```bash
  git ls-files -- '*.md' '*.yml' '*.html' '*.js' '*.mmd' | xargs grep -lU $'\r'
  ```
- `node` comes from nvm and is absent in non-interactive shells — source
  `~/.nvm/nvm.sh` first.
- LibreOffice is not installed, so the deck cannot be rendered to images.
  `qa_pptx.py` substitutes geometric checks; say so when reporting.

## Committing

Do not commit unless asked. When asked: show the message for review first,
never add a `Co-Authored-By` trailer, and keep the facts-file SHA bump in the
same commit as the sync it describes.

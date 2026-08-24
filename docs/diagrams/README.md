# Diagrams

Sources and rendered artifacts for the platform's diagrams. Everything here is
generated from a text source that lives beside it — edit the source, re-render,
never touch the output.

## How GitHub actually renders these

Worth stating plainly, because it drives every decision below:

- GitHub renders Mermaid **inside a ` ```mermaid ` fence in a markdown file**.
- GitHub does **not** render a standalone `.mmd` file — open one and you get
  plain text.
- A Mermaid `.svg` uses `<foreignObject>` for its labels, and GitHub's
  sanitiser strips it, so the diagram often appears blank or partial.

So: overview diagrams are **embedded as fences** (below) and ship no image at
all; the detailed ones ship a **PNG**, which is the format GitHub can display.
SVG is generated on demand for print and is not committed.

## What is kept, and why

| File | Format | Why it exists |
|---|---|---|
| `architecture-overview.mmd` | source only | embedded below — GitHub renders it |
| `lab-deployment.mmd` | source only | embedded below — GitHub renders it |
| `dataflow-measurement-lifecycle.mmd` | source | too large to embed readably |
| `dataflow-runtime-topology.mmd` | source | too large to embed readably |
| `dataflow-*.png` | raster | the copy GitHub can actually display |
| `dataflow-*.svg` | vector | **not committed** — `render.sh --svg` when a paper needs it |
| `system-hero.html` | source | the one-picture overview |
| `system-hero.png` | 2880×1620 | slides, print |
| `system-hero-web.png` | 1600×900, 346 KB | embedded in the root README |

Two hero files on purpose: the root README is the most-loaded page in the
repo and should not pull a multi-megabyte image. Never swap the full-size one
into the README.

**Committing binaries:** regenerate as often as you like, but commit a PNG or
SVG only when the diagram meaningfully changed. Git stores each binary
revision whole — during one afternoon of iteration the hero was regenerated
seven times, which would have added ~30 MB of history for a single image.

## Rendering

```bash
bash docs/diagrams/render.sh
```

```bash
bash .claude/skills/doc-sync/scripts/render_hero.sh
```

Needs `@mermaid-js/mermaid-cli` (which brings its own Chromium). The two
overview diagrams need no toolchain at all — GitHub renders them from the
fences below.

## Editing

The palette and fonts are set once per file, in the `%%{init: …}%%` block and
the `classDef` lines. Change a hex value there, never per-node. Grayscale
only; dark fill `#2B3136` means *timestamp* in the lifecycle diagram and
*clock authority* elsewhere.

Never leave a bare `%%` line — Mermaid can read it as a directive and swallow
the lines that follow.

---

## Architecture overview

Source: [`architecture-overview.mmd`](architecture-overview.mmd) — keep the two
copies identical when editing.

```mermaid
flowchart LR
  classDef comp fill:#F1F2F3,stroke:#5A6167,stroke-width:1px,color:#1A1A1A
  classDef spine fill:#2B3136,stroke:#2B3136,color:#FFFFFF
  classDef store fill:#FFFFFF,stroke:#5A6167,stroke-width:1px,color:#1A1A1A
  classDef note fill:#FFFFFF,stroke:#B9BDC0,stroke-dasharray:3 3,color:#444C52

  subgraph UE["UE — robot compute"]
    LIDAR["LiDAR sensor<br/>(synthetic source today)"]
    ROSC["ROS2 client<br/>stamps capture_ns, send_ns"]
    RENDER["ROS2 render node<br/>stamps process_done_ns · draws the result"]
    LIDAR --> ROSC
  end

  subgraph NET["5G network — lab"]
    GNB["srsRAN — O-CU / O-DU<br/>MAC scheduler in the DU · USRP radio"]
    CORE["Open5GS core<br/>UPF / N6"]
    GNB --> CORE
  end

  subgraph EDGE["MEC edge server"]
    ZR["Zenoh router"]
    EN["Edge ingest node<br/>stamps recv_ns, process_done_ns"]
    ADMIN["mec-cast-admin<br/>run control plane · WebSocket"]
    ZR --> EN
  end

  RANC["ran-collector<br/>O-DU MAC / scheduler KPIs"]
  SPINE["mec-cast-telemetry — shared spine<br/>64-byte TimingEnvelope · DelayStats · clocks + PTP · lock-free recorder"]
  CSV["Per-frame CSV<br/>runs/&lt;RUN_ID&gt;/"]
  LOG["Logging service + PostgreSQL<br/>2 s aggregated snapshots"]

  ROSC -->|"Uu — PointCloud2"| GNB
  CORE -->|"UPF / N6"| ZR
  EN -.->|"mec_cast/result — voxel cloud, opt-in"| RENDER
  GNB -.->|"UDP JSON metrics"| RANC

  ROSC --- SPINE
  EN --- SPINE
  RENDER --- SPINE
  RANC --- SPINE

  SPINE --> CSV
  SPINE -->|"HTTP, joined by trace_id = RUN_ID"| LOG

  ADMIN <-.->|"control · run start/stop, status"| ROSC
  ADMIN <-.-> EN
  ADMIN <-.-> RANC
  ADMIN <-.-> RENDER

  PTP["PTP grandmaster — management / backhaul LAN<br/>ptp4l + phc2sys on every measuring host, never the 5G user plane"]
  PTP -.-> UE
  PTP -.-> EDGE
  PTP -.-> RANC

  class LIDAR,ROSC,RENDER,GNB,CORE,ZR,EN,RANC,ADMIN comp
  class SPINE spine
  class CSV,LOG store
  class PTP note
```

## Lab deployment

Source: [`lab-deployment.mmd`](lab-deployment.mmd). Deploy order:
`infra → edge → gnb → ue`.

```mermaid
  "background":"#FFFFFF","primaryColor":"#F1F2F3","primaryTextColor":"#1A1A1A",
  "primaryBorderColor":"#5A6167","lineColor":"#5A6167","textColor":"#1A1A1A",
  "clusterBkg":"#FFFFFF","clusterBorder":"#1A1A1A","titleColor":"#1A1A1A","nodeTextColor":"#1A1A1A",
  "fontFamily":"Calibri, Arial, sans-serif","fontSize":"14px"}} }%%
flowchart LR
  classDef host fill:#F1F2F3,stroke:#5A6167,stroke-width:1px,color:#1A1A1A
  classDef svc fill:#FFFFFF,stroke:#5A6167,stroke-width:1px,color:#1A1A1A
  classDef sync fill:#2B3136,stroke:#2B3136,color:#FFFFFF

  subgraph UEH["Host 1 — role: ue"]
    direction TB
    LC["lidar-client<br/>network_mode: host<br/>needs EDGE_HOST, LOGGING_HOST"]
    PTPU["/dev/ptp0"]
  end

  subgraph GNBH["Host 2 — role: gnb"]
    direction TB
    SRS["srsRAN — O-CU / O-DU<br/>metrics: addr + port in gnb.yml"]
    RC["ran-collector<br/>binds UDP :55555"]
    SRS -->|"UDP JSON"| RC
  end

  subgraph EDGEH["Host 3 — role: edge"]
    direction TB
    ZR["zenoh-router<br/>listens udp/:7447?rel=1"]
    ED["edge<br/>ingest + processing"]
    ADM["mec-cast-admin<br/>:8099 control plane"]
    ZR --> ED
  end

  subgraph INFRAH["Host 4 — role: infra"]
    direction TB
    LOGS["logging service<br/>:8000, auto-migrate"]
    PG["postgres:16<br/>volume pgdata"]
    LOGS --> PG
  end

  LC -->|"5G user plane: UE → O-DU/O-CU → UPF → udp/7447?rel=1"| ZR
  LC -.->|"HTTP :8000 snapshots"| LOGS
  ED -.->|"HTTP :8000 snapshots"| LOGS
  RC -.->|"HTTP :8000 snapshots"| LOGS

  LC -.->|"ws :8099 control"| ADM
  ED -.-> ADM
  RC -.-> ADM

  GM["PTP grandmaster — management / backhaul LAN"]
  GM -->|"ptp4l + phc2sys"| UEH
  GM -->|"ptp4l + phc2sys"| GNBH
  GM -->|"ptp4l + phc2sys"| EDGEH

  RUNS["runs/&lt;RUN_ID&gt;/ on each host<br/>per-frame CSV stays local"]
  LC -.-> RUNS
  ED -.-> RUNS
  RC -.-> RUNS

  class UEH,GNBH,EDGEH,INFRAH host
  class LC,SRS,RC,ZR,ED,ADM,LOGS,PG,PTPU,RUNS svc
  class GM sync
```

## Detailed dataflow

Too large to embed readably; rendered instead.

**Measurement lifecycle** — one frame's journey through every timestamp,
thread and queue.
[PNG](dataflow-measurement-lifecycle.png) ·
[source](dataflow-measurement-lifecycle.mmd)

**Runtime topology** — every process, port, protocol, env var and volume.
[PNG](dataflow-runtime-topology.png) ·
[source](dataflow-runtime-topology.mmd)

For print or a paper, `bash docs/diagrams/render.sh --svg` produces vector
copies alongside (gitignored).

## The hero image

`system-hero.html` is hand-written HTML/CSS rather than Mermaid: it needs
gradients, soft shadows and real typographic control, and it is meant for
people who will not read anything else. Four sites over a measurement axis,
using the vocabulary an O-RAN or ROS2 reader already knows.

The full design contract lives in the
[`doc-sync` skill](../../.claude/skills/doc-sync/SKILL.md).

## Known limitation: auto-layout

The Mermaid diagrams are laid out automatically rather than hand-positioned.
That keeps the source easy to edit, but the router occasionally draws an edge
across a box it is not logically connected to (visible on
`dataflow-runtime-topology`). The connection itself is always correct; only
the visual path is imperfect. If a diagram ever needs exact positioning,
hand-authored SVG is the alternative.

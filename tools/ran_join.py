#!/usr/bin/env python3
"""Join a run's per-frame latency with the RAN state each frame met.

    python3 tools/ran_join.py runs/<run_id>                 # summary
    python3 tools/ran_join.py runs/<run_id> -o joined.csv   # per-frame table
    python3 tools/ran_join.py runs/<run_id> --source kpm --ue e2:0

For every frame in ``edge-0/samples.csv`` it takes, per RAN metric, the most
recent RAN sample at or before the frame's time (an as-of join), for the
chosen UE and its cell. A RAN sample older than ``--max-age-ms`` (default:
twice the observed report period) is left blank rather than stretched.

The frame time is ``capture_ns`` by default — when the cloud was made on the
UE, i.e. when it was about to go on the air — or ``recv_ns`` with ``--at
recv``. Both are on the shared clock; across hosts that join is only as good
as PTP (docs/architecture/timing-model.md), and the summary says which.

RAN rows come from ``ran/kpi.csv`` (json, the collector) or ``ran-kpm/kpi.csv``
(kpm, the xApp). A run recorded before kpi.csv existed is re-normalised from
``ran/reports.jsonl`` with the same rules (ran/py, mec_cast_ran).

Standard library only. The summary is descriptive — a Pearson coefficient per
metric against network and end-to-end delay — not a model; it is there to say
where to look.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ran" / "py" / "src"))

from mec_cast_ran import normalise_json  # noqa: E402


def load_ran(run: Path, source: str) -> list[dict]:
    leaf = "ran" if source == "json" else "ran-kpm"
    kpi = run / leaf / "kpi.csv"
    if kpi.exists():
        with kpi.open() as f:
            return [
                {**r, "gnb_ts_ns": int(r["gnb_ts_ns"]), "recv_ns": int(r["recv_ns"]),
                 "value": float(r["value"])}
                for r in csv.DictReader(f)
            ]
    raw = run / leaf / "reports.jsonl"
    if source == "json" and raw.exists():
        print(f"note: no {kpi.relative_to(run)}; re-normalising {raw.relative_to(run)}", file=sys.stderr)
        rows = []
        for line in raw.open():
            try:
                report = json.loads(line)
            except ValueError:
                continue
            for s in normalise_json(report):
                rows.append({**s.as_dict(), "recv_ns": 0})
        return rows
    raise SystemExit(f"no RAN data for source={source} under {run}/{leaf}/")


def load_frames(run: Path, site: str) -> list[dict]:
    path = run / site / "samples.csv"
    if not path.exists():
        raise SystemExit(f"no {path}")
    with path.open() as f:
        return [r for r in csv.DictReader(f) if r.get("kind", "frame") == "frame"]


def pick_ue(rows: list[dict], wanted: str | None) -> str:
    ues = sorted({r["ue"] for r in rows if r["ue"]})
    if wanted:
        if wanted not in ues:
            raise SystemExit(f"--ue {wanted!r} not in the data; UEs seen: {ues}")
        return wanted
    if not ues:
        raise SystemExit("no per-UE RAN rows in this run")
    if len(ues) == 1:
        print(f"UE: {ues[0]} (the only one in the data)", file=sys.stderr)
        return ues[0]
    ul = defaultdict(float)
    for r in rows:
        if r["metric"] == "ue.ul_throughput_bps":
            ul[r["ue"]] += r["value"]
    choice = max(ues, key=lambda u: ul[u])
    print(f"UE: {choice} — most uplink of {ues}; pass --ue to choose", file=sys.stderr)
    return choice


def series(rows: list[dict], ue: str) -> dict[str, tuple[list[int], list[float]]]:
    """metric -> (sorted gnb_ts_ns, values) for this UE and its cell."""
    cells = {r["cell"] for r in rows if r["ue"] == ue}
    out: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for r in rows:
        if r["gnb_ts_ns"] <= 0:
            continue
        if r["ue"] == ue or (not r["ue"] and r["cell"] in cells):
            out[r["metric"]].append((r["gnb_ts_ns"], r["value"]))
    result = {}
    for metric, pts in out.items():
        pts.sort()
        result[metric] = ([t for t, _ in pts], [v for _, v in pts])
    return result


def report_period_ns(s: dict[str, tuple[list[int], list[float]]]) -> int:
    gaps = []
    for ts, _ in s.values():
        gaps += [b - a for a, b in zip(ts, ts[1:]) if b > a]
    return int(statistics.median(gaps)) if gaps else 1_000_000_000


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run", type=Path, help="runs/<run_id>")
    ap.add_argument("--source", choices=["json", "kpm"], default="json")
    ap.add_argument("--ue", help="RNTI (json) or e2:<id> (kpm); default: inferred")
    ap.add_argument("--site", default="edge-0", help="per-frame CSV directory (edge-0)")
    ap.add_argument("--at", choices=["capture", "recv"], default="capture")
    ap.add_argument("--max-age-ms", type=float, help="default: 2x the report period")
    ap.add_argument("-o", "--out", type=Path, help="write the per-frame table here")
    args = ap.parse_args(argv)

    rows = load_ran(args.run, args.source)
    frames = load_frames(args.run, args.site)
    ue = pick_ue(rows, args.ue)
    s = series(rows, ue)
    period = report_period_ns(s)
    max_age = int(args.max_age_ms * 1e6) if args.max_age_ms else 2 * period
    metrics = sorted(s)
    at_key = f"{args.at}_ns"

    joined = []
    for fr in frames:
        t = int(fr[at_key] or 0)
        if t <= 0:
            continue
        rec = {"seq": fr["seq"], at_key: t, "network_ns": fr.get("network_ns", ""),
               "e2e_ns": fr.get("e2e_ns", "")}
        for m in metrics:
            ts, vs = s[m]
            i = bisect.bisect_right(ts, t) - 1
            if i >= 0 and t - ts[i] <= max_age:
                rec[m] = vs[i]
                rec[f"{m}__age_ms"] = round((t - ts[i]) / 1e6, 3)
            else:
                rec[m] = ""
        joined.append(rec)

    if args.out:
        cols = ["seq", at_key, "network_ns", "e2e_ns"]
        for m in metrics:
            cols += [m, f"{m}__age_ms"]
        with args.out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(joined)
        print(f"wrote {len(joined)} frames x {len(metrics)} metrics to {args.out}", file=sys.stderr)

    # --- summary ---
    covered = sum(1 for r in joined if any(r[m] != "" for m in metrics))
    print(f"run {args.run.name}: {len(joined)} frames, {covered} with RAN state "
          f"(source={args.source}, ue={ue}, report period ~{period / 1e6:.0f} ms, "
          f"max age {max_age / 1e6:.0f} ms, joined at {args.at}_ns)")
    print("cross-host join: valid only under a reliable PTP lock (check context.ptp.reliable)")
    print("r is descriptive: RAN state and delay are both autocorrelated, so |r| ~0.5 arises")
    print("by chance over a short run. Measured on gnb-sim, whose RAN model is NOT coupled to")
    print("the network at all: r(cqi, network) = -0.69 over 45 s. Compare runs; do not read one.")
    print(f"\n{'metric':40s} {'n':>6s} {'mean':>14s} {'r(network)':>11s} {'r(e2e)':>9s}")
    for m in metrics:
        pairs = [(float(r[m]), r["network_ns"], r["e2e_ns"]) for r in joined if r[m] != ""]
        if not pairs:
            continue
        vals = [p[0] for p in pairs]
        net = [(v, float(n)) for v, n, _ in pairs if n not in ("", None)]
        e2e = [(v, float(e)) for v, _, e in pairs if e not in ("", None)]
        rn = pearson([a for a, _ in net], [b for _, b in net])
        re = pearson([a for a, _ in e2e], [b for _, b in e2e])
        fmt = lambda r: f"{r:+.2f}" if r is not None else "-"  # noqa: E731
        print(f"{m:40s} {len(vals):6d} {statistics.fmean(vals):14.4g} {fmt(rn):>11s} {fmt(re):>9s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

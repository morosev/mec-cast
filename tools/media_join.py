#!/usr/bin/env python3
"""Join a media run's frames across its sites, and with WebRTC's own state.

    python3 tools/media_join.py runs/<run_id>                 # summary
    python3 tools/media_join.py runs/<run_id> -o joined.csv   # per-frame table

A media run (docs/architecture/str0m-profile.md) records each video frame at
the relay (``relay/samples.csv``, site 3) and the receiver
(``receiver/samples.csv``, site 4), keyed by the frame's RTP timestamp
(``seq``). Joined, one frame splits into its legs:

- ``uplink``: sender to relay (the relay's ``network_ns``);
- ``relay``: the relay's forwarding (its ``processing_ns``);
- ``downlink``: relay to receiver (receiver ``recv_ns`` - relay ``process_done_ns``);
- ``receiver``: jitter buffer, decode, render (the receiver's ``processing_ns``);
- ``e2e``: glass-to-glass (the receiver's ``e2e_ns``).

Every node also writes ``webrtc.csv``: WebRTC's statistics per RTP stream
every second, cumulative counters. Each frame gets, as of its capture time
(an as-of join, blank when the latest row is older than ``--max-age-ms``),
the video stream's state at the three nodes -- the sender's bandwidth
estimate, target and actual bitrate; jitter and loss as the relay saw the
uplink; the receiver's jitter, jitter buffer and freezes. Rates (bitrate,
packets lost per second) are derived from consecutive rows.

Frames against the RAN's state: ``tools/ran_join.py runs/<run_id> --site
relay`` (or ``--site receiver``) -- these samples share the platform schema.

Cross-host legs are only as good as PTP (docs/architecture/timing-model.md).
Standard library only.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import statistics
import sys
from pathlib import Path

LEGS = ("uplink", "relay", "downlink", "receiver", "e2e")

# (column in the joined table, node, direction, webrtc.csv field or derived rate)
STATE = (
    ("send_bwe_bps", "sender", "out", "bwe_bps"),
    ("send_target_bps", "sender", "out", "target_bps"),
    ("send_bitrate_bps", "sender", "out", "bitrate_bps"),
    ("send_fps", "sender", "out", "fps"),
    ("send_height", "sender", "out", "height"),
    ("send_quality_limitation", "sender", "out", "quality_limitation"),
    ("uplink_jitter_ms", "relay", "in", "jitter_ms"),
    ("uplink_loss_fraction", "relay", "in", "loss_fraction"),
    ("uplink_rtt_ms", "relay", "in", "rtt_ms"),
    ("downlink_jitter_ms", "receiver", "in", "jitter_ms"),
    ("downlink_lost_per_s", "receiver", "in", "lost_per_s"),
    ("recv_bitrate_bps", "receiver", "in", "bitrate_bps"),
    ("recv_jitter_buffer_ms", "receiver", "in", "jitter_buffer_ms"),
    ("recv_freezes", "receiver", "in", "freezes"),
    ("recv_nacks", "receiver", "in", "nacks"),
    ("recv_plis", "receiver", "in", "plis"),
)


def num(v: str | None) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load_frames(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        return {r["seq"]: r for r in csv.DictReader(f) if r.get("kind", "frame") == "frame"}


def join_frames(relay: dict[str, dict], receiver: dict[str, dict]) -> list[dict]:
    """One row per frame both sites saw, its legs in ns, in capture order."""
    out = []
    for seq, v in receiver.items():
        r = relay.get(seq)
        if r is None:
            continue
        out.append(
            {
                "seq": int(seq),
                "capture_ns": int(v["capture_ns"]),
                "uplink": int(r["network_ns"]),
                "relay": int(r["processing_ns"]),
                "downlink": int(v["recv_ns"]) - int(r["process_done_ns"]),
                "receiver": int(v["processing_ns"]),
                "e2e": int(v["e2e_ns"]),
            }
        )
    out.sort(key=lambda f: f["capture_ns"])
    return out


def load_stats(run: Path) -> dict[tuple[str, str], list[dict]]:
    """Video rows per (node, direction), one stream each, in time order, with
    rates derived from consecutive counters. At the relay the first
    subscriber stands for the downlink."""
    series: dict[tuple[str, str], list[dict]] = {}
    for node in ("sender", "relay", "receiver"):
        path = run / node / "webrtc.csv"
        if not path.exists():
            continue
        with path.open(newline="") as f:
            rows = [r for r in csv.DictReader(f) if r["kind"] == "video"]
        peers: dict[tuple[str, str], str] = {}
        for r in rows:
            peers.setdefault((r["direction"], r["peer"]), r["peer"])
        for direction in ("in", "out"):
            first = next((p for (d, p) in peers if d == direction), None)
            if first is None:
                continue
            s = [r for r in rows if r["direction"] == direction and r["peer"] == first]
            s.sort(key=lambda r: int(r["ts_ns"]))
            for prev, cur in zip([None] + s[:-1], s):
                cur["bitrate_bps"] = cur["lost_per_s"] = ""
                if prev is None:
                    continue
                dt = (int(cur["ts_ns"]) - int(prev["ts_ns"])) / 1e9
                if dt <= 0:
                    continue
                b0, b1 = num(prev["bytes"]), num(cur["bytes"])
                if b0 is not None and b1 is not None:
                    cur["bitrate_bps"] = round((b1 - b0) * 8 / dt)
                l0, l1 = num(prev["packets_lost"]), num(cur["packets_lost"])
                if l0 is not None and l1 is not None:
                    cur["lost_per_s"] = round((l1 - l0) / dt, 3)
            series[(node, direction)] = s
    return series


def period_ns(rows: list[dict]) -> int:
    ts = [int(r["ts_ns"]) for r in rows]
    gaps = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    return int(statistics.median(gaps)) if gaps else 1_000_000_000


def as_of(rows: list[dict], keys: list[int], t: int, max_age: int) -> dict | None:
    i = bisect.bisect_right(keys, t) - 1
    if i < 0 or t - keys[i] > max_age:
        return None
    return rows[i]


def attach_state(frames: list[dict], series: dict, max_age_ns: int | None) -> None:
    index = {k: ([int(r["ts_ns"]) for r in s], s) for k, s in series.items()}
    for f in frames:
        for col, node, direction, field in STATE:
            f[col] = ""
            if (node, direction) not in index:
                continue
            keys, rows = index[(node, direction)]
            age = max_age_ns if max_age_ns is not None else 2 * period_ns(rows)
            row = as_of(rows, keys, f["capture_ns"], age)
            if row is not None:
                f[col] = row.get(field, "")


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def fmt(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:g}"


def summary(frames: list[dict], series: dict) -> str:
    lines = [f"{len(frames)} frames seen at both relay and receiver"]
    if frames:
        lines.append(f"  {'leg':<10}{'median ms':>11}{'p95 ms':>9}{'max ms':>9}")
        for leg in LEGS:
            xs = [f[leg] / 1e6 for f in frames]
            lines.append(
                f"  {leg:<10}{statistics.median(xs):>11.3f}{pct(xs, 0.95):>9.3f}{max(xs):>9.3f}"
            )
    for col, node, direction, field in STATE:
        s = series.get((node, direction))
        if not s:
            continue
        xs = [num(str(r.get(field, ""))) for r in s]
        xs = [x for x in xs if x is not None]
        if xs:
            lines.append(f"  {col:<26} median {fmt(statistics.median(xs))}, max {fmt(max(xs))}")
        elif field == "quality_limitation":
            reasons = sorted({r[field] for r in s if r[field]})
            if reasons:
                lines.append(f"  {col:<26} {', '.join(reasons)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run", type=Path, help="runs/<run_id>")
    ap.add_argument("--max-age-ms", type=float, help="default: 2x the stats period")
    ap.add_argument("-o", "--out", type=Path, help="write the per-frame table here")
    args = ap.parse_args(argv)

    relay = load_frames(args.run / "relay" / "samples.csv")
    receiver = load_frames(args.run / "receiver" / "samples.csv")
    if not relay or not receiver:
        print(f"{args.run}: needs relay/ and receiver/ samples.csv", file=sys.stderr)
        return 2
    frames = join_frames(relay, receiver)
    series = load_stats(args.run)
    max_age = int(args.max_age_ms * 1e6) if args.max_age_ms is not None else None
    attach_state(frames, series, max_age)

    print(summary(frames, series))
    if args.out:
        cols = ["seq", "capture_ns", *(f"{leg}_ns" for leg in LEGS), *(c for c, *_ in STATE)]
        with args.out.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for fr in frames:
                w.writerow(
                    [fr["seq"], fr["capture_ns"], *(fr[leg] for leg in LEGS)]
                    + [fr[c] for c, *_ in STATE]
                )
        print(f"wrote {len(frames)} frames to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

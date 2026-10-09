"""srsRAN JSON reports and E2SM-KPM indications -> :class:`RanSample` rows.

The rules, kept identical to ``ran/collector/src/normalise.rs`` by
``ran/schema/vectors.json``:

JSON (srsRAN metrics, both layouts — ``ue_list[].ue_container`` up to 24.x,
``cells[].{cell_metrics,event_list,ue_list}`` from 25.x):

* every **numeric** field of a UE becomes ``ue.<canonical>`` when the field is
  in :data:`JSON_UE`, otherwise ``ue.raw.<field>``;
* a UE field that is an ``{"avg": .., "max": ..}`` object becomes one row per
  numeric key, ``<name>_<key>``;
* cell fields likewise, ``cell.<canonical>`` / ``cell.raw.<field>``; a numeric
  array becomes ``<name>_mean``;
* each ``event_list`` entry becomes ``event.<event_type>`` = 1 for its RNTI;
* booleans, strings, ``null`` and identity fields (rnti, pci, ue) are not
  measurements and produce no row;
* a report with no cells and no UEs (another metrics layer) produces nothing —
  its raw form is still kept by whoever recorded it.

Nothing is guessed: a field whose unit srsRAN does not document gets unit "".
Rows are returned sorted by (cell, ue, metric) so both implementations agree
on order.
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Any, Iterable

from .model import RanSample

# --- JSON (srsRAN) ------------------------------------------------------------

#: srsRAN per-UE field -> (canonical metric, unit, scale)
JSON_UE: dict[str, tuple[str, str, float]] = {
    "dl_brate": ("ue.dl_throughput_bps", "bit/s", 1.0),
    "ul_brate": ("ue.ul_throughput_bps", "bit/s", 1.0),
    "dl_mcs": ("ue.dl_mcs", "index", 1.0),
    "ul_mcs": ("ue.ul_mcs", "index", 1.0),
    "cqi": ("ue.cqi", "index", 1.0),
    "ri": ("ue.dl_ri", "layers", 1.0),
    "dl_ri": ("ue.dl_ri", "layers", 1.0),
    "ul_ri": ("ue.ul_ri", "layers", 1.0),
    "dl_nof_ok": ("ue.dl_harq_ok", "count", 1.0),
    "dl_nof_nok": ("ue.dl_harq_nok", "count", 1.0),
    "ul_nof_ok": ("ue.ul_harq_ok", "count", 1.0),
    "ul_nof_nok": ("ue.ul_harq_nok", "count", 1.0),
    "dl_bs": ("ue.dl_buffer_bytes", "byte", 1.0),
    "bsr": ("ue.ul_bsr_bytes", "byte", 1.0),
    "last_phr": ("ue.phr_db", "dB", 1.0),
    "pusch_snr_db": ("ue.pusch_snr_db", "dB", 1.0),
    "pucch_snr_db": ("ue.pucch_snr_db", "dB", 1.0),
    "pusch_rsrp_db": ("ue.pusch_rsrp_db", "dB", 1.0),
    "ta_ns": ("ue.ta_ns", "ns", 1.0),
    "pusch_ta_ns": ("ue.pusch_ta_ns", "ns", 1.0),
    "pucch_ta_ns": ("ue.pucch_ta_ns", "ns", 1.0),
    "srs_ta_ns": ("ue.srs_ta_ns", "ns", 1.0),
    # srsRAN does not document the unit of these; verify in the lab and only
    # then give them one (ran/schema/metrics.md, "verify" column).
    "sr_to_pusch_delay": ("ue.sr_to_pusch_delay", "", 1.0),
    "pusch_harq_delay": ("ue.pusch_harq_delay", "", 1.0),
    "pucch_harq_delay": ("ue.pucch_harq_delay", "", 1.0),
    "crc_delay": ("ue.crc_delay", "", 1.0),
    "ce_delay": ("ue.ce_delay", "", 1.0),
    "max_pusch_distance": ("ue.max_pusch_distance", "", 1.0),
    "max_pdsch_distance": ("ue.max_pdsch_distance", "", 1.0),
}

#: srsRAN per-cell field -> (canonical metric, unit, scale)
JSON_CELL: dict[str, tuple[str, str, float]] = {
    "dl_brate": ("cell.dl_throughput_bps", "bit/s", 1.0),
    "ul_brate": ("cell.ul_throughput_bps", "bit/s", 1.0),
    "error_indication_count": ("cell.error_indications", "count", 1.0),
    "nof_failed_pdcch_allocs": ("cell.failed_pdcch_allocs", "count", 1.0),
    "nof_failed_uci_allocs": ("cell.failed_uci_allocs", "count", 1.0),
    "late_dl_harqs": ("cell.late_dl_harqs", "count", 1.0),
    "late_ul_harqs": ("cell.late_ul_harqs", "count", 1.0),
    "msg3_nof_ok": ("cell.msg3_ok", "count", 1.0),
    "msg3_nof_nok": ("cell.msg3_nok", "count", 1.0),
    "pusch_prbs_used_per_tdd_slot_idx": ("cell.pusch_prbs_used", "prb", 1.0),
    "pdsch_prbs_used_per_tdd_slot_idx": ("cell.pdsch_prbs_used", "prb", 1.0),
    "average_latency": ("cell.avg_latency", "", 1.0),
    "max_latency": ("cell.max_latency", "", 1.0),
    "avg_prach_delay": ("cell.avg_prach_delay", "", 1.0),
    "pucch_tot_rb_usage_avg": ("cell.pucch_rb_usage_avg", "", 1.0),
}

#: Identity, not measurement.
_IDENTITY = {"rnti", "pci", "ue", "timestamp"}


def _number(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


def _parse_iso_ns(text: str) -> int | None:
    t = text.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        d = _dt.datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)  # srsRAN writes no zone: UTC
    epoch = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
    delta = d - epoch
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def _ts_ns(v: Any) -> int | None:
    if isinstance(v, str):
        return _parse_iso_ns(v)
    n = _number(v)
    if n is None or n <= 0:
        return None
    # Microsecond rounding, as the Rust side: f64 epoch seconds hold ~1e-7.
    return round(n * 1e6) * 1_000


def report_timestamp_ns(report: dict) -> int | None:
    """The gNB's timestamp for a report: top level, else the first cell's."""
    if "timestamp" in report:
        return _ts_ns(report["timestamp"])
    cells = report.get("cells")
    if isinstance(cells, list) and cells and isinstance(cells[0], dict):
        return _ts_ns(cells[0].get("timestamp"))
    return None


def _fields(
    obj: dict, table: dict[str, tuple[str, str, float]], scope: str
) -> Iterable[tuple[str, float, str]]:
    """(metric, value, unit) for every measurement in one UE or cell object."""
    for key, raw in obj.items():
        if key in _IDENTITY:
            continue
        name, unit, scale = table.get(key, (f"{scope}.raw.{key}", "", 1.0))
        n = _number(raw)
        if n is not None:
            yield name, n * scale, unit
        elif isinstance(raw, dict):
            for sub, v in raw.items():
                n = _number(v)
                if n is not None:
                    yield f"{name}_{sub}", n * scale, unit
        elif isinstance(raw, list) and raw and all(_number(x) is not None for x in raw):
            yield f"{name}_mean", sum(float(x) for x in raw) / len(raw) * scale, unit


def _rnti(ue: dict) -> str:
    r = ue.get("rnti")
    return str(int(r)) if _number(r) is not None else ""


def _pci(obj: dict, default: str = "") -> str:
    p = obj.get("pci")
    return str(int(p)) if _number(p) is not None else default


def normalise_json(report: dict) -> list[RanSample]:
    """One srsRAN JSON metrics report -> rows. ``recv_ns`` is left 0."""
    if not isinstance(report, dict):
        return []
    ts = report_timestamp_ns(report) or 0
    rows: list[RanSample] = []

    def add(cell: str, ue: str, metric: str, value: float, unit: str, t: int) -> None:
        rows.append(RanSample("json", t, cell, ue, metric, value, unit))

    def ue_rows(entries: Any, cell_default: str, t: int) -> None:
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            ue = entry.get("ue_container", entry)
            if not isinstance(ue, dict):
                continue
            cell, rnti = _pci(ue, cell_default), _rnti(ue)
            for metric, value, unit in _fields(ue, JSON_UE, "ue"):
                add(cell, rnti, metric, value, unit, t)

    # 25.x layout
    for c in report.get("cells") or []:
        if not isinstance(c, dict):
            continue
        t = _ts_ns(c.get("timestamp")) or ts
        cm = c.get("cell_metrics") if isinstance(c.get("cell_metrics"), dict) else {}
        cell = _pci(cm)
        for metric, value, unit in _fields(cm, JSON_CELL, "cell"):
            add(cell, "", metric, value, unit, t)
        for ev in c.get("event_list") or []:
            if isinstance(ev, dict) and isinstance(ev.get("event_type"), str):
                add(cell, _rnti(ev), f"event.{ev['event_type']}", 1.0, "count", t)
        ue_rows(c.get("ue_list"), cell, t)

    # <= 24.x layout
    if isinstance(report.get("cell_metrics"), dict):
        cm = report["cell_metrics"]
        cell = _pci(cm)
        for metric, value, unit in _fields(cm, JSON_CELL, "cell"):
            add(cell, "", metric, value, unit, ts)
    if "ue_list" in report:
        default = (
            _pci(report["cell_metrics"]) if isinstance(report.get("cell_metrics"), dict) else ""
        )
        ue_rows(report["ue_list"], default, ts)

    rows.sort(key=lambda r: (r.cell, r.ue, r.metric))
    return rows


# --- E2SM-KPM -------------------------------------------------------------

#: O-RAN KPM measurement -> (canonical metric, unit, scale). TS 28.552 units:
#: throughput in kbit/s, volumes in kbit. srsRAN's agent is to be verified
#: against those in the lab (WF_RAN_SOURCES_DISAGREE exists to catch it).
KPM: dict[str, tuple[str, str, float]] = {
    "DRB.UEThpDl": ("dl_throughput_bps", "bit/s", 1000.0),
    "DRB.UEThpUl": ("ul_throughput_bps", "bit/s", 1000.0),
    "DRB.RlcSduTransmittedVolumeDL": ("dl_rlc_sdu_volume_bits", "bit", 1000.0),
    "DRB.RlcSduTransmittedVolumeUL": ("ul_rlc_sdu_volume_bits", "bit", 1000.0),
    "DRB.RlcPacketDropRateDl": ("dl_rlc_drop_rate", "", 1.0),
    "DRB.PacketSuccessRateUlgNBUu": ("ul_packet_success_rate", "", 1.0),
    # srsRAN documents these three as dummy values to be removed. Kept, under
    # a name nobody will mistake for a measurement.
    "CQI": ("kpm_dummy.cqi", "", 1.0),
    "RSRP": ("kpm_dummy.rsrp", "", 1.0),
    "RSRQ": ("kpm_dummy.rsrq", "", 1.0),
}


def _kpm_rows(
    out: list[RanSample], cell: str, ue: str, scope: str, meas: dict, t0: int, granul_ms: int
) -> None:
    for name, values in (meas or {}).items():
        canon, unit, scale = KPM.get(name, (f"raw.{name}", "", 1.0))
        metric = f"{scope}.{canon}"
        seq = values if isinstance(values, list) else [values]
        for i, v in enumerate(seq):
            n = _number(v)
            if n is None:
                continue
            # One value per granularity period, oldest first.
            t = t0 + i * granul_ms * 1_000_000 if t0 else 0
            out.append(RanSample("kpm", t, cell, ue, metric, n * scale, unit))


def normalise_kpm(indication: dict) -> list[RanSample]:
    """One decoded E2SM-KPM indication -> rows.

    ``indication`` is what ``mec_cast_xapp`` builds from a RIC indication::

        {"e2_node_id": "gnbd_001_001_00019b_0",
         "collect_start_ns": 1762271486000000000,   # header colletStartTime
         "meas": <extract_meas_data() result>}

    where ``meas`` is ``{"measData": {name: [v, ...]}, "granulPeriod": ms}``
    (report styles 1 and 2: node level) or
    ``{"ueMeasData": {ue_id: {"measData": {...}, "granulPeriod": ms}}}``
    (styles 3-5: per UE). Node-level rows are ``cell.*``, per-UE rows ``ue.*``
    with ``ue = "e2:<id>"`` — an E2 UE id, not an RNTI; mapping the two is the
    identity step of ran/schema/metrics.md.
    """
    cell = str(indication.get("e2_node_id") or "")
    t0 = int(indication.get("collect_start_ns") or 0)
    meas = indication.get("meas") or {}
    out: list[RanSample] = []
    granul = int(meas.get("granulPeriod") or 0)
    if isinstance(meas.get("measData"), dict):
        ue = indication.get("ue_id")
        if ue is not None:  # style 2: one UE, named by the subscription
            _kpm_rows(out, cell, f"e2:{ue}", "ue", meas["measData"], t0, granul)
        else:
            _kpm_rows(out, cell, "", "cell", meas["measData"], t0, granul)
    for ue_id, ue_meas in (meas.get("ueMeasData") or {}).items():
        if isinstance(ue_meas, dict):
            g = int(ue_meas.get("granulPeriod") or granul)
            _kpm_rows(out, cell, f"e2:{ue_id}", "ue", ue_meas.get("measData") or {}, t0, g)
    out.sort(key=lambda r: (r.cell, r.ue, r.metric, r.gnb_ts_ns))
    return out

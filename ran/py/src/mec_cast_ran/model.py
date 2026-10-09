"""The row both RAN sources are normalised into."""

from __future__ import annotations

from dataclasses import asdict, dataclass

#: Column order of ``runs/<run_id>/<leaf>/kpi.csv``, identical for every source.
KPI_CSV_HEADER = ("source", "gnb_ts_ns", "recv_ns", "cell", "ue", "metric", "value", "unit")


@dataclass(frozen=True)
class RanSample:
    """One metric, for one cell or UE, at one gNB timestamp.

    * ``source`` — ``json`` (srsRAN metrics) or ``kpm`` (E2SM-KPM).
    * ``gnb_ts_ns`` — the RAN's own timestamp for the measurement, 0 if none.
    * ``recv_ns`` — when the collector/xApp received it; filled at record time.
    * ``cell`` — PCI (json) or E2 node id (kpm).
    * ``ue`` — RNTI (json), ``e2:<gNB-CU-UE-F1AP-ID>`` (kpm), "" for cell rows.
    * ``metric`` — canonical name from ran/schema/metrics.md.
    * ``unit`` — canonical unit, or "" where srsRAN does not document one.
    """

    source: str
    gnb_ts_ns: int
    cell: str
    ue: str
    metric: str
    value: float
    unit: str
    recv_ns: int = 0

    def as_dict(self) -> dict:
        return asdict(self)

    def csv_row(self) -> list:
        value = int(self.value) if float(self.value).is_integer() else self.value
        return [
            self.source,
            self.gnb_ts_ns,
            self.recv_ns,
            self.cell,
            self.ue,
            self.metric,
            value,
            self.unit,
        ]

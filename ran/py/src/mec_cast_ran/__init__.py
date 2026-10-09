"""One RAN data model for mec-cast (ADR-0010).

Two sources describe the RAN: srsRAN's JSON metrics (the deep, vendor-specific
tap in ``ran/collector``) and E2SM-KPM indications (the standard path, in
``ran/xapp``). Both are normalised into :class:`RanSample` rows with canonical
metric names and units, so analysis never has to know which produced a row.

The catalogue of names and units is ``ran/schema/metrics.md``. The contract
between this implementation and the Rust one in ``ran/collector`` is
``ran/schema/vectors.json``: both are tested against it.
"""

from .model import KPI_CSV_HEADER, RanSample
from .normalise import normalise_json, normalise_kpm, report_timestamp_ns

__all__ = [
    "KPI_CSV_HEADER",
    "RanSample",
    "normalise_json",
    "normalise_kpm",
    "report_timestamp_ns",
]

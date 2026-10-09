"""The mec-cast E2 xApp (ADR-0010).

A RIC-agnostic core (:mod:`.core`) runs *capabilities* — KPM monitoring, RC
control, and whatever comes next — over an :class:`~.core.E2Port`. Adapters
implement the port: ``osc`` on the O-RAN SC RIC (inside its xApp runner), and
``sim`` against ``gnb-sim`` for testing without a radio.
"""

__version__ = "0.1.0"

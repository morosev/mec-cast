"""The FlexRIC adapter against a fake mec-cast-e2-bridge (tests/fake_bridge.py).

The real bridge needs FlexRIC and a running nearRT-RIC; it is exercised by
`flexric.sh emu` and in the lab. This covers everything on this side of the
pipe: the protocol, a refusal, an ack and a failure, the slice on a control,
the abort FlexRIC does on a refused subscription, and a restart.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

from mec_cast_xapp.adapters.flexric import FlexricPort, parse_slice
from mec_cast_xapp.core import SubscriptionRejected

FAKE = [sys.executable, str(Path(__file__).with_name("fake_bridge.py"))]


def wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def port():
    p = FlexricPort(command=FAKE, timeout_s=3.0)
    yield p
    p.close()


def test_nodes_and_per_ue_indications(port):
    assert port.connected_nodes() == ["gnbd_001_01_19b"]
    got = []
    handle = port.subscribe_kpm("gnbd_001_01_19b", 4, ["DRB.UEThpUl"], [0], 1000, got.append)
    assert wait_for(lambda: len(got) >= 2)
    assert got[0]["meas"]["ueMeasData"]["0"]["measData"]["DRB.UEThpUl"][0] > 5000
    port.unsubscribe(handle)


def test_a_refused_subscription_raises_rejected(port):
    with pytest.raises(SubscriptionRejected, match=r"does not offer DRB\.Unsupported"):
        port.subscribe_kpm("gnbd_001_01_19b", 4, ["DRB.Unsupported"], [0], 1000, lambda _: None)


def test_controls_carry_the_slice_and_report_ack_and_failure():
    port = FlexricPort(command=FAKE, slice_spec="00102:2:0x0000ff")
    try:
        answers = []
        done = threading.Event()

        def cb(ok, detail):
            answers.append((ok, detail))
            if len(answers) == 2:
                done.set()

        port.control_prb_quota("gnbd_001_01_19b", 0, 0, 30, 0, cb)
        port.control_prb_quota("gnbd_001_01_19b", 0, 0, 13, 0, cb)  # the fake refuses 13
        assert done.wait(5)
        assert answers[0] == (True, "")
        assert answers[1][0] is False and "slice=('00102', 2, 255)" in answers[1][1]
    finally:
        port.close()


def test_flexrics_abort_on_a_refused_subscription_is_a_refusal(monkeypatch):
    monkeypatch.setenv("FAKE_BRIDGE_ABORT", "1")
    port = FlexricPort(command=FAKE, timeout_s=5.0)
    try:
        t0 = time.monotonic()
        with pytest.raises(SubscriptionRejected, match="FlexRIC aborts"):
            port.subscribe_kpm("gnbd_001_01_19b", 4, ["DRB.UEThpUl"], [0], 1000, lambda _: None)
        assert time.monotonic() - t0 < 4, "answered when the bridge died, not at the timeout"
    finally:
        port.close()


def test_a_crashed_bridge_is_restarted_and_resubscribed(port):
    got = []
    port.subscribe_kpm("gnbd_001_01_19b", 4, ["DRB.UEThpUl"], [0], 1000, got.append)
    assert wait_for(lambda: got)
    port._proc.kill()
    port._proc.wait()
    n = len(got)
    assert port.connected_nodes() == ["gnbd_001_01_19b"]  # any request restarts it
    assert wait_for(lambda: len(got) > n + 2), "indications resume after the restart"


def test_slice_parsing():
    assert parse_slice("00101:1:1") == {"plmn": "00101", "sst": 1, "sd": 1}
    assert parse_slice("001001:1:0xffffff") == {"plmn": "001001", "sst": 1, "sd": 0xFFFFFF}
    with pytest.raises(ValueError):
        parse_slice("0010:1:1")

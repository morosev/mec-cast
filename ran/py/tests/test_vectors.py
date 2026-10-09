"""ran/schema/vectors.json is the contract; the Rust normaliser reads it too."""

import json
import math
import pathlib

import pytest

from mec_cast_ran import normalise_json, normalise_kpm

VECTORS = json.loads(
    (pathlib.Path(__file__).resolve().parents[2] / "schema" / "vectors.json").read_text()
)


def keyed(rows):
    out = {}
    for r in rows:
        key = (r["cell"], r["ue"], r["metric"], r["gnb_ts_ns"])
        assert key not in out, f"duplicate row {key}"
        out[key] = r
    return out


def check(actual, expected):
    got = keyed([{k: v for k, v in r.as_dict().items() if k != "recv_ns"} for r in actual])
    want = keyed(expected)
    assert set(got) == set(want), (
        f"missing {sorted(set(want) - set(got))}, unexpected {sorted(set(got) - set(want))}"
    )
    for key, w in want.items():
        g = got[key]
        assert math.isclose(g["value"], w["value"], rel_tol=1e-9, abs_tol=1e-9), (key, g, w)
        assert g["unit"] == w["unit"], key
        assert g["source"] == w["source"], key


@pytest.mark.parametrize("case", VECTORS["json"], ids=lambda c: c["name"])
def test_json_vectors(case):
    check(normalise_json(case["input"]), case["expected"])


@pytest.mark.parametrize("case", VECTORS["kpm"], ids=lambda c: c["name"])
def test_kpm_vectors(case):
    check(normalise_kpm(case["input"]), case["expected"])


def test_non_objects_produce_nothing():
    assert normalise_json([1, 2]) == []
    assert normalise_kpm({}) == []


def test_every_fixture_normalises_to_ue_rows():
    """Every collector fixture yields per-UE rows, whichever layout it uses."""
    testdata = pathlib.Path(__file__).resolve().parents[2] / "collector" / "testdata"
    for path in sorted(testdata.glob("*.jsonl")):
        ue_rows = [
            r
            for line in path.read_text().splitlines()
            if line.strip()
            for r in normalise_json(json.loads(line))
            if r.ue
        ]
        assert ue_rows, f"{path.name}: no per-UE rows"
        assert any(r.metric == "ue.dl_throughput_bps" for r in ue_rows), path.name

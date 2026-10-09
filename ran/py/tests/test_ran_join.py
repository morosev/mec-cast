"""tools/ran_join.py: the as-of join, the staleness cut-off and the UE choice."""

import csv
import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("ran_join", REPO / "tools" / "ran_join.py")
ran_join = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ran_join)

S = 1_000_000_000


def write(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def make_run(tmp_path):
    run = tmp_path / "run"
    write(
        run / "ran" / "kpi.csv",
        ["source", "gnb_ts_ns", "recv_ns", "cell", "ue", "metric", "value", "unit"],
        [
            ["json", 10 * S, 0, "1", "7", "ue.dl_mcs", 20, "index"],
            ["json", 11 * S, 0, "1", "7", "ue.dl_mcs", 25, "index"],
            ["json", 10 * S, 0, "1", "7", "ue.ul_throughput_bps", 9e6, "bit/s"],
            ["json", 10 * S, 0, "1", "8", "ue.ul_throughput_bps", 1e6, "bit/s"],
            ["json", 10 * S, 0, "1", "", "cell.late_dl_harqs", 3, "count"],
        ],
    )
    write(
        run / "edge-0" / "samples.csv",
        ["seq", "kind", "capture_ns", "recv_ns", "network_ns", "e2e_ns"],
        [
            [0, "frame", 9 * S, 9 * S + 1, 5, 9],  # before any RAN sample
            [1, "frame", 10 * S + S // 2, 0, 6, 10],  # sees the 10 s sample
            [2, "frame", 11 * S + 1, 0, 7, 11],  # sees the 11 s sample
            [3, "frame", 20 * S, 0, 8, 12],  # long after: too stale
        ],
    )
    return run


def test_as_of_join_staleness_and_ue_choice(tmp_path, capsys):
    run = make_run(tmp_path)
    out = tmp_path / "joined.csv"
    assert ran_join.main([str(run), "-o", str(out)]) == 0
    err = capsys.readouterr().err
    assert "UE: 7" in err and "most uplink" in err  # two UEs: the uplink-heavy one
    rows = {r["seq"]: r for r in csv.DictReader(out.open())}
    assert rows["0"]["ue.dl_mcs"] == ""
    assert rows["1"]["ue.dl_mcs"] == "20.0"
    assert rows["1"]["cell.late_dl_harqs"] == "3.0"  # the UE's cell rows join too
    assert rows["2"]["ue.dl_mcs"] == "25.0"
    assert rows["3"]["ue.dl_mcs"] == ""  # older than 2x the 1 s period


def test_an_explicit_ue_wins(tmp_path, capsys):
    run = make_run(tmp_path)
    assert ran_join.main([str(run), "--ue", "8"]) == 0
    assert "8" in capsys.readouterr().out

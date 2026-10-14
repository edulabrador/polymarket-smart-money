"""Checks del backtest por replay del historial de git y de las metricas
(sin red). `python tests/test_backtest.py` o `pytest`."""
import json
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import metrics  # noqa: E402
from backtest import (alertable_snapshot, evaluate, group_stats, replay,  # noqa: E402
                      snapshots)


def sig(id_, cur, stale=False, n=5, tier=None, **kw):
    s = {"id": id_, "curPrice": cur, "stale": stale, "numTraders": n,
         "title": f"Mercado {id_}", "outcome": "Yes"}
    if tier:
        s["tier"] = tier
    s.update(kw)
    return s


def test_replay_alerts_once_at_first_fresh_snapshot():
    snaps = [
        (100, {"signals": [sig("0xa:0", 0.80, stale=True)]}),   # nace descartada
        (200, {"signals": [sig("0xa:0", 0.55)]}),               # vuelve: se alerta AQUI
        (300, {"signals": [sig("0xa:0", 0.60)]}),               # precio con retraso
        (400, {"signals": [sig("0xa:0", 0.40)]}),               # no se re-alerta
        (500, {"signals": [sig("0xb:1", 0.95, n=3, tier="especialistas")]}),  # favorito
    ]
    rows = {r["id"]: r for r in replay(snaps)}
    a = rows["0xa:0"]
    assert a["detectPrice"] == 0.80 and a["staleAtDetection"]
    assert a["alerted"] and a["alertTs"] == 200 and a["alertPrice"] == 0.55
    assert a["alertPriceDelayed"] == 0.60 and a["tierAtAlert"] == "amplio"
    assert a["lastTs"] == 400 and a["lastPrice"] == 0.40
    b = rows["0xb:1"]
    assert not b["alerted"] and b["outcomeIndex"] == 1 and b["tierAtDetection"] == "especialistas"


def test_evaluate_uses_oracle_and_costs():
    rows = replay([(100, {"signals": [sig("0xa:0", 0.5)]}),
                   (200, {"signals": [sig("0xl:1", 0.4)]}),     # 0xa ya desaparecio
                   (300, {"signals": [sig("0xp:0", 0.3)]})])
    # 0xa gana, 0xl pierde (sus traders se fueron antes: da igual, cuenta), 0xp pendiente
    evaluate(rows, {"0xa": [1.0, 0.0], "0xl": [1.0, 0.0]}, cost=0.01)
    by = {r["id"]: r for r in rows}
    assert by["0xa:0"]["entryPrice"] == 0.51 and by["0xa:0"]["roi"] == round(1 / 0.51 - 1, 4)
    assert by["0xl:1"]["won"] is False and by["0xl:1"]["roi"] == -1.0
    assert by["0xp:0"]["status"] == "pendiente" and "roi" not in by["0xp:0"]
    groups, al = group_stats(rows)
    assert groups["alertadas"]["n"] == 2 and groups["alertadas"]["wins"] == 1
    assert groups["detectadas"]["n"] == 2
    assert len(al) == 2 and al[0]["alertedAt"].startswith("1970")


def test_alertable_snapshot_rules():
    assert alertable_snapshot(sig("x", 0.5))
    assert not alertable_snapshot(sig("x", 0.5, stale=True))
    assert not alertable_snapshot(sig("x", 0.9))
    assert alertable_snapshot(sig("x", 0.5, edgeEst=0.05), min_edge=0.02)
    assert not alertable_snapshot(sig("x", 0.5, edgeEst=0.01), min_edge=0.02)
    # versiones antiguas sin edgeEst: el filtro opcional no las descarta
    assert alertable_snapshot(sig("x", 0.5), min_edge=0.02)


def test_snapshots_reads_git_history():
    tmp = pathlib.Path(tempfile.mkdtemp())
    run = lambda *a: subprocess.run(["git", "-C", str(tmp), *a], check=True,
                                    capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (tmp / "docs").mkdir()
    for i in range(3):
        (tmp / "docs" / "signals.json").write_text(json.dumps({"signals": [sig(f"0x{i}:0", 0.5)]}))
        run("add", ".")
        run("commit", "-q", "-m", f"v{i}")
    snaps = list(snapshots(tmp))
    assert [d["signals"][0]["id"] for _, d in snaps] == ["0x0:0", "0x1:0", "0x2:0"]


def test_summarize_and_verdict():
    rows = [{"roi": 1.0, "won": True, "entryPrice": 0.5}] * 3 + \
           [{"roi": -1.0, "won": False, "entryPrice": 0.5}]
    s = metrics.summarize(rows)
    assert s["n"] == 4 and s["wins"] == 3 and s["hitRate"] == 0.75
    assert s["avgEntry"] == 0.5 and s["avgRoi"] == 0.5 and s["medianRoi"] == 1.0
    assert s["ci95"][0] < 0.5 < s["ci95"][1]
    assert metrics.verdict(s, min_sample=20) == "sin muestra"
    assert metrics.verdict(s, min_sample=4) == "no concluyente"  # IC incluye el 0
    assert metrics.summarize([])["n"] == 0
    assert metrics.roi(1.0, 0.25) == 3.0 and metrics.roi(0.0, 0.7) == -1.0


def test_paper_ledger_drawdown_and_capital():
    rows = [
        {"roi": 1.0, "alertedAt": "2026-01-01T00:00:00+00:00", "resolvedAt": "2026-01-05T00:00:00+00:00"},
        {"roi": -1.0, "alertedAt": "2026-01-02T00:00:00+00:00", "resolvedAt": "2026-01-03T00:00:00+00:00"},
        {"roi": -1.0, "alertedAt": "2026-01-02T12:00:00+00:00", "resolvedAt": "2026-01-04T00:00:00+00:00"},
        {"roi": None, "alertedAt": "2026-01-01T00:00:00+00:00", "resolvedAt": "2026-01-02T00:00:00+00:00"},
    ]
    p = metrics.paper_ledger(rows, stake=100)
    # orden de resolucion: -100, -100, +100 -> PnL -100, caida maxima 200
    assert p["bets"] == 3 and p["pnl"] == -100.0 and p["maxDrawdown"] == 200.0
    # del 2 al 3 de enero hay 3 apuestas abiertas a la vez -> $300 inmovilizados
    assert p["maxOpen"] == 3 and p["capitalNeeded"] == 300.0
    assert [c[1] for c in p["curve"]] == [-100.0, -200.0, -100.0]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"OK {name}")

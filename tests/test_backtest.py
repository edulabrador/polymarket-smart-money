"""Checks del backtest por replay del historial de git y de las metricas
(sin red). `python tests/test_backtest.py` o `pytest`."""
import json
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import metrics  # noqa: E402
from backtest import (alertable_snapshot, evaluate, group_stats, load_series,  # noqa: E402
                      replay, snapshots, walk_forward)


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


def test_evaluate_uses_gamma_close_time():
    rows = replay([(100, {"signals": [sig("0xa:0", 0.5, eventSlug="ev")]})])
    evaluate(rows, {"0xa": {"payouts": [1.0, 0.0], "closedAt": "1970-01-02T00:00:00+00:00"}})
    _, al = group_stats(rows)
    assert al[0]["resolvedAt"] == "1970-01-02T00:00:00+00:00"   # cierre real, no lastTs


def test_walk_forward_measures_out_of_sample():
    # 1a mitad: las baratas (0.4) ganan y las caras (0.85) pierden; 2a mitad,
    # al reves. El ajuste elige "solo baratas" (max_price 0.7/0.8) porque ahi
    # gano; en la prueba esa regla pierde: es lo que el metodo debe destapar.
    snaps, res = [], {}
    for i in range(120):
        primera = i < 60
        cara = i % 2 == 0
        cid = f"0x{i}"
        snaps.append((i * 100, {"signals": [sig(f"{cid}:0", 0.85 if cara else 0.4, n=5,
                                                avgEntryPrice=0.85 if cara else 0.4,
                                                eventSlug=f"ev{i}")]}))
        gana = (not cara) == primera
        res[cid] = [1.0, 0.0] if gana else [0.0, 1.0]
    wf = walk_forward(load_series(snaps), res, split_ts=6000)
    e = wf["elegida"]
    assert wf["combinaciones"] == 135 and e["rules"]["max_price"] < 0.85
    assert e["train"]["avgRoi"] > 1 and e["test"]["avgRoi"] == -1.0   # se cae fuera de muestra
    assert wf["reglasActuales"]["test"]["n"] == 60


def test_groups_by_game_phase():
    rows = replay([(1000, {"signals": [sig("0xa:0", 0.5, eventSlug="e1")]}),
                   (1000, {"signals": [sig("0xb:0", 0.5, eventSlug="e2")]}),
                   (1000, {"signals": [sig("0xc:0", 0.5, eventSlug="e3")]})])
    evaluate(rows, {
        "0xa": {"payouts": [1.0, 0.0], "gameStart": "1970-01-01T01:00:00+00:00"},  # empieza despues
        "0xb": {"payouts": [0.0, 1.0], "gameStart": "1970-01-01T00:00:01+00:00"},  # ya empezado
        "0xc": {"payouts": [1.0, 0.0]}})                                          # sin hora
    g, _ = group_stats(rows)
    assert g["alertadas:previa"]["n"] == 1 and g["alertadas:previa"]["wins"] == 1
    assert g["alertadas:directo"]["n"] == 1 and g["alertadas:directo"]["wins"] == 0
    assert g["alertadas:sin_hora"]["n"] == 1


def test_alertable_snapshot_rules():
    assert alertable_snapshot(sig("x", 0.5))
    assert not alertable_snapshot(sig("x", 0.5, stale=True))
    assert not alertable_snapshot(sig("x", 0.9))
    assert alertable_snapshot(sig("x", 0.5, edgeEst=0.05), min_edge=0.02)
    assert not alertable_snapshot(sig("x", 0.5, edgeEst=0.01), min_edge=0.02)
    # versiones antiguas sin edgeEst: el filtro opcional no las descarta
    assert alertable_snapshot(sig("x", 0.5), min_edge=0.02)
    # parametros de la rejilla: deriva recalculada, minimo de traders, niveles
    s = sig("x", 0.5, avgEntryPrice=0.3)
    assert not alertable_snapshot(s, max_drift=0.1) and alertable_snapshot(s, max_drift=0.25)
    assert not alertable_snapshot(sig("x", 0.5, n=3), min_traders=4)
    assert not alertable_snapshot(sig("x", 0.5, n=3, tier="especialistas"), tiers={"amplio"})


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


def test_ci_is_robust_to_same_event_bets():
    # 10 apuestas en 10 eventos distintos vs las mismas en solo 2 eventos:
    # misma media, pero el IC por evento es mas ancho (menos informacion real)
    rows = [{"roi": 1.0 if i % 2 else -1.0, "won": bool(i % 2), "entryPrice": 0.5,
             "eventSlug": f"ev{i}"} for i in range(10)]
    indep = metrics.summarize(rows)
    juntas = metrics.summarize([dict(r, eventSlug=f"ev{i % 2}") for i, r in enumerate(rows)])
    assert indep["avgRoi"] == juntas["avgRoi"] == 0.0
    assert indep["events"] == 10 and juntas["events"] == 2
    assert (juntas["ci95"][1] - juntas["ci95"][0]) > (indep["ci95"][1] - indep["ci95"][0])
    # un solo evento: no hay intervalo posible
    assert metrics.summarize([dict(r, eventSlug="uno") for r in rows])["ci95"] is None


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

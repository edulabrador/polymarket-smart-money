"""Checks del backtest en dos niveles (sin red: funciones puras)."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from backtest import (_winrate, avg_proven_winrate, classify,  # noqa: E402
                      coincidences, wallet_records)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def redeem(cond, usd=1000, title="Neutral market", ts=1000):
    return {"type": "REDEEM", "conditionId": cond, "title": title,
            "usdcSize": usd, "timestamp": ts}


def losing_pos(cond, idx=1, usd=1000, title="Neutral market"):
    return {"conditionId": cond, "outcomeIndex": idx, "outcome": "No",
            "title": title, "redeemable": True, "curPrice": 0.0,
            "initialValue": usd, "endDate": "2026-01-01"}


def test_winrate_leave_one_out():
    # 5 aciertos, pero uno es el propio mercado puntuado (cW): al excluirlo
    # quedan 4 < WHALE_MIN_TRACK(5) -> None. Sin excluir -> 1.0.
    rec = {"A": {"wins": {c: "otras" for c in
                          ("cW", "w1", "w2", "w3", "w4")}, "losses": {}}}
    assert _winrate(rec, "A", None, "cW") is None
    assert _winrate(rec, "A", None, None) == 1.0
    # la categoria filtra: solo cuentan mercados de esa categoria
    rec["A"]["wins"]["d1"] = "deportes"
    assert _winrate(rec, "A", "deportes", None) is None  # 1 solo < 5


def test_avg_prefers_category_over_global():
    rec = {
        "A": {"wins": {**{f"g{i}": "otras" for i in range(5)},
                       **{f"d{i}": "deportes" for i in range(5)}},
              "losses": {}},
    }
    # con muestra en deportes, manda deportes (1.0), no el global
    assert avg_proven_winrate(rec, ["A"], "deportes", "x") == 1.0


def _specialist_redeems(wallet, n=5):
    # 5 aciertos propios (distintos por wallet, no forman coincidencia) + cW
    return [redeem(f"{wallet}_f{i}") for i in range(n)] + [redeem("cW")]


def test_classify_three_tiers():
    redeems = {
        # 3 especialistas coinciden en cW (ganadora); cada uno con 5 aciertos propios
        "A": _specialist_redeems("A"), "B": _specialist_redeems("B"),
        "C": _specialist_redeems("C"),
        # 5 wallets coinciden en cB (ganadora): consenso amplio, sin historial
        "P1": [redeem("cB")], "P2": [redeem("cB")], "P3": [redeem("cB")],
        "P4": [redeem("cB")], "P5": [redeem("cB")],
    }
    positions = {  # 3 wallets sin historial atrapados en la perdedora cX
        "F": [losing_pos("cX")], "G": [losing_pos("cX")], "H": [losing_pos("cX")],
    }
    rec = wallet_records(redeems, positions, since_ts=0)
    groups = classify(coincidences(redeems, positions, since_ts=0), rec)
    by_tier = {g["tier"]: g for g in groups}
    assert by_tier["amplio"]["cond"] == "cB" and by_tier["amplio"]["won"]
    assert by_tier["especialistas"]["cond"] == "cW"
    assert by_tier["especialistas"]["won"] and by_tier["especialistas"]["avgWinRate"] == 1.0
    # 3 traders sin acierto probado en la perdedora: el algoritmo NO la dispara
    assert by_tier["descartada"]["cond"] == "cX" and not by_tier["descartada"]["won"]


def test_coincidences_mixes_redeems_and_dead_positions():
    # ganadas = canjes REDEEM; perdidas = posiciones muertas que nadie canjea
    canje = {"type": "REDEEM", "conditionId": "0xwin", "timestamp": 100,
             "usdcSize": 900.0, "title": "Ganado", "outcome": ""}
    redeems = {f"0xw{i}": [canje] for i in range(5)}
    perdida = {"conditionId": "0xl", "outcomeIndex": 0, "outcome": "No",
               "title": "Perdido", "redeemable": True, "curPrice": 0.0,
               "initialValue": 800, "endDate": "2026-01-01"}
    positions = {f"0xl{i}": [perdida] for i in range(5)}
    # ruido que no cuenta: canje pequeno, fuera de ventana, posicion viva,
    # ganadora sin canjear (contara cuando se canjee)
    redeems["0xmini"] = [dict(canje, usdcSize=10)]
    redeems["0xviejo"] = [dict(canje, timestamp=1)]
    positions["0xviva"] = [{"conditionId": "0xv", "outcomeIndex": 0, "redeemable": False,
                            "curPrice": 0.5, "initialValue": 9999, "endDate": "2026-01-01",
                            "outcome": "Yes", "title": "Viva"}]
    positions["0xsin"] = [{"conditionId": "0xs", "outcomeIndex": 0, "redeemable": True,
                           "curPrice": 1.0, "initialValue": 9999, "endDate": "2026-01-01",
                           "outcome": "Yes", "title": "Sin canjear"}]
    groups = coincidences(redeems, positions, min_users=5, since_ts=50)
    assert {g["cond"]: g["won"] for g in groups} == {"0xwin": True, "0xl": False}


def test_coincidences_parses_real_activity_fixture():
    activity = json.loads((FIXTURES / "activity.json").read_text(encoding="utf-8"))
    redeems = {f"0xr{i}": activity for i in range(5)}
    groups = coincidences(redeems, {}, min_users=5, since_ts=0)
    grandes = {e["conditionId"] for e in activity
               if e.get("type") == "REDEEM" and e["usdcSize"] >= 500}
    assert len(groups) == len(grandes)
    assert all(g["won"] and len(g["wallets"]) == 5 for g in groups)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("OK", fn.__name__)
    print(f"{len(fns)} checks del backtest OK")

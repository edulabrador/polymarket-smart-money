"""Checks del banco de pruebas de estrategias (sin red).
`python tests/test_strategies.py` o `pytest`."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import metrics  # noqa: E402
import strategies as st  # noqa: E402
from scanner import source_stats, wallet_entry_stats  # noqa: E402

NOW = "2026-10-14T10:00:00+00:00"


def pos(cond, price, end="2026-10-16", idx=0, title="Mercado", outcome="Yes", **kw):
    p = {"conditionId": cond, "outcomeIndex": idx, "outcome": outcome, "title": title,
         "eventSlug": f"ev-{cond}", "endDate": end, "curPrice": price, "redeemable": False,
         "oppositeOutcome": "No" if outcome == "Yes" else "Yes"}
    p.update(kw)
    return p


def test_universe_one_per_market():
    datos = {"a": {"positions": [pos("0x1", 0.5), pos("0x2", 0.4, redeemable=True)]},
             "b": {"positions": [pos("0x1", 0.5, idx=1, outcome="No")]}}
    u = st.universe(datos)
    assert list(u) == ["0x1"] and u["0x1"]["outcomeIndex"] == 0


def test_price_bias_records_each_market_once_by_bucket():
    u = {"0xf": pos("0xf", 0.95), "0xl": pos("0xl", 0.05), "0xm": pos("0xm", 0.5),
         "0xlejos": pos("0xlejos", 0.5, end="2027-01-01"),       # largo plazo: no
         "0xcasi": pos("0xcasi", 0.995)}                         # fuera de tramos
    seen = {}
    bets = st.price_bias_bets(u, seen, NOW, entry_cost=0.01)
    tags = {b["conditionId"]: b["tag"] for b in bets}
    assert tags == {"0xf": "0.90-0.99", "0xl": "0.01-0.10", "0xm": "0.30-0.70"}
    assert all(b["source"] == "sesgo" for b in bets)
    assert next(b for b in bets if b["conditionId"] == "0xf")["entryPrice"] == 0.96
    # ya vistos: no se repiten
    assert st.price_bias_bets(u, seen, NOW, entry_cost=0.01) == []


def test_same_team_avoids_false_matches():
    assert st.same_team("Atletico Madrid", "Club Atlético de Madrid")
    assert st.same_team("Arsenal", "Arsenal FC")
    assert not st.same_team("Manchester City", "Manchester United")
    assert not st.same_team("Real Madrid", "Atletico Madrid")


def evento(home, away, odds, book="pinnacle", when="2026-10-15T19:00:00Z"):
    return {"home_team": home, "away_team": away, "commence_time": when,
            "bookmakers": [{"key": book, "markets": [{"key": "h2h", "outcomes": [
                {"name": n, "price": o} for n, o in odds.items()]}]}]}


def test_fair_probs_removes_margin_and_prefers_order():
    ev = evento("Arsenal", "Chelsea", {"Arsenal": 2.0, "Chelsea": 4.0, "Draw": 4.0})
    probs, book = st.fair_probs(ev)
    assert book == "pinnacle" and abs(sum(probs.values()) - 1) < 1e-9
    assert round(probs["Arsenal"], 4) == 0.5            # 0.5 / (0.5+0.25+0.25)
    ev2 = evento("A", "B", {"A": 1.5, "B": 3.0}, book="otra")
    assert st.fair_probs(ev2) == (None, None)            # casa no admitida


def test_match_market_formats():
    ev = evento("Arsenal", "Chelsea", {"Arsenal": 2.0, "Chelsea": 4.0, "Draw": 4.0})
    gana = pos("0xa", 0.4, title="Will Arsenal FC win on 2026-10-15?", outcome="Yes")
    fair, book, _ = st.match_market(gana, [ev])
    assert round(fair, 4) == 0.5
    no = pos("0xa", 0.6, title="Will Arsenal FC win on 2026-10-15?", outcome="No", idx=1)
    assert round(st.match_market(no, [ev])[0], 4) == 0.5   # No = 1 - P(gana)
    otro_dia = pos("0xa", 0.4, title="Will Arsenal FC win on 2026-11-30?")
    assert st.match_market(otro_dia, [ev]) is None
    nba = evento("Boston Celtics", "Miami Heat", {"Boston Celtics": 1.5, "Miami Heat": 3.0})
    vs = pos("0xn", 0.55, title="Celtics vs. Heat", outcome="Celtics")
    assert round(st.match_market(vs, [nba])[0], 4) == round((1 / 1.5) / (1 / 1.5 + 1 / 3), 4)
    hcp = pos("0xh", 0.5, title="Spread: Celtics (-3.5)", outcome="Celtics")
    assert st.match_market(hcp, [nba]) is None             # handicap: no es 1X2


def test_odds_bets_picks_undervalued_side_once():
    ev = evento("Arsenal", "Chelsea", {"Arsenal": 2.0, "Chelsea": 4.0, "Draw": 4.0})
    # Polymarket da 0.40 a que gana Arsenal; las casas, 0.50 -> ventaja 0.09 tras coste
    u = {"0xa": pos("0xa", 0.40, title="Will Arsenal FC win on 2026-10-15?")}
    seen = {}
    nuevas, disc = st.odds_bets(u, [ev], seen, NOW, entry_cost=0.01, min_edge=0.03)
    assert len(nuevas) == 1 and nuevas[0]["outcomeIndex"] == 0
    assert nuevas[0]["edge"] == 0.09 and nuevas[0]["fair"] == 0.5 and nuevas[0]["book"] == "pinnacle"
    assert nuevas[0]["tag"] == "ventaja 5%+" and nuevas[0]["source"] == "cuotas"
    # segunda pasada: sigue como discrepancia en la web, pero no se re-apuesta
    nuevas2, disc2 = st.odds_bets(u, [ev], seen, NOW, entry_cost=0.01, min_edge=0.03)
    assert nuevas2 == [] and len(disc2) == 1
    # Polymarket sobrevalora a Arsenal (0.60): la apuesta es el "No"
    u2 = {"0xb": pos("0xb", 0.60, title="Will Arsenal FC win on 2026-10-15?")}
    n3, _ = st.odds_bets(u2, [ev], {}, NOW, entry_cost=0.01, min_edge=0.03)
    assert n3[0]["outcomeIndex"] == 1 and n3[0]["outcome"] == "No"


def test_run_without_key_and_key_never_leaks():
    original = st.ODDS_API_KEY
    try:
        st.ODDS_API_KEY = ""
        state = {}
        st.run({"0xf": pos("0xf", 0.95)}, state, NOW, 0.01, fetch=None)
        assert "sin clave" in state["odds"]["status"] and "0xf" in state["seen"]
        st.ODDS_API_KEY = "SECRETO"
        def falla(url):
            raise RuntimeError(f"fallo temporal consultando {url}")
        state = {}
        st.run({}, state, NOW, 0.01, fetch=falla)
        assert state["odds"]["errors"] and all("SECRETO" not in e for e in state["odds"]["errors"])
        # no toca hasta pasadas ODDS_EVERY_MIN
        assert not st.odds_due(state["odds"], NOW)
    finally:
        st.ODDS_API_KEY = original


def test_wallet_history_and_strategy_groups():
    res = [{"roi": 1.0, "won": True, "wallets": ["0xw"]}] * 4 + \
          [{"roi": -1.0, "won": False, "wallets": ["0xw", "0xz"]}]
    ws = wallet_entry_stats(res)
    assert ws["0xw"] == {"n": 5, "wins": 4, "roi": 0.6} and ws["0xz"]["n"] == 1
    ent = [{"id": f"e{i}", "payout": 1.0, "won": True, "roi": 0.5, "entryPrice": 0.6,
            "eventSlug": f"ev{i}", "alertedAt": f"2026-10-{10 + i}",
            "walletPrior": {"n": 6, "roi": 0.2} if i < 2 else {"n": 2, "roi": 0.9}}
           for i in range(4)]
    strat = [{"source": "sesgo", "tag": "0.90-0.99", "payout": 1.0, "won": True, "roi": 0.05,
              "entryPrice": 0.95, "eventSlug": f"s{i}", "alertedAt": f"2026-10-{10 + i}"}
             for i in range(6)]
    stt = source_stats([], entries=ent, strategy_rows=strat)
    assert stt["entrada:historial+"]["n"] == 2              # solo con historial suficiente
    assert stt["sesgo"]["n"] == 6 and stt["sesgo:0.90-0.99"]["n"] == 6
    assert stt["sesgo"]["suppressed"] is False and stt["sesgo"]["halves"] == [0.05, 0.05]
    assert metrics.halves([{"roi": 1.0, "alertedAt": "a"}] * 3) is None   # muy pocas


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"OK {name}")

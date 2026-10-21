"""Checks del banco de pruebas de estrategias (sin red).
`python tests/test_strategies.py` o `pytest`."""
import json
import pathlib
import sys
from datetime import datetime

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


def gamma_event(title, start="2026-10-15 19:00:00+00", markets=None, slug="ev-ars-che"):
    return {"title": title, "slug": slug, "endDate": "2026-10-15T19:00:00Z", "markets": markets}


def mk(cond, question, prices, team="", outcomes=("Yes", "No"), kind="moneyline",
       start="2026-10-15 19:00:00+00"):
    # gamma manda outcomes y precios como TEXTO JSON
    return {"conditionId": cond, "question": question, "groupItemTitle": team,
            "outcomes": json.dumps(list(outcomes)), "outcomePrices": json.dumps([str(p) for p in prices]),
            "sportsMarketType": kind, "gameStartTime": start, "endDate": "2026-10-15T19:00:00Z"}


def gamma_api(eventos):
    def fetch(url):
        if url.endswith("/sports"):
            return [{"sport": "epl", "series": "10188"}, {"sport": "nfl", "series": "12185"}]
        if "series_id=10188" in url:
            return eventos
        return []
    return fetch


ARS_CHE = gamma_event("Arsenal FC vs. Chelsea FC", markets=[
    # la fecha del titulo es la ORIGINAL (partido aplazado): manda gameStartTime
    mk("0xa", "Will Arsenal FC win on 2026-09-16?", [0.40, 0.60], team="Arsenal FC"),
    mk("0xd", "Will Arsenal FC vs. Chelsea FC end in a draw?", [0.25, 0.75],
       team="Draw (Arsenal FC vs. Chelsea FC)"),
    mk("0xc", "Will Chelsea FC win on 2026-09-16?", [0.30, 0.70], team="Chelsea FC"),
    mk("0xs", "Spread: Arsenal FC (-1.5)", [0.5, 0.5], kind="spreads")])


def test_poly_matches_reads_league_markets_from_gamma():
    otros = [gamma_event("Arsenal FC vs. Chelsea FC - Total Corners", markets=[
                 mk("0xk", "Corners?", [0.5, 0.5], kind="totals")]),
             gamma_event("Leeds vs. Hull", markets=[   # ya empezado: fuera
                 mk("0xl", "Will Leeds win?", [0.5, 0.5], team="Leeds",
                    start="2026-10-14 09:00:00+00")])]
    partidos, err = st.poly_matches(gamma_api([ARS_CHE] + otros), NOW,
                                    sports=["soccer_epl", "soccer_spain_la_liga"])
    assert err == [] and len(partidos) == 1
    p = partidos[0]
    assert p["teams"] == ["Arsenal FC", "Chelsea FC"] and p["slug"] == "ev-ars-che"
    assert [m["conditionId"] for m in p["markets"]] == ["0xa", "0xd", "0xc"]   # sin hándicap
    assert p["markets"][0]["prices"] == [0.40, 0.60] and p["start"].hour == 19
    # un fallo de gamma se informa, no rompe
    def falla(url):
        raise RuntimeError("caido")
    assert st.poly_matches(falla, NOW)[0] == [] and st.poly_matches(falla, NOW)[1]


def test_odds_bets_matches_by_kickoff_and_prices_all_three_outcomes():
    ev = evento("Arsenal", "Chelsea", {"Arsenal": 2.0, "Chelsea": 4.0, "Draw": 4.0})
    partidos, _ = st.poly_matches(gamma_api([ARS_CHE]), NOW, sports=["soccer_epl"])
    seen = {}
    nuevas, disc, diag = st.odds_bets(partidos, [ev], seen, NOW, entry_cost=0.01, min_edge=0.03)
    assert diag == {"candidatos": 3, "emparejados": 3, "partidos": 1, "partidosEmparejados": 1}
    by = {b["conditionId"]: b for b in nuevas}
    # Arsenal: casas 0.50, Polymarket 0.40 -> compra "Yes" con ventaja 0.09
    a = by["0xa"]
    assert a["outcomeIndex"] == 0 and a["edge"] == 0.09 and a["fair"] == 0.5
    assert a["book"] == "pinnacle" and a["tag"] == "ventaja 5%+" and a["source"] == "cuotas"
    assert a["eventSlug"] == "ev-ars-che" and a["league"] == "soccer_epl"
    # empate: casas 0.25 = Polymarket 0.25 -> sin ventaja
    assert "0xd" not in by
    # Chelsea: casas 0.25, Polymarket 0.30 -> el "No" (0.70) vale 0.75: ventaja 0.04
    c = by["0xc"]
    assert c["outcomeIndex"] == 1 and c["outcome"] == "No" and c["edge"] == 0.04
    # segunda pasada: sigue en la web, no se re-apuesta
    n2, d2, _ = st.odds_bets(partidos, [ev], seen, NOW, entry_cost=0.01, min_edge=0.03)
    assert n2 == [] and len(d2) == 2


def test_match_event_needs_same_kickoff_and_one_sure_team():
    p = {"teams": ["FC Bayern München", "Borussia Dortmund"],
         "start": datetime.fromisoformat("2026-10-15T19:00:00+00:00")}
    ev = evento("Bayern Munich", "Borussia Dortmund", {"Bayern Munich": 1.5, "Borussia Dortmund": 5.0,
                                                       "Draw": 5.0})
    r = st.match_event(p, [ev])
    assert r and r[1] == {"FC Bayern München": "Bayern Munich",
                          "Borussia Dortmund": "Borussia Dortmund"}
    otro_dia = dict(ev, commence_time="2026-10-18T19:00:00Z")
    assert st.match_event(p, [otro_dia]) is None
    nfl = {"teams": ["Seahawks", "Broncos"], "start": p["start"]}
    ev2 = evento("Denver Broncos", "Seattle Seahawks", {"Denver Broncos": 2.0, "Seattle Seahawks": 2.0})
    assert st.match_event(nfl, [ev2])[1] == {"Seahawks": "Seattle Seahawks", "Broncos": "Denver Broncos"}
    # NFL: el resultado 0 del mercado es un equipo, no "Yes"
    m = {"question": "Seahawks vs. Broncos", "outcomes": ["Seahawks", "Broncos"], "team": ""}
    assert st.market_fair(m, nfl, st.match_event(nfl, [ev2])[1], {"Seattle Seahawks": 0.6,
                                                                  "Denver Broncos": 0.4}) == 0.6


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
        assert any("gamma" in e for e in state["odds"]["errors"])   # Polymarket tambien
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

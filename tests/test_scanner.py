"""Tests del nucleo de deteccion. Corren sin red: `python tests/test_scanner.py`
o `pytest`."""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import scanner
from scanner import (alertable, annotate_win_rates, categoria, detect_signals,
                     detect_whales, enrich_whales, entry_price, entry_timing,
                     fetch_market_resolutions, fill_price, final_payouts,
                     for_recipient, format_message, format_resolved,
                     format_sample_reached, format_whales, mark_alerted, migrate,
                     qualify_signals, recipients, resolve_tracked, resolve_whales,
                     shrunk_edge, source_stats, track_record, track_signals,
                     update_track_records, whale_notifiable)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def pos(cond="0xabc", outcome_idx=0, usd=1000.0, **over):
    p = {
        "conditionId": cond, "outcomeIndex": outcome_idx,
        "outcome": "Yes" if outcome_idx == 0 else "No",
        "title": "Mercado de prueba", "slug": "prueba", "eventSlug": "evento",
        "endDate": "2026-12-31", "curPrice": 0.5, "currentValue": usd,
        "avgPrice": 0.4, "redeemable": False,
    }
    p.update(over)
    return p


def traders(n, prefix="a", **pos_over):
    return {f"0x{prefix}{i}": {"name": f"trader{i}", "positions": [pos(**pos_over)]}
            for i in range(n)}


def test_get_json_wraps_transient_failure():
    original_urlopen, original_sleep = scanner.urllib.request.urlopen, scanner.time.sleep
    calls = []

    def offline(request, timeout):
        calls.append(request.full_url)
        raise OSError("offline")

    scanner.urllib.request.urlopen = offline
    scanner.time.sleep = lambda _: None
    try:
        try:
            scanner.get_json("https://example.test", retries=3)
            assert False, "debio lanzar FetchError"
        except scanner.FetchError as exc:
            assert "fallo temporal" in str(exc)
        assert len(calls) == 3
    finally:
        scanner.urllib.request.urlopen = original_urlopen
        scanner.time.sleep = original_sleep

def test_five_traders_same_outcome_is_signal():
    signals = detect_signals(traders(5), min_users=5, min_usd=500)
    assert len(signals) == 1
    s = signals[0]
    assert s["numTraders"] == 5
    assert s["totalUsd"] == 5000.0
    assert s["outcome"] == "Yes"
    assert s["avgEntryPrice"] == 0.4
    assert s["upside"] == 1.0        # comprar a 0.5, cobrar 1 -> x1 (+100%)
    assert s["entryGap"] == 0.1      # precio 0.5 vs entrada media 0.4


def test_four_traders_no_signal():
    assert detect_signals(traders(4), min_users=5, min_usd=500) == []


def test_opposite_outcomes_not_grouped():
    data = {**traders(3, outcome_idx=0), **traders(3, prefix="b", outcome_idx=1)}
    assert detect_signals(data, min_users=5, min_usd=500) == []


def test_small_position_filtered():
    data = traders(4)
    data["0xsmall"] = {"name": "", "positions": [pos(usd=100)]}
    assert detect_signals(data, min_users=5, min_usd=500) == []


def test_redeemable_filtered():
    data = traders(4)
    data["0xdone"] = {"name": "", "positions": [pos(redeemable=True)]}
    assert detect_signals(data, min_users=5, min_usd=500) == []


def test_track_signals_keeps_first_detection():
    tracked = {}
    signals = detect_signals(traders(5), min_users=5, min_usd=500)
    assert track_signals(tracked, signals, "T1") == [signals[0]["id"]]
    t = tracked[signals[0]["id"]]
    assert t["firstSeen"] == "T1" and t["detectPrice"] == 0.5 and not t["alerted"]
    # el precio se mueve, pero la deteccion queda fijada en la 1a vez
    signals2 = detect_signals(traders(5, curPrice=0.8), min_users=5, min_usd=500)
    assert track_signals(tracked, signals2, "T2") == []
    assert t["detectPrice"] == 0.5 and t["lastPrice"] == 0.8 and t["lastSeen"] == "T2"
    assert signals2[0]["firstSeen"] == "T1"
    # desaparece y vuelve: NO es un episodio nuevo con otra entrada
    track_signals(tracked, [], "T3")
    assert track_signals(tracked, detect_signals(traders(5, curPrice=0.3), min_users=5,
                                                 min_usd=500), "T4") == []
    assert t["detectPrice"] == 0.5 and len(tracked) == 1


def test_parses_real_api_fixture():
    real = json.loads((FIXTURES / "positions.json").read_text(encoding="utf-8"))
    live = [dict(p, redeemable=False, currentValue=9999) for p in real]
    data = {f"0xr{i}": {"name": f"t{i}", "positions": live} for i in range(5)}
    signals = detect_signals(data, min_users=5, min_usd=500)
    expected = len({(p["conditionId"], p["outcomeIndex"]) for p in real})
    assert len(signals) == expected
    assert all(s["numTraders"] == 5 for s in signals)
    assert all(s["title"] and s["slug"] for s in signals)


def test_price_drift_marks_stale():
    # entrada media 0.4, precio 0.7 -> deriva +0.3 > 0.15: descartada
    data = traders(5, curPrice=0.7)
    signals = detect_signals(data, min_users=5, min_usd=500, max_drift=0.15)
    assert len(signals) == 1 and signals[0]["stale"] is True
    # entrada 0.4, precio 0.5 -> deriva 0.1: fresca
    signals = detect_signals(traders(5), min_users=5, min_usd=500, max_drift=0.15)
    assert signals[0]["stale"] is False
    # la deriva es ABSOLUTA: desplome muy por debajo de la entrada tambien descarta
    signals = detect_signals(traders(5, curPrice=0.1), min_users=5, min_usd=500, max_drift=0.15)
    assert signals[0]["stale"] is True


def test_stale_signals_sort_last():
    data = {**traders(7, cond="0xviejo", curPrice=0.9),
            **traders(5, prefix="b", cond="0xnuevo")}
    signals = detect_signals(data, min_users=5, min_usd=500, max_drift=0.15)
    assert [s["id"].split(":")[0] for s in signals] == ["0xnuevo", "0xviejo"]
    assert [s["stale"] for s in signals] == [False, True]


def _tracked_alerted(entry=0.5, **over):
    tracked = {}
    signals = detect_signals(traders(5), min_users=5, min_usd=500)
    track_signals(tracked, signals, "2026-10-01T00:00:00+00:00")
    t = tracked[signals[0]["id"]]
    mark_alerted(t, signals[0], entry, "libro", "2026-10-01T00:00:00+00:00", sent=True)
    t.update(over)
    return tracked, t


def test_resolve_tracked_uses_oracle_not_positions():
    # la senal desaparecio (sus traders cayeron bajo $500 al ir perdiendo):
    # el veredicto lo da el oraculo igualmente -> la perdida SI se registra
    tracked, t = _tracked_alerted()
    now = "2026-10-02T00:00:00+00:00"
    h = resolve_tracked(tracked, {"0xabc": [0.0, 1.0]}, now, active_ids=set())
    assert len(h) == 1 and h[0]["won"] is False and h[0]["roi"] == -1.0
    assert h[0]["resolvedAt"] == now and tracked == {}
    # gana: entrada 0.5 -> +100%; investigacion con entrada deteccion + coste
    tracked, t = _tracked_alerted()
    h = resolve_tracked(tracked, {"0xabc": [1.0, 0.0]}, now)
    assert h[0]["won"] and h[0]["roi"] == 1.0
    assert h[0]["roiDetect"] == round(1 / 0.51 - 1, 4)
    # 50-50: cobra 0.5 por accion -> ROI 0 a entrada 0.5, ni acierto ni fallo
    tracked, t = _tracked_alerted()
    h = resolve_tracked(tracked, {"0xabc": [0.5, 0.5]}, now)
    assert h[0]["won"] is False and h[0]["roi"] == 0.0 and h[0]["payout"] == 0.5
    # sin liquidar: sigue en seguimiento
    tracked, t = _tracked_alerted()
    assert resolve_tracked(tracked, {}, now) == [] and len(tracked) == 1
    # no alertada: roi None (no mezcla), roiDetect si
    tracked, t = _tracked_alerted(alerted=False)
    h = resolve_tracked(tracked, {"0xabc": [1.0, 0.0]}, now)
    assert h[0]["roi"] is None and h[0]["roiDetect"] > 0


def test_resolve_tracked_expires_unresolved():
    tracked, t = _tracked_alerted(endDate="2026-01-01")
    h = resolve_tracked(tracked, {}, "2026-10-02T00:00:00+00:00", active_ids=set())
    assert h[0]["unresolved"] and h[0]["roi"] is None and tracked == {}
    # si sigue activa no se expira aunque la fecha haya pasado
    tracked, t = _tracked_alerted(endDate="2026-01-01")
    assert resolve_tracked(tracked, {}, "2026-10-02T00:00:00+00:00",
                           active_ids={t["id"]}) == []


def test_format_message():
    signals = detect_signals(traders(5), min_users=5, min_usd=500)
    msg = format_message(signals)
    assert "5 traders" in msg
    assert "Mercado de prueba" in msg
    assert "[Yes]" in msg
    assert "https://polymarket.com/event/evento" in msg
    assert len(format_message(signals * 30)) < 4096  # limite de Telegram


def test_format_resolved():
    tracked, _ = _tracked_alerted()
    ganada = resolve_tracked(tracked, {"0xabc": [1.0, 0.0]}, "2026-10-02T00:00:00+00:00")
    msg = format_resolved(ganada)
    assert "gano" in msg and "ROI +100%" in msg  # entrada 0.5 -> +100%
    tracked, _ = _tracked_alerted()
    perdida = resolve_tracked(tracked, {"0xabc": [0.0, 1.0]}, "2026-10-02T00:00:00+00:00")
    assert "ROI -100%" in format_resolved(perdida)
    assert len(format_resolved(ganada * 30)) < 4096  # limite de Telegram


def test_detect_whales_from_real_fixture():
    trades = json.loads((FIXTURES / "trades.json").read_text(encoding="utf-8"))
    top = {trades[0]["proxyWallet"]}
    whales = detect_whales(trades, last_ts=0, top_wallets=top, min_usd=10000)
    assert whales, "el fixture tiene compras > $10k"
    assert all(w["usd"] >= 10000 for w in whales)
    # cuota altisima (>= max_price) fuera: comprar a 0.99 no es senal
    assert all(w["price"] < 0.9 for w in whales)
    con_cara = detect_whales(trades, last_ts=0, top_wallets=top, min_usd=10000, max_price=1.0)
    assert len(con_cara) > len(whales)  # el fixture tiene una compra >= 0.9
    assert all(w["timestamp"] >= whales[-1]["timestamp"] for w in whales)  # desc
    # los SELL nunca pasan
    sells = [t for t in trades if t["side"] == "SELL"]
    assert all(w["tx"] not in {s["transactionHash"] for s in sells} for w in whales)
    # marca de agua: nada anterior o igual a last_ts
    newest = whales[0]["timestamp"]
    assert detect_whales(trades, last_ts=newest, top_wallets=set(), min_usd=10000) == []
    # isTop refleja pertenencia al leaderboard
    assert any(w["isTop"] for w in whales if w["wallet"] in top) or not any(
        w["wallet"] in top for w in whales)
    # longshot = compra a cuota improbable (precio <= 0.3)
    assert all(w["longshot"] == (w["price"] <= 0.3) for w in whales)


def test_format_whales():
    w = {"tx": "0x1", "wallet": "0xabcdef1234", "name": "bigfish", "usd": 75000.0,
         "price": 0.45, "title": "Mercado X", "outcome": "Yes",
         "eventSlug": "evento-x", "timestamp": 1, "isTop": True}
    msg = format_whales([w])
    assert "$75,000" in msg and "Mercado X" in msg and "bigfish" in msg
    assert "(TOP 50)" in msg and "evento-x" in msg
    assert "LONGSHOT" not in msg
    assert "LONGSHOT" in format_whales([dict(w, longshot=True)])
    assert len(format_whales([w] * 50)) < 4096


def test_enrich_whales_adds_wallet_context():
    prev = [{"wallet": "0xw"}]
    new = [{"wallet": "0xw"}, {"wallet": "0xnuevo"}]
    tracks = {"0xw": {"wins": 6, "losses": 2, "net": 12500.0}}
    poscache = {"0xw": [{"currentValue": 1000}, {"currentValue": 250.5}]}
    out = enrich_whales(new, prev, tracks, poscache, {})
    assert out[0]["repeatBuys"] == 2 and out[1]["repeatBuys"] == 1
    assert out[0]["walletUsd"] == 1250.5
    assert out[0]["winRate"] == 0.75 and out[0]["wins30d"] == 6
    assert out[0]["netPnl"] == 12500.0
    # sin datos: campos a None, nunca revienta
    assert out[1]["walletUsd"] is None and out[1]["winRate"] is None
    assert out[1]["wins30d"] is None and out[1]["losses30d"] is None
    assert out[1]["netPnl"] is None


def test_track_record_wins_and_losses():
    since = 1_700_000_000  # ventana desde 2023-11-14
    actividad = [
        {"type": "REDEEM", "conditionId": "0xg", "timestamp": since + 10, "usdcSize": 5000},
        {"type": "REDEEM", "conditionId": "0xg", "timestamp": since + 20, "usdcSize": 5000},  # duplicado
        {"type": "REDEEM", "conditionId": "0xviejo", "timestamp": 10, "usdcSize": 5000},      # fuera de ventana
        {"type": "REDEEM", "conditionId": "0xpolvo", "timestamp": since + 10, "usdcSize": 1}, # ruido (cuenta caja, no win)
        {"side": "BUY", "timestamp": since + 5, "usdcSize": 3000},   # compra: sale caja
        {"side": "SELL", "timestamp": since + 6, "usdcSize": 500},   # venta: entra caja
        {"side": "BUY", "timestamp": 5, "usdcSize": 9999},           # compra fuera de ventana: ignorada
    ]
    posiciones = [
        dict(pos(cond="0xl", redeemable=True, curPrice=0), initialValue=800, endDate="2099-01-01"),
        # ganadora sin canjear: no es perdida
        dict(pos(cond="0xgana", redeemable=True, curPrice=1), initialValue=800, endDate="2099-01-01"),
        # perdida fuera de la ventana
        dict(pos(cond="0xantigua", redeemable=True, curPrice=0), initialValue=800, endDate="2001-01-01"),
        # posicion viva: su valor de mercado cuenta como caja abierta
        dict(pos(cond="0xviva", redeemable=False), currentValue=1200),
    ]
    wins, losses, net, cats, edge = track_record("0x", posiciones, since,
                                                 fetch=lambda url: actividad)
    assert (wins, losses) == (1, 1)
    # net = canjes(5000+5000+1) + venta(500) - compra(3000) + abierto(1200) = 8701
    assert net == 8701.0
    # sin titulo reconocible, win y loss caen en "otras" (1 y 1)
    assert cats["otras"]["wins"] == 1 and cats["otras"]["losses"] == 1
    # ventaja: la compra no trae conditionId (no se sabe el precio de la
    # ganada); la perdida si (avgPrice 0.4 de la posicion) -> -0.4
    assert edge == {"sum": -0.4, "n": 1}


def test_track_record_edge_vs_price():
    since = 1_700_000_000
    actividad = [
        # compro 'Yes' de 0xa a 0.25 y lo canjeo: ventaja +0.75
        {"side": "BUY", "conditionId": "0xa", "outcomeIndex": 0, "timestamp": since + 1,
         "usdcSize": 250, "size": 1000},
        {"type": "REDEEM", "conditionId": "0xa", "timestamp": since + 9, "usdcSize": 1000},
        # compro los DOS lados de 0xb: no se sabe que lado gano -> sin ventaja
        {"side": "BUY", "conditionId": "0xb", "outcomeIndex": 0, "timestamp": since + 1,
         "usdcSize": 100, "size": 200},
        {"side": "BUY", "conditionId": "0xb", "outcomeIndex": 1, "timestamp": since + 1,
         "usdcSize": 100, "size": 200},
        {"type": "REDEEM", "conditionId": "0xb", "timestamp": since + 9, "usdcSize": 200},
    ]
    posiciones = [dict(pos(cond="0xl", redeemable=True, curPrice=0, avgPrice=0.9),
                       initialValue=900, endDate="2099-01-01")]
    wins, losses, _, _, edge = track_record("0x", posiciones, since, fetch=lambda u: actividad)
    assert (wins, losses) == (2, 1)
    # +0.75 (gano a 0.25) - 0.9 (perdio comprando a 0.9) = -0.15 en 2 medibles
    assert edge == {"sum": -0.15, "n": 2}
    # encogido hacia 0 con 10 apuestas neutras: -0.15 / 12
    assert shrunk_edge(edge["sum"], edge["n"]) == round(-0.15 / 12, 4)
    assert shrunk_edge(0, 0) is None


def test_categoria_keywords():
    assert categoria("France vs. Spain: Team to Advance") == "deportes"
    assert categoria("Will Trump win the election?") == "politica"
    assert categoria("Bitcoin above $100k?") == "cripto"
    assert categoria("Will aliens be confirmed?") == "otras"


def test_annotate_category_win_rates():
    signals = detect_signals(traders(5, title="NBA Finals: Lakers vs Celtics"),
                             min_users=5, min_usd=500)
    tracks = {"0xa0": {"wins": 8, "losses": 2,
                       "cats": {"deportes": {"wins": 9, "losses": 1}}},
              "0xa1": {"wins": 6, "losses": 4,
                       "cats": {"deportes": {"wins": 1, "losses": 1}}}}  # muestra corta
    annotate_win_rates(signals, tracks)
    s = signals[0]
    assert s["cat"] == "deportes"
    por_wallet = {t["wallet"]: t["catWinRate"] for t in s["traders"]}
    assert por_wallet["0xa0"] == 0.9      # 9/10 en deportes
    assert por_wallet["0xa1"] is None     # 2 mercados: muestra insuficiente
    assert s["avgCatWinRate"] == 0.9      # media de los que tienen muestra


def test_resolve_whales_roi_and_pending():
    whales = [
        {"conditionId": "0xW", "outcomeIndex": 1, "price": 0.25, "followPrice": 0.27,
         "title": "M", "outcome": "No", "eventSlug": "e", "timestamp": 1},  # gana
        {"conditionId": "0xL", "outcomeIndex": 0, "price": 0.6, "title": "M2",
         "outcome": "Yes", "eventSlug": "e2", "timestamp": 1},             # pierde
        {"conditionId": "0xOpen", "outcomeIndex": 0, "price": 0.5, "title": "M3",
         "outcome": "Yes", "eventSlug": "e3", "timestamp": 1},             # sin liquidar
    ]
    kept, res = resolve_whales(whales, {"0xW": [0.0, 1.0], "0xL": [0.0, 1.0]}, "T")
    assert [w["conditionId"] for w in kept] == ["0xOpen"]
    by = {r["id"]: r for r in res}
    # entrada = precio del SEGUIDOR (0.27), no el de la whale (0.25)
    assert by["0xW:1"]["won"] and by["0xW:1"]["roi"] == round(1 / 0.27 - 1, 4)
    assert by["0xW:1"]["whalePrice"] == 0.25
    # sin followPrice (whale antigua): precio de la whale + coste de entrada
    assert by["0xL:0"]["entryPrice"] == 0.61 and by["0xL:0"]["roi"] == -1.0
    assert all(r["source"] == "whale" for r in res)


def test_fetch_market_resolutions_parses_gamma():
    fake = [
        {"conditionId": "0xA", "closed": True, "outcomePrices": '["1", "0"]'},
        {"conditionId": "0xB", "closed": True, "outcomePrices": '["0", "1"]'},
        {"conditionId": "0xC", "closed": False, "outcomePrices": '["0.4", "0.6"]'},
        # cerrado pero sin liquidar (precios intermedios): NO es veredicto
        {"conditionId": "0xD", "closed": True, "outcomePrices": '["0.97", "0.03"]'},
        {"conditionId": "0xE", "closed": True, "outcomePrices": '["0.5", "0.5"]'},  # 50-50
    ]
    got = fetch_market_resolutions(["0xA", "0xB", "0xC", "0xD", "0xE"], fetch=lambda url: fake)
    assert got == {"0xA": [1.0, 0.0], "0xB": [0.0, 1.0], "0xE": [0.5, 0.5]}
    assert final_payouts({"closed": True, "outcomePrices": ["1", "1"]}) is None
    assert final_payouts({"closed": True, "outcomePrices": "basura"}) is None
    # un lote que falla no rompe el escaneo
    def roto(url):
        raise OSError("caido")
    assert fetch_market_resolutions(["0xA"], fetch=roto) == {}


def test_source_stats_suppresses_losing_source():
    def h(**kw):
        base = {"source": "coincidencia", "payout": 1.0, "won": True, "roi": 1.0,
                "roiDetect": 1.0, "detectPrice": 0.5, "entryPrice": 0.5,
                "alerted": True, "tier": "amplio", "id": "x"}
        base.update(kw)
        return base
    hist = (
        [h(source="whale", id=f"w{i}", payout=0.0, won=False, roi=-1.0) for i in range(18)]
        + [h(source="whale", id=f"g{i}", roi=0.5) for i in range(2)]
        + [h(source="whale", id="g0", roi=0.5)] * 5            # mismo mercado: cuenta 1
        + [h()] * 3
        + [h(alerted=False, roi=None)]                         # no alertada: solo investigacion
        + [h(legacy=True)] * 50                                # historico viejo: no cuenta
        + [h(payout=None, roi=None, unresolved=True)]          # sin resolver: no cuenta
    )
    stats = source_stats(hist, min_sample=20)
    fm = stats["whale"]
    assert fm["n"] == 20 and fm["wins"] == 2 and fm["avgRoi"] < 0
    assert fm["suppressed"] is True and fm["verdict"] == "pierde"
    co = stats["coincidencia"]
    assert co["n"] == 3 and co["suppressed"] is False and co["verdict"] == "sin muestra"
    assert stats["coincidencia:amplio"]["n"] == 3
    assert stats["investigacion"]["n"] == 4 and stats["investigacion"]["suppressed"] is False
    # rentable solo si TODO el IC95 esta por encima de 0
    mix = [h(roi=1.0)] * 15 + [h(payout=0.0, won=False, roi=-1.0)] * 5
    s = source_stats(mix, min_sample=20)["coincidencia"]
    assert s["avgRoi"] == 0.5 and s["ci95"][0] > 0 and s["verdict"] == "rentable"
    mix = [h(roi=1.0)] * 11 + [h(payout=0.0, won=False, roi=-1.0)] * 9
    assert source_stats(mix, min_sample=20)["coincidencia"]["verdict"] == "no concluyente"


def test_format_sample_reached():
    rentable = format_sample_reached("coincidencia", {
        "n": 22, "wins": 14, "avgRoi": 0.18, "ci95": [0.02, 0.34], "avgEntry": 0.5,
        "verdict": "rentable"})
    assert "22 señales" in rentable and "+18%" in rentable and "RENTABLE" in rentable
    assert "IC95 +2% a +34%" in rentable and "implicaba 50%" in rentable
    perdedora = format_sample_reached("whale", {"n": 30, "wins": 8, "avgRoi": -0.4,
                                                 "ci95": None, "verdict": "pierde"})
    assert "-40%" in perdedora and "silencia" in perdedora


def test_qualify_signals_two_tiers():
    def sig(id_, n, **kw):
        s = {"id": id_, "numTraders": n, "avgWinRate": None, "avgCatWinRate": None}
        s.update(kw)
        return s
    cands = [
        sig("amplio", 5),                        # >=5: siempre pasa
        sig("esp_cat", 3, avgCatWinRate=0.7),    # 3 con acierto categoria alto
        sig("esp_glob", 4, avgWinRate=0.65),     # 4 con acierto global alto
        sig("flojo", 3, avgWinRate=0.5),         # 3 sin acierto probado: fuera
        sig("sin_datos", 4),                     # 4 sin veredicto: fuera
    ]
    out = qualify_signals(cands)
    assert {s["id"]: s["tier"] for s in out} == {
        "amplio": "amplio", "esp_cat": "especialistas", "esp_glob": "especialistas"}
    # la categoria manda sobre el global cuando existe: buen global no salva
    # un mal historial en la categoria concreta del mercado
    assert qualify_signals([sig("mal_cat", 3, avgCatWinRate=0.4, avgWinRate=0.9)]) == []


def test_update_track_records_respects_ttl():
    cache = {"0xa": {"wins": 3, "losses": 3, "at": 1000}}
    calls = []
    def fetch(url):
        calls.append(url)
        return []
    datos = {"0xa": {"positions": []}}
    tracks, _ = update_track_records(cache, {"0xa"}, datos, now_ts=2000, fetch=fetch)
    assert tracks["0xa"]["wins"] == 3 and calls == []  # cache fresca: sin red
    tracks, _ = update_track_records(cache, {"0xa"}, datos, now_ts=1000 + 7 * 3600, fetch=fetch)
    assert tracks["0xa"]["at"] == 1000 + 7 * 3600  # caducada: recalculado


def test_whale_notifiable_filters_losers():
    # sin PnL conocido: cae a la tasa de acierto (o al top 50 de comodin)
    assert whale_notifiable({"isTop": True, "winRate": None})
    assert whale_notifiable({"isTop": False, "winRate": 0.6})
    assert not whale_notifiable({"isTop": False, "winRate": 0.3})   # perdedor
    assert not whale_notifiable({"isTop": False, "winRate": None})  # sin historial
    # el top 50 NO borra un historial perdedor confirmado
    assert not whale_notifiable({"isTop": True, "winRate": 0.2})
    # con PnL real conocido manda el dinero, por encima de todo lo demas
    assert whale_notifiable({"isTop": False, "winRate": 0.3, "netPnl": 5000})   # net+ salva
    assert not whale_notifiable({"isTop": True, "winRate": 0.9, "netPnl": -1000})  # net- hunde
    assert not whale_notifiable({"isTop": True, "winRate": None, "netPnl": 0})


def test_annotate_win_rates():
    signals = detect_signals(traders(5), min_users=5, min_usd=500)
    tracks = {"0xa0": {"wins": 8, "losses": 2}, "0xa1": {"wins": 3, "losses": 3},
              "0xa2": {"wins": 1, "losses": 0}}  # historial corto: no cuenta
    annotate_win_rates(signals, tracks)
    por_wallet = {t["wallet"]: t["winRate"] for t in signals[0]["traders"]}
    assert por_wallet["0xa0"] == 0.8 and por_wallet["0xa1"] == 0.5
    assert por_wallet["0xa2"] is None and por_wallet["0xa4"] is None
    assert signals[0]["avgWinRate"] == 0.65  # media de los que tienen datos


def test_recipients_env():
    os.environ["TELEGRAM_CHAT_ID"] = "111, 222"
    assert [r["chatId"] for r in recipients()] == ["111", "222"]
    # por defecto sin minimo propio: llegan TODAS las cualificadas
    assert recipients()[0]["minUsers"] is None
    os.environ["TELEGRAM_RECIPIENTS"] = '[{"chatId": 333, "minUsers": 7}]'
    r = recipients()[0]
    assert r["chatId"] == "333" and r["minUsers"] == 7
    assert r["whaleMinUsd"] == 50000.0  # umbral no definido cae al global
    del os.environ["TELEGRAM_RECIPIENTS"], os.environ["TELEGRAM_CHAT_ID"]
    assert recipients() == []


def test_specialist_signals_reach_default_recipient():
    # el bug: el filtro por destinatario caia a MIN_USERS=5 y las de
    # especialistas (3-4 traders) no llegaban nunca por Telegram
    esp = {"numTraders": 3, "tier": "especialistas"}
    amplio = {"numTraders": 6, "tier": "amplio"}
    assert for_recipient([esp, amplio], {"minUsers": None}) == [esp, amplio]
    assert for_recipient([esp, amplio], {"minUsers": 5}) == [amplio]


def test_alertable_rules():
    base = {"stale": False, "curPrice": 0.5, "edgeEst": 0.02}
    assert alertable(base)
    assert not alertable(dict(base, stale=True))
    assert not alertable(dict(base, curPrice=0.92))          # favorito casi resuelto
    assert alertable(base, min_edge=0.01)
    assert not alertable(base, min_edge=0.05)
    assert not alertable(dict(base, edgeEst=None), min_edge=0.0)


def test_fill_price_walks_the_book():
    book = {"asks": [{"price": "0.55", "size": "100"}, {"price": "0.52", "size": "100"},
                     {"price": "0.60", "size": "1000"}]}
    # $100: 100 acc a 0.52 ($52) + 87.27 acc a 0.55 ($48) -> media ~0.5339
    assert fill_price(book, 100) == round(100 / (100 + 48 / 0.55), 4)
    assert fill_price(book, 20) == 0.52                     # cabe en el mejor ask
    assert fill_price({"asks": [{"price": "0.5", "size": "10"}]}, 100) is None  # sin liquidez
    assert fill_price({}, 100) is None


def test_entry_price_falls_back_to_cost():
    libro = {"asks": [{"price": "0.53", "size": "1000"}]}
    assert entry_price("tok", 0.5, fetch=lambda url, retries=1: libro) == (0.53, "libro")
    def caido(url, retries=1):
        raise OSError("sin red")
    assert entry_price("tok", 0.5, fetch=caido) == (0.51, "estimado")
    assert entry_price("", 0.5) == (0.51, "estimado")       # sin token: estimado


def test_entry_timing_counts_recent_buys():
    s = detect_signals(traders(3, cond="0xc"), min_users=3, min_usd=500)[0]
    now = 1_000_000
    feeds = {
        "0xa0": [{"side": "BUY", "conditionId": "0xc", "outcomeIndex": 0, "timestamp": now - 3600}],
        "0xa1": [{"side": "BUY", "conditionId": "0xc", "outcomeIndex": 0, "timestamp": now - 5 * 86400},
                 {"side": "BUY", "conditionId": "0xc", "outcomeIndex": 1, "timestamp": now}],  # otro lado
        "0xa2": [],
    }
    act = lambda url: feeds[url.split("user=")[1].split("&")[0]]
    entry_timing(s, act, now)
    assert s["recentEntrants"] == 1 and s["lastEntryTs"] == now - 3600


def test_annotate_edge_estimate():
    signals = detect_signals(traders(2, title="Mercado"), min_users=2, min_usd=500)
    tracks = {"0xa0": {"wins": 1, "losses": 0, "edge": {"sum": 2.0, "n": 10}},
              "0xa1": {"wins": 1, "losses": 0}}                  # sin datos de ventaja
    annotate_win_rates(signals, tracks)
    s = signals[0]
    assert s["edgeEst"] == 0.1                  # 2.0 / (10 + 10)
    assert s["pEst"] == 0.6 and s["expRoi"] == round(0.6 / 0.51 - 1, 4)


def test_migrate_marks_old_history_legacy():
    doc = {"history": [{"source": "coincidencia", "won": True, "roi": 1.0}],
           "signals": [{"id": "0xq:1", "title": "T", "outcome": "No", "curPrice": 0.7,
                        "entryPrice": 0.6, "numTraders": 4, "stale": False}],
           "whales": [{"wallet": "0x"}], "announcedSamples": ["coincidencia"]}
    out = migrate(doc, "2026-10-14T00:00:00+00:00")
    assert out["history"][0]["legacy"] and out["whales"][0]["legacy"]
    t = out["tracked"]["0xq:1"]
    assert t["seeded"] and t["detectPrice"] == 0.6 and t["outcomeIndex"] == 1
    assert out["announcedSamples"] == [] and out["schema"] == 2
    assert migrate(out, "otra") is out          # idempotente


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"OK {name}")

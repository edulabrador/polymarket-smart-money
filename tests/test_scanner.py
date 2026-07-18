"""Tests del nucleo de deteccion. Corren sin red: `python tests/test_scanner.py`
o `pytest`."""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from backtest import backtest_signals
from scanner import (annotate_win_rates, detect_signals, detect_whales,
                     enrich_whales, format_message, format_resolved,
                     format_whales, merge_previous, recipients,
                     resolve_history, track_record, update_track_records,
                     whale_notifiable)

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


def test_merge_keeps_firstseen_and_flags_new():
    signals = detect_signals(traders(5), min_users=5, min_usd=500)
    merged, new = merge_previous(signals, [], "T1")
    assert new == merged
    assert merged[0]["firstSeen"] == "T1"
    assert merged[0]["entryPrice"] == 0.5   # precio de mercado al aparecer
    # el precio de mercado se mueve, pero entryPrice queda fijado en el 1er visto
    signals2 = detect_signals(traders(5, curPrice=0.8), min_users=5, min_usd=500)
    merged2, new2 = merge_previous(signals2, merged, "T2")
    assert new2 == []
    assert merged2[0]["firstSeen"] == "T1"
    assert merged2[0]["lastSeen"] == "T2"
    assert merged2[0]["entryPrice"] == 0.5   # NO se re-captura al precio nuevo


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


def test_resolve_history_records_verdict():
    signals = detect_signals(traders(5), min_users=5, min_usd=500)
    merged, _ = merge_previous(signals, [], "T1")
    sid = merged[0]["id"]
    ganada = {sid: pos(redeemable=True, curPrice=1)}
    perdida = {sid: pos(redeemable=True, curPrice=0)}
    sin_veredicto = {}  # los traders salieron antes de resolver

    h = resolve_history(merged, set(), ganada, "T2")
    assert len(h) == 1 and h[0]["won"] is True and h[0]["resolvedAt"] == "T2"
    # entrada 0.5, gana -> ROI (1-0.5)/0.5 = +1.0 (dobla)
    assert h[0]["entryPrice"] == 0.5 and h[0]["roi"] == 1.0
    h = resolve_history(merged, set(), perdida, "T2")
    assert h[0]["won"] is False and h[0]["roi"] == -1.0   # pierde el 100%
    assert resolve_history(merged, set(), sin_veredicto, "T2") == []
    # si la senal sigue activa, no pasa al historico
    assert resolve_history(merged, {sid}, ganada, "T2") == []


def test_format_message():
    signals = detect_signals(traders(5), min_users=5, min_usd=500)
    msg = format_message(signals)
    assert "5 traders" in msg
    assert "Mercado de prueba" in msg
    assert "[Yes]" in msg
    assert "https://polymarket.com/event/evento" in msg
    assert len(format_message(signals * 30)) < 4096  # limite de Telegram


def test_format_resolved():
    signals = detect_signals(traders(5), min_users=5, min_usd=500)
    merged, _ = merge_previous(signals, [], "T1")
    ganada = resolve_history(merged, set(), {merged[0]["id"]: pos(redeemable=True, curPrice=1)}, "T2")
    msg = format_resolved(ganada)
    assert "gano" in msg and "ROI +100%" in msg  # entrada 0.5 -> +100%
    perdida = resolve_history(merged, set(), {merged[0]["id"]: pos(redeemable=True, curPrice=0)}, "T2")
    assert "ROI -100%" in format_resolved(perdida)
    assert len(format_resolved(ganada * 30)) < 4096  # limite de Telegram


def test_detect_whales_from_real_fixture():
    trades = json.loads((FIXTURES / "trades.json").read_text(encoding="utf-8"))
    top = {trades[0]["proxyWallet"]}
    whales = detect_whales(trades, last_ts=0, top_wallets=top, min_usd=10000)
    assert whales, "el fixture tiene compras > $10k"
    assert all(w["usd"] >= 10000 for w in whales)
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
    wins, losses, net = track_record("0x", posiciones, since, fetch=lambda url: actividad)
    assert (wins, losses) == (1, 1)
    # net = canjes(5000+5000+1) + venta(500) - compra(3000) + abierto(1200) = 8701
    assert net == 8701.0


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
    os.environ["TELEGRAM_RECIPIENTS"] = '[{"chatId": 333, "minUsers": 7}]'
    r = recipients()[0]
    assert r["chatId"] == "333" and r["minUsers"] == 7
    assert r["whaleMinUsd"] == 50000.0  # umbral no definido cae al global
    del os.environ["TELEGRAM_RECIPIENTS"], os.environ["TELEGRAM_CHAT_ID"]
    assert recipients() == []


def test_backtest_mixes_redeems_and_dead_positions():
    # ganadas = canjes REDEEM; perdidas = posiciones muertas que nadie canjea
    canje = {"type": "REDEEM", "conditionId": "0xwin", "timestamp": 100,
             "usdcSize": 900.0, "title": "Ganado", "outcome": ""}
    redeems = {f"0xw{i}": [canje] for i in range(5)}
    perdida = dict(pos(cond="0xl", redeemable=True, curPrice=0),
                   currentValue=0, initialValue=800)
    positions = {f"0xl{i}": [perdida] for i in range(5)}
    # ruido que no debe contar: canje pequeno, canje fuera de ventana,
    # posicion viva, ganadora sin canjear (contara cuando se canjee)
    redeems["0xmini"] = [dict(canje, usdcSize=10)]
    redeems["0xviejo"] = [dict(canje, timestamp=1)]
    positions["0xviva"] = [dict(pos(cond="0xv"), initialValue=9999)]
    positions["0xsin"] = [dict(pos(cond="0xs", redeemable=True, curPrice=1),
                               initialValue=9999)]
    out = backtest_signals(redeems, positions, min_users=5, min_usd=500, since_ts=50)
    assert {s["id"]: s["won"] for s in out} == {"0xwin:win": True, "0xl:0": False}


def test_backtest_parses_real_activity_fixture():
    activity = json.loads((FIXTURES / "activity.json").read_text(encoding="utf-8"))
    redeems = {f"0xr{i}": activity for i in range(5)}
    out = backtest_signals(redeems, {}, min_users=5, min_usd=500, since_ts=0)
    grandes = {e["conditionId"] for e in activity if e["usdcSize"] >= 500}
    assert len(out) == len(grandes)
    assert all(s["won"] and s["numTraders"] == 5 for s in out)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"OK {name}")

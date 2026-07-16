"""Tests del nucleo de deteccion. Corren sin red: `python tests/test_scanner.py`
o `pytest`."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from scanner import (detect_signals, detect_whales, format_message,
                     format_whales, merge_previous, resolve_history)

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
    merged2, new2 = merge_previous(signals, merged, "T2")
    assert new2 == []
    assert merged2[0]["firstSeen"] == "T1"
    assert merged2[0]["lastSeen"] == "T2"


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
    h = resolve_history(merged, set(), perdida, "T2")
    assert h[0]["won"] is False
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


def test_format_whales():
    w = {"tx": "0x1", "wallet": "0xabcdef1234", "name": "bigfish", "usd": 75000.0,
         "price": 0.45, "title": "Mercado X", "outcome": "Yes",
         "eventSlug": "evento-x", "timestamp": 1, "isTop": True}
    msg = format_whales([w])
    assert "$75,000" in msg and "Mercado X" in msg and "bigfish" in msg
    assert "(TOP 50)" in msg and "evento-x" in msg
    assert len(format_whales([w] * 50)) < 4096


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"OK {name}")

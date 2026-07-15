"""Tests del nucleo de deteccion. Corren sin red: `python tests/test_scanner.py`
o `pytest`."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from scanner import detect_signals, format_message, merge_previous

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


def test_format_message():
    signals = detect_signals(traders(5), min_users=5, min_usd=500)
    msg = format_message(signals)
    assert "5 traders" in msg
    assert "Mercado de prueba" in msg
    assert "[Yes]" in msg
    assert "https://polymarket.com/event/evento" in msg
    assert len(format_message(signals * 30)) < 4096  # limite de Telegram


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"OK {name}")

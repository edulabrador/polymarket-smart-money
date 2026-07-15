"""Detecta coincidencias de los traders mas rentables de Polymarket.

Corre en GitHub Actions (la DGOJ bloquea *.polymarket.com en redes espanolas):
descarga el top de PnL del mes, las posiciones de cada trader, y escribe en
docs/signals.json las apuestas donde >= MIN_USERS traders coinciden.
Solo stdlib: sin dependencias.
"""
import json
import os
import pathlib
import time
import urllib.request
from datetime import datetime, timezone

TOP_N = int(os.getenv("TOP_N", "50"))
MIN_USERS = int(os.getenv("MIN_USERS", "5"))
MIN_POSITION_USD = float(os.getenv("MIN_POSITION_USD", "500"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SIGNALS_PATH = pathlib.Path(__file__).parent / "docs" / "signals.json"

LEADERBOARD_URL = ("https://data-api.polymarket.com/v1/leaderboard"
                   "?timePeriod=MONTH&orderBy=PNL&limit={n}")
# sizeThreshold va en tokens, pero toda posicion con currentValue >= X USD
# tiene al menos X tokens (precio <= 1), asi que sirve de prefiltro seguro.
POSITIONS_URL = ("https://data-api.polymarket.com/positions"
                 "?user={wallet}&sizeThreshold={threshold}&limit=500")


def get_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "polymarket-smart-money/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def detect_signals(positions_by_trader, min_users=MIN_USERS, min_usd=MIN_POSITION_USD):
    """positions_by_trader: {wallet: {"name": str, "positions": [dicts de la API]}}.

    Senal = grupo (conditionId, outcomeIndex) con >= min_users traders
    distintos, cada uno con posicion viva de >= min_usd USD actuales.
    """
    groups = {}
    for wallet, info in positions_by_trader.items():
        for p in info["positions"]:
            if p.get("redeemable") or p.get("currentValue", 0) < min_usd:
                continue
            key = f'{p["conditionId"]}:{p["outcomeIndex"]}'
            g = groups.setdefault(key, {"meta": p, "traders": {}})
            g["traders"][wallet] = {
                "wallet": wallet,
                "name": info.get("name", ""),
                "usd": round(p["currentValue"], 2),
                "avgPrice": p["avgPrice"],
            }

    signals = []
    for key, g in groups.items():
        if len(g["traders"]) < min_users:
            continue
        traders = sorted(g["traders"].values(), key=lambda t: -t["usd"])
        total = sum(t["usd"] for t in traders)
        m = g["meta"]
        signals.append({
            "id": key,
            "title": m["title"],
            "outcome": m["outcome"],
            "slug": m["slug"],
            "eventSlug": m["eventSlug"],
            "endDate": m.get("endDate", ""),
            "curPrice": m["curPrice"],
            "numTraders": len(traders),
            "totalUsd": round(total, 2),
            "avgEntryPrice": round(sum(t["avgPrice"] * t["usd"] for t in traders) / total, 4),
            "traders": traders,
        })
    signals.sort(key=lambda s: (-s["numTraders"], -s["totalUsd"]))
    return signals


def merge_previous(signals, previous, now):
    """Conserva firstSeen de senales ya conocidas; devuelve (signals, nuevas)."""
    prev = {s["id"]: s for s in previous}
    new = []
    for s in signals:
        old = prev.get(s["id"])
        s["firstSeen"] = old["firstSeen"] if old else now
        s["lastSeen"] = now
        if not old:
            new.append(s)
    return signals, new


def format_message(new_signals, cap=10):
    lines = ["\U0001F6A8 Nuevas coincidencias de top traders en Polymarket:"]
    for s in new_signals[:cap]:
        lines.append(
            f"\n• {s['numTraders']} traders → {s['title']} [{s['outcome']}]"
            f"\n  precio {s['curPrice']} | entrada media {s['avgEntryPrice']}"
            f" | ${s['totalUsd']:,.0f}"
            f"\n  https://polymarket.com/event/{s['eventSlug']}")
    if len(new_signals) > cap:
        lines.append(f"\n…y {len(new_signals) - cap} mas")
    return "\n".join(lines)


def notify_telegram(new_signals):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID and new_signals):
        return  # sin secrets (p. ej. en local) se omite sin fallar
    body = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": format_message(new_signals),
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30)


def main():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    leaderboard = get_json(LEADERBOARD_URL.format(n=TOP_N))
    positions_by_trader = {}
    for entry in leaderboard:
        wallet = entry["proxyWallet"]
        url = POSITIONS_URL.format(wallet=wallet, threshold=int(MIN_POSITION_USD))
        positions_by_trader[wallet] = {
            "name": entry.get("userName", ""),
            "positions": get_json(url),
        }
        time.sleep(0.3)  # ~50 requests por escaneo: sin prisa, evita rate limits

    signals = detect_signals(positions_by_trader)
    previous = []
    if SIGNALS_PATH.exists():
        previous = json.loads(SIGNALS_PATH.read_text(encoding="utf-8")).get("signals", [])
    signals, new = merge_previous(signals, previous, now)

    SIGNALS_PATH.parent.mkdir(exist_ok=True)
    SIGNALS_PATH.write_text(json.dumps({
        "updatedAt": now,
        "params": {"topN": TOP_N, "minUsers": MIN_USERS, "minPositionUsd": MIN_POSITION_USD},
        "signals": signals,
    }, indent=1), encoding="utf-8")

    notify_telegram(new)
    print(f"{len(signals)} senales activas, {len(new)} nuevas")
    for s in new:
        print(f"  NUEVA: {s['numTraders']} traders -> {s['title']} [{s['outcome']}] @ {s['curPrice']}")


if __name__ == "__main__":
    main()

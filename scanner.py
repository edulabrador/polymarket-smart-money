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
# si el precio actual difiere de la entrada media en mas de esto (en cualquier
# direccion), la senal se marca "stale": al alza los top ya ganaron ese tramo;
# a la baja la tesis de entrada ya no es la actual (p. ej. partido en vivo)
MAX_PRICE_DRIFT = float(os.getenv("MAX_PRICE_DRIFT", "0.15"))
# wallets con mas posiciones abiertas que esto son bots/market makers que
# apuestan a todo: su "coincidencia" no aporta senal
MAX_POSITIONS_PER_TRADER = int(os.getenv("MAX_POSITIONS_PER_TRADER", "200"))
# segunda fuente de oportunidades: compras individuales por encima de esto
WHALE_MIN_USD = float(os.getenv("WHALE_MIN_USD", "50000"))
# whale comprando a cuota improbable = la mas informativa (posible insider)
LONGSHOT_MAX_PRICE = float(os.getenv("LONGSHOT_MAX_PRICE", "0.3"))
# una compra grande de un gran perdedor no es senal: solo se notifica la
# whale con historial ganador (o del top 50). Sin historial suficiente
# (menos de WHALE_MIN_TRACK mercados resueltos en 30 dias) tampoco se avisa.
WHALE_MIN_WINRATE = float(os.getenv("WHALE_MIN_WINRATE", "0.55"))
WHALE_MIN_TRACK = int(os.getenv("WHALE_MIN_TRACK", "5"))
TRACK_WINDOW_DAYS = 30
TRACK_TTL_S = 6 * 3600  # el track record por wallet se recalcula cada 6 h
TRACK_MIN_USD = 100.0   # canjes/perdidas menores son ruido
WHALES_KEEP = 100  # movimientos whale que guarda el JSON para la web
SCAN_EVERY_MIN = 10  # cadencia real del bucle en .github/workflows/scan.yml
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
SIGNALS_PATH = pathlib.Path(__file__).parent / "docs" / "signals.json"

LEADERBOARD_URL = ("https://data-api.polymarket.com/v1/leaderboard"
                   "?timePeriod=MONTH&orderBy=PNL&limit={n}")
# sizeThreshold va en tokens, pero toda posicion con currentValue >= X USD
# tiene al menos X tokens (precio <= 1), asi que sirve de prefiltro seguro.
POSITIONS_URL = ("https://data-api.polymarket.com/positions"
                 "?user={wallet}&sizeThreshold={threshold}&limit=500")
TRADES_URL = ("https://data-api.polymarket.com/trades"
              "?takerOnly=true&filterType=CASH&filterAmount={amount}&limit=100")
ACTIVITY_URL = ("https://data-api.polymarket.com/activity"
                "?user={wallet}&type=REDEEM&limit=500")


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


def detect_signals(positions_by_trader, min_users=MIN_USERS, min_usd=MIN_POSITION_USD,
                   max_drift=MAX_PRICE_DRIFT):
    """positions_by_trader: {wallet: {"name": str, "positions": [dicts de la API]}}.

    Senal = grupo (conditionId, outcomeIndex) con >= min_users traders
    distintos, cada uno con posicion viva de >= min_usd USD actuales.
    Si el precio ya subio mas de max_drift sobre la entrada media, la senal
    se marca stale=True (se muestra descartada y no se notifica).
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
        avg_entry = round(sum(t["avgPrice"] * t["usd"] for t in traders) / total, 4)
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
            "avgEntryPrice": avg_entry,
            "stale": abs(m["curPrice"] - avg_entry) > max_drift,
            "traders": traders,
        })
    signals.sort(key=lambda s: (s["stale"], -s["numTraders"], -s["totalUsd"]))
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


def resolve_history(previous, active_ids, position_index, now):
    """Senales que desaparecieron porque su mercado resolvio -> historico con
    acierto/fallo. position_index: {id: posicion}, incluyendo redeemable.
    Si los traders salieron antes de resolver (no hay posicion redeemable),
    no hay veredicto y no se registra."""
    resolved = []
    for s in previous:
        if s["id"] in active_ids:
            continue
        p = position_index.get(s["id"])
        if p is None or not p.get("redeemable"):
            continue
        resolved.append({
            "id": s["id"], "title": s["title"], "outcome": s["outcome"],
            "eventSlug": s.get("eventSlug", ""),
            "numTraders": s["numTraders"], "avgEntryPrice": s["avgEntryPrice"],
            "stale": s.get("stale", False), "firstSeen": s.get("firstSeen", ""),
            "resolvedAt": now, "won": p["curPrice"] > 0.5,
        })
    return resolved


def detect_whales(trades, last_ts, top_wallets, min_usd=WHALE_MIN_USD):
    """Compras nuevas (timestamp > last_ts) por encima de min_usd USD.
    Los SELL no son oportunidad de entrada y se ignoran. isTop marca si el
    comprador esta ademas en el leaderboard vigilado."""
    whales, seen = [], set()
    for t in trades:
        usd = t["size"] * t["price"]
        if (t["side"] != "BUY" or t["timestamp"] <= last_ts
                or usd < min_usd or t["transactionHash"] in seen):
            continue
        seen.add(t["transactionHash"])
        whales.append({
            "tx": t["transactionHash"],
            "wallet": t["proxyWallet"],
            "name": t.get("name") or t.get("pseudonym") or "",
            "usd": round(usd, 2),
            "price": t["price"],
            "title": t["title"],
            "outcome": t["outcome"],
            "eventSlug": t["eventSlug"],
            "timestamp": t["timestamp"],
            "isTop": t["proxyWallet"] in top_wallets,
            "longshot": t["price"] <= LONGSHOT_MAX_PRICE,
        })
    whales.sort(key=lambda w: -w["timestamp"])
    return whales


def track_record(wallet, posiciones, since_ts, fetch=get_json):
    """(ganados, perdidos) del wallet en la ventana. Ganados = canjes REDEEM
    del feed /activity (canjear implica tener el lado ganador); perdidos =
    posiciones muertas (redeemable a ~0) que siguen en /positions. Mismo
    metodo que backtest.py, aplicado a un solo wallet."""
    since_date = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    redeems = fetch(ACTIVITY_URL.format(wallet=wallet))
    wins = {e["conditionId"] for e in redeems
            if e["timestamp"] >= since_ts and e.get("usdcSize", 0) >= TRACK_MIN_USD}
    losses = {p["conditionId"] for p in posiciones
              if p.get("redeemable") and p.get("curPrice", 1) <= 0.5
              and p.get("initialValue", 0) >= TRACK_MIN_USD
              and (p.get("endDate") or "") >= since_date}
    return len(wins), len(losses)


def win_rate(t):
    """Tasa de acierto, o None si el historial es demasiado corto para fiarse."""
    n = t["wins"] + t["losses"]
    return round(t["wins"] / n, 2) if n >= WHALE_MIN_TRACK else None


def update_track_records(cache, wallets, positions_by_trader, now_ts, fetch=get_json):
    """Refresca el track record solo de los wallets con cache caducada (TTL
    6 h); reutiliza las posiciones ya descargadas del top 50. Devuelve
    (tracks, positions_cache) — el segundo evita re-descargar posiciones de
    wallets whale para calcular su cartera."""
    since_ts = max(now_ts - TRACK_WINDOW_DAYS * 86400, 0)
    tracks, positions_cache = {}, {}
    for w in wallets:
        old = cache.get(w)
        if old and now_ts - old.get("at", 0) < TRACK_TTL_S:
            tracks[w] = old
            continue
        try:
            if w in positions_by_trader:
                posiciones = positions_by_trader[w]["positions"]
            else:
                posiciones = fetch(POSITIONS_URL.format(wallet=w, threshold=100))
                positions_cache[w] = posiciones
                time.sleep(0.3)
            wins, losses = track_record(w, posiciones, since_ts, fetch=fetch)
            tracks[w] = {"wins": wins, "losses": losses, "at": now_ts}
            time.sleep(0.3)
        except Exception:
            tracks[w] = old or {"wins": 0, "losses": 0, "at": 0}
    return tracks, positions_cache


def annotate_win_rates(signals, tracks):
    """Acierto 30d por trader y media por senal: mismo numero de traders
    coincidentes no vale lo mismo si su historial reciente es malo."""
    for s in signals:
        rates = []
        for t in s["traders"]:
            r = win_rate(tracks.get(t["wallet"], {"wins": 0, "losses": 0}))
            t["winRate"] = r
            if r is not None:
                rates.append(r)
        s["avgWinRate"] = round(sum(rates) / len(rates), 2) if rates else None
    return signals


def enrich_whales(new_whales, prev_whales, tracks, positions_cache, positions_by_trader):
    """Contexto por wallet: cartera abierta, recurrencia y acierto 30d."""
    counts = {}
    for w in prev_whales:
        counts[w["wallet"]] = counts.get(w["wallet"], 0) + 1
    for w in new_whales:
        w["repeatBuys"] = counts.get(w["wallet"], 0) + 1
        counts[w["wallet"]] = w["repeatBuys"]
        posiciones = (positions_cache.get(w["wallet"])
                      or positions_by_trader.get(w["wallet"], {}).get("positions"))
        w["walletUsd"] = (round(sum(p.get("currentValue", 0) for p in posiciones), 2)
                          if posiciones is not None else None)
        t = tracks.get(w["wallet"])
        w["wins30d"] = t["wins"] if t else 0
        w["losses30d"] = t["losses"] if t else 0
        w["winRate"] = win_rate(t) if t else None
    return new_whales


def whale_notifiable(w):
    """Solo se avisa de whales con historial ganador (o del top 50): una
    compra de $300k de alguien que va perdiendo no es dinero inteligente."""
    return w["isTop"] or (w.get("winRate") is not None
                          and w["winRate"] >= WHALE_MIN_WINRATE)


def format_whales(whales, cap=10):
    lines = ["\U0001F40B Compras grandes en Polymarket:"]
    for w in whales[:cap]:
        top = " (TOP 50)" if w["isTop"] else ""
        insider = " \U0001F575 LONGSHOT" if w.get("longshot") else ""
        quien = w["name"] or w["wallet"][:10]
        extra = ""
        if w.get("winRate") is not None:
            extra += (f" | acierto 30d {round(w['winRate'] * 100)}%"
                      f" ({w['wins30d']}✓ {w['losses30d']}✗)")
        if w.get("walletUsd"):
            extra += f" | cartera ${w['walletUsd']:,.0f}"
        if w.get("repeatBuys", 0) > 1:
            extra += f" | {w['repeatBuys']}ª compra grande"
        lines.append(
            f"\n• ${w['usd']:,.0f} → {w['title']} [{w['outcome']}] @ {w['price']}{insider}"
            f"\n  por {quien}{top}{extra}"
            f"\n  https://polymarket.com/event/{w['eventSlug']}")
    if len(whales) > cap:
        lines.append(f"\n…y {len(whales) - cap} mas")
    return "\n".join(lines)


def format_message(new_signals, cap=10):
    lines = ["\U0001F6A8 Nuevas coincidencias de top traders en Polymarket:"]
    for s in new_signals[:cap]:
        acierto = (f" | acierto medio 30d {round(s['avgWinRate'] * 100)}%"
                   if s.get("avgWinRate") is not None else "")
        lines.append(
            f"\n• {s['numTraders']} traders → {s['title']} [{s['outcome']}]"
            f"\n  precio {s['curPrice']} | entrada media {s['avgEntryPrice']}"
            f" | ${s['totalUsd']:,.0f}{acierto}"
            f"\n  https://polymarket.com/event/{s['eventSlug']}")
    if len(new_signals) > cap:
        lines.append(f"\n…y {len(new_signals) - cap} mas")
    return "\n".join(lines)


def recipients():
    """Destinatarios de alertas, cada uno con sus umbrales. Formatos:
    - TELEGRAM_RECIPIENTS (JSON): [{"chatId": "...", "minUsers": 7,
      "whaleMinUsd": 100000}, ...] — umbrales opcionales, caen al global.
    - TELEGRAM_CHAT_ID: uno o varios chat ids separados por comas.
    """
    raw = os.getenv("TELEGRAM_RECIPIENTS", "")
    if raw:
        return [{"chatId": str(r["chatId"]),
                 "minUsers": r.get("minUsers", MIN_USERS),
                 "whaleMinUsd": r.get("whaleMinUsd", WHALE_MIN_USD)}
                for r in json.loads(raw)]
    return [{"chatId": c.strip(), "minUsers": MIN_USERS, "whaleMinUsd": WHALE_MIN_USD}
            for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]


def send_telegram(text, chat_id):
    if not (TELEGRAM_TOKEN and chat_id and text):
        return  # sin secrets (p. ej. en local) se omite sin fallar
    body = json.dumps({
        "chat_id": chat_id,
        "text": text,
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

    # los bots/market makers (cientos de posiciones) no aportan senal
    scannable = {w: info for w, info in positions_by_trader.items()
                 if len(info["positions"]) <= MAX_POSITIONS_PER_TRADER}
    signals = detect_signals(scannable)

    doc = {}
    if SIGNALS_PATH.exists():
        doc = json.loads(SIGNALS_PATH.read_text(encoding="utf-8"))
    previous, history = doc.get("signals", []), doc.get("history", [])
    signals, new = merge_previous(signals, previous, now)

    # segunda fuente: compras individuales gigantes (feed global de trades)
    trades = get_json(TRADES_URL.format(amount=int(WHALE_MIN_USD)))
    last_whale_ts = doc.get("lastWhaleTs", 0)
    new_whales = detect_whales(trades, last_whale_ts, set(positions_by_trader))
    now_ts = int(time.time())
    wallets_needed = set(positions_by_trader) | {w["wallet"] for w in new_whales}
    tracks, positions_cache = update_track_records(
        doc.get("trackRecords", {}), wallets_needed, positions_by_trader, now_ts)
    enrich_whales(new_whales, doc.get("whales", []), tracks, positions_cache,
                  positions_by_trader)
    annotate_win_rates(signals, tracks)
    first_run = "lastWhaleTs" not in doc  # primera vez: fijar marca sin avisar
    whales = (new_whales + doc.get("whales", []))[:WHALES_KEEP]
    last_whale_ts = max([last_whale_ts] + [t["timestamp"] for t in trades])

    # indice de TODAS las posiciones (incluidas redeemable) para dar veredicto
    # a las senales cuyo mercado ya resolvio
    position_index = {}
    for info in positions_by_trader.values():
        for p in info["positions"]:
            key = f'{p["conditionId"]}:{p["outcomeIndex"]}'
            if key not in position_index or p.get("redeemable"):
                position_index[key] = p
    history += resolve_history(previous, {s["id"] for s in signals}, position_index, now)

    SIGNALS_PATH.parent.mkdir(exist_ok=True)
    SIGNALS_PATH.write_text(json.dumps({
        "updatedAt": now,
        "params": {"topN": TOP_N, "minUsers": MIN_USERS, "minPositionUsd": MIN_POSITION_USD,
                   "maxPriceDrift": MAX_PRICE_DRIFT, "scanEveryMin": SCAN_EVERY_MIN,
                   "maxPositionsPerTrader": MAX_POSITIONS_PER_TRADER,
                   "whaleMinUsd": WHALE_MIN_USD},
        "signals": signals,
        "whales": whales,
        "lastWhaleTs": last_whale_ts,
        "trackRecords": tracks,
        "history": history,
    }, indent=1), encoding="utf-8")

    fresh_new = [s for s in new if not s["stale"]]
    for r in recipients():
        mias = [s for s in fresh_new if s["numTraders"] >= r["minUsers"]]
        if mias:
            send_telegram(format_message(mias), r["chatId"])
        grandes = [w for w in new_whales
                   if w["usd"] >= r["whaleMinUsd"] and whale_notifiable(w)]
        if grandes and not first_run:
            send_telegram(format_whales(grandes), r["chatId"])
    stale_count = sum(1 for s in signals if s["stale"])
    bots = len(positions_by_trader) - len(scannable)
    aciertos = sum(1 for h in history if h["won"])
    notificables = sum(1 for w in new_whales if whale_notifiable(w))
    print(f"{len(signals)} senales ({stale_count} stale), {len(new)} nuevas, "
          f"{len(fresh_new)} notificadas | {len(new_whales)} whales nuevas "
          f"({notificables} con historial ganador) | "
          f"{bots} wallets excluidos por bot | "
          f"historico {aciertos}/{len(history)} aciertos")
    for s in new:
        tag = "STALE" if s["stale"] else "NUEVA"
        print(f"  {tag}: {s['numTraders']} traders -> {s['title']} [{s['outcome']}] @ {s['curPrice']}")


if __name__ == "__main__":
    main()

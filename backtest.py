"""Backtest del algoritmo de coincidencias en DOS NIVELES con mercados ya
resueltos (ventana de 30 dias). Responde a la pregunta clave del cambio:
el gate de especialistas sobre 3-4 traders, ¿acierta o es ruido?

Reconstruye coincidencias historicas (>= 3 traders del top en el mismo lado de
un mercado resuelto) y las clasifica igual que el escaner en vivo:

- **amplio**: >= MIN_USERS (5) traders.
- **especialistas**: 3-4 traders cuyo acierto 30d PROBADO (categoria si hay
  muestra, si no global) es >= SPECIALIST_MIN_WINRATE (60%).
- **descartada**: 3-4 traders sin acierto probado alto (lo que el algoritmo
  NO dispara) — se mide para ver si el gate de verdad separa ganadoras de
  casi-coin-flips.

Fuentes (mismo truco que antes, por el sesgo de supervivencia: las ganadoras
se canjean y desaparecen de /positions):
- **Ganadas**: canjes REDEEM del feed /activity (canjear con payout implica
  tener el lado ganador). REDEEM no trae outcomeIndex, pero agrupar por
  conditionId ya junta a quienes coincidieron en la ganadora.
- **Perdidas**: posiciones redeemable con curPrice ~0 que siguen en /positions.

Anti-lookahead: al calcular el acierto de un trader se EXCLUYE el propio
mercado que se esta puntuando (leave-one-out); el resultado que intentamos
predecir no ayuda a cualificar a sus traders.

Mide TASA DE ACIERTO, no ROI: los precios de entrada de mercados ya resueltos
no son recuperables (las ganadoras se canjearon), asi que el ROI real de cada
señal historica no se puede reconstruir. La tasa de acierto por nivel es lo
que responde a "¿es fiable?"; la rentabilidad la mide el ROI en vivo del
escaner (docs/signals.json) bajo el gate auto-corrector.

Corre en Actions (workflow `backtest`) y escribe docs/backtest.json.
"""
import json
import time
from datetime import datetime, timezone

from scanner import (ACTIVITY_URL, LEADERBOARD_URL, MIN_POSITION_USD, MIN_USERS,
                     MIN_USERS_SPECIALIST, POSITIONS_URL, SIGNALS_PATH,
                     SPECIALIST_MIN_WINRATE, TOP_N, TRACK_MIN_USD,
                     WHALE_MIN_TRACK, categoria, get_json)

OUT = SIGNALS_PATH.parent / "backtest.json"
WINDOW_DAYS = 30


def wallet_records(redeems_by_trader, positions_by_trader, since_ts):
    """Por wallet en la ventana: {'wins': {conditionId: categoria},
    'losses': {conditionId: categoria}}. Base del acierto probado de cada
    trader — mismo criterio que scanner.track_record, pero guardando los
    conditionId para poder excluir el mercado puntuado (leave-one-out)."""
    since_date = (datetime.fromtimestamp(since_ts, tz=timezone.utc)
                  .strftime("%Y-%m-%d") if since_ts else "")
    rec = {}
    for wallet, eventos in redeems_by_trader.items():
        r = rec.setdefault(wallet, {"wins": {}, "losses": {}})
        for e in eventos:
            if (e.get("type") != "REDEEM" or e.get("timestamp", 0) < since_ts
                    or e.get("usdcSize", 0) < TRACK_MIN_USD):
                continue
            r["wins"][e["conditionId"]] = categoria(e.get("title", ""))
    for wallet, posiciones in positions_by_trader.items():
        r = rec.setdefault(wallet, {"wins": {}, "losses": {}})
        for p in posiciones:
            if (p.get("redeemable") and p.get("curPrice", 1) <= 0.5
                    and p.get("initialValue", 0) >= TRACK_MIN_USD
                    and (p.get("endDate") or "") >= since_date):
                r["losses"][p["conditionId"]] = categoria(p.get("title", ""))
    return rec


def _winrate(rec, wallet, cat, exclude):
    """Acierto del wallet (en la categoria cat, o global si cat=None),
    excluyendo el conditionId `exclude`. None si la muestra es < WHALE_MIN_TRACK."""
    r = rec.get(wallet)
    if not r:
        return None
    def cnt(d):
        return sum(1 for c, k in d.items()
                   if c != exclude and (cat is None or k == cat))
    w, l = cnt(r["wins"]), cnt(r["losses"])
    n = w + l
    return w / n if n >= WHALE_MIN_TRACK else None


def avg_proven_winrate(rec, wallets, cat, cond):
    """Acierto medio probado de los traders que coincidieron: la categoria del
    mercado si hay muestra, si no el global (mismo orden que qualify_signals)."""
    cat_rates = [x for x in (_winrate(rec, w, cat, cond) for w in wallets)
                 if x is not None]
    if cat_rates:
        return sum(cat_rates) / len(cat_rates)
    glob_rates = [x for x in (_winrate(rec, w, None, cond) for w in wallets)
                  if x is not None]
    return sum(glob_rates) / len(glob_rates) if glob_rates else None


def coincidences(redeems_by_trader, positions_by_trader,
                 min_users=MIN_USERS_SPECIALIST, min_usd=MIN_POSITION_USD, since_ts=0):
    """Coincidencias en mercados resueltos: >= min_users traders del top en el
    mismo lado. usdcSize (payout) en canjes, initialValue (lo invertido) en
    perdedoras — las perdedoras valen $0 hoy y currentValue sesgaria la muestra."""
    since_date = (datetime.fromtimestamp(since_ts, tz=timezone.utc)
                  .strftime("%Y-%m-%d") if since_ts else "")
    groups = {}
    for wallet, eventos in redeems_by_trader.items():
        for e in eventos:
            if (e.get("type") != "REDEEM" or e["timestamp"] < since_ts
                    or e.get("usdcSize", 0) < min_usd):
                continue
            g = groups.setdefault(e["conditionId"] + ":win", {
                "cond": e["conditionId"], "title": e["title"],
                "outcome": "(ganadora)", "cat": categoria(e.get("title", "")),
                "won": True, "wallets": set()})
            g["wallets"].add(wallet)
    for wallet, posiciones in positions_by_trader.items():
        for p in posiciones:
            if (not p.get("redeemable") or p.get("initialValue", 0) < min_usd
                    or p.get("curPrice", 0) > 0.5
                    or (p.get("endDate") or "") < since_date):
                continue
            g = groups.setdefault(f'{p["conditionId"]}:{p["outcomeIndex"]}', {
                "cond": p["conditionId"], "title": p["title"],
                "outcome": p["outcome"], "cat": categoria(p.get("title", "")),
                "won": False, "wallets": set()})
            g["wallets"].add(wallet)
    return [g for g in groups.values() if len(g["wallets"]) >= min_users]


def classify(groups, rec, min_broad=MIN_USERS, min_spec=MIN_USERS_SPECIALIST,
             min_wr=SPECIALIST_MIN_WINRATE):
    """Etiqueta cada coincidencia con su nivel — misma logica que
    qualify_signals — anotando nº de traders y su acierto medio probado."""
    for g in groups:
        n = len(g["wallets"])
        wr = avg_proven_winrate(rec, g["wallets"], g["cat"], g["cond"])
        if n >= min_broad:
            tier = "amplio"
        elif n >= min_spec and wr is not None and wr >= min_wr:
            tier = "especialistas"
        else:
            tier = "descartada"
        g["tier"] = tier
        g["numTraders"] = n
        g["avgWinRate"] = round(wr, 2) if wr is not None else None
    return groups


def main():
    since_ts = int(time.time()) - WINDOW_DAYS * 86400
    leaderboard = get_json(LEADERBOARD_URL.format(n=TOP_N))
    redeems, positions = {}, {}
    for entry in leaderboard:
        wallet = entry["proxyWallet"]
        positions[wallet] = get_json(POSITIONS_URL.format(wallet=wallet, threshold=1))
        redeems[wallet] = get_json(ACTIVITY_URL.format(wallet=wallet))
        time.sleep(0.3)

    rec = wallet_records(redeems, positions, since_ts)
    groups = classify(coincidences(redeems, positions, since_ts=since_ts), rec)

    tiers = {}
    for g in groups:
        t = tiers.setdefault(g["tier"], {"won": 0, "total": 0})
        t["total"] += 1
        t["won"] += 1 if g["won"] else 0
    # el tile de la web muestra lo que el algoritmo DISPARA (amplio + especialistas)
    notif = [g for g in groups if g["tier"] in ("amplio", "especialistas")]
    won = sum(1 for g in notif if g["won"])
    signals = [{"id": g["cond"], "title": g["title"], "outcome": g["outcome"],
                "numTraders": g["numTraders"], "won": g["won"],
                "tier": g["tier"], "avgWinRate": g["avgWinRate"]}
               for g in sorted(groups, key=lambda x: (x["tier"], -x["numTraders"]))]
    OUT.write_text(json.dumps({
        "ranAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "redeem+positions",
        "windowDays": WINDOW_DAYS,
        "params": {"topN": TOP_N, "minUsers": MIN_USERS,
                   "minUsersSpecialist": MIN_USERS_SPECIALIST,
                   "specialistMinWinRate": SPECIALIST_MIN_WINRATE,
                   "minPositionUsd": MIN_POSITION_USD},
        "won": won,
        "total": len(notif),
        "tiers": tiers,
        "signals": signals,
    }, indent=1), encoding="utf-8")

    def linea(t, d):
        pct = round(100 * d["won"] / d["total"]) if d["total"] else 0
        return f"  {t}: {d['won']}/{d['total']} ({pct}%)"
    print(f"backtest: notifiadas {won}/{len(notif)} acertadas "
          f"({round(100 * won / len(notif)) if notif else 0}%)")
    for t in ("amplio", "especialistas", "descartada"):
        if t in tiers:
            print(linea(t, tiers[t]))


if __name__ == "__main__":
    main()

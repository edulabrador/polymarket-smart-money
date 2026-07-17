"""Backtest de la hipotesis con mercados YA resueltos (ventana de 30 dias).

El primer intento (solo posiciones redeemable) daba 0% de acierto por sesgo
de supervivencia: las ganadoras se canjean y desaparecen de /positions, las
perdedoras (valen $0) se quedan para siempre. Solucion con dos fuentes:

- **Ganadas**: eventos REDEEM del feed /activity — canjear con payout > 0
  implica que tenias el lado ganador. El evento no trae outcomeIndex util
  (siempre 999), pero no hace falta: agrupar canjes por conditionId ya junta
  a los que coincidieron en la ganadora.
- **Perdidas**: posiciones redeemable con curPrice ~0 que siguen en /positions.

Misma ventana temporal para ambos lados. Corre en Actions (workflow
`backtest`) y escribe docs/backtest.json, que la web muestra como tile.
"""
import json
import time
from datetime import datetime, timezone

from scanner import (ACTIVITY_URL, LEADERBOARD_URL, MIN_POSITION_USD,
                     MIN_USERS, POSITIONS_URL, SIGNALS_PATH, TOP_N, get_json)

OUT = SIGNALS_PATH.parent / "backtest.json"
WINDOW_DAYS = 30


def backtest_signals(redeems_by_trader, positions_by_trader,
                     min_users=MIN_USERS, min_usd=MIN_POSITION_USD, since_ts=0):
    """redeems_by_trader: {wallet: [eventos REDEEM]};
    positions_by_trader: {wallet: [posiciones de la API]}.

    El filtro de tamano usa usdcSize (payout) en canjes e initialValue (lo
    invertido) en perdedoras: las perdedoras valen $0 hoy y currentValue
    sesgaria la muestra. Ganadoras sin canjear se excluyen para no contar el
    mismo mercado dos veces: contaran cuando su dueno las canjee."""
    since_date = (datetime.fromtimestamp(since_ts, tz=timezone.utc)
                  .strftime("%Y-%m-%d") if since_ts else "")
    groups = {}
    for wallet, eventos in redeems_by_trader.items():
        for e in eventos:
            if e["timestamp"] < since_ts or e.get("usdcSize", 0) < min_usd:
                continue
            key = e["conditionId"] + ":win"
            g = groups.setdefault(key, {"title": e["title"],
                                        "outcome": "(ganadora)",
                                        "won": True, "wallets": set()})
            g["wallets"].add(wallet)
    for wallet, posiciones in positions_by_trader.items():
        for p in posiciones:
            if (not p.get("redeemable") or p.get("initialValue", 0) < min_usd
                    or p.get("curPrice", 0) > 0.5
                    or (p.get("endDate") or "") < since_date):
                continue
            key = f'{p["conditionId"]}:{p["outcomeIndex"]}'
            g = groups.setdefault(key, {"title": p["title"],
                                        "outcome": p["outcome"],
                                        "won": False, "wallets": set()})
            g["wallets"].add(wallet)

    out = [{"id": k, "title": g["title"], "outcome": g["outcome"],
            "numTraders": len(g["wallets"]), "won": g["won"]}
           for k, g in groups.items() if len(g["wallets"]) >= min_users]
    out.sort(key=lambda s: -s["numTraders"])
    return out


def main():
    since_ts = int(time.time()) - WINDOW_DAYS * 86400
    leaderboard = get_json(LEADERBOARD_URL.format(n=TOP_N))
    redeems, positions = {}, {}
    for entry in leaderboard:
        wallet = entry["proxyWallet"]
        positions[wallet] = get_json(POSITIONS_URL.format(wallet=wallet, threshold=1))
        redeems[wallet] = get_json(ACTIVITY_URL.format(wallet=wallet))
        time.sleep(0.3)

    senales = backtest_signals(redeems, positions, since_ts=since_ts)
    ganadas = sum(1 for s in senales if s["won"])
    OUT.write_text(json.dumps({
        "ranAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "redeem+positions",
        "windowDays": WINDOW_DAYS,
        "params": {"topN": TOP_N, "minUsers": MIN_USERS,
                   "minPositionUsd": MIN_POSITION_USD},
        "won": ganadas,
        "total": len(senales),
        "signals": senales,
    }, indent=1), encoding="utf-8")
    print(f"backtest: {ganadas}/{len(senales)} senales historicas acertadas")


if __name__ == "__main__":
    main()

"""Backtest puntual de la hipotesis con mercados YA resueltos.

Toma el top del leaderboard y sus posiciones redeemable (resueltas y aun sin
canjear): si >= MIN_USERS coincidieron en el mismo resultado, cuenta como
senal historica y mira si gano. Corre en Actions (workflow `backtest`) y
escribe docs/backtest.json, que la web muestra como estimacion inicial.

ponytail: la muestra son posiciones resueltas sin canjear -- quien canjea
rapido desaparece de ella, asi que es una foto parcial, no un backtest
completo; suficiente como primera estimacion mientras el historico en vivo
acumula datos.
"""
import json
import time
from datetime import datetime, timezone

from scanner import (LEADERBOARD_URL, MIN_POSITION_USD, MIN_USERS,
                     POSITIONS_URL, SIGNALS_PATH, TOP_N, get_json)

OUT = SIGNALS_PATH.parent / "backtest.json"


def backtest_signals(positions_by_trader, min_users=MIN_USERS, min_usd=MIN_POSITION_USD):
    """positions_by_trader: {wallet: [posiciones de la API]}. Agrupa las
    redeemable por (conditionId, outcome). El filtro de tamano usa
    initialValue: las perdedoras valen $0 hoy y filtrar por currentValue
    dejaria solo ganadoras (100% de acierto falso)."""
    groups = {}
    for wallet, posiciones in positions_by_trader.items():
        for p in posiciones:
            if not p.get("redeemable") or p.get("initialValue", 0) < min_usd:
                continue
            key = f'{p["conditionId"]}:{p["outcomeIndex"]}'
            g = groups.setdefault(key, {"meta": p, "wallets": set()})
            g["wallets"].add(wallet)

    out = []
    for key, g in groups.items():
        if len(g["wallets"]) < min_users:
            continue
        m = g["meta"]
        out.append({"id": key, "title": m["title"], "outcome": m["outcome"],
                    "numTraders": len(g["wallets"]), "won": m["curPrice"] > 0.5})
    out.sort(key=lambda s: -s["numTraders"])
    return out


def main():
    leaderboard = get_json(LEADERBOARD_URL.format(n=TOP_N))
    positions_by_trader = {}
    for entry in leaderboard:
        wallet = entry["proxyWallet"]
        positions_by_trader[wallet] = get_json(
            POSITIONS_URL.format(wallet=wallet, threshold=1))
        time.sleep(0.3)

    senales = backtest_signals(positions_by_trader)
    ganadas = sum(1 for s in senales if s["won"])
    OUT.write_text(json.dumps({
        "ranAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "params": {"topN": TOP_N, "minUsers": MIN_USERS,
                   "minPositionUsd": MIN_POSITION_USD},
        "won": ganadas,
        "total": len(senales),
        "signals": senales,
    }, indent=1), encoding="utf-8")
    print(f"backtest: {ganadas}/{len(senales)} senales historicas acertadas")


if __name__ == "__main__":
    main()

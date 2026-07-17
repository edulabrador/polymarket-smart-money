"""Fase 0: verifica los endpoints reales de Polymarket y guarda fixtures.

Se ejecuta en GitHub Actions (Polymarket esta bloqueado por la DGOJ en redes
espanolas). Escribe tests/fixtures/*.json y NOTES.md con lo confirmado.
"""
import json
import pathlib
import urllib.request

FIXTURES = pathlib.Path("tests/fixtures")

LEADERBOARD_CANDIDATES = [
    "https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=50",
    "https://data-api.polymarket.com/v1/leaderboard?timePeriod=ALL&orderBy=PNL&limit=50",
    "https://data-api.polymarket.com/v1/leaderboard?window=30d&rankType=pnl&limit=50",
    "https://data-api.polymarket.com/v1/leaderboard",
]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "smart-money-probe/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    FIXTURES.mkdir(parents=True, exist_ok=True)
    report = ["# NOTES - Fase 0 (generado por probe.py en GitHub Actions)", ""]

    leaderboard, lb_url = None, None
    for url in LEADERBOARD_CANDIDATES:
        try:
            data = get(url)
        except Exception as e:
            report.append(f"- FALLO `{url}` -> {e}")
            continue
        entries = data if isinstance(data, list) else data.get("data") or data.get("entries")
        if entries:
            report.append(f"- OK `{url}` -> {len(entries)} entradas, campos: {sorted(entries[0].keys())}")
            if leaderboard is None:
                leaderboard, lb_url = entries, url
        else:
            report.append(f"- VACIO `{url}` -> {str(data)[:200]}")

    assert leaderboard, "Ningun endpoint de leaderboard funciono:\n" + "\n".join(report)
    (FIXTURES / "leaderboard.json").write_text(json.dumps(leaderboard, indent=1), encoding="utf-8")
    report += ["", f"**Leaderboard elegido**: `{lb_url}`", ""]

    wallet_key = next(k for k in ("proxyWallet", "wallet", "address", "user") if k in leaderboard[0])
    positions = None
    for entry in leaderboard[:5]:
        wallet = entry[wallet_key]
        url = f"https://data-api.polymarket.com/positions?user={wallet}&sizeThreshold=100&limit=500"
        try:
            data = get(url)
        except Exception as e:
            report.append(f"- FALLO positions de `{wallet}`: {e}")
            continue
        if data:
            positions = data
            report.append(f"- OK positions de `{wallet}` -> {len(data)} posiciones, campos: {sorted(data[0].keys())}")
            break
        report.append(f"- VACIO positions de `{wallet}`")

    assert positions, "No se pudieron obtener posiciones:\n" + "\n".join(report)
    (FIXTURES / "positions.json").write_text(json.dumps(positions, indent=1), encoding="utf-8")

    report.append("")
    trades = None
    for url in [
        "https://data-api.polymarket.com/trades?limit=5&takerOnly=true&filterType=CASH&filterAmount=10000",
        "https://data-api.polymarket.com/trades?limit=5",
        "https://clob.polymarket.com/trades?limit=5",
    ]:
        try:
            data = get(url)
        except Exception as e:
            report.append(f"- FALLO `{url}` -> {e}")
            continue
        entries = data if isinstance(data, list) else data.get("data")
        if entries:
            report.append(f"- OK `{url}` -> {len(entries)} trades, campos: {sorted(entries[0].keys())}")
            if trades is None:
                trades = entries
        else:
            report.append(f"- VACIO `{url}` -> {str(data)[:200]}")
    if trades:
        (FIXTURES / "trades.json").write_text(json.dumps(trades, indent=1), encoding="utf-8")

    # actividad REDEEM: canjear una posicion resuelta = la ganaste. Es la
    # unica forma de ver los mercados ganados (las posiciones ganadoras
    # desaparecen de /positions al canjearse)
    report.append("")
    activity = None
    for entry in leaderboard[:5]:
        wallet = entry[wallet_key]
        url = f"https://data-api.polymarket.com/activity?user={wallet}&type=REDEEM&limit=100"
        try:
            data = get(url)
        except Exception as e:
            report.append(f"- FALLO activity de `{wallet}`: {e}")
            continue
        entries = data if isinstance(data, list) else data.get("data")
        if entries:
            activity = entries
            report.append(f"- OK activity REDEEM de `{wallet}` -> {len(entries)} eventos, campos: {sorted(entries[0].keys())}")
            report.append(f"  ejemplo: {json.dumps(entries[0])[:400]}")
            break
        report.append(f"- VACIO activity de `{wallet}`")
    if activity:
        (FIXTURES / "activity.json").write_text(json.dumps(activity, indent=1), encoding="utf-8")

    pathlib.Path("NOTES.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))


if __name__ == "__main__":
    main()

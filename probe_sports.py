"""Sonda temporal: como expone gamma los mercados deportivos (ligas, eventos
y mercados de partido). Escribe probe_out/sports.md. Se ejecuta en Actions."""
import json
import pathlib
import urllib.request

OUT = pathlib.Path("probe_out")
G = "https://gamma-api.polymarket.com"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "smart-money-probe/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    OUT.mkdir(exist_ok=True)
    rep = []
    try:
        sports = get(f"{G}/sports")
        rep.append(f"## /sports: {len(sports)} entradas, campos {sorted(sports[0].keys())}")
        for s in sports:
            rep.append("- " + json.dumps(s)[:300])
    except Exception as e:
        rep.append(f"FALLO /sports: {e}")
        sports = []
    keys = ("epl", "lal", "ucl", "uel", "bun", "sea", "fl1", "nfl", "mls")
    for s in sports:
        if s.get("sport") not in keys:
            continue
        url = f"{G}/events?series_id={s.get('series')}&closed=false&active=true&limit=4&order=startDate&ascending=true"
        try:
            evs = get(url)
        except Exception as e:
            rep.append(f"FALLO {url}: {e}")
            continue
        rep.append(f"\n## {s.get('sport')} series={s.get('series')}: {len(evs)} eventos ({url})")
        for ev in evs[:3]:
            rep.append(f"### {ev.get('title')} | slug {ev.get('slug')} | start {ev.get('startDate')} "
                       f"| endDate {ev.get('endDate')} | campos evento {sorted(ev.keys())}")
            for m in ev.get("markets", [])[:8]:
                rep.append("  - " + json.dumps({k: m.get(k) for k in (
                    "question", "conditionId", "outcomes", "outcomePrices", "bestAsk", "bestBid",
                    "lastTradePrice", "gameStartTime", "endDate", "sportsMarketType",
                    "groupItemTitle", "clobTokenIds", "active", "closed", "liquidityNum")}))
    for url in (f"{G}/events?tag_slug=soccer&closed=false&limit=3",
                f"{G}/markets?sports_market_types=moneyline&closed=false&limit=3"):
        try:
            d = get(url)
            rep.append(f"\n## {url}: {len(d)}")
            for x in d[:3]:
                rep.append("  - " + json.dumps(x)[:700])
        except Exception as e:
            rep.append(f"FALLO {url}: {e}")
    (OUT / "sports.md").write_text("\n".join(rep) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

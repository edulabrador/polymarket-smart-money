"""Backtest por REPLAY del historial de git: lo que el escaner vio de verdad.

Cada escaneo hace commit de docs/signals.json, asi que el historial de git es
una grabacion punto a punto de las senales que existian en cada momento y de
su precio. Este backtest recorre esas versiones en orden y aplica las reglas
de alerta ACTUALES a lo que habia en cada instante:

- Una senal se detecta la primera vez que aparece (precio de deteccion).
- Se alerta UNA vez, en la primera version en que es fresca (no descartada
  por deriva) y su precio es < MAX_ALERT_PRICE. El seguidor compra a ese
  precio + ENTRY_COST (spread/deslizamiento; el libro de ordenes de entonces
  no se guardo). Variante "con retraso": al precio de la version siguiente.
- El resultado lo da el oraculo (gamma: mercado liquidado y pago por
  resultado), NUNCA las posiciones de los traders. Asi no hay sesgo de
  supervivencia: una perdedora cuenta aunque sus traders hayan salido.

Por que es fiable (y el backtest anterior no): no mira el futuro. El anterior
elegia a los traders con el leaderboard de HOY (seleccionado ya por cuanto
ganaron) y reconstruia sus coincidencias a posteriori sin precio de entrada.
Aqui solo se usa lo que existia en cada momento y el precio real de entonces.

Limites conocidos (se publican junto al resultado):
- Las reglas de deteccion de cada momento son las que corrian entonces (p. ej.
  antes del 22-07-2026 solo existia el consenso amplio de >= 5 traders).
- La hora de resolucion se aproxima por la ultima vez que se vio la senal.
- Sin el libro historico, la entrada es precio + ENTRY_COST: se publica la
  sensibilidad del ROI a distintos costes.

Corre en Actions (workflow `backtest`, necesita historial completo:
fetch-depth 0) y escribe docs/backtest.json.
"""
import json
import os
import pathlib
import subprocess
from datetime import datetime, timezone

import metrics
from scanner import (ENTRY_COST, MAX_ALERT_PRICE, MIN_EDGE, MIN_USERS, PAPER_STAKE,
                     SIGNALS_PATH, SOURCE_MIN_SAMPLE, categoria,
                     fetch_market_resolutions)

REPO = pathlib.Path(__file__).resolve().parent
OUT = SIGNALS_PATH.parent / "backtest.json"
DRY_RUN = os.getenv("DRY_RUN", "") not in ("", "0")
COSTS = (0.0, 0.01, 0.02, 0.03)


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def snapshots(repo=REPO, path="docs/signals.json"):
    """(timestamp, doc) de cada version de `path` en el historial, en orden."""
    revs = subprocess.run(
        ["git", "-C", str(repo), "log", "--reverse", "--format=%H %ct", "--", path],
        capture_output=True, text=True, check=True).stdout.split("\n")
    proc = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for line in revs:
            if not line.strip():
                continue
            sha, ct = line.split()
            proc.stdin.write(f"{sha}:{path}\n".encode())
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            if len(header) < 3 or header[1] == b"missing":
                continue
            data = proc.stdout.read(int(header[2]))
            proc.stdout.read(1)
            try:
                yield int(ct), json.loads(data)
            except ValueError:
                continue
    finally:
        proc.stdin.close()
        proc.wait()


def tier_of(s):
    # antes de los dos niveles solo se guardaban senales de >= MIN_USERS
    return s.get("tier") or ("amplio" if s.get("numTraders", 0) >= MIN_USERS else None)


def alertable_snapshot(s, max_price=MAX_ALERT_PRICE, min_edge=MIN_EDGE):
    """Misma regla que scanner.alertable sobre una version guardada."""
    if s.get("stale") or s.get("curPrice") is None or s["curPrice"] >= max_price:
        return False
    if min_edge is not None and s.get("edgeEst") is not None:
        return s["edgeEst"] >= min_edge
    return True


def replay(snaps):
    """Una fila por senal (id = mercado:resultado) con su deteccion, su alerta
    (si la hubo segun las reglas actuales) y precios de entonces."""
    rows = {}
    pending_delay = {}
    for ts, doc in snaps:
        present = set()
        for s in doc.get("signals", []):
            sid, cur = s.get("id"), s.get("curPrice")
            if not sid or cur is None:
                continue
            present.add(sid)
            cond, _, idx = sid.partition(":")
            r = rows.get(sid)
            if r is None:
                r = rows[sid] = {
                    "id": sid, "conditionId": cond, "outcomeIndex": int(idx or 0),
                    "title": s.get("title", ""), "outcome": s.get("outcome", ""),
                    "eventSlug": s.get("eventSlug", ""),
                    "cat": s.get("cat") or categoria(s.get("title", "")),
                    "firstTs": ts, "detectPrice": cur, "tierAtDetection": tier_of(s),
                    "staleAtDetection": bool(s.get("stale")), "maxTraders": 0,
                    "alerted": False}
            r["lastTs"], r["lastPrice"] = ts, cur
            r["maxTraders"] = max(r["maxTraders"], s.get("numTraders", 0))
            if sid in pending_delay:
                r["alertPriceDelayed"] = cur
                del pending_delay[sid]
            if not r["alerted"] and alertable_snapshot(s):
                r.update({"alerted": True, "alertTs": ts, "alertPrice": cur,
                          "tierAtAlert": tier_of(s), "numTradersAtAlert": s.get("numTraders"),
                          "edgeEst": s.get("edgeEst")})
                pending_delay[sid] = True
        # si desaparecio antes de la version siguiente, sin precio con retraso
        for sid in list(pending_delay):
            if sid not in present:
                del pending_delay[sid]
    return list(rows.values())


def evaluate(rows, resolutions, cost=ENTRY_COST):
    """Anota pago y ROI en cada fila. Entradas siempre con coste incluido."""
    for r in rows:
        pays = resolutions.get(r["conditionId"])
        if not pays or r["outcomeIndex"] >= len(pays):
            r["status"] = "pendiente"
            continue
        p = pays[r["outcomeIndex"]]
        r.update({"status": "resuelta", "payout": p, "won": p >= 0.999,
                  "roiDetect": metrics.roi(p, min(r["detectPrice"] + cost, 0.999))})
        if r["alerted"]:
            delayed = r.get("alertPriceDelayed", r["alertPrice"])
            r["entryPrice"] = round(min(r["alertPrice"] + cost, 0.999), 4)
            r["roi"] = metrics.roi(p, r["entryPrice"])
            r["roiDelayed"] = metrics.roi(p, min(delayed + cost, 0.999))
    return rows


def group_stats(rows):
    res = [r for r in rows if r.get("status") == "resuelta"]
    al = [dict(r, alertedAt=iso(r["alertTs"]), resolvedAt=iso(r["lastTs"]))
          for r in res if r["alerted"]]
    groups = {
        "alertadas": al,
        "alertadas_con_retraso": [dict(r, roi=r["roiDelayed"]) for r in al],
        "detectadas": [dict(r, roi=r["roiDetect"], entryPrice=r["detectPrice"]) for r in res],
        "descartadas_al_nacer": [dict(r, roi=r["roiDetect"], entryPrice=r["detectPrice"])
                                 for r in res if r["staleAtDetection"] and not r["alerted"]],
        "favoritos_no_alertados": [dict(r, roi=r["roiDetect"], entryPrice=r["detectPrice"])
                                   for r in res if not r["staleAtDetection"] and not r["alerted"]],
    }
    for tier in ("amplio", "especialistas"):
        groups[f"alertadas:{tier}"] = [r for r in al if r.get("tierAtAlert") == tier]
    out = {}
    for k, g in groups.items():
        s = metrics.summarize(g)
        s["verdict"] = metrics.verdict(s, SOURCE_MIN_SAMPLE)
        out[k] = s
    return out, al


def main():
    snaps = list(snapshots())
    if not snaps:
        print("sin historial de docs/signals.json (¿checkout con fetch-depth 0?)")
        return
    rows = replay(snaps)
    resolutions = fetch_market_resolutions([r["conditionId"] for r in rows])
    evaluate(rows, resolutions)
    groups, al = group_stats(rows)
    sens = {}
    for c in COSTS:
        tmp = evaluate([dict(r) for r in rows if r["alerted"]], resolutions, cost=c)
        sens[str(c)] = metrics.summarize([r for r in tmp if r.get("status") == "resuelta"])
    paper = metrics.paper_ledger(al, PAPER_STAKE)
    keep = ("id", "title", "outcome", "eventSlug", "cat", "firstTs", "detectPrice",
            "tierAtDetection", "staleAtDetection", "maxTraders", "alerted", "alertTs",
            "alertPrice", "alertPriceDelayed", "tierAtAlert", "entryPrice", "status",
            "payout", "won", "roi", "roiDelayed", "roiDetect", "lastTs")
    out = {
        "ranAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "replay-git",
        "period": {"from": iso(snaps[0][0]), "to": iso(snaps[-1][0])},
        "snapshots": len(snaps),
        "params": {"entryCost": ENTRY_COST, "maxAlertPrice": MAX_ALERT_PRICE,
                   "minEdge": MIN_EDGE, "paperStake": PAPER_STAKE,
                   "sourceMinSample": SOURCE_MIN_SAMPLE},
        "counts": {"senales": len(rows),
                   "alertadas": sum(1 for r in rows if r["alerted"]),
                   "resueltas": sum(1 for r in rows if r.get("status") == "resuelta"),
                   "pendientes": sum(1 for r in rows if r.get("status") == "pendiente")},
        "groups": groups,
        "costSensitivity": sens,
        "paper": paper,
        "caveats": [
            "Reglas de deteccion de cada momento: antes del 22-07-2026 solo consenso amplio (>= 5).",
            "Entrada = precio guardado + coste fijo (no hay libro historico); ver costSensitivity.",
            "Hora de resolucion aproximada por la ultima vez que se vio la senal.",
            "Mercados del mismo evento no son independientes: el IC95 puede ser optimista.",
        ],
        "rows": [{k: r.get(k) for k in keep} for r in rows],
    }
    path = pathlib.Path("/tmp/backtest.dry.json") if DRY_RUN else OUT
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")

    def linea(k):
        s = groups[k]
        if not s["n"]:
            return f"  {k}: sin resueltas"
        return (f"  {k}: n={s['n']} acierto {s['hitRate']:.0%} (precio implicaba "
                f"{s['avgEntry']:.0%}) ROI {s['avgRoi']:+.1%} IC95 {s['ci95']} -> {s['verdict']}")
    c = out["counts"]
    print(f"replay de {len(snaps)} versiones ({out['period']['from']} a {out['period']['to']}): "
          f"{c['senales']} senales, {c['alertadas']} alertadas, {c['resueltas']} resueltas, "
          f"{c['pendientes']} pendientes")
    for k in groups:
        print(linea(k))
    print(f"  cartera simulada (${PAPER_STAKE:.0f}/apuesta): {paper['bets']} apuestas, "
          f"PnL ${paper['pnl']:,.0f}, caida max ${paper['maxDrawdown']:,.0f}, "
          f"capital necesario ${paper['capitalNeeded']:,.0f}")


if __name__ == "__main__":
    main()

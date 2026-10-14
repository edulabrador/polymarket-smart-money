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
                     fetch_market_resolutions, phase)

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


# reglas de alerta: las actuales por defecto. max_drift None = usar la marca
# `stale` guardada (calculada con la deriva de entonces, 0.15)
CURRENT_RULES = {"max_drift": None, "max_price": MAX_ALERT_PRICE, "min_traders": 1,
                 "tiers": None, "min_edge": MIN_EDGE}


def alertable_snapshot(s, max_price=MAX_ALERT_PRICE, min_edge=MIN_EDGE, max_drift=None,
                       min_traders=1, tiers=None):
    """Misma regla que scanner.alertable sobre una version guardada, con los
    parametros variables para la validacion fuera de la muestra."""
    cur = s.get("curPrice")
    if cur is None or cur >= max_price or s.get("numTraders", 0) < min_traders:
        return False
    if tiers is not None and tier_of(s) not in tiers:
        return False
    if max_drift is None:
        if s.get("stale"):
            return False
    elif s.get("avgEntryPrice") is None or abs(cur - s["avgEntryPrice"]) > max_drift:
        return False
    if min_edge is not None and s.get("edgeEst") is not None:
        return s["edgeEst"] >= min_edge
    return True


def load_series(snaps):
    """{id: [(indice_version, ts, senal), ...]}: cada senal con su historia,
    para poder repetir el replay con distintas reglas sin releer git."""
    series = {}
    for k, (ts, doc) in enumerate(snaps):
        for s in doc.get("signals", []):
            if s.get("id") and s.get("curPrice") is not None:
                series.setdefault(s["id"], []).append((k, ts, s))
    return series


def replay(snaps_or_series, rules=None):
    """Una fila por senal (id = mercado:resultado) con su deteccion, su alerta
    (si la hubo segun `rules`) y los precios de entonces."""
    rules = {**CURRENT_RULES, **(rules or {})}
    series = (snaps_or_series if isinstance(snaps_or_series, dict)
              else load_series(snaps_or_series))
    rows = []
    for sid, pts in series.items():
        k0, ts0, s0 = pts[0]
        cond, _, idx = sid.partition(":")
        r = {"id": sid, "conditionId": cond, "outcomeIndex": int(idx or 0),
             "title": s0.get("title", ""), "outcome": s0.get("outcome", ""),
             "eventSlug": s0.get("eventSlug", ""),
             "cat": s0.get("cat") or categoria(s0.get("title", "")),
             "firstTs": ts0, "detectPrice": s0["curPrice"], "tierAtDetection": tier_of(s0),
             "staleAtDetection": bool(s0.get("stale")), "alerted": False,
             "maxTraders": max(s.get("numTraders", 0) for _, _, s in pts),
             "lastTs": pts[-1][1], "lastPrice": pts[-1][2]["curPrice"]}
        for j, (k, ts, s) in enumerate(pts):
            if alertable_snapshot(s, max_price=rules["max_price"], min_edge=rules["min_edge"],
                                  max_drift=rules["max_drift"],
                                  min_traders=rules["min_traders"], tiers=rules["tiers"]):
                r.update({"alerted": True, "alertTs": ts, "alertPrice": s["curPrice"],
                          "tierAtAlert": tier_of(s), "numTradersAtAlert": s.get("numTraders"),
                          "edgeEst": s.get("edgeEst"), "specialistBy": s.get("specialistBy")})
                # precio "con retraso": el de la version siguiente si sigue ahi
                if j + 1 < len(pts) and pts[j + 1][0] == k + 1:
                    r["alertPriceDelayed"] = pts[j + 1][2]["curPrice"]
                break
        rows.append(r)
    return rows


def evaluate(rows, resolutions, cost=ENTRY_COST):
    """Anota pago, ROI y hora de cierre en cada fila. Entradas con coste.
    `resolutions`: {cond: pagos} o {cond: {"payouts", "closedAt"}}."""
    for r in rows:
        res = resolutions.get(r["conditionId"])
        pays = res.get("payouts") if isinstance(res, dict) else res
        if not pays or r["outcomeIndex"] >= len(pays):
            r["status"] = "pendiente"
            continue
        p = pays[r["outcomeIndex"]]
        r.update({"status": "resuelta", "payout": p, "won": p >= 0.999,
                  "closedAt": res.get("closedAt") if isinstance(res, dict) else None,
                  "gameStart": res.get("gameStart") if isinstance(res, dict) else None,
                  "roiDetect": metrics.roi(p, min(r["detectPrice"] + cost, 0.999))})
        if r["alerted"]:
            delayed = r.get("alertPriceDelayed", r["alertPrice"])
            r["entryPrice"] = round(min(r["alertPrice"] + cost, 0.999), 4)
            r["roi"] = metrics.roi(p, r["entryPrice"])
            r["roiDelayed"] = metrics.roi(p, min(delayed + cost, 0.999))
            r["phase"] = phase(r["gameStart"], iso(r["alertTs"]))
    return rows


def alerted_rows(rows):
    """Alertas resueltas con hora de apertura y cierre (la real de gamma si
    existe; si no, la ultima vez que se vio la senal)."""
    return [dict(r, alertedAt=iso(r["alertTs"]), resolvedAt=r.get("closedAt") or iso(r["lastTs"]))
            for r in rows if r.get("status") == "resuelta" and r["alerted"]]


def group_stats(rows):
    res = [r for r in rows if r.get("status") == "resuelta"]
    al = alerted_rows(rows)
    det = lambda g: [dict(r, roi=r["roiDetect"], entryPrice=r["detectPrice"]) for r in g]
    groups = {
        "alertadas": al,
        "alertadas_con_retraso": [dict(r, roi=r["roiDelayed"]) for r in al],
        "detectadas": det(res),
        "descartadas_al_nacer": det([r for r in res if r["staleAtDetection"] and not r["alerted"]]),
        "favoritos_no_alertados": det([r for r in res if not r["staleAtDetection"]
                                       and not r["alerted"]]),
    }
    for tier in ("amplio", "especialistas"):
        groups[f"alertadas:{tier}"] = [r for r in al if r.get("tierAtAlert") == tier]
    # antes del partido / en directo / sin hora de inicio (no deportivo)
    for f in ("previa", "directo", "sin_hora"):
        groups[f"alertadas:{f}"] = [r for r in al if r.get("phase") == f]
    for by in ("acierto", "ventaja"):  # solo existe en versiones recientes
        g = [r for r in al if by in (r.get("specialistBy") or [])]
        if g:
            groups[f"especialistas_por_{by}"] = g
    out = {}
    for k, g in groups.items():
        s = metrics.summarize(g)
        s["verdict"] = metrics.verdict(s, SOURCE_MIN_SAMPLE)
        out[k] = s
    return out, al


# rejilla de reglas para la validacion fuera de la muestra
GRID = {"max_drift": [0.05, 0.10, 0.15, 0.25, 1.0],
        "max_price": [0.7, 0.8, 0.9],
        "min_traders": [3, 4, 5],
        # todas / solo antes del partido / todo menos directo
        "phases": [None, ("previa",), ("previa", "sin_hora")]}
WF_MIN_TRAIN = 30  # alertas resueltas minimas en la mitad de ajuste


def walk_forward(series, resolutions, split_ts):
    """Validacion FUERA DE LA MUESTRA. Se prueban todas las combinaciones de
    GRID con las alertas de antes de split_ts (ajuste), se elige la de mejor
    ROI, y se mide esa misma regla SOLO con las alertas posteriores (prueba).
    Si una regla gana de verdad, deberia seguir ganando en la prueba; si solo
    gano por azar en el ajuste, se cae. Se compara con las reglas actuales."""
    resultados = []
    for d in GRID["max_drift"]:
        for p in GRID["max_price"]:
            for m in GRID["min_traders"]:
                rules = {"max_drift": d, "max_price": p, "min_traders": m}
                al = alerted_rows(evaluate(replay(series, rules), resolutions))
                for ph in GRID["phases"]:
                    sel = [r for r in al if ph is None or r.get("phase") in ph]
                    train = metrics.summarize([r for r in sel if r["alertTs"] < split_ts])
                    test = metrics.summarize([r for r in sel if r["alertTs"] >= split_ts])
                    resultados.append({"rules": {**rules, "phases": list(ph) if ph else None},
                                       "train": train, "test": test})
    validas = [x for x in resultados if x["train"]["n"] >= WF_MIN_TRAIN]
    actual_al = alerted_rows(evaluate(replay(series), resolutions))
    actual = {"train": metrics.summarize([r for r in actual_al if r["alertTs"] < split_ts]),
              "test": metrics.summarize([r for r in actual_al if r["alertTs"] >= split_ts])}
    elegida = max(validas, key=lambda x: x["train"]["avgRoi"]) if validas else None
    positivas_train = sum(1 for x in validas if x["train"]["avgRoi"] > 0)
    return {"split": iso(split_ts), "combinaciones": len(resultados),
            "validas": len(validas), "positivasEnAjuste": positivas_train,
            "elegida": elegida, "reglasActuales": actual}


def main():
    snaps = list(snapshots())
    if not snaps:
        print("sin historial de docs/signals.json (¿checkout con fetch-depth 0?)")
        return
    series = load_series(snaps)
    rows = replay(series)
    resolutions = fetch_market_resolutions([r["conditionId"] for r in rows], with_time=True)
    evaluate(rows, resolutions)
    groups, al = group_stats(rows)
    sens = {}
    for c in COSTS:
        tmp = evaluate([dict(r) for r in rows if r["alerted"]], resolutions, cost=c)
        sens[str(c)] = metrics.summarize([r for r in tmp if r.get("status") == "resuelta"])
    paper = metrics.paper_ledger(al, PAPER_STAKE)
    wf = walk_forward(series, resolutions, (snaps[0][0] + snaps[-1][0]) / 2)
    keep = ("id", "title", "outcome", "eventSlug", "cat", "firstTs", "detectPrice",
            "tierAtDetection", "staleAtDetection", "maxTraders", "alerted", "alertTs",
            "alertPrice", "alertPriceDelayed", "tierAtAlert", "entryPrice", "status",
            "payout", "won", "roi", "roiDelayed", "roiDetect", "lastTs", "closedAt",
            "gameStart", "phase")
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
                   "pendientes": sum(1 for r in rows if r.get("status") == "pendiente"),
                   "conHoraDeCierre": sum(1 for r in rows if r.get("closedAt")),
                   "conHoraDeInicio": sum(1 for r in rows if r.get("gameStart"))},
        "groups": groups,
        "costSensitivity": sens,
        "paper": paper,
        "walkForward": wf,
        "caveats": [
            "Reglas de deteccion de cada momento: antes del 22-07-2026 solo consenso amplio (>= 5).",
            "Entrada = precio guardado + coste fijo (no hay libro historico); ver costSensitivity.",
            "Hora de resolucion: la de cierre de gamma si existe; si no, la ultima vez que se vio.",
            "IC95 robusto por evento: varios mercados de un mismo partido cuentan como uno.",
            "walkForward: la regla se elige con la 1a mitad y se mide con la 2a; solo cuenta la 2a.",
        ],
        "rows": [{k: r.get(k) for k in keep} for r in rows],
    }
    path = pathlib.Path("/tmp/backtest.dry.json") if DRY_RUN else OUT
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")

    def linea(k):
        s = groups[k]
        if not s["n"]:
            return f"  {k}: sin resueltas"
        return (f"  {k}: n={s['n']} ({s['events']} eventos) acierto {s['hitRate']:.0%} "
                f"(precio implicaba {s['avgEntry']:.0%}) ROI {s['avgRoi']:+.1%} "
                f"IC95 {s['ci95']} -> {s['verdict']}")
    c = out["counts"]
    print(f"replay de {len(snaps)} versiones ({out['period']['from']} a {out['period']['to']}): "
          f"{c['senales']} senales, {c['alertadas']} alertadas, {c['resueltas']} resueltas "
          f"({c['conHoraDeCierre']} con hora de cierre, {c['conHoraDeInicio']} con hora de inicio), "
          f"{c['pendientes']} pendientes")
    for k in groups:
        print(linea(k))
    print(f"  cartera simulada (${PAPER_STAKE:.0f}/apuesta): {paper['bets']} apuestas, "
          f"PnL ${paper['pnl']:,.0f}, caida max ${paper['maxDrawdown']:,.0f}, "
          f"capital necesario ${paper['capitalNeeded']:,.0f}")
    e = wf["elegida"]
    print(f"  fuera de muestra (corte {wf['split']}): {wf['validas']}/{wf['combinaciones']} "
          f"reglas con muestra, {wf['positivasEnAjuste']} positivas en el ajuste")
    if e:
        print(f"    mejor en ajuste {e['rules']}: ajuste ROI {e['train']['avgRoi']:+.1%} "
              f"(n={e['train']['n']}) -> prueba ROI "
              f"{(e['test']['avgRoi'] or 0):+.1%} (n={e['test']['n']}) IC95 {e['test']['ci95']}")
    a = wf["reglasActuales"]
    print(f"    reglas actuales: ajuste {(a['train']['avgRoi'] or 0):+.1%} (n={a['train']['n']}) "
          f"-> prueba {(a['test']['avgRoi'] or 0):+.1%} (n={a['test']['n']})")


if __name__ == "__main__":
    main()

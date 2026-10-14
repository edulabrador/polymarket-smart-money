"""Metricas de rentabilidad compartidas por el escaner (historico en vivo) y el
backtest (replay del historial de git). Funciones puras, sin red.

Convencion: una apuesta compra a `entry` (0-1) y cobra `payout` por accion al
resolver (1 si gana, 0 si pierde, 0.5 en un 50-50). ROI = payout / entry - 1.
"""
import math
from datetime import datetime


def roi(payout, entry):
    entry = min(max(entry, 0.001), 0.999)
    return round(payout / entry - 1, 4)


def summarize(rows, roi_key="roi", entry_key="entryPrice", cluster_key="eventSlug"):
    """Resumen de un grupo de apuestas resueltas.

    - hitRate frente a avgEntry: el precio de entrada es la probabilidad que
      el mercado daba; acertar MAS que eso es la unica ventaja real.
    - avgRoi con su intervalo de confianza al 95%. Si el IC incluye el 0, el
      resultado no es concluyente aunque la media sea positiva.
    - El IC es ROBUSTO POR EVENTO (cluster_key): ganador, handicap y mas/menos
      de un mismo partido no son apuestas independientes; tratarlas como tales
      estrecha el intervalo de forma enganosa. Con menos de 2 eventos no hay IC.
    """
    rows = [r for r in rows if r.get(roi_key) is not None]
    n = len(rows)
    if not n:
        return {"n": 0, "events": 0, "wins": 0, "hitRate": None, "avgEntry": None,
                "avgRoi": None, "medianRoi": None, "ci95": None}
    rois = [r[roi_key] for r in rows]
    wins = sum(1 for r in rows if r.get("won"))
    mean = sum(rois) / n
    # varianza robusta por cluster de la media: sum_g (S_g - n_g*media)^2 / n^2,
    # con correccion G/(G-1). Con un evento por apuesta equivale a la clasica.
    clusters = {}
    for i, r in enumerate(rows):
        k = r.get(cluster_key) or f"_fila{i}"
        s = clusters.setdefault(k, [0.0, 0])
        s[0] += r[roi_key]
        s[1] += 1
    g = len(clusters)
    ci = None
    if g >= 2:
        var = sum((s - c * mean) ** 2 for s, c in clusters.values()) / n ** 2 * g / (g - 1)
        half = 1.96 * math.sqrt(var)
        ci = [round(mean - half, 4), round(mean + half, 4)]
    srt = sorted(rois)
    med = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2
    entries = [r[entry_key] for r in rows if r.get(entry_key) is not None]
    return {"n": n, "events": g, "wins": wins, "hitRate": round(wins / n, 4),
            "avgEntry": round(sum(entries) / len(entries), 4) if entries else None,
            "avgRoi": round(mean, 4), "medianRoi": round(med, 4), "ci95": ci}


def first_per(rows, key="id"):
    """Una fila por `key` (la primera): varias compras del mismo mercado y lado
    no son apuestas independientes."""
    seen, out = set(), []
    for r in rows:
        k = r.get(key)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def halves(rows, roi_key="roi", time_key="alertedAt"):
    """ROI medio de la primera y la segunda mitad (por fecha) de las apuestas.
    Para una estrategia nueva es la prueba fuera de muestra en vivo: si solo
    gana en una mitad, lo mas probable es que fuera azar."""
    rows = sorted((r for r in rows if r.get(roi_key) is not None),
                  key=lambda r: r.get(time_key) or "")
    if len(rows) < 4:
        return None
    mid = len(rows) // 2
    media = lambda g: round(sum(r[roi_key] for r in g) / len(g), 4)
    return [media(rows[:mid]), media(rows[mid:])]


def verdict(s, min_sample):
    """Lectura honesta de un resumen: solo es 'rentable' si TODO el intervalo
    de confianza esta por encima de 0."""
    if s["n"] < min_sample:
        return "sin muestra"
    if s["avgRoi"] < 0:
        return "pierde"
    if s["ci95"] and s["ci95"][0] > 0:
        return "rentable"
    return "no concluyente"


def _ts(x):
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return datetime.fromisoformat(x).timestamp()
    except (TypeError, ValueError):
        return None


def paper_ledger(rows, stake, roi_key="roi", open_key="alertedAt",
                 close_key="resolvedAt", curve_points=300):
    """Cartera simulada: `stake` USD en cada apuesta, en orden de resolucion.

    Devuelve el PnL, la caida maxima desde un pico (lo que habrias tenido que
    aguantar) y el capital que habria estado inmovilizado a la vez (maximo de
    apuestas abiertas simultaneamente x stake)."""
    bets = [r for r in rows if r.get(roi_key) is not None and _ts(r.get(close_key))]
    bets.sort(key=lambda r: _ts(r[close_key]))
    equity = peak = dd = 0.0
    curve = []
    for r in bets:
        equity += stake * r[roi_key]
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
        curve.append([r[close_key], round(equity, 2)])
    # apuestas abiertas a la vez: barrido de eventos apertura(+1)/cierre(-1)
    events = []
    for r in bets:
        a, c = _ts(r.get(open_key)), _ts(r[close_key])
        if a is not None and c is not None and c >= a:
            events += [(a, 1), (c, -1)]
    events.sort(key=lambda e: (e[0], e[1]))
    cur = max_open = 0
    for _, d in events:
        cur += d
        max_open = max(max_open, cur)
    if len(curve) > curve_points:
        step = len(curve) / curve_points
        curve = [curve[int(i * step)] for i in range(curve_points - 1)] + [curve[-1]]
    n = len(bets)
    return {"stake": stake, "bets": n, "pnl": round(equity, 2),
            "roi": round(equity / (stake * n), 4) if n else None,
            "maxDrawdown": round(dd, 2), "maxOpen": max_open,
            "capitalNeeded": round(max_open * stake, 2), "curve": curve}

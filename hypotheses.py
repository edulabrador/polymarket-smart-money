"""Registro de hipotesis con fecha: la unica forma honesta de validar una idea
sacada MIRANDO los datos.

Al cortar los datos de 25 formas, alguna sale "rentable" por puro azar (con un
IC al 95%, ~1 de cada 20). Por eso cada idea se apunta aqui con la fecha en que
se formulo (`since`) y solo se evalua con apuestas POSTERIORES a esa fecha,
que no se usaron para encontrarla. Ademas, el IC se corrige por el numero de
hipotesis activas (Bonferroni): con 5 a la vez, cada una necesita un IC al 99%
para declararse rentable.

Reglas para que el registro siga siendo honesto:
- No se cambia el filtro ni la fecha de una hipotesis ya registrada; si la idea
  cambia, es una hipotesis NUEVA con fecha nueva.
- Se registran ANTES de mirar sus datos nuevos, y no se borran las que
  fallan (se marcan `retired` con el motivo): borrar las perdedoras es otra
  forma de sesgo de supervivencia.
"""
from datetime import datetime

import metrics

MIN_SAMPLE = 30  # apuestas (posteriores a su fecha) antes de dar veredicto


def hours_to_start(r):
    """Horas desde la apuesta hasta el inicio del partido (negativo = en
    directo). None si el mercado no tiene hora de inicio."""
    if not r.get("gameStart") or not r.get("alertedAt"):
        return None
    try:
        return (datetime.fromisoformat(r["gameStart"])
                - datetime.fromisoformat(r["alertedAt"])).total_seconds() / 3600
    except ValueError:
        return None


def _h(r, lo=None, hi=None):
    h = hours_to_start(r)
    return h is not None and (lo is None or h > lo) and (hi is None or h < hi)


# Registradas el 24-10-2026 tras analizar 807 compras nuevas (14-23 oct). Los
# numeros de "origen" son los de ese analisis: dentro de muestra, NO cuentan.
HYPOTHESES = [
    {"id": "H1", "since": "2026-10-24", "source": "entrada",
     "desc": "Compras nuevas hechas más de 24 h antes del partido",
     "origin": "n=19, ROI +51%, IC +7% a +96% (1 de ~25 cortes probados)",
     "test": lambda r: _h(r, lo=24)},
    {"id": "H2", "since": "2026-10-24", "source": "entrada",
     "desc": "Compras nuevas en deportes",
     "origin": "n=455, ROI +7%, IC -4% a +19%, mitades +13% / +1%",
     "test": lambda r: r.get("cat") == "deportes"},
    {"id": "H3", "since": "2026-10-24", "source": "entrada",
     "desc": "Compras nuevas en directo con más de 1 h de partido",
     "origin": "n=185 (51 eventos), ROI +12%, IC -8% a +32%",
     "test": lambda r: _h(r, hi=-1)},
    {"id": "H4", "since": "2026-10-24", "source": "entrada",
     "desc": "Compras nuevas grandes (≥ $10.000)",
     "origin": "sin datos previos: el tamaño no se guardaba al liquidar",
     "test": lambda r: (r.get("usd") or 0) >= 10000},
    {"id": "H5", "since": "2026-10-24", "source": "entrada",
     "desc": "Compras nuevas a precio 0,40-0,60 (sin favoritos ni longshots)",
     "origin": "n=451, ROI +4%, IC -8% a +17%",
     "test": lambda r: 0.40 <= (r.get("entryPrice") or 0) < 0.60},
]


def evaluate(rows_by_source, hypotheses=HYPOTHESES, min_sample=MIN_SAMPLE):
    """{id: resumen} de cada hipotesis, SOLO con apuestas desde su fecha y con
    el IC corregido por el numero de hipotesis activas."""
    activas = [h for h in hypotheses if not h.get("retired")]
    z = metrics.bonferroni_z(len(activas))
    out = {}
    for h in hypotheses:
        rows = [r for r in rows_by_source.get(h["source"], [])
                if (r.get("alertedAt") or "") >= h["since"] and h["test"](r)]
        s = metrics.summarize(rows, z=z)
        s["verdict"] = metrics.verdict(s, min_sample)
        s["halves"] = metrics.halves(rows)
        if s["verdict"] == "rentable" and s["halves"] and min(s["halves"]) <= 0:
            s["verdict"] = "no concluyente"  # gana solo en una mitad: no basta
        s["byWallet"] = metrics.by_wallet(rows, z=z)
        out[h["id"]] = {"desc": h["desc"], "since": h["since"], "source": h["source"],
                        "origin": h.get("origin"), "retired": h.get("retired"),
                        "confidence": round(1 - 0.05 / max(len(activas), 1), 4), **s}
    return out

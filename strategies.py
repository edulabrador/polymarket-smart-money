"""Banco de pruebas de estrategias: cada idea de apuesta genera apuestas "en
sombra" (se registran con su precio de entrada, no se envian), que se miden
con el mismo oraculo, ROI e IC95 que todo lo demas. Ninguna llega a Telegram
mientras los datos no la respalden.

Estrategias de este modulo (las de "smart money" viven en scanner.py):

- B · sesgo de precio (favorito-longshot): en muchos mercados de apuestas los
  resultados improbables se pagan de mas y los casi seguros de menos. Se
  registra cada mercado de corto plazo UNA vez, con su precio, y se mide el
  ROI por tramo de precio. Si el sesgo existe en Polymarket, los tramos altos
  daran ROI positivo y los bajos negativo.
- A · cuotas de casas "afiladas": en deportes, Pinnacle y el exchange de
  Betfair son la referencia mas eficiente. Si Polymarket da a un resultado
  bastante menos probabilidad que ellas (sin su margen), comprar ahi tiene
  ventaja esperada. Requiere una clave de The Odds API (ODDS_API_KEY).
"""
import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- comun

SEEN_DAYS = 30  # un mercado ya registrado no se vuelve a registrar en 30 dias


def short_term(endDate, today, days):
    """El mercado termina en los proximos `days` dias (resuelve pronto: la
    estrategia se mide en semanas, no en meses)."""
    end = (endDate or "")[:10]
    if not end:
        return False
    limite = (datetime.fromisoformat(today) + timedelta(days=days)).strftime("%Y-%m-%d")
    return today <= end <= limite


def universe(positions_by_trader):
    """Mercados vistos en este escaneo (posiciones de todos los wallets
    descargados), uno por mercado: {conditionId: posicion}. Es el universo que
    ya tenemos sin pedir nada mas a la API."""
    out = {}
    for info in positions_by_trader.values():
        for p in info["positions"]:
            if p.get("redeemable") or p.get("curPrice") is None:
                continue
            out.setdefault(p["conditionId"], p)
    return out


def prune_seen(seen, now):
    corte = (datetime.fromisoformat(now) - timedelta(days=SEEN_DAYS)).isoformat()
    return {k: v for k, v in seen.items() if v >= corte}


def bet(p, now, entry_cost, source, tag, idx=None, price=None, **extra):
    """Apuesta en sombra a partir de una posicion (o del otro lado si idx)."""
    idx = p["outcomeIndex"] if idx is None else idx
    price = p["curPrice"] if price is None else price
    outcome = p.get("outcome", "")
    if idx != p["outcomeIndex"]:
        outcome = p.get("oppositeOutcome") or f"resultado {idx}"
    return {"id": f'{p["conditionId"]}:{idx}', "conditionId": p["conditionId"],
            "outcomeIndex": idx, "title": p.get("title", ""), "outcome": outcome,
            "eventSlug": p.get("eventSlug", ""), "endDate": p.get("endDate", ""),
            "at": now, "price": round(price, 4),
            "entryPrice": round(min(price + entry_cost, 0.999), 4),
            "source": source, "tag": tag, **extra}


# ------------------------------------------------- B · sesgo de precio

# tramos de precio (probabilidad implicita) en los que se mide el ROI
BIAS_BUCKETS = [(0.01, 0.10, "0.01-0.10"), (0.10, 0.30, "0.10-0.30"),
                (0.30, 0.70, "0.30-0.70"), (0.70, 0.90, "0.70-0.90"),
                (0.90, 0.99, "0.90-0.99")]
BIAS_DAYS = int(os.getenv("BIAS_DAYS", "7"))


def bucket(price):
    for lo, hi, name in BIAS_BUCKETS:
        if lo <= price < hi:
            return name
    return None


def price_bias_bets(univ, seen, now, entry_cost, days=BIAS_DAYS):
    """Registra UNA vez cada mercado de corto plazo del universo, con el lado
    que se vio y su precio. Muta `seen`. Un solo lado por mercado: los dos
    lados de un mercado binario son la misma apuesta al reves."""
    today = now[:10]
    out = []
    for cond, p in univ.items():
        if cond in seen or not short_term(p.get("endDate"), today, days):
            continue
        b = bucket(p["curPrice"])
        if b is None:
            continue
        seen[cond] = now
        out.append(bet(p, now, entry_cost, "sesgo", b))
    return out


# ----------------------------------------- A · cuotas de casas afiladas

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_SPORTS = [s.strip() for s in os.getenv(
    "ODDS_SPORTS", "soccer_epl,soccer_spain_la_liga,basketball_nba").split(",") if s.strip()]
ODDS_BOOKMAKERS = [b.strip() for b in os.getenv(
    "ODDS_BOOKMAKERS", "pinnacle,betfair_ex_eu").split(",") if b.strip()]
# el plan gratuito de The Odds API da pocas peticiones al mes: por defecto se
# consultan las cuotas cada 6 h (3 deportes x 4 al dia = ~360 al mes)
ODDS_EVERY_MIN = int(os.getenv("ODDS_EVERY_MIN", "360"))
ODDS_MIN_EDGE = float(os.getenv("ODDS_MIN_EDGE", "0.03"))
ODDS_URL = ("https://api.the-odds-api.com/v4/sports/{sport}/odds/?apiKey={key}"
            "&regions=eu&markets=h2h&oddsFormat=decimal&bookmakers={books}")
STOP = {"fc", "cf", "sc", "afc", "club", "de", "the", "cd", "ud", "rc", "ac", "as",
        "ssc", "sv", "fk", "bk", "if"}


def norm_team(name):
    """Tokens significativos de un nombre de equipo, sin acentos ni 'FC'."""
    t = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    toks = re.findall(r"[a-z0-9]+", t)
    sig = [x for x in toks if x not in STOP]
    return set(sig or toks)


def same_team(a, b):
    """Mismo equipo si TODOS los tokens significativos del nombre mas corto
    estan en el otro: 'Atletico Madrid' = 'Club Atlético de Madrid', pero
    'Manchester City' != 'Manchester United'. Prefiere no emparejar a
    emparejar mal ('Man City' no casa con 'Manchester City')."""
    ta, tb = norm_team(a), norm_team(b)
    if not ta or not tb:
        return False
    corto, largo = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return corto <= largo


def fair_probs(event, books=ODDS_BOOKMAKERS):
    """{equipo o 'Draw': probabilidad justa} de la primera casa disponible
    en el orden de `books`, quitando su margen (normalizando 1/cuota)."""
    casas = {b.get("key"): b for b in event.get("bookmakers", [])}
    for key in books:
        b = casas.get(key)
        if not b:
            continue
        for m in b.get("markets", []):
            if m.get("key") != "h2h":
                continue
            inv = {o["name"]: 1 / o["price"] for o in m.get("outcomes", [])
                   if o.get("price", 0) > 1}
            total = sum(inv.values())
            if total > 0 and len(inv) >= 2:
                return {k: v / total for k, v in inv.items()}, key
    return None, None


WILL_WIN = re.compile(r"^will (.+?) win(?: on (\d{4}-\d{2}-\d{2}))?\??$", re.I)
VS = re.compile(r"^(.+?) vs\.? (.+?)$", re.I)


def match_market(p, events):
    """(probabilidad justa del lado de la posicion, casa, evento) si el
    mercado de Polymarket se corresponde con un partido de las casas.
    Formatos: 'Will X win on FECHA?' (Yes/No) y 'X vs. Y' (lado = equipo).
    Hándicaps, totales y mercados con ':' se ignoran (no son 1X2)."""
    title = (p.get("title") or "").strip()
    if ":" in title or "spread" in title.lower() or "o/u" in title.lower():
        return None
    m = WILL_WIN.match(title)
    if m:
        team, fecha = m.group(1), m.group(2)
        for ev in events:
            if fecha and abs((datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
                              .date() - datetime.fromisoformat(fecha).date()).days) > 1:
                continue
            probs, book = fair_probs(ev)
            if not probs:
                continue
            for name, prob in probs.items():
                if name != "Draw" and same_team(name, team):
                    yes = p.get("outcome", "").lower() == "yes"
                    return (prob if yes else 1 - prob), book, ev
        return None
    m = VS.match(title)
    if m:
        a, b = m.group(1), m.group(2)
        for ev in events:
            nombres = [ev.get("home_team", ""), ev.get("away_team", "")]
            if not (any(same_team(a, n) for n in nombres) and any(same_team(b, n) for n in nombres)):
                continue
            probs, book = fair_probs(ev)
            if not probs:
                continue
            for name, prob in probs.items():
                if name != "Draw" and same_team(name, p.get("outcome", "")):
                    return prob, book, ev
    return None


def odds_due(state, now, every_min=ODDS_EVERY_MIN):
    last = state.get("lastAt")
    # sin diagnostico guardado (estado de una version anterior): consultar ya
    return (not last or "candidatos" not in state
            or datetime.fromisoformat(now) - datetime.fromisoformat(last)
            >= timedelta(minutes=every_min))


def fetch_odds(fetch, key=ODDS_API_KEY, sports=ODDS_SPORTS, books=ODDS_BOOKMAKERS):
    """Partidos con cuotas de las casas de referencia. ([eventos], [errores])."""
    eventos, errores = [], []
    for sport in sports:
        try:
            eventos += fetch(ODDS_URL.format(sport=sport, key=key, books=",".join(books))) or []
        except Exception as exc:  # noqa: BLE001 - se informa en la web
            # el error lleva la URL, y la URL la clave: nunca a un JSON publico
            errores.append(f"{sport}: {str(exc).replace(key, '***') if key else exc}")
    return eventos, errores


def odds_bets(univ, events, seen, now, entry_cost, min_edge=ODDS_MIN_EDGE, days=3):
    """Compara cada mercado deportivo de corto plazo con la probabilidad justa
    de las casas. Devuelve (apuestas nuevas en sombra, discrepancias para la
    web). Se apuesta al lado que Polymarket infravalora (el de la posicion o
    el contrario) si la ventaja tras coste es >= min_edge; una vez por mercado."""
    today = now[:10]
    nuevas, discrepancias = [], []
    diag = {"candidatos": 0, "emparejados": 0}
    for cond, p in univ.items():
        if not short_term(p.get("endDate"), today, days):
            continue
        diag["candidatos"] += 1
        r = match_market(p, events)
        if not r:
            continue
        diag["emparejados"] += 1
        fair, book, ev = r
        cur = p["curPrice"]
        lados = [(p["outcomeIndex"], cur, fair), (1 - p["outcomeIndex"], 1 - cur, 1 - fair)]
        for idx, precio, justa in lados:
            edge = round(justa - min(precio + entry_cost, 0.999), 4)
            if edge < min_edge or not 0.02 < precio < 0.98:
                continue
            d = bet(p, now, entry_cost, "cuotas", "ventaja 5%+" if edge >= 0.05 else "ventaja 3-5%",
                    idx=idx, price=precio, fair=round(justa, 4), book=book, edge=edge)
            discrepancias.append(d)
            if cond not in seen:
                seen[cond] = now
                nuevas.append(d)
    discrepancias.sort(key=lambda d: -d["edge"])
    return nuevas, discrepancias[:30], diag


def run(univ, state, now, entry_cost, fetch):
    """Ejecuta B (siempre) y A (si hay clave y toca). Muta `state`
    (seen, odds) y devuelve las apuestas nuevas en sombra."""
    seen = prune_seen(state.get("seen", {}), now)
    nuevas = price_bias_bets(univ, seen, now, entry_cost)
    odds = state.get("odds", {})
    if not ODDS_API_KEY:
        odds.update({"status": "sin clave: define el secret ODDS_API_KEY (The Odds API)"})
    elif odds_due(odds, now):
        eventos, errores = fetch_odds(fetch)
        nuevas_a, disc, diag = odds_bets(univ, eventos, seen, now, entry_cost)
        nuevas += nuevas_a
        # diagnostico: distinguir "no hay partidos comunes" de "no hay ventaja"
        odds.update({"lastAt": now, "events": len(eventos), "errors": errores,
                     "discrepancies": disc, **diag,
                     "status": f"{len(eventos)} partidos de {len(ODDS_SPORTS)} deportes; "
                               f"{diag['candidatos']} mercados de Polymarket a 3 días o menos, "
                               f"{diag['emparejados']} emparejados con un partido; "
                               f"{len(disc)} discrepancias >= {ODDS_MIN_EDGE:.0%}"})
    state["seen"], state["odds"] = seen, odds
    return nuevas


def load(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save(path, state):
    path.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

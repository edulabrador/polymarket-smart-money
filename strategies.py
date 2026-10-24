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
import hashlib
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
    """Olvida lo visto hace mas de SEEN_DAYS. Se guarda solo la fecha (el
    dia basta y ocupa menos que la hora completa)."""
    corte = (datetime.fromisoformat(now) - timedelta(days=SEEN_DAYS)).strftime("%Y-%m-%d")
    return {k: v[:10] for k, v in seen.items() if v[:10] >= corte}


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
# muestreo: se registra 1 de cada BIAS_SAMPLE mercados. Con todo el universo
# eran ~265 apuestas al dia y strategies.json crecia sin freno; tras 2.600
# resueltas el ROI ya estaba en ~0% con IC estrecho, asi que una muestra basta
# para seguir vigilandolo. Determinista por mercado (hash del conditionId):
# no hace falta recordar los descartados y los dos lados nunca se mezclan.
BIAS_SAMPLE = max(1, int(os.getenv("BIAS_SAMPLE", "5")))


def sampled(cond, n=None):
    n = BIAS_SAMPLE if n is None else n
    return n <= 1 or int(hashlib.sha1(cond.encode()).hexdigest()[:8], 16) % n == 0


def bucket(price):
    for lo, hi, name in BIAS_BUCKETS:
        if lo <= price < hi:
            return name
    return None


def price_bias_bets(univ, seen, now, entry_cost, days=BIAS_DAYS, sample=None):
    """Registra UNA vez cada mercado de corto plazo del universo, con el lado
    que se vio y su precio. Muta `seen`. Un solo lado por mercado: los dos
    lados de un mercado binario son la misma apuesta al reves."""
    today = now[:10]
    out = []
    for cond, p in univ.items():
        if (cond in seen or not sampled(cond, sample)
                or not short_term(p.get("endDate"), today, days)):
            continue
        b = bucket(p["curPrice"])
        if b is None:
            continue
        seen[cond] = today
        out.append(bet(p, now, entry_cost, "sesgo", b))
    return out


# ----------------------------------------- A · cuotas de casas afiladas

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
# competiciones cuyos mercados aparecen de verdad en las carteras vigiladas
# (el 14-10-2026 eran sobre todo partidos europeos de Champions/Europa League
# y NFL; con solo Premier/LaLiga/NBA no se emparejaba ninguno)
ODDS_SPORTS = [s.strip() for s in os.getenv(
    "ODDS_SPORTS", "soccer_uefa_champs_league,soccer_uefa_europa_league,soccer_epl,"
    "soccer_spain_la_liga,americanfootball_nfl").split(",") if s.strip()]
ODDS_BOOKMAKERS = [b.strip() for b in os.getenv(
    "ODDS_BOOKMAKERS", "pinnacle,betfair_ex_eu").split(",") if b.strip()]
# el plan gratuito de The Odds API da pocas peticiones al mes (~500): por
# defecto cada 8 h -> 5 deportes x 3 al dia x 30 = ~450 al mes
ODDS_EVERY_MIN = int(os.getenv("ODDS_EVERY_MIN", "480"))
ODDS_MIN_EDGE = float(os.getenv("ODDS_MIN_EDGE", "0.03"))
ODDS_VERSION = 2  # 2 = mercados pedidos a gamma por liga (no los de las carteras)
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


# ligas de Polymarket (codigo de gamma /sports) para cada deporte de The Odds API
POLY_LEAGUES = {"soccer_epl": "epl", "soccer_spain_la_liga": "lal",
                "soccer_uefa_champs_league": "ucl", "soccer_uefa_europa_league": "uel",
                "soccer_germany_bundesliga": "bun", "soccer_italy_serie_a": "sea",
                "soccer_france_ligue_one": "fl1", "soccer_usa_mls": "mls",
                "americanfootball_nfl": "nfl"}
GAMMA = "https://gamma-api.polymarket.com"
EVENTS_URL = (GAMMA + "/events?series_id={series}&closed=false&limit=200"
              "&end_date_min={desde}&end_date_max={hasta}")
MAX_KICKOFF_GAP_H = 2  # misma hora de inicio (con margen) = mismo partido


def _lista(x):
    """gamma devuelve outcomes/outcomePrices como texto JSON."""
    if isinstance(x, str):
        try:
            return json.loads(x)
        except ValueError:
            return []
    return x or []


def _hora(x):
    """'2026-10-21 18:00:00+00' / '...Z' -> datetime con zona, o None."""
    if not x:
        return None
    x = x.replace(" ", "T").replace("Z", "+00:00")
    if re.search(r"[+-]\d\d$", x):
        x += ":00"
    try:
        return datetime.fromisoformat(x)
    except ValueError:
        return None


def poly_matches(fetch, now, sports=ODDS_SPORTS, days=3):
    """Partidos de Polymarket de las mismas ligas que las casas, pidiendolos
    directamente a gamma (antes solo se veian los mercados que tenian los top
    traders: casi nunca coincidian). Solo partidos que empiezan en los
    proximos `days` dias y solo sus mercados de ganador/empate (moneyline).
    Devuelve ([partido], [errores]); partido = {title, slug, teams, start,
    markets: [{conditionId, question, outcomes, prices, team}]}."""
    t0 = datetime.fromisoformat(now)
    desde, hasta = t0.strftime("%Y-%m-%dT%H:%M:%SZ"), (t0 + timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    try:
        series = {s.get("sport"): s.get("series") for s in fetch(GAMMA + "/sports") or []}
    except Exception as exc:  # noqa: BLE001
        return [], [f"gamma /sports: {exc}"]
    partidos, errores = [], []
    for sport in sports:
        sid = series.get(POLY_LEAGUES.get(sport))
        if not sid:
            continue
        try:
            eventos = fetch(EVENTS_URL.format(series=sid, desde=desde, hasta=hasta)) or []
        except Exception as exc:  # noqa: BLE001
            errores.append(f"gamma {sport}: {exc}")
            continue
        for ev in eventos:
            m = VS.match((ev.get("title") or "").strip())
            if not m or " - " in ev.get("title", ""):   # '... - Total Corners', etc.
                continue
            mercados = []
            inicio = None
            for mk in ev.get("markets") or []:
                if mk.get("sportsMarketType") != "moneyline" or mk.get("closed"):
                    continue
                inicio = inicio or _hora(mk.get("gameStartTime"))
                outs, precios = _lista(mk.get("outcomes")), _lista(mk.get("outcomePrices"))
                if len(outs) != 2 or len(precios) != 2:
                    continue
                mercados.append({"conditionId": mk.get("conditionId"), "question": mk.get("question", ""),
                                 "outcomes": outs, "prices": [float(x) for x in precios],
                                 "team": mk.get("groupItemTitle") or "",
                                 "endDate": mk.get("endDate") or ev.get("endDate") or ""})
            # solo antes del partido: en directo las cuotas de hace horas no valen
            if mercados and inicio and t0 < inicio <= t0 + timedelta(days=days):
                partidos.append({"title": ev["title"], "slug": ev.get("slug", ""),
                                 "teams": [m.group(1).strip(), m.group(2).strip()],
                                 "start": inicio, "sport": sport, "markets": mercados})
    return partidos, errores


VS = re.compile(r"^(.+?) vs\.? (.+?)$", re.I)


def match_event(partido, events):
    """Partido de las casas que corresponde al de Polymarket: misma hora de
    inicio (+-MAX_KICKOFF_GAP_H) y al menos un equipo que casa sin dudas; a
    esa hora y en esa liga eso ya identifica el partido aunque el otro nombre
    difiera ('Bayern München' / 'Bayern Munich'). Devuelve (evento,
    {equipo de Polymarket: nombre en las casas}) o None."""
    a, b = partido["teams"]
    for ev in events:
        ko = _hora(ev.get("commence_time"))
        if not ko or abs((ko - partido["start"]).total_seconds()) > MAX_KICKOFF_GAP_H * 3600:
            continue
        home, away = ev.get("home_team", ""), ev.get("away_team", "")
        if same_team(a, home) or same_team(b, away):
            return ev, {a: home, b: away}
        if same_team(a, away) or same_team(b, home):
            return ev, {a: away, b: home}
    return None


def market_fair(mk, partido, nombres, probs):
    """Probabilidad justa del resultado 0 del mercado (Yes, o el primer
    equipo en la NFL). None si no se puede asignar sin dudas."""
    q = mk["question"].lower()
    outs = [o.lower() for o in mk["outcomes"]]
    if outs == ["yes", "no"]:
        if "draw" in q:
            return probs.get("Draw")
        equipo = mk["team"]
    else:
        equipo = mk["outcomes"][0]
    for pm in partido["teams"]:
        if same_team(pm, equipo):
            return probs.get(nombres[pm])
    return None


def odds_due(state, now, every_min=ODDS_EVERY_MIN):
    last = state.get("lastAt")
    # sin diagnostico guardado (estado de una version anterior): consultar ya
    return (not last or "candidatos" not in state
            or state.get("sports") != ODDS_SPORTS  # lista de deportes cambiada
            or state.get("v") != ODDS_VERSION      # emparejado nuevo: consultar ya
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


def odds_bets(partidos, events, seen, now, entry_cost, min_edge=ODDS_MIN_EDGE):
    """Compara cada mercado de ganador/empate de Polymarket con la probabilidad
    justa de las casas. Devuelve (apuestas nuevas en sombra, discrepancias para
    la web, diagnostico). Se apuesta al lado que Polymarket infravalora si la
    ventaja tras coste es >= min_edge; una vez por mercado."""
    nuevas, discrepancias = [], []
    diag = {"candidatos": 0, "emparejados": 0, "partidos": len(partidos),
            "partidosEmparejados": 0}
    for partido in partidos:
        diag["candidatos"] += len(partido["markets"])
        r = match_event(partido, events)
        if not r:
            continue
        ev, nombres = r
        probs, book = fair_probs(ev)
        if not probs:
            continue
        diag["partidosEmparejados"] += 1
        for mk in partido["markets"]:
            fair = market_fair(mk, partido, nombres, probs)
            if fair is None:
                continue
            diag["emparejados"] += 1
            p = {"conditionId": mk["conditionId"], "outcomeIndex": 0, "outcome": mk["outcomes"][0],
                 "oppositeOutcome": mk["outcomes"][1], "title": mk["question"],
                 "eventSlug": partido["slug"], "endDate": mk["endDate"],
                 "curPrice": mk["prices"][0]}
            lados = [(0, mk["prices"][0], fair), (1, mk["prices"][1], 1 - fair)]
            for idx, precio, justa in lados:
                edge = round(justa - min(precio + entry_cost, 0.999), 4)
                if edge < min_edge or not 0.02 < precio < 0.98:
                    continue
                d = bet(p, now, entry_cost, "cuotas",
                        "ventaja 5%+" if edge >= 0.05 else "ventaja 3-5%",
                        idx=idx, price=precio, fair=round(justa, 4), book=book, edge=edge,
                        gameStart=partido["start"].isoformat(), league=partido["sport"])
                discrepancias.append(d)
                if mk["conditionId"] not in seen:
                    seen[mk["conditionId"]] = now[:10]
                    nuevas.append(d)
    discrepancias.sort(key=lambda d: -d["edge"])
    return nuevas, discrepancias[:30], diag


def run(univ, state, now, entry_cost, fetch):
    """Ejecuta B (siempre) y A (si hay clave y toca). Muta `state`
    (seen, seenOdds, odds) y devuelve las apuestas nuevas en sombra. A y B
    llevan registros de vistos separados: que B ya registrara un mercado no
    debe impedir que A lo apueste."""
    # los vistos que el muestreo ya no elegiria sobran: no se volverian a mirar
    seen = {k: v for k, v in prune_seen(state.get("seen", {}), now).items() if sampled(k)}
    nuevas = price_bias_bets(univ, seen, now, entry_cost)
    seen_odds = prune_seen(state.get("seenOdds", {}), now)
    odds = state.get("odds", {})
    if not ODDS_API_KEY:
        odds.update({"status": "sin clave: define el secret ODDS_API_KEY (The Odds API)"})
    elif odds_due(odds, now):
        eventos, errores = fetch_odds(fetch)
        partidos, errores_pm = poly_matches(fetch, now)
        nuevas_a, disc, diag = odds_bets(partidos, eventos, seen_odds, now, entry_cost)
        nuevas += nuevas_a
        # diagnostico: distinguir "no hay partidos comunes" de "no hay ventaja"
        odds.update({"lastAt": now, "events": len(eventos), "errors": errores + errores_pm,
                     "sports": ODDS_SPORTS, "v": ODDS_VERSION,
                     "discrepancies": disc, **diag,
                     "status": f"{len(eventos)} partidos con cuotas de {len(ODDS_SPORTS)} "
                               f"competiciones; {diag['partidos']} partidos de Polymarket en "
                               f"3 días, {diag['partidosEmparejados']} emparejados "
                               f"({diag['emparejados']} mercados); "
                               f"{len(disc)} discrepancias >= {ODDS_MIN_EDGE:.0%}"})
    state["seen"], state["seenOdds"], state["odds"] = seen, seen_odds, odds
    return nuevas


# campos de una apuesta resuelta que se usan (estadisticas, resumen, web);
# el resto (copiado de las pendientes) solo engordaba el fichero
RESOLVED_KEYS = ("id", "title", "outcome", "eventSlug", "alertedAt", "entryPrice", "payout",
                 "won", "roi", "resolvedAt", "source", "tag", "fair", "book", "edge", "league",
                 "gameStart")


def compact(row, keys=RESOLVED_KEYS):
    """Sin campos vacios y, si se dan `keys`, solo esos campos."""
    return {k: v for k, v in row.items() if v is not None and (keys is None or k in keys)}


def resolved_path(path):
    """Las resueltas van en un fichero aparte: solo crece y solo se reescribe
    cuando hay novedades (antes viajaban 1,8 MB en cada commit y la web los
    descargaba sin usarlos)."""
    return path.with_name(path.stem + "_resolved.json")


def load(path, keys=RESOLVED_KEYS):
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    rp = resolved_path(path)
    if rp.exists():  # formato nuevo; el viejo traia "resolved" dentro
        state["resolved"] = json.loads(rp.read_text(encoding="utf-8"))
    state["resolved"] = [compact(r, keys) for r in state.get("resolved", [])]
    return state


def save(path, state, keys=RESOLVED_KEYS):
    """Escribe el estado ligero en `path` y las resueltas en su fichero, este
    solo si cambiaron (un commit sin cambios en el no lo toca)."""
    state = dict(state)
    filas = [compact(r, keys) for r in state.pop("resolved", [])]
    resueltas = json.dumps(filas, separators=(",", ":"))
    rp = resolved_path(path)
    if not rp.exists() or rp.read_text(encoding="utf-8") != resueltas:
        rp.write_text(resueltas, encoding="utf-8")
    state["resolvedCount"] = len(filas)
    path.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

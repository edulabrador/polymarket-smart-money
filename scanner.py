"""Detecta coincidencias de los traders mas rentables de Polymarket.

Corre en GitHub Actions (la DGOJ bloquea *.polymarket.com en redes espanolas):
descarga el top de PnL del mes, las posiciones de cada trader, y escribe en
docs/signals.json las apuestas donde >= MIN_USERS traders coinciden.
Solo stdlib: sin dependencias.
"""
import json
import os
import pathlib
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import metrics

TOP_N = int(os.getenv("TOP_N", "50"))
MIN_USERS = int(os.getenv("MIN_USERS", "5"))
# segundo umbral, mas bajo, para coincidencias de ESPECIALISTAS: bastan
# MIN_USERS_SPECIALIST traders si su acierto 30d probado es alto. Encuentra la
# señal antes (no espera a que confluyan 5) sin perder fiabilidad — exige que
# esos pocos sean gente que demostradamente gana, no solo mas cuerpos.
MIN_USERS_SPECIALIST = int(os.getenv("MIN_USERS_SPECIALIST", "3"))
SPECIALIST_MIN_WINRATE = float(os.getenv("SPECIALIST_MIN_WINRATE", "0.6"))
MIN_POSITION_USD = float(os.getenv("MIN_POSITION_USD", "500"))
# si el precio actual difiere de la entrada media en mas de esto (en cualquier
# direccion), la senal se marca "stale": al alza los top ya ganaron ese tramo;
# a la baja la tesis de entrada ya no es la actual (p. ej. partido en vivo)
MAX_PRICE_DRIFT = float(os.getenv("MAX_PRICE_DRIFT", "0.15"))
# wallets con mas posiciones abiertas que esto son bots/market makers que
# apuestan a todo: su "coincidencia" no aporta senal
MAX_POSITIONS_PER_TRADER = int(os.getenv("MAX_POSITIONS_PER_TRADER", "200"))
# segunda fuente de oportunidades: compras individuales por encima de esto
WHALE_MIN_USD = float(os.getenv("WHALE_MIN_USD", "50000"))
# comprar a cuota altisima (>= esto) no es senal: asegura +poco%, sin recorrido
# ni informacion (aparcar dinero en una casi-certeza). Aplica a whales y avisos.
MAX_ALERT_PRICE = float(os.getenv("MAX_ALERT_PRICE", "0.9"))
# whale comprando a cuota improbable = la mas informativa (posible insider)
LONGSHOT_MAX_PRICE = float(os.getenv("LONGSHOT_MAX_PRICE", "0.3"))
# una compra grande de un gran perdedor no es senal: solo se notifica la
# whale con historial ganador (o del top 50). Sin historial suficiente
# (menos de WHALE_MIN_TRACK mercados resueltos en 30 dias) tampoco se avisa.
WHALE_MIN_WINRATE = float(os.getenv("WHALE_MIN_WINRATE", "0.55"))
WHALE_MIN_TRACK = int(os.getenv("WHALE_MIN_TRACK", "5"))
TRACK_WINDOW_DAYS = 30
TRACK_TTL_S = 6 * 3600  # el track record por wallet se recalcula cada 6 h
TRACK_MIN_USD = 100.0   # canjes/perdidas menores son ruido
WHALES_KEEP = 400  # whales recientes que se muestran en la web
# la medicion del ROI de las whales va aparte (whalesPending) y no se recorta:
# si se midieran solo las que caben en la web, las de mercados largos saldrian
# de la lista antes de liquidar y el ROI quedaria sesgado a apuestas cortas.
# Solo se descartan sin medir las que llevan mas de esto sin liquidar.
WHALE_PENDING_DAYS = 400
ORACLE_RECHECK_S = 50 * 60  # una pendiente sin fecha de fin se re-consulta cada ~hora
# registro de COMPRAS NUEVAS de los top traders (investigacion, no se notifica):
# una compra cuenta si la posicion es nueva o crece >= 20% y el dinero anadido
# es >= ENTRY_MIN_USD. Se mide su ROI con el oraculo, como el resto.
ENTRY_MIN_USD = float(os.getenv("ENTRY_MIN_USD", "1000"))
ENTRIES_PATH_NAME = "entries.json"
# especialistas por VENTAJA frente al precio (alternativa a la tasa de acierto)
SPECIALIST_MIN_EDGE = float(os.getenv("SPECIALIST_MIN_EDGE", "0.02"))
OUTBOX_TTL_MIN = 60  # un aviso de Telegram que falla se reintenta durante 1 h
# gate auto-corrector: una fuente con >= SOURCE_MIN_SAMPLE señales resueltas y
# ROI medio real negativo deja de notificar (sigue en la web, marcada). No es
# una decision fija: si las señales nuevas resuelven en positivo, se reactiva.
SOURCE_MIN_SAMPLE = int(os.getenv("SOURCE_MIN_SAMPLE", "20"))
# cadencia real del bucle en .github/workflows/scan.yml (lee la misma variable)
SCAN_EVERY_MIN = int(os.getenv("SCAN_EVERY_MIN", "5"))
# coste de entrada cuando no se puede leer el libro de ordenes: spread +
# deslizamiento tipico. El ROI medido descuenta esto; sin el, el ROI de seguir
# una senal sale inflado (nadie compra al precio medio, compra al ask).
ENTRY_COST = float(os.getenv("ENTRY_COST", "0.01"))
# apuesta fija de la cartera simulada (y tamano con el que se "barre" el
# libro de ordenes para calcular el precio de entrada real)
PAPER_STAKE = float(os.getenv("PAPER_STAKE", "100"))
# filtro opcional por ventaja estimada (edgeEst, sin validar aun): si se
# define, solo se alertan senales con edgeEst >= MIN_EDGE. Por defecto no filtra.
MIN_EDGE = float(os.environ["MIN_EDGE"]) if os.getenv("MIN_EDGE") else None
# niveles que se ENVIAN por Telegram. Los demas se alertan "en sombra": se
# registran con su precio de entrada y se mide su ROI, pero no se envian.
# Por defecto solo consenso amplio: el backtest por replay (jul-oct 2026) dio
# a especialistas ROI -6% sobre 194 alertas resueltas (acierto 55% con un
# precio que ya implicaba 57%). Si el historico en vivo les da la razon, se
# activan con ALERT_TIERS=amplio,especialistas.
ALERT_TIERS = {t.strip() for t in os.getenv("ALERT_TIERS", "amplio").split(",") if t.strip()}
# un trader "entro hace poco" si su ultima compra en ese mercado es de las
# ultimas RECENT_ENTRY_HOURS horas (vs. posiciones viejas que solo siguen ahi)
RECENT_ENTRY_HOURS = 24
# encogimiento bayesiano de la ventaja de un trader: con pocas apuestas su
# ventaja medida se acerca a 0 (equivale a sumar EDGE_PRIOR_N apuestas neutras)
EDGE_PRIOR_N = 10
# una senal seguida cuyo mercado no resuelve en el oraculo pasados estos dias
# desde su endDate sale del seguimiento como "sin resolver" (no cuenta en ROI)
TRACK_EXPIRE_DAYS = 45
SCHEMA = 2  # version del formato de signals.json (2 = seguimiento persistente)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
# prueba en seco: lee el estado real pero escribe en DRY_RUN_PATH y no envia
# nada por Telegram (lo usa el workflow de tests para validar contra la API)
DRY_RUN = os.getenv("DRY_RUN", "") not in ("", "0")
DRY_RUN_PATH = os.getenv("DRY_RUN_PATH", "/tmp/signals.dry.json")
SIGNALS_PATH = pathlib.Path(__file__).parent / "docs" / "signals.json"

LEADERBOARD_URL = ("https://data-api.polymarket.com/v1/leaderboard"
                   "?timePeriod=MONTH&orderBy=PNL&limit={n}")
# sizeThreshold va en tokens, pero toda posicion con currentValue >= X USD
# tiene al menos X tokens (precio <= 1), asi que sirve de prefiltro seguro.
POSITIONS_URL = ("https://data-api.polymarket.com/positions"
                 "?user={wallet}&sizeThreshold={threshold}&limit=500")
TRADES_URL = ("https://data-api.polymarket.com/trades"
              "?takerOnly=true&filterType=CASH&filterAmount={amount}&limit=100")
# sin filtro de tipo: un solo fetch da los canjes (REDEEM = mercados ganados)
# y el flujo de caja (compras/ventas) para el PnL neto real del wallet
ACTIVITY_URL = ("https://data-api.polymarket.com/activity"
                "?user={wallet}&limit=500&offset={offset}")
ACTIVITY_PAGE = 500
ACTIVITY_MAX_PAGES = 6  # hasta 3.000 eventos por wallet
# resolucion de mercados: gamma devuelve closed + outcomePrices (["1","0"]...),
# el indice del precio "1" es el resultado ganador. Sirve para medir el ROI de
# las whales, cuyos compradores no son del top 50 (no estan en position_index).
MARKETS_URL = "https://gamma-api.polymarket.com/markets?closed=true&limit=100&{ids}"
# libro de ordenes publico del CLOB: el precio al que de verdad compraria un
# seguidor (ask), no el precio medio que da /positions
BOOK_URL = "https://clob.polymarket.com/book?token_id={token}"


class FetchError(RuntimeError):
    """La API no respondio tras varios intentos. No es un bug del scanner."""


def get_json(url, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "polymarket-smart-money/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except (OSError, json.JSONDecodeError) as exc:
            last = exc
            if attempt == retries - 1:
                break
            time.sleep(2 ** attempt)
    raise FetchError(f"fallo temporal consultando {url}: {last}") from last


def detect_signals(positions_by_trader, min_users=MIN_USERS, min_usd=MIN_POSITION_USD,
                   max_drift=MAX_PRICE_DRIFT):
    """positions_by_trader: {wallet: {"name": str, "positions": [dicts de la API]}}.

    Senal = grupo (conditionId, outcomeIndex) con >= min_users traders
    distintos, cada uno con posicion viva de >= min_usd USD actuales.
    Si el precio ya subio mas de max_drift sobre la entrada media, la senal
    se marca stale=True (se muestra descartada y no se notifica).

    OJO: este filtro de min_usd sirve para DETECTAR, nunca para dar veredicto.
    Una posicion que va perdiendo baja de valor y sale del grupo; si el
    veredicto dependiera de seguir aqui, las perdedoras desaparecerian sin
    registrarse (sesgo de supervivencia). El resultado lo da el oraculo.
    """
    groups = {}
    for wallet, info in positions_by_trader.items():
        for p in info["positions"]:
            if p.get("redeemable") or p.get("currentValue", 0) < min_usd:
                continue
            key = f'{p["conditionId"]}:{p["outcomeIndex"]}'
            g = groups.setdefault(key, {"meta": p, "traders": {}})
            g["traders"][wallet] = {
                "wallet": wallet,
                "name": info.get("name", ""),
                "usd": round(p["currentValue"], 2),
                "avgPrice": p["avgPrice"],
            }

    signals = []
    for key, g in groups.items():
        if len(g["traders"]) < min_users:
            continue
        traders = sorted(g["traders"].values(), key=lambda t: -t["usd"])
        total = sum(t["usd"] for t in traders)
        m = g["meta"]
        avg_entry = round(sum(t["avgPrice"] * t["usd"] for t in traders) / total, 4)
        cur = m["curPrice"]
        signals.append({
            "id": key,
            "conditionId": m["conditionId"],
            "outcomeIndex": m["outcomeIndex"],
            # token del resultado: con el se lee el libro de ordenes (ask real)
            "asset": m.get("asset", ""),
            "title": m["title"],
            "outcome": m["outcome"],
            "slug": m["slug"],
            "eventSlug": m["eventSlug"],
            "endDate": m.get("endDate", ""),
            "curPrice": cur,
            "numTraders": len(traders),
            "totalUsd": round(total, 2),
            "avgEntryPrice": avg_entry,
            "stale": abs(cur - avg_entry) > max_drift,
            # cuanto ganas por $ si gana comprando ahora: favorito a 0.9 -> x0.11
            # (sin margen); longshot a 0.25 -> x3. Donde el dinero listo tiene edge.
            "upside": round((1 - cur) / cur, 2) if cur > 0 else 0,
            # +pp del precio actual sobre la entrada media: si es pequeno aun
            # puedes entrar cerca de su precio; si es grande, llegas tarde.
            "entryGap": round(cur - avg_entry, 3),
            "traders": traders,
        })
    signals.sort(key=lambda s: (s["stale"], -s["numTraders"], -s["totalUsd"]))
    return signals


# --------------------------------------------------------------------------
# Precio de entrada real y oraculo de resolucion
# --------------------------------------------------------------------------

def fill_price(book, stake):
    """Precio medio pagado al comprar `stake` USD barriendo los asks del libro
    (del mas barato al mas caro). None si el libro no tiene liquidez para
    llenar la orden entera."""
    asks = []
    for a in (book or {}).get("asks", []):
        try:
            p, s = float(a["price"]), float(a["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 < p < 1 and s > 0:
            asks.append((p, s))
    asks.sort()
    spent = shares = 0.0
    for p, s in asks:
        take = min(s * p, stake - spent)
        spent += take
        shares += take / p
        if spent >= stake - 1e-9:
            break
    if shares <= 0 or spent < stake * 0.999:
        return None
    return round(spent / shares, 4)


def entry_price(asset, cur, fetch=None, stake=PAPER_STAKE):
    """(precio, fuente) al que entraria un seguidor AHORA.
    'libro': ask real del CLOB para `stake` USD. 'estimado': sin libro
    disponible, precio de mercado + ENTRY_COST (spread y deslizamiento)."""
    fetch = fetch or get_json
    if asset:
        try:
            p = fill_price(fetch(BOOK_URL.format(token=asset), retries=1), stake)
            if p is not None:
                return p, "libro"
        except Exception:
            pass
    return round(min(cur + ENTRY_COST, 0.999), 4), "estimado"


def final_payouts(market):
    """Pago por accion de cada resultado si el mercado esta resuelto de forma
    DEFINITIVA; None si no. Estricto a proposito: un mercado cerrado pero con
    precios intermedios (0.97/0.03) aun no esta liquidado y no da veredicto.
    Admite el 50-50 (todos a 0.5) que usa Polymarket en mercados anulados."""
    if not market.get("closed"):
        return None
    precios = market.get("outcomePrices")
    if isinstance(precios, str):
        try:
            precios = json.loads(precios)
        except ValueError:
            return None
    try:
        ps = [float(p) for p in precios or []]
    except (TypeError, ValueError):
        return None
    if len(ps) < 2:
        return None
    if all(p <= 0.001 or p >= 0.999 for p in ps) and sum(1 for p in ps if p >= 0.999) == 1:
        return [1.0 if p >= 0.999 else 0.0 for p in ps]
    if all(abs(p - 0.5) < 0.001 for p in ps):
        return [0.5] * len(ps)
    return None


def parse_time(x):
    """ISO normalizado de las fechas de gamma ('2026-09-10 03:12:45+00', con Z,
    o solo fecha). None si no se entiende."""
    if not x or not isinstance(x, str):
        return None
    t = x.strip().replace(" ", "T").replace("Z", "+00:00")
    if re.search(r"[+-]\d\d$", t):
        t += ":00"
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def fetch_market_resolutions(condition_ids, fetch=None, chunk=40, with_time=False):
    """{conditionId: [pago por resultado]} de los mercados YA liquidados de la
    lista (gamma: closed + outcomePrices). Es el oraculo UNICO del proyecto:
    da el veredicto de senales y whales sin depender de que nadie siga
    teniendo la posicion. Tolerante a fallos: un lote que falla se salta (se
    reintenta en el siguiente escaneo). Con with_time devuelve
    {conditionId: {"payouts": [...], "closedAt": iso o None}} (hora real de
    cierre, para ordenar la cartera simulada del backtest)."""
    fetch = fetch or get_json
    out = {}
    ids = [c for c in dict.fromkeys(condition_ids) if c]  # unicos, sin vacios
    for i in range(0, len(ids), chunk):
        q = "&".join(f"condition_ids={c}" for c in ids[i:i + chunk])
        try:
            markets = fetch(MARKETS_URL.format(ids=q)) or []
        except Exception:
            continue
        for m in markets:
            cid = m.get("conditionId") or m.get("condition_id")
            pays = final_payouts(m)
            if cid and pays:
                out[cid] = pays if not with_time else {
                    "payouts": pays,
                    "closedAt": parse_time(m.get("closedTime") or m.get("umaEndDate"))}
    return out


# --------------------------------------------------------------------------
# Seguimiento persistente de senales (base del historico fiable)
# --------------------------------------------------------------------------

def alertable(s, min_edge=MIN_EDGE):
    """Una senal se alerta si es fresca (precio aun cerca de la entrada de los
    traders), no es un favorito casi resuelto y, si MIN_EDGE esta definido,
    su ventaja estimada lo supera."""
    if s["stale"] or s["curPrice"] >= MAX_ALERT_PRICE:
        return False
    if min_edge is not None:
        return s.get("edgeEst") is not None and s["edgeEst"] >= min_edge
    return True


def track_signals(tracked, signals, now):
    """Actualiza el registro persistente `tracked` ({id: entrada}) con las
    senales cualificadas de este escaneo. Devuelve los ids nuevos.

    Cada id se sigue UNA vez desde su primera deteccion hasta que su mercado
    resuelve, aunque entre medias la senal desaparezca y vuelva (no se crean
    episodios nuevos con otro precio de entrada)."""
    nuevos = []
    for s in signals:
        t = tracked.get(s["id"])
        if t is None:
            t = tracked[s["id"]] = {
                "id": s["id"], "conditionId": s["conditionId"],
                "outcomeIndex": s["outcomeIndex"], "title": s["title"],
                "outcome": s["outcome"], "eventSlug": s.get("eventSlug", ""),
                "endDate": s.get("endDate", ""), "cat": s.get("cat") or categoria(s["title"]),
                "firstSeen": now, "detectPrice": s["curPrice"],
                "tierAtDetection": s.get("tier"), "staleAtDetection": s["stale"],
                "alerted": False, "maxTraders": 0,
            }
            nuevos.append(s["id"])
        t["lastSeen"] = now
        t["lastPrice"] = s["curPrice"]
        t["tier"] = s.get("tier")
        t["numTraders"] = s["numTraders"]
        t["maxTraders"] = max(t.get("maxTraders", 0), s["numTraders"])
        t["specialistBy"] = sorted(set(t.get("specialistBy") or []) | set(s.get("specialistBy") or []))
        # datos para la web: cuando se vio por primera vez y a que precio
        s["firstSeen"] = t["firstSeen"]
        s["detectPrice"] = t["detectPrice"]
        s["alerted"] = t["alerted"]
        if t["alerted"]:
            s["alertedAt"] = t["alertedAt"]
            s["entryPrice"] = t["entryPrice"]
            s["sent"] = t.get("sent")
    return nuevos


def mark_alerted(t, s, price, source, now, sent):
    """Registra la alerta de una senal: el precio al que entraria el seguidor
    en ese momento es la base de su ROI. `sent` dice si de verdad salio por
    Telegram: un nivel fuera de ALERT_TIERS o un gate silenciado alerta 'en
    sombra', para seguir midiendo y poder reactivarse con datos."""
    t.update({"alerted": True, "alertedAt": now, "alertPrice": s["curPrice"],
              "entryPrice": price, "entrySource": source, "sent": sent,
              "tierAtAlert": s.get("tier"), "numTradersAtAlert": s["numTraders"],
              "specialistByAtAlert": s.get("specialistBy"),
              "edgeEst": s.get("edgeEst"), "pEst": s.get("pEst"),
              "recentEntrants": s.get("recentEntrants"),
              "lastEntryTs": s.get("lastEntryTs")})
    s.update({"alerted": True, "alertedAt": now, "entryPrice": price, "sent": sent})


def resolve_tracked(tracked, resolutions, now, active_ids=()):
    """Senales seguidas cuyo mercado ya liquido -> historico con su ROI real.
    Las que llevan TRACK_EXPIRE_DAYS sin liquidar tras su endDate salen como
    'sin resolver' (se registran, pero no cuentan en el ROI). Muta `tracked`.

    - roi: el de la ALERTA (entrada = precio real del seguidor al avisar).
      Solo existe si la senal se alerto: es lo que mide la rentabilidad.
    - roiDetect: investigacion, para TODAS las detectadas (entrada = precio en
      la primera deteccion + ENTRY_COST). Nunca se mezcla con `roi`."""
    out = []
    expire_before = (datetime.fromisoformat(now) - timedelta(days=TRACK_EXPIRE_DAYS)
                     ).strftime("%Y-%m-%d")
    for sid in list(tracked):
        t = tracked[sid]
        pays = resolutions.get(t["conditionId"])
        h = {k: t.get(k) for k in (
            "id", "title", "outcome", "eventSlug", "cat", "firstSeen", "detectPrice",
            "tierAtDetection", "staleAtDetection", "maxTraders", "alerted", "alertedAt",
            "alertPrice", "entryPrice", "entrySource", "sent", "tierAtAlert",
            "numTradersAtAlert", "edgeEst", "pEst", "recentEntrants", "seeded",
            "specialistBy", "specialistByAtAlert", "delivered")}
        h.update({"source": "coincidencia", "numTraders": t.get("maxTraders"),
                  "tier": t.get("tierAtAlert") or t.get("tier") or t.get("tierAtDetection"),
                  "resolvedAt": now, "v": SCHEMA})
        if pays is not None and t["outcomeIndex"] < len(pays):
            payout = pays[t["outcomeIndex"]]
            h.update({"payout": payout, "won": payout >= 0.999,
                      "roiDetect": metrics.roi(payout, min(t["detectPrice"] + ENTRY_COST, 0.999)),
                      "roi": metrics.roi(payout, t["entryPrice"]) if t.get("alerted") else None})
        elif (sid not in active_ids and t.get("endDate")
              and t["endDate"][:10] < expire_before):
            h.update({"payout": None, "won": None, "roi": None, "roiDetect": None,
                      "unresolved": True})
        else:
            continue
        out.append(h)
        del tracked[sid]
    return out


def needs_oracle(t, active_ids, today):
    """Solo se consulta el oraculo para senales que ya no estan activas o cuya
    fecha de fin ya llego (una activa con fecha futura no puede haber liquidado)."""
    return t["id"] not in active_ids or (t.get("endDate") or "9999")[:10] <= today


# --------------------------------------------------------------------------
# Compras nuevas de los top traders (investigacion: via "entrar en caliente")
# --------------------------------------------------------------------------

def position_sizes(positions_by_trader):
    """{wallet: {mercado:resultado: acciones}}: foto de tamanos para detectar
    compras entre escaneos. Por ACCIONES, no por valor: una subida de precio
    cambia el valor de una posicion sin que nadie haya comprado nada."""
    return {w: {f'{p["conditionId"]}:{p["outcomeIndex"]}': round(p.get("size", 0), 2)
                for p in info["positions"] if not p.get("redeemable")}
            for w, info in positions_by_trader.items()}


def detect_entries(prev_sizes, positions_by_trader, tracks, now, pending,
                   min_usd=ENTRY_MIN_USD, max_price=MAX_ALERT_PRICE):
    """Compras nuevas desde el escaneo anterior: posicion nueva o que crece
    >= 20% con >= min_usd anadidos, a precio < max_price. Solo para wallets
    que ya estaban en la foto anterior (uno que entra al top no "compra" todo
    su historico de golpe). Una compra por mercado y lado cada 24 h: si otro
    top trader entra en el mismo, se suma a `wallets` de la ya registrada.
    Muta `pending` (lista de compras pendientes de medir) y devuelve las nuevas."""
    reciente = {}
    corte = (datetime.fromisoformat(now) - timedelta(hours=24)).isoformat(timespec="seconds")
    for e in pending:
        if e["at"] >= corte:
            reciente[e["id"]] = e
    nuevas = []
    for w, info in positions_by_trader.items():
        prev = prev_sizes.get(w)
        if prev is None:
            continue
        for p in info["positions"]:
            if p.get("redeemable"):
                continue
            sid = f'{p["conditionId"]}:{p["outcomeIndex"]}'
            size, cur = p.get("size", 0), p.get("curPrice", 0)
            antes = prev.get(sid, 0)
            if size <= antes * 1.2 or (size - antes) * cur < min_usd or not 0 < cur < max_price:
                continue
            if sid in reciente:
                if w not in reciente[sid]["wallets"]:
                    reciente[sid]["wallets"].append(w)
                continue
            tr = tracks.get(w) or {}
            cat = categoria(p.get("title", ""))
            e = {"id": sid, "conditionId": p["conditionId"], "outcomeIndex": p["outcomeIndex"],
                 "title": p.get("title", ""), "outcome": p.get("outcome", ""),
                 "eventSlug": p.get("eventSlug", ""), "endDate": p.get("endDate", ""),
                 "cat": cat, "at": now, "price": cur,
                 "entryPrice": round(min(cur + ENTRY_COST, 0.999), 4),
                 "usd": round((size - antes) * cur, 2), "wallets": [w],
                 "traderEdge": trader_edge(tr, cat), "traderWinRate": win_rate(tr) if tr else None}
            reciente[sid] = e
            pending.append(e)
            nuevas.append(e)
    return nuevas


def oracle_due(item, now_ts, today, key_end="endDate"):
    """Toca consultar el oraculo para una pendiente si su fecha de fin ya
    llego o, sin fecha conocida, si no se consulto en la ultima hora."""
    end = (item.get(key_end) or "")[:10]
    if end:
        return end <= today
    return now_ts - item.get("chk", 0) >= ORACLE_RECHECK_S


def resolve_pending(pending, resolutions, now, source, expire_before=None,
                    max_age_ts=None, now_ts=None, queried=()):
    """Pendientes (compras nuevas o whales) cuyo mercado liquido -> filas de
    historico con ROI a su precio de entrada. Devuelve (pendientes, resueltas,
    caducadas). Caducan las de endDate anterior a expire_before o, sin
    fecha, las mas viejas que max_age_ts. A las consultadas (`queried`) se
    les apunta la hora para no re-consultarlas hasta ORACLE_RECHECK_S."""
    kept, out, caducadas = [], [], 0
    for e in pending:
        pays = resolutions.get(e["conditionId"])
        idx = e.get("outcomeIndex")
        if pays is not None and isinstance(idx, int) and idx < len(pays):
            p = pays[idx]
            out.append({
                "id": e["id"], "title": e["title"], "outcome": e["outcome"],
                "eventSlug": e.get("eventSlug", ""), "cat": e.get("cat"),
                "alertedAt": e.get("at"), "entryPrice": e["entryPrice"], "payout": p,
                "won": p >= 0.999, "roi": metrics.roi(p, e["entryPrice"]),
                "numTraders": len(e.get("wallets") or [1]), "traderEdge": e.get("traderEdge"),
                "resolvedAt": now, "source": source, "v": SCHEMA})
            continue
        end = (e.get("endDate") or "")[:10]
        if (end and expire_before and end < expire_before) or (
                not end and max_age_ts and e.get("ts", now_ts or 0) < max_age_ts):
            caducadas += 1
            continue
        if now_ts is not None and e["conditionId"] in queried:
            e["chk"] = now_ts
        kept.append(e)
    return kept, out, caducadas


def source_stats(history, min_sample=SOURCE_MIN_SAMPLE, entries=()):
    """Rendimiento real por fuente sobre el historico FIABLE (formato v2; el
    historico anterior queda marcado `legacy` y no cuenta):

    - coincidencia / coincidencia:<nivel>: solo senales ALERTADAS, con el ROI
      al precio real de entrada del aviso. Es lo que decide el gate.
    - whale: compras whale con precio de seguidor, una por mercado y lado
      (varias compras en el mismo mercado no son apuestas independientes).
    - investigacion: TODAS las coincidencias detectadas (alertadas o no) con
      entrada en la deteccion. Sirve para estudiar, nunca para el gate.

    `suppressed` = muestra suficiente y ROI medio negativo: deja de notificar
    (se sigue alertando en sombra y se reactiva sola si vuelve a positivo)."""
    reliable = [h for h in history if not h.get("legacy") and h.get("payout") is not None]
    groups = {"coincidencia": [], "investigacion": [], "whale": []}
    for h in reliable:
        if h.get("source") == "coincidencia":
            groups["investigacion"].append({**h, "roi": h.get("roiDetect"),
                                            "entryPrice": h.get("detectPrice")})
            if h.get("alerted") and h.get("roi") is not None and not h.get("seeded"):
                groups["coincidencia"].append(h)
                groups.setdefault(f"coincidencia:{h.get('tier') or '?'}", []).append(h)
                for by in h.get("specialistByAtAlert") or []:
                    groups.setdefault(f"especialistas_por_{by}", []).append(h)
        elif h.get("source") == "whale":
            groups["whale"].append(h)
    groups["whale"] = metrics.first_per(groups["whale"])
    # compras nuevas de top traders (investigacion): todas, de traders con
    # ventaja probada, y en las que entraron 2 o mas top traders en 24 h
    ent = [e for e in entries if e.get("payout") is not None]
    if ent:
        groups["entrada"] = ent
        groups["entrada:ventaja+"] = [e for e in ent if (e.get("traderEdge") or 0) > 0]
        groups["entrada:2+"] = [e for e in ent if e.get("numTraders", 1) >= 2]
    out = {}
    for key, rows in groups.items():
        s = metrics.summarize(rows)
        s["verdict"] = metrics.verdict(s, min_sample)
        s["suppressed"] = (key != "investigacion" and not key.startswith("entrada")
                           and s["n"] >= min_sample and s["avgRoi"] < 0)
        out[key] = s
    return out


def migrate(doc, now):
    """Pasa un signals.json antiguo (sin seguimiento persistente) al formato v2.

    El historico viejo se conserva pero se marca `legacy`: tenia sesgo de
    supervivencia (las perdedoras desaparecian sin registrarse) y medio ROI
    con un precio que nadie habria conseguido. No cuenta en ninguna metrica.
    Las senales activas pasan a seguimiento como `seeded` (no se sabe si se
    avisaron ni a que precio: solo cuentan para investigacion)."""
    if doc.get("schema") == SCHEMA:
        return doc
    for h in doc.get("history", []):
        h["legacy"] = True
    tracked = {}
    for s in doc.get("signals", []):
        cond, _, idx = s["id"].partition(":")
        tracked[s["id"]] = {
            "id": s["id"], "conditionId": cond, "outcomeIndex": int(idx or 0),
            "title": s["title"], "outcome": s["outcome"],
            "eventSlug": s.get("eventSlug", ""), "endDate": s.get("endDate", ""),
            "cat": s.get("cat") or categoria(s["title"]),
            "firstSeen": s.get("firstSeen", now),
            "detectPrice": s.get("entryPrice") or s["curPrice"],
            "tierAtDetection": s.get("tier"), "staleAtDetection": s.get("stale", False),
            "alerted": False, "seeded": True, "maxTraders": s.get("numTraders", 0)}
    for w in doc.get("whales", []):
        w["legacy"] = True  # sin precio de seguidor: su ROI no seria comparable
    doc.update({"schema": SCHEMA, "tracked": tracked, "announcedSamples": [],
                "migratedAt": now})
    return doc


def detect_whales(trades, last_ts, top_wallets, min_usd=WHALE_MIN_USD,
                  max_price=MAX_ALERT_PRICE):
    """Compras nuevas (timestamp > last_ts) por encima de min_usd USD.
    Los SELL no son oportunidad de entrada y se ignoran. Las compras a cuota
    >= max_price se descartan: comprar a 0.99 asegura +poco% y no aporta ni
    recorrido ni informacion. isTop marca si el comprador esta ademas en el
    leaderboard vigilado."""
    whales, seen = [], set()
    for t in trades:
        usd = t["size"] * t["price"]
        if (t["side"] != "BUY" or t["timestamp"] <= last_ts
                or usd < min_usd or t["price"] >= max_price
                or t["transactionHash"] in seen):
            continue
        seen.add(t["transactionHash"])
        whales.append({
            "tx": t["transactionHash"],
            "wallet": t["proxyWallet"],
            "name": t.get("name") or t.get("pseudonym") or "",
            "usd": round(usd, 2),
            "price": t["price"],
            "title": t["title"],
            "outcome": t["outcome"],
            "eventSlug": t["eventSlug"],
            # conditionId+outcomeIndex para resolver el ROI cuando cierre el mercado
            "conditionId": t["conditionId"],
            "outcomeIndex": t["outcomeIndex"],
            "asset": t.get("asset", ""),
            "timestamp": t["timestamp"],
            "isTop": t["proxyWallet"] in top_wallets,
            "longshot": t["price"] <= LONGSHOT_MAX_PRICE,
        })
    whales.sort(key=lambda w: -w["timestamp"])
    return whales


def categoria(titulo):
    """Categoria del mercado por palabras clave del titulo (la API no la
    expone). Mismo criterio que la heuristica de la web."""
    t = titulo.lower()
    if re.search(r"bitcoin|\bbtc\b|ethereum|\beth\b|crypto|solana|\bxrp\b|doge", t):
        return "cripto"
    if re.search(r" vs\.? |world cup|mundial|\bnba\b|\bnfl\b|\bmlb\b|\bnhl\b|\bufc\b"
                 r"|\bf1\b|grand prix|premier|la ?liga|champions|europa league"
                 r"|bundesliga|serie a|\bopen\b|masters|\batp\b|\bwta\b|dota"
                 r"|league of legends|lol:|cs2|valorant|olympic|golden (ball|boot)"
                 r"|to advance", t):
        return "deportes"
    if re.search(r"election|president|senate|congress|governor|mayor|minister"
                 r"|parliament|trump|nominee|impeach|ceasefire|\bwar\b|tariff"
                 r"|\bfed\b|rate (cut|hike)|supreme court|shutdown", t):
        return "politica"
    return "otras"


def fetch_activity(wallet, since_ts=0, fetch=None):
    """Feed /activity del wallet PAGINADO hasta cubrir la ventana (since_ts) o
    ACTIVITY_MAX_PAGES paginas. Con una sola pagina de 500 eventos, en los
    wallets mas activos la "ventana de 30 dias" eran en realidad unos dias.
    Si la API ignorase el offset y repitiera la pagina, se detecta y se para."""
    fetch = fetch or get_json
    out, vistos = [], set()
    for page in range(ACTIVITY_MAX_PAGES):
        lote = fetch(ACTIVITY_URL.format(wallet=wallet, offset=page * ACTIVITY_PAGE)) or []
        nuevos = []
        for e in lote:
            k = (e.get("transactionHash"), e.get("type"), e.get("conditionId"),
                 e.get("outcomeIndex"), e.get("timestamp"), e.get("size"), e.get("usdcSize"))
            if k not in vistos:
                vistos.add(k)
                nuevos.append(e)
        out += nuevos
        if (not nuevos or len(lote) < ACTIVITY_PAGE
                or min(e.get("timestamp", 0) for e in lote) < since_ts):
            break
    return out


def track_record(wallet, posiciones, since_ts, fetch=None):
    """Rendimiento del wallet en la ventana. Devuelve (wins, losses, net, cats, edge):

    - wins/losses (TASA de acierto): canjes REDEEM del feed /activity (canjear
      implica tener el lado ganador) vs posiciones muertas (redeemable a ~0)
      que siguen en /positions.
    - net (PnL REAL en dolares): dinero que entra (ventas + canjes) menos el
      que sale (compras) mas el valor de mercado de lo que sigue abierto. Asi
      un wallet que gana 10 apuestas de $50 pero pierde una de $20k sale en
      NEGATIVO — cosa que la tasa de acierto por si sola no distingue.
    - edge ({"sum", "n"}): VENTAJA frente al precio pagado. Por cada mercado
      resuelto con precio de compra conocido suma (1 - precio) si gano o
      (0 - precio) si perdio. Un 90% de acierto comprando a 0.95 da ventaja
      negativa; un 40% comprando a 0.25 da ventaja positiva. Es la medida de
      habilidad que importa (la tasa de acierto premia apostar a favoritos).
      Tambien desglosada por categoria en cats[c]["edgeSum"/"edgeN"].

    /activity se pagina hasta cubrir la ventana (maximo 3.000 eventos); en
    los wallets mas activos del todo puede quedar algo corta.
    """
    fetch = fetch or get_json
    since_date = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    wins, cash_in, cash_out = {}, 0.0, 0.0
    buys = {}  # conditionId -> {outcomeIndex: [usd, acciones]}
    cats = {}  # acierto por categoria: mismo trader no vale igual en todas
    edge = {"sum": 0.0, "n": 0}

    def anota(titulo, campo, e=None):
        c = cats.setdefault(categoria(titulo), {"wins": 0, "losses": 0})
        c[campo] += 1
        if e is not None:
            c["edgeSum"] = round(c.get("edgeSum", 0.0) + e, 4)
            c["edgeN"] = c.get("edgeN", 0) + 1
            edge["sum"] = round(edge["sum"] + e, 4)
            edge["n"] += 1

    for e in fetch_activity(wallet, since_ts, fetch=fetch):
        if e.get("timestamp", 0) < since_ts:
            continue
        usd = e.get("usdcSize", 0) or 0
        if e.get("type") == "REDEEM":
            cash_in += usd
            if usd >= TRACK_MIN_USD and e["conditionId"] not in wins:
                wins[e["conditionId"]] = e.get("title", "")
        elif e.get("side") == "BUY":
            cash_out += usd
            if e.get("conditionId") and e.get("size"):
                b = buys.setdefault(e["conditionId"], {}).setdefault(e.get("outcomeIndex"), [0.0, 0.0])
                b[0] += usd
                b[1] += e["size"]
        elif e.get("side") == "SELL":
            cash_in += usd
    for cond, titulo in wins.items():
        lados = buys.get(cond, {})
        e = None
        # REDEEM no dice que lado se canjeo: solo se puede medir la ventaja si
        # el wallet compro un unico lado de ese mercado
        if len(lados) == 1:
            usd, acc = next(iter(lados.values()))
            if acc > 0:
                e = 1 - usd / acc
        anota(titulo, "wins", e)
    losses = set()
    for p in posiciones:
        if (p.get("redeemable") and p.get("curPrice", 1) <= 0.5
                and p.get("initialValue", 0) >= TRACK_MIN_USD
                and (p.get("endDate") or "") >= since_date
                and p["conditionId"] not in losses):
            losses.add(p["conditionId"])
            precio = p.get("avgPrice")
            anota(p.get("title", ""), "losses", -precio if precio else None)
    open_value = sum(p.get("currentValue", 0) for p in posiciones
                     if not p.get("redeemable"))
    net = round(cash_in - cash_out + open_value, 2)
    return len(wins), len(losses), net, cats, edge


def win_rate(t):
    """Tasa de acierto, o None si el historial es demasiado corto para fiarse."""
    n = t["wins"] + t["losses"]
    return round(t["wins"] / n, 2) if n >= WHALE_MIN_TRACK else None


def shrunk_edge(total, n, prior=EDGE_PRIOR_N):
    """Ventaja media encogida hacia 0: sum / (n + prior). Con 3 apuestas
    ganadas a 0.2 (+0.8 cada una) no sale +0.8 sino +0.18: poca muestra, poca
    confianza. None si no hay ninguna apuesta medible."""
    return round(total / (n + prior), 4) if n else None


def trader_edge(t, cat):
    """Ventaja encogida del trader en la categoria (si hay muestra) o global."""
    c = (t.get("cats") or {}).get(cat) or {}
    if c.get("edgeN", 0) >= WHALE_MIN_TRACK:
        return shrunk_edge(c["edgeSum"], c["edgeN"])
    e = t.get("edge") or {}
    return shrunk_edge(e.get("sum", 0), e.get("n", 0))


def activity_fetcher(fetch=None):
    """Lector de /activity con cache por escaneo: el track record y el momento
    de entrada de una senal leen el mismo feed, sin pedirlo dos veces."""
    fetch = fetch or get_json
    cache = {}

    def get(url):
        if url not in cache:
            cache[url] = fetch(url)
        return cache[url]
    return get


def update_track_records(cache, wallets, positions_by_trader, now_ts, fetch=None):
    """Refresca el track record solo de los wallets con cache caducada (TTL
    6 h); reutiliza las posiciones ya descargadas del top 50. Devuelve
    (tracks, positions_cache) — el segundo evita re-descargar posiciones de
    wallets whale para calcular su cartera."""
    fetch = fetch or get_json
    since_ts = max(now_ts - TRACK_WINDOW_DAYS * 86400, 0)
    tracks, positions_cache = {}, {}
    for w in wallets:
        old = cache.get(w)
        if old and now_ts - old.get("at", 0) < TRACK_TTL_S:
            tracks[w] = old
            continue
        try:
            if w in positions_by_trader:
                posiciones = positions_by_trader[w]["positions"]
            else:
                posiciones = fetch(POSITIONS_URL.format(wallet=w, threshold=100))
                positions_cache[w] = posiciones
                time.sleep(0.3)
            wins, losses, net, cats, edge = track_record(w, posiciones, since_ts, fetch=fetch)
            tracks[w] = {"wins": wins, "losses": losses, "net": net,
                         "cats": cats, "edge": edge, "at": now_ts}
            time.sleep(0.3)
        except Exception:
            tracks[w] = old or {"wins": 0, "losses": 0, "net": None, "at": 0}
    return tracks, positions_cache


def annotate_win_rates(signals, tracks):
    """Acierto 30d por trader y media por senal: mismo numero de traders
    coincidentes no vale lo mismo si su historial reciente es malo. Ademas,
    acierto EN LA CATEGORIA de la senal: un 70% en deportes es senal fuerte
    en un partido y dice poco en politica.

    Y la ventaja estimada de la senal (edgeEst): media de la ventaja encogida
    de sus traders. pEst = precio + edgeEst es una probabilidad estimada
    (heuristica SIN VALIDAR: se registra en cada senal para que el historico
    diga si sirve antes de usarla para filtrar)."""
    for s in signals:
        cat = categoria(s["title"])
        s["cat"] = cat
        rates, cat_rates, edges = [], [], []
        for t in s["traders"]:
            tr = tracks.get(t["wallet"], {"wins": 0, "losses": 0})
            r = win_rate(tr)
            t["winRate"] = r
            if r is not None:
                rates.append(r)
            c = (tr.get("cats") or {}).get(cat)
            cr = None
            if c and c["wins"] + c["losses"] >= WHALE_MIN_TRACK:
                cr = round(c["wins"] / (c["wins"] + c["losses"]), 2)
            t["catWinRate"] = cr
            if cr is not None:
                cat_rates.append(cr)
            e = trader_edge(tr, cat)
            t["edge"] = e
            if e is not None:
                edges.append(e)
        s["avgWinRate"] = round(sum(rates) / len(rates), 2) if rates else None
        s["avgCatWinRate"] = (round(sum(cat_rates) / len(cat_rates), 2)
                              if cat_rates else None)
        s["edgeEst"] = round(sum(edges) / len(edges), 4) if edges else None
        if s["edgeEst"] is not None:
            p = min(max(s["curPrice"] + s["edgeEst"], 0.01), 0.99)
            s["pEst"] = round(p, 4)
            s["expRoi"] = metrics.roi(p, min(s["curPrice"] + ENTRY_COST, 0.999))
        else:
            s["pEst"] = s["expRoi"] = None
    return signals


def entry_timing(s, activity, now_ts, hours=RECENT_ENTRY_HOURS):
    """Cuando compraron los traders de la senal: ultima compra de cada uno en
    ese mercado y lado (de su feed /activity). recentEntrants = cuantos
    compraron en las ultimas `hours` horas: 4 traders entrando hoy no es lo
    mismo que 4 posiciones de hace un mes que simplemente siguen ahi."""
    last, recent = None, 0
    for t in s["traders"]:
        try:
            eventos = fetch_activity(t["wallet"], now_ts - TRACK_WINDOW_DAYS * 86400,
                                     fetch=activity)
        except Exception:
            continue
        compras = [e.get("timestamp", 0) for e in eventos
                   if e.get("side") == "BUY" and e.get("conditionId") == s["conditionId"]
                   and e.get("outcomeIndex") == s["outcomeIndex"]]
        if compras:
            t["lastBuyTs"] = max(compras)
            last = max(last or 0, t["lastBuyTs"])
            if now_ts - t["lastBuyTs"] <= hours * 3600:
                recent += 1
    s["lastEntryTs"] = last
    s["recentEntrants"] = recent
    return s


def qualify_signals(signals, min_broad=MIN_USERS, min_specialist=MIN_USERS_SPECIALIST,
                    min_winrate=SPECIALIST_MIN_WINRATE, min_edge=SPECIALIST_MIN_EDGE):
    """Dos niveles de coincidencia (requiere annotate_win_rates ya aplicado):

    - consenso amplio (>= min_broad traders): siempre señal.
    - especialistas (>= min_specialist traders): si su acierto 30d medio
      probado es >= min_winrate (el de su categoria si hay muestra, si no el
      global) O si su ventaja estimada frente al precio es >= min_edge.
      `specialistBy` dice por que criterio(s) cualifico, para medir cada uno
      por separado: el backtest dio ROI -6% al criterio de acierto (que premia
      apostar a favoritos), y la ventaja frente al precio es la alternativa.

    Se descartan las coincidencias de 3-4 traders sin ninguno de los dos.
    Una senal que cae de nivel entre escaneos sigue en seguimiento
    (track_signals) hasta que su mercado resuelve: no se pierde su veredicto.
    """
    out = []
    for s in signals:
        broad = s["numTraders"] >= min_broad
        wr = s.get("avgCatWinRate")
        if wr is None:
            wr = s.get("avgWinRate")
        by = []
        if s["numTraders"] >= min_specialist:
            if wr is not None and wr >= min_winrate:
                by.append("acierto")
            if s.get("edgeEst") is not None and s["edgeEst"] >= min_edge:
                by.append("ventaja")
        if broad or by:
            s["tier"] = "amplio" if broad else "especialistas"
            s["specialistBy"] = by
            out.append(s)
    return out


def enrich_whales(new_whales, prev_whales, tracks, positions_cache, positions_by_trader):
    """Contexto por wallet: cartera abierta, recurrencia y acierto 30d."""
    counts = {}
    for w in prev_whales:
        counts[w["wallet"]] = counts.get(w["wallet"], 0) + 1
    for w in new_whales:
        w["repeatBuys"] = counts.get(w["wallet"], 0) + 1
        counts[w["wallet"]] = w["repeatBuys"]
        posiciones = (positions_cache.get(w["wallet"])
                      or positions_by_trader.get(w["wallet"], {}).get("positions"))
        w["walletUsd"] = (round(sum(p.get("currentValue", 0) for p in posiciones), 2)
                          if posiciones is not None else None)
        t = tracks.get(w["wallet"])
        w["winRate"] = win_rate(t) if t else None
        w["netPnl"] = t.get("net") if t else None
        # solo se guardan wins/losses cuando hay veredicto (winRate no None):
        # el conteo crudo de una whale sin historial suficiente ya se leeria
        # como "va perdiendo" aunque no este confirmado
        w["wins30d"] = t["wins"] if t and w["winRate"] is not None else None
        w["losses30d"] = t["losses"] if t and w["winRate"] is not None else None
    return new_whales


def whale_notifiable(w):
    """Nunca se guarda ni se avisa de una whale perdedora, sea o no del top 50
    -- estar en el top 50 no borra que vaya perdiendo. Con PnL real conocido
    manda el dinero: neto positivo pasa, negativo fuera (un buen acierto en
    numero de apuestas no salva perder $300k). Sin PnL suficiente cae a la
    tasa de acierto, o al top 50 de comodin."""
    net = w.get("netPnl")
    if net is not None:
        return net > 0
    r = w.get("winRate")
    return r >= WHALE_MIN_WINRATE if r is not None else w["isTop"]


def format_whales(whales, cap=10):
    lines = ["\U0001F40B Compras grandes en Polymarket:"]
    for w in whales[:cap]:
        top = " (TOP 50)" if w["isTop"] else ""
        insider = " \U0001F575 LONGSHOT" if w.get("longshot") else ""
        quien = w["name"] or w["wallet"][:10]
        extra = ""
        if w.get("netPnl") is not None:
            extra += f" | PnL 30d ${w['netPnl']:,.0f}"
        if w.get("winRate") is not None:
            extra += (f" | acierto 30d {round(w['winRate'] * 100)}%"
                      f" ({w['wins30d']}✓ {w['losses30d']}✗)")
        if w.get("walletUsd"):
            extra += f" | cartera ${w['walletUsd']:,.0f}"
        if w.get("repeatBuys", 0) > 1:
            extra += f" | {w['repeatBuys']}ª compra grande"
        lines.append(
            f"\n• ${w['usd']:,.0f} → {w['title']} [{w['outcome']}] @ {w['price']}{insider}"
            f"\n  por {quien}{top}{extra}"
            f"\n  https://polymarket.com/event/{w['eventSlug']}")
    if len(whales) > cap:
        lines.append(f"\n…y {len(whales) - cap} mas")
    return "\n".join(lines)


def _pct(x):
    return f"{'+' if x >= 0 else ''}{round(x * 100)}%"


def format_message(new_signals, cap=10):
    lines = ["\U0001F6A8 Nuevas coincidencias de top traders en Polymarket:"]
    for s in new_signals[:cap]:
        acierto = (f" | acierto medio 30d {round(s['avgWinRate'] * 100)}%"
                   if s.get("avgWinRate") is not None else "")
        if s.get("avgCatWinRate") is not None:
            acierto += f" | en {s.get('cat', '?')} {round(s['avgCatWinRate'] * 100)}%"
        nivel = " especialistas \U0001F3AF" if s.get("tier") == "especialistas" else ""
        entrada = (f" | entrada real {s['entryPrice']}" if s.get("entryPrice") else "")
        extra = ""
        if s.get("recentEntrants"):
            extra += f"\n  {s['recentEntrants']} entraron en las ultimas {RECENT_ENTRY_HOURS} h"
        if s.get("edgeEst") is not None:
            extra += (f"\n  ventaja estimada {_pct(s['edgeEst'])} (sin validar)")
        lines.append(
            f"\n• {s['numTraders']} traders{nivel} → {s['title']} [{s['outcome']}]"
            f"\n  precio {s['curPrice']}{entrada} | entrada media traders {s['avgEntryPrice']}"
            f" | ${s['totalUsd']:,.0f}{acierto}{extra}"
            f"\n  https://polymarket.com/event/{s['eventSlug']}")
    if len(new_signals) > cap:
        lines.append(f"\n…y {len(new_signals) - cap} mas")
    return "\n".join(lines)


def format_resolved(resolved, cap=10):
    """Aviso de ALERTAS que acaban de resolver, con su ROI real (entrada al
    precio del aviso). Las senales que nunca se avisaron no se notifican."""
    lines = ["\U0001F3C1 Alertas resueltas en Polymarket (ROI real):"]
    for h in resolved[:cap]:
        verdict = ("✅ gano" if h["won"] else
                   "➖ anulado (50-50)" if h.get("payout") == 0.5 else "❌ perdio")
        lines.append(
            f"\n• {verdict} → {h['title']} [{h['outcome']}]"
            f"\n  {h['numTraders']} traders · entrada {h['entryPrice']}"
            f" · ROI {_pct(h['roi'])}"
            f"\n  https://polymarket.com/event/{h.get('eventSlug', '')}")
    if len(resolved) > cap:
        lines.append(f"\n…y {len(resolved) - cap} mas")
    return "\n".join(lines)


FUENTE_NOMBRE = {"coincidencia": "alertas de coincidencias",
                 "coincidencia:amplio": "alertas de consenso amplio",
                 "coincidencia:especialistas": "alertas de especialistas",
                 "whale": "compras whale"}


def format_sample_reached(src, st):
    """Aviso de una sola vez: una fuente alcanzo muestra suficiente y ya se
    puede juzgar si paga. El veredicto exige que el intervalo de confianza
    entero este por encima de 0 para llamarlo rentable."""
    ci = st.get("ci95")
    ic = f" (IC95 {_pct(ci[0])} a {_pct(ci[1])})" if ci else ""
    veredicto = {
        "rentable": "es RENTABLE con confianza estadistica: sigue avisando.",
        "no concluyente": "ROI positivo pero NO concluyente todavia: sigue avisando y midiendo.",
        "pierde": "NO ha sido rentable: se silencia (se reactivara si vuelve a positivo).",
    }.get(st.get("verdict"), "sigue midiendo.")
    return (f"\U0001F4CA Ya hay muestra suficiente para '{FUENTE_NOMBRE.get(src, src)}': "
            f"{st['n']} señales resueltas.\n"
            f"ROI medio {_pct(st['avgRoi'])}{ic} | {st['wins']}/{st['n']} aciertos"
            f" (el precio implicaba {round((st.get('avgEntry') or 0) * 100)}%).\n"
            f"→ {veredicto}")


def recipients():
    """Destinatarios de alertas, cada uno con sus umbrales. Formatos:
    - TELEGRAM_RECIPIENTS (JSON): [{"chatId": "...", "minUsers": 7,
      "whaleMinUsd": 100000}, ...] — umbrales opcionales.
    - TELEGRAM_CHAT_ID: uno o varios chat ids separados por comas.

    minUsers None (por defecto) = todas las senales cualificadas, incluidas
    las de especialistas (3-4 traders). Antes caia a MIN_USERS (5) y las de
    especialistas no llegaban nunca.
    """
    raw = os.getenv("TELEGRAM_RECIPIENTS", "")
    if raw:
        return [{"chatId": str(r["chatId"]),
                 "minUsers": r.get("minUsers"),
                 "whaleMinUsd": r.get("whaleMinUsd", WHALE_MIN_USD)}
                for r in json.loads(raw)]
    return [{"chatId": c.strip(), "minUsers": None, "whaleMinUsd": WHALE_MIN_USD}
            for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]


def for_recipient(signals, r):
    return [s for s in signals if r["minUsers"] is None or s["numTraders"] >= r["minUsers"]]


def send_telegram(text, chat_id):
    """True si Telegram acepto el mensaje, False si fallo (red, token, limite),
    None si se omite a proposito (sin secrets o en prueba en seco). Nunca
    lanza: un fallo de Telegram no debe tumbar el escaneo ni perder el estado."""
    if DRY_RUN or not (TELEGRAM_TOKEN and chat_id and text):
        return None
    body = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30)
        return True
    except Exception as exc:  # noqa: BLE001 - se registra y se reintenta
        print(f"AVISO: Telegram no acepto el mensaje para {chat_id}: {exc}")
        return False


def deliver(messages, outbox, now):
    """Envia `messages` [(chat_id, texto, ids)] y reintenta la cola `outbox`
    de envios fallidos anteriores. Lo que falla vuelve a la cola, que se
    reintenta cada escaneo durante OUTBOX_TTL_MIN. Devuelve (cola, entregados)
    con entregados = {id: True/False/None} por alerta (None = sin envio)."""
    cola, entregados = [], {}
    limite = (datetime.fromisoformat(now) - timedelta(minutes=OUTBOX_TTL_MIN)).isoformat()
    for m in outbox:
        if m["at"] < limite:
            print(f"AVISO: se descarta un aviso sin entregar desde {m['at']} para {m['chatId']}")
        elif send_telegram(m["text"], m["chatId"]) is False:
            cola.append(m)
    for chat, texto, ids in messages:
        ok = send_telegram(texto, chat)
        if ok is False:
            cola.append({"chatId": chat, "text": texto, "at": now})
        for i in ids:  # entregada si llego a algun destinatario
            if ok is True or entregados.get(i) is True:
                entregados[i] = True
            elif ok is False or entregados.get(i) is False:
                entregados[i] = False
            else:
                entregados[i] = None
    return cola, entregados


def main():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    now_ts = int(time.time())
    today = now[:10]
    try:
        leaderboard = get_json(LEADERBOARD_URL.format(n=TOP_N))
    except FetchError as exc:
        print(f"AVISO: {exc}; se conservan los datos anteriores")
        return
    positions_by_trader = {}
    failed_wallets = []
    for entry in leaderboard:
        wallet = entry["proxyWallet"]
        url = POSITIONS_URL.format(wallet=wallet, threshold=int(MIN_POSITION_USD))
        try:
            posiciones = get_json(url)
        except FetchError:
            failed_wallets.append(wallet)
            continue
        positions_by_trader[wallet] = {
            "name": entry.get("userName", ""),
            "positions": posiciones,
        }
        time.sleep(0.3)  # ~50 requests por escaneo: sin prisa, evita rate limits

    # Un snapshot parcial cambiaria el conjunto de senales por un fallo de red.
    # Conservamos el JSON anterior y reintentamos en la siguiente vuelta.
    if failed_wallets:
        print(f"AVISO: fallaron {len(failed_wallets)}/{len(leaderboard)} wallets; "
              "se conservan los datos anteriores")
        return

    # los bots/market makers (cientos de posiciones) no aportan senal
    scannable = {w: info for w, info in positions_by_trader.items()
                 if len(info["positions"]) <= MAX_POSITIONS_PER_TRADER}
    signals = detect_signals(scannable, min_users=MIN_USERS_SPECIALIST)  # candidatos (>=3)

    doc = {}
    if SIGNALS_PATH.exists():
        doc = json.loads(SIGNALS_PATH.read_text(encoding="utf-8"))
    first_run = not doc  # sin estado previo: fijar marcas sin avisar
    doc = migrate(doc, now)
    history, tracked = doc.get("history", []), doc.get("tracked", {})

    # segunda fuente: compras individuales gigantes (feed global de trades)
    try:
        trades = get_json(TRADES_URL.format(amount=int(WHALE_MIN_USD)))
    except FetchError as exc:
        print(f"AVISO: feed de whales no disponible ({exc}); continuo sin whales nuevas")
        trades = []
    last_whale_ts = doc.get("lastWhaleTs", 0)
    new_whales = detect_whales(trades, last_whale_ts, set(positions_by_trader))
    activity = activity_fetcher()
    wallets_needed = set(positions_by_trader) | {w["wallet"] for w in new_whales}
    tracks, positions_cache = update_track_records(
        doc.get("trackRecords", {}), wallets_needed, positions_by_trader, now_ts,
        fetch=activity)
    enrich_whales(new_whales, doc.get("whales", []), tracks, positions_cache,
                  positions_by_trader)
    # acierto y ventaja por trader/categoria y luego el gate de dos niveles
    annotate_win_rates(signals, tracks)
    signals = qualify_signals(signals)
    nuevos = set(track_signals(tracked, signals, now))
    active_ids = {s["id"] for s in signals}

    # estadisticas ANTES de alertar: el gate decide con lo ya resuelto
    stats = source_stats(history)

    def paga(key):
        st = stats.get(key)
        return not (st and st["suppressed"])

    # alertas: una por senal en toda su vida, en el primer escaneo en que es
    # fresca (aunque naciera descartada y el precio volviera despues)
    # Las heredadas de la migracion (seeded) no se alertan: el codigo anterior
    # pudo avisarlas ya y no se sabe a que precio.
    alerts = []
    for s in signals:
        t = tracked[s["id"]]
        quiere = (not (t["alerted"] or t.get("seeded") or first_run)) and alertable(s)
        if s["id"] in nuevos or quiere:
            entry_timing(s, activity, now_ts)
            t["recentEntrants"] = s.get("recentEntrants")
            t["lastEntryTs"] = s.get("lastEntryTs")
        else:
            s.setdefault("recentEntrants", t.get("recentEntrants"))
            s.setdefault("lastEntryTs", t.get("lastEntryTs"))
        if not quiere:
            continue
        price, source = entry_price(s.get("asset"), s["curPrice"])
        sent = (s.get("tier") in ALERT_TIERS and paga("coincidencia")
                and paga(f"coincidencia:{s.get('tier')}"))
        mark_alerted(t, s, price, source, now, sent)
        if sent:
            alerts.append(s)

    # whales: una con historial perdedor no se guarda ni se muestra en ningun
    # sitio (ni la nueva ni las que ya estuvieran guardadas de antes)
    detected = len(new_whales)
    new_whales = [w for w in new_whales if whale_notifiable(w)]
    whales_pending = doc.get("whalesPending", [])
    for w in new_whales:  # precio al que entraria un seguidor al verla
        w["followPrice"], w["followSource"] = entry_price(w.get("asset"), w["price"])
        whales_pending.append({
            "id": f'{w["conditionId"]}:{w["outcomeIndex"]}', "conditionId": w["conditionId"],
            "outcomeIndex": w["outcomeIndex"], "title": w["title"], "outcome": w["outcome"],
            "eventSlug": w.get("eventSlug", ""), "entryPrice": w["followPrice"],
            "whalePrice": w["price"], "ts": w["timestamp"],
            "at": datetime.fromtimestamp(w["timestamp"], tz=timezone.utc).isoformat(timespec="seconds")})
    prev_whales = [w for w in doc.get("whales", [])
                   if whale_notifiable(w) and w["price"] < MAX_ALERT_PRICE]
    all_whales = new_whales + prev_whales

    # compras nuevas de los top traders desde el escaneo anterior (investigacion)
    entries_src = SIGNALS_PATH.parent / ENTRIES_PATH_NAME
    entries_path = (pathlib.Path(DRY_RUN_PATH).with_name("entries.dry.json") if DRY_RUN
                    else entries_src)
    ent_doc = (json.loads(entries_src.read_text(encoding="utf-8"))
               if entries_src.exists() else {})
    ent_pending, ent_resolved = ent_doc.get("pending", []), ent_doc.get("resolved", [])
    nuevas_compras = detect_entries(ent_doc.get("sizes", {}), scannable, tracks, now, ent_pending)

    # oraculo unico (gamma) para senales seguidas, whales y compras: el
    # veredicto nunca depende de que alguien siga dentro del mercado
    due = ([t["conditionId"] for t in tracked.values() if needs_oracle(t, active_ids, today)]
           + [w["conditionId"] for w in whales_pending if oracle_due(w, now_ts, today)]
           + [e["conditionId"] for e in ent_pending if oracle_due(e, now_ts, today)])
    resolutions = fetch_market_resolutions(due)
    queried = set(due)
    resolved = resolve_tracked(tracked, resolutions, now, active_ids)
    expire_before = (datetime.fromisoformat(now) - timedelta(days=TRACK_EXPIRE_DAYS)).strftime("%Y-%m-%d")
    whales_pending, resolved_wh, cad_wh = resolve_pending(
        whales_pending, resolutions, now, "whale",
        max_age_ts=now_ts - WHALE_PENDING_DAYS * 86400, now_ts=now_ts, queried=queried)
    ent_pending, resolved_ent, cad_ent = resolve_pending(
        ent_pending, resolutions, now, "entrada", expire_before=expire_before, now_ts=now_ts,
        queried=queried)
    ent_resolved += resolved_ent
    # la lista de la web solo pierde las whales cuyo mercado ya liquido
    whales = [w for w in all_whales if w.get("conditionId") not in resolutions][:WHALES_KEEP]
    last_whale_ts = max([last_whale_ts] + [t["timestamp"] for t in trades])
    history += resolved + resolved_wh
    stats = source_stats(history, entries=ent_resolved)
    # cartera simulada = lo que te llega por Telegram (alertas enviadas, no
    # las de sombra): "si hubiera apostado en cada aviso que recibi"
    paper = metrics.paper_ledger(
        [h for h in history if h.get("source") == "coincidencia" and not h.get("legacy")
         and h.get("alerted") and h.get("sent") and not h.get("seeded")], PAPER_STAKE)

    # se guarda el track record de todos los wallets vigentes (top + whales
    # visibles), perdedores incluidos: asi no se re-descargan en cada escaneo
    vigentes = set(positions_by_trader) | {w["wallet"] for w in whales}
    tracks = {w: t for w, t in tracks.items() if w in vigentes}

    # aviso de una sola vez cuando una fuente de alertas alcanza muestra
    announced = set(doc.get("announcedSamples", []))
    reached = [k for k, st in stats.items() if k != "investigacion"
               and st["n"] >= SOURCE_MIN_SAMPLE and k not in announced]
    announced |= set(reached)

    # SOLO se notifican coincidencias por Telegram. Se envia ANTES de guardar
    # para registrar si se entrego; lo que falla queda en cola y se reintenta.
    resueltas_alertas = [h for h in resolved if h.get("alerted") and h.get("roi") is not None
                         and h.get("sent")]
    mensajes = []
    for r in recipients():
        mias = for_recipient(alerts, r)
        if mias:
            mensajes.append((r["chatId"], format_message(mias), [s["id"] for s in mias]))
        if resueltas_alertas:
            mensajes.append((r["chatId"], format_resolved(resueltas_alertas), []))
        for k in reached:
            if k.startswith("coincidencia"):
                mensajes.append((r["chatId"], format_sample_reached(k, stats[k]), []))
    outbox, entregados = deliver(mensajes, doc.get("outbox", []), now)
    for sid, ok in entregados.items():
        if sid in tracked:
            tracked[sid]["delivered"] = ok

    out_path = pathlib.Path(DRY_RUN_PATH) if DRY_RUN else SIGNALS_PATH
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps({
        "schema": SCHEMA,
        "updatedAt": now,
        "params": {"topN": TOP_N, "minUsers": MIN_USERS,
                   "minUsersSpecialist": MIN_USERS_SPECIALIST,
                   "specialistMinWinRate": SPECIALIST_MIN_WINRATE,
                   "specialistMinEdge": SPECIALIST_MIN_EDGE,
                   "minPositionUsd": MIN_POSITION_USD,
                   "maxPriceDrift": MAX_PRICE_DRIFT, "scanEveryMin": SCAN_EVERY_MIN,
                   "maxPositionsPerTrader": MAX_POSITIONS_PER_TRADER,
                   "whaleMinUsd": WHALE_MIN_USD, "maxAlertPrice": MAX_ALERT_PRICE,
                   "entryCost": ENTRY_COST, "paperStake": PAPER_STAKE,
                   "minEdge": MIN_EDGE, "sourceMinSample": SOURCE_MIN_SAMPLE,
                   "alertTiers": sorted(ALERT_TIERS), "entryMinUsd": ENTRY_MIN_USD},
        "migratedAt": doc.get("migratedAt"),
        "signals": signals,
        "tracked": tracked,
        "whales": whales,
        "whalesPending": whales_pending,
        "lastWhaleTs": last_whale_ts,
        "trackRecords": tracks,
        "sourceStats": stats,
        "paper": paper,
        "announcedSamples": sorted(announced),
        "outbox": outbox,
        "history": history,
    }, indent=1), encoding="utf-8")
    entries_path.write_text(json.dumps({
        "updatedAt": now,
        "sizes": position_sizes(scannable),
        "pending": ent_pending,
        "resolved": ent_resolved,
    }, separators=(",", ":")), encoding="utf-8")

    stale_count = sum(1 for s in signals if s["stale"])
    bots = len(positions_by_trader) - len(scannable)
    sombra = sum(1 for s in signals if s.get("alertedAt") == now) - len(alerts)
    print(f"{len(signals)} senales ({stale_count} stale), {len(nuevos)} nuevas, "
          f"{len(alerts)} alertadas{f' (+{sombra} en sombra)' if sombra else ''} | "
          f"{len(tracked)} en seguimiento | {len(new_whales)}/{detected} whales "
          f"con historial ganador | {bots} wallets excluidos por bot | "
          f"{len(resolved)} senales y {len(resolved_wh)} whales resueltas | "
          f"{len(nuevas_compras)} compras nuevas, {len(ent_pending)} pendientes, "
          f"{len(resolved_ent)} resueltas | caducadas sin liquidar: "
          f"{cad_wh} whales, {cad_ent} compras | {len(outbox)} avisos en cola")
    for k, st in sorted(stats.items()):
        if st["n"]:
            marca = " [SILENCIADA: pierde dinero]" if st["suppressed"] else ""
            print(f"  {k}: n={st['n']} ({st['events']} eventos) ROI medio {_pct(st['avgRoi'])} "
                  f"IC95 {st['ci95']} -> {st['verdict']}{marca}")
    for s in signals:
        if s["id"] in nuevos:
            tag = "STALE" if s["stale"] else "NUEVA"
            print(f"  {tag}: {s['numTraders']} traders -> {s['title']} [{s['outcome']}] @ {s['curPrice']}")


if __name__ == "__main__":
    main()

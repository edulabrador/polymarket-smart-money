"""Escaneo completo (main) de punta a punta SIN red: la API de Polymarket se
simula por URL. Comprueba el ciclo de vida que importa para que el historico
sea fiable: migracion del estado real, deteccion, alerta UNA vez con precio de
libro, perdedora que desaparece pero se resuelve igual, y Telegram solo con lo
que de verdad se aviso. `python tests/test_main.py` o `pytest`."""
import json
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import scanner  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
WALLETS = [f"0xw{i}" for i in range(6)]


def position(cond, idx, value, cur, avg=0.45, asset="tok"):
    return {"conditionId": cond, "outcomeIndex": idx, "outcome": ["Yes", "No"][idx],
            "title": f"Mercado {cond}", "slug": cond, "eventSlug": f"ev-{cond}",
            "endDate": "2026-12-31", "curPrice": cur, "currentValue": value,
            "avgPrice": avg, "redeemable": False, "asset": asset, "initialValue": value,
            "size": round(value / cur, 2)}


class FakeApi:
    """Router de URLs -> respuestas. `state` cambia entre escaneos."""

    def __init__(self):
        self.positions = {}
        self.resolved = {}
        self.starts = {"0xA": "2099-01-01 18:00:00+00"}  # partido aun por empezar
        self.calls = []

    def __call__(self, url, retries=3):
        self.calls.append(url)
        if "/v1/leaderboard" in url:
            return [{"proxyWallet": w, "userName": w} for w in WALLETS]
        if "/positions" in url:
            w = url.split("user=")[1].split("&")[0]
            return self.positions.get(w, [])
        if "/trades" in url:
            return []
        if "/activity" in url:
            return []
        if "/book" in url:
            return {"asks": [{"price": "0.47", "size": "100000"}]}
        if "gamma-api" in url and "closed=true" not in url:   # metadatos (hora de inicio)
            return [{"conditionId": c, "gameStartTime": g}
                    for c, g in self.starts.items() if f"condition_ids={c}" in url]
        if "gamma-api" in url:
            return [{"conditionId": c, "closed": True, "outcomePrices": json.dumps(p)}
                    for c, p in self.resolved.items() if f"condition_ids={c}" in url]
        raise AssertionError(f"URL inesperada {url}")


def run_scan(api, path, sent, ok=True):
    scanner.get_json = api
    scanner.SIGNALS_PATH = path
    scanner.send_telegram = lambda text, chat: (sent.append(text), ok)[1]
    scanner.time.sleep = lambda _: None
    scanner.main()
    return json.loads(path.read_text(encoding="utf-8"))


def test_full_lifecycle_on_real_state():
    tmp = pathlib.Path(tempfile.mkdtemp())
    path = tmp / "signals.json"
    shutil.copy(ROOT / "docs" / "signals.json", path)  # estado REAL de produccion
    viejo = json.loads(path.read_text(encoding="utf-8"))
    import os
    os.environ["TELEGRAM_CHAT_ID"] = "1"
    originals = (scanner.get_json, scanner.SIGNALS_PATH, scanner.send_telegram,
                 scanner.time.sleep, scanner.LIVE_ALERTS, scanner.WEEKLY_SUMMARY)
    # este test recorre el envio en vivo; el modo por defecto (en silencio +
    # resumen semanal) tiene su propio test abajo
    scanner.LIVE_ALERTS, scanner.WEEKLY_SUMMARY = True, False
    try:
        api, sent = FakeApi(), []
        # escaneo 1: 5 traders en 0xA:Yes (fresca, 0.5 vs entrada 0.45) y 4 en
        # 0xB:No (descartada por deriva: 0.8 vs 0.45)
        for w in WALLETS[:5]:
            api.positions[w] = [position("0xA", 0, 2000, 0.5)]
        for w in WALLETS[:4]:
            api.positions[w].append(position("0xB", 1, 2000, 0.8))
        # Telegram CAIDO en el primer aviso: no se pierde, queda en cola
        d = run_scan(api, path, sent, ok=False)

        assert d["schema"] == 2
        # el historico viejo se conserva entero, pero en su propio archivo
        n_legacy = sum(1 for h in viejo.get("history", []) if h.get("legacy")
                       or viejo.get("schema") != 2)
        if n_legacy:  # estado anterior a la separacion: se mueve al archivo aparte
            legacy = json.loads((tmp / "history_legacy.json").read_text(encoding="utf-8"))
            assert len(legacy) == n_legacy and all(h.get("legacy") for h in legacy)
        assert not any(h.get("legacy") for h in d["history"])
        # produccion ya tiene alertas resueltas: se comprueban los INCREMENTOS
        base_st, base_paper = d["sourceStats"], d["paper"]
        base = lambda k, f="n": (base_st.get(k) or {}).get(f) or 0
        a = d["tracked"]["0xA:0"]
        assert a["alerted"] and a["entryPrice"] == 0.47 and a["entrySource"] == "libro"
        assert d["tracked"]["0xA:0"]["detectPrice"] == 0.5
        # 0xB tiene 4 traders sin acierto probado: no cualifica, no se sigue
        assert "0xB:1" not in d["tracked"]
        assert len(sent) == 1 and "Mercado 0xA" in sent[0] and "entrada real 0.47" in sent[0]
        assert a["delivered"] is False and len(d["outbox"]) == 1
        # hora de inicio: alertada ANTES del partido
        assert a["gameStart"] == "2099-01-01T18:00:00+00:00" and a["phaseAtAlert"] == "previa"
        ent = json.loads((tmp / "entries.json").read_text(encoding="utf-8"))
        assert ent["pending"] == [] and "0xw0" in ent["sizes"]   # 1a foto: sin compras

        # escaneo 2 (Telegram vuelve): se reintenta la cola; misma senal -> NO
        # se re-alerta; y 3 especialistas nuevos
        # en 0xE se alertan EN SOMBRA (nivel fuera de ALERT_TIERS por defecto)
        for w in WALLETS[:3]:
            api.positions[w].append(position("0xE", 0, 2000, 0.4, avg=0.38))
        scanner_tracks = json.loads(path.read_text(encoding="utf-8"))
        for w in WALLETS[:3]:  # historial probado alto -> especialistas
            scanner_tracks["trackRecords"][w] = {"wins": 9, "losses": 1, "net": 1.0,
                                                 "at": 4102444800}
        path.write_text(json.dumps(scanner_tracks), encoding="utf-8")
        sent.clear()
        d = run_scan(api, path, sent)
        assert len(sent) == 1 and "Mercado 0xA" in sent[0] and d["outbox"] == []
        assert d["tracked"]["0xA:0"]["entryPrice"] == 0.47
        # 3 top traders compraron 0xE desde el escaneo anterior: UNA compra
        # registrada con los 3 wallets, para medir su ROI con el oraculo
        ent = json.loads((tmp / "entries.json").read_text(encoding="utf-8"))
        assert [e["id"] for e in ent["pending"]] == ["0xE:0"]
        assert len(ent["pending"][0]["wallets"]) == 3
        e = d["tracked"]["0xE:0"]
        assert e["tier"] == "especialistas" and e["alerted"] and e["sent"] is False

        # escaneo 3: va perdiendo, sus posiciones bajan de $500 y la senal
        # DESAPARECE. Antes eso borraba la perdida del historico.
        for w in WALLETS[:5]:
            api.positions[w] = [position("0xA", 0, 120, 0.06)]
        d = run_scan(api, path, sent)
        assert d["signals"] == [] and "0xA:0" in d["tracked"]  # sigue en seguimiento

        # escaneo 4: el mercado liquida a favor de "No": se registra la PERDIDA;
        # 0xE liquida a favor de "Yes": la compra registrada gana
        api.resolved["0xA"] = ["0", "1"]
        api.resolved["0xE"] = ["1", "0"]
        for e in ent["pending"]:  # fecha de fin ya pasada: toca consultar el oraculo
            e["endDate"] = "2026-01-01"
        (tmp / "entries.json").write_text(json.dumps(ent), encoding="utf-8")
        sent.clear()
        d = run_scan(api, path, sent)
        h = [x for x in d["history"] if x.get("id") == "0xA:0" and not x.get("legacy")]
        assert len(h) == 1 and h[0]["won"] is False and h[0]["roi"] == -1.0
        assert "0xA:0" not in d["tracked"]
        # el ROI de alertas incluye las de sombra (0xE, especialistas, gana);
        # por nivel: amplio pierde la de 0xA, especialistas gana la de 0xE
        st = d["sourceStats"]
        assert st["coincidencia"]["n"] == base("coincidencia") + 2
        assert st["coincidencia:amplio"]["n"] == base("coincidencia:amplio") + 1
        assert st["coincidencia:amplio"]["wins"] == base("coincidencia:amplio", "wins")
        assert st["coincidencia:previa"]["n"] == base("coincidencia:previa") + 1   # 0xA
        assert st["coincidencia:especialistas"]["n"] == base("coincidencia:especialistas") + 1
        assert st["coincidencia:especialistas"]["wins"] == base("coincidencia:especialistas", "wins") + 1
        # cartera simulada: solo la enviada (0xA, pierde $100)
        assert d["paper"]["bets"] == (base_paper.get("bets") or 0) + 1
        assert d["paper"]["pnl"] == round((base_paper.get("pnl") or 0) - 100.0, 2)
        assert len(sent) == 1 and "perdio" in sent[0] and "ROI -100%" in sent[0]
        ent = json.loads((tmp / "entries.json").read_text(encoding="utf-8"))
        assert ent["pending"] == [] and ent["resolved"][0]["won"]
        assert d["sourceStats"]["entrada"]["n"] == 1
    finally:
        (scanner.get_json, scanner.SIGNALS_PATH, scanner.send_telegram,
         scanner.time.sleep, scanner.LIVE_ALERTS, scanner.WEEKLY_SUMMARY) = originals
        del os.environ["TELEGRAM_CHAT_ID"]
        shutil.rmtree(tmp)


def test_silent_alerts_and_weekly_summary():
    """Por defecto: las coincidencias se alertan y miden igual, pero no se
    envian; por Telegram solo sale el resumen semanal, una vez por semana."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    path = tmp / "signals.json"
    shutil.copy(ROOT / "docs" / "signals.json", path)
    import os
    os.environ["TELEGRAM_CHAT_ID"] = "1"
    originals = (scanner.get_json, scanner.SIGNALS_PATH, scanner.send_telegram,
                 scanner.time.sleep, scanner.LIVE_ALERTS, scanner.WEEKLY_SUMMARY)
    scanner.LIVE_ALERTS, scanner.WEEKLY_SUMMARY = False, True
    try:
        api, sent = FakeApi(), []
        for w in WALLETS[:5]:
            api.positions[w] = [position("0xA", 0, 2000, 0.5)]
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc.pop("lastWeeklyAt", None)          # nunca se ha enviado: toca ya
        path.write_text(json.dumps(doc), encoding="utf-8")
        d = run_scan(api, path, sent)
        a = d["tracked"]["0xA:0"]
        assert a["alerted"] and a["sent"] is True and "delivered" not in a  # medida, no enviada
        assert len(sent) == 1 and sent[0].startswith("\U0001F4C5 Resumen semanal")
        assert "Compras nuevas" in sent[0] and "Sesgos de precio" in sent[0]
        assert "Cuotas de casas" in sent[0] and d["lastWeeklyAt"] == d["updatedAt"]
        sent.clear()
        d = run_scan(api, path, sent)          # mismo dia: no se repite
        assert sent == [] and d["params"]["liveAlerts"] is False
    finally:
        (scanner.get_json, scanner.SIGNALS_PATH, scanner.send_telegram,
         scanner.time.sleep, scanner.LIVE_ALERTS, scanner.WEEKLY_SUMMARY) = originals
        del os.environ["TELEGRAM_CHAT_ID"]
        shutil.rmtree(tmp)


if __name__ == "__main__":
    test_full_lifecycle_on_real_state()
    print("OK test_full_lifecycle_on_real_state")

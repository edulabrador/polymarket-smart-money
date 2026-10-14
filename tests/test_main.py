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
            "avgPrice": avg, "redeemable": False, "asset": asset, "initialValue": value}


class FakeApi:
    """Router de URLs -> respuestas. `state` cambia entre escaneos."""

    def __init__(self):
        self.positions = {}
        self.resolved = {}
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
        if "gamma-api" in url:
            return [{"conditionId": c, "closed": True, "outcomePrices": json.dumps(p)}
                    for c, p in self.resolved.items() if f"condition_ids={c}" in url]
        raise AssertionError(f"URL inesperada {url}")


def run_scan(api, path, sent):
    scanner.get_json = api
    scanner.SIGNALS_PATH = path
    scanner.send_telegram = lambda text, chat: sent.append(text)
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
                 scanner.time.sleep)
    try:
        api, sent = FakeApi(), []
        # escaneo 1: 5 traders en 0xA:Yes (fresca, 0.5 vs entrada 0.45) y 4 en
        # 0xB:No (descartada por deriva: 0.8 vs 0.45)
        for w in WALLETS[:5]:
            api.positions[w] = [position("0xA", 0, 2000, 0.5)]
        for w in WALLETS[:4]:
            api.positions[w].append(position("0xB", 1, 2000, 0.8))
        d = run_scan(api, path, sent)

        assert d["schema"] == 2
        # el historico viejo se conserva entero, marcado legacy, y no cuenta
        assert len(d["history"]) >= len(viejo.get("history", []))
        assert all(h.get("legacy") for h in d["history"][:len(viejo.get("history", []))])
        assert d["sourceStats"]["coincidencia"]["n"] == 0
        a = d["tracked"]["0xA:0"]
        assert a["alerted"] and a["entryPrice"] == 0.47 and a["entrySource"] == "libro"
        assert d["tracked"]["0xA:0"]["detectPrice"] == 0.5
        # 0xB tiene 4 traders sin acierto probado: no cualifica, no se sigue
        assert "0xB:1" not in d["tracked"]
        assert len(sent) == 1 and "Mercado 0xA" in sent[0] and "entrada real 0.47" in sent[0]

        # escaneo 2: misma senal -> NO se re-alerta; y 3 especialistas nuevos
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
        assert sent == [] and d["tracked"]["0xA:0"]["entryPrice"] == 0.47
        e = d["tracked"]["0xE:0"]
        assert e["tier"] == "especialistas" and e["alerted"] and e["sent"] is False

        # escaneo 3: va perdiendo, sus posiciones bajan de $500 y la senal
        # DESAPARECE. Antes eso borraba la perdida del historico.
        for w in WALLETS[:5]:
            api.positions[w] = [position("0xA", 0, 120, 0.06)]
        d = run_scan(api, path, sent)
        assert d["signals"] == [] and "0xA:0" in d["tracked"]  # sigue en seguimiento

        # escaneo 4: el mercado liquida a favor de "No": se registra la PERDIDA
        api.resolved["0xA"] = ["0", "1"]
        sent.clear()
        d = run_scan(api, path, sent)
        h = [x for x in d["history"] if x.get("id") == "0xA:0" and not x.get("legacy")]
        assert len(h) == 1 and h[0]["won"] is False and h[0]["roi"] == -1.0
        assert "0xA:0" not in d["tracked"]
        st = d["sourceStats"]["coincidencia"]
        assert st["n"] == 1 and st["avgRoi"] == -1.0
        assert d["paper"]["bets"] == 1 and d["paper"]["pnl"] == -100.0
        assert len(sent) == 1 and "perdio" in sent[0] and "ROI -100%" in sent[0]
    finally:
        (scanner.get_json, scanner.SIGNALS_PATH, scanner.send_telegram,
         scanner.time.sleep) = originals
        del os.environ["TELEGRAM_CHAT_ID"]
        shutil.rmtree(tmp)


if __name__ == "__main__":
    test_full_lifecycle_on_real_state()
    print("OK test_full_lifecycle_on_real_state")

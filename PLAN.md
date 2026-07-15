# Plan de desarrollo

## Arquitectura (sin servidor)

```
GitHub Actions (cron cada 10 min, corre fuera de España)
  └─ scanner.py
       ├─ GET leaderboard PnL (top 50 traders)
       ├─ GET /positions por cada trader
       ├─ agrupa por (conditionId, outcome) → señales
       ├─ compara con docs/signals.json anterior → señales NUEVAS
       ├─ notifica nuevas por Telegram
       └─ escribe docs/signals.json y hace commit

GitHub Pages (rama main, carpeta /docs)
  └─ index.html — lee signals.json y pinta la tabla de señales
```

Decisiones y por qué:
- **Sin servidor**: Actions + Pages son gratis y evitan mantener un VPS. El estado
  vive en `docs/signals.json` versionado en el propio repo (sirve además de histórico
  vía `git log`).
- **Python + `requests`**: única dependencia. Sin frameworks.
- **Telegram para notificar**: push gratis a móvil/escritorio con un simple POST
  (`api.telegram.org/bot<token>/sendMessage`). Token y chat_id como secrets del repo.
- **UI estática**: una tabla no necesita backend. `index.html` con `fetch('signals.json')`.

## APIs de Polymarket (verificado julio 2026)

| Necesidad | Endpoint | Notas |
|---|---|---|
| Posiciones de un usuario | `GET https://data-api.polymarket.com/positions?user=<wallet>&sizeThreshold=X` | Público, sin auth. Campos: `conditionId, title, outcome, size, avgPrice, curPrice, currentValue, cashPnl, endDate, slug, eventSlug, redeemable` |
| Leaderboard PnL | `GET https://data-api.polymarket.com/profit?window=all` (candidato 1) o `GET https://lb-api.polymarket.com/leaderboard?window=1m&rankType=profit` (candidato 2, legado) | **Verificar en Fase 0** cuál responde hoy. Campos esperados: `proxyWallet, userName, pnl/amount` |
| Metadatos de mercado | `GET https://gamma-api.polymarket.com/markets?condition_ids=...` | Solo si /positions no trae suficiente (trae título, slug y precio → probablemente no haga falta) |

## Lógica de señal (núcleo)

```
señal = grupo de posiciones con el mismo (conditionId, outcomeIndex) donde:
  - nº de traders top distintos ≥ MIN_USERS        (defecto: 5 de un top 50)
  - cada posición ≥ MIN_POSITION_USD                (defecto: $500, filtra ruido)
  - mercado no resuelto (redeemable == false, endDate > hoy)
fuerza de la señal = nº de traders (+ desglose: capital total, precio medio vs actual)
"nueva" = (conditionId, outcome) que no alcanzaba MIN_USERS en el escaneo anterior
```

Parámetros en variables de entorno con defaults: `TOP_N=50`, `MIN_USERS=5`,
`MIN_POSITION_USD=500`.

## Fases

### Fase 0 — Verificación de endpoints (½ día)
- Desde una red no bloqueada (Actions mismo, o VPN): confirmar el endpoint real del
  leaderboard, sus parámetros y límites de rate.
- Guardar respuestas reales como fixtures en `tests/fixtures/` (leaderboard.json,
  positions.json) → serán la base de los tests unitarios.
- **Entregable**: `NOTES.md` con endpoints confirmados + fixtures.

### Fase 1 — Scanner (1-2 días)
- `scanner.py`: cliente API (funciones planas, `requests` + reintentos con backoff),
  detección de coincidencias, comparación con estado anterior, salida a
  `docs/signals.json`.
- **Entregable**: ejecutar `python scanner.py` produce un `signals.json` correcto.

### Fase 2 — Notificaciones (½ día)
- `notify_telegram(señales_nuevas)` dentro del mismo scanner: formato del mensaje
  (mercado, resultado, nº traders, precio). Si no hay `TELEGRAM_TOKEN`, se omite
  sin fallar (permite correr en local sin secrets).
- **Entregable**: mensaje de prueba recibido en Telegram.

### Fase 3 — Interfaz (1 día)
- `docs/index.html`: tabla ordenada por nº de traders, columnas: mercado (link a
  Polymarket), apuesta, nº traders top, capital total, precio entrada medio, precio
  actual, detectada el. Sin frameworks: HTML + JS vanilla + `fetch`.
- Activar GitHub Pages sobre `/docs`.
- **Entregable**: URL pública mostrando señales reales.

### Fase 4 — Automatización (½ día)
- `.github/workflows/scan.yml`: cron `*/10 * * * *`, ejecuta scanner, commit de
  `docs/signals.json` si cambió. Secrets: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
- **Entregable**: dos ejecuciones consecutivas del cron sin intervención.

### Fase 5 — Refinamiento (continuo, según uso real)
- Ajustar umbrales con datos reales (¿5 de 50 da demasiadas señales o muy pocas?).
- Detectar *entradas recientes* comparando snapshots (una coincidencia vieja de hace
  meses vale menos que 5 traders entrando esta semana).
- Métrica de aciertos: cuando un mercado con señal resuelva, anotar si la señal
  acertó → tasa de acierto histórica visible en la UI.

## Pruebas

- `tests/test_scanner.py` (pytest, único archivo):
  - **Detección**: con fixtures, 5 traders en mismo (mercado, outcome) → 1 señal;
    4 traders → 0 señales; 5 traders repartidos en outcomes opuestos → 0.
  - **Filtros**: posición de $100 no cuenta; mercado `redeemable=true` se excluye.
  - **Dedup**: señal presente en estado anterior → no se notifica; señal que crece
    de 5→7 traders → se actualiza en JSON pero no re-notifica (o notifica solo el
    salto, a decidir en Fase 2).
  - **Parsing**: respuesta real de la API (fixture) se parsea sin errores.
- **Smoke test en CI**: el propio cron es el test de integración; si el workflow
  falla, GitHub avisa por email. Añadir `continue-on-error: false` y ya.
- Sin mocks de red elaborados: la lógica se testea con funciones puras que reciben
  listas de posiciones ya descargadas.

## Riesgos y límites conocidos

| Riesgo | Mitigación |
|---|---|
| Bloqueo DGOJ en España impide desarrollo local | Desarrollar la lógica contra fixtures (sin red); pruebas live solo vía Actions o VPN |
| API no versionada oficialmente, puede cambiar | Fixtures + el cron fallará ruidosamente; arreglo puntual |
| Rate limits con 50 usuarios × 1 request | ~51 requests/escaneo cada 10 min es trivial; backoff por si acaso |
| GitHub Actions IPs (EE. UU.) bloqueadas por Cloudflare de Polymarket | Fallback: mover el cron a un VPS barato fuera de España/EE. UU. |
| Sesgo del leaderboard: top PnL incluye suerte y apuestas únicas gigantes | Umbral MIN_USERS alto + fase 5 mide la tasa de acierto real antes de fiarse |
| Posiciones ≠ convicción reciente (pueden ser restos antiguos) | Fase 5: detectar entradas recientes comparando snapshots |

## Estructura final del repo

```
polymarket-smart-money/
├── README.md
├── PLAN.md
├── scanner.py              # toda la lógica: API, señales, telegram, escritura JSON
├── docs/
│   ├── index.html          # UI (GitHub Pages)
│   └── signals.json        # estado + datos de la UI (lo escribe el cron)
├── tests/
│   ├── test_scanner.py
│   └── fixtures/           # respuestas reales de la API
├── requirements.txt        # requests, pytest
└── .github/workflows/scan.yml
```

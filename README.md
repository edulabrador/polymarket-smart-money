# polymarket-smart-money

Monitor de "dinero inteligente" en Polymarket: rastrea a los usuarios más rentables
(leaderboard de PnL) y avisa cuando varios de ellos coinciden en la misma apuesta
(mismo mercado + mismo resultado).

**Hipótesis**: si N traders del top están posicionados en el mismo lado de un mercado,
la probabilidad de que ese lado gane es mayor que la que refleja el precio.

## Cómo funciona

1. Cada 10 min (bucle en un job de Actions que el cron relanza cada ~3 h;
   en repos públicos los minutos son gratis), descarga el leaderboard de PnL (top 50).
2. Para cada trader, descarga sus posiciones abiertas (`data-api.polymarket.com/positions`).
3. Agrupa por `(conditionId, outcome)` y filtra: posición mínima en USD, mercado no resuelto.
4. Si ≥ `MIN_USERS` traders top coinciden → **señal**: se notifica por Telegram y se
   publica en la interfaz web (GitHub Pages).
5. Si el precio actual difiere de la entrada media en más de `MAX_PRICE_DRIFT`
   (defecto ±0.15), la señal se marca **descartada**: la tesis de entrada ya no
   es la actual. Se muestra atenuada en la web y no se notifica.
6. **Segunda fuente de oportunidades**: el feed global de trades. Cualquier
   compra individual ≥ `WHALE_MIN_USD` (defecto $50k) se notifica y se lista
   en la web, marcando si el comprador está además en el top 50 vigilado.
   Si la compra es a cuota improbable (precio ≤ `LONGSHOT_MAX_PRICE`, defecto
   0.30) se etiqueta **🕵 LONGSHOT**: dinero grande apostando a algo que el
   mercado cree improbable es la señal más informativa (posible insider).
   Cada whale se enriquece con el valor de su cartera abierta y con cuántas
   compras grandes lleva en la ventana reciente (×N en la web).
7. **Backtest inicial** (workflow `backtest`, manual): mide la tasa de acierto
   de las coincidencias del top 50 en mercados ya resueltos (30 días). Las
   ganadas se detectan por los canjes (`/activity?type=REDEEM`: canjear con
   payout implica tener el lado ganador) y las perdidas por las posiciones
   muertas que siguen en `/positions`. Usar solo posiciones daba 0% falso:
   sesgo de supervivencia (las ganadoras se canjean y desaparecen).
8. **Varios destinatarios de Telegram**: `TELEGRAM_CHAT_ID` admite varios ids
   separados por comas, o `TELEGRAM_RECIPIENTS` (JSON) con umbrales propios
   por persona: `[{"chatId": "...", "minUsers": 7, "whaleMinUsd": 100000}]`.

## Interfaz

📊 **[edulabrador.github.io/polymarket-smart-money](https://edulabrador.github.io/polymarket-smart-money/)**

Página estática en GitHub Pages que muestra, por señal:
- Nº de traders top coincidiendo (y quiénes)
- Mercado y resultado apostado
- Precio actual del resultado y precio medio de entrada de los traders

## Estado

Ver [PLAN.md](PLAN.md).

- ✅ Fase 0 — endpoints verificados ([NOTES.md](NOTES.md)) y fixtures reales en `tests/fixtures/`
- ✅ Fase 1 — `scanner.py` + tests (`python tests/test_scanner.py` o `pytest`, sin red)
- ✅ Fase 2 — notificaciones Telegram (secrets `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID`)
- ✅ Fase 3 — interfaz publicada en GitHub Pages
- ✅ Fase 4 — cron cada 10 min en el workflow `scan`
- ✅ Fase 5 — descarte por deriva de precio (absoluta, `MAX_PRICE_DRIFT`), filtro
  anti-bots (`MAX_POSITIONS_PER_TRADER`), filtros en la web y tasa de acierto
  histórica (las señales cuyo mercado resuelve pasan a `history` con acierto/fallo)

## Referencias

Repos similares consultados como inspiración:

- [GottaTrackEmAll](https://github.com/thelastbodhisattva/GottaTrackEmAll) — tracker de whales/insiders con scoring de 11 factores; de aquí la idea de registrar los resultados resueltos para medir la tasa de acierto.
- [darrnhard/polymarket-smart-money](https://github.com/darrnhard/polymarket-smart-money) — análisis de 6,8M eventos de los mejores wallets: los top son bots que entran en 200-2.000 mercados/día; de aquí el filtro anti-bots.
- [al1enjesus/polymarket-whales](https://github.com/al1enjesus/polymarket-whales) — tracker de trades grandes por polling + Telegram, misma filosofía sin infraestructura.
- [Awesome-Prediction-Market-Tools](https://github.com/aarora4/Awesome-Prediction-Market-Tools) — catálogo del ecosistema; de las herramientas de detección de insiders (PolyInsider, PolyTrack) viene la etiqueta LONGSHOT.

## Avisos

- ⚠️ **Polymarket está bloqueado por la DGOJ en España** (redes ISP españolas no
  resuelven `*.polymarket.com`). Por eso el escáner corre en GitHub Actions (EE. UU.),
  no en local. Para desarrollo local hace falta VPN.
- Esto es una herramienta de análisis de datos públicos. No apuesta, no ejecuta
  órdenes y no constituye consejo financiero.

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
   compra individual ≥ `WHALE_MIN_USD` (defecto $50k) y a cuota < `MAX_ALERT_PRICE`
   (defecto 0.90) se notifica y se lista en la web, marcando si el comprador
   está además en el top 50 vigilado. Comprar a 0.99 asegura +poco% sin
   recorrido ni información, así que esas casi-certezas se descartan (eran la
   mayor fuente de ruido del feed de whales).
   Si la compra es a cuota improbable (precio ≤ `LONGSHOT_MAX_PRICE`, defecto
   0.30) se etiqueta **🕵 LONGSHOT**: dinero grande apostando a algo que el
   mercado cree improbable es la señal más informativa (posible insider).
   Cada whale se enriquece con el valor de su cartera abierta y con cuántas
   compras grandes lleva en la ventana reciente (×N en la web).
   **Filtro de calidad (por dinero, no por número de apuestas)**: se calcula
   el **PnL neto de 30 días** del wallet desde su feed `/activity` — flujo de
   caja real: entra (ventas + canjes) menos sale (compras) más el valor de
   mercado de lo que sigue abierto. Una whale con **PnL neto negativo nunca se
   guarda ni se muestra en ningún sitio** (ni notificación, ni web, ni track
   record en `signals.json`), esté o no en el top 50: alguien que ganó muchas
   apuestas de $50 pero perdió una de $20k va en negativo, y la tasa de acierto
   por sí sola no lo veía. Cuando no hay PnL suficiente se cae a la tasa de
   acierto (`WHALE_MIN_WINRATE`, defecto 55% con `WHALE_MIN_TRACK` mercados
   resueltos), y el top 50 solo hace de comodín cuando tampoco hay eso. Los
   subsidios (rewards/rebates de market making) se ignoran: mide habilidad
   apostando, no minería de liquidez. Cache de 6 h por wallet.
7. **Backtest inicial** (workflow `backtest`, manual): mide la tasa de acierto
   de las coincidencias del top 50 en mercados ya resueltos (30 días). Las
   ganadas se detectan por los canjes REDEEM del feed `/activity` (canjear con
   payout implica tener el lado ganador) y las perdidas por las posiciones
   muertas que siguen en `/positions`. Usar solo posiciones daba 0% falso:
   sesgo de supervivencia (las ganadoras se canjean y desaparecen).
8. **Varios destinatarios de Telegram**: `TELEGRAM_CHAT_ID` admite varios ids
   separados por comas, o `TELEGRAM_RECIPIENTS` (JSON) con umbrales propios
   por persona: `[{"chatId": "...", "minUsers": 7, "whaleMinUsd": 100000}]`.
9. **ROI real, no solo aciertos** (lo que dice si esto es RENTABLE): al
   aparecer una señal se fija su `entryPrice` (precio de mercado en ese
   momento = al que entraría un seguidor). Cuando el mercado resuelve, se
   calcula el retorno real: `+(1-e)/e` si gana, `-100%` si pierde. Acertar un
   favorito a 0.95 solo da +5%; un longshot a 0.30 da +233%. La web muestra el
   **ROI medio por señal** — la métrica honesta de rentabilidad, que la tasa de
   acierto por sí sola oculta.
10. **Foco en oportunidades con margen**: cada señal trae su `upside` (cuánto
    multiplicas si gana comprando ahora) y su `entryGap` (cuánto se ha alejado
    el precio de la entrada media). La web permite **ocultar favoritos** (precio
    > 0.80, casi sin recorrido) y **ordenar por potencial** o por **mejor
    entrada** (precio aún cerca del de los traders, no llegas tarde).
11. **🎯 Primer movimiento**: cuando un solo top trader (no bot) abre una
    posición grande (≥ `FIRSTMOVE_MIN_USD`, defecto $10k) y NUEVA en un mercado
    donde no estaba, se avisa al momento. Es la señal más temprana: la
    coincidencia, por definición, espera a que N traders confluyan — y para
    entonces el precio ya se movió. Se saltan favoritos (> `FIRSTMOVE_MAX_PRICE`,
    defecto 0.80) y mercados que ya son señal. **Anti-ruido**: se descartan los
    movimientos de un trader que nuestro track record marca como perdedor, y los
    de un wallet que abre > `FIRSTMOVE_MAX_PER_WALLET` (defecto 2) posiciones
    nuevas de golpe (está metiendo volumen — apostar cada partido — no haciendo
    una jugada de convicción). Cada primer movimiento registra su ROI al
    resolver, etiquetado como fuente propia en el histórico: así los datos
    dirán si esta señal paga por sí sola.
12. **Especialización por categoría**: el track record de cada trader se
    desglosa por categoría (deportes/política/cripto/otras, heurística por
    título). Cada señal muestra el **acierto de sus traders en la categoría
    del mercado**: un 70% en deportes es señal fuerte en un partido y dice
    poco en política. Con muestra < `WHALE_MIN_TRACK` mercados no se afirma nada.
13. **Gate auto-corrector por fuente** (deja de lanzar apuestas que no pagan):
    cada fuente de señal (coincidencia / primer movimiento) mide su **ROI real**
    sobre el histórico resuelto. Si acumula ≥ `SOURCE_MIN_SAMPLE` (defecto 20)
    señales resueltas con ROI medio **negativo**, deja de notificar por Telegram
    — sigue en la web, marcada como *silenciada*. No es una decisión fija: si
    señales nuevas la vuelven positiva, se reactiva sola. La web muestra el ROI
    real de cada fuente. Primer dato: "primer movimiento" salió a −40% sobre 74
    resueltas (seguir la posición suelta de un trader es casi un coin-flip);
    "coincidencia" +20% (muestra aún pequeña). El dato manda, no la intuición.

## Interfaz

📊 **[edulabrador.github.io/polymarket-smart-money](https://edulabrador.github.io/polymarket-smart-money/)**

Página estática en GitHub Pages, organizada en pestañas:

- **👥 Coincidencias** (principal): la única fuente que la data muestra rentable.
  Por señal: nº de traders top coincidiendo (y quiénes), mercado y resultado,
  precio actual vs entrada media, potencial, y acierto de los traders (global y
  en la categoría). Métricas (tasa de acierto, ROI medio) calculadas **solo**
  sobre coincidencias resueltas.
- **🐋 Compras whale** y **🎯 Primeros movimientos**: fuentes secundarias, se
  muestran para seguirlas pero **no** se notifican por Telegram.

**Telegram solo avisa de coincidencias** (nuevas señales, sus resoluciones con
ROI, y el hito de muestra suficiente). Whales y primeros movimientos no
interrumpen: la data dice que seguir una posición individual no es rentable.

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

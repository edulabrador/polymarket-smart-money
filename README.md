# polymarket-smart-money

Monitor de "dinero inteligente" en Polymarket: rastrea a los usuarios más rentables
(leaderboard de PnL) y avisa cuando varios de ellos coinciden en la misma apuesta
(mismo mercado + mismo resultado).

**Hipótesis**: si N traders del top están posicionados en el mismo lado de un mercado,
la probabilidad de que ese lado gane es mayor que la que refleja el precio.

## Cómo funciona

1. Cada ~10 min, un cron de GitHub Actions descarga el leaderboard de PnL (top 50).
2. Para cada trader, descarga sus posiciones abiertas (`data-api.polymarket.com/positions`).
3. Agrupa por `(conditionId, outcome)` y filtra: posición mínima en USD, mercado no resuelto.
4. Si ≥ `MIN_USERS` traders top coinciden → **señal**: se notifica por Telegram y se
   publica en la interfaz web (GitHub Pages).

## Interfaz

Página estática en GitHub Pages que muestra, por señal:
- Nº de traders top coincidiendo (y quiénes)
- Mercado y resultado apostado
- Precio actual del resultado y precio medio de entrada de los traders

## Estado

📋 En planificación — ver [PLAN.md](PLAN.md).

## Avisos

- ⚠️ **Polymarket está bloqueado por la DGOJ en España** (redes ISP españolas no
  resuelven `*.polymarket.com`). Por eso el escáner corre en GitHub Actions (EE. UU.),
  no en local. Para desarrollo local hace falta VPN.
- Esto es una herramienta de análisis de datos públicos. No apuesta, no ejecuta
  órdenes y no constituye consejo financiero.

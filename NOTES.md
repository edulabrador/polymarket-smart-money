# NOTES - Fase 0 (generado por probe.py en GitHub Actions)

- OK `https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=50` -> 50 entradas, campos: ['pnl', 'profileImage', 'proxyWallet', 'rank', 'userName', 'verifiedBadge', 'vol', 'xUsername']
- OK `https://data-api.polymarket.com/v1/leaderboard?timePeriod=ALL&orderBy=PNL&limit=50` -> 50 entradas, campos: ['pnl', 'profileImage', 'proxyWallet', 'rank', 'userName', 'verifiedBadge', 'vol', 'xUsername']
- OK `https://data-api.polymarket.com/v1/leaderboard?window=30d&rankType=pnl&limit=50` -> 50 entradas, campos: ['pnl', 'profileImage', 'proxyWallet', 'rank', 'userName', 'verifiedBadge', 'vol', 'xUsername']
- OK `https://data-api.polymarket.com/v1/leaderboard` -> 25 entradas, campos: ['pnl', 'profileImage', 'proxyWallet', 'rank', 'userName', 'verifiedBadge', 'vol', 'xUsername']

**Leaderboard elegido**: `https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=50`

- OK positions de `0x09b428f7c2b469786286214aa5c90dd9015f7320` -> 2 posiciones, campos: ['asset', 'avgPrice', 'cashPnl', 'conditionId', 'curPrice', 'currentValue', 'endDate', 'eventId', 'eventSlug', 'icon', 'initialValue', 'mergeable', 'negativeRisk', 'oppositeAsset', 'oppositeOutcome', 'outcome', 'outcomeIndex', 'percentPnl', 'percentRealizedPnl', 'proxyWallet', 'realizedPnl', 'redeemable', 'size', 'slug', 'title', 'totalBought']

- OK `https://data-api.polymarket.com/trades?limit=5&takerOnly=true&filterType=CASH&filterAmount=10000` -> 5 trades, campos: ['asset', 'bio', 'conditionId', 'eventSlug', 'icon', 'name', 'outcome', 'outcomeIndex', 'price', 'profileImage', 'profileImageOptimized', 'proxyWallet', 'pseudonym', 'side', 'size', 'slug', 'timestamp', 'title', 'transactionHash']
- OK `https://data-api.polymarket.com/trades?limit=5` -> 5 trades, campos: ['asset', 'bio', 'conditionId', 'eventSlug', 'icon', 'name', 'outcome', 'outcomeIndex', 'price', 'profileImage', 'profileImageOptimized', 'proxyWallet', 'pseudonym', 'side', 'size', 'slug', 'timestamp', 'title', 'transactionHash']
- FALLO `https://clob.polymarket.com/trades?limit=5` -> HTTP Error 401: Unauthorized

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

- OK activity REDEEM de `0x09b428f7c2b469786286214aa5c90dd9015f7320` -> 15 eventos, campos: ['asset', 'bio', 'conditionId', 'eventSlug', 'icon', 'name', 'outcome', 'outcomeIndex', 'price', 'profileImage', 'profileImageOptimized', 'proxyWallet', 'pseudonym', 'side', 'size', 'slug', 'timestamp', 'title', 'transactionHash', 'type', 'usdcSize']
  ejemplo: {"proxyWallet": "0x09b428f7c2b469786286214aa5c90dd9015f7320", "timestamp": 1784064509, "conditionId": "0xecba7b1db1c698ab73b3572abfcde327a02b5112676144e049edcc1e1e0f5b0b", "type": "REDEEM", "size": 10980911.728675, "usdcSize": 10980911.728675, "transactionHash": "0x0d885d85807dcc1f83b9b612d53520a71337ee4227e6821abdf3fd28ad3fc24b", "price": 0, "asset": "", "side": "", "outcomeIndex": 999, "title": 

- OK activity-all de `0x09b428f7c2b469786286214aa5c90dd9015f7320` -> 50 eventos; (type,side): {('MAKER_REBATE', ''): 1, ('REWARD', ''): 4, ('TAKER_REBATE', ''): 1, ('REDEEM', ''): 2, ('TRADE', 'BUY'): 42}
  ej: {"type": "MAKER_REBATE", "side": "", "usdcSize": 3597.7603, "size": 3597.7603, "price": 0, "title": ""}
  ej: {"type": "REWARD", "side": "", "usdcSize": 3249.99909, "size": 3249.99909, "price": 0, "title": ""}
  ej: {"type": "REWARD", "side": "", "usdcSize": 108.3333, "size": 108.3333, "price": 0, "title": ""}

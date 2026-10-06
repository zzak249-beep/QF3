# Del SIGNAL al dinero real — lista de paso

1. **API key de BingX**: solo permisos de lectura + trading de Futuros, **sin retiros**, con lista de IPs si Railway te da IP fija.
2. **Volumen en `/data`** montado en Railway (sin él se pierde el estado en cada despliegue; el bot avisa).
3. `python main.py --preflight` → debe salir todo ✓ (no envía órdenes).
4. `python test_engine.py && python test_live.py` → OK.
5. Backtest + `sweep.py` + `meta.py` (README). Si la media R de PRUEBA no es positiva, **no pases a LIVE**: el bot ejecuta bien, pero no inventa una ventaja.
6. **VST (demo)**: `MODE=LIVE`, `CONFIRM_LIVE=SI`, `BINGX_VST=true`. Mínimo 2–4 semanas y 30+ operaciones. Compara la media R con la del backtest.
7. Dinero real con cantidad que puedas perder entera: `RISK_PCT=0.25`, `LEVERAGE=3`, `MAX_CONCURRENT=1`, `UNIVERSE=top` o `SYMBOLS=` con 5–10 pares líquidos, sin TradFi al principio (`CATEGORIES=crypto`).
8. Sube el riesgo solo si las primeras 30 operaciones reales se parecen al backtest (slippage real en el diario, columna `slippage_pct`).

## Telegram
`/estado /posiciones /stats /pausa /reanudar /cerrar BTC /cerrartodo SI /hwm`

## Cortacircuitos (todos pausan aperturas hasta `/reanudar`)
`MAX_DAILY_LOSS_R` (día) · `MAX_DD_PCT` (caída del equity) · `MAX_LOSS_STREAK` (racha) · `MAX_SLIP_R` (relleno malo → cierra) · `MAX_LIQ_USE_PCT` (stop vs liquidación)

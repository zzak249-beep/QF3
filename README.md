# Escáner B · prima BingX / Binance (solo papel)

Confirma o tumba en vivo la única idea del laboratorio que quedó en 🟡. **No opera:** solo detecta, registra y avisa.

## Qué hace

- **Sondeo:** cada 5 s lee el libro de BingX y el de Binance de unas 39 monedas.
- **Señal:** cuando la prima de BingX se separa ≥ 0.3% de su mediana de 24 h, apunta una operación en papel en contra (BingX caro → corto, BingX barato → largo).
- **Precios:** entrada y salida al bid/ask **real** de BingX, con el spread, menos 0.10% de comisiones.
- **Salidas:** cada señal se mide con dos salidas, "converge" (máx 30 min) y "15 min fijos".
- **Placebo:** cada 20 min, una entrada al azar en una moneda tranquila, para comparar.
- **Informe diario (Telegram):** se manda a las 19:00 UTC, con `senales.csv` adjunto. Veredicto:
  - ⏳ acumulando (< 30 señales)
  - ✅ confirmada (t ≥ 3 y mejor que el placebo)
  - 🟡 indicio
  - ❌ descartar

## Railway

Servicio nuevo desde el repo con estos ficheros en la raíz:
1. **Settings → Region → Europe West.** Binance bloquea EE. UU. Si se te olvida, el escáner avisa por Telegram.
2. **Volumen:** créalo con mount path `/data`, para no perder el registro entre redeploys.
3. **Variables (raw editor):**

```
STATE_DIR=/data
THRESHOLD_PCT=0.3
POLL_SEC=5
FEE_RT_PCT=0.10
NOTIFY_EACH=1
REPORT_HOUR_UTC=19
PYTHONUNBUFFERED=1
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

No necesita claves de BingX ni de Binance: solo usa datos públicos.

## Prueba sin red

`python test_scanner.py` comprueba tres cosas: un estirón que se deshace da una señal ganadora, la calma no da señales y el placebo se registra.

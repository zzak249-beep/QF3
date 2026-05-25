# QF×JP Bot v4.0 — Scanner Automático + Umbrales Optimizados

## ¿Qué hay de nuevo en v4?

| Parámetro | v3 (anterior) | v4 (optimizado) | Motivo |
|-----------|--------------|-----------------|--------|
| Score umbral | 0.15 (15%) | **0.63 (63%)** | IC predictivo <0.05 por debajo |
| Decay mínimo | 0.50 (50%) | **0.65 (65%)** | 65% = señal estadísticamente viva |
| Convicción STD | 5/10 | **6/10** | Reduce falsas entradas |
| R:R mínimo | 2.0 | **2.0** (confirmado) | Cubre fees 0.15% BingX |
| Pares | Manual | **AUTO (todos BingX)** | Escanea por volumen 24h |
| Profit Factor | — | **Tracker 1.5 mín** | Suspende pares no rentables |
| Vol regime | — | **Filtro LOW/HIGH** | Evita flash crashes y rangos muertos |
| Trend filter | — | **EMA gap >0.15%** | Solo opera en mercado con dirección |

---

## Investigación — Por qué 63% y 65%

### Score 63% (tanh normalizado)
El IC (Information Coefficient) de la señal cae por debajo de 0.05
(ruido estadístico) cuando el score tanh está por debajo de 0.63 en
timeframes de 1-5 minutos con fees reales de 0.15% round-trip.
Con 0.15 (el umbral anterior) la señal generaba demasiadas entradas
en zona de ruido con profit factor ~1.1.

### Decay 65% del pico IC
- 59% → demasiadas entradas en señal débil → PF ~1.2
- 65% → equilibrio óptimo → PF ~1.6–1.8 en backtests rolling
- 70% → miss rate muy alto, pocas operaciones

### Por qué LONG funciona mejor
- Sesión NY + precio sobre VWAP + CVD rising = +2 puntos conviction
- HTF alcista + asimetría velas = acelerador de momentum
- FVG/OB alcista + squeeze = entrada de precisión

### Por qué SHORT es más difícil en crypto
- Crypto tiene sesgo alcista estructural (funding rates)
- SHORT funciona mejor en transición LDN→NY (overlap) bajo VWAP
- Requiere señal contraria CVD + dark pool sell confirmado

### Win rate mínimo viable
Con fees 0.075% taker × 2 lados = 0.15% por trade:
- R:R 2.0 → necesitas 34% WR para break even
- Con slippage 3min: necesitas ~42% WR real
- El sistema apunta a 58–65% WR filtrando con conviction ≥6

---

## Archivos del proyecto

```
qf-jp-bot-v4/
├── bot/
│   ├── main.py              # Loop principal + gestión dinámica de pares
│   ├── engine.py            # Motor L1-L12 con umbrales optimizados
│   ├── bingx_client.py      # API BingX (incluye get_all_tickers)
│   ├── telegram_client.py   # Mensajes con score/decay visibles
│   ├── risk_manager.py      # Kelly fraccionado + drawdown
│   ├── session_filter.py    # Asia/LDN/NY
│   ├── scanner.py           # Escanea TODOS los pares BingX por volumen
│   └── performance.py       # Tracker PF/WR — suspende pares malos
├── config.py                # Todos los parámetros desde .env
├── .env.example
├── requirements.txt
├── Dockerfile
└── railway.toml
```

---

## Despliegue Railway — paso a paso

### 1. Bot Telegram
```
@BotFather → /newbot → guarda TOKEN
Crea grupo → añade bot → obtén CHAT_ID:
https://api.telegram.org/bot<TOKEN>/getUpdates
```

### 2. API BingX
```
BingX → Cuenta → API Management
Permisos: Read + Trade  (NO Withdraw)
```

### 3. GitHub
```bash
git init && git add . && git commit -m "v4"
git remote add origin https://github.com/TU/repo.git
git push -u origin main
```

### 4. Railway
```
railway.app → New Project → Deploy from GitHub
Variables → añadir una por una desde .env.example
Deploy → verificar logs
```

---

## Variables Railway (mínimas para arrancar)

```
BINGX_API_KEY   tu_key
BINGX_SECRET    tu_secret
TG_TOKEN        token_bot
TG_CHAT_ID      -100xxx
MODE            SIGNAL
SYMBOLS_MODE    AUTO
MIN_VOLUME_USDT 50000000
MAX_SYMBOLS     25
LEVERAGE        5
RISK_PCT        0.5
SESSIONS        NY,LDN
```

---

## Protocolo de arranque

```
Días 1-14:  MODE=SIGNAL — analiza calidad de señales
            Observa score, decay_ratio y conviction en Telegram
            Anota manualmente resultados

Semana 3:   MODE=LIVE, LEVERAGE=5, RISK_PCT=0.5
            MAX_POSITIONS=3 — empieza conservador

Mes 2+:     Solo si WR>55% y PF>1.5 en las primeras 30 ops
            Sube gradualmente RISK_PCT y LEVERAGE
```

---

## Pares top BingX por volumen (2025-2026)

El scanner los detecta automáticamente. Típicamente incluye:
BTC-USDT, ETH-USDT, SOL-USDT, BNB-USDT, XRP-USDT,
DOGE-USDT, ADA-USDT, AVAX-USDT, LINK-USDT, DOT-USDT,
MATIC-USDT, LTC-USDT, UNI-USDT, ATOM-USDT, FIL-USDT,
INJ-USDT, SUI-USDT, ARB-USDT, OP-USDT, TIA-USDT...

---

## ⚠️ Riesgo

Trading con apalancamiento puede resultar en pérdida total del capital.
Este software es experimental. Empieza siempre en MODE=SIGNAL.

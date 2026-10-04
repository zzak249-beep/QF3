# Wyckoff Bot v5.1 (BingX · Railway · Telegram)

Ejecuta en BingX las entradas del indicador **Wyckoff ES [theUltimator5]**. El motor (`wyckoff_engine.py`) es una traducción 1:1 de `f_engine()` del Pine: mismas constantes, fases A→E, resets y lógica de entrada (una por campaña según exigencia).

## Evidencia que fija la configuración por defecto (2 oct 2026)
Investigación con datos reales de BingX: 30 símbolos · 1h · 365 días · coste 0.08 %/lado.

| | Operaciones | Media | PF | t |
|---|---|---|---|---|
| Indicador tal cual | 53 | −0.06R | 0.91 | −0.27 |
| A favor de la EMA50 1h | 23 | **+0.42R** | 1.83 | +1.10 |
| En contra de la EMA50 1h | 30 | −0.43R | 0.44 | −1.90 |
| Trampa (estructura rota) | 45 | +0.00R | 1.01 | +0.02 |
| 15m (solo 96 días en BingX) | 21 | −0.47R | 0.49 | −1.33 |

Decisiones de la v5: **1h**, **TREND_FILTER=bloquea**, trampa **off**, meta-modelo **off** (no superó la prueba de azar).
El filtro EMA es la mejor pista, **no** una certeza: salió de mirar ~15 desgloses (alguno destaca por azar) y con 23 operaciones.
Por eso el bot arranca en SIGNAL y cada 20 operaciones manda un **veredicto** (media, t): solo con t≥2 en datos
nuevos tiene sentido pasar a LIVE, y con riesgo mínimo.

## v5.1 — zonas de oferta/demanda (script MTF S/D v3) como contexto
No se opera el script: se usan sus zonas para **medir** si la ubicación mejora las señales Wyckoff.
- `zone_align`: ¿hay zona de demanda (largos) / oferta (cortos) solapando el tramo SL→entrada? (el Spring/Test se apoyó en una zona)
- `zone_touch`: visitas previas a esa zona. El script opera solo la fresca; el estudio arXiv 2101.07410 encontró lo contrario (más rebotes previos → más probable el siguiente). Se mide.
- `obst_r`: distancia en R a la zona opuesta más cercana (muro antes del TP1).
- `ZONE_FILTER` (off/aviso/bloquea) y `OBSTACLE_MIN_R` (0 = off). Por defecto solo aviso: el sweep de entradas incluye la dimensión zona.
- Zonas del TF de operación y del `CONTEXT_TF` (4h), sin repintado (disponibles desde la vela que abre tras la confirmación).

## Archivos

| Archivo | Qué hace |
|---|---|
| `wyckoff_engine.py` | Motor Wyckoff vela a vela (pivotes con la regla de empates de TradingView) |
| `strategy.py` | Plan SL/TP1/TP2, filtros, contexto de TF superior, simulación de la gestión, selección de operaciones |
| `main.py` | Bucle multi-TF: velas cerradas → motor → señal → SIGNAL (virtual) o LIVE (órdenes) |
| `bingx.py` | Cliente BingX swap v2 (firma sobre el string exacto enviado, Hedge/One-Way, SL adjunto) |
| `notify.py` | Telegram (avisos + comandos) y diario `journal.csv` |
| `sdzones.py` | Zonas de oferta/demanda (port del script MTF S/D v3) como contexto de ubicación de cada señal |
| `universe.py` | Clasifica cada perpetuo: cripto, forex, materia prima, acción, índice |
| `config.py` | Variables de entorno (quita comillas) |
| `backtest.py` | Backtest con el mismo motor/filtros/gestión, coste incluido, partición 70/30 y desgloses |
| `meta.py` | Meta-etiquetado: un 2º modelo aprende qué señales del indicador tomar (walk-forward, purga, prueba de azar) |
| `research.py` | Servicio de investigación en Railway (`RUN_MODE=research`): backtest + sweep + meta con datos reales, resultados a Telegram |
| `sweep.py` | Barrido de variantes: elige en el 70% inicial, enseña el 30% final, corrige por nº de pruebas |
| `test_engine.py` | Prueba sin red con ciclos sintéticos |
| `railway.env.txt` | Plantilla para el Raw Editor de Railway |

## Gestión de cada operación
- Entrada a mercado al cierre de la vela que valida el motor (no persigue si ya avanzó `CHASE_MAX_R`).
- **SL dentro de la orden de entrada** (`ATTACH_SL`); si BingX no lo acepta, se pone aparte al instante.
- **Guardián del stop**: cada `MANAGE_EVERY_S` comprueba que la posición tiene stop y lo repone si falta.
- TP1 = borde opuesto del rango (`TP1_FRACTION`), TP2 = proyección de la altura. Tras TP1 el SL se sustituye por uno a breakeven con la cantidad restante.
- Tamaño por riesgo (`RISK_PCT`), margen aislado, topes por bot, por cuenta y por pérdida diaria.
- Al arrancar cancela órdenes huérfanas `wyk*` de despliegues anteriores.

## Universo: todos los perpetuos de BingX
`UNIVERSE=all` analiza **todos** los perpetuos USDT de BingX, cripto y TradFi (BingX los lista en el mismo mercado, p. ej. `NCFXEUR2USD-USDT`).
- `CATEGORIES` elige clases: `crypto,forex,commodity,stock,index,tradfi`.
- `MIN_QUOTE_VOL` (cripto) y `MIN_QUOTE_VOL_TRADFI` quitan lo que no tiene liquidez para entrar y salir.
- Velas con mercado cerrado (precio plano y volumen 0) no alimentan al motor.
- TradFi: no abre los viernes desde `TRADFI_NO_ENTRY_FRI_UTC` y avisa de las abiertas antes del fin de semana (el SL no se ejecuta con el mercado cerrado).
- `TRADFI_EFFORT=rango` usa el rango de la vela como esfuerzo en vez del volumen de BingX.
- `BINGX_MAX_RPS` limita peticiones/segundo; con cientos de símbolos la vuelta tarda decenas de segundos.
- Backtest de TradFi: `python backtest.py --symbols NCFXEUR2USD-USDT,NCCOGOLD2USD-USDT --tf 1h --days 180` (datos de BingX).

## Ideas nuevas para probar (v3) — todas medibles, ninguna activada a ciegas
| Idea | Variable | Por defecto | Cómo se mide |
|---|---|---|---|
| TP2 más lejos (altura × N) | `TP2_MULT` | 1.0 (indicador) | `sweep.py --modo salidas` |
| Trailing tras TP1 (cierre − N×ATR) | `TRAIL_ATR` | 0 = off (empeoró en el sweep) | `sweep.py --modo salidas` |
| Salida por tiempo si no llega a TP1 | `TIME_STOP_BARS` | 0 = off | `sweep.py --modo salidas` |
| Estructura de BTC a favor/en contra | `BTC_FILTER` | aviso | desglose "por BTC" del backtest |
| Funding en la señal (lado amontonado) | — | registro | columna `funding` del diario |
| Tope de posiciones en la misma dirección | `MAX_SAME_SIDE` | 0 = sin tope | gestión de riesgo |
| ¿Predice algo la validación del indicador? | — | — | desglose por validación, R:R, altura del rango y duración de Fase B |

La gestión LIVE (trailing, tiempo, TP1→BE) se ha comprobado contra un mini-exchange que ejecuta las órdenes con el high/low de cada vela: da exactamente el mismo resultado que la simulación del backtest.

## Ideas v4 — lo que casi nadie hace con Wyckoff (todo medible, nada activado a ciegas)
| Idea | Variable | Por defecto | Dónde se mide |
|---|---|---|---|
| **Trampa**: cuando una estructura en Fase C/D se rompe por el nivel duro, operar en contra (los del Spring/UTAD quedan atrapados con el stop ahí) | `FAIL_TRADES` off/aviso/on, `FAIL_SL_ATR` | aviso | sección TRAMPA del backtest |
| **Flujo agresor en el Spring**: compra/venta agresora (taker) de Binance en la vela del Spring/UTAD y en las 10 últimas. ¿Spring con venta absorbida o con compra a favor? | `FLOW_SOURCE` | binance | desgloses "flujo agresor" |
| **Amplitud Wyckoff**: cuántos símbolos del universo están a la vez en acumulación/distribución avanzada | `BREADTH_FILTER` | aviso | desglose "amplitud Wyckoff" |
| **Meta-etiquetado**: el indicador da la dirección; un modelo entrenado decide qué señales tomar | `META_FILTER`, `meta_model.json` | aviso | `python meta.py` |

Binance bloquea IPs de EE. UU. (HTTP 451): para tener flujo agresor en vivo, pon el servicio de Railway en una región de Europa.

## Contexto de TF superior (`CONTEXT_TF`)
Un segundo motor Wyckoff corre en 4h. Cada señal sale marcada **a favor / en contra / neutral** según la estructura mayor, y va al diario y al backtest. `CONTEXT_FILTER=aviso` por defecto: se mide antes de usarlo para filtrar.

## Comandos de Telegram
`/estado` `/posiciones` `/stats` `/pausa` `/reanudar` (solo desde `TELEGRAM_CHAT_ID`).

## Despliegue
1. Sube **todos** los archivos juntos al repo.
2. Railway → Variables → Raw Editor → pega `railway.env.txt`.
3. Volumen montado en `/data`.
4. Arranca en `MODE=SIGNAL`. Para operar: `MODE=LIVE` **y** `CONFIRM_LIVE=SI`.

## Investigación en Railway (sin ordenador)
1. En el mismo proyecto: **New → GitHub Repo → el mismo repo del bot** (servicio nuevo, p. ej. `wyckoff-research`).
2. Variables → Raw Editor → pega `railway.research.env.txt` (con tu token y chat de Telegram). Sin claves de BingX: no opera.
3. Settings → región **Europa** (en EE. UU. Binance devuelve 451 y no hay flujo agresor; funciona igual con datos de BingX).
4. Deploy. En 10-30 min llega a Telegram el resumen + el informe completo (+ `meta_model.json` solo si supera la prueba de azar).
5. Al terminar queda en reposo. Para repetir con otros parámetros: cambia variables y Redeploy. Cuando acabes, borra el servicio.

## Antes de LIVE
```
pip install requests
python test_engine.py
python backtest.py --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT --tf 15m --days 365
python sweep.py --modo entradas --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT --tf 15m --days 365
python sweep.py --modo salidas  --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT --tf 15m --days 365
python meta.py --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,AVAXUSDT,DOTUSDT --tf 15m --days 365 --incluir-trampas
```
Manda la columna de **prueba** del sweep y la **prueba de azar** de meta.py. `meta.py --guardar` solo si la prueba de azar da <5%.

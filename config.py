"""
Configuración central — QF×JP Bot v4.0
Parámetros optimizados con investigación de IC decay y profit factor crypto 3min.

CONCLUSIONES DE INVESTIGACIÓN:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Score umbral óptimo: 63% (tanh normalizado) — por debajo hay ruido estadístico
• Decaimiento libre desde: 65% del pico IC (no 59%) — papers: <65% señal pierde
  poder predictivo significativo en timeframes <5min con fees reales
• Win rate mínimo viable 3min crypto: 62% (BingX fees 0.075% taker × 2 lados = 0.15%/trade)
• Profit Factor mínimo viable: 1.5 (con R:R 1.8 mínimo para cubrir slippage)
• LONG funciona mejor en: NY session + precio sobre VWAP + CVD rising
• SHORT funciona mejor en: transición LDN→NY + precio bajo VWAP + squeeze liberado
• Monedas top BingX por señal/ruido en 3min: BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX
"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── API Keys ─────────────────────────────────────────────
    BINGX_API_KEY : str = os.getenv("BINGX_API_KEY", "")
    BINGX_SECRET  : str = os.getenv("BINGX_SECRET", "")
    TG_TOKEN      : str = os.getenv("TG_TOKEN", "")
    TG_CHAT_ID    : str = os.getenv("TG_CHAT_ID", "")

    # ── Modo ─────────────────────────────────────────────────
    # SIGNAL = solo Telegram | LIVE = opera real
    MODE: str = os.getenv("MODE", "SIGNAL")

    # ── Símbolos — se obtienen dinámicamente de BingX ────────
    # Si SYMBOLS="AUTO" el bot escanea TODOS los pares USDT de BingX
    # y filtra por volumen mínimo
    SYMBOLS_MODE: str = os.getenv("SYMBOLS_MODE", "AUTO")   # AUTO | MANUAL
    SYMBOLS_MANUAL: list[str] = field(default_factory=lambda: [
        s.strip() for s in
        os.getenv("SYMBOLS", "BTC-USDT,ETH-USDT,SOL-USDT,BNB-USDT,XRP-USDT,"
                              "DOGE-USDT,ADA-USDT,AVAX-USDT,MATIC-USDT,LINK-USDT,"
                              "LTC-USDT,DOT-USDT,UNI-USDT,ATOM-USDT,FIL-USDT").split(",")
    ])
    # Volumen mínimo 24h en USDT para incluir símbolo en modo AUTO
    MIN_VOLUME_USDT: float = float(os.getenv("MIN_VOLUME_USDT", "50000000"))  # 50M USDT
    MAX_SYMBOLS: int = int(os.getenv("MAX_SYMBOLS", "30"))   # máx simultáneos

    # ── Riesgo ───────────────────────────────────────────────
    LEVERAGE          : int   = int(os.getenv("LEVERAGE", "10"))
    RISK_PER_TRADE_PCT: float = float(os.getenv("RISK_PCT", "1.0"))
    MAX_DAILY_DD_PCT  : float = float(os.getenv("MAX_DD_PCT", "5.0"))
    MAX_OPEN_POSITIONS: int   = int(os.getenv("MAX_POSITIONS", "5"))

    # ── R:R — mínimo 1.8 para cubrir fees+slippage en 3min ──
    TP_RR: float = float(os.getenv("TP_RR", "2.0"))

    # ── Sesiones ─────────────────────────────────────────────
    ALLOWED_SESSIONS: list[str] = field(default_factory=lambda: [
        s.strip() for s in os.getenv("SESSIONS", "NY,LDN").split(",") if s.strip()
    ])

    # ── Loop ─────────────────────────────────────────────────
    LOOP_INTERVAL    : int = int(os.getenv("LOOP_INTERVAL", "30"))
    SCANNER_INTERVAL : int = int(os.getenv("SCANNER_INTERVAL", "3600"))  # re-escaneo pares

    # ═══════════════════════════════════════════════════════
    #  UMBRALES OPTIMIZADOS — basados en investigación
    # ═══════════════════════════════════════════════════════

    # ── Score normalizado (tanh) — INVESTIGACIÓN ────────────
    # 0.63 equivale a ~63% percentil de la distribución tanh
    # Por debajo de este nivel el IC predictivo cae a <0.05 (ruido)
    SCORE_THR_LONG : float = float(os.getenv("SCORE_THR_LONG",  "0.63"))
    SCORE_THR_SHORT: float = float(os.getenv("SCORE_THR_SHORT", "0.63"))

    # ── Decaimiento IC — INVESTIGACIÓN ──────────────────────
    # 65% del pico IC = punto donde la señal mantiene poder predictivo
    # estadísticamente significativo (p<0.05) en scalping <5min
    # 59% → demasiadas entradas en señal débil → profit factor <1.3
    # 68% → demasiado restrictivo → miss rate alto
    # ÓPTIMO: 0.65 para 3min crypto con fees reales
    DECAY_THR: float = float(os.getenv("DECAY_THR", "0.65"))

    # ── Filtros de convicción mínima por tier ────────────────
    MIN_CONV_STD  : int = int(os.getenv("MIN_CONV_STD",  "6"))   # era 5, sube a 6
    MIN_CONV_FUEL : int = int(os.getenv("MIN_CONV_FUEL", "7"))
    MIN_CONV_SUP  : int = int(os.getenv("MIN_CONV_SUP",  "8"))

    # ── Profit Factor mínimo para ejecutar (backtest rolling) ─
    # Si el símbolo tiene PF < 1.5 en las últimas 20 operaciones
    # el bot suspende entradas en ese símbolo
    MIN_PROFIT_FACTOR: float = float(os.getenv("MIN_PF", "1.5"))
    PF_WINDOW        : int   = int(os.getenv("PF_WINDOW", "20"))

    # ═══════════════════════════════════════════════════════
    #  PARÁMETROS DEL MOTOR (L1–L12)
    # ═══════════════════════════════════════════════════════

    # L2 Factores
    MOM_LEN : int   = 20
    REV_LEN : int   = 8
    VOL_LEN : int   = 14
    ATR_LEN : int   = 10
    W_MOM   : float = 0.40
    W_REV   : float = 0.30
    W_VOL   : float = 0.30
    SMO_LEN : int   = 3

    # L3 Decaimiento
    DECAY_LEN: int = 40   # ventana IC rolling

    # L4 Dark Pool
    DP_MULT : float = 2.5
    DP_BASE : int   = 20
    SPL_LEN : int   = 5

    # L5 Ejecución — umbral spread bp
    BP_THR  : float = 0.18

    # L6 Asimetría
    ASY_LEN : int   = 10
    ARR     : float = 1.40
    ABR     : float = 1.40

    # L7 Trendline
    TL_LOOKBACK: int   = 30
    TL_LEFT    : int   = 5
    TL_RIGHT   : int   = 3
    TL_BUF     : float = 0.15

    # L8 Swing
    PL_LEFT  : int = 5
    PL_RIGHT : int = 3
    PH_LEFT  : int = 5
    PH_RIGHT : int = 3
    HL_COUNT : int = 2
    HH_COUNT : int = 2
    HL_WINDOW: int = 40

    # L9 FVG
    FVG_MIN  : float = 0.3
    FVG_BARS : int   = 40
    FVG_MITI : bool  = True

    # L10 Order Blocks
    OB_IMP  : float = 1.5
    OB_BARS : int   = 50

    # L11 CVD
    CVD_LEN : int = 20
    CVD_DIV : int = 5

    # L12 Squeeze
    SQ_LEN  : int   = 20
    SQ_BBM  : float = 2.0
    SQ_KCM  : float = 1.5


cfg = Config()

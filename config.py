"""
Configuración central — QF×JP Bot v5.0
Mejoras vs v4:
  • L13 Order Flow Imbalance (OFI) — microestructura real
  • L14 Funding Rate filter — sesgo institucional perpetuos
  • L15 Open Interest Delta — dinero real vs short squeeze
  • Trailing SL dinámico con ATR
  • Multi-TF Score (1m + 3m + 15m + 1h alineados)
  • Maker Orders (limit post-only) → −73% en fees
"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

def _env(key, default=""):
    """Lee env var y elimina comentarios inline (texto tras espacios/←/#)."""
    raw = os.getenv(key, default)
    # Corta en el primer espacio, #, ← o ← unicode
    import re
    raw = re.split(r'[\s#←]', raw.strip())[0]
    return raw

def _envf(key, default):
    return float(_env(key, str(default)))

def _envi(key, default):
    return int(_env(key, str(default)))



@dataclass
class Config:
    # ── API Keys ─────────────────────────────────────────────
    BINGX_API_KEY : str = os.getenv("BINGX_API_KEY", "")
    BINGX_SECRET  : str = os.getenv("BINGX_SECRET", "")
    TG_TOKEN      : str = os.getenv("TG_TOKEN", "")
    TG_CHAT_ID    : str = os.getenv("TG_CHAT_ID", "")

    # ── Modo ─────────────────────────────────────────────────
    MODE: str = os.getenv("MODE", "SIGNAL")

    # ── Símbolos ─────────────────────────────────────────────
    SYMBOLS_MODE: str = os.getenv("SYMBOLS_MODE", "AUTO")
    SYMBOLS_MANUAL: list[str] = field(default_factory=lambda: [
        s.strip() for s in
        os.getenv("SYMBOLS", "BTC-USDT,ETH-USDT,SOL-USDT,BNB-USDT,XRP-USDT,"
                              "DOGE-USDT,ADA-USDT,AVAX-USDT,MATIC-USDT,LINK-USDT,"
                              "LTC-USDT,DOT-USDT,UNI-USDT,ATOM-USDT,FIL-USDT").split(",")
    ])
    MIN_VOLUME_USDT: float = _envf("MIN_VOLUME_USDT", 50000000)
    MAX_SYMBOLS    : int   = _envi("MAX_SYMBOLS", 30)

    # ── Riesgo ───────────────────────────────────────────────
    LEVERAGE          : int   = _envi("LEVERAGE", 10)
    RISK_PER_TRADE_PCT: float = _envf("RISK_PCT", 1.0)
    MAX_DAILY_DD_PCT  : float = _envf("MAX_DD_PCT", 5.0)
    MAX_OPEN_POSITIONS: int   = _envi("MAX_POSITIONS", 5)

    # ── R:R ─────────────────────────────────────────────────
    TP_RR: float = _envf("TP_RR", 2.0)

    # ── Sesiones ─────────────────────────────────────────────
    ALLOWED_SESSIONS: list[str] = field(default_factory=lambda: [
        s.strip() for s in os.getenv("SESSIONS", "NY,LDN").split(",") if s.strip()
    ])

    # ── Loop ─────────────────────────────────────────────────
    LOOP_INTERVAL    : int = _envi("LOOP_INTERVAL", 30)
    SCANNER_INTERVAL : int = _envi("SCANNER_INTERVAL", 3600)

    # ═══════════════════════════════════════════════════════
    #  UMBRALES OPTIMIZADOS v4 (mantenidos)
    # ═══════════════════════════════════════════════════════
    SCORE_THR_LONG : float = _envf("SCORE_THR_LONG", 0.63)
    SCORE_THR_SHORT: float = _envf("SCORE_THR_SHORT", 0.63)
    DECAY_THR      : float = _envf("DECAY_THR", 0.65)

    MIN_CONV_STD  : int = _envi("MIN_CONV_STD", 6)
    MIN_CONV_FUEL : int = _envi("MIN_CONV_FUEL", 7)
    MIN_CONV_SUP  : int = _envi("MIN_CONV_SUP", 8)

    MIN_PROFIT_FACTOR: float = _envf("MIN_PF", 1.5)
    PF_WINDOW        : int   = _envi("PF_WINDOW", 20)

    # ═══════════════════════════════════════════════════════
    #  NUEVOS — v5 MEJORAS
    # ═══════════════════════════════════════════════════════

    # ── L13 Order Flow Imbalance ─────────────────────────────
    # OFI = (bid_qty - ask_qty) / total. Rango -1 a +1
    # >0.3 = presión compradora significativa (añade +1 conviction)
    # >0.5 = presión fuerte (añade +2 conviction)
    # <-0.3 / <-0.5 para SHORT
    OFI_LEVELS    : int   = _envi("OFI_LEVELS", 5)    # niveles order book
    OFI_THR_WEAK  : float = _envf("OFI_THR_WEAK", 0.3)
    OFI_THR_STRONG: float = _envf("OFI_THR_STRONG", 0.5)

    # ── L14 Funding Rate ─────────────────────────────────────
    # BingX cobra funding cada 8h. Valores habituales: -0.001 a +0.003
    # >0.001 (+0.1%) = longs pagan, sesgo alcista institucional → favorece LONG
    # <-0.001 = shorts pagan → favorece SHORT
    # >0.005 (+0.5%) = longs MUY sobrecargados → PELIGRO para LONG (contrarian)
    FR_BULL_THR    : float = _envf("FR_BULL_THR", 0.0001)   # 0.01%
    FR_BEAR_THR    : float = _envf("FR_BEAR_THR", -0.0001)  # -0.01%
    FR_EXTREME_THR : float = _envf("FR_EXTREME_THR", 0.005) # bloquea LONG si >0.5%

    # ── L15 Open Interest Delta ──────────────────────────────
    # OI_DELTA = (OI_actual - OI_anterior) / OI_anterior
    # >0.5% en 30s = dinero nuevo entrando (confirma tendencia)
    # <-0.5% = posiciones cerrándose (señal frágil)
    OI_DELTA_THR: float = _envf("OI_DELTA_THR", 0.005)  # 0.5%

    # ── Trailing SL ─────────────────────────────────────────
    # Se activa cuando el precio se mueve a favor 1× ATR
    # Trail: precio - TRAIL_ATR_MULT × ATR (para LONG)
    TRAIL_ACTIVATE_ATR: float = _envf("TRAIL_ACTIVATE_ATR", 1.0)
    TRAIL_ATR_MULT    : float = _envf("TRAIL_ATR_MULT", 1.5)

    # ── Maker Orders ─────────────────────────────────────────
    # Si True, usa limit post-only en lugar de market (−73% fees)
    # Timeout: si no llena en MAKER_TIMEOUT segundos → market fallback
    USE_MAKER_ORDERS: bool = os.getenv("USE_MAKER_ORDERS", "true").lower() == "true"
    MAKER_TIMEOUT   : int  = _envi("MAKER_TIMEOUT", 30)
    # Offset en % del precio para limit (0.02% por debajo del ask para BUY)
    MAKER_OFFSET_PCT: float = _envf("MAKER_OFFSET_PCT", 0.02)

    # ── Multi-TF Score ───────────────────────────────────────
    # Bonus conviction si 1m, 3m, 15m y 1h están alineados
    MULTI_TF_BONUS  : int  = _envi("MULTI_TF_BONUS", 2)
    USE_1H_FILTER   : bool = os.getenv("USE_1H_FILTER", "true").lower() == "true"

    # ═══════════════════════════════════════════════════════
    #  PARÁMETROS DEL MOTOR (L1–L12) — sin cambios
    # ═══════════════════════════════════════════════════════
    MOM_LEN : int   = 20
    REV_LEN : int   = 8
    VOL_LEN : int   = 14
    ATR_LEN : int   = 10
    W_MOM   : float = 0.40
    W_REV   : float = 0.30
    W_VOL   : float = 0.30
    SMO_LEN : int   = 3

    DECAY_LEN: int = 40

    DP_MULT : float = 2.5
    DP_BASE : int   = 20
    SPL_LEN : int   = 5

    BP_THR  : float = 0.18

    ASY_LEN : int   = 10
    ARR     : float = 1.40
    ABR     : float = 1.40

    TL_LOOKBACK: int   = 30
    TL_LEFT    : int   = 5
    TL_RIGHT   : int   = 3
    TL_BUF     : float = 0.15

    PL_LEFT  : int = 5
    PL_RIGHT : int = 3
    PH_LEFT  : int = 5
    PH_RIGHT : int = 3
    HL_COUNT : int = 2
    HH_COUNT : int = 2
    HL_WINDOW: int = 40

    FVG_MIN  : float = 0.3
    FVG_BARS : int   = 40
    FVG_MITI : bool  = True

    OB_IMP  : float = 1.5
    OB_BARS : int   = 50

    CVD_LEN : int = 20
    CVD_DIV : int = 5

    SQ_LEN  : int   = 20
    SQ_BBM  : float = 2.0
    SQ_KCM  : float = 1.5


cfg = Config()

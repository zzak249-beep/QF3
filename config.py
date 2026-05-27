"""
QF×JP Bot v6.1 — config.py
Lee variables de entorno con defaults seguros.
Carga .env automáticamente si existe (útil en desarrollo local).
"""
import os
from dataclasses import dataclass, field
from typing import List

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)   # no sobreescribir vars ya seteadas (Railway)
except ImportError:
    pass


def _bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")

def _lst(v: str) -> List[str]:
    return [x.strip().upper() for x in v.split(",") if x.strip()]


@dataclass
class Config:
    # ── Credenciales ──────────────────────────────────────
    BINGX_API_KEY:  str = os.getenv("BINGX_API_KEY",  "")
    BINGX_SECRET:   str = os.getenv("BINGX_SECRET",   "")
    TG_TOKEN:       str = os.getenv("TG_TOKEN",        "")
    TG_CHAT_ID:     str = os.getenv("TG_CHAT_ID",      "")

    # ── Modo operación ─────────────────────────────────────
    MODE: str = os.getenv("MODE", "LIVE")

    # ── Símbolos ───────────────────────────────────────────
    SYMBOLS_MODE:    str   = os.getenv("SYMBOLS_MODE",    "AUTO")
    MIN_VOLUME_USDT: float = float(os.getenv("MIN_VOLUME_USDT", "50000000"))
    MAX_VOLUME_USDT: float = float(os.getenv("MAX_VOLUME_USDT", "600000000"))
    MAX_SYMBOLS:     int   = int(os.getenv("MAX_SYMBOLS",       "20"))

    # ── Riesgo ─────────────────────────────────────────────
    LEVERAGE:             int   = int(os.getenv("LEVERAGE",         "5"))
    RISK_PER_TRADE_PCT:   float = float(os.getenv("RISK_PCT",       "0.5"))
    MAX_DAILY_DD_PCT:     float = float(os.getenv("MAX_DD_PCT",     "5.0"))
    MAX_OPEN_POSITIONS:   int   = int(os.getenv("MAX_POSITIONS",    "5"))
    TP_RR:                float = float(os.getenv("TP_RR",          "2.0"))

    # ── Sesiones ───────────────────────────────────────────
    ALLOWED_SESSIONS: List[str] = field(
        default_factory=lambda: _lst(os.getenv("SESSIONS", "NY,LDN,ASIA"))
    )

    # ── Timing ─────────────────────────────────────────────
    LOOP_INTERVAL:    int = int(os.getenv("LOOP_INTERVAL",    "30"))
    SCANNER_INTERVAL: int = int(os.getenv("SCANNER_INTERVAL", "3600"))

    # ── Score / señal ──────────────────────────────────────
    SCORE_THR_LONG:      float = float(os.getenv("SCORE_THR_LONG",      "0.60"))
    SCORE_THR_SHORT:     float = float(os.getenv("SCORE_THR_SHORT",     "0.60"))
    DECAY_THR:           float = float(os.getenv("DECAY_THR",           "0.60"))
    ENTRY_MIN_COMPOSITE: float = float(os.getenv("ENTRY_MIN_COMPOSITE", "0.50"))

    # ── Convicción mínima por tier ─────────────────────────
    MIN_CONV_STD:  int = int(os.getenv("MIN_CONV_STD",  "5"))
    MIN_CONV_FUEL: int = int(os.getenv("MIN_CONV_FUEL", "6"))
    MIN_CONV_SUP:  int = int(os.getenv("MIN_CONV_SUP",  "7"))

    # ── Performance filter ─────────────────────────────────
    MIN_PF:    float = float(os.getenv("MIN_PF",    "1.2"))
    PF_WINDOW: int   = int(os.getenv("PF_WINDOW",   "20"))

    # ── OFI ────────────────────────────────────────────────
    OFI_LEVELS:     int   = int(os.getenv("OFI_LEVELS",       "5"))
    OFI_THR_WEAK:   float = float(os.getenv("OFI_THR_WEAK",   "0.22"))
    OFI_THR_STRONG: float = float(os.getenv("OFI_THR_STRONG", "0.40"))

    # ── Funding Rate ───────────────────────────────────────
    FR_BULL_THR:    float = float(os.getenv("FR_BULL_THR",    "0.0001"))
    FR_BEAR_THR:    float = float(os.getenv("FR_BEAR_THR",   "-0.0001"))
    FR_EXTREME_THR: float = float(os.getenv("FR_EXTREME_THR", "0.005"))

    # ── Open Interest ──────────────────────────────────────
    OI_DELTA_THR: float = float(os.getenv("OI_DELTA_THR", "0.005"))

    # ── Trailing SL ────────────────────────────────────────
    TRAIL_ACTIVATE_ATR: float = float(os.getenv("TRAIL_ACTIVATE_ATR", "1.0"))
    TRAIL_ATR_MULT:     float = float(os.getenv("TRAIL_ATR_MULT",     "1.5"))

    # ── Órdenes maker ─────────────────────────────────────
    USE_MAKER_ORDERS:   bool  = _bool(os.getenv("USE_MAKER_ORDERS",  "true"))
    MAKER_TIMEOUT:      int   = int(os.getenv("MAKER_TIMEOUT",       "20"))
    MAKER_OFFSET_PCT:   float = float(os.getenv("MAKER_OFFSET_PCT",  "0.015"))

    # ── Multi-TF ───────────────────────────────────────────
    USE_1H_FILTER:  bool = _bool(os.getenv("USE_1H_FILTER", "true"))
    MULTI_TF_BONUS: int  = int(os.getenv("MULTI_TF_BONUS",  "2"))

    # ── Anti-rate-limit ────────────────────────────────────
    BALANCE_CACHE_TTL: int   = int(os.getenv("BALANCE_CACHE_TTL", "60"))
    API_RETRY_MAX:     int   = int(os.getenv("API_RETRY_MAX",      "3"))
    API_RETRY_DELAY:   float = float(os.getenv("API_RETRY_DELAY",  "2.0"))

    # ── Weights del composite (internos al engine) ─────────
    W_SCORE:  float = float(os.getenv("W_SCORE",  "0.45"))
    W_CVD:    float = float(os.getenv("W_CVD",    "0.25"))
    W_MOM:    float = float(os.getenv("W_MOM",    "0.18"))
    W_DECAY:  float = float(os.getenv("W_DECAY",  "0.12"))


cfg = Config()

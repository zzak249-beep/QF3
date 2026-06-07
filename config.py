"""
Config v6.2 — FIX Railway env vars con sufijos ('900s', '30s', etc.)
"""
import os, re
from dataclasses import dataclass, field
from dotenv import load_dotenv
load_dotenv()

def _int(key: str, default: int) -> int:
    """Lee env var como int, tolerando sufijos como '900s', '15m', '1h'."""
    val = os.getenv(key, str(default)).strip()
    m = re.match(r'^(\d+)', val)
    return int(m.group(1)) if m else default

def _float(key: str, default: float) -> float:
    val = os.getenv(key, str(default)).strip()
    m = re.match(r'^([0-9]*\.?[0-9]+)', val)
    return float(m.group(1)) if m else default

def _bool(key: str, default: bool) -> bool:
    val = os.getenv(key, str(default)).strip().lower()
    return val in ("1", "true", "yes", "on")

def _list(key: str, default: str) -> list[str]:
    return [s.strip() for s in os.getenv(key, default).split(",") if s.strip()]

@dataclass
class Config:
    BINGX_API_KEY : str = os.getenv("BINGX_API_KEY", "")
    BINGX_SECRET  : str = os.getenv("BINGX_SECRET", "")
    TG_TOKEN      : str = os.getenv("TG_TOKEN", "")
    TG_CHAT_ID    : str = os.getenv("TG_CHAT_ID", "")
    MODE         : str = os.getenv("MODE", "SIGNAL")
    SYMBOLS_MODE : str = os.getenv("SYMBOLS_MODE", "AUTO")
    SYMBOLS_MANUAL: list[str] = field(default_factory=lambda: _list("SYMBOLS",
        "BTC-USDT,ETH-USDT,SOL-USDT,BNB-USDT,XRP-USDT,"
        "DOGE-USDT,ADA-USDT,AVAX-USDT,LINK-USDT,DOT-USDT"))

    MIN_VOLUME_USDT: float = _float("MIN_VOLUME_USDT", 5_000_000.0)
    MAX_SYMBOLS    : int   = _int("MAX_SYMBOLS", 40)
    LEVERAGE          : int   = _int("LEVERAGE", 5)
    RISK_PER_TRADE_PCT: float = _float("RISK_PCT", 0.5)
    MAX_DAILY_DD_PCT  : float = _float("MAX_DD_PCT", 80.0)
    MAX_OPEN_POSITIONS: int   = _int("MAX_POSITIONS", 5)
    TP_RR             : float = _float("TP_RR", 2.0)

    ALLOWED_SESSIONS: list[str] = field(default_factory=lambda: _list("SESSIONS", "NY,LDN"))

    LOOP_INTERVAL   : int = _int("LOOP_INTERVAL", 30)
    SCANNER_INTERVAL: int = _int("SCANNER_INTERVAL", 900)   # FIX: tolera '900s'

    # ── Umbrales señal ────────────────────────────────────────
    SCORE_THR_LONG : float = _float("SCORE_THR_LONG",  0.10)
    SCORE_THR_SHORT: float = _float("SCORE_THR_SHORT", 0.10)
    DECAY_THR      : float = _float("DECAY_THR", 0.25)
    DECAY_ADAPT_PCT: int   = _int("DECAY_ADAPT_PCT", 20)

    SC_THR_STD : int = _int("SC_THR_STD",  42)
    SC_THR_FUEL: int = _int("SC_THR_FUEL", 55)
    SC_THR_SUP : int = _int("SC_THR_SUP",  70)

    MIN_CONV_STD  : int   = _int("MIN_CONV_STD",  3)
    MIN_CONV_FUEL : int   = _int("MIN_CONV_FUEL", 4)
    MIN_CONV_SUP  : int   = _int("MIN_CONV_SUP",  5)
    MIN_PROFIT_FACTOR: float = _float("MIN_PF", 1.3)
    PF_WINDOW        : int   = _int("PF_WINDOW", 20)

    ADX_LEN      : int   = _int("ADX_LEN", 14)
    ADX_TREND_THR: float = _float("ADX_TREND_THR", 20.0)

    VOL_ATR_THR: float = _float("VOL_ATR_THR", 0.50)

    OFI_LEVELS    : int   = _int("OFI_LEVELS", 5)
    OFI_THR_WEAK  : float = _float("OFI_THR_WEAK",   0.2)
    OFI_THR_STRONG: float = _float("OFI_THR_STRONG", 0.4)
    FR_BULL_THR   : float = _float("FR_BULL_THR",    0.0001)
    FR_BEAR_THR   : float = _float("FR_BEAR_THR",   -0.0001)
    FR_EXTREME_THR: float = _float("FR_EXTREME_THR", 0.01)
    OI_DELTA_THR  : float = _float("OI_DELTA_THR",   0.003)

    TRAIL_ACTIVATE_ATR: float = _float("TRAIL_ACTIVATE_ATR", 1.0)
    TRAIL_ATR_MULT    : float = _float("TRAIL_ATR_MULT",     1.5)

    USE_MAKER_ORDERS: bool  = _bool("USE_MAKER_ORDERS", True)
    MAKER_TIMEOUT   : int   = _int("MAKER_TIMEOUT", 15)
    MAKER_OFFSET_PCT: float = _float("MAKER_OFFSET_PCT", 0.05)

    USE_1H_FILTER  : bool = _bool("USE_1H_FILTER", False)
    MULTI_TF_BONUS : int  = _int("MULTI_TF_BONUS", 1)

    # Motor (constantes — no necesitan env vars)
    MOM_LEN : int   = 20;  REV_LEN : int   = 8
    VOL_LEN : int   = 14;  ATR_LEN : int   = 10
    W_MOM   : float = 0.40; W_REV  : float = 0.30;  W_VOL  : float = 0.30
    SMO_LEN : int   = 3;   DECAY_LEN: int  = 40
    DP_MULT : float = 2.5; DP_BASE : int   = 20;    SPL_LEN: int   = 5
    BP_THR  : float = 0.25
    ASY_LEN : int   = 10;  ARR     : float = 1.20;  ABR    : float = 1.20
    TL_LOOKBACK: int = 30; TL_LEFT : int   = 5;     TL_RIGHT: int  = 3
    TL_BUF  : float = 0.10
    PL_LEFT : int = 5;    PL_RIGHT: int = 3;        PH_LEFT: int  = 5;  PH_RIGHT: int = 3
    HL_COUNT: int = 2;    HH_COUNT: int = 2;        HL_WINDOW: int = 40
    FVG_MIN : float = 0.2; FVG_BARS: int = 40;      FVG_MITI: bool = True
    OB_IMP  : float = 1.2; OB_BARS : int = 50
    CVD_LEN : int = 20;   CVD_DIV : int = 5;        CVD_ROLL: int = 100
    SQ_LEN  : int = 20;   SQ_BBM  : float = 2.0;   SQ_KCM : float = 1.5

cfg = Config()

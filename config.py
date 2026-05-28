"""
Config v5.3 — umbrales ajustados para señales reales en 3min crypto
"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv
load_dotenv()

@dataclass
class Config:
    BINGX_API_KEY : str = os.getenv("BINGX_API_KEY", "")
    BINGX_SECRET  : str = os.getenv("BINGX_SECRET", "")
    TG_TOKEN      : str = os.getenv("TG_TOKEN", "")
    TG_CHAT_ID    : str = os.getenv("TG_CHAT_ID", "")

    MODE         : str = os.getenv("MODE", "SIGNAL")
    SYMBOLS_MODE : str = os.getenv("SYMBOLS_MODE", "AUTO")
    SYMBOLS_MANUAL: list[str] = field(default_factory=lambda: [
        s.strip() for s in os.getenv("SYMBOLS",
            "BTC-USDT,ETH-USDT,SOL-USDT,BNB-USDT,XRP-USDT,"
            "DOGE-USDT,ADA-USDT,AVAX-USDT,LINK-USDT,DOT-USDT").split(",")
    ])
    MIN_VOLUME_USDT: float = float(os.getenv("MIN_VOLUME_USDT", "10000000"))  # 10M
    MAX_SYMBOLS    : int   = int(os.getenv("MAX_SYMBOLS", "40"))

    LEVERAGE          : int   = int(os.getenv("LEVERAGE", "5"))
    RISK_PER_TRADE_PCT: float = float(os.getenv("RISK_PCT", "0.5"))
    MAX_DAILY_DD_PCT  : float = float(os.getenv("MAX_DD_PCT", "5.0"))
    MAX_OPEN_POSITIONS: int   = int(os.getenv("MAX_POSITIONS", "5"))
    TP_RR             : float = float(os.getenv("TP_RR", "2.0"))

    ALLOWED_SESSIONS: list[str] = field(default_factory=lambda: [
        s.strip() for s in os.getenv("SESSIONS", "NY,LDN").split(",") if s.strip()
    ])
    LOOP_INTERVAL   : int = int(os.getenv("LOOP_INTERVAL", "30"))
    SCANNER_INTERVAL: int = int(os.getenv("SCANNER_INTERVAL", "3600"))

    # ── Umbrales señal — AJUSTADOS para 3min ────────────────
    # v4 usaba 0.63 — demasiado estricto para altcoins en 3min
    # 0.45 genera ~3x más señales manteniendo IC positivo
    SCORE_THR_LONG : float = float(os.getenv("SCORE_THR_LONG",  "0.45"))
    SCORE_THR_SHORT: float = float(os.getenv("SCORE_THR_SHORT", "0.45"))

    # Decay: 0.55 en lugar de 0.65 — más señales sin perder calidad base
    DECAY_THR      : float = float(os.getenv("DECAY_THR", "0.55"))

    # Conviction mínima — bajada 1 punto para generar más señales
    MIN_CONV_STD  : int = int(os.getenv("MIN_CONV_STD",  "5"))
    MIN_CONV_FUEL : int = int(os.getenv("MIN_CONV_FUEL", "6"))
    MIN_CONV_SUP  : int = int(os.getenv("MIN_CONV_SUP",  "7"))

    MIN_PROFIT_FACTOR: float = float(os.getenv("MIN_PF", "1.3"))
    PF_WINDOW        : int   = int(os.getenv("PF_WINDOW", "20"))

    # ── v5: L13 OFI ─────────────────────────────────────────
    OFI_LEVELS    : int   = int(os.getenv("OFI_LEVELS", "5"))
    OFI_THR_WEAK  : float = float(os.getenv("OFI_THR_WEAK", "0.2"))   # más sensible
    OFI_THR_STRONG: float = float(os.getenv("OFI_THR_STRONG", "0.4"))

    # ── v5: L14 Funding Rate ─────────────────────────────────
    FR_BULL_THR   : float = float(os.getenv("FR_BULL_THR",    "0.0001"))
    FR_BEAR_THR   : float = float(os.getenv("FR_BEAR_THR",   "-0.0001"))
    FR_EXTREME_THR: float = float(os.getenv("FR_EXTREME_THR", "0.01"))  # más permisivo

    # ── v5: L15 OI Delta ────────────────────────────────────
    OI_DELTA_THR: float = float(os.getenv("OI_DELTA_THR", "0.003"))

    # ── v5: Trailing SL ─────────────────────────────────────
    TRAIL_ACTIVATE_ATR: float = float(os.getenv("TRAIL_ACTIVATE_ATR", "1.0"))
    TRAIL_ATR_MULT    : float = float(os.getenv("TRAIL_ATR_MULT", "1.5"))

    # ── v5: Maker Orders ────────────────────────────────────
    USE_MAKER_ORDERS: bool  = os.getenv("USE_MAKER_ORDERS", "true").lower() == "true"
    MAKER_TIMEOUT   : int   = int(os.getenv("MAKER_TIMEOUT", "30"))
    MAKER_OFFSET_PCT: float = float(os.getenv("MAKER_OFFSET_PCT", "0.02"))

    # ── v5: Multi-TF ────────────────────────────────────────
    USE_1H_FILTER  : bool = os.getenv("USE_1H_FILTER", "false").lower() == "true"  # OFF por defecto
    MULTI_TF_BONUS : int  = int(os.getenv("MULTI_TF_BONUS", "2"))

    # ── Motor L1–L12 ────────────────────────────────────────
    MOM_LEN : int   = 20;  REV_LEN : int   = 8
    VOL_LEN : int   = 14;  ATR_LEN : int   = 10
    W_MOM   : float = 0.40; W_REV  : float = 0.30;  W_VOL  : float = 0.30
    SMO_LEN : int   = 3;   DECAY_LEN: int  = 40
    DP_MULT : float = 2.5; DP_BASE : int   = 20;    SPL_LEN: int   = 5
    BP_THR  : float = 0.25  # más permisivo (era 0.18)
    ASY_LEN : int   = 10;  ARR     : float = 1.30;  ABR    : float = 1.30  # era 1.40
    TL_LOOKBACK: int = 30; TL_LEFT : int   = 5;     TL_RIGHT: int  = 3
    TL_BUF  : float = 0.10  # era 0.15
    PL_LEFT : int = 5;    PL_RIGHT: int = 3;        PH_LEFT: int  = 5;  PH_RIGHT: int = 3
    HL_COUNT: int = 2;    HH_COUNT: int = 2;        HL_WINDOW: int = 40
    FVG_MIN : float = 0.2; FVG_BARS: int = 40;      FVG_MITI: bool = True  # era 0.3
    OB_IMP  : float = 1.2; OB_BARS : int = 50       # era 1.5
    CVD_LEN : int = 20;   CVD_DIV : int = 5
    SQ_LEN  : int = 20;   SQ_BBM  : float = 2.0;   SQ_KCM : float = 1.5

cfg = Config()

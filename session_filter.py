"""
Filtro de Sesiones Crypto — UTC
Asia: 00-08 | Londres: 07-16 | NY: 13-22
"""
from datetime import datetime, timezone
from config import cfg


class SessionFilter:

    def current_session(self) -> str:
        now_h = datetime.now(timezone.utc).hour
        if 13 <= now_h < 22:
            return "NY"
        if 7 <= now_h < 16:
            return "LDN"
        if 0 <= now_h < 8:
            return "ASIA"
        return "OFF"

    def is_tradeable(self) -> bool:
        """Devuelve True si estamos en una sesión permitida por config."""
        session = self.current_session()
        allowed = cfg.ALLOWED_SESSIONS
        if not allowed:          # lista vacía = operar siempre
            return True
        return session in allowed

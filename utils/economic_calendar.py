"""
utils/economic_calendar.py
═══════════════════════════════════════════════════════════════
Calendario de eventos de alto impacto con ventanas de blackout.

Blackout: 30 min ANTES / 15 min DESPUÉS de cada evento.
En esas ventanas, no se abren posiciones nuevas.

Eventos cubiertos (2025-2026):
  NFP     — primer viernes de cada mes (USD)
  FOMC    — 8 reuniones al año (USD)
  CPI USA — mensual (USD)
  ECB     — cada ~6 semanas (EUR)
  BOE     — cada ~6 semanas (GBP)
  GDP USA — trimestral flash

Pares afectados:
  USD → EUR_USD, GBP_USD, USD_JPY, USD_CHF, AUD_USD, USD_CAD
  EUR → EUR_USD
  GBP → GBP_USD
═══════════════════════════════════════════════════════════════
"""
from datetime import datetime, timezone
from typing import List, Optional


class EventoCalendario:
    def __init__(self, nombre: str, dt: datetime,
                 monedas: List[str], impacto: str = "HIGH"):
        self.nombre  = nombre
        self.dt      = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        self.monedas = monedas
        self.impacto = impacto


class EconomicCalendar:
    """
    Gestiona el calendario de eventos de alto impacto.
    Determina si un timestamp cae en ventana de blackout.
    """

    BLACKOUT_ANTES  = 30   # minutos antes del evento
    BLACKOUT_DESPUES = 15  # minutos después del evento

    def __init__(self):
        self._eventos = self._cargar_eventos()

    def en_blackout(self, timestamp: datetime, par: str) -> bool:
        """
        True si el timestamp cae en ventana de alto riesgo para ese par.

        Args:
            timestamp: momento actual (UTC)
            par: p.ej. "EUR_USD"

        Returns:
            True → no operar
        """
        if not timestamp:
            return False

        ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        monedas_par = self._monedas_del_par(par)

        for evento in self._eventos:
            if not any(m in monedas_par for m in evento.monedas):
                continue
            delta_min = (ts - evento.dt).total_seconds() / 60
            if -self.BLACKOUT_ANTES <= delta_min <= self.BLACKOUT_DESPUES:
                return True
        return False

    def proximo_evento(self, timestamp: datetime,
                        par: str,
                        ventana_h: int = 4) -> Optional[dict]:
        """
        Retorna el próximo evento relevante en las próximas `ventana_h` horas.
        Útil para reducir confianza de señales cerca de eventos.
        """
        if not timestamp:
            return None
        ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        monedas_par = self._monedas_del_par(par)

        for evento in sorted(self._eventos, key=lambda e: e.dt):
            if not any(m in monedas_par for m in evento.monedas):
                continue
            delta_min = (evento.dt - ts).total_seconds() / 60
            if 0 < delta_min <= ventana_h * 60:
                return {
                    "nombre":     evento.nombre,
                    "en_minutos": int(delta_min),
                    "monedas":    evento.monedas,
                }
        return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _monedas_del_par(par: str) -> List[str]:
        """EUR_USD → ['EUR', 'USD']"""
        return par.replace("_", "/").split("/")

    @staticmethod
    def _u(y, mo, d, h=13, mi=30) -> datetime:
        """Shorthand para crear datetime UTC."""
        return datetime(y, mo, d, h, mi, 0, tzinfo=timezone.utc)

    def _cargar_eventos(self) -> List[EventoCalendario]:
        """
        Eventos de alto impacto 2025-2026 hardcodeados para backtest.
        Hora aproximada en UTC de la publicación oficial.
        """
        u = self._u
        eventos = [

            # ── NFP (Non-Farm Payrolls) — primer viernes, 12:30 UTC ──────────
            EventoCalendario("NFP", u(2025, 1, 3, 13, 30),  ["USD"]),
            EventoCalendario("NFP", u(2025, 2, 7, 13, 30),  ["USD"]),
            EventoCalendario("NFP", u(2025, 3, 7, 13, 30),  ["USD"]),
            EventoCalendario("NFP", u(2025, 4, 4, 12, 30),  ["USD"]),
            EventoCalendario("NFP", u(2025, 5, 2, 12, 30),  ["USD"]),
            EventoCalendario("NFP", u(2025, 6, 6, 12, 30),  ["USD"]),
            EventoCalendario("NFP", u(2025, 7, 4, 12, 30),  ["USD"]),
            EventoCalendario("NFP", u(2025, 8, 1, 12, 30),  ["USD"]),
            EventoCalendario("NFP", u(2025, 9, 5, 12, 30),  ["USD"]),
            EventoCalendario("NFP", u(2025, 10, 3, 12, 30), ["USD"]),
            EventoCalendario("NFP", u(2025, 11, 7, 13, 30), ["USD"]),
            EventoCalendario("NFP", u(2025, 12, 5, 13, 30), ["USD"]),
            EventoCalendario("NFP", u(2026, 1, 9, 13, 30),  ["USD"]),
            EventoCalendario("NFP", u(2026, 2, 6, 13, 30),  ["USD"]),
            EventoCalendario("NFP", u(2026, 3, 6, 13, 30),  ["USD"]),
            EventoCalendario("NFP", u(2026, 4, 3, 12, 30),  ["USD"]),
            EventoCalendario("NFP", u(2026, 5, 8, 12, 30),  ["USD"]),
            EventoCalendario("NFP", u(2026, 6, 5, 12, 30),  ["USD"]),

            # ── FOMC — reuniones Fed 2025 (19:00 UTC) ────────────────────────
            EventoCalendario("FOMC", u(2025, 1, 29, 19, 0), ["USD"]),
            EventoCalendario("FOMC", u(2025, 3, 19, 18, 0), ["USD"]),
            EventoCalendario("FOMC", u(2025, 5, 7, 18, 0),  ["USD"]),
            EventoCalendario("FOMC", u(2025, 6, 18, 18, 0), ["USD"]),
            EventoCalendario("FOMC", u(2025, 7, 30, 18, 0), ["USD"]),
            EventoCalendario("FOMC", u(2025, 9, 17, 18, 0), ["USD"]),
            EventoCalendario("FOMC", u(2025, 11, 5, 19, 0), ["USD"]),
            EventoCalendario("FOMC", u(2025, 12, 17, 19, 0),["USD"]),
            EventoCalendario("FOMC", u(2026, 1, 28, 19, 0), ["USD"]),
            EventoCalendario("FOMC", u(2026, 3, 18, 18, 0), ["USD"]),
            EventoCalendario("FOMC", u(2026, 4, 29, 18, 0), ["USD"]),
            EventoCalendario("FOMC", u(2026, 6, 17, 18, 0), ["USD"]),

            # ── CPI USA — ~tercer miércoles/martes, 12:30 UTC ─────────────────
            EventoCalendario("CPI_USD", u(2025, 1, 15, 13, 30), ["USD"]),
            EventoCalendario("CPI_USD", u(2025, 2, 12, 13, 30), ["USD"]),
            EventoCalendario("CPI_USD", u(2025, 3, 12, 12, 30), ["USD"]),
            EventoCalendario("CPI_USD", u(2025, 4, 10, 12, 30), ["USD"]),
            EventoCalendario("CPI_USD", u(2025, 5, 13, 12, 30), ["USD"]),
            EventoCalendario("CPI_USD", u(2025, 6, 11, 12, 30), ["USD"]),
            EventoCalendario("CPI_USD", u(2025, 7, 15, 12, 30), ["USD"]),
            EventoCalendario("CPI_USD", u(2025, 8, 12, 12, 30), ["USD"]),
            EventoCalendario("CPI_USD", u(2025, 9, 10, 12, 30), ["USD"]),
            EventoCalendario("CPI_USD", u(2025, 10, 15, 12, 30),["USD"]),
            EventoCalendario("CPI_USD", u(2025, 11, 12, 13, 30),["USD"]),
            EventoCalendario("CPI_USD", u(2025, 12, 10, 13, 30),["USD"]),
            EventoCalendario("CPI_USD", u(2026, 1, 14, 13, 30), ["USD"]),
            EventoCalendario("CPI_USD", u(2026, 2, 11, 13, 30), ["USD"]),
            EventoCalendario("CPI_USD", u(2026, 3, 11, 12, 30), ["USD"]),
            EventoCalendario("CPI_USD", u(2026, 4, 15, 12, 30), ["USD"]),
            EventoCalendario("CPI_USD", u(2026, 5, 13, 12, 30), ["USD"]),

            # ── ECB Decision — cada ~6 semanas, 12:15 UTC ─────────────────────
            EventoCalendario("ECB", u(2025, 1, 30, 13, 15), ["EUR"]),
            EventoCalendario("ECB", u(2025, 3, 6, 13, 15),  ["EUR"]),
            EventoCalendario("ECB", u(2025, 4, 17, 12, 15), ["EUR"]),
            EventoCalendario("ECB", u(2025, 6, 5, 12, 15),  ["EUR"]),
            EventoCalendario("ECB", u(2025, 7, 24, 12, 15), ["EUR"]),
            EventoCalendario("ECB", u(2025, 9, 11, 12, 15), ["EUR"]),
            EventoCalendario("ECB", u(2025, 10, 30, 13, 15),["EUR"]),
            EventoCalendario("ECB", u(2025, 12, 11, 13, 15),["EUR"]),
            EventoCalendario("ECB", u(2026, 1, 22, 13, 15), ["EUR"]),
            EventoCalendario("ECB", u(2026, 3, 5, 13, 15),  ["EUR"]),
            EventoCalendario("ECB", u(2026, 4, 16, 12, 15), ["EUR"]),
            EventoCalendario("ECB", u(2026, 6, 4, 12, 15),  ["EUR"]),

            # ── BOE Decision — cada ~6 semanas, 12:00 UTC ─────────────────────
            EventoCalendario("BOE", u(2025, 2, 6, 12, 0),  ["GBP"]),
            EventoCalendario("BOE", u(2025, 3, 20, 12, 0), ["GBP"]),
            EventoCalendario("BOE", u(2025, 5, 8, 11, 0),  ["GBP"]),
            EventoCalendario("BOE", u(2025, 6, 19, 11, 0), ["GBP"]),
            EventoCalendario("BOE", u(2025, 8, 7, 11, 0),  ["GBP"]),
            EventoCalendario("BOE", u(2025, 9, 18, 11, 0), ["GBP"]),
            EventoCalendario("BOE", u(2025, 11, 6, 12, 0), ["GBP"]),
            EventoCalendario("BOE", u(2025, 12, 18, 12, 0),["GBP"]),
            EventoCalendario("BOE", u(2026, 2, 5, 12, 0),  ["GBP"]),
            EventoCalendario("BOE", u(2026, 3, 19, 12, 0), ["GBP"]),
            EventoCalendario("BOE", u(2026, 5, 7, 11, 0),  ["GBP"]),
            EventoCalendario("BOE", u(2026, 6, 18, 11, 0), ["GBP"]),

            # ── GDP USA flash — trimestral, 12:30 UTC ─────────────────────────
            EventoCalendario("GDP_USA", u(2025, 1, 30, 13, 30), ["USD"]),
            EventoCalendario("GDP_USA", u(2025, 4, 30, 12, 30), ["USD"]),
            EventoCalendario("GDP_USA", u(2025, 7, 30, 12, 30), ["USD"]),
            EventoCalendario("GDP_USA", u(2025, 10, 30, 12, 30),["USD"]),
            EventoCalendario("GDP_USA", u(2026, 1, 29, 13, 30), ["USD"]),
            EventoCalendario("GDP_USA", u(2026, 4, 29, 12, 30), ["USD"]),
        ]
        return eventos

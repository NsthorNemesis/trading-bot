"""
utils/oanda_sentiment.py
═══════════════════════════════════════════════════════════════
Filtro de sentimiento retail OANDA.

Lógica contrarian:
  ≥70% retail LONG  → sesgo contrarian_short (reducir conf LONG, subir SHORT)
  ≤30% retail LONG  → sesgo contrarian_long  (subir conf LONG, reducir SHORT)
  30-70%            → neutral

En backtest: devuelve neutral (no hay datos históricos de sentimiento).
En live: llama al endpoint /labs/v1/sentiment de OANDA.

Ajuste de confianza:
  Confirma dirección  → +0.05
  Contradice dirección → -0.08
  Neutral             → 0
═══════════════════════════════════════════════════════════════
"""
import logging
import time
from typing import Optional

logger = logging.getLogger("oanda_sentiment")


class OandaSentiment:
    """
    Consulta y cachea el sentimiento retail de OANDA por par.
    """

    UMBRAL_LONG  = 0.70   # >70% retail long → contrarian short
    UMBRAL_SHORT = 0.30   # <30% retail long → contrarian long
    AJUSTE_CONFIRMA    =  0.05
    AJUSTE_CONTRADICE  = -0.08
    CACHE_TTL_SEG      = 900   # 15 minutos

    def __init__(self, oanda_api=None, modo_backtest: bool = True):
        """
        Args:
            oanda_api:      instancia de oandapyV20.API (None en backtest)
            modo_backtest:  si True, siempre devuelve neutral
        """
        self._api           = oanda_api
        self._modo_backtest = modo_backtest
        self._cache: dict   = {}   # par → {"data": dict, "ts": float}

    def get_sentiment(self, par: str) -> dict:
        """
        Retorna el sentimiento retail para el par.

        Returns:
            {
              "pct_long":  float 0-1,
              "pct_short": float 0-1,
              "sesgo":     "neutral" | "contrarian_long" | "contrarian_short",
            }
        """
        if self._modo_backtest:
            return self._neutral()

        # Revisar cache
        cached = self._cache.get(par)
        if cached and (time.time() - cached["ts"]) < self.CACHE_TTL_SEG:
            return cached["data"]

        try:
            data = self._llamar_api(par)
            self._cache[par] = {"data": data, "ts": time.time()}
            return data
        except Exception as e:
            logger.warning(f"[Sentiment] Error consultando {par}: {e} — usando neutral")
            return self._neutral()

    def ajustar_confianza(self, confianza: float,
                           par: str, direccion: str) -> float:
        """
        Ajusta la confianza de una señal según el sesgo de sentimiento.

        Args:
            confianza:  confianza base (0-1)
            par:        par de divisas
            direccion:  "long" o "short"

        Returns:
            confianza ajustada (clipeada a 0.0-1.0)
        """
        sent = self.get_sentiment(par)
        sesgo = sent.get("sesgo", "neutral")

        if sesgo == "neutral":
            return confianza

        # ¿El sesgo contrarian confirma o contradice la dirección?
        # contrarian_long → retail está short → institucionales long
        # contrarian_short → retail está long → institucionales short
        confirma = (
            (sesgo == "contrarian_long"  and direccion == "long") or
            (sesgo == "contrarian_short" and direccion == "short")
        )

        ajuste = self.AJUSTE_CONFIRMA if confirma else self.AJUSTE_CONTRADICE
        nueva  = max(0.0, min(1.0, confianza + ajuste))

        if ajuste != 0:
            logger.debug(
                f"[Sentiment] {par} {direccion.upper()} | sesgo={sesgo} "
                f"conf {confianza:.2f} → {nueva:.2f}"
            )
        return nueva

    # ── Internos ──────────────────────────────────────────────────────────────

    def _neutral(self) -> dict:
        return {"pct_long": 0.50, "pct_short": 0.50, "sesgo": "neutral"}

    def _llamar_api(self, par: str) -> dict:
        """
        Llama al endpoint de sentimiento de OANDA.
        Solo activo en modo live (modo_backtest=False).
        """
        try:
            try:
                import oandapyV20.endpoints.labs as labs
            except ImportError:
                raise Exception("oandapyV20.endpoints.labs no disponible en esta versión")
            endpoint = labs.Sentiment(params={"instrument": par})
            rv = self._api.request(endpoint)
            data = rv.get(par, rv)

            pct_long = float(data.get("long", {}).get("percent", 50)) / 100
            pct_short = 1.0 - pct_long

            if pct_long >= self.UMBRAL_LONG:
                sesgo = "contrarian_short"
            elif pct_long <= self.UMBRAL_SHORT:
                sesgo = "contrarian_long"
            else:
                sesgo = "neutral"

            logger.info(
                f"[Sentiment] {par}: {pct_long:.0%} long | sesgo={sesgo}"
            )
            return {"pct_long": pct_long, "pct_short": pct_short, "sesgo": sesgo}

        except Exception as e:
            logger.warning(f"[Sentiment] API error {par}: {e}")
            return self._neutral()

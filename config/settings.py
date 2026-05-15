"""
config/settings.py
Configuracion central del Trading Bot v11.

PRINCIPIO: strategy_params.json es la UNICA fuente de verdad para
parametros operacionales. Si el archivo no existe o esta corrupto,
el bot falla con un mensaje claro -- no hay fallback silencioso.
"""
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("settings")

# APIs externas
OANDA_TOKEN   = os.getenv("OANDA_ACCESS_TOKEN", "")
OANDA_ACCOUNT = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ENV     = os.getenv("OANDA_ENVIRONMENT", "practice")

DEEPSEEK_KEY      = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MODEL_FAST        = "deepseek-chat"
MODEL_DEEP        = "deepseek-reasoner"

ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Paths
BASE_DIR    = Path(__file__).parent.parent
HIST_DIR    = BASE_DIR / "data" / "historical"
BT_DIR      = BASE_DIR / "data" / "backtesting"
CALIB_DIR   = BASE_DIR / "data" / "calibration"
PARAMS_FILE = CALIB_DIR / "strategy_params.json"
TRADES_LOG  = BASE_DIR / "logs" / "trades.json"
BOT_LOG     = BASE_DIR / "logs" / "trading_bot.log"

# Pares soportados (display names). Pares ACTIVOS se leen de strategy_params.json.
PARES_DISPLAY = {
    "EUR_USD": "EUR/USD", "GBP_USD": "GBP/USD",
    "USD_JPY": "USD/JPY", "USD_CHF": "USD/CHF",
    "AUD_USD": "AUD/USD", "USD_CAD": "USD/CAD",
    "NZD_USD": "NZD/USD", "EUR_GBP": "EUR/GBP",
    "EUR_JPY": "EUR/JPY", "GBP_JPY": "GBP/JPY",
}

TIMEFRAMES_HISTORICO = ["M1", "M15", "H1", "H4"]
ANOS_HISTORICO       = 5

# Schema de validacion: campos requeridos y sus tipos
PARAMS_SCHEMA = {
    "estrategias_activas": list,
    "pares_activos":       list,
    "sl_atr_mult":         (int, float),
    "rr_ratio":            (int, float),
    "min_win_rate":        (int, float),
    "min_confidence":      (int, float),
    "riesgo_pct":          (int, float),
}

# Valores opcionales: se aplican solo si no estan en el JSON
PARAMS_DEFAULTS_OPCIONALES = {
    "signal_timeframe":    "M15",
    "sesiones_activas":    ["london", "overlap", "new_york"],
    "cooldown_minutes":    15,
    "max_posiciones":      3,
    "max_drawdown_dia":    0.04,
    "circuit_breaker_pct": 0.15,
    "min_sl_pips":         10,
    "max_sl_pips":         40,
    "estrategias_pausadas": [],
}


def _validar_params(params, source):
    errores = []
    for campo, tipo in PARAMS_SCHEMA.items():
        if campo not in params:
            errores.append("  - Falta el campo '{}'".format(campo))
        elif not isinstance(params[campo], tipo):
            tipo_str = tipo.__name__ if isinstance(tipo, type) else "/".join(t.__name__ for t in tipo)
            errores.append("  - '{}': esperado {}, recibido {}".format(
                campo, tipo_str, type(params[campo]).__name__))
    if "rr_ratio" in params and params.get("rr_ratio", 0) < 1.0:
        errores.append("  - 'rr_ratio' debe ser >= 1.0")
    if "riesgo_pct" in params and not (0 < params.get("riesgo_pct", 0) <= 0.05):
        errores.append("  - 'riesgo_pct' debe estar entre 0 y 0.05")
    if "pares_activos" in params and not params["pares_activos"]:
        errores.append("  - 'pares_activos' no puede estar vacio")
    if errores:
        raise ValueError(
            "\nErrores de validacion en {}:\n".format(source) +
            "\n".join(errores) +
            "\n\nRevisa: {}".format(PARAMS_FILE)
        )


def cargar_params():
    """
    Carga parametros desde strategy_params.json.
    Falla EXPLICITO si: archivo no existe, JSON corrupto, campos requeridos faltantes.
    No hay fallback silencioso.
    """
    # Advertir sobre JSON duplicado en la raiz
    root_json = BASE_DIR / "strategy_params.json"
    if root_json.exists():
        logger.warning(
            "ADVERTENCIA: strategy_params.json encontrado en raiz del proyecto ({}). "
            "El bot usa EXCLUSIVAMENTE {}. El archivo raiz es IGNORADO. "
            "Eliminalo: rm {}".format(root_json, PARAMS_FILE, root_json)
        )

    if not PARAMS_FILE.exists():
        raise FileNotFoundError(
            "\n" + "=" * 60 + "\n"
            "CRITICO: Archivo de configuracion no encontrado:\n"
            "  {}\n\n"
            "El bot no puede arrancar sin configuracion explicita.\n"
            "Restaura desde backup o crea el archivo.\n".format(PARAMS_FILE) +
            "=" * 60
        )

    try:
        with open(PARAMS_FILE, encoding="utf-8") as f:
            params = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "strategy_params.json corrupto:\n  {}\nRevisa: {}".format(exc, PARAMS_FILE)
        ) from exc

    _validar_params(params, str(PARAMS_FILE))

    for campo, valor in PARAMS_DEFAULTS_OPCIONALES.items():
        params.setdefault(campo, valor)

    return params


try:
    PARAMS = cargar_params()
    PARES  = PARAMS["pares_activos"]
except (FileNotFoundError, ValueError):
    raise

"""
strategies/__init__.py
Auto-descubrimiento de estrategias de trading.

Escanea todos los módulos de este paquete, encuentra clases que heredan
de BaseStrategy, y las retorna como diccionario {nombre: instancia}.

Para agregar una nueva estrategia, basta crear un archivo .py en esta
carpeta — no hay que modificar este __init__.py ni ningún otro archivo.
"""
import importlib
import logging
import pkgutil
from pathlib import Path
from typing import Dict, List, Optional

from .base_strategy import BaseStrategy

logger = logging.getLogger("strategies")

_SKIP_MODULES = {"base_strategy", "__init__"}


def load_strategies(
    active_only: Optional[List[str]] = None,
) -> Dict[str, BaseStrategy]:
    """
    Carga dinámicamente todas las estrategias disponibles en este paquete.

    Args:
        active_only: si se provee, filtra solo las estrategias cuyos NAME
                     están en esta lista. Usado para respetar estrategias_activas.

    Returns:
        Dict {NAME: instancia_estrategia}
    """
    strategies: Dict[str, BaseStrategy] = {}
    package_dir = Path(__file__).parent

    for module_info in pkgutil.iter_modules([str(package_dir)]):
        if module_info.name in _SKIP_MODULES:
            continue
        try:
            module = importlib.import_module(
                f".{module_info.name}", package=__name__
            )
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, BaseStrategy)
                    and attr is not BaseStrategy
                    and getattr(attr, "NAME", "")
                ):
                    instance = attr()
                    strategies[instance.NAME] = instance
                    logger.debug(f"Estrategia registrada: {instance.NAME}")
        except Exception as exc:
            logger.warning(
                f"No se pudo cargar módulo strategies/{module_info.name}: {exc}"
            )

    all_names = sorted(strategies.keys())
    logger.info(f"Estrategias disponibles en disco: {all_names}")

    if active_only is not None:
        strategies = {k: v for k, v in strategies.items() if k in active_only}
        logger.info(f"Estrategias activas (filtradas): {sorted(strategies.keys())}")

    return strategies


__all__ = ["BaseStrategy", "load_strategies"]

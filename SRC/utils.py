"""Utilidades comunes del proyecto Golden Hour.

Toda la configuración del proyecto (departamentos, rutas, categorías
resolutivas, bounding box, motor de ruteo, etc.) vive en ``config.md`` como
un único bloque ```yaml```. Este módulo lo lee y expone accesores tipados
para que el resto del pipeline (``src/``, notebooks, dashboard) nunca tenga
que hardcodear esos valores.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.md"

_YAML_BLOCK_RE = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)


class ConfigError(RuntimeError):
    """Error al leer o parsear config.md."""


@lru_cache(maxsize=1)
def load_config(config_path: str | Path = CONFIG_PATH) -> dict[str, Any]:
    """Extrae y parsea el bloque YAML embebido en config.md.

    El resultado se cachea en memoria (config.md no cambia durante una
    ejecución); usar ``load_config.cache_clear()`` si se necesita recargar.
    """
    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"No se encontró el archivo de configuración: {path}")

    text = path.read_text(encoding="utf-8")
    match = _YAML_BLOCK_RE.search(text)
    if not match:
        raise ConfigError(f"No se encontró un bloque ```yaml``` en {path}")

    try:
        config = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Error al parsear YAML en {path}: {exc}") from exc

    if not isinstance(config, dict):
        raise ConfigError(f"El bloque YAML en {path} no es un mapeo válido")

    return config


def get_departments() -> dict[str, dict[str, str]]:
    """Devuelve el dict {region_natural: {name, region_type}}."""
    return load_config()["departments"]


def get_department_names() -> list[str]:
    """Lista plana de los 3 departamentos seleccionados."""
    return [dept["name"] for dept in get_departments().values()]


def get_paths(create: bool = True) -> dict[str, Path]:
    """Rutas de datos/reporte/logs como ``Path`` absolutos.

    Si ``create=True`` (default), crea los directorios si no existen.
    """
    raw_paths = load_config()["paths"]
    paths = {key: PROJECT_ROOT / value for key, value in raw_paths.items()}
    if create:
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
    return paths


def get_health_categories() -> list[str]:
    """Lista blanca de categorías resolutivas (p.ej. 'II-1', 'III-E')."""
    return list(load_config()["health_facility_categories"])


def is_valid_category(category: str) -> bool:
    return category in get_health_categories()


def get_peru_bbox() -> dict[str, float]:
    """Bounding box geográfico de Perú: lon_min/lon_max/lat_min/lat_max."""
    return dict(load_config()["peru_bbox"])


def is_within_peru_bbox(lon: float, lat: float) -> bool:
    bbox = get_peru_bbox()
    return bbox["lon_min"] <= lon <= bbox["lon_max"] and bbox["lat_min"] <= lat <= bbox["lat_max"]


def get_routing_config() -> dict[str, Any]:
    """Config del motor de ruteo activo (``routing.engine`` + sub-config)."""
    return dict(load_config()["routing"])


def get_golden_hour_threshold() -> int:
    """Umbral en minutos que define la 'hora dorada'."""
    return int(load_config()["golden_hour"]["threshold_minutes"])


def get_crs() -> dict[str, str]:
    """CRS geográfico y proyectado a usar en el pipeline geoespacial."""
    return dict(load_config()["crs"])


def get_data_sources() -> dict[str, Any]:
    """Config de fuentes de datos (URLs, columnas esperadas) para acquisition.py."""
    return dict(load_config()["data_sources"])


def get_routing_execution_config() -> dict[str, Any]:
    """Config de ejecución del ruteo (muestreo, velocidades de respaldo, caché) para routing.py."""
    return dict(load_config()["routing_execution"])


def setup_logging(name: str = "golden_hour", level: int = logging.INFO) -> logging.Logger:
    """Logger a consola + archivo en ``logs/<name>.log`` (ruta desde config.md)."""
    logs_dir = get_paths()["logs"]
    log_file = logs_dir / f"{name}.log"

    logger = logging.getLogger(name)
    logger.setLevel(level)
    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        )

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    return logger


if __name__ == "__main__":
    from pprint import pprint

    pprint(load_config())
    print("\nRutas resueltas:")
    pprint(get_paths())

"""Fase 1 — Adquisición de datos crudos para Golden Hour.

Descarga (o reutiliza desde caché) las tres fuentes de datos declaradas en
``config.md -> data_sources``:

    a) RENIPRESS      — Establecimientos de Salud (SUSALUD/MINSA)
    b) Centros Poblados — IGN, vía Plataforma Nacional de Datos Abiertos
       (sustituye a MINEDU/SIGMED: ver nota en config.md, sección
       ``data_sources.centros_poblados`` — SIGMED no expone una URL de
       descarga estática)
    c) Límites Administrativos — Departamentos, Provincias y Distritos (IGN)
    d) Centros Poblados INEI (censo 1999/2002) — vía ArcGIS REST de
       INGEMMET/GEOCATMIN. Fuente única de puntos de demanda (reemplaza a
       (b) para ese propósito): a diferencia del dataset IGN, trae población
       real y clasificación urbano/rural oficial, y no tiene el defecto de
       coordenadas que invalida el 100% de los registros de Loreto en (b).
       Ver ``config.md -> data_sources.centros_poblados_inei_censo``.

Cada función ``fetch_*`` es reejecutable: si el archivo destino ya existe en
``data/raw/`` no se vuelve a descargar (salvo ``force=True``), y cualquier
error de descarga queda registrado con fecha/hora en
``logs/acquisition.log`` sin interrumpir el resto de la adquisición.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import urllib3

import utils

TIMEOUT_SECONDS = 60
MAX_RETRIES = 2
# El portal datosabiertos.gob.pe está detrás de un WAF que bloquea (HTTP 418)
# el User-Agent por defecto de `requests` ("python-requests/x.y"); se envía
# un UA de navegador real para poder consultar la API y descargar archivos.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-PE,es;q=0.9,en;q=0.8",
}

logger = utils.setup_logging("acquisition")


class AcquisitionError(RuntimeError):
    """Error irrecuperable al adquirir una fuente de datos."""


def _is_cached(dest_path: Path) -> bool:
    return dest_path.exists() and dest_path.stat().st_size > 0


def _download_to(url: str, dest_path: Path) -> None:
    """Descarga ``url`` a ``dest_path`` de forma atómica (archivo temporal + rename)."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with requests.get(url, headers=HEADERS, stream=True, timeout=TIMEOUT_SECONDS) as resp:
                resp.raise_for_status()
                with open(tmp_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            fh.write(chunk)
            shutil.move(str(tmp_path), str(dest_path))
            return
        except (requests.RequestException, OSError) as exc:
            last_exc = exc
            tmp_path.unlink(missing_ok=True)
            logger.warning(
                "Intento %d/%d fallido descargando %s: %s", attempt, MAX_RETRIES, url, exc
            )

    logger.error(
        "Descarga fallida definitivamente | url=%s | destino=%s | error=%s",
        url, dest_path, last_exc,
    )
    raise AcquisitionError(f"No se pudo descargar {url}: {last_exc}") from last_exc


def _created_date(resource: dict[str, Any]) -> datetime:
    match = re.search(r"(\d{2})/(\d{2})/(\d{4})", resource.get("created", ""))
    if not match:
        return datetime.min
    month, day, year = match.groups()
    return datetime(int(year), int(month), int(day))


def _resolve_latest_renipress_url(base_url: str, package_id: str) -> str:
    """Consulta la API CKAN del portal de datos abiertos por el CSV RENIPRESS más reciente.

    RENIPRESS se publica mensualmente con un nombre de archivo distinto cada
    vez (p.ej. RENIPRESS_31-08-2026.csv), por lo que fijar una URL estática
    quedaría obsoleto; se resuelve dinámicamente vía la API CKAN del portal.
    """
    api_url = f"{base_url.rstrip('/')}/api/3/action/package_show"
    resp = requests.get(api_url, headers=HEADERS, params={"id": package_id}, timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        raise AcquisitionError(f"CKAN respondió success=false para el paquete '{package_id}'")

    # Este portal CKAN envuelve el paquete en una lista de 1 elemento en vez
    # de devolverlo como dict directo; se soportan ambas formas por robustez.
    result = payload["result"]
    package = result[0] if isinstance(result, list) else result

    csv_resources = [
        r for r in package["resources"] if r.get("format", "").lower() == "csv"
    ]
    if not csv_resources:
        raise AcquisitionError(f"El paquete CKAN '{package_id}' no tiene recursos CSV")

    latest = max(csv_resources, key=_created_date)
    return latest["url"]


def fetch_renipress(force: bool = False) -> Path:
    """Descarga (o reutiliza) el CSV RENIPRESS más reciente."""
    data_sources = utils.get_data_sources()
    cfg = data_sources["renipress"]
    dest_path = utils.get_paths()["data_raw"] / cfg["target_filename"]

    if _is_cached(dest_path) and not force:
        logger.info("RENIPRESS ya está en caché: %s (usar force=True para re-descargar)", dest_path)
        return dest_path

    try:
        url = _resolve_latest_renipress_url(data_sources["ckan_base_url"], cfg["ckan_package_id"])
        logger.info("URL RENIPRESS resuelta dinámicamente vía API CKAN: %s", url)
    except (requests.RequestException, AcquisitionError) as exc:
        url = cfg["fallback_url"]
        logger.warning(
            "No se pudo resolver la URL de RENIPRESS vía API CKAN (%s); usando fallback_url: %s",
            exc, url,
        )

    _download_to(url, dest_path)
    logger.info("RENIPRESS descargado correctamente: %s", dest_path)
    return dest_path


def fetch_centros_poblados(force: bool = False) -> Path:
    """Descarga (o reutiliza) el shapefile de Centros Poblados (IGN, sustituto de MINEDU/SIGMED)."""
    cfg = utils.get_data_sources()["centros_poblados"]
    dest_path = utils.get_paths()["data_raw"] / cfg["target_filename"]

    if _is_cached(dest_path) and not force:
        logger.info("Centros Poblados ya está en caché: %s", dest_path)
        return dest_path

    _download_to(cfg["direct_url"], dest_path)
    logger.info("Centros Poblados descargado correctamente: %s", dest_path)
    return dest_path


def fetch_limites_administrativos(force: bool = False) -> dict[str, Path]:
    """Descarga (o reutiliza) los shapefiles de límites departamentales, provinciales y distritales."""
    cfg = utils.get_data_sources()["limites_administrativos"]
    paths = utils.get_paths()
    result: dict[str, Path] = {}

    for nivel, nivel_cfg in cfg.items():
        dest_path = paths["data_raw"] / nivel_cfg["target_filename"]
        if _is_cached(dest_path) and not force:
            logger.info("Límites '%s' ya en caché: %s", nivel, dest_path)
        else:
            _download_to(nivel_cfg["direct_url"], dest_path)
            logger.info("Límites '%s' descargados correctamente: %s", nivel, dest_path)
        result[nivel] = dest_path

    return result


def _arcgis_query(url: str, params: dict[str, Any], insecure_ssl: bool) -> dict[str, Any]:
    """Ejecuta una consulta ArcGIS REST con reintentos; devuelve el JSON decodificado."""
    if insecure_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url, params=params, headers=HEADERS, timeout=TIMEOUT_SECONDS, verify=not insecure_ssl
            )
            resp.raise_for_status()
            payload = resp.json()
            if "error" in payload:
                raise AcquisitionError(f"ArcGIS respondió error: {payload['error']}")
            return payload
        except (requests.RequestException, ValueError, AcquisitionError) as exc:
            last_exc = exc
            logger.warning("Intento %d/%d fallido en consulta ArcGIS %s: %s", attempt, MAX_RETRIES, url, exc)

    logger.error("Consulta ArcGIS fallida definitivamente | url=%s | params=%s | error=%s", url, params, last_exc)
    raise AcquisitionError(f"No se pudo consultar {url}: {last_exc}") from last_exc


def fetch_centros_poblados_inei_censo(force: bool = False) -> Path:
    """Descarga (o reutiliza) Centros Poblados INEI (censo 1999/2002, vía ArcGIS REST).

    Fuente única de puntos de demanda para los 3 departamentos configurados
    en ``departments`` (ver config.md): trae población real y clasificación
    urbano/rural oficial, ninguna disponible en la fuente IGN (``centros_poblados``).

    El servicio ArcGIS de origen no soporta paginación por offset y su límite
    es de 1000 registros por consulta, así que se pagina manualmente
    consultando cada provincia de cada departamento por separado (todas caen
    bajo ese límite en la práctica) y se concatenan los resultados.
    """
    cfg = utils.get_data_sources()["centros_poblados_inei_censo"]
    dest_path = utils.get_paths()["data_raw"] / cfg["target_filename"]

    if _is_cached(dest_path) and not force:
        logger.info("Centros Poblados INEI (censo) ya está en caché: %s", dest_path)
        return dest_path

    insecure_ssl = bool(cfg.get("insecure_ssl", False))
    if insecure_ssl:
        logger.warning(
            "Se omite la verificación TLS para %s (cadena de certificados incompleta en el servidor, "
            "verificado manualmente); solo se usa para leer datos públicos de solo lectura.",
            cfg["arcgis_query_url"],
        )

    dept_field, prov_field = cfg["department_field"], cfg["province_field"]
    department_values = [name.upper() for name in utils.get_department_names()]

    all_records: list[dict[str, Any]] = []
    for dept_value in department_values:
        provinces_payload = _arcgis_query(
            cfg["arcgis_query_url"],
            {
                "where": f"{dept_field}='{dept_value}'",
                "outFields": prov_field,
                "returnDistinctValues": "true",
                "returnGeometry": "false",
                "f": "json",
            },
            insecure_ssl,
        )
        provinces = [f["attributes"][prov_field] for f in provinces_payload.get("features", [])]
        logger.info("Centros Poblados INEI [%s]: %d provincias a consultar (%s)", dept_value, len(provinces), provinces)

        for prov in provinces:
            payload = _arcgis_query(
                cfg["arcgis_query_url"],
                {
                    "where": f"{dept_field}='{dept_value}' AND {prov_field}='{prov}'",
                    "outFields": cfg["out_fields"],
                    "returnGeometry": "false",
                    "f": "json",
                },
                insecure_ssl,
            )
            records = [f["attributes"] for f in payload.get("features", [])]
            if payload.get("exceededTransferLimit"):
                logger.warning(
                    "[%s] Provincia '%s' excede el límite de transferencia (1000) incluso tras filtrar; "
                    "se obtuvieron %d registros parciales.", dept_value, prov, len(records),
                )
            all_records.extend(records)
            logger.info("Centros Poblados INEI [%s]: provincia '%s' -> %d registros", dept_value, prov, len(records))

    if not all_records:
        raise AcquisitionError("Centros Poblados INEI (censo) no devolvió ningún registro")

    df = pd.DataFrame(all_records)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(dest_path, index=False, encoding="utf-8-sig")
    logger.info("Centros Poblados INEI (censo) guardado: %s (%d registros)", dest_path, len(df))
    return dest_path


def fetch_all(force: bool = False) -> dict[str, Any]:
    """Ejecuta las 4 adquisiciones de forma independiente y devuelve un resumen por fuente."""
    tasks = (
        ("renipress", lambda: fetch_renipress(force=force)),
        ("centros_poblados", lambda: fetch_centros_poblados(force=force)),
        ("limites_administrativos", lambda: fetch_limites_administrativos(force=force)),
        ("centros_poblados_inei_censo", lambda: fetch_centros_poblados_inei_censo(force=force)),
    )

    summary: dict[str, Any] = {}
    for name, fn in tasks:
        try:
            summary[name] = {"status": "ok", "result": fn()}
        except AcquisitionError as exc:
            summary[name] = {"status": "error", "error": str(exc)}
            logger.error("Fuente '%s' falló y fue omitida: %s", name, exc)

    ok = sum(1 for v in summary.values() if v["status"] == "ok")
    logger.info("Adquisición completa: %d/%d fuentes correctas", ok, len(tasks))
    return summary


if __name__ == "__main__":
    from pprint import pprint

    pprint(fetch_all())

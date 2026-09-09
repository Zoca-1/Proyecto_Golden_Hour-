"""Fase 1 — Validación y control de calidad de datos para Golden Hour.

Toma los datos crudos descargados por ``acquisition.py`` (RENIPRESS, Centros
Poblados y límites distritales), los filtra a los 3 departamentos
configurados en ``config.md`` y les aplica una batería de reglas de calidad:

    - coordenadas nulas, en cero o faltantes
    - coordenadas fuera del bounding box de Perú
    - latitud/longitud intercambiadas (con corrección automática)
    - puntos fuera de su polígono distrital declarado (validación espacial)
    - códigos de establecimiento duplicados (RENIPRESS)
    - problemas de codificación UTF-8 vs Latin-1
    - normalización de categorías resolutivas vs no resolutivas (RENIPRESS)

Cada regla queda documentada en ``logs/data_quality_report.csv`` (registros
afectados, acción tomada y justificación), y los datasets resultantes se
exportan como GeoParquet en ``data/processed/``.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

import utils

logger = utils.setup_logging("validation")

QUALITY_REPORT_COLUMNS = ["dataset", "regla", "registros_afectados", "accion", "justificacion"]


def _normalize_text(value: Any) -> str:
    """Quita tildes, recorta espacios y pasa a mayúsculas (para comparar nombres)."""
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return text.strip().upper()


def _read_csv_with_encoding_fallback(
    path: Path, delimiter: str, candidates: list[str]
) -> tuple[pd.DataFrame, str, bool]:
    """Intenta leer ``path`` probando cada encoding candidato en orden.

    Devuelve (dataframe, encoding_usado, se_usó_fallback). El primer
    candidato que decodifica sin error gana; se asume que la lista está
    ordenada de más a menos estricta (p.ej. utf-8-sig antes que latin-1,
    que nunca falla por diseño pero puede producir mojibake).
    """
    last_exc: Exception | None = None
    for i, encoding in enumerate(candidates):
        try:
            df = pd.read_csv(path, sep=delimiter, encoding=encoding, dtype=str, low_memory=False)
            return df, encoding, i > 0
        except (UnicodeDecodeError, UnicodeError) as exc:
            last_exc = exc
            logger.warning("Encoding '%s' falló para %s: %s", encoding, path, exc)
    raise ValueError(f"No se pudo leer {path} con ninguno de {candidates}") from last_exc


def _detect_and_fix_lat_lon_swap(
    lat_raw: pd.Series, lon_raw: pd.Series, bbox: dict[str, float]
) -> dict[str, pd.Series]:
    """Detecta filas donde NORTE/lat y ESTE/lon están intercambiados y las corrige.

    Perú cae en longitudes ~[-81.4,-68.6] y latitudes ~[-18.4,-0.04] — rangos
    disjuntos, así que si el valor de "latitud" cae en el rango de longitud
    (y viceversa), se puede inferir con confianza que están intercambiados.
    """
    lat_ok = lat_raw.between(bbox["lat_min"], bbox["lat_max"])
    lon_ok = lon_raw.between(bbox["lon_min"], bbox["lon_max"])
    already_valid = lat_ok & lon_ok

    looks_swapped = (
        ~already_valid
        & lat_raw.between(bbox["lon_min"], bbox["lon_max"])
        & lon_raw.between(bbox["lat_min"], bbox["lat_max"])
    )

    lat_fixed = lat_raw.where(~looks_swapped, lon_raw)
    lon_fixed = lon_raw.where(~looks_swapped, lat_raw)

    return {
        "lat": lat_fixed,
        "lon": lon_fixed,
        "swapped_mask": looks_swapped,
        "already_valid_mask": already_valid,
    }


def _flag_within_declared_polygon(
    points: gpd.GeoSeries, distritos: gpd.GeoDataFrame, declared_ubigeo: pd.Series
) -> pd.Series:
    """Para cada punto, ¿cae dentro del polígono del distrito que él mismo declara?

    Devuelve True / False / pd.NA (NA = el punto no cae dentro de NINGÚN
    polígono distrital, p.ej. coordenada mala o justo en el límite/costa).
    """
    lookup = distritos[["UBIGEO", "geometry"]].rename(columns={"UBIGEO": "_ubigeo_poligono"})
    points_gdf = gpd.GeoDataFrame({"geometry": points}, geometry="geometry", crs=distritos.crs)

    joined = gpd.sjoin(points_gdf, lookup, how="left", predicate="within")
    joined = joined[~joined.index.duplicated(keep="first")]
    espacial = joined["_ubigeo_poligono"].reindex(points_gdf.index)

    declarado = declared_ubigeo.astype(str).str.strip().str.zfill(6)
    result = pd.Series(pd.NA, index=points_gdf.index, dtype="object")
    has_match = espacial.notna()
    result[has_match] = declarado[has_match].values == espacial[has_match].astype(str).str.zfill(6).values
    return result


def load_distritos() -> gpd.GeoDataFrame:
    """Carga el shapefile nacional de distritos, filtrado a los 3 departamentos configurados."""
    cfg = utils.get_data_sources()["limites_administrativos"]["distritos"]
    raw_path = utils.get_paths()["data_raw"] / cfg["target_filename"]

    distritos = gpd.read_file(f"zip://{raw_path}")
    dept_names = {_normalize_text(n) for n in utils.get_department_names()}
    distritos["_dept_norm"] = distritos["DEPARTAMEN"].map(_normalize_text)
    distritos = distritos[distritos["_dept_norm"].isin(dept_names)].copy()

    geo_crs = utils.get_crs()["geographic"]
    if distritos.crs is None:
        distritos = distritos.set_crs(geo_crs)
    elif str(distritos.crs) != geo_crs:
        distritos = distritos.to_crs(geo_crs)

    logger.info("Distritos cargados para %s: %d polígonos", sorted(dept_names), len(distritos))
    return distritos


def validate_renipress(distritos: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, list[dict[str, Any]]]:
    """Aplica todas las reglas de calidad al dataset RENIPRESS y devuelve (gdf, filas_reporte)."""
    cfg = utils.get_data_sources()["renipress"]
    bbox = utils.get_peru_bbox()
    dept_names = {_normalize_text(n) for n in utils.get_department_names()}
    categories_whitelist = set(utils.get_health_categories())
    non_resolutive_levels = {"I-1", "I-2", "I-3", "I-4"}
    rows: list[dict[str, Any]] = []

    raw_path = utils.get_paths()["data_raw"] / cfg["target_filename"]
    df, encoding_used, used_fallback = _read_csv_with_encoding_fallback(
        raw_path, cfg["delimiter"], cfg["encoding_candidates"]
    )
    rows.append({
        "dataset": "renipress",
        "regla": "codificacion_utf8_vs_latin1",
        "registros_afectados": len(df) if used_fallback else 0,
        "accion": "corregido" if used_fallback else "conservado",
        "justificacion": (
            f"Se tuvo que re-leer el archivo con el encoding de respaldo '{encoding_used}' "
            "tras fallar el primer candidato."
            if used_fallback
            else f"Archivo leído sin degradación de caracteres con '{encoding_used}' "
                 "(BOM UTF-8 detectado en la fuente original)."
        ),
    })

    total_raw = len(df)
    df["_dept_norm"] = df[cfg["department_column"]].map(_normalize_text)
    df = df[df["_dept_norm"].isin(dept_names)].copy()
    logger.info("RENIPRESS filtrado a los 3 departamentos: %d/%d registros", len(df), total_raw)

    lat_raw = pd.to_numeric(df[cfg["lat_column"]], errors="coerce")
    lon_raw = pd.to_numeric(df[cfg["lon_column"]], errors="coerce")
    null_or_zero = lat_raw.isna() | lon_raw.isna() | (lat_raw == 0) | (lon_raw == 0)
    rows.append({
        "dataset": "renipress",
        "regla": "coordenadas_nulas_cero_o_faltantes",
        "registros_afectados": int(null_or_zero.sum()),
        "accion": "descartado" if null_or_zero.any() else "conservado",
        "justificacion": "Sin coordenadas utilizables para geocodificar/rutear; se excluyen del GeoParquet.",
    })
    df, lat_raw, lon_raw = df[~null_or_zero].copy(), lat_raw[~null_or_zero], lon_raw[~null_or_zero]

    swap = _detect_and_fix_lat_lon_swap(lat_raw, lon_raw, bbox)
    lat_fixed, lon_fixed, swapped_mask = swap["lat"], swap["lon"], swap["swapped_mask"]
    originally_out_of_bbox = (~swap["already_valid_mask"]).sum()
    recovery_rate = (swapped_mask.sum() / originally_out_of_bbox) if originally_out_of_bbox > 0 else 0.0
    rows.append({
        "dataset": "renipress",
        "regla": "latitud_longitud_intercambiada",
        "registros_afectados": int(swapped_mask.sum()),
        "accion": "corregido" if swapped_mask.any() else "conservado",
        "justificacion": (
            f"NORTE/ESTE tenían magnitudes propias de longitud/latitud invertidas; se intercambiaron "
            f"columnas. Tasa de recuperación sobre registros inicialmente fuera de rango: {recovery_rate:.1%} "
            f"({int(swapped_mask.sum())}/{int(originally_out_of_bbox)})."
        ),
    })

    out_of_bbox = ~(lat_fixed.between(bbox["lat_min"], bbox["lat_max"]) & lon_fixed.between(bbox["lon_min"], bbox["lon_max"]))
    rows.append({
        "dataset": "renipress",
        "regla": "coordenadas_fuera_de_bbox_peru",
        "registros_afectados": int(out_of_bbox.sum()),
        "accion": "descartado" if out_of_bbox.any() else "conservado",
        "justificacion": (
            f"Coordenadas fuera de lon [{bbox['lon_min']}, {bbox['lon_max']}] / "
            f"lat [{bbox['lat_min']}, {bbox['lat_max']}] incluso tras evaluar el intercambio lat/lon; "
            "no corregibles automáticamente."
        ),
    })
    df["latitud"], df["longitud"] = lat_fixed, lon_fixed
    df = df[~out_of_bbox].copy()

    dup_mask = df.duplicated(subset=[cfg["code_column"]], keep="first")
    rows.append({
        "dataset": "renipress",
        "regla": "codigos_establecimiento_duplicados",
        "registros_afectados": int(dup_mask.sum()),
        "accion": "descartado" if dup_mask.any() else "conservado",
        "justificacion": (
            f"Se conserva la primera ocurrencia de cada '{cfg['code_column']}'; duplicados posteriores "
            "se descartan para evitar doble conteo de establecimientos."
            if dup_mask.any()
            else f"No se encontraron valores duplicados de '{cfg['code_column']}' en el subconjunto filtrado."
        ),
    })
    df = df[~dup_mask].copy()

    categoria_norm = df[cfg["category_column"]].fillna("").astype(str).str.strip().str.upper()
    reconocida = categoria_norm.isin(categories_whitelist | non_resolutive_levels)
    df["categoria_normalizada"] = categoria_norm
    df["es_resolutivo"] = categoria_norm.isin(categories_whitelist)
    rows.append({
        "dataset": "renipress",
        "regla": "normalizacion_categoria_resolutivo",
        "registros_afectados": int((~reconocida).sum()),
        "accion": "conservado",
        "justificacion": (
            f"CATEGORIA normalizada (trim + mayúsculas) y clasificada como resolutiva "
            f"({sorted(categories_whitelist)}) vs no resolutiva ({sorted(non_resolutive_levels)}). "
            f"{int((~reconocida).sum())} registros no tienen una categoría reconocida "
            "(p.ej. '0' = sin categorizar en RENIPRESS) y se conservan con es_resolutivo=False."
        ),
    })

    geometry = [Point(lon, lat) for lon, lat in zip(df["longitud"], df["latitud"])]
    gdf = gpd.GeoDataFrame(
        df.drop(columns=["_dept_norm"]), geometry=geometry, crs=utils.get_crs()["geographic"]
    )

    dentro = _flag_within_declared_polygon(gdf.geometry, distritos, gdf[cfg["ubigeo_column"]])
    gdf["dentro_poligono_declarado"] = dentro
    fuera_mask = dentro == False  # noqa: E712 (pd.NA != False, distinción intencional)
    sin_match_mask = dentro.isna()
    rows.append({
        "dataset": "renipress",
        "regla": "punto_fuera_de_poligono_distrital_declarado",
        "registros_afectados": int(fuera_mask.sum()),
        "accion": "conservado",
        "justificacion": (
            "El punto no cae dentro del polígono del distrito (UBIGEO) que el propio registro declara; "
            "se conserva marcado en 'dentro_poligono_declarado=False' para revisión manual, ya que el "
            f"error puede estar en la coordenada o en el UBIGEO. Adicionalmente {int(sin_match_mask.sum())} "
            "registros tienen un punto que no cae dentro de ningún polígono distrital del shapefile "
            "(coordenada inválida o fuera de cobertura)."
        ),
    })

    logger.info("RENIPRESS validado: %d registros finales", len(gdf))
    return gdf, rows


def _load_centros_poblados_inei_censo(geo_crs: str) -> gpd.GeoDataFrame:
    """Carga Centros Poblados INEI (censo 1999/2002, vía ArcGIS REST de INGEMMET/GEOCATMIN).

    Fuente única de puntos de demanda (ver ``config.md ->
    data_sources.centros_poblados_inei_censo``): a diferencia del dataset IGN
    (``centros_poblados``), trae población real (``TOT_POB99``) y
    clasificación urbano/rural oficial (``CLASIF02``) de forma consistente en
    los 3 departamentos, y no tiene el defecto de coordenadas que invalida el
    100% de los registros de Loreto en la fuente IGN.
    """
    cfg = utils.get_data_sources()["centros_poblados_inei_censo"]
    raw_path = utils.get_paths()["data_raw"] / cfg["target_filename"]
    raw = pd.read_csv(raw_path, encoding="utf-8-sig", dtype=str)

    harmonized = pd.DataFrame({
        "NOM_POBLAD": raw["NOMCCPP02"],
        "FUENTE": "INEI (censo 1999/2002, vía INGEMMET/GEOCATMIN)",
        "CÓDIGO": raw["CODCCPP02"],
        "DIST": raw["NOMBDI02"],
        "PROV": raw["NOMBPV02"],
        "DEP": raw["NOMBDD02"],
        "CÓD_INT": raw["CCDI02"],
        "CATEGORIA": raw["NOMCAT02"],
        "X": pd.to_numeric(raw["X_COORD"], errors="coerce"),
        "Y": pd.to_numeric(raw["Y_COORD"], errors="coerce"),
        "N_BUSQDA": raw["NOMCCPP02"],
        "poblacion_1999": pd.to_numeric(raw["TOT_POB99"], errors="coerce"),
        "clasificacion_urbano_rural": raw["CLASIF02"],
    })
    geometry = [Point(x, y) for x, y in zip(harmonized["X"], harmonized["Y"])]
    return gpd.GeoDataFrame(harmonized, geometry=geometry, crs=geo_crs)


def validate_centros_poblados(distritos: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, list[dict[str, Any]]]:
    """Aplica las reglas de calidad geográficas al dataset de Centros Poblados (INEI censo)."""
    bbox = utils.get_peru_bbox()
    dept_names = {_normalize_text(n) for n in utils.get_department_names()}
    rows: list[dict[str, Any]] = []

    geo_crs = utils.get_crs()["geographic"]
    gdf = _load_centros_poblados_inei_censo(geo_crs)

    total_raw = len(gdf)
    gdf["_dept_norm"] = gdf["DEP"].map(_normalize_text)
    gdf = gdf[gdf["_dept_norm"].isin(dept_names)].copy()
    logger.info("Centros Poblados filtrado a los 3 departamentos: %d/%d registros", len(gdf), total_raw)

    lat_raw = pd.to_numeric(gdf["Y"], errors="coerce")
    lon_raw = pd.to_numeric(gdf["X"], errors="coerce")
    null_or_zero = lat_raw.isna() | lon_raw.isna() | (lat_raw == 0) | (lon_raw == 0)
    rows.append({
        "dataset": "centros_poblados",
        "regla": "coordenadas_nulas_cero_o_faltantes",
        "registros_afectados": int(null_or_zero.sum()),
        "accion": "descartado" if null_or_zero.any() else "conservado",
        "justificacion": "Sin coordenadas utilizables como destino/origen de ruteo; se excluyen del GeoParquet.",
    })
    gdf, lat_raw, lon_raw = gdf[~null_or_zero].copy(), lat_raw[~null_or_zero], lon_raw[~null_or_zero]

    swap = _detect_and_fix_lat_lon_swap(lat_raw, lon_raw, bbox)
    lat_fixed, lon_fixed, swapped_mask = swap["lat"], swap["lon"], swap["swapped_mask"]
    originally_out_of_bbox = (~swap["already_valid_mask"]).sum()
    recovery_rate = (swapped_mask.sum() / originally_out_of_bbox) if originally_out_of_bbox > 0 else 0.0
    rows.append({
        "dataset": "centros_poblados",
        "regla": "latitud_longitud_intercambiada",
        "registros_afectados": int(swapped_mask.sum()),
        "accion": "corregido" if swapped_mask.any() else "conservado",
        "justificacion": (
            f"Columnas X/Y con magnitudes de longitud/latitud invertidas; se intercambiaron. "
            f"Tasa de recuperación sobre registros inicialmente fuera de rango: {recovery_rate:.1%} "
            f"({int(swapped_mask.sum())}/{int(originally_out_of_bbox)})."
        ),
    })

    out_of_bbox = ~(lat_fixed.between(bbox["lat_min"], bbox["lat_max"]) & lon_fixed.between(bbox["lon_min"], bbox["lon_max"]))
    rows.append({
        "dataset": "centros_poblados",
        "regla": "coordenadas_fuera_de_bbox_peru",
        "registros_afectados": int(out_of_bbox.sum()),
        "accion": "descartado" if out_of_bbox.any() else "conservado",
        "justificacion": (
            f"Coordenadas fuera de lon [{bbox['lon_min']}, {bbox['lon_max']}] / "
            f"lat [{bbox['lat_min']}, {bbox['lat_max']}] incluso tras evaluar el intercambio lat/lon."
        ),
    })
    gdf["latitud"], gdf["longitud"] = lat_fixed, lon_fixed
    gdf = gdf[~out_of_bbox].copy()
    gdf["geometry"] = [Point(lon, lat) for lon, lat in zip(gdf["longitud"], gdf["latitud"])]
    gdf = gdf.set_geometry("geometry").set_crs(geo_crs, allow_override=True)

    # Centros Poblados no trae UBIGEO; el distrito declarado se matchea por
    # nombre normalizado de distrito+provincia+departamento contra el shapefile del IGN.
    distritos_lookup = distritos.copy()
    distritos_lookup["_key"] = (
        distritos_lookup["DISTRITO"].map(_normalize_text) + "|"
        + distritos_lookup["PROVINCIA"].map(_normalize_text) + "|"
        + distritos_lookup["_dept_norm"]
    )
    gdf["_key"] = (
        gdf["DIST"].map(_normalize_text) + "|" + gdf["PROV"].map(_normalize_text) + "|" + gdf["_dept_norm"]
    )
    key_to_ubigeo = dict(zip(distritos_lookup["_key"], distritos_lookup["UBIGEO"]))
    ubigeo_declarado = gdf["_key"].map(key_to_ubigeo)

    sin_distrito_match_mask = ubigeo_declarado.isna()
    dentro = pd.Series(pd.NA, index=gdf.index, dtype="object")
    if (~sin_distrito_match_mask).any():
        dentro.loc[~sin_distrito_match_mask] = _flag_within_declared_polygon(
            gdf.loc[~sin_distrito_match_mask].geometry,
            distritos,
            ubigeo_declarado.loc[~sin_distrito_match_mask],
        )
    gdf["dentro_poligono_declarado"] = dentro
    fuera_mask = dentro == False  # noqa: E712
    sin_geom_match_mask = dentro.isna() & ~sin_distrito_match_mask
    rows.append({
        "dataset": "centros_poblados",
        "regla": "punto_fuera_de_poligono_distrital_declarado",
        "registros_afectados": int(fuera_mask.sum()),
        "accion": "conservado",
        "justificacion": (
            "El punto no cae dentro del polígono del distrito (nombre DIST/PROV/DEP) que el propio "
            "registro declara; se conserva marcado en 'dentro_poligono_declarado=False' para revisión "
            f"manual. {int(sin_distrito_match_mask.sum())} registros no encontraron un distrito homónimo "
            f"en el shapefile del IGN (posible diferencia de nomenclatura), y {int(sin_geom_match_mask.sum())} "
            "cayeron fuera de todo polígono distrital pese a resolverse el nombre."
        ),
    })

    gdf = gdf.drop(columns=["_dept_norm", "_key"])
    logger.info("Centros Poblados validado: %d registros finales", len(gdf))
    return gdf, rows


def write_quality_report(rows: list[dict[str, Any]], dest_path: Path) -> Path:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    report_df = pd.DataFrame(rows, columns=QUALITY_REPORT_COLUMNS)
    report_df.to_csv(dest_path, index=False, encoding="utf-8-sig")
    logger.info("Reporte de calidad escrito: %s (%d reglas registradas)", dest_path, len(report_df))
    return dest_path


def export_geoparquet(gdf: gpd.GeoDataFrame, dest_path: Path) -> Path:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(dest_path, index=False)
    logger.info("GeoParquet exportado: %s (%d registros)", dest_path, len(gdf))
    return dest_path


def run_all() -> dict[str, Any]:
    """Ejecuta la validación completa: RENIPRESS + Centros Poblados -> GeoParquet + reporte."""
    paths = utils.get_paths()
    distritos = load_distritos()

    renipress_gdf, renipress_rows = validate_renipress(distritos)
    ccpp_gdf, ccpp_rows = validate_centros_poblados(distritos)

    renipress_out = export_geoparquet(renipress_gdf, paths["data_processed"] / "renipress_validado.parquet")
    ccpp_out = export_geoparquet(ccpp_gdf, paths["data_processed"] / "centros_poblados_validado.parquet")

    report_path = write_quality_report(renipress_rows + ccpp_rows, paths["logs"] / "data_quality_report.csv")

    summary = {
        "renipress": {"registros_finales": len(renipress_gdf), "geoparquet": renipress_out},
        "centros_poblados": {"registros_finales": len(ccpp_gdf), "geoparquet": ccpp_out},
        "data_quality_report": report_path,
    }
    logger.info("Validación completa: %s", summary)
    return summary


if __name__ == "__main__":
    from pprint import pprint

    pprint(run_all())

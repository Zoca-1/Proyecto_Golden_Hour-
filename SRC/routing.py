"""Fase 2 — Ruteo y cálculo de tiempos de traslado para Golden Hour.

Lee los datos validados en ``data/processed/`` (centros poblados = demanda,
RENIPRESS = establecimientos), arma una matriz Origen-Destino de demanda x
establecimientos RESOLUTIVOS, y enriquece cada punto de demanda con el
establecimiento resolutivo más cercano en auto/caminata/bicicleta (y, para
puntos urbanos, el establecimiento de cualquier tipo más cercano a pie).

Motor de ruteo: se intenta OSRM Docker (``routing.osrm.host`` en config.md)
únicamente para el perfil 'car' (el contenedor solo expone un perfil,
'driving'); si no está disponible, o para foot/bike (que ese OSRM no puede
rutear), se usa un fallback automático a OSMnx/NetworkX. Cuando un punto no
tiene snap válido a la red o cae en una componente desconectada, se estima
su distancia/tiempo con Haversine × ``detour_factor`` (ver
``routing_execution`` en config.md).

Caching estricto: si ``routing_matrix.parquet`` y ``demand_routed.parquet``
ya existen en ``data/processed/``, ``run_all()`` no vuelve a calcular rutas.
"""

from __future__ import annotations

import time
import unicodedata
from pathlib import Path
from typing import Any

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
import requests

import utils

logger = utils.setup_logging("routing")

# Por defecto, OSMnx cachea las respuestas HTTP de Overpass en "./cache"
# (relativo al cwd del proceso, no al proyecto) — frágil si el módulo se
# ejecuta desde otro directorio. Se fija explícitamente junto al resto de
# datos crudos/caché del proyecto.
ox.settings.cache_folder = str(utils.get_paths()["data_raw"] / "osmnx_http_cache")

EARTH_RADIUS_KM = 6371.0088


class RoutingError(RuntimeError):
    """Error irrecuperable al construir un grafo o calcular una matriz de ruteo."""


def _normalize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return text.strip().upper()


def _haversine_km(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(v, dtype=float)) for v in (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


# ---------------------------------------------------------------------------
# 1. Ingesta y muestreo estratificado de la demanda
# ---------------------------------------------------------------------------

def load_processed_datasets() -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Carga centros poblados (demanda) y RENIPRESS (establecimientos) desde data/processed/."""
    paths = utils.get_paths()
    demand = gpd.read_parquet(paths["data_processed"] / "centros_poblados_validado.parquet")
    facilities = gpd.read_parquet(paths["data_processed"] / "renipress_validado.parquet")
    return demand, facilities


def _population_weight(row: pd.Series) -> float:
    """Peso poblacional para el muestreo: población real (censo INEI 1999/2002, los 3
    departamentos, ver validation.py::_load_centros_poblados_inei_censo).

    Se usa log(1 + población) en vez de la población cruda: la población real tiene
    varios órdenes de magnitud de rango (decenas a miles), y un peso sin amortiguar
    hace que unos pocos asentamientos grandes dominen por completo el muestreo
    ponderado sin reemplazo (e incluso lo vuelve numéricamente inviable en pandas).
    """
    pob = row.get("poblacion_1999")
    return float(np.log1p(max(float(pob), 0.0))) + 1.0 if pd.notna(pob) else 1.0


def sample_demand_points(demand: gpd.GeoDataFrame, max_points: int, seed: int) -> gpd.GeoDataFrame:
    """100% de los puntos si total <= max_points; si no, muestreo estratificado por distrito
    ponderado por población real (censo INEI). Documentado en logs."""
    total = len(demand)
    if total <= max_points:
        logger.info("Puntos de demanda (%d) <= %d: se usa el 100%% sin muestreo.", total, max_points)
        return demand.copy()

    logger.warning(
        "Puntos de demanda (%d) > %d: aplicando muestreo ESTRATIFICADO POR DISTRITO, ponderado por "
        "población real (censo INEI 1999/2002). Procedimiento: fracción global = max_points/total; "
        "dentro de cada distrito se muestrea esa fracción del grupo con probabilidad proporcional al "
        "peso poblacional; si el redondeo global excede max_points, se recorta con un muestreo final ponderado.",
        total, max_points,
    )
    df = demand.copy()
    df["_peso"] = df.apply(_population_weight, axis=1)
    df["_distrito_key"] = df["DEP"].astype(str) + "|" + df["PROV"].astype(str) + "|" + df["DIST"].astype(str)

    def _safe_weighted_sample(group: pd.DataFrame, n: int) -> pd.DataFrame:
        """Muestreo ponderado sin reemplazo; si los pesos son numéricamente inviables
        (un peso domina demasiado el grupo para el algoritmo de pandas), cae a uniforme."""
        n = min(len(group), n)
        try:
            return group.sample(n=n, weights=group["_peso"], random_state=seed)
        except ValueError:
            return group.sample(n=n, random_state=seed)

    frac = max_points / total
    parts = [
        _safe_weighted_sample(group, max(1, round(len(group) * frac)))
        for _, group in df.groupby("_distrito_key", sort=False)
    ]
    sampled = pd.concat(parts)
    if len(sampled) > max_points:
        try:
            sampled = sampled.sample(n=max_points, weights=sampled["_peso"], random_state=seed)
        except ValueError:
            logger.warning("Muestreo ponderado en el recorte final no es numéricamente viable; se usa muestreo uniforme.")
            sampled = sampled.sample(n=max_points, random_state=seed)

    sampled = sampled.drop(columns=["_peso", "_distrito_key"])
    logger.info(
        "Muestreo estratificado completado: %d/%d puntos de demanda seleccionados (%.1f%%).",
        len(sampled), total, 100 * len(sampled) / total,
    )
    return gpd.GeoDataFrame(sampled, geometry="geometry", crs=demand.crs)


def _flag_urban(demand: gpd.GeoDataFrame) -> pd.Series:
    """True si el punto es urbano, según CLASIF02 (clasificación oficial INEI)."""
    return demand["clasificacion_urbano_rural"].astype(str).str.upper().eq("URBANO")


# ---------------------------------------------------------------------------
# 2. Motor de ruteo: disponibilidad de OSRM, grafos OSMnx y snapping
# ---------------------------------------------------------------------------

def _osrm_available(host: str, timeout: int) -> bool:
    try:
        resp = requests.get(f"{host}/nearest/v1/driving/-77.03,-12.05", timeout=timeout)
        return resp.status_code == 200
    except requests.RequestException as exc:
        logger.info("OSRM no disponible en %s (%s).", host, exc)
        return False


def _bbox_for_points(geometries: gpd.GeoSeries, buffer_deg: float) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = geometries.total_bounds
    return (minx - buffer_deg, miny - buffer_deg, maxx + buffer_deg, maxy + buffer_deg)


def _load_or_build_graph(
    key: str, bbox: tuple[float, float, float, float], network_type: str, cache_dir: Path, max_bbox_area_deg2: float,
) -> nx.MultiDiGraph:
    """Carga el grafo vial desde caché en disco, o lo descarga de OSM/Overpass y lo cachea.

    La clave de caché incluye una firma del bbox (redondeado a 3 decimales, ~110m) para
    que dos consultas con el mismo ``key`` pero áreas distintas nunca colisionen y
    reutilicen por error un grafo que no cubre los puntos solicitados.
    """
    minx, miny, maxx, maxy = bbox
    bbox_area = (maxx - minx) * (maxy - miny)
    bbox_signature = "_".join(f"{v:.3f}" for v in bbox)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{key}_{bbox_signature}.graphml"

    if cache_path.exists():
        logger.info("Grafo '%s' cargado desde caché: %s", key, cache_path)
        return ox.load_graphml(cache_path)

    if bbox_area > max_bbox_area_deg2:
        raise RoutingError(
            f"El área del bbox ({bbox_area:.2f} grados² > umbral {max_bbox_area_deg2}) es "
            "impráctica para descargar vía la API pública de Overpass (generaría cientos de "
            "sub-consultas y probablemente agotaría el tiempo de espera); este es exactamente "
            "el caso de uso para el que OSRM (extractos OSM locales, sin este límite) está "
            "documentado como motor primario. Con OSRM no disponible, se usa el fallback "
            "Haversine × detour_factor directamente para esta área."
        )

    logger.info("Descargando grafo '%s' (network_type=%s, bbox=%s, área=%.2f°²) desde OSM/Overpass...", key, network_type, bbox, bbox_area)
    t0 = time.time()
    try:
        G = ox.graph_from_bbox(bbox=bbox, network_type=network_type, simplify=True)
    except Exception as exc:  # Overpass puede fallar de muchas formas (timeout, sin datos, etc.)
        raise RoutingError(f"No se pudo construir el grafo '{key}': {exc}") from exc

    logger.info(
        "Grafo '%s' descargado en %.1fs: %d nodos, %d aristas",
        key, time.time() - t0, G.number_of_nodes(), G.number_of_edges(),
    )
    ox.save_graphml(G, cache_path)
    return G


def _snap_points(G: nx.MultiDiGraph, points: gpd.GeoDataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Snapea cada punto al nodo vial más cercano; devuelve (node_ids, distancia_snap_km)."""
    xs, ys = points.geometry.x.to_numpy(), points.geometry.y.to_numpy()
    nodes = np.atleast_1d(ox.distance.nearest_nodes(G, X=xs, Y=ys))
    node_xs = np.array([G.nodes[n]["x"] for n in nodes])
    node_ys = np.array([G.nodes[n]["y"] for n in nodes])
    snap_km = _haversine_km(ys, xs, node_ys, node_xs)
    return nodes, snap_km


def _log_snap_quality(label: str, snap_km: np.ndarray, max_snap_km: float) -> None:
    n_unsnappable = int((snap_km > max_snap_km).sum())
    logger.info(
        "Snapping [%s]: distancia promedio de ajuste = %.3f km | no-snappables (> %.1f km): %d/%d",
        label, float(snap_km.mean()) if len(snap_km) else 0.0, max_snap_km, n_unsnappable, len(snap_km),
    )


# ---------------------------------------------------------------------------
# 3. Cálculo de matrices: OSRM (car) y OSMnx/NetworkX (fallback, todos los modos)
# ---------------------------------------------------------------------------

def _empty_unroutable_matrix(demand: gpd.GeoDataFrame, targets: gpd.GeoDataFrame, target_id_col: str) -> pd.DataFrame:
    target_ids = targets[target_id_col].to_numpy()
    rows = [
        {"demand_id": demand_id, "facility_id": tid, "distance_km": None, "duration_min": None, "is_unroutable": True}
        for demand_id in demand.index
        for tid in target_ids
    ]
    return pd.DataFrame(rows, columns=["demand_id", "facility_id", "distance_km", "duration_min", "is_unroutable"])


def _matrix_via_osmnx(
    G: nx.MultiDiGraph,
    demand: gpd.GeoDataFrame,
    targets: gpd.GeoDataFrame,
    target_id_col: str,
    mode_speed_kmh: float | None,
    max_snap_km: float,
    log_prefix: str,
) -> pd.DataFrame:
    """Matriz completa demanda x targets vía Dijkstra de una sola fuente por origen (eficiente
    para one-to-many). Si mode_speed_kmh es None, usa el atributo 'travel_time' del grafo (car,
    velocidades reales de OSM); si no, deriva el tiempo de la distancia de la ruta más corta y la
    velocidad asumida del modo (foot/bike, sin tags maxspeed en OSM)."""
    if len(demand) == 0 or len(targets) == 0:
        return _empty_unroutable_matrix(demand, targets, target_id_col)

    demand_nodes, demand_snap_km = _snap_points(G, demand)
    target_nodes, target_snap_km = _snap_points(G, targets)
    _log_snap_quality(log_prefix, np.concatenate([demand_snap_km, target_snap_km]), max_snap_km)

    target_ids = targets[target_id_col].to_numpy()
    time_weight = "length" if mode_speed_kmh is not None else "travel_time"

    n = len(demand)
    log_every = max(1, n // 10)
    t0 = time.time()
    rows: list[dict[str, Any]] = []

    for pos, (demand_id, origin_node, o_snap) in enumerate(zip(demand.index, demand_nodes, demand_snap_km)):
        if o_snap > max_snap_km or origin_node not in G:
            rows.extend(
                {"demand_id": demand_id, "facility_id": tid, "distance_km": None, "duration_min": None, "is_unroutable": True}
                for tid in target_ids
            )
        else:
            try:
                weighted_lengths = nx.single_source_dijkstra_path_length(G, origin_node, weight=time_weight)
            except Exception:
                weighted_lengths = {}
            try:
                dist_lengths = weighted_lengths if time_weight == "length" else nx.single_source_dijkstra_path_length(
                    G, origin_node, weight="length"
                )
            except Exception:
                dist_lengths = {}

            for tid, tnode, t_snap in zip(target_ids, target_nodes, target_snap_km):
                reachable = t_snap <= max_snap_km and tnode in dist_lengths and (
                    time_weight == "length" or tnode in weighted_lengths
                )
                if not reachable:
                    rows.append({"demand_id": demand_id, "facility_id": tid, "distance_km": None, "duration_min": None, "is_unroutable": True})
                    continue
                dist_km = dist_lengths[tnode] / 1000
                duration_min = (dist_km / mode_speed_kmh) * 60 if mode_speed_kmh is not None else weighted_lengths[tnode] / 60
                rows.append({"demand_id": demand_id, "facility_id": tid, "distance_km": dist_km, "duration_min": duration_min, "is_unroutable": False})

        if (pos + 1) % log_every == 0 or (pos + 1) == n:
            logger.info(
                "%s progreso: %d/%d orígenes procesados (%.1fs transcurridos)",
                log_prefix, pos + 1, n, time.time() - t0,
            )

    return pd.DataFrame(rows)


def _car_matrix_via_osrm(host: str, demand: gpd.GeoDataFrame, targets: gpd.GeoDataFrame, target_id_col: str, timeout: int) -> pd.DataFrame | None:
    """Matriz completa vía OSRM Table API, en chunks de origen. Devuelve None si falla (fallback OSMnx)."""
    try:
        dest_coords = [f"{lon},{lat}" for lon, lat in zip(targets.geometry.x, targets.geometry.y)]
        target_ids = targets[target_id_col].to_numpy()
        n_dest = len(dest_coords)
        demand_ids = list(demand.index)
        chunk_size = 100
        results: list[dict[str, Any]] = []
        t0 = time.time()

        for start in range(0, len(demand_ids), chunk_size):
            chunk_ids = demand_ids[start : start + chunk_size]
            chunk = demand.loc[chunk_ids]
            origin_coords = [f"{lon},{lat}" for lon, lat in zip(chunk.geometry.x, chunk.geometry.y)]
            n_orig = len(origin_coords)
            coords = ";".join(origin_coords + dest_coords)
            sources = ";".join(str(i) for i in range(n_orig))
            destinations = ";".join(str(n_orig + i) for i in range(n_dest))

            resp = requests.get(
                f"{host}/table/v1/driving/{coords}",
                params={"sources": sources, "destinations": destinations, "annotations": "distance,duration"},
                timeout=timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("code") != "Ok":
                raise RoutingError(f"OSRM table respondió code={payload.get('code')}")

            durations, distances = payload["durations"], payload["distances"]
            for i, demand_id in enumerate(chunk_ids):
                for j, facility_id in enumerate(target_ids):
                    dur, dist = durations[i][j], distances[i][j]
                    results.append({
                        "demand_id": demand_id, "facility_id": facility_id,
                        "distance_km": None if dist is None else dist / 1000,
                        "duration_min": None if dur is None else dur / 60,
                        "is_unroutable": dur is None,
                    })
            logger.info(
                "OSRM table progreso: %d/%d orígenes procesados (%.1fs transcurridos)",
                min(start + chunk_size, len(demand_ids)), len(demand_ids), time.time() - t0,
            )

        return pd.DataFrame(results)
    except (requests.RequestException, RoutingError, KeyError, ValueError) as exc:
        logger.warning("OSRM table falló (%s); fallback a OSMnx/NetworkX para este departamento.", exc)
        return None


def _apply_haversine_fallback(
    df: pd.DataFrame, demand: gpd.GeoDataFrame, targets: gpd.GeoDataFrame, target_id_col: str,
    speed_kmh: float, detour_factor: float,
) -> pd.DataFrame:
    """Para filas is_unroutable=True: estima distancia/tiempo con Haversine × detour_factor."""
    df = df.copy()
    mask = df["is_unroutable"].to_numpy(dtype=bool)
    if not mask.any():
        return df

    sub = df.loc[mask, ["demand_id", "facility_id"]]
    origin_geom = demand.geometry.loc[sub["demand_id"]]
    target_geom = targets.set_index(target_id_col).geometry.loc[sub["facility_id"]]

    hav_km = _haversine_km(
        origin_geom.y.to_numpy(), origin_geom.x.to_numpy(), target_geom.y.to_numpy(), target_geom.x.to_numpy()
    )
    est_km = hav_km * detour_factor
    est_min = est_km / speed_kmh * 60

    df.loc[mask, "distance_km"] = est_km
    df.loc[mask, "duration_min"] = est_min
    return df


def _nearest_per_demand(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """Reduce una matriz larga (demand_id, facility_id, distance_km, duration_min, is_unroutable)
    al establecimiento más cercano (menor duration_min) por demand_id."""
    if df.empty:
        return pd.DataFrame(columns=[f"{prefix}facility_id", f"{prefix}distance_km", f"{prefix}duration_min", f"{prefix}is_unroutable"])
    idx = df.groupby("demand_id")["duration_min"].idxmin()
    nearest = df.loc[idx].set_index("demand_id")
    nearest = nearest.rename(columns={
        "facility_id": f"{prefix}facility_id", "distance_km": f"{prefix}distance_km",
        "duration_min": f"{prefix}duration_min", "is_unroutable": f"{prefix}is_unroutable",
    })
    return nearest[[f"{prefix}facility_id", f"{prefix}distance_km", f"{prefix}duration_min", f"{prefix}is_unroutable"]]


# ---------------------------------------------------------------------------
# 4. Orquestación por departamento (no se rutea entre departamentos: son
#    regiones no contiguas, fuera del alcance de un único grafo vial)
# ---------------------------------------------------------------------------

def _process_department(
    dept: str,
    demand_dept: gpd.GeoDataFrame,
    resolutive_dept: gpd.GeoDataFrame,
    all_facilities_dept: gpd.GeoDataFrame,
    cfg: dict[str, Any],
    osrm_host: str | None,
) -> tuple[list[dict[str, Any]], dict[Any, dict[str, Any]]]:
    cache_dir = utils.PROJECT_ROOT / cfg["osmnx_graph_cache_dir"]
    buffer_deg = cfg["graph_bbox_buffer_deg"]
    max_snap_km = cfg["max_snap_distance_km"]
    detour_factor = cfg["detour_factor"]
    fallback_speeds = cfg["fallback_speeds_kmh"]
    mode_speeds = cfg["osmnx_mode_speeds_kmh"]
    max_bbox_area = cfg["max_graph_bbox_area_deg2"]
    dept_key = _normalize_text(dept).replace(" ", "_")

    bbox = _bbox_for_points(pd.concat([demand_dept.geometry, all_facilities_dept.geometry]), buffer_deg)
    demand_rows: dict[Any, dict[str, Any]] = {demand_id: {"demand_id": demand_id} for demand_id in demand_dept.index}

    def _merge_nearest(nearest_df: pd.DataFrame) -> None:
        for demand_id, row in nearest_df.iterrows():
            demand_rows.setdefault(demand_id, {"demand_id": demand_id}).update(row.to_dict())

    # ---- CAR: OSRM primero (único perfil configurado), OSMnx 'drive' como fallback ----
    car_df = _car_matrix_via_osrm(osrm_host, demand_dept, resolutive_dept, "COD_IPRESS", cfg["osrm_ping_timeout_seconds"] * 10) if osrm_host else None
    if car_df is None:
        try:
            drive_graph = _load_or_build_graph(f"{dept_key}_drive", bbox, "drive", cache_dir, max_bbox_area)
            drive_graph = ox.routing.add_edge_speeds(drive_graph)
            drive_graph = ox.routing.add_edge_travel_times(drive_graph)
            car_df = _matrix_via_osmnx(drive_graph, demand_dept, resolutive_dept, "COD_IPRESS", None, max_snap_km, f"[{dept}/car]")
        except RoutingError as exc:
            logger.error("Grafo 'drive' no disponible para %s (%s); modo car cae 100%% a Haversine.", dept, exc)
            car_df = _empty_unroutable_matrix(demand_dept, resolutive_dept, "COD_IPRESS")

    car_df = _apply_haversine_fallback(car_df, demand_dept, resolutive_dept, "COD_IPRESS", fallback_speeds["car"], detour_factor)
    car_df["mode"] = "car"
    matrix_rows = car_df.drop(columns=["mode"]).assign(mode="car").to_dict("records")
    _merge_nearest(_nearest_per_demand(car_df, "car_"))

    # ---- FOOT & BIKE: comparten el grafo 'walk' (incluye sendas + vías carrozables) ----
    try:
        walk_graph = _load_or_build_graph(f"{dept_key}_walk", bbox, "walk", cache_dir, max_bbox_area)
    except RoutingError as exc:
        logger.error("Grafo 'walk' no disponible para %s (%s); foot/bike caen 100%% a Haversine.", dept, exc)
        walk_graph = None

    for mode in ("foot", "bike"):
        if walk_graph is not None:
            mode_df = _matrix_via_osmnx(walk_graph, demand_dept, resolutive_dept, "COD_IPRESS", mode_speeds[mode], max_snap_km, f"[{dept}/{mode}]")
        else:
            mode_df = _empty_unroutable_matrix(demand_dept, resolutive_dept, "COD_IPRESS")
        mode_df = _apply_haversine_fallback(mode_df, demand_dept, resolutive_dept, "COD_IPRESS", fallback_speeds[mode], detour_factor)
        _merge_nearest(_nearest_per_demand(mode_df, f"{mode}_"))

    # ---- Puntos urbanos: a pie a CUALQUIER establecimiento (resolutivo o no) ----
    urban_demand = demand_dept[demand_dept["es_urbano"]]
    if len(urban_demand) > 0:
        if walk_graph is not None:
            urban_df = _matrix_via_osmnx(walk_graph, urban_demand, all_facilities_dept, "COD_IPRESS", mode_speeds["foot"], max_snap_km, f"[{dept}/urbano_foot]")
        else:
            urban_df = _empty_unroutable_matrix(urban_demand, all_facilities_dept, "COD_IPRESS")
        urban_df = _apply_haversine_fallback(urban_df, urban_demand, all_facilities_dept, "COD_IPRESS", fallback_speeds["foot"], detour_factor)
        _merge_nearest(_nearest_per_demand(urban_df, "urbano_cualquier_foot_"))

    return matrix_rows, demand_rows


# ---------------------------------------------------------------------------
# 5. Orquestación general + exportación
# ---------------------------------------------------------------------------

def run_all(max_demand_points: int | None = None) -> dict[str, Any]:
    """Ejecuta la Fase 2 completa. ``max_demand_points`` sobreescribe el techo de
    config.md únicamente para esta corrida (útil para pruebas a menor escala)."""
    t_start = time.time()
    paths = utils.get_paths()
    cfg = utils.get_routing_execution_config()
    if max_demand_points is not None:
        cfg = {**cfg, "max_demand_points": max_demand_points}

    matrix_path = paths["data_processed"] / "routing_matrix.parquet"
    demand_routed_path = paths["data_processed"] / "demand_routed.parquet"

    if matrix_path.exists() and demand_routed_path.exists():
        logger.info(
            "Caching estricto: '%s' y '%s' ya existen; no se recalculan rutas (bórralos para forzar).",
            matrix_path.name, demand_routed_path.name,
        )
        return {"status": "cached", "routing_matrix": matrix_path, "demand_routed": demand_routed_path}

    demand, facilities = load_processed_datasets()
    resolutive = facilities[facilities["es_resolutivo"]].copy()
    logger.info("[Fase 2] Establecimientos resolutivos: %d / %d totales", len(resolutive), len(facilities))

    demand_sample = sample_demand_points(demand, cfg["max_demand_points"], cfg["random_seed"])
    demand_sample["es_urbano"] = _flag_urban(demand_sample)
    logger.info(
        "[Fase 2] Puntos de demanda a rutear: %d (urbanos: %d, %.1f%%)",
        len(demand_sample), int(demand_sample["es_urbano"].sum()),
        100 * demand_sample["es_urbano"].mean() if len(demand_sample) else 0.0,
    )

    osrm_full_host = utils.load_config()["routing"]["osrm"]["host"]
    osrm_available = _osrm_available(osrm_full_host, cfg["osrm_ping_timeout_seconds"])
    logger.info("[Fase 2] Motor de ruteo para 'car': %s", "OSRM Docker" if osrm_available else "OSMnx/NetworkX (fallback automático)")
    osrm_host = osrm_full_host if osrm_available else None

    all_matrix_rows: list[dict[str, Any]] = []
    all_demand_rows: dict[Any, dict[str, Any]] = {}

    departments = sorted(demand_sample["DEP"].dropna().unique())
    for i, dept in enumerate(departments, start=1):
        t_dept = time.time()
        logger.info("[Fase 2] (%d/%d) Procesando departamento '%s'...", i, len(departments), dept)

        demand_dept = demand_sample[demand_sample["DEP"] == dept]
        resolutive_dept = resolutive[resolutive["DEPARTAMENTO"] == dept]
        all_facilities_dept = facilities[facilities["DEPARTAMENTO"] == dept]

        if resolutive_dept.empty:
            logger.warning("[Fase 2] '%s' no tiene establecimientos resolutivos; se omite el ruteo.", dept)
            continue

        dept_matrix, dept_demand_rows = _process_department(dept, demand_dept, resolutive_dept, all_facilities_dept, cfg, osrm_host)
        all_matrix_rows.extend(dept_matrix)
        all_demand_rows.update(dept_demand_rows)
        logger.info("[Fase 2] '%s' completado en %.1fs", dept, time.time() - t_dept)

    matrix_df = pd.DataFrame(all_matrix_rows)
    matrix_df.to_parquet(matrix_path, index=False)
    logger.info("[Fase 2] routing_matrix.parquet exportado: %s (%d filas)", matrix_path, len(matrix_df))

    demand_summary_df = pd.DataFrame(list(all_demand_rows.values()))
    if not demand_summary_df.empty:
        demand_summary_df = demand_summary_df.set_index("demand_id")
    demand_routed = demand_sample.join(demand_summary_df, how="left")
    demand_routed = gpd.GeoDataFrame(demand_routed, geometry="geometry", crs=demand_sample.crs)
    demand_routed.to_parquet(demand_routed_path, index=False)
    logger.info("[Fase 2] demand_routed.parquet exportado: %s (%d registros)", demand_routed_path, len(demand_routed))

    elapsed = time.time() - t_start
    logger.info("[Fase 2] Completa en %.1fs (%.1f min)", elapsed, elapsed / 60)

    return {
        "status": "computed",
        "routing_matrix": matrix_path,
        "demand_routed": demand_routed_path,
        "n_demand": len(demand_sample),
        "n_resolutivos": len(resolutive),
        "elapsed_seconds": elapsed,
    }


if __name__ == "__main__":
    from pprint import pprint

    pprint(run_all())

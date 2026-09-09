"""Fase 3 — Construcción de métricas de accesibilidad a salud resolutiva.

Funciones puras: reciben/devuelven ``pandas`` DataFrames/Series y no importan
ninguna librería de visualización (matplotlib/seaborn viven en ``export.py``).
Toda métrica pondera por población salvo que se indique explícitamente lo
contrario ("simple"), siguiendo el requisito de evitar promedios que igualen
un caserío con una capital.

Entrada esperada: ``data/processed/demand_routed.parquet`` (Fase 2), con al
menos las columnas ``car_duration_min`` (t_min), ``poblacion_1999`` (peso
poblacional real, censo INEI) y ``es_urbano`` (clasificación oficial INEI).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

import utils

logger = utils.setup_logging("metrics")

TIME_COL = "car_duration_min"
POP_COL = "poblacion_1999"
URBAN_COL = "es_urbano"

GOLDEN_HOUR_BINS = [0, 30, 60, 120, np.inf]
GOLDEN_HOUR_LABELS = ["<30 min", "30-60 min", "60-120 min", ">120 min (fuera de Golden Hour)"]


# ---------------------------------------------------------------------------
# 1. Tiempo de acceso t_min(i)
# ---------------------------------------------------------------------------

def get_access_time_minutes(demand: pd.DataFrame, time_col: str = TIME_COL) -> pd.Series:
    """t_min(i): minutos en automóvil de cada punto de demanda i a su
    establecimiento resolutivo más cercano (ya calculado en Fase 2 -
    ``routing.py`` — vía red vial u, para puntos no-ruteables, Haversine ×
    detour_factor, con ``{time_col}_is_unroutable`` marcando cuáles).
    """
    t_min = demand[time_col]
    if (t_min < 0).any():
        raise ValueError(f"'{time_col}' contiene valores negativos, no puede ser un tiempo de viaje")
    return t_min.rename("t_min")


def is_within_golden_hour(demand: pd.DataFrame, time_col: str = TIME_COL, threshold_minutes: float | None = None) -> pd.Series:
    """True si t_min(i) <= umbral de 'hora dorada' (``golden_hour.threshold_minutes`` en config.md)."""
    threshold = threshold_minutes if threshold_minutes is not None else utils.get_golden_hour_threshold()
    return (demand[time_col] <= threshold).rename("dentro_golden_hour")


def compute_golden_hour_summary(
    demand: pd.DataFrame, threshold_minutes: float, time_col: str = TIME_COL, pop_col: str = POP_COL,
) -> dict[str, float]:
    """Resumen binario dentro/fuera de un umbral de Golden Hour arbitrario (p.ej. para
    un control deslizante interactivo), en puntos y en población ponderada."""
    dentro = is_within_golden_hour(demand, time_col, threshold_minutes)
    total_puntos, total_poblacion = len(demand), demand[pop_col].sum()
    pob_fuera = demand.loc[~dentro, pop_col].sum()
    return {
        "umbral_minutos": threshold_minutes,
        "n_puntos_dentro": int(dentro.sum()),
        "n_puntos_fuera": int((~dentro).sum()),
        "pct_puntos_fuera": 100 * (~dentro).sum() / total_puntos if total_puntos else float("nan"),
        "poblacion_dentro": float(total_poblacion - pob_fuera),
        "poblacion_fuera": float(pob_fuera),
        "pct_poblacion_fuera": 100 * pob_fuera / total_poblacion if total_poblacion else float("nan"),
    }


# ---------------------------------------------------------------------------
# 2. Bandas de cobertura
# ---------------------------------------------------------------------------

def compute_coverage_bands(demand: pd.DataFrame, time_col: str = TIME_COL, pop_col: str = POP_COL) -> pd.DataFrame:
    """Conteo y % de puntos/población por banda de tiempo de acceso.

    Bandas semiabiertas [a, b): <30 incluye 0-29.99..., 30-60 incluye
    30-59.99..., etc.; el último tramo (>120) captura todo lo demás,
    incluyendo estimaciones Haversine para puntos no-ruteables.
    """
    banda = pd.cut(demand[time_col], bins=GOLDEN_HOUR_BINS, labels=GOLDEN_HOUR_LABELS, right=False).rename("banda")

    result = demand.groupby(banda, observed=False).agg(
        n_puntos=(time_col, "size"),
        poblacion=(pop_col, "sum"),
    ).reindex(GOLDEN_HOUR_LABELS).reset_index()

    total_puntos, total_poblacion = result["n_puntos"].sum(), result["poblacion"].sum()
    result["pct_puntos"] = 100 * result["n_puntos"] / total_puntos if total_puntos else np.nan
    result["pct_poblacion"] = 100 * result["poblacion"] / total_poblacion if total_poblacion else np.nan
    return result


# ---------------------------------------------------------------------------
# 3. Tiempos de acceso ponderados por población (jerárquico)
# ---------------------------------------------------------------------------

def weighted_mean(values: Any, weights: Any) -> float:
    """Media ponderada robusta a NaN; devuelve NaN si no hay peso total > 0."""
    values = np.asarray(values, dtype=float)
    weights = np.asarray(weights, dtype=float)
    mask = ~(np.isnan(values) | np.isnan(weights))
    if not mask.any() or weights[mask].sum() <= 0:
        return float("nan")
    return float(np.average(values[mask], weights=weights[mask]))


def aggregate_access_time(
    demand: pd.DataFrame, level: list[str], time_col: str = TIME_COL, pop_col: str = POP_COL,
) -> pd.DataFrame:
    """Agregación jerárquica del tiempo de acceso (nivel Distrital/Provincial/
    Departamental según ``level``, p.ej. ``["DEP","PROV","DIST"]``).

    Reporta la media PONDERADA por población (métrica principal, evita que un
    caserío de 30 habitantes pese lo mismo que una capital de 30,000) junto a
    la media simple y la mediana, solo para contraste/diagnóstico.
    """
    grouped = demand.groupby(level, observed=True)
    base = grouped.agg(
        n_puntos=(time_col, "size"),
        poblacion_total=(pop_col, "sum"),
        tiempo_medio_simple_min=(time_col, "mean"),
        tiempo_mediana_min=(time_col, "median"),
    ).reset_index()

    weighted = grouped.apply(lambda g: weighted_mean(g[time_col], g[pop_col]), include_groups=False)
    weighted.name = "tiempo_medio_ponderado_min"
    result = base.merge(weighted.reset_index(), on=level)
    return result.sort_values(level).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 4. Ranking de brechas críticas
# ---------------------------------------------------------------------------

def rank_critical_districts(
    district_agg: pd.DataFrame, n: int = 10, dep_col: str = "DEP", time_col: str = "tiempo_medio_ponderado_min",
) -> pd.DataFrame:
    """Los `n` distritos con PEOR (mayor) tiempo de acceso ponderado, por departamento."""
    ranked = district_agg.sort_values(time_col, ascending=False).groupby(dep_col, group_keys=False, observed=True).head(n).copy()
    ranked["ranking"] = ranked.groupby(dep_col, observed=True)[time_col].rank(ascending=False, method="first").astype(int)
    return ranked.sort_values([dep_col, "ranking"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 5. Desigualdad: Gini + Curva de Lorenz
# ---------------------------------------------------------------------------

def lorenz_curve(values: Any, weights: Any) -> pd.DataFrame:
    """Puntos (%población acumulada, %tiempo acumulado) de la curva de Lorenz,
    ordenando por tiempo de acceso ascendente."""
    values = np.asarray(values, dtype=float)
    weights = np.asarray(weights, dtype=float)
    mask = ~(np.isnan(values) | np.isnan(weights))
    values, weights = values[mask], weights[mask]

    order = np.argsort(values)
    values, weights = values[order], weights[order]
    cum_w = np.cumsum(weights)
    cum_wv = np.cumsum(weights * values)

    p = np.concatenate([[0.0], cum_w / cum_w[-1]])
    l = np.concatenate([[0.0], cum_wv / cum_wv[-1]])
    return pd.DataFrame({"poblacion_acumulada_pct": p * 100, "tiempo_acumulado_pct": l * 100})


def gini_coefficient(values: Any, weights: Any) -> float:
    """Coeficiente de Gini ponderado por población, aplicado al tiempo de acceso.

    Justificación metodológica: Gini/Lorenz miden tradicionalmente la
    desigualdad en la distribución de un BIEN (ingreso) entre personas. Aquí
    se aplican a un COSTO (minutos de viaje): un Gini alto no significa que
    pocos concentran un beneficio, sino que la CARGA de tiempo de traslado
    está muy desigualmente repartida (algunos residentes soportan tiempos
    mucho mayores que el resto). Es una adaptación estándar en la literatura
    de equidad en transporte/accesibilidad (Gini de tiempos de viaje), y se
    reporta junto a la curva de Lorenz correspondiente para que la lectura
    "más alto = más desigual" quede acompañada de su forma completa.
    """
    curve = lorenz_curve(values, weights)
    p = curve["poblacion_acumulada_pct"].to_numpy() / 100
    l = curve["tiempo_acumulado_pct"].to_numpy() / 100
    trapz = getattr(np, "trapezoid", None) or np.trapz
    area_bajo_lorenz = trapz(l, p)
    return float(1 - 2 * area_bajo_lorenz)


# ---------------------------------------------------------------------------
# 6. Contraste urbano vs. rural
# ---------------------------------------------------------------------------

def compare_urban_rural(
    demand: pd.DataFrame, time_col: str = TIME_COL, pop_col: str = POP_COL, urban_col: str = URBAN_COL,
) -> dict[str, Any]:
    """Compara estadísticamente el tiempo de acceso entre puntos urbanos y rurales.

    Usa Mann-Whitney U (no paramétrico) en vez de una prueba t: los tiempos de
    viaje suelen tener distribuciones muy sesgadas a la derecha (colas largas
    por puntos mal conectados), violando el supuesto de normalidad de la t.
    """
    is_urban = demand[urban_col].astype(bool)
    urban_t, rural_t = demand.loc[is_urban, time_col].dropna(), demand.loc[~is_urban, time_col].dropna()

    def _row(label: str, mask: pd.Series, t: pd.Series) -> dict[str, Any]:
        pop = demand.loc[mask, pop_col]
        return {
            "grupo": label,
            "n_puntos": int(mask.sum()),
            "poblacion": float(pop.sum()),
            "tiempo_medio_ponderado_min": weighted_mean(demand.loc[mask, time_col], pop),
            "tiempo_medio_simple_min": float(t.mean()) if len(t) else float("nan"),
            "tiempo_mediana_min": float(t.median()) if len(t) else float("nan"),
        }

    summary = pd.DataFrame([_row("Urbano", is_urban, urban_t), _row("Rural", ~is_urban, rural_t)])

    if len(urban_t) > 0 and len(rural_t) > 0:
        statistic, p_value = stats.mannwhitneyu(urban_t, rural_t, alternative="two-sided")
    else:
        statistic, p_value = float("nan"), float("nan")

    return {
        "summary": summary,
        "test": {"metodo": "Mann-Whitney U", "estadistico": float(statistic), "p_value": float(p_value)},
    }


# ---------------------------------------------------------------------------
# 2 (spec) — Cruce analítico obligatorio: tiempo de acceso × índice de ruralidad
# ---------------------------------------------------------------------------

def compute_rurality_index(
    demand: pd.DataFrame, level: list[str], urban_col: str = URBAN_COL, pop_col: str = POP_COL,
) -> pd.DataFrame:
    """% de población clasificada RURAL (INEI, oficial) por unidad geográfica.

    Se usa como "segundo indicador regional" del cruce analítico exigido:
    se deriva directamente de datos ya validados (sin necesidad de una fuente
    externa nueva) y es una medida legítima de ruralidad a nivel de unidad
    geográfica.
    """
    def _pct_rural(g: pd.DataFrame) -> float:
        total = g[pop_col].sum()
        if total <= 0:
            return float("nan")
        return 100 * g.loc[~g[urban_col].astype(bool), pop_col].sum() / total

    idx = demand.groupby(level, observed=True).apply(_pct_rural, include_groups=False)
    idx.name = "indice_ruralidad_pct"
    return idx.reset_index()


def cross_analysis_rurality_access(
    district_agg: pd.DataFrame, rurality_df: pd.DataFrame, level: list[str],
    time_col: str = "tiempo_medio_ponderado_min",
) -> dict[str, Any]:
    """Cruza el tiempo de acceso ponderado con el índice de ruralidad a nivel distrital.

    NATURALEZA DE LA RELACIÓN (documentado también en logs, según lo exigido):
    esta es una asociación CORRELACIONAL / ESPACIAL entre unidades geográficas
    agregadas (distritos) — un "ecological correlation" — NO una relación
    CAUSAL. Un distrito más rural puede tener peor acceso por múltiples
    factores confundidos entre sí (menor densidad vial, mayor distancia a la
    capital provincial, orografía, dispersión poblacional) que este cruce
    bivariado no aísla ni controla. Se reporta un coeficiente de correlación
    de Spearman (robusto a relaciones monótonas no lineales y a outliers),
    no un modelo causal.
    """
    merged = district_agg.merge(rurality_df, on=level, how="inner")
    valid = merged.dropna(subset=[time_col, "indice_ruralidad_pct"])

    if len(valid) >= 3:
        corr, p_value = stats.spearmanr(valid["indice_ruralidad_pct"], valid[time_col])
    else:
        corr, p_value = float("nan"), float("nan")

    logger.info(
        "Cruce analítico tiempo_acceso x indice_ruralidad (n=%d distritos): rho de Spearman=%.3f "
        "(p=%.4f). NATURALEZA: asociación CORRELACIONAL/ESPACIAL entre distritos, NO causal — "
        "no se controla por densidad vial, orografía ni distancia a capital provincial.",
        len(valid), corr if not np.isnan(corr) else float("nan"), p_value if not np.isnan(p_value) else float("nan"),
    )

    return {
        "merged": merged,
        "correlation": {
            "metodo": "Spearman",
            "naturaleza": "correlacional/espacial (no causal)",
            "coeficiente": float(corr),
            "p_value": float(p_value),
            "n_distritos": int(len(valid)),
        },
    }


if __name__ == "__main__":
    import geopandas as gpd
    from pprint import pprint

    demand = gpd.read_parquet(utils.get_paths()["data_processed"] / "demand_routed.parquet")
    pprint(compute_coverage_bands(demand))
    district_agg = aggregate_access_time(demand, ["DEP", "PROV", "DIST"])
    pprint(rank_critical_districts(district_agg).head())
    pprint(gini_coefficient(demand[TIME_COL], demand[POP_COL]))

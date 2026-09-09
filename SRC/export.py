"""Fase 3 — Exportación de métricas para el reporte (LaTeX) y el dashboard.

Orquesta ``metrics.py`` (lógica pura) sobre ``data/processed/demand_routed.parquet``
y exporta:

    - Tablas en CSV a ``data/outputs/`` (una por métrica).
    - Las mismas tablas en LaTeX (``.tex``, con reglas ``booktabs`` vía
      ``pandas.Styler.to_latex(hrules=True)``) a ``data/outputs/``, listas para
      ``\\input{}`` en el reporte de Fase 5. Requiere ``\\usepackage{booktabs}``
      en el preámbulo del documento LaTeX que las incluya.
    - 3 figuras clave a ``report/figures/`` en PDF (vectorial, para LaTeX) y
      PNG de alta resolución (200 dpi, para el dashboard): Curva de Lorenz,
      distribución de tiempos de acceso por departamento, y mapa de brechas
      distrital.

Paleta: colores tomados del sistema de diseño de referencia del proyecto
(secuencial azul para magnitud/mapa; categórico azul/naranja/aguamarina en
orden fijo para los 3 departamentos) — nunca "rainbow", nunca color como
único portador de identidad.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

import geopandas as gpd
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap

import metrics
import utils
import validation

logger = utils.setup_logging("export")

# --- Paleta (ver skill de dataviz del proyecto: secuencial de un solo hue,
# categórico en orden fijo, nunca rainbow) ---
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
BASELINE = "#c3c2b7"
SURFACE = "#fcfcfb"
CATEGORICAL = {"ICA": "#2a78d6", "CUSCO": "#eb6834", "LORETO": "#1baf7a"}
SEQUENTIAL_BLUE_STEPS = ["#cde2fb", "#9ec5f4", "#5598e7", "#2a78d6", "#184f95", "#0d366b"]
SEQUENTIAL_CMAP = LinearSegmentedColormap.from_list("golden_hour_seq", SEQUENTIAL_BLUE_STEPS)

DEP_ORDER = ["ICA", "CUSCO", "LORETO"]  # Costa, Sierra, Selva


def _normalize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return text.strip().upper()


def _apply_chart_style(ax: plt.Axes, title: str, xlabel: str = "", ylabel: str = "") -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, color=INK_PRIMARY, fontsize=13, fontweight="bold", loc="left")
    ax.set_xlabel(xlabel, color=INK_SECONDARY, fontsize=10)
    ax.set_ylabel(ylabel, color=INK_SECONDARY, fontsize=10)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(BASELINE)
    ax.grid(True, color=GRIDLINE, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)


# ---------------------------------------------------------------------------
# 1. Carga de datos + cómputo de todas las métricas (usa metrics.py)
# ---------------------------------------------------------------------------

def load_demand_routed() -> gpd.GeoDataFrame:
    return gpd.read_parquet(utils.get_paths()["data_processed"] / "demand_routed.parquet")


def compute_all_metrics(demand: gpd.GeoDataFrame) -> dict[str, Any]:
    """Ejecuta toda la batería de métricas de Fase 3 y devuelve un dict con cada tabla."""
    logger.info("Calculando métricas sobre %d puntos de demanda...", len(demand))

    coverage = metrics.compute_coverage_bands(demand)

    by_district = metrics.aggregate_access_time(demand, ["DEP", "PROV", "DIST"])
    by_province = metrics.aggregate_access_time(demand, ["DEP", "PROV"])
    by_department = metrics.aggregate_access_time(demand, ["DEP"])

    critical = metrics.rank_critical_districts(by_district, n=10)

    gini_national = metrics.gini_coefficient(demand[metrics.TIME_COL], demand[metrics.POP_COL])
    gini_by_dept = demand.groupby("DEP", observed=True).apply(
        lambda g: metrics.gini_coefficient(g[metrics.TIME_COL], g[metrics.POP_COL]), include_groups=False
    )
    gini_summary = pd.concat(
        [pd.Series({"NACIONAL (3 deptos)": gini_national}), gini_by_dept]
    ).rename("gini").reset_index().rename(columns={"index": "ambito"})
    lorenz = metrics.lorenz_curve(demand[metrics.TIME_COL], demand[metrics.POP_COL])

    urban_rural = metrics.compare_urban_rural(demand)

    rurality_district = metrics.compute_rurality_index(demand, ["DEP", "PROV", "DIST"])
    cross = metrics.cross_analysis_rurality_access(by_district, rurality_district, ["DEP", "PROV", "DIST"])

    logger.info(
        "Métricas calculadas: Gini nacional=%.3f | Mann-Whitney U urbano/rural p=%.4f | "
        "correlación ruralidad-acceso (Spearman)=%.3f (n=%d distritos, %s)",
        gini_national, urban_rural["test"]["p_value"],
        cross["correlation"]["coeficiente"], cross["correlation"]["n_distritos"],
        cross["correlation"]["naturaleza"],
    )

    return {
        "coverage_bands": coverage,
        "access_time_by_district": by_district,
        "access_time_by_province": by_province,
        "access_time_by_department": by_department,
        "critical_districts_ranking": critical,
        "gini_summary": gini_summary,
        "lorenz_curve": lorenz,
        "urban_rural_summary": urban_rural["summary"],
        "urban_rural_test": pd.DataFrame([urban_rural["test"]]),
        "rurality_vs_access": cross["merged"],
        "rurality_correlation": pd.DataFrame([cross["correlation"]]),
    }


# ---------------------------------------------------------------------------
# 2. Exportación de tablas: CSV + LaTeX (booktabs)
# ---------------------------------------------------------------------------

def export_csv(df: pd.DataFrame, name: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{name}.csv"
    df.to_csv(dest, index=False, encoding="utf-8-sig")
    logger.info("CSV exportado: %s (%d filas)", dest, len(df))
    return dest


# snake_case -> encabezado legible en español. Necesario porque un '_' crudo
# rompe la compilación LaTeX en modo texto (requeriría \_); cualquier columna
# no listada aquí cae al fallback genérico en _humanize_column.
COLUMN_LABELS = {
    "DEP": "Departamento", "PROV": "Provincia", "DIST": "Distrito",
    "n_puntos": "Núm. centros poblados", "n_distritos": "Núm. distritos",
    "poblacion_total": "Población", "poblacion": "Población",
    "tiempo_medio_simple_min": "Tiempo medio simple (min)", "tiempo_mediana_min": "Mediana (min)",
    "tiempo_medio_ponderado_min": "Tiempo ponderado (min)", "ranking": "Ranking",
    "banda": "Banda de tiempo", "pct_puntos": "Pct. centros poblados", "pct_poblacion": "Pct. población",
    "ambito": "Ámbito", "gini": "Gini", "grupo": "Grupo", "p_value": "p-valor",
    "metodo": "Método", "estadistico": "Estadístico", "naturaleza": "Naturaleza", "coeficiente": "Coeficiente",
}


def _humanize_column(col: str) -> str:
    return COLUMN_LABELS.get(col, col.replace("_", " ").capitalize())


# Formatos por columna que necesitan más (p-valores: se pierden bajo
# precision=2, p.ej. 0.0002 -> "0.00") o menos (conteos: sin decimales,
# separador de miles) precisión que el default global de la tabla.
CUSTOM_FORMATTERS = {
    "poblacion_total": "{:,.0f}", "poblacion": "{:,.0f}", "n_puntos": "{:,.0f}",
    "n_distritos": "{:,.0f}", "ranking": "{:,.0f}", "estadistico": "{:,.0f}",
    "p_value": "{:.4f}", "gini": "{:.3f}", "coeficiente": "{:.3f}",
}


def export_latex_table(df: pd.DataFrame, name: str, out_dir: Path, caption: str, label: str) -> Path:
    """Tabla LaTeX con reglas booktabs (requiere \\usepackage{booktabs}).

    Los encabezados se traducen a texto legible (ver ``COLUMN_LABELS``): un
    '_' crudo en un encabezado rompe la compilación LaTeX en modo texto.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{name}.tex"
    display_df = df.rename(columns=_humanize_column)
    formatters = {_humanize_column(c): fmt for c, fmt in CUSTOM_FORMATTERS.items() if c in df.columns}
    styler = display_df.style.hide(axis="index").format(formatter=formatters, precision=2, na_rep="--", escape="latex")
    latex = styler.to_latex(
        hrules=True, caption=caption, label=f"tab:{label}", position="htbp", position_float="centering",
    )
    # Ancho de contenido natural de `tabular` ignora \textwidth; se envuelve en
    # \resizebox (requiere \usepackage{graphicx}) para que NUNCA desborde la
    # página. El `\ifdim` evita agrandar tablas angostas que ya caben solas.
    latex = latex.replace(
        "\\begin{tabular}",
        "\\resizebox{\\ifdim\\width>\\textwidth\\textwidth\\else\\width\\fi}{!}{%\n\\begin{tabular}",
        1,
    )
    latex = latex.replace("\\end{tabular}", "\\end{tabular}%\n}", 1)
    dest.write_text(latex, encoding="utf-8")
    logger.info("Tabla LaTeX exportada: %s", dest)
    return dest


TABLE_SPECS: dict[str, tuple[str, str]] = {
    # access_time_by_district (200 filas) se omite del LaTeX a propósito: es
    # dato desagregado para el dashboard/anexo (CSV), no una tabla de reporte.
    "coverage_bands": ("Cobertura por banda de tiempo de acceso a salud resolutiva", "coverage-bands"),
    "access_time_by_province": ("Tiempo de acceso ponderado por población, nivel provincial", "access-province"),
    "access_time_by_department": ("Tiempo de acceso ponderado por población, nivel departamental", "access-department"),
    "critical_districts_ranking": ("Top 10 distritos con peor accesibilidad ponderada, por departamento", "critical-districts"),
    "gini_summary": ("Coeficiente de Gini del tiempo de acceso, nacional y por departamento", "gini"),
    "urban_rural_summary": ("Tiempo de acceso: contraste urbano vs. rural", "urban-rural"),
    "urban_rural_test": ("Prueba de Mann-Whitney U: tiempo de acceso urbano vs. rural", "urban-rural-test"),
    "rurality_correlation": ("Correlación (Spearman) entre índice de ruralidad y tiempo de acceso distrital", "rurality-correlation"),
}


def export_all_tables(tables: dict[str, pd.DataFrame], out_dir: Path) -> dict[str, dict[str, Path]]:
    result: dict[str, dict[str, Path]] = {}
    for name, df in tables.items():
        csv_path = export_csv(df, name, out_dir)
        entry = {"csv": csv_path}
        if name in TABLE_SPECS:
            caption, label = TABLE_SPECS[name]
            entry["tex"] = export_latex_table(df, name, out_dir, caption, label)
        result[name] = entry
    return result


# ---------------------------------------------------------------------------
# 3. Figuras: Curva de Lorenz, distribución por departamento, mapa de brechas
# ---------------------------------------------------------------------------

def _save_figure(fig: plt.Figure, name: str, figures_dir: Path) -> dict[str, Path]:
    figures_dir.mkdir(parents=True, exist_ok=True)
    pdf_path, png_path = figures_dir / f"{name}.pdf", figures_dir / f"{name}.png"
    fig.savefig(pdf_path, bbox_inches="tight")
    fig.savefig(png_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    logger.info("Figura exportada: %s (+ .png a 200dpi)", pdf_path)
    return {"pdf": pdf_path, "png": png_path}


def plot_lorenz_curve(lorenz: pd.DataFrame, gini: float, figures_dir: Path) -> dict[str, Path]:
    fig, ax = plt.subplots(figsize=(7, 6.3))
    ax.plot([0, 100], [0, 100], linestyle="--", linewidth=1.5, color=BASELINE, zorder=2)
    ax.plot(
        lorenz["poblacion_acumulada_pct"], lorenz["tiempo_acumulado_pct"],
        linewidth=2.5, color=CATEGORICAL["ICA"], zorder=3,
    )
    ax.fill_between(
        lorenz["poblacion_acumulada_pct"], lorenz["poblacion_acumulada_pct"], lorenz["tiempo_acumulado_pct"],
        color=SEQUENTIAL_BLUE_STEPS[0], alpha=0.5, zorder=1,
    )
    ax.text(60, 66, "Igualdad perfecta", color=INK_MUTED, fontsize=9, rotation=38, ha="center")
    ax.text(75, 40, f"Curva de Lorenz\n(Gini = {gini:.3f})", color=CATEGORICAL["ICA"], fontsize=10, fontweight="bold")
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    _apply_chart_style(
        ax, "Desigualdad en el tiempo de acceso a salud resolutiva",
        "% de población acumulada (de menor a mayor tiempo de acceso)", "% de tiempo de acceso acumulado",
    )
    fig.tight_layout()
    return _save_figure(fig, "lorenz_curve", figures_dir)


def plot_access_time_distribution(demand: pd.DataFrame, figures_dir: Path) -> dict[str, Path]:
    data = [demand.loc[demand["DEP"] == dep, metrics.TIME_COL].dropna() for dep in DEP_ORDER]
    colors = [CATEGORICAL[dep] for dep in DEP_ORDER]

    fig, ax = plt.subplots(figsize=(8, 5.5))
    bp = ax.boxplot(data, tick_labels=DEP_ORDER, patch_artist=True, showfliers=False, widths=0.55, zorder=3)
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.55)
        patch.set_edgecolor(color)
    for element in ("whiskers", "caps"):
        for artist in bp[element]:
            artist.set_color(INK_MUTED)
    for median in bp["medians"]:
        median.set_color(INK_PRIMARY)
        median.set_linewidth(1.8)

    for umbral, etiqueta in ((30, "30 min"), (60, "60 min"), (120, "120 min")):
        ax.axhline(umbral, color=BASELINE, linewidth=1, linestyle=":", zorder=1)
        ax.text(3.52, umbral, etiqueta, color=INK_MUTED, fontsize=8, va="center")

    _apply_chart_style(ax, "Distribución del tiempo de acceso en auto, por departamento", "", "Minutos (t_min)")
    ax.set_xlim(0.5, 3.7)
    fig.tight_layout()
    return _save_figure(fig, "access_time_distribution_by_department", figures_dir)


def plot_gap_map(district_agg: pd.DataFrame, distritos: gpd.GeoDataFrame, figures_dir: Path) -> dict[str, Path]:
    distritos = distritos.copy()
    distritos["_key"] = (
        distritos["DEPARTAMEN"].map(_normalize_text) + "|"
        + distritos["PROVINCIA"].map(_normalize_text) + "|"
        + distritos["DISTRITO"].map(_normalize_text)
    )
    agg = district_agg.copy()
    agg["_key"] = (
        agg["DEP"].map(_normalize_text) + "|" + agg["PROV"].map(_normalize_text) + "|" + agg["DIST"].map(_normalize_text)
    )
    merged = distritos.merge(agg[["_key", "tiempo_medio_ponderado_min"]], on="_key", how="left")

    # Pequeños múltiplos (uno por departamento) en vez de un único mapa nacional:
    # Ica/Cusco/Loreto no son contiguos, así que un mapa nacional desperdicia casi
    # toda su área en el territorio vacío entre ellos y encoge las 3 zonas de interés.
    vmin, vmax = merged["tiempo_medio_ponderado_min"].min(), merged["tiempo_medio_ponderado_min"].max()
    norm = plt.Normalize(vmin=vmin, vmax=vmax)

    fig, axes = plt.subplots(1, len(DEP_ORDER), figsize=(5 * len(DEP_ORDER), 5.5))
    for ax, dep in zip(axes, DEP_ORDER):
        subset = merged[merged["_key"].str.startswith(f"{dep}|")]
        subset.plot(
            column="tiempo_medio_ponderado_min", cmap=SEQUENTIAL_CMAP, norm=norm,
            linewidth=0.3, edgecolor=BASELINE, ax=ax,
            missing_kwds={"color": "#f0efec", "edgecolor": BASELINE, "label": "Sin datos"},
        )
        ax.set_title(dep.title(), color=INK_PRIMARY, fontsize=12, fontweight="bold")
        ax.set_axis_off()

    sm = plt.cm.ScalarMappable(cmap=SEQUENTIAL_CMAP, norm=norm)
    cbar = fig.colorbar(sm, ax=axes, orientation="horizontal", shrink=0.5, pad=0.02, aspect=30)
    cbar.set_label("Tiempo de acceso ponderado por población (min)", color=INK_SECONDARY, fontsize=10)
    cbar.ax.tick_params(colors=INK_MUTED, labelsize=9)

    fig.suptitle(
        "Brechas de accesibilidad a salud resolutiva por distrito", color=INK_PRIMARY, fontsize=14, fontweight="bold", x=0.125, ha="left",
    )
    return _save_figure(fig, "gap_map_district_access_time", figures_dir)


def export_all_figures(demand: gpd.GeoDataFrame, tables: dict[str, pd.DataFrame], figures_dir: Path) -> dict[str, dict[str, Path]]:
    gini_national = tables["gini_summary"].loc[tables["gini_summary"]["ambito"] == "NACIONAL (3 deptos)", "gini"].iloc[0]
    distritos = validation.load_distritos()

    return {
        "lorenz_curve": plot_lorenz_curve(tables["lorenz_curve"], gini_national, figures_dir),
        "access_time_distribution_by_department": plot_access_time_distribution(demand, figures_dir),
        "gap_map_district_access_time": plot_gap_map(tables["access_time_by_district"], distritos, figures_dir),
    }


# ---------------------------------------------------------------------------
# 4. Orquestación
# ---------------------------------------------------------------------------

def run_all() -> dict[str, Any]:
    paths = utils.get_paths()
    demand = load_demand_routed()
    tables = compute_all_metrics(demand)

    table_paths = export_all_tables(tables, paths["data_outputs"])
    figure_paths = export_all_figures(demand, tables, paths["report_figures"])

    logger.info("Fase 3 completa: %d tablas y %d figuras exportadas.", len(table_paths), len(figure_paths))
    return {"tables": table_paths, "figures": figure_paths}


if __name__ == "__main__":
    from pprint import pprint

    pprint(run_all())

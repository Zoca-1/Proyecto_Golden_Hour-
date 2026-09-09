"""Fase 4 — Dashboard interactivo Golden Hour (Streamlit).

Lee los datos ya procesados en Fases 1-3 (``data/processed/``,
``data/outputs/``) y reutiliza la lógica pura de ``metrics.py`` para
recalcular en vivo todas las métricas sobre el subconjunto filtrado por el
usuario (departamento/provincia/distrito, perfil de transporte, umbral de
Golden Hour) — no se limita a mostrar las tablas estáticas de Fase 3.

Ejecutar con:  streamlit run golden_hour/src/dashboard.py
"""

from __future__ import annotations

from typing import Any

import branca.colormap as bcm
import folium
import geopandas as gpd
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from streamlit_folium import st_folium

import metrics
import utils
import validation

st.set_page_config(layout="wide", page_title="Golden Hour - Accesibilidad a Salud en Perú")

MODE_LABELS = {"car": "Automóvil 🚗", "foot": "Caminata 🚶", "bike": "Bicicleta 🚲"}
THRESHOLD_OPTIONS = [30, 60, 90, 120]
DEP_ORDER = ["ICA", "CUSCO", "LORETO"]
MAX_MAP_POINTS = 2000

CATEGORICAL = {"ICA": "#2a78d6", "CUSCO": "#eb6834", "LORETO": "#1baf7a"}
SEQUENTIAL_BLUE = ["#cde2fb", "#9ec5f4", "#5598e7", "#2a78d6", "#184f95", "#0d366b"]


# ---------------------------------------------------------------------------
# 1. Carga de datos (cacheada)
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner="Cargando centros poblados...")
def load_demand() -> gpd.GeoDataFrame:
    return gpd.read_parquet(utils.get_paths()["data_processed"] / "demand_routed.parquet")


@st.cache_data(show_spinner="Cargando establecimientos de salud...")
def load_facilities() -> gpd.GeoDataFrame:
    return gpd.read_parquet(utils.get_paths()["data_processed"] / "renipress_validado.parquet")


@st.cache_data(show_spinner="Cargando límites distritales...")
def load_distritos() -> gpd.GeoDataFrame:
    return validation.load_distritos()


@st.cache_data
def load_output_table(name: str) -> pd.DataFrame:
    path = utils.get_paths()["data_outputs"] / f"{name}.csv"
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


# ---------------------------------------------------------------------------
# 2. Filtros globales (sidebar)
# ---------------------------------------------------------------------------

def render_sidebar(demand: gpd.GeoDataFrame) -> dict[str, Any]:
    st.sidebar.title("🏥 Golden Hour")
    st.sidebar.caption("Filtros globales — afectan las 4 pestañas")

    dep_opts = ["Todos"] + [d.title() for d in DEP_ORDER if d in demand["DEP"].unique()]
    dep_sel = st.sidebar.selectbox("Departamento", dep_opts, index=0)
    dep_key = dep_sel.upper() if dep_sel != "Todos" else None

    prov_scope = demand if dep_key is None else demand[demand["DEP"] == dep_key]
    prov_opts = ["Todas"] + sorted(prov_scope["PROV"].dropna().unique())
    prov_sel = st.sidebar.selectbox("Provincia", prov_opts, index=0, disabled=(dep_key is None))
    prov_key = prov_sel if prov_sel != "Todas" else None

    dist_scope = prov_scope if prov_key is None else prov_scope[prov_scope["PROV"] == prov_key]
    dist_opts = ["Todos"] + sorted(dist_scope["DIST"].dropna().unique())
    dist_sel = st.sidebar.selectbox("Distrito", dist_opts, index=0, disabled=(prov_key is None))
    dist_key = dist_sel if dist_sel != "Todos" else None

    st.sidebar.divider()
    profile = st.sidebar.radio(
        "Perfil de transporte", options=list(MODE_LABELS.keys()), format_func=lambda k: MODE_LABELS[k],
    )
    threshold = st.sidebar.select_slider("Umbral de Golden Hour (min)", options=THRESHOLD_OPTIONS, value=60)

    st.sidebar.divider()
    st.sidebar.caption(
        "Los tiempos provienen de OSRM/OSMnx cuando la red vial está disponible; si no, se "
        "estiman con distancia Haversine × factor de desvío (ver Fase 2)."
    )

    return {"dep": dep_key, "prov": prov_key, "dist": dist_key, "profile": profile, "threshold": threshold}


def apply_filters(df: gpd.GeoDataFrame, filters: dict[str, Any], dep_col="DEP", prov_col="PROV", dist_col="DIST") -> gpd.GeoDataFrame:
    if filters["dep"] is not None:
        df = df[df[dep_col] == filters["dep"]]
    if filters["prov"] is not None:
        df = df[df[prov_col] == filters["prov"]]
    if filters["dist"] is not None:
        df = df[df[dist_col] == filters["dist"]]
    return df


def filter_facilities(facilities: gpd.GeoDataFrame, filters: dict[str, Any]) -> gpd.GeoDataFrame:
    return apply_filters(facilities, filters, dep_col="DEPARTAMENTO", prov_col="PROVINCIA", dist_col="DISTRITO")


# ---------------------------------------------------------------------------
# 3. Tab 1 — Visión general y KPIs
# ---------------------------------------------------------------------------

def render_tab_overview(demand: gpd.GeoDataFrame, facilities: gpd.GeoDataFrame, filters: dict[str, Any]) -> None:
    time_col = f"{filters['profile']}_duration_min"

    if demand.empty:
        st.warning("No hay puntos de demanda para el filtro seleccionado.")
        return

    weighted_time = metrics.weighted_mean(demand[time_col], demand[metrics.POP_COL])
    gh_summary = metrics.compute_golden_hour_summary(demand, filters["threshold"], time_col)
    n_resolutivos = int(filter_facilities(facilities, filters)["es_resolutivo"].sum())
    poblacion_total = demand[metrics.POP_COL].sum()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"Tiempo promedio ponderado ({MODE_LABELS[filters['profile']]})", f"{weighted_time:.1f} min")
    c2.metric(f"% Población fuera de Golden Hour (>{filters['threshold']} min)", f"{gh_summary['pct_poblacion_fuera']:.1f}%")
    c3.metric("Establecimientos resolutivos", f"{n_resolutivos:,}")
    c4.metric("Población analizada", f"{poblacion_total:,.0f}")

    st.divider()
    col_left, col_right = st.columns(2)

    with col_left:
        by_dept = metrics.aggregate_access_time(demand, ["DEP"], time_col=time_col)
        fig = px.bar(
            by_dept, x="DEP", y="tiempo_medio_ponderado_min", color="DEP",
            color_discrete_map=CATEGORICAL, category_orders={"DEP": DEP_ORDER},
            labels={"tiempo_medio_ponderado_min": "Minutos (ponderado)", "DEP": "Departamento"},
            title=f"Tiempo de acceso ponderado por departamento ({MODE_LABELS[filters['profile']]})",
        )
        fig.update_layout(showlegend=False, plot_bgcolor="#fcfcfb", paper_bgcolor="#fcfcfb")
        st.plotly_chart(fig, width="stretch")

    with col_right:
        coverage = metrics.compute_coverage_bands(demand, time_col=time_col)
        fig = px.bar(
            coverage, x="banda", y="pct_poblacion",
            labels={"pct_poblacion": "% de población", "banda": "Banda de tiempo de acceso"},
            title="Distribución de la población por banda de acceso",
            color_discrete_sequence=[CATEGORICAL["ICA"]],
        )
        fig.update_layout(plot_bgcolor="#fcfcfb", paper_bgcolor="#fcfcfb")
        st.plotly_chart(fig, width="stretch")


# ---------------------------------------------------------------------------
# 4. Tab 2 — Explorador espacial de brechas (Folium)
# ---------------------------------------------------------------------------

def render_tab_map(demand: gpd.GeoDataFrame, facilities: gpd.GeoDataFrame, distritos: gpd.GeoDataFrame, filters: dict[str, Any]) -> None:
    time_col = f"{filters['profile']}_duration_min"

    if demand.empty:
        st.warning("No hay puntos de demanda para el filtro seleccionado.")
        return

    facilities_scope = filter_facilities(facilities, filters)
    resolutivos_scope = facilities_scope[facilities_scope["es_resolutivo"]]

    distritos_scope = distritos
    if filters["dep"] is not None:
        distritos_scope = distritos_scope[distritos_scope["_dept_norm"] == filters["dep"]]
    if filters["prov"] is not None:
        distritos_scope = distritos_scope[distritos_scope["PROVINCIA"].str.upper() == filters["prov"].upper()]
    if filters["dist"] is not None:
        distritos_scope = distritos_scope[distritos_scope["DISTRITO"].str.upper() == filters["dist"].upper()]

    demand_map = demand
    if len(demand_map) > MAX_MAP_POINTS:
        st.caption(f"Mostrando una muestra aleatoria de {MAX_MAP_POINTS:,} de {len(demand_map):,} puntos por rendimiento del mapa.")
        demand_map = demand_map.sample(n=MAX_MAP_POINTS, random_state=42)

    bounds = demand_map.total_bounds  # minx, miny, maxx, maxy
    center = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]

    # OpenStreetMap: único proveedor de teselas gratuito sin API key en folium
    # (CartoDB/Stamen ahora requieren una, ver github.com/python-visualization/folium/issues/1993).
    m = folium.Map(location=center, tiles="OpenStreetMap", control_scale=True)
    m.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])

    if not distritos_scope.empty:
        folium.GeoJson(
            distritos_scope[["DISTRITO", "PROVINCIA", "geometry"]],
            name="Límites distritales",
            style_function=lambda f: {"color": "#898781", "weight": 1, "fillOpacity": 0},
            tooltip=folium.GeoJsonTooltip(fields=["DISTRITO", "PROVINCIA"], aliases=["Distrito", "Provincia"]),
        ).add_to(m)

    vmin, vmax = float(demand_map[time_col].min()), float(demand_map[time_col].max())
    colormap = bcm.LinearColormap(SEQUENTIAL_BLUE, vmin=vmin, vmax=vmax, caption=f"Tiempo de acceso - {MODE_LABELS[filters['profile']]} (min)")
    colormap.add_to(m)

    demand_layer = folium.FeatureGroup(name="Centros poblados (demanda)")
    for _, row in demand_map.iterrows():
        folium.CircleMarker(
            location=[row.geometry.y, row.geometry.x],
            radius=4,
            color=colormap(row[time_col]),
            fill=True,
            fill_color=colormap(row[time_col]),
            fill_opacity=0.85,
            weight=0.5,
            popup=(
                f"<b>{row['NOM_POBLAD']}</b><br>{row['DIST']}, {row['PROV']}, {row['DEP']}<br>"
                f"Población: {row[metrics.POP_COL]:,.0f}<br>{MODE_LABELS[filters['profile']]}: {row[time_col]:.1f} min"
            ),
        ).add_to(demand_layer)
    demand_layer.add_to(m)

    facility_layer = folium.FeatureGroup(name="Establecimientos resolutivos")
    for _, row in resolutivos_scope.iterrows():
        folium.Marker(
            location=[row.geometry.y, row.geometry.x],
            icon=folium.Icon(color="red", icon="plus-sign"),
            popup=f"<b>{row['NOMBRE']}</b><br>{row['DISTRITO']}, {row['PROVINCIA']}<br>Categoría: {row['categoria_normalizada']}",
        ).add_to(facility_layer)
    facility_layer.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)
    st.caption(
        f"{len(demand_map):,} centros poblados | {len(resolutivos_scope):,} establecimientos resolutivos | "
        f"{len(distritos_scope):,} distritos en el área mostrada"
    )
    st_folium(m, use_container_width=True, height=620, returned_objects=[])


# ---------------------------------------------------------------------------
# 5. Tab 3 — Desigualdad y perfiles
# ---------------------------------------------------------------------------

def render_tab_inequality(demand: gpd.GeoDataFrame, filters: dict[str, Any]) -> None:
    time_col = f"{filters['profile']}_duration_min"

    if demand.empty:
        st.warning("No hay puntos de demanda para el filtro seleccionado.")
        return

    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Curva de Lorenz")
        lorenz = metrics.lorenz_curve(demand[time_col], demand[metrics.POP_COL])
        gini = metrics.gini_coefficient(demand[time_col], demand[metrics.POP_COL])

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=[0, 100], y=[0, 100], mode="lines", line=dict(dash="dash", color="#c3c2b7"), name="Igualdad perfecta"))
        fig.add_trace(go.Scatter(
            x=lorenz["poblacion_acumulada_pct"], y=lorenz["tiempo_acumulado_pct"], mode="lines",
            line=dict(color=CATEGORICAL["ICA"], width=3), fill="tonexty", name="Curva de Lorenz",
        ))
        fig.update_layout(
            xaxis_title="% de población acumulada", yaxis_title="% de tiempo de acceso acumulado",
            plot_bgcolor="#fcfcfb", paper_bgcolor="#fcfcfb", showlegend=True,
        )
        st.plotly_chart(fig, width="stretch")

        gini_ref = load_output_table("gini_summary")
        gini_nacional = (
            gini_ref.loc[gini_ref["ambito"] == "NACIONAL (3 deptos)", "gini"].iloc[0]
            if not gini_ref.empty else None
        )
        mc1, mc2 = st.columns(2)
        mc1.metric("Gini (filtro actual, auto)", f"{gini:.3f}")
        mc2.metric("Gini nacional de referencia (Fase 3)", f"{gini_nacional:.3f}" if gini_nacional is not None else "n/d")
        st.caption(
            "Gini aplicado a un COSTO (minutos), no a un bien: valores altos indican que la carga "
            "de tiempo de traslado está muy desigualmente repartida entre la población. La referencia "
            "nacional viene de la tabla precalculada en Fase 3 (perfil auto, sin filtros)."
        )

    with col_right:
        st.subheader("Urbano vs. Rural")
        ur = metrics.compare_urban_rural(demand, time_col=time_col)
        fig = px.bar(
            ur["summary"], x="grupo", y="tiempo_medio_ponderado_min", color="grupo",
            color_discrete_map={"Urbano": CATEGORICAL["CUSCO"], "Rural": CATEGORICAL["LORETO"]},
            labels={"tiempo_medio_ponderado_min": "Minutos (ponderado)", "grupo": ""},
            title="Tiempo de acceso ponderado: urbano vs. rural",
        )
        fig.update_layout(showlegend=False, plot_bgcolor="#fcfcfb", paper_bgcolor="#fcfcfb")
        st.plotly_chart(fig, width="stretch")
        p_value = ur["test"]["p_value"]
        st.metric("Mann-Whitney U — p-value", f"{p_value:.4f}" if pd.notna(p_value) else "n/d")
        st.caption(
            "p < 0.05: la diferencia urbano/rural es estadísticamente significativa "
            "(prueba no paramétrica, apropiada para distribuciones sesgadas de tiempos de viaje)."
        )

    st.divider()
    st.subheader("Comparativa entre perfiles de transporte")
    profile_rows = []
    for mode, label in MODE_LABELS.items():
        col = f"{mode}_duration_min"
        profile_rows.append({
            "perfil": label,
            "tiempo_medio_ponderado_min": metrics.weighted_mean(demand[col], demand[metrics.POP_COL]),
        })
    profile_df = pd.DataFrame(profile_rows)
    fig = px.bar(
        profile_df, x="perfil", y="tiempo_medio_ponderado_min",
        labels={"tiempo_medio_ponderado_min": "Minutos (ponderado)", "perfil": ""},
        color_discrete_sequence=[CATEGORICAL["ICA"]],
        title="Tiempo de acceso ponderado por perfil de transporte (área filtrada actual)",
    )
    fig.update_layout(plot_bgcolor="#fcfcfb", paper_bgcolor="#fcfcfb")
    st.plotly_chart(fig, width="stretch")


# ---------------------------------------------------------------------------
# 6. Tab 4 — Priorización y simulación de impacto
# ---------------------------------------------------------------------------

def render_tab_priorization(demand: gpd.GeoDataFrame, filters: dict[str, Any]) -> None:
    time_col = f"{filters['profile']}_duration_min"

    if demand.empty:
        st.warning("No hay puntos de demanda para el filtro seleccionado.")
        return

    st.subheader("Top 10 distritos más críticos, por departamento")
    by_district = metrics.aggregate_access_time(demand, ["DEP", "PROV", "DIST"], time_col=time_col)
    critical = metrics.rank_critical_districts(by_district, n=10)
    st.dataframe(
        critical.rename(columns={
            "DEP": "Departamento", "PROV": "Provincia", "DIST": "Distrito",
            "poblacion_total": "Población", "tiempo_medio_ponderado_min": "Tiempo ponderado (min)",
            "n_puntos": "N° centros poblados", "ranking": "Ranking",
        }),
        width="stretch", hide_index=True,
    )

    st.divider()
    st.subheader("Centros poblados aislados (> 120 min de acceso)")
    aislados = demand[demand[time_col] > 120].copy()
    search = st.text_input("Buscar por nombre de centro poblado", "")
    if search:
        aislados = aislados[aislados["NOM_POBLAD"].str.contains(search, case=False, na=False)]

    aislados = aislados.sort_values(time_col, ascending=False)
    st.caption(f"{len(aislados):,} centros poblados aislados en el área filtrada (perfil: {MODE_LABELS[filters['profile']]}).")
    st.dataframe(
        aislados[["NOM_POBLAD", "DIST", "PROV", "DEP", metrics.POP_COL, time_col, f"{filters['profile']}_is_unroutable"]].rename(columns={
            "NOM_POBLAD": "Centro poblado", "DIST": "Distrito", "PROV": "Provincia", "DEP": "Departamento",
            metrics.POP_COL: "Población", time_col: "Tiempo de acceso (min)",
            f"{filters['profile']}_is_unroutable": "Estimado (sin ruta en red)",
        }),
        width="stretch", hide_index=True,
    )


# ---------------------------------------------------------------------------
# 7. Main
# ---------------------------------------------------------------------------

def main() -> None:
    st.title("🏥 Golden Hour — Accesibilidad a Salud Resolutiva en Perú")
    st.caption("Ica (Costa) · Cusco (Sierra) · Loreto (Selva) — Fases 1-4 del proyecto integrador")

    demand_full = load_demand()
    facilities_full = load_facilities()
    distritos_full = load_distritos()

    filters = render_sidebar(demand_full)
    demand_filtered = apply_filters(demand_full, filters)

    tab1, tab2, tab3, tab4 = st.tabs([
        "📊 Visión General", "🗺️ Explorador Espacial", "⚖️ Desigualdad y Perfiles", "🎯 Priorización",
    ])
    with tab1:
        render_tab_overview(demand_filtered, facilities_full, filters)
    with tab2:
        render_tab_map(demand_filtered, facilities_full, distritos_full, filters)
    with tab3:
        render_tab_inequality(demand_filtered, filters)
    with tab4:
        render_tab_priorization(demand_filtered, filters)


main()

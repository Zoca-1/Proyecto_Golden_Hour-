# Golden Hour — Configuración del Proyecto

Proyecto integrador para evaluar la **accesibilidad a salud resolutiva en Perú**
("Golden Hour": tiempo máximo de traslado tolerable a un establecimiento de
salud con capacidad resolutiva quirúrgica/especializada).

Este archivo es la **única fuente de verdad** de configuración del proyecto.
Todo el código en `src/` debe leer sus parámetros desde aquí a través de
`src/utils.py::load_config()` (y las funciones `get_*` derivadas) — no se
deben hardcodear rutas, departamentos, categorías ni umbrales en el código.

## Parámetros

```yaml
departments:
  costa:
    name: Ica
    region_type: Costa
  sierra:
    name: Cusco
    region_type: Sierra
  selva:
    name: Loreto
    region_type: Selva

paths:
  data_raw: data/raw
  data_processed: data/processed
  data_outputs: data/outputs
  report_figures: report/figures
  logs: logs

health_facility_categories:
  - "II-1"
  - "II-2"
  - "II-E"
  - "III-1"
  - "III-2"
  - "III-E"

peru_bbox:
  lon_min: -81.4
  lon_max: -68.6
  lat_min: -18.4
  lat_max: -0.04

routing:
  engine: osrm_docker   # osrm_docker | osmnx_local
  osrm:
    host: http://localhost:5000
    profile: driving
  osmnx:
    network_type: drive
    simplify: true

golden_hour:
  threshold_minutes: 60

crs:
  geographic: "EPSG:4326"
  projected: "EPSG:32718"   # UTM 18S; revisar huso adecuado por departamento

routing_execution:
  max_demand_points: 5000
  detour_factor: 1.4          # multiplicador sobre distancia Haversine cuando no hay ruta en red
  fallback_speeds_kmh:        # usadas solo para el fallback Haversine (no-red / no-ruteable)
    car: 40
    foot: 5
    bike: 15
  osmnx_mode_speeds_kmh:      # velocidad asumida sobre el grafo 'walk' (no hay tags maxspeed para foot/bike)
    foot: 5
    bike: 15
  max_snap_distance_km: 2.0   # si el nodo vial más cercano está más lejos que esto, se considera no-snappable
  osrm_ping_timeout_seconds: 3
  osmnx_graph_cache_dir: "data/raw/osmnx_cache"
  graph_bbox_buffer_deg: 0.08
  max_graph_bbox_area_deg2: 5.0   # por encima de esto, Overpass público no es práctico -> Haversine directo
  random_seed: 42

data_sources:
  ckan_base_url: "https://www.datosabiertos.gob.pe"

  renipress:
    description: "RENIPRESS — Registro Nacional de IPRESS (SUSALUD/MINSA), vía Plataforma Nacional de Datos Abiertos"
    ckan_package_id: "registro-nacional-de-entidades-prestadoras-de-servicios-de-salud-renipress"
    fallback_url: "https://www.datosabiertos.gob.pe/sites/default/files/RENIPRESS_31-08-2026.csv"
    target_filename: "renipress.csv"
    delimiter: ";"
    encoding_candidates: ["utf-8-sig", "latin-1"]
    lat_column: "NORTE"
    lon_column: "ESTE"
    code_column: "COD_IPRESS"
    category_column: "CATEGORIA"
    department_column: "DEPARTAMENTO"
    ubigeo_column: "UBIGEO"

  centros_poblados:
    # Sustituto de MINEDU/SIGMED: el portal sigmed.minedu.gob.pe/descargas no expone
    # una URL de archivo estática -- el botón "Descarga Centros Poblados" dispara un
    # postback ASP.NET parametrizado por departamento/provincia/distrito, sin endpoint
    # fijo verificable programáticamente. Se usa en su lugar el dataset oficial y
    # equivalente del IGN ("Centros Poblados" a nivel nacional) publicado en el mismo
    # portal de datos abiertos que RENIPRESS y los límites administrativos.
    description: "Centros Poblados (IGN), vía Plataforma Nacional de Datos Abiertos — sustituye a MINEDU/SIGMED (sin URL estática)"
    direct_url: "https://www.datosabiertos.gob.pe/sites/default/files/CCPP_0.zip"
    target_filename: "centros_poblados.zip"

  limites_administrativos:
    departamentos:
      description: "Límites Departamentales (IGN)"
      direct_url: "https://www.datosabiertos.gob.pe/sites/default/files/DEPARTAMENTOS_LIMITES.zip"
      target_filename: "limites_departamentos.zip"
    provincias:
      description: "Límites Provinciales (IGN)"
      direct_url: "https://www.datosabiertos.gob.pe/sites/default/files/PROVINCIALES_LIMITES.zip"
      target_filename: "limites_provincias.zip"
    distritos:
      description: "Límites Distritales (IGN)"
      direct_url: "https://www.datosabiertos.gob.pe/sites/default/files/DISTRITOS_LIMITES.zip"
      target_filename: "limites_distritos.zip"

  centros_poblados_inei_censo:
    # Fuente ÚNICA de puntos de demanda (reemplaza a `centros_poblados` (IGN)
    # para este propósito, en los 3 departamentos, no solo Loreto): el
    # dataset IGN no trae población -indispensable para la ponderación por
    # habitantes de Fase 3- y además tiene un defecto de coordenadas que
    # invalida el 100% de sus registros de Loreto (Y duplicada con X,
    # detectado en Fase 1). Esta capa INEI (censo 1999/2002) sí trae
    # población real (TOT_POB99) y clasificación urbano/rural oficial
    # (CLASIF02) de forma consistente en los 3 departamentos configurados.
    # `department_field`/`province_field` se consultan dinámicamente contra
    # los departamentos de `departments` (arriba) — no se listan aquí para
    # no duplicar esa fuente de verdad.
    description: "Centros Poblados INEI (censo 1999/2002) vía ArcGIS REST de INGEMMET/GEOCATMIN — fuente única de demanda"
    arcgis_query_url: "https://geocatmin.ingemmet.gob.pe/arcgis/rest/services/SERV_OTRAS_FUENTES/MapServer/20/query"
    department_field: "NOMBDD02"
    province_field: "NOMBPV02"
    out_fields: "NOMBDD02,NOMBPV02,NOMBDI02,NOMCCPP02,NOMCAT02,CLASIF02,TOT_POB99,CODCCPP02,CCDI02,X_COORD,Y_COORD"
    target_filename: "centros_poblados_inei_censo.csv"
    # El servidor geocatmin.ingemmet.gob.pe no envía la cadena de certificados
    # TLS intermedia completa (falla `unable to get local issuer certificate`
    # en cualquier cliente HTTPS estándar, no solo el nuestro). Verificado
    # manualmente que el contenido servido es el correcto; se omite la
    # verificación TLS ÚNICAMENTE para este host y solo para datos públicos
    # de solo lectura (censo de centros poblados), nunca para credenciales.
    insecure_ssl: true
```

## Notas de diseño

- **Departamentos**: uno por región natural (Costa/Sierra/Selva) para capturar
  la heterogeneidad geográfica y orográfica en el acceso a salud. Cambiar
  aquí basta para que todo el pipeline (descarga, procesamiento, ruteo,
  reporte) opere sobre otro departamento.
- **Categorías resolutivas**: establecimientos de nivel II y III (capacidad
  quirúrgica o de mayor especialización), según la categorización normativa
  del MINSA. Cualquier categoría fuera de esta lista blanca debe excluirse
  al filtrar el registro de establecimientos (IPRESS).
- **Bounding box de Perú**: usado para validar/descartar geometrías o puntos
  fuera del territorio continental durante la limpieza de datos.
- **Motor de ruteo** (`routing.engine`):
  - `osrm_docker`: requiere un contenedor OSRM corriendo localmente
    (ver el servicio expuesto en `routing.osrm.host`). Más rápido para
    grafos viales grandes.
  - `osmnx_local`: calcula el grafo vial en memoria con OSMnx, sin
    dependencias externas, pero más lento y limitado por RAM.
- **`golden_hour.threshold_minutes`**: umbral (en minutos) que define si un
  centro poblado está dentro o fuera de la "hora dorada" respecto al
  establecimiento resolutivo más cercano.
- **CRS**: `geographic` para lectura/visualización, `projected` para
  cálculos de distancia/área en metros.
- **`data_sources`**: todas las URLs de descarga usadas por
  `src/acquisition.py`. `renipress` resuelve dinámicamente el CSV del mes
  más reciente vía la API CKAN del portal (`ckan_package_id`), con
  `fallback_url` como respaldo si la API no responde. `centros_poblados`
  (IGN) se conserva como referencia/comparación, pero **no** es la fuente de
  demanda usada por `validate_centros_poblados`: no trae población (necesaria
  para la ponderación por habitantes de Fase 3) y tiene un defecto que
  invalida el 100% de sus registros de Loreto (Y duplicada con X, detectado
  en Fase 1). La fuente de demanda real es `centros_poblados_inei_censo`
  (censo INEI 1999/2002, vía INGEMMET/GEOCATMIN): misma capa nacional
  consultada para los 3 departamentos configurados de forma consistente,
  con población real (`TOT_POB99`) y clasificación urbano/rural oficial
  (`CLASIF02`) en todos los casos — ver
  `src/validation.py::validate_centros_poblados`. Todas las URLs fueron
  verificadas manualmente (HTTP 200, tipo de contenido y tamaño coherentes)
  antes de fijarlas aquí.
- **`routing_execution`** (Fase 2, `src/routing.py`): parámetros del cálculo
  de matrices de ruteo. `max_demand_points` es el techo de puntos de demanda
  antes de activar muestreo estratificado por distrito. `detour_factor` y
  `fallback_speeds_kmh` solo se usan cuando un punto no puede ruteares por
  la red (sin snap válido o red desconectada): se estima con distancia
  Haversine × `detour_factor` y la velocidad de respaldo del modo.
  `osmnx_mode_speeds_kmh` aplica sobre el grafo 'walk' del fallback OSMnx
  para foot/bike, ya que esas vías no traen tags `maxspeed` en OSM (a
  diferencia de 'drive', donde se usa la velocidad real de OSM vía
  `osmnx.routing.add_edge_speeds`). OSRM (cuando está disponible) solo se usa
  para el perfil 'car', porque el contenedor Docker de `routing.osrm` expone
  un único perfil ('driving'); foot y bike siempre usan el fallback OSMnx.
  `max_graph_bbox_area_deg2`: los departamentos grandes/dispersos (Cusco,
  Loreto) pueden generar un bbox de decenas de grados² una vez que el
  muestreo estratificado dispersa puntos por muchos distritos; descargar eso
  vía la API pública de Overpass genera cientos de sub-consultas y agota el
  tiempo de espera (probado empíricamente). Por encima de este umbral se usa
  directamente el fallback Haversine × `detour_factor` en vez de intentar (y
  fallar) la descarga — este es precisamente el escenario para el que OSRM
  (extractos OSM locales, sin este límite) está pensado como motor primario.

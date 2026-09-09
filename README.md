# Golden Hour

**Evaluación de la accesibilidad espacial a salud resolutiva en Perú**

Proyecto integrador de ciencia de datos geoespacial que mide, con datos
abiertos del Estado peruano, cuánto tiempo le toma a cada centro poblado de
**Ica** (Costa), **Cusco** (Sierra) y **Loreto** (Selva) llegar al
establecimiento de salud resolutivo (nivel II/III, MINSA) más cercano — el
concepto de *golden hour* ("hora dorada") aplicado a la geografía peruana.

El pipeline es reproducible de extremo a extremo: descarga y valida datos
abiertos, calcula matrices de tiempo de viaje (auto, caminata, bicicleta),
construye indicadores de accesibilidad y desigualdad ponderados por
población, los expone en un tablero interactivo, y compila un reporte
académico final en LaTeX. Cada decisión técnica no trivial (sustituciones de
fuente de datos, obstáculos de adquisición, límites del motor de ruteo) está
documentada en `config.md` y en el reporte final.

## Estructura del repositorio

```
golden_hour/
├── config.md                  # Única fuente de verdad de configuración (parámetros, rutas, fuentes de datos)
├── requirements.txt           # Dependencias de Python del proyecto
├── src/                       # Código del pipeline, un módulo por fase
│   ├── utils.py               #   Fase 0 — lee config.md y expone accesores tipados al resto de módulos
│   ├── acquisition.py         #   Fase 1 — descarga/cachea las fuentes de datos crudos en data/raw/
│   ├── validation.py          #   Fase 1 — valida y exporta GeoParquet limpios a data/processed/
│   ├── routing.py             #   Fase 2 — matrices de tiempo de viaje (OSRM/OSMnx + fallback Haversine)
│   ├── metrics.py             #   Fase 3 — indicadores de accesibilidad/desigualdad (funciones puras)
│   ├── export.py              #   Fase 3 — exporta tablas (CSV/LaTeX) y figuras para el reporte
│   └── dashboard.py           #   Fase 4 — tablero interactivo Streamlit
├── data/
│   ├── raw/                   # Datos crudos descargados (cacheados; no versionado salvo .gitkeep)
│   ├── processed/             # GeoParquet validados y enriquecidos (demand_routed.parquet, etc.)
│   └── outputs/                # Tablas finales de métricas, en CSV y LaTeX (.tex, con booktabs)
├── report/
│   ├── main.tex                # Reporte final en LaTeX (fuente)
│   ├── main.pdf                # Reporte final compilado
│   └── figures/                 # Figuras vectoriales (PDF) y PNG de alta resolución para el reporte/dashboard
└── logs/                       # Logs de ejecución por módulo + data_quality_report.csv (Fase 1)
```

- **`src/`** — un módulo de Python por fase del pipeline; cada uno es
  ejecutable de forma independiente (`python src/<modulo>.py`) además de
  usarse como librería desde los demás.
- **`data/`** — separación estándar *raw → processed → outputs*: `raw/` es
  descartable y se re-descarga con `acquisition.py`; `processed/` son los
  GeoParquet ya validados/enriquecidos que consumen `routing.py`,
  `metrics.py` y `dashboard.py`; `outputs/` son las tablas finales listas
  para el reporte y el dashboard.
- **`report/`** — el reporte académico final (fuente LaTeX + PDF compilado)
  y las figuras que consume vía `\includegraphics`.
- **`logs/`** — un archivo de log por módulo (`acquisition.log`,
  `validation.log`, `routing.log`, `metrics.log`, `export.log`) más
  `data_quality_report.csv`, el detalle de cada regla de validación aplicada
  en Fase 1 (registros afectados, acción tomada, justificación).
- **`config.md`** — toda la configuración del proyecto (departamentos,
  rutas, categorías resolutivas, bounding box, fuentes de datos, parámetros
  de ruteo y muestreo) en un único bloque YAML embebido, leído por
  `src/utils.py`. Cambiar un parámetro aquí basta para que todo el pipeline
  lo respete — no hay valores hardcodeados en el código.
- **`requirements.txt`** — todas las dependencias de Python del proyecto
  (geoespacial, ruteo, estadística, dashboard, reporte).

## Requisitos previos e instalación

- Python 3.11+ (probado con 3.14).
- Una distribución de LaTeX con `pdflatex` si se desea recompilar el reporte
  (p.ej. [MiKTeX](https://miktex.org/) en Windows o TeX Live en Linux/macOS;
  se requieren los paquetes `booktabs`, `graphicx`, `babel` y `url`, que la
  mayoría de distribuciones instalan automáticamente al compilar).
- (Opcional) [Docker](https://www.docker.com/) si se quiere levantar un
  contenedor OSRM local para ruteo por red vial real — ver la nota sobre
  datos precalculados más abajo.

Crear un entorno virtual e instalar las dependencias:

```bash
# Desde la raíz del repositorio
python -m venv .venv

# Activar el entorno virtual
.venv\Scripts\activate        # Windows (PowerShell/cmd)
source .venv/bin/activate     # Linux/macOS

# Instalar dependencias del proyecto
pip install -r golden_hour/requirements.txt
```

Todos los comandos de las siguientes secciones se ejecutan **desde la
carpeta `golden_hour/`**, salvo que se indique lo contrario:

```bash
cd golden_hour
```

## Guía de ejecución reproducible por fases

El pipeline está diseñado para ejecutarse en orden y es **reejecutable de
forma segura**: cada fase cachea o valida antes de recomputar, así que
correr un script dos veces no duplica trabajo ni datos.

### 1. Ingesta y validación de datos (Fase 1)

```bash
python src/acquisition.py    # Descarga y cachea las fuentes en data/raw/
python src/validation.py     # Valida, limpia y exporta GeoParquet a data/processed/
```

Genera `data/processed/renipress_validado.parquet`,
`data/processed/centros_poblados_validado.parquet` y
`logs/data_quality_report.csv` (detalle de cada regla de calidad aplicada).

### 2. Cálculo de matrices de ruteo (Fase 2)

```bash
python src/routing.py
```

Genera `data/processed/routing_matrix.parquet` (matriz completa
demanda × establecimientos resolutivos) y
`data/processed/demand_routed.parquet` (dataset de demanda enriquecido con
tiempos/distancias en auto, caminata y bicicleta). **Este paso tiene caching
estricto**: si ambos archivos ya existen, no se recalculan rutas — hay que
borrarlos manualmente para forzar un recálculo.

### 3. Construcción de métricas y exportación (Fase 3)

```bash
python src/export.py
```

Calcula todos los indicadores (bandas de cobertura, tiempos ponderados por
población, ranking de brechas críticas, Gini/Lorenz, contraste
urbano/rural, cruce con índice de ruralidad) vía las funciones puras de
`src/metrics.py`, y exporta las tablas a `data/outputs/*.csv` / `*.tex` y
las figuras a `report/figures/*.pdf` / `*.png`.

### 4. Tablero interactivo (Fase 4)

```bash
streamlit run src/dashboard.py
```

Abre el tablero en el navegador (por defecto en `http://localhost:8501`).
Los filtros de departamento/provincia/distrito, perfil de transporte y
umbral de *Golden Hour* recalculan las métricas **en vivo** sobre los datos
ya procesados, reutilizando `src/metrics.py` — no dependen de volver a
correr `routing.py`.

### 5. Compilación del reporte final (Fase 5)

```bash
cd report
pdflatex -interaction=nonstopmode main.tex
pdflatex -interaction=nonstopmode main.tex   # 2da pasada: resuelve índice y referencias cruzadas
```

Genera `report/main.pdf`. Se recomienda correr `pdflatex` dos veces para
que el índice y las referencias a tablas/figuras (`\ref{}`) queden
resueltas correctamente en la primera compilación desde cero.

## Nota sobre datos precalculados

El repositorio incluye **`data/processed/demand_routed.parquet`,
`data/processed/routing_matrix.parquet`** y las tablas de
`data/outputs/` ya calculados y versionados. Esto permite:

- Ejecutar `streamlit run src/dashboard.py` **inmediatamente** después de
  clonar el repositorio e instalar dependencias, sin necesidad de volver a
  correr todo el pipeline de ingesta/ruteo ni de levantar un contenedor
  OSRM local.
- Recompilar `report/main.pdf` sin depender de que `src/export.py` se haya
  ejecutado en esa misma sesión.

**Motor de ruteo usado en los datos precalculados:** `src/routing.py` usa
OSRM (Docker local) como motor primario solo para el perfil de auto, con
*fallback* automático a OSMnx/NetworkX cuando OSRM no está disponible. En
el entorno donde se generaron estos archivos no había un contenedor OSRM
activo, y el muestreo estratificado de puntos de demanda dispersa el área a
cubrir por departamento más allá de lo que la API pública de Overpass
puede resolver en un tiempo razonable (ver `config.md ->
routing_execution.max_graph_bbox_area_deg2` y la Sección de Metodología de
`report/main.pdf`). En consecuencia, **el 100 % de los tiempos de viaje en
los datos precalculados provienen del *fallback* de distancia Haversine ×
factor de desvío (1.4)**, no de rutas reales sobre la red vial — una cota
aproximada y conservadora, no una medición de tiempos de viaje verificados.

Para obtener tiempos de viaje reales por red vial, levantar un contenedor
OSRM local con un extracto OSM de Perú (ver `routing.osrm.host` en
`config.md`), borrar `data/processed/routing_matrix.parquet` y
`data/processed/demand_routed.parquet`, y volver a correr
`python src/routing.py`.

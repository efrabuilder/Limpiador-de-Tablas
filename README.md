# Limpiador de Tablas

Herramienta en Python para **detectar y corregir problemas de calidad de datos**
en tablas provenientes de CSV, Excel o bases de datos SQL, con un **reporte
detallado** de todo lo encontrado y del archivo limpio resultante.

## ¿Qué detecta?

| Tipo            | Descripción                                                        |
|-----------------|---------------------------------------------------------------------|
| `faltante`      | Celdas vacías / nulas                                               |
| `duplicado`     | Filas completas repetidas                                           |
| `tipo_invalido` | Texto dentro de una columna que debería ser numérica (ej. "abc")    |
| `atipico`       | Valores atípicos (outliers) por método **IQR** y/o **Z-score**      |

## ¿Qué se puede hacer con cada hallazgo?

Para cada tipo de problema, usted elige la acción a aplicar:

- `reemplazar_media` / `reemplazar_mediana` / `reemplazar_moda`
- `limitar` (winsorizing: recorta el atípico al límite válido más cercano)
- `valor_fijo` (usted define el valor de reemplazo)
- `eliminar_fila`
- `marcar_solo` (no modifica el dato, solo queda registrado en el reporte)

## Instalación

```bash
pip install -r requirements.txt
```

## Interfaces disponibles

El proyecto ofrece varias formas de usar la misma lógica de `data_cleaner/`,
según el caso de uso:

| Interfaz | Archivo | Cuándo usarla |
|---|---|---|
| 🌐 Web (Streamlit) | `app.py` | Uso general, sin instalar nada extra en el navegador. |
| 💻 Escritorio (Tkinter) | `desktop_app.py` | Ventana nativa, sin servidor ni navegador. |
| ⌨️ CLI por flags | `cli.py` | Automatización: scripts, cron, CI/CD. |
| 🔌 API REST (FastAPI) | `api.py` | Integrarlo en otra app o un frontend propio. |
| 📓 Notebook | `notebook_interactivo.ipynb` | Uso analítico/educativo, paso a paso con gráficos. |
| ⌨️ CLI interactiva | `main.py` | Uso manual guiado por preguntas (ver abajo). |

### 🌐 Interfaz web (Streamlit)

```bash
streamlit run app.py
```

Se abre en el navegador. Permite subir un CSV/Excel (o usar los datos de
ejemplo), analizar la tabla, elegir la acción para cada tipo de hallazgo
desde menús desplegables, y descargar directamente los datos limpios
(CSV/Excel) y el reporte de calidad, sin tocar la terminal.

### 💻 Interfaz de escritorio (Tkinter)

```bash
python desktop_app.py
```

Ventana nativa (no requiere navegador ni conexión a internet). Requiere
tener Tk instalado en el sistema — en Debian/Ubuntu:
`sudo apt-get install python3-tk` (en Windows/Mac ya viene incluido con
Python). Botón para abrir archivo, pestañas de Datos/Hallazgos, menús
desplegables por tipo de problema y botón para guardar los resultados en
una carpeta.

### ⌨️ CLI por flags (automatización)

Pensada para correr sin preguntas, ideal para scripts o pipelines:

```bash
# Solo analizar (no modifica nada)
python cli.py analizar --input ejemplo_datos.csv --metodo-atipicos ambos

# Analizar + limpiar + exportar, todo en un comando
python cli.py limpiar --input ejemplo_datos.csv --outdir salida \
    --faltante reemplazar_mediana --duplicado eliminar_fila \
    --atipico limitar --tipo-invalido marcar_solo --formato-salida excel

# Con valor fijo de reemplazo (columna=valor, repetible)
python cli.py limpiar --input ejemplo_datos.csv --outdir salida \
    --faltante valor_fijo --valor-fijo edad=0 --valor-fijo salario=0
```

Ver todas las opciones con `python cli.py limpiar --help`.

### 🔌 API REST (FastAPI)

```bash
uvicorn api:app --reload
```

Documentación interactiva en `http://localhost:8000/docs`. Endpoints:

- `POST /analizar` — sube un archivo, devuelve el resumen de hallazgos en JSON.
- `POST /limpiar` — sube un archivo + configuración, devuelve un `id` de resultado.
- `GET /descargar/{id}/datos` y `GET /descargar/{id}/reporte` — descargan los
  archivos Excel generados por `/limpiar`.

Ejemplo con `curl`:

```bash
curl -X POST http://localhost:8000/limpiar \
  -F "archivo=@ejemplo_datos.csv" \
  -F "tipo_invalido=valor_fijo" \
  -F 'valores_fijos={"salario":"0"}'
```

### 📓 Notebook interactivo (Jupyter)

```bash
jupyter notebook notebook_interactivo.ipynb
```

Widgets (botones, menús desplegables, campos de texto) para cargar datos,
analizar, configurar la corrección por tipo de hallazgo y descargar el
resultado — con gráfico de hallazgos por tipo incluido. Pensado para uso
analítico o educativo (EDA paso a paso).

### ⌨️ Uso por consola (interactivo, con preguntas)

```bash
python main.py
```

El programa le preguntará:
1. Tipo de fuente (`csv`, `excel` o `sql`) y su ruta/consulta.
2. Método de detección de atípicos (`iqr`, `zscore` o `ambos`).
3. Qué acción aplicar a cada tipo de problema encontrado.
4. Formato del archivo de salida (`csv` o `excel`).

Al finalizar obtendrá, dentro de la carpeta `salida/`:

- **`datos_limpios.xlsx` (o `.csv`)** — la tabla ya corregida, lista para descargar.
- **`reporte_calidad_datos.xlsx`** — reporte con dos hojas:
  - **Resumen**: cantidad analizada, hallazgos por tipo y por columna.
  - **Detalle_Hallazgos**: una fila por cada problema, indicando **columna,
    número de fila, valor original, acción aplicada y valor nuevo**.

## Modo demo (sin preguntas, con datos de ejemplo)

```bash
python main.py --demo --input ejemplo_datos.csv --outdir salida
```

## Uso como librería en su propio script

```python
from data_cleaner import load_table, analizar, limpiar, construir_reporte, exportar_reporte_excel, exportar

df = load_table("mis_datos.xlsx", kind="excel")

resultado = analizar(df, metodo_atipicos="iqr")

config = {
    "faltante": "reemplazar_mediana",
    "duplicado": "eliminar_fila",
    "atipico": "limitar",
    "tipo_invalido": "marcar_solo",
}
df_limpio, registro = limpiar(df, resultado.issues, config=config)

tablas = construir_reporte(resultado, registro, nombre_fuente="mis_datos.xlsx")
exportar_reporte_excel(tablas, "reporte.xlsx")
exportar(df_limpio, "datos_limpios.xlsx", kind="excel")
```

## Fuentes y salidas SQL

Para leer de una base de datos:

```python
df = load_table("sqlite:///datos.db", kind="sql", table_name="empleados")
# o con una consulta personalizada:
df = load_table("sqlite:///datos.db", kind="sql", query="SELECT * FROM empleados WHERE activo=1")
```

Para escribir el resultado limpio de vuelta a SQL:

```python
from data_cleaner.exporters import exportar_sql
exportar_sql(df_limpio, "sqlite:///datos.db", table_name="empleados_limpios")
```

Compatible con SQLite, MySQL y PostgreSQL (vía SQLAlchemy) — solo cambia el
`connection_string`.

## Estructura del proyecto

```
data_cleaner/
├── data_cleaner/
│   ├── loaders.py     # Carga CSV / Excel / SQL
│   ├── analyzer.py    # Detección de faltantes, duplicados, tipo y atípicos
│   ├── cleaner.py      # Aplica las acciones de corrección elegidas
│   ├── report.py       # Genera el reporte (Resumen + Detalle)
│   └── exporters.py    # Exporta el archivo limpio (CSV/Excel/SQL)
├── app.py                       # Interfaz web (Streamlit)
├── desktop_app.py                # Interfaz de escritorio (Tkinter)
├── cli.py                        # CLI por flags (automatización)
├── api.py                        # API REST (FastAPI)
├── notebook_interactivo.ipynb    # Notebook interactivo (Jupyter + ipywidgets)
├── main.py                       # CLI interactiva (con preguntas)
├── ejemplo_datos.csv             # Datos de ejemplo para probar
└── requirements.txt
```

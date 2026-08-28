# Limpiador de Tablas

Herramienta en Python para **detectar y corregir problemas de calidad de datos**
en tablas provenientes de CSV, Excel o bases de datos SQL, con un **reporte
detallado** de todo lo encontrado y del archivo limpio resultante.

## ¿Qué detecta?

**Genéricos** (cualquier columna, siempre activos):

| Tipo            | Descripción                                                        |
|-----------------|---------------------------------------------------------------------|
| `faltante`      | Celdas vacías / nulas                                               |
| `duplicado`     | Filas completas repetidas                                           |
| `tipo_invalido` | Texto dentro de una columna que debería ser numérica (ej. "abc")    |
| `atipico`       | Valores atípicos (outliers) por método **IQR** y/o **Z-score**      |

**Reglas de negocio** (opcionales, se auto-detectan por nombre de columna
o se indican explícitamente — ver "Auto-detección de columnas" abajo):

| Tipo                   | Descripción                                                                 |
|------------------------|-------------------------------------------------------------------------------|
| `fecha_invalida`       | Fecha no parseable, o fuera de un rango `--fecha-min`/`--fecha-max` dado      |
| `email_invalido`       | No cumple el formato `usuario@dominio.tld`                                    |
| `telefono_invalido`    | Formato o cantidad de dígitos inválida (configurable, por defecto 8 dígitos)   |
| `id_duplicado`         | Valor repetido en una columna identificadora (ID, código, folio…), aunque el resto de la fila sea distinto — a diferencia de `duplicado`, que exige la fila completa igual |
| `formula_incorrecta`   | Una columna no coincide con el resultado de otras (ej. `Total ≠ Cantidad × Precio_Unitario`) |
| `texto_inconsistente`  | Variantes o errores de tipeo del mismo valor categórico (ej. "San Jose" / "san josé " / "SanJosé") en columnas de baja cardinalidad (categoría, vendedor, método de pago, ciudad…) |

### Auto-detección de columnas

Estas 6 reglas nuevas no vienen atadas a nombres de columna de un proyecto
en particular: si no se les indica una columna explícita, el analizador
intenta adivinarla por el nombre (`fecha`/`date`, `email`/`correo`,
`telefono`/`phone`, `id`/`codigo`/`folio`, `total`+`cantidad`+`precio`, y
columnas de texto de baja cardinalidad para variantes). Esto permite
reutilizar el mismo motor en datasets distintos sin tocar código. Se puede
apagar con `auto_detectar_columnas=False` (o `--sin-auto-columnas` en la
CLI) y pasar las columnas a mano.

```python
resultado = analizar(
    df,
    columnas_fecha=["Fecha_Venta"], fecha_min="2023-01-01", fecha_max="2025-12-31",
    columnas_email=["Email_Cliente"],
    columnas_telefono=["Telefono"], digitos_telefono=(8, 8),
    columnas_id=["ID_Venta"],
    columna_total="Total_Venta", columna_cantidad="Cantidad", columna_precio="Precio_Unitario",
    columnas_texto=["Categoria_Producto", "Vendedor", "Metodo_Pago"],
)
```

## ¿Qué se puede hacer con cada hallazgo?

Para cada tipo de problema, usted elige la acción a aplicar:

- `reemplazar_media` / `reemplazar_mediana` / `reemplazar_moda`
- `limitar` (winsorizing: recorta el atípico al límite válido más cercano)
- `valor_fijo` (usted define el valor de reemplazo)
- `usar_sugerido` (solo para `formula_incorrecta` y `texto_inconsistente`:
  usa el valor correcto/canónico que el propio analizador calculó — el
  total esperado, o la grafía más frecuente de ese texto)
- `eliminar_fila`
- `marcar_solo` (no modifica el dato; en la tabla limpia se agrega una
  columna `_revisar_calidad` que indica, por fila, qué problema(s) se
  detectaron y en qué columna — solo aparece cuando al menos un hallazgo
  usó esta acción)

Las 6 reglas nuevas quedan por defecto en `marcar_solo`: corregir un email,
teléfono o fecha "a ciegas" es riesgoso, así que el default solo las deja
señaladas en el reporte para revisión manual (excepto `formula_incorrecta`
y `texto_inconsistente`, donde si usted elige `usar_sugerido` sí existe un
valor de reemplazo calculable con confianza).

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

# Con las reglas de negocio nuevas (auto-detectadas por nombre de columna)
python cli.py limpiar --input ventas.xlsx --outdir salida \
    --formula-incorrecta usar_sugerido --texto-inconsistente usar_sugerido \
    --email-invalido marcar_solo --telefono-invalido marcar_solo

# Indicando las columnas a mano (útil si los nombres no son obvios)
python cli.py limpiar --input ventas.xlsx --outdir salida \
    --columnas-email Correo_Cliente --columnas-telefono Cel \
    --total Monto_Total --cantidad Unidades --precio Precio_Unit \
    --digitos-telefono 8-8 --fecha-min 2023-01-01 --fecha-max 2025-12-31
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

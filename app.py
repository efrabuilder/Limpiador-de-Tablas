#!/usr/bin/env python3
"""
Limpiador de Tablas — Interfaz web (Streamlit)
================================================
Interfaz gráfica para cargar una tabla (CSV/Excel), analizar su calidad,
elegir qué hacer con cada tipo de problema encontrado y descargar el
archivo limpio junto con el reporte de calidad de datos.

Uso:
    streamlit run app.py
"""
from __future__ import annotations

import io

import pandas as pd
import streamlit as st

from data_cleaner import (
    load_table,
    analizar,
    limpiar,
    DEFAULT_CONFIG,
    construir_reporte,
    exportar_reporte_excel,
    exportar,
)
from data_cleaner.loaders import load_excel
from data_cleaner.cleaner import ACCIONES_VALIDAS  # noqa: F401 (referencia)
from data_cleaner.exportador import (
    generar_script_powerbi, generar_script_universal, generar_editor_m,
)
from data_cleaner.exportador_m import generar_editor_m_puro

# --------------------------------------------------------------------------
# Configuración de página y constantes
# --------------------------------------------------------------------------

st.set_page_config(
    page_title="Limpiador de Tablas",
    page_icon="🧹",
    layout="wide",
)

OPCIONES_ACCION = {
    "faltante": ["reemplazar_mediana", "reemplazar_media", "reemplazar_moda",
                 "valor_fijo", "eliminar_fila", "marcar_solo"],
    "duplicado": ["eliminar_fila", "marcar_solo"],
    "atipico": ["limitar", "reemplazar_mediana", "reemplazar_media",
                "eliminar_fila", "marcar_solo"],
    "tipo_invalido": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "fecha_invalida": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "email_invalido": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "telefono_invalido": ["editar_individualmente", "eliminar_fila", "valor_fijo", "marcar_solo"],
    "id_duplicado": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "formula_incorrecta": ["usar_sugerido", "eliminar_fila", "valor_fijo", "marcar_solo"],
    "texto_inconsistente": ["usar_sugerido", "eliminar_fila", "valor_fijo", "marcar_solo"],
}

NOMBRES_TIPO = {
    "faltante": "Valores faltantes (vacíos/nulos)",
    "duplicado": "Filas duplicadas",
    "atipico": "Valores atípicos (outliers)",
    "tipo_invalido": "Errores de tipo (texto en columna numérica)",
    "fecha_invalida": "Fechas inválidas o fuera de rango",
    "email_invalido": "Correos electrónicos inválidos",
    "telefono_invalido": "Teléfonos con formato/longitud inválida",
    "id_duplicado": "IDs duplicados (columna identificadora)",
    "formula_incorrecta": "Total no coincide (Cantidad × Precio)",
    "texto_inconsistente": "Variantes / errores de tipeo en texto",
}

ICONOS_TIPO = {
    "faltante": "🕳️",
    "duplicado": "📑",
    "atipico": "📈",
    "tipo_invalido": "🔤",
    "fecha_invalida": "📅",
    "email_invalido": "📧",
    "telefono_invalido": "📞",
    "id_duplicado": "🆔",
    "formula_incorrecta": "🧮",
    "texto_inconsistente": "✏️",
}

NOMBRES_ACCION = {
    "reemplazar_media": "Reemplazar por la media",
    "reemplazar_mediana": "Reemplazar por la mediana",
    "reemplazar_moda": "Reemplazar por la moda",
    "valor_fijo": "Reemplazar por un valor fijo",
    "limitar": "Limitar al rango válido (winsorizing)",
    "usar_sugerido": "Usar el valor sugerido por el análisis",
    "eliminar_fila": "Eliminar la fila",
    "marcar_solo": "Solo marcar en el reporte (no modifica el dato)",
    "editar_individualmente": "Editar cada uno por separado (corregir dígito a dígito)",
}

# País(es) disponibles para el rango de dígitos de teléfono/celular (ver
# patrones.DIGITOS_TELEFONO_PAIS). Se usa tanto para el análisis inicial
# como para la generación del M puro, para no repetir la lista.
PAISES_TELEFONO_DISPONIBLES = {
    "Costa Rica": "cr", "México": "mexico", "Colombia": "colombia",
    "Argentina": "argentina", "España": "espana", "Estados Unidos": "us",
    "Panamá": "panama", "Guatemala": "guatemala", "Honduras": "honduras",
    "Nicaragua": "nicaragua", "El Salvador": "el_salvador", "Chile": "chile",
    "Perú": "peru", "Ecuador": "ecuador", "Venezuela": "venezuela",
    "Brasil": "brasil", "Uruguay": "uruguay", "Bolivia": "bolivia",
    "República Dominicana": "republica_dominicana", "Reino Unido": "reino_unido",
    "Alemania": "alemania", "Francia": "francia", "Canadá": "canada",
}


# --------------------------------------------------------------------------
# Estado de sesión
# --------------------------------------------------------------------------

def _reset_estado() -> None:
    for key in ("df", "resultado", "nombre_fuente", "df_limpio", "registro", "tablas_reporte",
                "config_aplicada", "valores_fijos_aplicados"):
        st.session_state.pop(key, None)


if "df" not in st.session_state:
    _reset_estado()


# --------------------------------------------------------------------------
# Cabecera
# --------------------------------------------------------------------------

st.title("🧹 Limpiador de Tablas")
st.caption(
    "Detecta y corrige problemas de calidad de datos en tablas CSV o Excel: "
    "valores faltantes, filas duplicadas, errores de tipo y valores atípicos."
)

# --------------------------------------------------------------------------
# Paso 1 — Carga de datos
# --------------------------------------------------------------------------

with st.sidebar:
    st.header("1. Cargar datos")
    origen = st.radio("Fuente", ["Archivo (CSV / Excel)", "Base de datos SQL", "Datos de ejemplo"], index=0)

    if origen == "Archivo (CSV / Excel)":
        archivo = st.file_uploader("Seleccione un archivo", type=["csv", "xlsx", "xls"])
        if archivo is not None:
            try:
                if archivo.name.lower().endswith(".csv"):
                    df_cargado = pd.read_csv(archivo)
                    clave_fuente = archivo.name
                else:
                    # Cada hoja de un Excel puede ser una tabla distinta
                    # (esquemas distintos, ej. un libro Power BI con varias
                    # tablas). Concatenar todo por defecto mezcla columnas
                    # que no existen entre sí y genera NaN falsos. Se pide
                    # elegir una hoja específica; "todas concatenadas" solo
                    # tiene sentido si de verdad es la misma tabla repartida.
                    hojas_disponibles = pd.ExcelFile(archivo).sheet_names
                    if len(hojas_disponibles) == 1:
                        hoja_elegida = hojas_disponibles[0]
                    else:
                        opciones_hoja = ["(elegir una hoja)"] + hojas_disponibles + \
                            ["Todas las hojas (concatenadas)"]
                        hoja_elegida = st.selectbox(
                            "Hoja a limpiar",
                            opciones_hoja,
                            help="Elija la hoja que quiere analizar. Use 'Todas las hojas' "
                                 "solo si son la misma tabla repartida entre varias hojas "
                                 "(mismas columnas); si son tablas distintas, concatenarlas "
                                 "genera valores faltantes falsos.",
                        )
                        if hoja_elegida == "(elegir una hoja)":
                            st.info("Seleccione una hoja para continuar.")
                            st.stop()

                    archivo.seek(0)
                    if hoja_elegida == "Todas las hojas (concatenadas)":
                        df_cargado = load_excel(archivo)
                    else:
                        df_cargado = load_excel(archivo, sheet_name=hoja_elegida)
                    clave_fuente = f"{archivo.name}::{hoja_elegida}"

                if st.session_state.get("nombre_fuente") != clave_fuente:
                    _reset_estado()
                    st.session_state.df = df_cargado
                    st.session_state.nombre_fuente = clave_fuente
            except Exception as exc:
                st.error(f"No se pudo leer el archivo: {exc}")

    elif origen == "Base de datos SQL":
        # load_sql (en data_cleaner/loaders.py) ya existe y usa SQLAlchemy;
        # esto solo arma la cadena de conexión y la muestra en la interfaz.
        # OJO: segun el motor elegido hace falta el driver correspondiente
        # instalado (psycopg2-binary para PostgreSQL, pymysql para MySQL,
        # pyodbc para SQL Server) — agregarlo a requirements.txt si falta.
        motor_sql = st.selectbox(
            "Motor de base de datos",
            ["PostgreSQL", "MySQL", "SQL Server", "SQLite", "Otra (cadena de conexión manual)"],
        )
        cadena_conexion = ""
        if motor_sql == "SQLite":
            ruta_sqlite = st.text_input("Ruta del archivo .db", value="")
            if ruta_sqlite:
                cadena_conexion = f"sqlite:///{ruta_sqlite}"
        elif motor_sql == "Otra (cadena de conexión manual)":
            cadena_conexion = st.text_input(
                "Cadena de conexión SQLAlchemy completa",
                type="password",
                help="Ej: mssql+pyodbc://usuario:clave@host/basedatos?driver=ODBC+Driver+17+for+SQL+Server",
            )
        else:
            _driver_por_motor = {
                "PostgreSQL": ("postgresql+psycopg2", "5432"),
                "MySQL": ("mysql+pymysql", "3306"),
                "SQL Server": ("mssql+pyodbc", "1433"),
            }
            driver_sql, puerto_defecto = _driver_por_motor[motor_sql]
            col_sql_a, col_sql_b = st.columns(2)
            with col_sql_a:
                host_sql = st.text_input("Host", value="localhost", key="host_sql")
                usuario_sql = st.text_input("Usuario", key="usuario_sql")
                basedatos_sql = st.text_input("Base de datos", key="basedatos_sql")
            with col_sql_b:
                puerto_sql = st.text_input("Puerto", value=puerto_defecto, key="puerto_sql")
                clave_sql = st.text_input("Contraseña", type="password", key="clave_sql")
            if usuario_sql and basedatos_sql and host_sql:
                cadena_conexion = f"{driver_sql}://{usuario_sql}:{clave_sql}@{host_sql}:{puerto_sql}/{basedatos_sql}"
                if motor_sql == "SQL Server":
                    cadena_conexion += "?driver=ODBC+Driver+17+for+SQL+Server"

        modo_fuente_sql = st.radio(
            "¿Cómo traer los datos?", ["Nombre de tabla", "Consulta SQL personalizada"],
            horizontal=True, key="modo_fuente_sql",
        )
        tabla_sql = query_sql = None
        if modo_fuente_sql == "Nombre de tabla":
            tabla_sql = st.text_input("Nombre de la tabla", key="tabla_sql")
        else:
            query_sql = st.text_area(
                "Consulta SQL (SELECT ...)", key="query_sql",
                help="Solo se ejecutan lecturas: se usa para traer los datos a limpiar, no modifica la base de datos.",
            )

        if st.button("🔌 Conectar y cargar"):
            if not cadena_conexion:
                st.error("Complete los datos de conexión.")
            elif not tabla_sql and not query_sql:
                st.error("Indique una tabla o una consulta SQL.")
            else:
                try:
                    df_cargado = load_table(
                        cadena_conexion, kind="sql", table_name=tabla_sql or None, query=query_sql or None,
                    )
                    clave_fuente = f"sql::{motor_sql}::{tabla_sql or 'consulta_personalizada'}"
                    if st.session_state.get("nombre_fuente") != clave_fuente:
                        _reset_estado()
                    st.session_state.df = df_cargado
                    st.session_state.nombre_fuente = clave_fuente
                    st.success(f"Se cargaron {len(df_cargado)} filas desde la base de datos.")
                except Exception as exc:
                    st.error(f"No se pudo conectar/cargar desde la base de datos: {exc}")

    else:
        if st.button("Cargar ejemplo_datos.csv"):
            _reset_estado()
            st.session_state.df = load_table("ejemplo_datos.csv", kind="csv")
            st.session_state.nombre_fuente = "ejemplo_datos.csv"

    st.divider()
    st.header("2. Método de atípicos")
    metodo_atipicos = st.selectbox(
        "Detección de valores atípicos",
        ["iqr", "zscore", "ambos"],
        format_func=lambda v: {"iqr": "IQR (rango intercuartílico)",
                                "zscore": "Z-score",
                                "ambos": "Ambos (fusionados)"}[v],
    )

    st.divider()
    st.header("3. Teléfono")
    st.caption("Rango de dígitos aceptado al analizar la columna de teléfono.")
    modo_telefono_sidebar = st.radio(
        "Rango de dígitos", ["Automático por país", "Rango manual"],
        horizontal=True, key="modo_telefono_sidebar",
    )
    digitos_telefono_sidebar = None
    paises_telefono_sidebar = None
    if modo_telefono_sidebar == "Automático por país":
        paises_sel_sidebar = st.multiselect(
            "País(es) del teléfono",
            options=list(PAISES_TELEFONO_DISPONIBLES.keys()),
            default=["Costa Rica"],
            help="Vacío = rango internacional amplio (7-15 dígitos, estándar E.164). "
                 "Con varios países, se acepta la UNIÓN de sus rangos típicos de celular.",
            key="paises_telefono_sidebar",
        )
        paises_telefono_sidebar = [PAISES_TELEFONO_DISPONIBLES[n] for n in paises_sel_sidebar] or None
    else:
        digitos_tel_sidebar = st.number_input(
            "Dígitos exactos esperados", min_value=1, max_value=20, value=8,
            key="digitos_tel_sidebar",
        )
        digitos_telefono_sidebar = (int(digitos_tel_sidebar), int(digitos_tel_sidebar))
    permitir_codigo_pais_sidebar = st.checkbox(
        "Aceptar el número con código de país adelante (ej. +506 ...)",
        value=True, key="permitir_codigo_pais_sidebar",
    )

    analizar_btn = st.button(
        "🔍 Analizar tabla", type="primary",
        disabled=st.session_state.get("df") is None,
        use_container_width=True,
    )

if st.session_state.get("df") is None:
    st.info("⬅️ Cargue un archivo CSV/Excel o los datos de ejemplo desde el panel izquierdo para comenzar.")
    st.stop()

df = st.session_state.df

st.subheader(f"Vista previa — {st.session_state.nombre_fuente}")
st.caption(f"{len(df)} filas × {len(df.columns)} columnas")
st.dataframe(df.head(20), use_container_width=True)

if analizar_btn:
    st.session_state.resultado = analizar(
        df, metodo_atipicos=metodo_atipicos,
        digitos_telefono=digitos_telefono_sidebar,
        paises_telefono=paises_telefono_sidebar,
        permitir_codigo_pais_telefono=permitir_codigo_pais_sidebar,
    )
    st.session_state.pop("df_limpio", None)
    st.session_state.pop("registro", None)
    st.session_state.pop("tablas_reporte", None)
    st.session_state.pop("correcciones_individuales", None)

resultado = st.session_state.get("resultado")
if resultado is None:
    st.info("Presione **Analizar tabla** en el panel izquierdo para detectar problemas de calidad.")
    st.stop()

# --------------------------------------------------------------------------
# Paso 2 — Resumen del análisis
# --------------------------------------------------------------------------

st.divider()
st.subheader("📊 Resumen del análisis")

col_a, col_b, col_c = st.columns(3)
col_a.metric("Filas analizadas", resultado.filas_analizadas)
col_b.metric("Columnas analizadas", resultado.columnas_analizadas)
col_c.metric("Total de hallazgos", len(resultado.issues))

por_tipo = resultado.por_tipo()

if not resultado.issues:
    st.success("✅ No se encontraron problemas de calidad de datos en esta tabla.")
    st.stop()

cols_tipo = st.columns(len(por_tipo))
for col, (tipo, cantidad) in zip(cols_tipo, por_tipo.items()):
    col.metric(f"{ICONOS_TIPO.get(tipo, '•')} {NOMBRES_TIPO.get(tipo, tipo)}", cantidad)

por_columna = resultado.por_columna()
if por_columna:
    st.caption("Hallazgos por columna")
    st.bar_chart(pd.Series(por_columna, name="hallazgos"))

with st.expander("Ver detalle de hallazgos"):
    detalle_preview = pd.DataFrame([
        {
            "tipo": i.tipo,
            "columna": i.columna or "(fila completa)",
            "fila": i.fila,
            "valor_original": i.valor_original,
            "detalle": i.detalle,
        }
        for i in resultado.issues
    ])
    st.dataframe(detalle_preview, use_container_width=True, height=300)

# --------------------------------------------------------------------------
# Paso 3 — Configurar acciones de corrección
# --------------------------------------------------------------------------

st.divider()
st.subheader("🛠️ Configurar corrección")
st.caption("Elija qué hacer con cada tipo de problema encontrado.")

config: dict[str, str] = {}
valores_fijos: dict[str, object] = {}
correcciones_individuales: dict[tuple, object] = {}

for tipo, cantidad in por_tipo.items():
    if tipo not in OPCIONES_ACCION:
        continue
    opciones = OPCIONES_ACCION[tipo]
    defecto = DEFAULT_CONFIG.get(tipo, opciones[0])
    idx_defecto = opciones.index(defecto) if defecto in opciones else 0

    st.markdown(f"**{ICONOS_TIPO.get(tipo, '•')} {NOMBRES_TIPO.get(tipo, tipo)}** — {cantidad} encontrados")
    accion = st.selectbox(
        "Acción a aplicar",
        opciones,
        index=idx_defecto,
        format_func=lambda a: NOMBRES_ACCION.get(a, a),
        key=f"accion_{tipo}",
        label_visibility="collapsed",
    )
    config[tipo] = accion

    if accion == "valor_fijo":
        columnas_afectadas = sorted({
            issue.columna for issue in resultado.issues
            if issue.tipo == tipo and issue.columna
        })
        cols_input = st.columns(min(len(columnas_afectadas), 4) or 1)
        for i, col_name in enumerate(columnas_afectadas):
            with cols_input[i % len(cols_input)]:
                valor = st.text_input(
                    f"Valor fijo para '{col_name}'",
                    key=f"valor_fijo_{tipo}_{col_name}",
                )
                if valor != "":
                    valores_fijos[col_name] = valor

    elif accion == "editar_individualmente":
        issues_tipo = [i for i in resultado.issues if i.tipo == tipo]
        st.caption(
            "Edite la columna **Valor corregido** fila por fila (puede corregir un solo "
            "carácter/dígito sin retipear todo el número). Deje igual al original si ese "
            "registro no necesita cambio."
        )
        tabla_edicion = pd.DataFrame([
            {
                "Fila": i.fila,
                "Columna": i.columna,
                "Valor original": "" if i.valor_original is None else str(i.valor_original),
                "Valor corregido": "" if i.valor_original is None else str(i.valor_original),
            }
            for i in issues_tipo
        ])
        tabla_editada = st.data_editor(
            tabla_edicion,
            key=f"editor_individual_{tipo}",
            use_container_width=True,
            hide_index=True,
            disabled=["Fila", "Columna", "Valor original"],
            num_rows="fixed",
        )
        for _, fila_editada in tabla_editada.iterrows():
            clave = (tipo, fila_editada["Columna"], int(fila_editada["Fila"]))
            valor_corr = fila_editada["Valor corregido"]
            correcciones_individuales[clave] = None if valor_corr == "" else valor_corr

    st.write("")

limpiar_btn = st.button("🧽 Limpiar tabla y generar reporte", type="primary")

if limpiar_btn:
    faltan_valores = [
        tipo for tipo, accion in config.items()
        if accion == "valor_fijo"
        and not any(
            issue.columna in valores_fijos
            for issue in resultado.issues if issue.tipo == tipo
        )
    ]
    if faltan_valores:
        st.warning(
            "Escriba un valor fijo de reemplazo para cada columna indicada antes de continuar."
        )
    else:
        df_limpio, registro = limpiar(
            df, resultado.issues, config=config, valores_fijos=valores_fijos,
            correcciones_individuales=correcciones_individuales,
        )
        tablas_reporte = construir_reporte(resultado, registro, nombre_fuente=st.session_state.nombre_fuente)
        st.session_state.df_limpio = df_limpio
        st.session_state.registro = registro
        st.session_state.tablas_reporte = tablas_reporte
        st.session_state.config_aplicada = config
        st.session_state.valores_fijos_aplicados = valores_fijos

# --------------------------------------------------------------------------
# Paso 4 — Resultado y descargas
# --------------------------------------------------------------------------

if st.session_state.get("df_limpio") is not None:
    st.divider()
    st.subheader("✅ Resultado")

    df_limpio = st.session_state.df_limpio
    tablas_reporte = st.session_state.tablas_reporte

    col_r1, col_r2 = st.columns(2)
    col_r1.metric("Filas finales", len(df_limpio), delta=len(df_limpio) - len(df))
    col_r2.metric("Correcciones aplicadas", len(st.session_state.registro))

    tab_limpio, tab_resumen, tab_detalle = st.tabs(["Tabla limpia", "Reporte — Resumen", "Reporte — Detalle"])
    with tab_limpio:
        st.dataframe(df_limpio, use_container_width=True, height=350)
    with tab_resumen:
        st.dataframe(tablas_reporte["resumen"], use_container_width=True, height=350)
    with tab_detalle:
        st.dataframe(tablas_reporte["detalle"], use_container_width=True, height=350)

    st.markdown("#### Descargas")
    col_d1, col_d2, col_d3 = st.columns(3)

    with col_d1:
        buf_csv = io.StringIO()
        df_limpio.to_csv(buf_csv, index=False)
        st.download_button(
            "⬇️ Datos limpios (CSV)", buf_csv.getvalue(),
            file_name="datos_limpios.csv", mime="text/csv",
            use_container_width=True,
        )

    with col_d2:
        buf_xlsx = io.BytesIO()
        exportar(df_limpio, buf_xlsx, kind="excel")
        st.download_button(
            "⬇️ Datos limpios (Excel)", buf_xlsx.getvalue(),
            file_name="datos_limpios.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    with col_d3:
        buf_reporte = io.BytesIO()
        exportar_reporte_excel(tablas_reporte, buf_reporte)
        st.download_button(
            "⬇️ Reporte de calidad (Excel)", buf_reporte.getvalue(),
            file_name="reporte_calidad_datos.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    # ----------------------------------------------------------------------
    # Paso 5 — Exportar como script portátil (Power BI / Tableau / etc.)
    # ----------------------------------------------------------------------
    st.divider()
    st.subheader("📤 Exportar como script portátil")
    st.caption(
        "Genera un script autocontenido (solo pandas + numpy, sin depender de este "
        "proyecto) con la MISMA configuración de arriba, listo para pegar en Power BI, "
        "Tableau Prep, Alteryx, Qlik u otro pipeline. Solo soporta detección de atípicos "
        "por método IQR."
    )

    config_aplicada = st.session_state.get("config_aplicada", {})
    valores_fijos_aplicados = st.session_state.get("valores_fijos_aplicados", {})

    tab_pbi, tab_m, tab_m_puro, tab_universal = st.tabs(
        ["Script Power BI (.py)", "Código M (Editor avanzado)",
         "Código M puro (sin Python)", "Script universal (.py)"]
    )

    with tab_pbi:
        script_pbi = generar_script_powerbi(config_aplicada, factor_iqr=1.5, valores_fijos=valores_fijos_aplicados)
        st.code(script_pbi, language="python")
        st.download_button(
            "⬇️ Descargar limpiador_powerbi_generado.py", script_pbi,
            file_name="limpiador_powerbi_generado.py", mime="text/x-python",
        )

    with tab_m:
        nombre_paso = st.text_input(
            "Nombre de tu último paso en Power Query (el que entrega la tabla a corregir)",
            value="TuPasoAnterior",
        )
        script_m = generar_editor_m(
            config_aplicada, factor_iqr=1.5, valores_fijos=valores_fijos_aplicados,
            nombre_paso_anterior=nombre_paso,
        )
        st.code(script_m, language="text")
        st.download_button(
            "⬇️ Descargar editor_avanzado_powerbi_generado.m", script_m,
            file_name="editor_avanzado_powerbi_generado.m", mime="text/plain",
        )

    with tab_m_puro:
        st.caption(
            "Genera la MISMA limpieza, pero como código M nativo — sin Python.Execute. "
            "No requiere Python configurado en Power BI Desktop y evita que se pierdan "
            "datos cuando Power BI trae de vuelta columnas con tipos mezclados. La "
            "columna se detecta y se corrige tal como aparece en el análisis de arriba; "
            "las correcciones de texto (typos, mayúsculas) quedan fijas con los valores "
            "vistos en ESTE análisis — si el origen cambia más adelante, vuelva a "
            "generar el código."
        )
        nombre_paso_puro = st.text_input(
            "Nombre de tu último paso en Power Query (el que entrega la tabla a corregir)",
            value="TuPasoAnterior", key="nombre_paso_puro",
        )

        PAISES_TELEFONO_DISPONIBLES_M = PAISES_TELEFONO_DISPONIBLES

        col_tel1, col_tel2 = st.columns(2)
        with col_tel1:
            modo_telefono = st.radio(
                "Rango de dígitos de teléfono",
                ["Automático por país", "Rango manual"],
                horizontal=True,
                key="modo_telefono_mpuro",
            )
        digitos_telefono_manual = None
        paises_telefono_sel = None
        if modo_telefono == "Automático por país":
            with col_tel2:
                paises_sel_nombres = st.multiselect(
                    "País(es) del teléfono",
                    options=list(PAISES_TELEFONO_DISPONIBLES_M.keys()),
                    default=["Costa Rica"],
                    help="Vacío = rango internacional amplio (7-15 dígitos, estándar E.164). "
                         "Con varios países, se acepta la UNIÓN de sus rangos típicos de celular.",
                    key="paises_telefono_mpuro",
                )
            paises_telefono_sel = [PAISES_TELEFONO_DISPONIBLES_M[n] for n in paises_sel_nombres] or None
        else:
            with col_tel2:
                digitos_tel = st.number_input(
                    "Dígitos exactos esperados en teléfono", min_value=1, max_value=20, value=8,
                    key="digitos_tel_mpuro",
                )
            digitos_telefono_manual = (int(digitos_tel), int(digitos_tel))

        col_tel3, col_tel4 = st.columns(2)
        with col_tel3:
            primeros_digitos_txt = st.text_input(
                "Primeros dígitos válidos de teléfono (coma-separado; vacío = no validar)",
                value="2,4,5,6,7,8",
                help="Ej. para Costa Rica: 2,4,5,6,7,8. Déjelo vacío si su país no aplica esta regla.",
                key="primeros_digitos_mpuro",
            )
        with col_tel4:
            permitir_codigo_pais = st.checkbox(
                "Aceptar el número con código de país adelante (ej. +506 ...)",
                value=True,
                key="permitir_codigo_pais_mpuro",
            )
        desglosar_digitos = st.checkbox(
            "Agregar columnas para ver y corregir cada dígito de teléfono por separado",
            value=True,
            help="Genera una columna por cada posición del teléfono y una tabla editable "
                 "(TablaCorreccionesDigitosTelefono) al inicio del código M, para corregir "
                 "un dígito puntual sin tocar el dato original y sin que se pierda al refrescar.",
            key="desglosar_digitos_mpuro",
        )
        primeros_digitos_lista = (
            [d.strip() for d in primeros_digitos_txt.split(",") if d.strip()]
            if primeros_digitos_txt.strip() else None
        )

        script_m_puro = generar_editor_m_puro(
            df,
            config=config_aplicada,
            factor_iqr=1.5,
            valores_fijos=valores_fijos_aplicados,
            nombre_paso_anterior=nombre_paso_puro,
            fecha_invalida=config_aplicada.get("fecha_invalida", "marcar_solo"),
            email_invalido=config_aplicada.get("email_invalido", "marcar_solo"),
            telefono_invalido=config_aplicada.get("telefono_invalido", "marcar_solo"),
            digitos_telefono=digitos_telefono_manual,
            paises_telefono=paises_telefono_sel,
            permitir_codigo_pais_telefono=permitir_codigo_pais,
            desglosar_digitos_telefono=desglosar_digitos,
            primeros_digitos_telefono_validos=primeros_digitos_lista,
            id_duplicado=config_aplicada.get("id_duplicado", "marcar_solo"),
            formula_incorrecta=config_aplicada.get("formula_incorrecta", "marcar_solo"),
            texto_inconsistente=config_aplicada.get("texto_inconsistente", "marcar_solo"),
        )
        st.code(script_m_puro, language="text")
        st.download_button(
            "⬇️ Descargar codigo_m_puro_generado.m", script_m_puro,
            file_name="codigo_m_puro_generado.m", mime="text/plain",
        )

    with tab_universal:
        script_universal = generar_script_universal(
            config_aplicada, factor_iqr=1.5, valores_fijos=valores_fijos_aplicados
        )
        st.code(script_universal, language="python")
        st.download_button(
            "⬇️ Descargar limpiador_universal_generado.py", script_universal,
            file_name="limpiador_universal_generado.py", mime="text/x-python",
        )

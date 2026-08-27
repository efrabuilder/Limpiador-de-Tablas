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
from data_cleaner.cleaner import ACCIONES_VALIDAS  # noqa: F401 (referencia)

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
}

NOMBRES_TIPO = {
    "faltante": "Valores faltantes (vacíos/nulos)",
    "duplicado": "Filas duplicadas",
    "atipico": "Valores atípicos (outliers)",
    "tipo_invalido": "Errores de tipo (texto en columna numérica)",
}

ICONOS_TIPO = {
    "faltante": "🕳️",
    "duplicado": "📑",
    "atipico": "📈",
    "tipo_invalido": "🔤",
}

NOMBRES_ACCION = {
    "reemplazar_media": "Reemplazar por la media",
    "reemplazar_mediana": "Reemplazar por la mediana",
    "reemplazar_moda": "Reemplazar por la moda",
    "valor_fijo": "Reemplazar por un valor fijo",
    "limitar": "Limitar al rango válido (winsorizing)",
    "eliminar_fila": "Eliminar la fila",
    "marcar_solo": "Solo marcar en el reporte (no modifica el dato)",
}


# --------------------------------------------------------------------------
# Estado de sesión
# --------------------------------------------------------------------------

def _reset_estado() -> None:
    for key in ("df", "resultado", "nombre_fuente", "df_limpio", "registro", "tablas_reporte"):
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
    origen = st.radio("Fuente", ["Archivo (CSV / Excel)", "Datos de ejemplo"], index=0)

    if origen == "Archivo (CSV / Excel)":
        archivo = st.file_uploader("Seleccione un archivo", type=["csv", "xlsx", "xls"])
        if archivo is not None:
            try:
                if archivo.name.lower().endswith(".csv"):
                    df_cargado = pd.read_csv(archivo)
                else:
                    df_cargado = pd.read_excel(archivo)
                if st.session_state.get("nombre_fuente") != archivo.name:
                    _reset_estado()
                    st.session_state.df = df_cargado
                    st.session_state.nombre_fuente = archivo.name
            except Exception as exc:
                st.error(f"No se pudo leer el archivo: {exc}")
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
    st.session_state.resultado = analizar(df, metodo_atipicos=metodo_atipicos)
    st.session_state.pop("df_limpio", None)
    st.session_state.pop("registro", None)
    st.session_state.pop("tablas_reporte", None)

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
        df_limpio, registro = limpiar(df, resultado.issues, config=config, valores_fijos=valores_fijos)
        tablas_reporte = construir_reporte(resultado, registro, nombre_fuente=st.session_state.nombre_fuente)
        st.session_state.df_limpio = df_limpio
        st.session_state.registro = registro
        st.session_state.tablas_reporte = tablas_reporte

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

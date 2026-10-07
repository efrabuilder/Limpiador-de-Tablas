# -*- coding: utf-8 -*-
"""
paginas_guiadas.py
==================
Las dos secciones nuevas de la app (Streamlit):

    - pagina_limpieza_guiada(): diagnostico de nulos, estandarizacion y
      regla de nulos por columna, paso a paso (ver data_cleaner/limpieza_guiada.py)
    - pagina_merge(): unir dos tablas con chequeo de llaves y auditoria
      (ver data_cleaner/merge_tablas.py)

Las dos aceptan CSV y Excel, y si el libro tiene varias hojas se elige la
hoja. Se llaman desde app.py segun el modo elegido en la barra lateral.
"""
from __future__ import annotations

import io
import os
import re

import pandas as pd
import streamlit as st

from data_cleaner import diccionario_datos as DD
from data_cleaner import limpieza_guiada as LG
from data_cleaner import merge_tablas as MT
from data_cleaner.loaders import leer_tabla_subida, listar_hojas, tabla_a_bytes, es_archivo_excel

MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# --------------------------------------------------------------------------
# Lectura de archivos (con cache para no releer en cada interaccion)
# --------------------------------------------------------------------------

@st.cache_data(show_spinner=False, max_entries=8)
def _hojas_cacheado(contenido: bytes, nombre: str) -> list:
    buffer = io.BytesIO(contenido)
    buffer.name = nombre  # pandas y loaders usan el nombre para saber el tipo
    return listar_hojas(buffer)


@st.cache_data(show_spinner="Leyendo el archivo…", max_entries=8)
def _leer_cacheado(contenido: bytes, nombre: str, hoja, como_texto: bool, detectar: bool):
    buffer = io.BytesIO(contenido)
    buffer.name = nombre
    return leer_tabla_subida(buffer, nombre=nombre, hoja=hoja, como_texto=como_texto,
                             detectar_encabezado=detectar)


def _resumen_columnas(df: pd.DataFrame) -> pd.DataFrame:
    """Una fila por columna: tipo, % con dato y un ejemplo."""
    filas = []
    for col in df.columns:
        nulos = LG.es_nulo(df[col])
        con_dato = df[col][~nulos]
        filas.append({
            "columna": col,
            "tipo": str(df[col].dtype),
            "con dato %": round((1 - nulos.mean()) * 100, 1) if len(df) else 0.0,
            "ejemplo": str(con_dato.iloc[0])[:40] if len(con_dato) else "",
        })
    return pd.DataFrame(filas)


def _cargar_tabla(prefijo: str, titulo: str, extras: dict = None):
    """Muestra el selector de origen (archivo o tabla guardada en la sesion),
    la hoja si es un libro Excel y un resumen de columnas. Devuelve un dict
    con df, nombre, hoja y clave, o None si todavia no hay tabla.
    `extras` = {texto de la opcion: (df, nombre)} con tablas de la sesion."""
    st.subheader(titulo)
    opciones = ["Subir archivo (CSV / Excel)"] + list((extras or {}).keys())
    origen = (st.radio("Origen", opciones, key=f"{prefijo}_origen")
              if len(opciones) > 1 else opciones[0])

    if origen != opciones[0]:
        df, nombre = extras[origen]
        st.caption(f"{len(df):,} filas × {df.shape[1]} columnas")
        return {"df": df, "nombre": nombre, "hoja": None,
                "clave": f"{prefijo}:{origen}:{len(df)}:{df.shape[1]}"}

    archivo = st.file_uploader("Archivo", type=["csv", "xlsx", "xlsm", "xls"],
                               key=f"{prefijo}_archivo")
    if archivo is None:
        st.info("Suba un archivo para continuar.")
        return None

    contenido = archivo.getvalue()
    hoja = None
    try:
        if es_archivo_excel(archivo.name):
            hojas = _hojas_cacheado(contenido, archivo.name)
            if not hojas:
                st.error("El libro no tiene hojas.")
                return None
            if len(hojas) == 1:
                hoja = hojas[0]
                st.caption(f"Hoja: {hoja}")
            else:
                hoja = st.selectbox(f"Hoja ({len(hojas)} en el libro)", hojas,
                                    key=f"{prefijo}_hoja")
    except Exception as exc:
        st.error(f"No se pudo abrir el libro: {exc}")
        return None

    c1, c2 = st.columns(2)
    como_texto = c1.checkbox(
        "Leer todo como texto", value=True, key=f"{prefijo}_texto",
        help="Recomendado: se ve el archivo tal cual está (los códigos con ceros a la "
             "izquierda no se pierden y los vacíos no se confunden con ceros).")
    detectar = True
    if hoja is not None:
        detectar = c2.checkbox(
            "Saltar títulos y notas de la hoja", value=True, key=f"{prefijo}_detectar",
            help="Si la hoja trae un título arriba o notas al final, se descartan. "
                 "Desmárquelo si la última fila de datos casi vacía desaparece.")

    try:
        df = _leer_cacheado(contenido, archivo.name, hoja, como_texto, detectar)
    except Exception as exc:
        st.error(f"No se pudo leer el archivo: {exc}")
        return None

    st.caption(f"{len(df):,} filas × {df.shape[1]} columnas")
    with st.expander("Ver columnas"):
        st.dataframe(_resumen_columnas(df), hide_index=True)
    clave = f"{prefijo}:{archivo.name}:{len(contenido)}:{hoja}:{como_texto}:{detectar}"
    return {"df": df, "nombre": archivo.name, "hoja": hoja, "clave": clave}


def _nombre_base(nombre: str) -> str:
    base = os.path.splitext(os.path.basename(str(nombre)))[0]
    return re.sub(r"[^\w\-]+", "_", base).strip("_") or "tabla"  # sin espacios ni símbolos


def _botones_descarga(df: pd.DataFrame, base: str, script: str, prefijo: str) -> None:
    c1, c2, c3 = st.columns(3)
    c1.download_button("⬇️ CSV", tabla_a_bytes(df, "csv"), file_name=f"{base}.csv",
                       mime="text/csv", key=f"{prefijo}_dl_csv")
    c2.download_button("⬇️ Excel (.xlsx)", tabla_a_bytes(df, "xlsx"), file_name=f"{base}.xlsx",
                       mime=MIME_XLSX, key=f"{prefijo}_dl_xlsx")
    c3.download_button("⬇️ Script Python", script, file_name=f"{base}_script.py",
                       mime="text/x-python", key=f"{prefijo}_dl_py")


def _boton_enviar_a_clasica(df: pd.DataFrame, nombre: str, al_enviar, prefijo: str) -> None:
    """Boton que deja la tabla en la limpieza clásica (sin descargar y subir)."""
    if al_enviar is None:
        return
    st.button("➡️ Enviar a limpieza clásica", on_click=al_enviar, args=(df, nombre),
              key=f"{prefijo}_enviar_clasica",
              help="Cambia al modo «Limpieza de una tabla» con esta tabla ya cargada.")


def seccion_diccionario(df: pd.DataFrame, prefijo: str, nombre_defecto: str, reglas=None,
                         origenes=None, fuentes=None, eliminadas=None) -> None:
    """Diccionario de datos de la tabla maestra: se completan la descripción y la
    justificación de cada campo y se descarga en Excel (hojas Resumen y Diccionario)."""
    st.subheader("📘 Diccionario de datos de la tabla maestra")
    st.caption("Tipo, completitud, valores únicos y rango salen de los datos. La descripción y la "
               "justificación de negocio las escribe usted: es lo que más le sirve a quien decide.")
    nombre = st.text_input("Nombre de la tabla maestra", value=nombre_defecto, key=f"{prefijo}_dic_nombre")
    base = DD.construir_diccionario(df, reglas, origenes)
    fijas = [c for c in base.columns if c not in DD.COLUMNAS_EDITABLES]
    editado = st.data_editor(
        base, hide_index=True, disabled=fijas,
        key=f"{prefijo}_dic_{hash((nombre_defecto, tuple(df.columns), len(df)))}",
        column_config={
            "Descripción": st.column_config.TextColumn("Descripción", width="large"),
            "Justificación de negocio": st.column_config.TextColumn("Justificación de negocio",
                                                                    width="large"),
        },
    )
    sin_descripcion = int((editado["Descripción"].fillna("").astype(str).str.strip() == "").sum())
    if sin_descripcion:
        st.warning(f"{sin_descripcion} de {len(editado)} campos todavía no tienen descripción "
                   "(quedan resaltados en amarillo en el Excel).")
    resumen = DD.resumen_tabla(df, nombre, editado, fuentes, eliminadas)
    st.download_button("⬇️ Diccionario de datos (Excel)", DD.diccionario_a_excel(editado, resumen),
                       file_name=f"diccionario_{_nombre_base(nombre)}.xlsx", mime=MIME_XLSX,
                       key=f"{prefijo}_dic_descarga")


# --------------------------------------------------------------------------
# Limpieza guiada (nulos)
# --------------------------------------------------------------------------

def pagina_limpieza_guiada(al_enviar_a_clasica=None) -> None:
    st.title("🩺 Limpieza guiada (nulos)")
    st.caption(
        "Paso a paso: diagnóstico de nulos, estandarización y una regla por columna. "
        "Al final descarga la tabla limpia y un script de pandas que repite lo mismo."
    )

    meta = _cargar_tabla("lg", "1. Cargar la tabla")
    if meta is None:
        return
    df, clave = meta["df"], meta["clave"]

    # ---- 2. Diagnostico
    st.subheader("2. Diagnóstico de nulos y vacíos")
    extra = st.multiselect(
        "Además de las celdas vacías, contar como nulo estos textos",
        ["nan", "none", "null"] + list(LG.TOKENS_NULOS_EXTRA),
        default=["nan", "none", "null"], key=f"lg_tokens_{clave}",
        help="«na» o «-» pueden ser datos reales, por eso no se cuentan salvo que los elija.")
    tokens = ("",) + tuple(extra)
    diag = LG.diagnostico_nulos(df, tokens)
    m1, m2, m3 = st.columns(3)
    m1.metric("Columnas con nulos", int((diag["total"] > 0).sum()))
    m2.metric("Celdas nulas", int(diag["total"].sum()))
    m3.metric("Filas", f"{len(df):,}")
    st.dataframe(diag.reset_index(), hide_index=True)
    st.caption("Semáforo: ✅ 0% · 🟢 menos de 5% · 🟡 menos de 30% · 🟠 menos de 70% · 🔴 70% o más.")

    # ---- 3. Estandarizacion global
    st.subheader("3. Estandarizar")
    snake = st.checkbox("Nombres de columna en snake_case", value=True, key=f"lg_snake_{clave}",
                        help="«State (Land)» → «state_land». Los pasos de abajo usan los nombres nuevos.")
    df_ren = LG.paso_nombres_columnas(df)[0] if snake else df
    sufijo = f"{clave}_{int(snake)}"  # las listas se reinician si cambian los nombres
    vacios = st.checkbox("Pasar los vacíos a nulos reales (NaN)", value=True, key=f"lg_vacios_{sufijo}")

    usar_texto = st.checkbox("Estandarizar texto", value=True, key=f"lg_usar_texto_{sufijo}")
    cols_texto, minus, espacios, comillas = [], True, True, True
    if usar_texto:
        cols_texto = st.multiselect(
            "Columnas de texto", list(df_ren.columns),
            default=LG.columnas_texto_sugeridas(df_ren, tokens), key=f"lg_cols_texto_{sufijo}",
            help="Por defecto no incluye identificadores, teléfonos, fechas ni nombres propios.")
        t1, t2, t3 = st.columns(3)
        minus = t1.checkbox("Minúsculas", value=True, key=f"lg_min_{sufijo}")
        espacios = t2.checkbox("Espacios repetidos", value=True, key=f"lg_esp_{sufijo}")
        comillas = t3.checkbox("Quitar comillas", value=True, key=f"lg_com_{sufijo}")

    cols_fecha = LG.columnas_fecha_por_nombre(df_ren)
    sugeridas_num = [c for c in df_ren.columns
                     if not pd.api.types.is_numeric_dtype(df_ren[c])
                     and LG.rol_columna(df_ren, c, cols_fecha, tokens) == "numerica"]
    cols_num = st.multiselect(
        "Columnas a convertir a número", list(df_ren.columns), default=sugeridas_num,
        key=f"lg_cols_num_{sufijo}",
        help="Entiende «1,5», «1.234,56» y símbolos de moneda. Lo que no se pueda leer queda nulo "
             "y se avisa cuántos fueron.")

    cols_coord_lat = [c for c in df_ren.columns if LG.tipo_coordenada(c) == "latitud"]
    cols_coord_lon = [c for c in df_ren.columns if LG.tipo_coordenada(c) == "longitud"]
    ninguna = "(ninguna)"
    k1, k2 = st.columns(2)
    lat = k1.selectbox("Columna de latitud", [ninguna] + list(df_ren.columns),
                       index=([ninguna] + list(df_ren.columns)).index(cols_coord_lat[0])
                       if cols_coord_lat else 0, key=f"lg_lat_{sufijo}")
    lon = k2.selectbox("Columna de longitud", [ninguna] + list(df_ren.columns),
                       index=([ninguna] + list(df_ren.columns)).index(cols_coord_lon[0])
                       if cols_coord_lon else 0, key=f"lg_lon_{sufijo}")
    rango_lat = rango_lon = None
    if lat != ninguna or lon != ninguna:
        if st.checkbox("Indicar el rango esperado de las coordenadas (ej. el país)",
                       key=f"lg_rango_{sufijo}",
                       help="Sin rango solo se corrigen valores imposibles (más de 90 / 180). Con el "
                            "rango del país, un valor como 9953281 se arregla a 9.953281."):
            r1, r2, r3, r4 = st.columns(4)
            if lat != ninguna:
                a = r1.number_input("Latitud mínima", value=-90.0, key=f"lg_lat_min_{sufijo}")
                b = r2.number_input("Latitud máxima", value=90.0, key=f"lg_lat_max_{sufijo}")
                rango_lat = (a, b)
            if lon != ninguna:
                c = r3.number_input("Longitud mínima", value=-180.0, key=f"lg_lon_min_{sufijo}")
                d = r4.number_input("Longitud máxima", value=180.0, key=f"lg_lon_max_{sufijo}")
                rango_lon = (c, d)

    config = {
        "tokens": tokens, "nombres_snake": snake, "vacios_a_nan": vacios,
        "texto": {"columnas": cols_texto, "minusculas": minus, "espacios": espacios,
                  "comillas": comillas} if usar_texto else None,
        "numericas": cols_num,
        "latitud": None if lat == ninguna else lat, "longitud": None if lon == ninguna else lon,
        "rango_latitud": rango_lat, "rango_longitud": rango_lon,
    }
    try:
        df_base, pasos_globales = LG.ejecutar_pasos_globales(df, config)
    except Exception as exc:
        st.error(f"No se pudo aplicar la estandarización: {exc}")
        return
    with st.expander(f"Qué hizo la estandarización ({len(pasos_globales)} pasos) y vista previa"):
        for paso in pasos_globales:
            st.markdown(f"**{paso.titulo}**")
            st.text(paso.detalle)
        st.dataframe(df_base.head(10))

    # ---- 4. Reglas de nulos
    st.subheader("4. Regla de nulos por columna")
    st.caption("La regla sugerida sale del tipo de columna (llaves y coordenadas: eliminar fila; "
               "números: mediana; texto: valor fijo; fechas: dejar). Puede cambiarla en la tabla.")
    with st.expander("¿Qué hace cada regla?"):
        for clave_regla, texto in LG.REGLAS_NULOS.items():
            st.markdown(f"- `{clave_regla}`: {texto}")
    tabla = LG.tabla_de_reglas(df_base, tokens)
    claves_columnas = "|".join(df_base.columns)
    editada = st.data_editor(
        tabla, hide_index=True, key=f"lg_reglas_{hash((clave, claves_columnas, tokens))}",
        disabled=["columna", "rol", "nulos", "%", "semaforo"],
        column_config={
            "regla": st.column_config.SelectboxColumn("regla", options=list(LG.REGLAS_NULOS),
                                                      required=True),
            "valor": st.column_config.TextColumn("valor", help="Para «valor_fijo»"),
            "grupo": st.column_config.SelectboxColumn(
                "grupo", options=[""] + list(df_base.columns),
                help="Solo para «mediana_por_grupo»"),
        },
    )

    # ---- 5. Aplicar
    if st.button("Aplicar limpieza", type="primary", key="lg_aplicar"):
        reglas = editada.fillna("").to_dict("records")
        try:
            df_final, pasos_reglas = LG.ejecutar_reglas(df_base, reglas, tokens)
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.session_state["lg_resultado"] = {
                "df": df_final, "antes": df, "clave": clave, "tokens": tokens,
                "pasos": pasos_globales + pasos_reglas, "nombre": meta["nombre"],
                "hoja": meta["hoja"], "reglas": reglas,
            }
            st.session_state["lg_version"] = st.session_state.get("lg_version", 0) + 1
            st.session_state["df_limpieza_guiada"] = (df_final, f"{_nombre_base(meta['nombre'])}_limpio.csv")

    resultado = st.session_state.get("lg_resultado")
    if not resultado or resultado["clave"] != clave:
        return

    # ---- 6. Auditoria final
    st.subheader("5. Auditoría final")
    aud = LG.auditoria_final(resultado["antes"], resultado["df"], resultado["tokens"])
    a1, a2, a3, a4 = st.columns(4)
    a1.metric("Filas", f"{aud['filas_despues']:,}", f"{aud['filas_despues'] - aud['filas_antes']:,}")
    a2.metric("Columnas", aud["columnas_despues"], aud["columnas_despues"] - aud["columnas_antes"])
    a3.metric("Celdas nulas", f"{aud['nulos_despues']:,}", f"{aud['nulos_despues'] - aud['nulos_antes']:,}",
              delta_color="inverse")
    a4.metric("Duplicados", aud["duplicados_despues"])
    if len(aud["nulos_restantes"]):
        st.warning("Quedan nulos en estas columnas (puede ser a propósito, por ejemplo fechas):")
        st.dataframe(aud["nulos_restantes"].reset_index())
    else:
        st.success("Sin nulos restantes.")
    with st.expander("Pasos aplicados"):
        for paso in resultado["pasos"]:
            st.markdown(f"**{paso.titulo}**")
            st.text(paso.detalle)
    with st.expander("Tipos de datos"):
        st.dataframe(aud["tipos"])
    st.dataframe(resultado["df"].head(20))

    script = LG.generar_script_limpieza(resultado["pasos"], resultado["tokens"],
                                        resultado["nombre"], resultado["hoja"])
    base_nombre = f"{_nombre_base(resultado['nombre'])}_limpio"
    _botones_descarga(resultado["df"], base_nombre, script, "lg")
    _boton_enviar_a_clasica(resultado["df"], f"{base_nombre}.csv", al_enviar_a_clasica, "lg")
    st.caption("Esta tabla también está disponible como Tabla A en la sección «Merge».")

    reglas_aplicadas = resultado.get("reglas") or []
    eliminadas = [r["columna"] for r in reglas_aplicadas
                  if r["regla"] == "eliminar_columna" and r.get("nulos")]
    seccion_diccionario(resultado["df"], "lg", base_nombre, reglas_aplicadas,
                         fuentes=[resultado["nombre"]], eliminadas=eliminadas)


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------

def _usar_resultado_como_a() -> None:
    """Callback del boton: el resultado pasa a ser la Tabla A."""
    resultado = st.session_state.get("m_resultado")
    if resultado:
        st.session_state["merge_resultado_df"] = (resultado["df"], "resultado_merge.csv")
        st.session_state["ma_origen"] = "Usar el resultado del merge anterior"


def pagina_merge(al_enviar_a_clasica=None) -> None:
    st.title("🔗 Merge (unir dos tablas)")
    st.caption(
        "A es la tabla que manda y B la que la enriquece. Se revisan las llaves antes de unir y "
        "se audita el resultado. Al final descarga la tabla y un script de pandas."
    )

    extras_a = {}
    if "df_limpieza_guiada" in st.session_state:
        extras_a["Usar la tabla de la Limpieza guiada"] = st.session_state["df_limpieza_guiada"]
    if "merge_resultado_df" in st.session_state:
        extras_a["Usar el resultado del merge anterior"] = st.session_state["merge_resultado_df"]

    col_a, col_b = st.columns(2)
    with col_a:
        meta_a = _cargar_tabla("ma", "1. Tabla A (la que manda)", extras_a)
    with col_b:
        meta_b = _cargar_tabla("mb", "Tabla B (la que enriquece)")
    if meta_a is None or meta_b is None:
        return
    df_a, df_b = meta_a["df"], meta_b["df"]
    clave = f"{meta_a['clave']}|{meta_b['clave']}"

    # ---- 2. Llaves
    st.subheader("2. Columnas llave")
    sugerencias = MT.sugerir_llaves(df_a, df_b)
    if len(sugerencias):
        st.caption("Pares de columnas que podrían ser la llave (mismo nombre o mismos valores):")
        st.dataframe(sugerencias, hide_index=True)
    claves_a = st.multiselect("Llave en A", list(df_a.columns), key=f"m_claves_a_{clave}",
                              default=[sugerencias.iloc[0]["columna_a"]] if len(sugerencias) else [])
    claves_b = st.multiselect("Llave en B (en el mismo orden que en A)", list(df_b.columns),
                              key=f"m_claves_b_{clave}",
                              default=[sugerencias.iloc[0]["columna_b"]] if len(sugerencias) else [])
    if not claves_a or len(claves_a) != len(claves_b):
        st.info("Elija la misma cantidad de columnas llave en A y en B.")
        return

    modo = st.radio("Cómo comparar las llaves", list(MT.MODOS_LLAVE), horizontal=True,
                    format_func=MT.MODOS_LLAVE.get, key=f"m_modo_{clave}")
    ancho = 0
    if modo == "codigo":
        ancho = int(st.number_input("Rellenar con ceros a la izquierda hasta (0 = no rellenar)",
                                    min_value=0, max_value=20, value=0, key=f"m_ancho_{clave}",
                                    help="Ej. 5 para códigos postales de Alemania: 1067 → 01067."))

    # ---- 3. Diagnostico de llaves
    st.subheader("3. Revisión de las llaves")
    try:
        d = MT.diagnosticar_llaves(df_a, df_b, claves_a, claves_b, modo, ancho)
    except Exception as exc:
        st.error(f"No se pudieron revisar las llaves: {exc}")
        return
    r1, r2, r3 = st.columns(3)
    r1.metric("Filas de A con pareja en B", f"{d['semaforo']} {d['pct_filas_con_pareja']}%",
              f"{d['pct_filas_con_pareja'] - d['pct_sin_normalizar']:+.1f} pts por normalizar")
    r2.metric("Llaves repetidas en A / B", f"{d['repetidas_a']:,} / {d['repetidas_b']:,}")
    r3.metric("Filas sin llave en A / B", f"{d['nulos_a']:,} / {d['nulos_b']:,}")
    if all(d["mismos_nombres_exactos"]):
        nombres_txt = "✅ Las llaves se llaman exactamente igual"
    elif all(d["mismos_nombres"]):
        nombres_txt = "⚠️ Las llaves solo cambian en mayúsculas o espacios (ej. ID_Tienda / id_tienda)"
    else:
        nombres_txt = "⚠️ Las llaves tienen nombres distintos (no es un error, confirme que son las correctas)"
    tipos_txt = ("✅ Las llaves tienen el mismo tipo de dato" if all(d["mismos_tipos"]) else
                 "⚠️ Tipos de dato distintos: un ID numérico no cruza con uno de texto en pandas, "
                 "pero aquí las llaves se comparan como texto, así que sí cruzan")
    st.markdown(f"- {nombres_txt}\n- {tipos_txt}\n- Relación entre las tablas: **{d['cardinalidad']}**")
    if len(d["ejemplos_sin_pareja"]):
        with st.expander("Llaves de A que no están en B (las 10 más frecuentes)"):
            st.dataframe(d["ejemplos_sin_pareja"], hide_index=True)

    # ---- 4. Preparar B
    st.subheader("4. Preparar la tabla B")
    agregaciones = None
    otras_b = [c for c in df_b.columns if c not in claves_b]
    if d["repetidas_b"] > 0:
        st.warning(f"B repite {d['repetidas_b']:,} llaves: si las une así, las filas de A se multiplican.")
        if st.checkbox("Dejar una fila por llave en B", value=True, key=f"m_colapsar_{clave}"):
            base_agg = pd.DataFrame({"columna": otras_b, "incluir": True, "funcion": "first"})
            editada = st.data_editor(
                base_agg, hide_index=True, key=f"m_agg_{clave}", disabled=["columna"],
                column_config={"funcion": st.column_config.SelectboxColumn(
                    "función", options=list(MT.AGREGACIONES), required=True,
                    help="first = el primer dato; sum/mean/median/max/min leen la columna como número")})
            agregaciones = {r["columna"]: r["funcion"] for r in editada.to_dict("records") if r["incluir"]}
            if not agregaciones:
                st.error("Incluya al menos una columna de B.")
                return
    p1, p2 = st.columns(2)
    prefijo = p1.text_input("Prefijo para las columnas de B (opcional)", value="",
                            key=f"m_prefijo_{clave}", help="Ej. «municipio_». Deja claro de dónde viene cada columna.")
    solo_repetidas = p2.radio("Aplicar el prefijo a", ["solo las columnas repetidas en A", "todas las de B"],
                              key=f"m_solo_{clave}") == "solo las columnas repetidas en A"
    sufijo_b = st.text_input("Sufijo para columnas repetidas que queden sin prefijo", value="_b",
                             key=f"m_sufijo_{clave}")

    # ---- 5. Tipo de union
    st.subheader("5. Tipo de unión")
    how = st.selectbox("¿Qué tabla manda?", list(MT.TIPOS_UNION), format_func=MT.TIPOS_UNION.get,
                       key=f"m_how_{clave}")
    if how == "inner":
        st.warning("El inner join puede eliminar filas de A sin que se note. Se mostrará el conteo antes y después.")
    colapsada = agregaciones is not None
    validacion_defecto = "many_to_one" if (colapsada or d["repetidas_b"] == 0) else ""
    opciones_val = list(MT.VALIDACIONES)
    validate = st.selectbox("Validar la relación", opciones_val, format_func=MT.VALIDACIONES.get,
                            index=opciones_val.index(validacion_defecto),
                            key=f"m_valid_{clave}_{int(colapsada)}",
                            help="Si la relación real es distinta, pandas avisa en vez de duplicar filas.")
    conservar = st.checkbox("Conservar la columna _merge (both / left_only / right_only)", value=False,
                            key=f"m_indicador_{clave}")

    # ---- 6. Unir
    if st.button("Unir tablas", type="primary", key="m_unir"):
        try:
            df_res, aud = MT.hacer_merge(
                df_a, df_b, claves_a, claves_b, how=how, validate=validate or None, modo=modo,
                ancho=ancho, prefijo_b=prefijo, solo_repetidas=solo_repetidas,
                agregaciones=agregaciones, sufijos=("", sufijo_b), conservar_indicador=conservar)
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.session_state["m_resultado"] = {
                "df": df_res, "aud": aud, "clave": clave, "rellenos": [],
                "params": dict(
                    nombre_a=meta_a["nombre"], nombre_b=meta_b["nombre"], claves_a=claves_a,
                    claves_b=claves_b, hoja_a=meta_a["hoja"], hoja_b=meta_b["hoja"], how=how,
                    validate=validate or None, modo=modo, ancho=ancho, prefijo_b=prefijo,
                    solo_repetidas=solo_repetidas, agregaciones=agregaciones,
                    sufijos=("", sufijo_b), conservar_indicador=conservar),
            }

    resultado = st.session_state.get("m_resultado")
    if not resultado or resultado["clave"] != clave:
        return

    # ---- 7. Resultado
    st.subheader("6. Resultado")
    aud = resultado["aud"]
    df_res = resultado["df"]
    x1, x2, x3, x4 = st.columns(4)
    x1.metric("Filas de A", f"{aud['filas_a']:,}")
    x2.metric("Filas de B", f"{aud['filas_b']:,}")
    x3.metric("Filas del resultado", f"{len(df_res):,}", f"{len(df_res) - aud['filas_a']:+,} vs A")
    x4.metric("A con pareja", f"{aud['semaforo']} {aud['pct_filas_con_pareja']}%")
    if aud["conteo_merge"]:
        st.caption("Conteo del cruce: " + " · ".join(f"{k}: {v:,}" for k, v in aud["conteo_merge"].items()))
    for aviso in aud["advertencias"]:
        st.warning(aviso)
    if aud["renombradas"]:
        with st.expander("Columnas de B renombradas"):
            st.json(aud["renombradas"])
    st.dataframe(df_res.head(20))

    with st.expander("Plan B: rellenar una columna con otra"):
        st.caption("Donde la columna principal quedó vacía se usa el valor de la columna de respaldo "
                   "(por ejemplo población del municipio, y si no hay, la del estado).")
        b1, b2 = st.columns(2)
        destino = b1.selectbox("Columna a rellenar", list(df_res.columns), key="m_plan_destino")
        respaldo = b2.selectbox("Columna de respaldo", list(df_res.columns), key="m_plan_respaldo")
        if st.button("Rellenar", key="m_plan_aplicar"):
            if destino == respaldo:
                st.error("Elija dos columnas distintas.")
            else:
                resultado["df"] = MT.rellenar_con_respaldo(df_res, destino, respaldo)
                resultado["rellenos"].append((destino, respaldo))
                st.session_state["m_resultado"] = resultado
                st.rerun()
        if resultado["rellenos"]:
            st.caption("Aplicado: " + ", ".join(f"{d_} ← {r_}" for d_, r_ in resultado["rellenos"]))

    script = MT.generar_script_merge(rellenos=resultado["rellenos"], **resultado["params"])
    _botones_descarga(resultado["df"], "resultado_merge", script, "m")
    _boton_enviar_a_clasica(resultado["df"], "resultado_merge.csv", al_enviar_a_clasica, "m")
    st.button("Usar este resultado como Tabla A de otro merge", on_click=_usar_resultado_como_a,
              key="m_encadenar")

    origenes = {c: ("Auditoría del merge" if c == "_merge" else
                    "Tabla A" if c in df_a.columns else "Tabla B") for c in resultado["df"].columns}
    seccion_diccionario(resultado["df"], "m", "tabla_maestra", origenes=origenes,
                         fuentes=[f"A: {meta_a['nombre']}", f"B: {meta_b['nombre']}"])

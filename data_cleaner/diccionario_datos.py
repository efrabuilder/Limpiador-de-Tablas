# -*- coding: utf-8 -*-
"""
diccionario_datos.py
====================
Diccionario de datos de la tabla maestra, pensado para presentarlo a quienes
toman decisiones. Una fila por campo, con lo que se puede sacar de los datos
(tipo, completitud, valores únicos, rango o ejemplos) y lo que solo sabe la
persona que conoce el negocio (descripción y justificación), que se deja
para escribir.

Columnas del diccionario:
    N°, Campo, Descripción, Tipo de dato, Rol, Nulos, Completitud %,
    Valores únicos, Rango o ejemplos, Origen, Tratamiento de nulos,
    Justificación de negocio, Clasificación ejecutiva, Modelo de datos

Origen, Justificación de negocio, Clasificación ejecutiva y Modelo de datos se llenan solos para
cualquier tabla (ver negocio.py): salen del nombre del campo, de su contenido y de la tabla. La persona
puede reescribirlos y lo que escriba se respeta.

Se exporta en dos documentos:
    - Diccionario técnico (Excel, «diccionario_datos.xlsx»): una fila por campo, con tipo
      nativo (int64, float64, datetime64[ns]...), límites lógicos y metadatos legibles
      por máquina. Sirve para catalogadores de datos y para importar a Power BI.
    - Documento de alcance / diccionario ejecutivo (Word): resumen para quien decide.
      Explica la arquitectura final y solo las variables críticas (KPIs, variables
      transformadas y llaves de unión), y remite al Excel técnico para el detalle de los campos.

También queda la función diccionario_a_excel (hojas Resumen y Diccionario con encabezados
en español), por si se necesita esa versión.
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .limpieza_guiada import (
    ROLES,
    TOKENS_NULOS_BASE,
    REGLAS_NULOS,
    a_numero,
    es_nulo,
    rol_columna,
)
from .patrones import columnas_fecha_por_nombre, tipo_coordenada
from .descripciones import completar_descripciones, describir_campo
from .negocio import (  # noqa: F401  (se reexportan: otras partes de la app los usan desde aquí)
    CLASIFICACION_ATRIBUTO,
    CLASIFICACION_KPI,
    CLASIFICACION_LLAVE,
    CLASIFICACION_METADATO,
    CLASIFICACION_TRANSFORMADA,
    CLASIFICACIONES,
    COLUMNA_CLASIFICACION,
    COLUMNA_JUSTIFICACION,
    COLUMNA_MODELO,
    COLUMNA_ORIGEN,
    COLUMNAS_NEGOCIO,
    TRATAMIENTOS_QUE_TRANSFORMAN as _TRATAMIENTOS_QUE_TRANSFORMAN,
    completar_campos_negocio,
)

COLUMNAS_EDITABLES = ("Descripción", COLUMNA_ORIGEN, COLUMNA_JUSTIFICACION, COLUMNA_CLASIFICACION, COLUMNA_MODELO)

NOMBRE_TECNICO = "diccionario_datos.xlsx"

# Descripciones que se pueden adelantar sin adivinar: el rol las deja claras.
_DESCRIPCION_POR_ROL = {
    "email": "Correo electrónico.",
    "telefono": "Número de teléfono.",
}

_FECHA_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
_FECHA_LATINA = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$")


# --------------------------------------------------------------------------
# Tipo y rango de cada campo
# --------------------------------------------------------------------------

def _formatear_numero(valor: float) -> str:
    """12.0 -> '12'; 12.3456 -> '12.35'; miles con coma."""
    if pd.isna(valor):
        return ""
    if float(valor).is_integer():
        return f"{int(valor):,}"
    return f"{valor:,.2f}"


def tipo_semantico(serie: pd.Series, rol: str, tokens=TOKENS_NULOS_BASE) -> str:
    """Tipo de dato en palabras de negocio: Entero, Decimal, Fecha, Sí/No,
    Categoría, Texto o Texto (código). Mira el contenido, no solo el dtype,
    porque una tabla leída como texto trae los números como texto."""
    con_dato = serie[~es_nulo(serie, tokens)]
    if len(con_dato) == 0:
        return "Vacío"
    if pd.api.types.is_bool_dtype(serie):
        return "Sí/No"
    if pd.api.types.is_datetime64_any_dtype(serie):
        return "Fecha"

    muestra = con_dato.head(5000)
    es_codigo = rol in ("id", "telefono")

    if pd.api.types.is_numeric_dtype(serie):
        return "Entero" if (muestra % 1 == 0).all() else "Decimal"

    texto = muestra.astype(str).str.strip()
    if es_codigo or texto.str.match(r"^0\d+$").any():  # ceros a la izquierda: es un código
        return "Texto (código)"
    if (texto.str.match(_FECHA_ISO) | texto.str.match(_FECHA_LATINA)).mean() >= 0.95:
        return "Fecha"
    numeros = a_numero(texto)
    if numeros.notna().mean() >= 0.98:
        return "Entero" if (numeros.dropna() % 1 == 0).all() else "Decimal"
    unicos = texto.nunique()
    if unicos <= 20 and unicos / len(texto) <= 0.5:
        return "Categoría"
    return "Texto"


def resumen_valores(serie: pd.Series, tipo: str, tokens=TOKENS_NULOS_BASE,
                    maximo_ejemplos: int = 5) -> str:
    """Rango (números y fechas), lista de valores (categorías) o ejemplos
    (texto) del campo, listo para leer en una celda."""
    con_dato = serie[~es_nulo(serie, tokens)]
    if len(con_dato) == 0:
        return ""
    if tipo in ("Entero", "Decimal"):
        numeros = a_numero(con_dato).dropna() if not pd.api.types.is_numeric_dtype(serie) else con_dato
        if len(numeros) == 0:
            return ""
        return (f"{_formatear_numero(numeros.min())} a {_formatear_numero(numeros.max())} "
                f"(promedio {_formatear_numero(numeros.mean())})")
    if tipo == "Fecha":
        fechas = (con_dato if pd.api.types.is_datetime64_any_dtype(serie)
                  else pd.to_datetime(con_dato.astype(str), errors="coerce", dayfirst=bool(
                      con_dato.astype(str).str.match(_FECHA_LATINA).any()))).dropna()
        if len(fechas) == 0:
            return ""
        return f"{fechas.min():%Y-%m-%d} a {fechas.max():%Y-%m-%d}"
    if tipo == "Sí/No":
        return "Sí / No"
    valores = con_dato.astype(str).value_counts().index.tolist()  # los más frecuentes primero
    texto = ", ".join(valores[:maximo_ejemplos])
    if tipo == "Categoría" and len(valores) > maximo_ejemplos:
        texto += f" (+{len(valores) - maximo_ejemplos} más)"
    return texto if tipo == "Categoría" else f"Ej.: {texto}"


def describir_regla(regla: str, valor: str = "", grupo: str = "", nulos: int = 0) -> str:
    """Frase corta de lo que se hizo con los nulos de una columna."""
    if regla == "dejar" or not regla:
        return f"Se dejaron vacíos ({nulos})" if nulos else "Sin nulos"
    if regla == "eliminar_fila":
        return f"Se eliminaron {nulos} filas con nulo" if nulos else "Filas con nulo eliminadas"
    cantidad = f" ({nulos} tratados)" if nulos else ""
    frases = {
        "valor_fijo": f"Rellenados con «{valor}»",
        "no_indica": f"Rellenados con «{valor or 'No indica'}» (nulo válido)",
        "cero": "Rellenados con 0",
        "mediana": "Rellenados con la mediana",
        "media": "Rellenados con el promedio",
        "moda": "Rellenados con el valor más frecuente",
        "mediana_por_grupo": f"Rellenados con la mediana por «{grupo}»",
        "eliminar_columna": "Columna eliminada",
        "rellenar_nan": "Celdas vacías convertidas a nulo real",
    }
    return frases.get(regla, REGLAS_NULOS.get(regla, regla)) + cantidad


# Acciones de la limpieza clásica -> regla equivalente de la limpieza guiada.
_ACCION_CLASICA_A_REGLA = {
    "reemplazar_media": "media", "reemplazar_mediana": "mediana", "reemplazar_moda": "moda",
    "valor_fijo": "valor_fijo", "eliminar_fila": "eliminar_fila", "rellenar_nan": "rellenar_nan",
    "marcar_solo": "dejar",
}


def reglas_desde_registro(registro: List[Dict]) -> List[Dict]:
    """Convierte el registro de la limpieza clásica en frases de «Tratamiento
    de nulos» por columna (solo los hallazgos de tipo faltante). Si en una
    columna se aplicaron varios tratamientos, los lista con su cantidad."""
    por_columna: Dict[str, Dict[str, Dict]] = {}
    for r in registro or []:
        if r.get("tipo") != "faltante":
            continue
        accion = r.get("accion_aplicada", "")
        grupo = por_columna.setdefault(r["columna"], {}).setdefault(
            accion, {"n": 0, "valor": r.get("valor_nuevo", "")})
        grupo["n"] += 1
    reglas = []
    for columna, acciones in por_columna.items():
        frases = []
        for accion, datos in acciones.items():
            regla = _ACCION_CLASICA_A_REGLA.get(accion, accion)
            valor = "" if pd.isna(datos["valor"]) else str(datos["valor"])
            frases.append(describir_regla(regla, valor, "", datos["n"]))
        reglas.append({"columna": columna, "descripcion": "; ".join(frases)})
    return reglas


# --------------------------------------------------------------------------
# Diccionario y resumen
# --------------------------------------------------------------------------

def construir_diccionario(df: pd.DataFrame, reglas: Optional[List[Dict]] = None,
                          origenes: Optional[Dict[str, str]] = None,
                          tokens=TOKENS_NULOS_BASE,
                          llaves: Optional[Sequence[str]] = None,
                          nombre_tabla: str = "",
                          fuentes: Optional[Sequence[str]] = None,
                          cruces: Optional[Sequence[Dict]] = None) -> pd.DataFrame:
    """Diccionario de datos de `df` (una fila por campo).
    `reglas`: lista [{columna, regla, valor, grupo, nulos}] de la limpieza
    guiada (o [{columna, descripcion}] ya redactadas, ver
    reglas_desde_registro), para contar qué se hizo con los nulos de cada campo.
    `origenes`: {columna: texto} con la tabla de la que viene cada campo
    (útil en una tabla maestra que sale de un merge).
    `llaves`: columnas usadas para unir tablas; si no se dan, se toman como
    llaves las de rol identificador.
    `nombre_tabla`, `fuentes` (archivos u hojas de donde sale la tabla) y `cruces` (uniones hechas) ayudan
    a redactar el origen y el modelo de datos.
    «Descripción» se redacta sola para todos los campos (nombre, rol, tipo, origen y
    tratamiento; ver descripciones.py) y la persona puede reescribirla.
    «Origen», «Justificación de negocio», «Clasificación ejecutiva» (KPI, variable transformada, llave,
    atributo descriptivo o metadato de control) y «Modelo de datos» también se llenan solos para todos los
    campos (ver negocio.py), con lo que dicen el nombre y los datos de cada columna."""
    por_columna = {r["columna"]: r for r in (reglas or [])}
    cols_fecha = columnas_fecha_por_nombre(df)
    total = max(len(df), 1)
    filas = []
    for i, col in enumerate(df.columns, 1):
        serie = df[col]
        nulos = int(es_nulo(serie, tokens).sum())
        rol = rol_columna(df, col, cols_fecha, tokens)
        tipo = tipo_semantico(serie, rol, tokens)
        if reglas is None:
            tratamiento = ""
        elif col in por_columna:
            r = por_columna[col]
            tratamiento = r.get("descripcion") or describir_regla(
                r["regla"], r.get("valor", ""), r.get("grupo", ""), int(r.get("nulos", 0) or 0))
        else:
            tratamiento = "Sin nulos" if nulos == 0 else f"Quedan {nulos} vacíos"
        if str(col).startswith("_revisar_calidad"):  # columna de marca de la limpieza clásica
            tratamiento = "No aplica (columna de marca)"
        es_llave = (col in llaves) if llaves is not None else rol == "id"
        descripcion = describir_campo(col, rol, tipo, tratamiento,
                                      (origenes or {}).get(col, ""), es_llave=es_llave,
                                      serie=serie,  # el contenido ayuda cuando el nombre no dice nada
                                      contexto=[str(c) for c in df.columns])  # y las columnas vecinas
        if rol == "coordenada" and tipo_coordenada(col):
            descripcion = f"{tipo_coordenada(col).capitalize()} en grados decimales."
        fila = {
            "N°": i,
            "Campo": col,
            "Descripción": descripcion,
            "Tipo de dato": tipo,
            "Rol": ROLES[rol],
            "Nulos": ("No aplica" if str(col).startswith("_revisar_calidad")
                      else "No" if nulos == 0 else f"Sí ({nulos})"),
            "Completitud %": (np.nan if str(col).startswith("_revisar_calidad")
                              else round((1 - nulos / total) * 100, 1)),
            "Valores únicos": int(serie.nunique(dropna=True)),
            "Rango o ejemplos": resumen_valores(serie, tipo, tokens),
        }
        fila[COLUMNA_ORIGEN] = ""  # lo redacta completar_campos_negocio con `origenes`, `fuentes` y `cruces`
        fila["Tratamiento de nulos"] = tratamiento
        fila[COLUMNA_JUSTIFICACION] = ""
        fila[COLUMNA_CLASIFICACION] = ""
        fila[COLUMNA_MODELO] = ""
        filas.append(fila)
    return completar_campos_negocio(pd.DataFrame(filas), df, nombre_tabla, fuentes, llaves, cruces, origenes,
                                    tokens)


def resumen_tabla(df: pd.DataFrame, nombre: str, diccionario: pd.DataFrame,
                  fuentes: Optional[List[str]] = None,
                  columnas_eliminadas: Optional[List[str]] = None,
                  tokens=TOKENS_NULOS_BASE) -> pd.DataFrame:
    """Hoja de resumen: qué es la tabla, cuántos datos tiene y qué tan
    completa está. Devuelve dos columnas: Dato / Valor."""
    diccionario = completar_campos_negocio(
        completar_descripciones(diccionario, df=df), df, nombre, fuentes)  # una celda borrada se vuelve a redactar
    celdas = max(df.shape[0] * df.shape[1], 1)
    nulas = int(sum(es_nulo(df[c], tokens).sum() for c in df.columns))
    sin_descripcion = int((diccionario["Descripción"].fillna("").astype(str).str.strip() == "").sum())
    sin_negocio = int(sum((diccionario[c].fillna("").astype(str).str.strip() == "").sum()
                          for c in COLUMNAS_NEGOCIO))
    filas = [
        ("Tabla maestra", nombre),
        ("Fecha de generación", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("Filas (registros)", f"{len(df):,}"),
        ("Columnas (campos)", f"{df.shape[1]}"),
        ("Completitud global", f"{round((1 - nulas / celdas) * 100, 1)}%"),
        ("Celdas con nulos", f"{nulas:,}"),
        ("Filas duplicadas", f"{int(df.duplicated().sum()):,}"),
    ]
    if fuentes:
        filas.append(("Fuentes", " | ".join(fuentes)))
    if columnas_eliminadas:
        filas.append(("Columnas eliminadas por nulos", ", ".join(columnas_eliminadas)))
    filas.append(("Campos sin descripción", f"{sin_descripcion} de {len(diccionario)}"))
    filas.append(("Celdas de negocio sin llenar (origen, justificación, clasificación, modelo)",
                  f"{sin_negocio} de {len(diccionario) * len(COLUMNAS_NEGOCIO)}"))
    return pd.DataFrame(filas, columns=["Dato", "Valor"])


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------

def diccionario_a_excel(diccionario: pd.DataFrame, resumen: pd.DataFrame) -> bytes:
    """Excel con las hojas «Resumen» y «Diccionario», con encabezado
    destacado, columnas anchas, filtros y las descripciones pendientes
    resaltadas en amarillo."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    encabezado = PatternFill("solid", fgColor="1F3864")
    pendiente = PatternFill("solid", fgColor="FFF2CC")
    borde = Border(*(Side(style="thin", color="BFBFBF"),) * 4)

    def dar_formato(hoja, anchos: Dict[str, int], columnas_pendientes=()):
        for celda in hoja[1]:
            celda.fill, celda.border = encabezado, borde
            celda.font = Font(bold=True, color="FFFFFF")
            celda.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        nombres = [c.value for c in hoja[1]]
        for letra_idx, nombre in enumerate(nombres, 1):
            letra = hoja.cell(row=1, column=letra_idx).column_letter
            hoja.column_dimensions[letra].width = anchos.get(nombre, 16)
        for fila in hoja.iter_rows(min_row=2):
            for celda in fila:
                celda.border = borde
                celda.alignment = Alignment(vertical="top", wrap_text=True)
                if nombres[celda.column - 1] in columnas_pendientes and not str(celda.value or "").strip():
                    celda.fill = pendiente
        hoja.freeze_panes = "A2"

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as escritor:
        resumen.to_excel(escritor, sheet_name="Resumen", index=False)
        diccionario.to_excel(escritor, sheet_name="Diccionario", index=False)
        dar_formato(escritor.sheets["Resumen"], {"Dato": 32, "Valor": 70})
        hoja = escritor.sheets["Diccionario"]
        dar_formato(hoja, {"N°": 5, "Campo": 24, "Descripción": 40, "Tipo de dato": 14, "Rol": 20,
                           "Nulos": 10, "Completitud %": 13, "Valores únicos": 11,
                           "Rango o ejemplos": 38, "Origen": 34, "Tratamiento de nulos": 34,
                           "Justificación de negocio": 60, COLUMNA_CLASIFICACION: 22, COLUMNA_MODELO: 46},
                    columnas_pendientes=("Descripción",) + COLUMNAS_NEGOCIO)
        hoja.auto_filter.ref = hoja.dimensions
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Diccionario técnico (Excel, legible por máquina)
# --------------------------------------------------------------------------

_TIPO_SUGERIDO = {
    "Entero": "int64", "Decimal": "float64", "Fecha": "datetime64[ns]", "Sí/No": "bool",
    "Categoría": "category", "Texto": "string", "Texto (código)": "string", "Vacío": "string",
}

COLUMNAS_TECNICO = (
    "orden", "nombre_campo", "descripcion", "tipo_dato_nativo", "tipo_dato_sugerido",
    "tipo_semantico", "rol", "es_llave", "acepta_nulos", "nulos", "completitud_pct",
    "valores_unicos", "valor_minimo", "valor_maximo", "longitud_maxima", "ejemplos",
    "origen", "tratamiento_nulos", "justificacion_negocio", "clasificacion_ejecutiva", "modelo_datos",
)

_LEYENDA = {
    "orden": "Posición del campo en la tabla.",
    "nombre_campo": "Nombre exacto de la columna.",
    "descripcion": "Qué representa el campo (redactada a partir del nombre y los datos; se puede reescribir).",
    "tipo_dato_nativo": "Tipo real con el que está guardada la columna (dtype de pandas).",
    "tipo_dato_sugerido": "Tipo recomendado para el modelo (Int64 = entero que admite vacíos).",
    "tipo_semantico": "Tipo en palabras de negocio, según el contenido.",
    "rol": "Qué papel cumple: identificador / llave, fecha, número, texto, etc.",
    "es_llave": "Sí si el campo está clasificado como llave o identificador.",
    "acepta_nulos": "Sí si hay al menos un vacío en el campo.",
    "nulos": "Cantidad de celdas vacías.",
    "completitud_pct": "Porcentaje de filas con dato.",
    "valores_unicos": "Cantidad de valores distintos.",
    "valor_minimo": "Límite lógico inferior (números y fechas).",
    "valor_maximo": "Límite lógico superior (números y fechas).",
    "longitud_maxima": "Largo máximo del texto (campos de texto).",
    "ejemplos": "Rango, categorías o ejemplos de valores.",
    "origen": "De dónde sale el campo: archivo, hoja, tabla, cruce o paso de limpieza que lo generó.",
    "tratamiento_nulos": "Qué se hizo con los vacíos del campo.",
    "justificacion_negocio": "Por qué el campo importa para el negocio, con los datos reales de la columna.",
    "clasificacion_ejecutiva": "KPI, variable transformada, llave, atributo descriptivo o metadato de control "
                               "(las tres primeras son las que explica el documento de alcance).",
    "modelo_datos": "Papel del campo en un modelo analítico: llave primaria o foránea, medida y cómo se agrega, "
                    "atributo de dimensión, tiempo o fuera del modelo.",
}


def _numero_crudo(valor) -> str:
    """Número sin separadores de miles, para que se lea igual en cualquier herramienta."""
    valor = float(valor)
    return str(int(valor)) if valor.is_integer() else str(round(valor, 6))


def _limites(serie: pd.Series, tipo: str, tokens=TOKENS_NULOS_BASE):
    """(mínimo, máximo, longitud máxima) del campo como texto; vacío si no aplica."""
    con_dato = serie[~es_nulo(serie, tokens)]
    if len(con_dato) == 0:
        return "", "", ""
    if tipo in ("Entero", "Decimal"):
        numeros = (con_dato if pd.api.types.is_numeric_dtype(serie) else a_numero(con_dato)).dropna()
        if len(numeros):
            return _numero_crudo(numeros.min()), _numero_crudo(numeros.max()), ""
        return "", "", ""
    if tipo == "Fecha":
        if pd.api.types.is_datetime64_any_dtype(serie):
            fechas = con_dato
        else:
            texto = con_dato.astype(str)
            fechas = pd.to_datetime(texto, errors="coerce", dayfirst=bool(texto.str.match(_FECHA_LATINA).any()))
        fechas = fechas.dropna()
        if len(fechas):
            return f"{fechas.min():%Y-%m-%d}", f"{fechas.max():%Y-%m-%d}", ""
        return "", "", ""
    if tipo == "Sí/No":
        return "", "", ""
    return "", "", str(int(con_dato.astype(str).str.len().max()))


def tabla_tecnica(df: pd.DataFrame, diccionario: pd.DataFrame, tokens=TOKENS_NULOS_BASE) -> pd.DataFrame:
    """Tabla del diccionario técnico: una fila por campo, con nombres de columna en minúsculas
    y sin espacios; tipo nativo (dtype real), tipo sugerido para el modelo, límites lógicos
    (mínimo, máximo, longitud máxima), completitud y las descripciones del diccionario."""
    diccionario = completar_campos_negocio(  # garantiza que ningún campo salga sin descripción ni datos de negocio
        completar_descripciones(diccionario, df=df), df)
    filas = []
    for i, col in enumerate(df.columns):
        d = diccionario.iloc[i] if len(diccionario) == len(df.columns) else \
            diccionario[diccionario["Campo"] == col].iloc[0]
        serie = df[col]
        nulos = int(es_nulo(serie, tokens).sum())
        tipo = str(d["Tipo de dato"])
        minimo, maximo, longitud = _limites(serie, tipo, tokens)
        sugerido = _TIPO_SUGERIDO.get(tipo, "string")
        if sugerido == "int64" and nulos:
            sugerido = "Int64"  # entero que admite vacíos
        marca = str(col).startswith("_revisar_calidad")
        clasif = d.get(COLUMNA_CLASIFICACION, "")
        filas.append({
            "orden": int(d["N°"]),
            "nombre_campo": col,
            "descripcion": d["Descripción"],
            "tipo_dato_nativo": str(serie.dtype),
            "tipo_dato_sugerido": sugerido,
            "tipo_semantico": tipo,
            "rol": d["Rol"],
            "es_llave": "Sí" if clasif == CLASIFICACION_LLAVE else "No",
            "acepta_nulos": "No aplica" if marca else ("Sí" if nulos else "No"),
            "nulos": nulos,
            "completitud_pct": "" if marca else d["Completitud %"],
            "valores_unicos": int(d["Valores únicos"]),
            "valor_minimo": minimo,
            "valor_maximo": maximo,
            "longitud_maxima": longitud,
            "ejemplos": d["Rango o ejemplos"],
            "origen": d[COLUMNA_ORIGEN],
            "tratamiento_nulos": d["Tratamiento de nulos"],
            "justificacion_negocio": d["Justificación de negocio"],
            "clasificacion_ejecutiva": clasif,
            "modelo_datos": d[COLUMNA_MODELO],
        })
    return pd.DataFrame(filas, columns=list(COLUMNAS_TECNICO)).fillna("")


def diccionario_tecnico_excel(df: pd.DataFrame, diccionario: pd.DataFrame, resumen: pd.DataFrame,
                              tokens=TOKENS_NULOS_BASE) -> bytes:
    """Excel del diccionario técnico («diccionario_datos.xlsx») con tres hojas:
    «Diccionario técnico» (una fila por campo), «Resumen» (datos generales de la tabla) y
    «Leyenda» (qué significa cada columna). Encabezado fijo, filtros y las descripciones
    pendientes resaltadas en amarillo. Se puede importar a Power BI o a un catálogo de datos."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    encabezado = PatternFill("solid", fgColor="1F3864")
    pendiente = PatternFill("solid", fgColor="FFF2CC")
    borde = Border(*(Side(style="thin", color="BFBFBF"),) * 4)
    anchos = {"orden": 7, "nombre_campo": 26, "descripcion": 40, "tipo_dato_nativo": 16,
              "tipo_dato_sugerido": 18, "tipo_semantico": 15, "rol": 20, "es_llave": 10,
              "acepta_nulos": 12, "nulos": 9, "completitud_pct": 14, "valores_unicos": 14,
              "valor_minimo": 16, "valor_maximo": 16, "longitud_maxima": 14, "ejemplos": 38,
              "origen": 34, "tratamiento_nulos": 34, "justificacion_negocio": 60,
              "clasificacion_ejecutiva": 22, "modelo_datos": 46, "Dato": 32, "Valor": 70, "columna": 26, "significado": 80}

    def dar_formato(hoja, pendientes=()):
        nombres = [c.value for c in hoja[1]]
        for celda in hoja[1]:
            celda.fill, celda.border = encabezado, borde
            celda.font = Font(bold=True, color="FFFFFF")
            celda.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for idx, nombre in enumerate(nombres, 1):
            hoja.column_dimensions[hoja.cell(row=1, column=idx).column_letter].width = anchos.get(nombre, 16)
        for fila in hoja.iter_rows(min_row=2):
            for celda in fila:
                celda.border = borde
                celda.alignment = Alignment(vertical="top", wrap_text=True)
                if nombres[celda.column - 1] in pendientes and not str(celda.value or "").strip():
                    celda.fill = pendiente
        hoja.freeze_panes = "A2"

    tecnica = tabla_tecnica(df, diccionario, tokens)
    leyenda = pd.DataFrame({"columna": list(COLUMNAS_TECNICO),
                            "significado": [_LEYENDA[c] for c in COLUMNAS_TECNICO]})
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as escritor:
        tecnica.to_excel(escritor, sheet_name="Diccionario técnico", index=False)
        resumen.to_excel(escritor, sheet_name="Resumen", index=False)
        leyenda.to_excel(escritor, sheet_name="Leyenda", index=False)
        hoja = escritor.sheets["Diccionario técnico"]
        dar_formato(hoja, pendientes=("descripcion", "origen", "justificacion_negocio",
                                      "clasificacion_ejecutiva", "modelo_datos"))
        hoja.auto_filter.ref = hoja.dimensions
        dar_formato(escritor.sheets["Resumen"])
        dar_formato(escritor.sheets["Leyenda"])
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Documento de alcance (Word, para quien decide)
# --------------------------------------------------------------------------

_PENDIENTE = "(pendiente de describir)"


_NOMBRES_CRUCE = {
    "left": "izquierdo (conserva todo A)", "inner": "interno (solo coincidencias)",
    "right": "derecho (conserva todo B)", "outer": "completo (conserva todo)",
}


def _nombre_cruce(how) -> str:
    return _NOMBRES_CRUCE.get(str(how), str(how or ""))


def _hipervinculo(parrafo, texto: str, destino: str) -> None:
    """Agrega un hipervínculo (ruta relativa o URL) al final del párrafo."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    r_id = parrafo.part.relate_to(
        destino, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True)
    enlace = OxmlElement("w:hyperlink")
    enlace.set(qn("r:id"), r_id)
    corrida = OxmlElement("w:r")
    formato = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    subrayado = OxmlElement("w:u")
    subrayado.set(qn("w:val"), "single")
    formato.append(color)
    formato.append(subrayado)
    corrida.append(formato)
    contenido = OxmlElement("w:t")
    contenido.text = texto
    contenido.set(qn("xml:space"), "preserve")
    corrida.append(contenido)
    enlace.append(corrida)
    parrafo._p.append(enlace)


def _tabla_word(doc, encabezados: List[str], filas: List[List[str]], anchos_cm: List[float]) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    tabla = doc.add_table(rows=1, cols=len(encabezados))
    tabla.style = "Table Grid"
    tabla.autofit = False
    for celda, texto in zip(tabla.rows[0].cells, encabezados):
        celda.text = ""
        corrida = celda.paragraphs[0].add_run(texto)
        corrida.bold = True
        corrida.font.size = Pt(10)
        corrida.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        celda.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.LEFT
        relleno = OxmlElement("w:shd")
        relleno.set(qn("w:val"), "clear")
        relleno.set(qn("w:fill"), "1F3864")
        celda._tc.get_or_add_tcPr().append(relleno)
    for fila in filas:
        celdas = tabla.add_row().cells
        for celda, texto in zip(celdas, fila):
            celda.text = ""
            celda.paragraphs[0].add_run(str(texto)).font.size = Pt(10)
    for fila in tabla.rows:
        for celda, ancho in zip(fila.cells, anchos_cm):
            celda.width = Cm(ancho)
    doc.add_paragraph()


_MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre")


def _n(valor) -> str:
    """Miles con punto, como en español: 6512 -> '6.512'."""
    return f"{int(valor):,}".replace(",", ".")


def _pct(valor) -> str:
    """'100 %' o '97,5 %'."""
    valor = float(valor)
    if pd.isna(valor):
        return "No aplica"
    return (f"{int(valor)} %" if valor.is_integer() else f"{valor:.1f}".replace(".", ",") + " %")


def _frase(texto: str) -> str:
    """Texto limpio y terminado en punto."""
    texto = str(texto or "").strip()
    return texto if not texto or texto[-1] in ".!?" else texto + "."


def _fecha_larga(fecha: datetime) -> str:
    return f"{fecha.day} de {_MESES[fecha.month - 1]} de {fecha.year}"


def _campo_pagina(parrafo) -> None:
    """Inserta el número de página (campo PAGE) al final del párrafo."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    campo = OxmlElement("w:fldSimple")
    campo.set(qn("w:instr"), "PAGE")
    corrida = OxmlElement("w:r")
    texto = OxmlElement("w:t")
    texto.text = "1"
    corrida.append(texto)
    campo.append(corrida)
    parrafo._p.append(campo)


def _parrafo_destacado(doc, etiqueta: str, texto: str) -> None:
    """Párrafo con fondo suave y la etiqueta en negrita («Conclusión ejecutiva.», etc.)."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    parrafo = doc.add_paragraph()
    parrafo.add_run(f"{etiqueta} ").bold = True
    parrafo.add_run(texto)
    relleno = OxmlElement("w:shd")
    relleno.set(qn("w:val"), "clear")
    relleno.set(qn("w:fill"), "EAF0FA")
    parrafo._p.get_or_add_pPr().append(relleno)


def _parrafo_con_etiqueta(doc, etiqueta: str, texto: str) -> None:
    parrafo = doc.add_paragraph()
    parrafo.add_run(f"{etiqueta} ").bold = True
    parrafo.add_run(texto)


def documento_alcance_docx(df: pd.DataFrame, diccionario: pd.DataFrame, nombre: str,
                           fuentes: Optional[List[str]] = None,
                           cruces: Optional[List[Dict]] = None,
                           columnas_eliminadas: Optional[List[str]] = None,
                           nombre_tecnico: str = NOMBRE_TECNICO,
                           textos: Optional[Dict[str, str]] = None,
                           etapas: Optional[List[tuple]] = None,
                           controles_extra: Optional[List[tuple]] = None,
                           pendientes_extra: Optional[List[tuple]] = None) -> bytes:
    """Documento de alcance y diccionario ejecutivo en Word, para quien decide.

    Estructura (la misma de un documento de alcance de proyecto):
      1 Propósito y alcance         (conclusión ejecutiva y tabla Etapa / Resultado)
      2 Arquitectura final y variables críticas del negocio  (unidad de análisis, tabla de
        llaves, KPIs y variables transformadas, y la referencia al Excel técnico)
      3 Preparación de datos y resultados del proceso        (tabla de controles y faltantes)
      4 Indicadores y condiciones para su uso                (aspectos pendientes)
      5 Recomendación para la siguiente fase, y fuentes documentales.
    No copia la tabla completa de campos: ese detalle vive en el Excel técnico.

    Lo que se calcula de los datos va solo (tamaños, completitud, unicidad de llaves, faltantes,
    campos con tipo equivocado). Lo que solo sabe la persona se pasa en `textos`, con las claves
    opcionales: proyecto, autor, proposito, conclusion, alcance, unidad_analisis, preparacion,
    recomendacion, fuentes_documentales. Si falta una, se redacta un texto neutro.
    `cruces`: [{tabla_a, col_a, tabla_b, col_b, cruce}] de las uniones hechas.
    `etapas`, `controles_extra`, `pendientes_extra`: filas (tuplas) que se suman a las tablas.
    Requiere python-docx."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    diccionario = completar_campos_negocio(  # ningún campo queda «pendiente de describir» ni sin datos de negocio
        completar_descripciones(diccionario, df=df), df, nombre, fuentes, cruces=cruces)
    textos = {k: str(v).strip() for k, v in (textos or {}).items() if v and str(v).strip()}
    proyecto = textos.get("proyecto", "Proyecto de análisis de datos")
    total, filas_df = len(diccionario), len(df)
    hoy = datetime.now()

    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)
    encabezado = doc.sections[0].header.paragraphs[0]
    encabezado.text = f"{proyecto} | Diccionario ejecutivo "
    _campo_pagina(encabezado)
    for corrida in encabezado.runs:
        corrida.font.size = Pt(9)

    # ---- portada
    doc.add_heading("Documento de alcance y diccionario ejecutivo", level=0)
    doc.add_paragraph(f"Tabla maestra «{nombre}»").runs[0].italic = True
    firma = " | ".join(x for x in (textos.get("autor", ""), _fecha_larga(hoy)) if x)
    doc.add_paragraph(firma).runs[0].italic = True

    # ---- datos calculados que se reutilizan
    nulos_por_col = {c: int(es_nulo(df[c]).sum()) for c in df.columns if not str(c).startswith("_revisar_calidad")}
    celdas = max(filas_df * len(nulos_por_col), 1)
    completitud_global = (1 - sum(nulos_por_col.values()) / celdas) * 100
    duplicadas = int(df.duplicated().sum())
    clasif = diccionario.get(COLUMNA_CLASIFICACION, pd.Series("", index=diccionario.index)).fillna("")

    def campos(etiqueta: str) -> pd.DataFrame:
        return diccionario[clasif == etiqueta]

    def fila_de(campo: str):
        f = diccionario[diccionario["Campo"] == campo]
        return f.iloc[0] if len(f) else None

    def unicidad(campo: str) -> str:
        if campo not in df.columns:
            return ""
        con_dato = df[campo][~es_nulo(df[campo])]
        if len(con_dato) == 0:
            return "sin datos"
        distintos = int(con_dato.nunique())
        return ("única por registro" if distintos == len(con_dato)
                else f"se repite ({_n(distintos)} valores distintos en {_n(len(con_dato))} filas)")

    def descripcion(fila) -> str:
        return _frase(fila["Descripción"]) or "Pendiente de describir en el diccionario."

    llaves = campos(CLASIFICACION_LLAVE)
    kpis = campos(CLASIFICACION_KPI)
    transformadas = campos(CLASIFICACION_TRANSFORMADA)

    # ---- 1. Propósito y alcance
    doc.add_heading("1 Propósito y alcance", level=1)
    doc.add_paragraph(textos.get("proposito") or (
        f"Este documento describe, en lenguaje de negocio, la tabla maestra «{nombre}»: de qué está "
        "hecha, qué tan completa es y cuáles son las variables que sostienen las decisiones. No "
        "reproduce el listado completo de campos; ese detalle está en el diccionario técnico."))
    _parrafo_destacado(doc, "Conclusión ejecutiva.", textos.get("conclusion") or (
        f"La tabla final reúne {_n(filas_df)} filas y {total} campos, con una completitud global de "
        f"{_pct(round(completitud_global, 1))} y {_n(duplicadas)} filas duplicadas. Estos controles describen la "
        "estructura y la cobertura de los datos; por sí solos no certifican la exactitud de los valores."))
    filas_etapas = list(etapas or [])
    if not filas_etapas:
        if fuentes:
            filas_etapas.append(("Fuentes", " | ".join(fuentes)))
        filas_etapas.append(("Tabla maestra final", f"{_n(filas_df)} filas y {total} columnas"))
    if columnas_eliminadas:
        filas_etapas.append(("Columnas eliminadas por exceso de vacíos", ", ".join(columnas_eliminadas)))
    _tabla_word(doc, ["Etapa", "Resultado registrado"], [list(f) for f in filas_etapas], [5.5, 11.0])
    if textos.get("alcance"):
        doc.add_paragraph(textos["alcance"])

    # ---- 2. Arquitectura y variables críticas
    doc.add_heading("2 Arquitectura final y variables críticas del negocio", level=1)
    por_rol = diccionario["Rol"].value_counts()
    composicion = ", ".join(f"{n} de tipo {rol}" for rol, n in por_rol.items())
    arquitectura = f"La tabla maestra reúne {_n(filas_df)} registros y {total} campos ({composicion})."
    if fuentes:
        arquitectura += " Se construyó a partir de: " + "; ".join(fuentes) + "."
    doc.add_paragraph(arquitectura)
    if cruces:
        doc.add_paragraph("Las fuentes se unieron así:")
        for c in cruces:
            doc.add_paragraph(f"{c['tabla_a']} ({c['col_a']}) con {c['tabla_b']} ({c['col_b']}); "
                              f"cruce {_nombre_cruce(c.get('cruce'))}.", style="List Bullet")
    unidad = textos.get("unidad_analisis")
    if not unidad and len(llaves):
        primera = str(llaves.iloc[0]["Campo"])
        if unicidad(primera) == "única por registro":
            unidad = f"Una fila por {primera}."
    if unidad:
        _parrafo_destacado(doc, "Unidad de análisis.", unidad)

    filas_vars = []
    for _, r in llaves.iterrows():
        campo = r["Campo"]
        une = [c for c in (cruces or []) if campo in (c["col_a"], c["col_b"])]
        partes = ["Llave."]
        if une:
            partes.append(f"Une {une[0]['tabla_a']} con {une[0]['tabla_b']}.")
        partes += [descripcion(r), f"{_pct(r['Completitud %'])} con dato; {unicidad(campo)}.",
                   _frase(r[COLUMNA_MODELO])]
        filas_vars.append([campo, " ".join(partes)])
    for _, r in kpis.iterrows():
        filas_vars.append([r["Campo"], " ".join(x for x in (
            "KPI.", descripcion(r), _frase(r["Justificación de negocio"]), _frase(r[COLUMNA_MODELO])) if x)])
    for _, r in transformadas.iterrows():
        filas_vars.append([r["Campo"], " ".join(x for x in (
            "Variable transformada.", descripcion(r), _frase(r["Tratamiento de nulos"]),
            _frase(r["Justificación de negocio"])) if x)])
    if filas_vars:
        _tabla_word(doc, ["Variable o llave", "Significado y función ejecutiva"], filas_vars, [4.5, 12.0])
    else:
        doc.add_paragraph(f"Todavía no se han marcado variables críticas. Márquelas (KPI, variable transformada "
                          f"o llave) en la columna «{COLUMNA_CLASIFICACION}» del diccionario y vuelva a "
                          "generar este documento.")
    if not len(kpis) and filas_vars:
        doc.add_paragraph(f"No se han marcado KPIs; márquelos en la columna «{COLUMNA_CLASIFICACION}».")

    referencia = doc.add_paragraph()
    referencia.add_run("Referencia técnica. ").bold = True
    referencia.add_run(f"El detalle técnico exhaustivo de los {total} campos estructurales se encuentra "
                       "en el archivo adjunto “")
    _hipervinculo(referencia, nombre_tecnico, nombre_tecnico)
    referencia.add_run("”.")

    # ---- 3. Preparación y resultados
    doc.add_heading("3 Preparación de datos y resultados del proceso", level=1)
    if textos.get("preparacion"):
        for bloque in textos["preparacion"].split("\n\n"):
            doc.add_paragraph(bloque.strip())
    tratados = [(r["Campo"], r["Tratamiento de nulos"]) for _, r in diccionario.iterrows()
                if str(r["Tratamiento de nulos"]).startswith(_TRATAMIENTOS_QUE_TRANSFORMAN)]
    if tratados:
        ejemplos = "; ".join(f"{c} ({t[:1].lower() + t[1:]})" for c, t in tratados[:6])
        mas = f" y {len(tratados) - 6} más" if len(tratados) > 6 else ""
        _parrafo_con_etiqueta(doc, "Tratamiento de vacíos.",
                              f"Se aplicó una regla a {len(tratados)} {'campo' if len(tratados) == 1 else 'campos'}: {ejemplos}{mas}.")
    if columnas_eliminadas:
        doc.add_paragraph("Se descartaron por exceso de vacíos: " + ", ".join(columnas_eliminadas) + ".")

    controles = [("Tamaño de la base final", f"{_n(filas_df)} filas y {total} columnas",
                  "Estructura de la tabla maestra entregada.")]
    for _, r in llaves.head(6).iterrows():
        campo = r["Campo"]
        unica = unicidad(campo)
        if r["Completitud %"] < 100:
            lectura = "Las filas sin valor no cruzan con otras tablas."
        elif unica == "única por registro":
            lectura = "Identifica cada registro sin repetirse."
        else:
            lectura = "Se repite: relación de uno a muchos; no sirve como identificador único."
        controles.append((f"Llave {campo}", f"{_pct(r['Completitud %'])} con dato; {unica}", lectura))
    controles.append(("Filas duplicadas", _n(duplicadas),
                      "Sin duplicados completos." if not duplicadas else "Revisar y depurar los duplicados."))
    controles.append(("Completitud global", _pct(round(completitud_global, 1)),
                      "Proporción de celdas con dato en toda la tabla."))
    controles += list(controles_extra or [])
    _tabla_word(doc, ["Control registrado", "Resultado", "Lectura ejecutiva"],
                [list(c) for c in controles], [4.2, 5.4, 6.9])

    con_nulos = sorted(((c, n) for c, n in nulos_por_col.items() if n), key=lambda x: -x[1])
    if con_nulos:
        lista = ", ".join(f"{_n(n)} de {c}" for c, n in con_nulos[:4])
        entre = "entre otros, " if len(con_nulos) > 4 else ""
        _parrafo_con_etiqueta(doc, "Faltantes que permanecen.",
                              f"La tabla conserva, {entre}{lista} valores faltantes. Por ello, la base no debe "
                              "describirse como completamente libre de nulos.")
    else:
        _parrafo_con_etiqueta(doc, "Faltantes que permanecen.", "La tabla no tiene valores faltantes.")

    # ---- 4. Condiciones para su uso
    doc.add_heading("4 Indicadores y condiciones para su uso", level=1)
    if len(kpis):
        doc.add_paragraph("Indicadores marcados: " + ", ".join(kpis["Campo"]) + ". Cada uno debe calcularse con "
                          "una definición y un período documentados antes de usarse para decidir.")
    pendientes = []
    sin_desc = int((diccionario["Descripción"].fillna("").astype(str).str.strip() == "").sum())
    if sin_desc:
        pendientes.append(("Descripciones", f"{sin_desc} de {total} campos no tienen descripción. "
                           "Completarla en el diccionario antes de entregar."))
    if not len(kpis):
        pendientes.append(("KPIs", "No se marcaron indicadores clave. Definirlos y marcarlos para que el "
                           "documento los explique."))
    texto_tipo = [str(r["Campo"]) for i, (_, r) in enumerate(diccionario.iterrows())
                  if r["Tipo de dato"] in ("Entero", "Decimal", "Fecha")
                  and not (pd.api.types.is_numeric_dtype(df.iloc[:, i]) or
                           pd.api.types.is_datetime64_any_dtype(df.iloc[:, i]))]
    if texto_tipo:
        pendientes.append(("Tipos de datos", "Estos campos son números o fechas guardados como texto: "
                           + ", ".join(texto_tipo[:8]) + (" y otros" if len(texto_tipo) > 8 else "")
                           + ". Convertirlos antes de sumar, promediar u ordenar."))
    bajos = diccionario[diccionario["Completitud %"].fillna(100) < 95]
    if len(bajos):
        pendientes.append(("Completitud", "Campos con menos de 95 % de datos: " + ", ".join(
            f"{r['Campo']} ({_pct(r['Completitud %'])})" for _, r in bajos.head(8).iterrows())
            + ". Confirmar si la ausencia es esperada o un error."))
    if duplicadas:
        pendientes.append(("Duplicados", f"{_n(duplicadas)} filas están duplicadas. Depurarlas para no contar "
                           "dos veces el mismo registro."))
    pendientes += list(pendientes_extra or [])
    if pendientes:
        _tabla_word(doc, ["Aspecto pendiente", "Implicación y acción necesaria"],
                    [list(p) for p in pendientes], [4.5, 12.0])
    else:
        doc.add_paragraph("No se detectaron aspectos pendientes.")

    # ---- 5. Recomendación y fuentes
    doc.add_heading("5 Recomendación para la siguiente fase", level=1)
    doc.add_paragraph(textos.get("recomendacion") or (
        "Resolver primero los aspectos señalados en la sección 4, completar las descripciones y marcar los "
        "indicadores clave; después calcularlos con la definición y el período acordados y acompañarlos del "
        "número de registros que los sustentan."))
    fuentes_doc = textos.get("fuentes_documentales") or "; ".join(
        list(fuentes or []) + [f"{nombre_tecnico} (diccionario técnico)"])
    p_fuentes = doc.add_paragraph()
    p_fuentes.add_run("Fuentes documentales: ").bold = True
    p_fuentes.add_run(_frase(fuentes_doc))

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()

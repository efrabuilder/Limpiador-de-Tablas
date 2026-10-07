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
    Valores únicos, Rango o ejemplos, Tratamiento de nulos,
    Justificación de negocio  (+ Origen si se sabe de qué tabla viene)

Se exporta en tres formatos:
    - Diccionario técnico (CSV, «diccionario_datos.csv»): una fila por campo, con tipo
      nativo (int64, float64, datetime64[ns]...), límites lógicos y metadatos legibles
      por máquina. Sirve para catalogadores de datos y para importar a Power BI.
    - Documento de alcance / diccionario ejecutivo (Word): resumen para quien decide.
      Explica la arquitectura final y solo las variables críticas (KPIs, variables
      transformadas y llaves de unión), y remite al CSV para el detalle de los campos.
    - Excel con dos hojas (Resumen y Diccionario), para revisar y completar.
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

COLUMNA_CLASIFICACION = "Clasificación ejecutiva"
CLASIFICACION_KPI = "KPI"
CLASIFICACION_TRANSFORMADA = "Variable transformada"
CLASIFICACION_LLAVE = "Llave / identificador"
CLASIFICACIONES = (CLASIFICACION_KPI, CLASIFICACION_TRANSFORMADA, CLASIFICACION_LLAVE)

COLUMNAS_EDITABLES = ("Descripción", "Justificación de negocio", COLUMNA_CLASIFICACION)

NOMBRE_CSV_TECNICO = "diccionario_datos.csv"

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

_TRATAMIENTOS_QUE_TRANSFORMAN = ("Rellenados", "Celdas vacías convertidas", "Se eliminaron")


def _clasificacion_inicial(col, rol: str, tratamiento: str, llaves) -> str:
    """Clasificación ejecutiva sugerida. Los KPIs no se adivinan: los marca la persona."""
    if str(col).startswith("_revisar_calidad") or col == "_merge":
        return ""
    es_llave = (col in llaves) if llaves is not None else rol == "id"
    if es_llave:
        return CLASIFICACION_LLAVE
    if str(tratamiento).startswith(_TRATAMIENTOS_QUE_TRANSFORMAN):
        return CLASIFICACION_TRANSFORMADA
    return ""


def construir_diccionario(df: pd.DataFrame, reglas: Optional[List[Dict]] = None,
                          origenes: Optional[Dict[str, str]] = None,
                          tokens=TOKENS_NULOS_BASE,
                          llaves: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """Diccionario de datos de `df` (una fila por campo).
    `reglas`: lista [{columna, regla, valor, grupo, nulos}] de la limpieza
    guiada (o [{columna, descripcion}] ya redactadas, ver
    reglas_desde_registro), para contar qué se hizo con los nulos de cada campo.
    `origenes`: {columna: texto} con la tabla de la que viene cada campo
    (útil en una tabla maestra que sale de un merge).
    `llaves`: columnas usadas para unir tablas; si no se dan, se toman como
    llaves las de rol identificador.
    «Descripción», «Justificación de negocio» y «Clasificación ejecutiva» (KPI,
    variable transformada o llave) quedan para completar; la clasificación se
    adelanta solo para llaves y para campos con tratamiento de nulos aplicado."""
    por_columna = {r["columna"]: r for r in (reglas or [])}
    cols_fecha = columnas_fecha_por_nombre(df)
    total = max(len(df), 1)
    filas = []
    for i, col in enumerate(df.columns, 1):
        serie = df[col]
        nulos = int(es_nulo(serie, tokens).sum())
        rol = rol_columna(df, col, cols_fecha, tokens)
        tipo = tipo_semantico(serie, rol, tokens)
        descripcion = _DESCRIPCION_POR_ROL.get(rol, "")
        if rol == "coordenada":
            lado = tipo_coordenada(col)
            descripcion = (f"{lado.capitalize()} en grados decimales." if lado
                           else "Coordenada geográfica en grados decimales.")
        if reglas is None:
            tratamiento = ""
        elif col in por_columna:
            r = por_columna[col]
            tratamiento = r.get("descripcion") or describir_regla(
                r["regla"], r.get("valor", ""), r.get("grupo", ""), int(r.get("nulos", 0) or 0))
        else:
            tratamiento = "Sin nulos" if nulos == 0 else f"Quedan {nulos} vacíos"
        if str(col).startswith("_revisar_calidad"):  # columna de marca de la limpieza clásica
            descripcion = ("Marca interna de la limpieza: lista los hallazgos que se dejaron solo "
                           "marcados en esa fila (vacío si no hay ninguno).")
            tratamiento = "No aplica (columna de marca)"
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
        if origenes is not None:
            fila["Origen"] = origenes.get(col, "")
        fila["Tratamiento de nulos"] = tratamiento
        fila["Justificación de negocio"] = ""
        fila[COLUMNA_CLASIFICACION] = _clasificacion_inicial(col, rol, tratamiento, llaves)
        filas.append(fila)
    return pd.DataFrame(filas)


def resumen_tabla(df: pd.DataFrame, nombre: str, diccionario: pd.DataFrame,
                  fuentes: Optional[List[str]] = None,
                  columnas_eliminadas: Optional[List[str]] = None,
                  tokens=TOKENS_NULOS_BASE) -> pd.DataFrame:
    """Hoja de resumen: qué es la tabla, cuántos datos tiene y qué tan
    completa está. Devuelve dos columnas: Dato / Valor."""
    celdas = max(df.shape[0] * df.shape[1], 1)
    nulas = int(sum(es_nulo(df[c], tokens).sum() for c in df.columns))
    sin_descripcion = int((diccionario["Descripción"].fillna("").astype(str).str.strip() == "").sum())
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
                           "Rango o ejemplos": 38, "Origen": 22, "Tratamiento de nulos": 34,
                           "Justificación de negocio": 44, COLUMNA_CLASIFICACION: 22},
                    columnas_pendientes=("Descripción",))
        hoja.auto_filter.ref = hoja.dimensions
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Diccionario técnico (CSV, legible por máquina)
# --------------------------------------------------------------------------

_TIPO_SUGERIDO = {
    "Entero": "int64", "Decimal": "float64", "Fecha": "datetime64[ns]", "Sí/No": "bool",
    "Categoría": "category", "Texto": "string", "Texto (código)": "string", "Vacío": "string",
}

COLUMNAS_CSV_TECNICO = (
    "orden", "nombre_campo", "descripcion", "tipo_dato_nativo", "tipo_dato_sugerido",
    "tipo_semantico", "rol", "es_llave", "acepta_nulos", "nulos", "completitud_pct",
    "valores_unicos", "valor_minimo", "valor_maximo", "longitud_maxima", "ejemplos",
    "origen", "tratamiento_nulos", "justificacion_negocio", "clasificacion_ejecutiva",
)


def _numero_crudo(valor) -> str:
    """Número sin separadores de miles, para que el CSV se lea igual en cualquier herramienta."""
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


def diccionario_tecnico_csv(df: pd.DataFrame, diccionario: pd.DataFrame,
                            tokens=TOKENS_NULOS_BASE) -> bytes:
    """CSV del diccionario técnico (UTF-8 con BOM, para que Excel y Power BI lean bien los
    acentos). Una fila por campo, con nombres de columna en minúsculas y sin espacios:
    tipo nativo (dtype real de la tabla), tipo sugerido para el modelo (int64, float64,
    datetime64[ns], bool, category, string), límites lógicos (mínimo, máximo, longitud
    máxima), completitud y las descripciones escritas en el diccionario.
    Se puede cargar en un catálogo de datos o importar a Power BI para poner las
    descripciones en los campos."""
    filas = []
    tiene_origen = "Origen" in diccionario.columns
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
        filas.append({
            "orden": int(d["N°"]),
            "nombre_campo": col,
            "descripcion": d["Descripción"],
            "tipo_dato_nativo": str(serie.dtype),
            "tipo_dato_sugerido": sugerido,
            "tipo_semantico": tipo,
            "rol": d["Rol"],
            "es_llave": "Sí" if d.get(COLUMNA_CLASIFICACION, "") == CLASIFICACION_LLAVE else "No",
            "acepta_nulos": "No aplica" if marca else ("Sí" if nulos else "No"),
            "nulos": nulos,
            "completitud_pct": "" if marca else d["Completitud %"],
            "valores_unicos": int(d["Valores únicos"]),
            "valor_minimo": minimo,
            "valor_maximo": maximo,
            "longitud_maxima": longitud,
            "ejemplos": d["Rango o ejemplos"],
            "origen": d["Origen"] if tiene_origen else "",
            "tratamiento_nulos": d["Tratamiento de nulos"],
            "justificacion_negocio": d["Justificación de negocio"],
            "clasificacion_ejecutiva": d.get(COLUMNA_CLASIFICACION, ""),
        })
    salida = pd.DataFrame(filas, columns=list(COLUMNAS_CSV_TECNICO)).fillna("")
    return salida.to_csv(index=False).encode("utf-8-sig")


# --------------------------------------------------------------------------
# Documento de alcance (Word, para quien decide)
# --------------------------------------------------------------------------

_PENDIENTE = "(pendiente de describir)"


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


def documento_alcance_docx(df: pd.DataFrame, diccionario: pd.DataFrame, resumen: pd.DataFrame,
                           nombre: str, fuentes: Optional[List[str]] = None,
                           uniones: Optional[List[str]] = None,
                           columnas_eliminadas: Optional[List[str]] = None,
                           nombre_csv: str = NOMBRE_CSV_TECNICO) -> bytes:
    """Documento de alcance (diccionario ejecutivo) en Word, para quien decide.

    No copia la tabla completa de campos: la Sección 2 resume la arquitectura final y
    explica solo las variables críticas (KPIs, variables calculadas o transformadas y
    llaves de unión), y remite al CSV técnico para el detalle de cada campo.
    Requiere python-docx."""
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)
    total = len(diccionario)

    doc.add_heading(f"Documento de alcance: {nombre}", level=0)
    doc.add_paragraph(f"Diccionario ejecutivo de la tabla maestra · generado el "
                      f"{datetime.now():%Y-%m-%d}").runs[0].italic = True

    # ---- 1. Alcance
    doc.add_heading("1. Alcance y resumen", level=1)
    doc.add_paragraph(
        f"Este documento describe, en lenguaje de negocio, la tabla maestra «{nombre}»: de qué "
        "está hecha, qué tan completa es y cuáles son las variables que sostienen las decisiones. "
        "No reproduce el listado completo de campos; ese detalle vive en el diccionario técnico.")
    _tabla_word(doc, ["Dato", "Valor"],
                [[r["Dato"], r["Valor"]] for _, r in resumen.iterrows()], [5.5, 11.0])

    # ---- 2. Arquitectura y variables críticas
    doc.add_heading("2. Arquitectura final y variables críticas de negocio", level=1)
    doc.add_heading("2.1 Arquitectura final", level=2)
    por_rol = diccionario["Rol"].value_counts()
    composicion = ", ".join(f"{n} de tipo {rol}" for rol, n in por_rol.items())
    texto = (f"La tabla maestra reúne {len(df):,} registros y {total} campos ({composicion}).")
    if fuentes:
        texto += " Se construyó a partir de: " + "; ".join(fuentes) + "."
    doc.add_paragraph(texto)
    if uniones:
        doc.add_paragraph("Cómo se unieron las fuentes:")
        for union in uniones:
            doc.add_paragraph(union, style="List Bullet")
    if columnas_eliminadas:
        doc.add_paragraph("Se descartaron por exceso de datos faltantes: " + ", ".join(columnas_eliminadas) + ".")

    clasif = diccionario.get(COLUMNA_CLASIFICACION, pd.Series("", index=diccionario.index)).fillna("")

    def campos(etiqueta: str) -> pd.DataFrame:
        return diccionario[clasif == etiqueta]

    def descripcion(fila) -> str:
        return str(fila["Descripción"]).strip() or _PENDIENTE

    doc.add_heading("2.2 Llaves de unión", level=2)
    llaves = campos(CLASIFICACION_LLAVE)
    if len(llaves):
        doc.add_paragraph("Campos que identifican cada registro o que permiten cruzar las fuentes.")
        _tabla_word(doc, ["Llave", "Qué identifica", "Completitud"],
                    [[r["Campo"], descripcion(r), f"{r['Completitud %']}%"] for _, r in llaves.iterrows()],
                    [4.0, 9.5, 3.0])
    else:
        doc.add_paragraph("No se marcaron llaves.")

    doc.add_heading("2.3 Indicadores clave (KPIs)", level=2)
    kpis = campos(CLASIFICACION_KPI)
    if len(kpis):
        _tabla_word(doc, ["KPI", "Qué mide", "Por qué importa para el negocio"],
                    [[r["Campo"], descripcion(r), str(r["Justificación de negocio"]).strip() or _PENDIENTE]
                     for _, r in kpis.iterrows()], [3.8, 6.2, 6.5])
    else:
        doc.add_paragraph("Todavía no se han marcado KPIs. Márquelos con «KPI» en la columna "
                          f"«{COLUMNA_CLASIFICACION}» del diccionario y vuelva a generar este documento.")

    doc.add_heading("2.4 Variables calculadas o transformadas", level=2)
    transformadas = campos(CLASIFICACION_TRANSFORMADA)
    if len(transformadas):
        _tabla_word(doc, ["Variable", "Qué significa", "Qué se hizo con ella"],
                    [[r["Campo"], descripcion(r), r["Tratamiento de nulos"]]
                     for _, r in transformadas.iterrows()], [3.8, 6.2, 6.5])
    else:
        doc.add_paragraph("No se marcaron variables transformadas.")

    cruce = doc.add_paragraph(f"El detalle técnico exhaustivo de los {total} campos estructurales "
                              "se encuentra en el archivo adjunto '")
    _hipervinculo(cruce, nombre_csv, nombre_csv)
    cruce.add_run("'.")

    # ---- 3. Calidad
    doc.add_heading("3. Calidad de los datos y pendientes", level=1)
    sin_desc = int((diccionario["Descripción"].fillna("").astype(str).str.strip() == "").sum())
    doc.add_paragraph(
        f"Campos todavía sin descripción: {sin_desc} de {total}."
        if sin_desc else "Todos los campos cuentan con descripción.", style="List Bullet")
    incompletos = diccionario[diccionario["Completitud %"].fillna(100) < 95]
    if len(incompletos):
        doc.add_paragraph("Campos con menos de 95% de completitud: " + ", ".join(
            f"{r['Campo']} ({r['Completitud %']}%)" for _, r in incompletos.iterrows()) + ".",
            style="List Bullet")
    else:
        doc.add_paragraph("Ningún campo tiene menos de 95% de completitud.", style="List Bullet")

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()

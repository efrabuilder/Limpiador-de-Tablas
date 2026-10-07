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

Se exporta a un Excel con dos hojas: Resumen y Diccionario.
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Dict, List, Optional

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

COLUMNAS_EDITABLES = ("Descripción", "Justificación de negocio")

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
    }
    return frases.get(regla, REGLAS_NULOS.get(regla, regla)) + cantidad


# --------------------------------------------------------------------------
# Diccionario y resumen
# --------------------------------------------------------------------------

def construir_diccionario(df: pd.DataFrame, reglas: Optional[List[Dict]] = None,
                          origenes: Optional[Dict[str, str]] = None,
                          tokens=TOKENS_NULOS_BASE) -> pd.DataFrame:
    """Diccionario de datos de `df` (una fila por campo).
    `reglas`: lista [{columna, regla, valor, grupo, nulos}] de la limpieza
    guiada, para contar qué se hizo con los nulos de cada campo.
    `origenes`: {columna: texto} con la tabla de la que viene cada campo
    (útil en una tabla maestra que sale de un merge). Las columnas
    «Descripción» y «Justificación de negocio» quedan para completar."""
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
            tratamiento = describir_regla(r["regla"], r.get("valor", ""), r.get("grupo", ""),
                                          int(r.get("nulos", 0) or 0))
        else:
            tratamiento = "Sin nulos" if nulos == 0 else f"Quedan {nulos} vacíos"
        fila = {
            "N°": i,
            "Campo": col,
            "Descripción": descripcion,
            "Tipo de dato": tipo,
            "Rol": ROLES[rol],
            "Nulos": "No" if nulos == 0 else f"Sí ({nulos})",
            "Completitud %": round((1 - nulos / total) * 100, 1),
            "Valores únicos": int(serie.nunique(dropna=True)),
            "Rango o ejemplos": resumen_valores(serie, tipo, tokens),
        }
        if origenes is not None:
            fila["Origen"] = origenes.get(col, "")
        fila["Tratamiento de nulos"] = tratamiento
        fila["Justificación de negocio"] = ""
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
                           "Justificación de negocio": 44},
                    columnas_pendientes=("Descripción",))
        hoja.auto_filter.ref = hoja.dimensions
    return buffer.getvalue()

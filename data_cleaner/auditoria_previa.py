# -*- coding: utf-8 -*-
"""
auditoria_previa.py
===================
Auditoria de una tabla ANTES de limpiarla o cruzarla (checklist ETL, fase 1): los cinco puntos que cada tabla
individual debe pasar en Python antes de cualquier limpieza profunda o merge.

    1. Tamano           -> filas y columnas reales                     (df.shape)
    2. Tipos de dato    -> numeros como numero y fechas como fecha, o
                           Python lo leyo todo como texto (object)     (df.info())
    3. Nulos            -> valores faltantes por columna               (df.isnull().sum())
    4. Duplicados       -> transacciones clonadas por error            (df.duplicated().sum())
    5. Logica de negocio-> valores fisicamente imposibles (precios
                           negativos, edades de 150 anos) y totales que
                           no cuadran con cantidad x precio            (df.describe())

Sin ninguna dependencia de Streamlit. Solo mide: no modifica la tabla.

    auditoria = auditar_tabla(df)
    auditoria.checklist     # los cinco puntos con su resultado y estado
    auditoria.tipos         # tipo leido vs tipo que deberia tener cada columna
    auditoria.imposibles    # cada valor imposible, con su fila y motivo
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from . import eda as ED
from .analyzer import detectar_atipicos_logicos
from .correccion_guiada import columnas_de_formula, detectar_regla_formula, filas_formula_incorrecta, texto_formula
from .limpieza_guiada import (ROLES, TOKENS_NULOS_BASE, a_numero, columnas_fecha_por_nombre, es_nulo,
                              rol_columna)

OK, REVISAR = "ok", "revisar"
MAX_EJEMPLOS = 50  # filas de ejemplo de duplicados e imposibles que se muestran


@dataclass
class AuditoriaPrevia:
    filas: int
    columnas: int
    tipos: pd.DataFrame
    nulos: pd.DataFrame
    duplicados: int
    ejemplos_duplicados: pd.DataFrame
    imposibles: pd.DataFrame
    describe: pd.DataFrame
    checklist: List[Dict] = field(default_factory=list)
    formula: Optional[Dict] = None  # {total, cantidad, precio, ...} si se encontro «total = cantidad x precio»
    formulas: pd.DataFrame = field(default_factory=pd.DataFrame)  # filas donde el total no coincide


def _tipos(df: pd.DataFrame, tokens) -> pd.DataFrame:
    """Tipo que Python leyo vs tipo que la columna deberia tener (por su nombre y contenido)."""
    cols_fecha = columnas_fecha_por_nombre(df)
    filas = []
    for col in df.columns:
        rol = rol_columna(df, col, cols_fecha, tokens)
        leido_como_texto = not (pd.api.types.is_numeric_dtype(df[col]) or pd.api.types.is_bool_dtype(df[col])
                                or pd.api.types.is_datetime64_any_dtype(df[col]))
        deberia = ROLES[rol] if rol in ("numerica", "fecha") else ""
        filas.append({
            "columna": col, "tipo_leido": str(df[col].dtype),
            "tipo_esperado": ROLES[rol],
            "revisar": "⚠ leída como texto" if leido_como_texto and deberia else "",
        })
    return pd.DataFrame(filas)


def _nulos(df: pd.DataFrame, tokens) -> pd.DataFrame:
    filas = [{"columna": col, "nulos": int(es_nulo(df[col], tokens).sum())} for col in df.columns]
    tabla = pd.DataFrame(filas)
    tabla["porcentaje"] = (tabla["nulos"] / max(len(df), 1) * 100).round(1)
    return tabla[tabla["nulos"] > 0].sort_values("nulos", ascending=False, ignore_index=True)


def _imposibles(df: pd.DataFrame) -> pd.DataFrame:
    """Valores fisicamente imposibles (edad fuera de 0-120, conteos o precios negativos...), con la logica
    de la limpieza clasica. «fila» cuenta desde 1, como en una hoja de calculo sin encabezado."""
    hallazgos = detectar_atipicos_logicos(df)
    return pd.DataFrame(
        [{"columna": h.columna, "fila": h.fila + 1, "valor": h.valor_original, "motivo": h.detalle}
         for h in hallazgos], columns=["columna", "fila", "valor", "motivo"])


def _formulas(df: pd.DataFrame):
    """(regla, filas) de «total = cantidad x precio» con la deteccion de la limpieza clasica, o (None, vacio).
    Las columnas se leen con a_numero, asi «₡7,650» cuenta como numero."""
    sin_formula = (None, pd.DataFrame(columns=["fila", "total", "esperado", "formula"]))
    candidatas = columnas_de_formula(df)  # solo las columnas que por su nombre pueden entrar en la formula
    if not candidatas:
        return sin_formula
    numerico = df[candidatas].apply(a_numero)
    regla = detectar_regla_formula(numerico)
    return sin_formula if regla is None else (regla, filas_formula_incorrecta(numerico, regla))


def auditar_tabla(df: pd.DataFrame, tokens=TOKENS_NULOS_BASE) -> AuditoriaPrevia:
    """Los cinco puntos del checklist para `df` (tal como se leyo, sin limpiar)."""
    if df is None or df.empty:
        raise ValueError("La tabla está vacía: no hay nada que auditar.")
    tipos, nulos, imposibles = _tipos(df, tokens), _nulos(df, tokens), _imposibles(df)
    regla_formula, formulas = _formulas(df)
    duplicadas = df.duplicated(keep="first")
    n_duplicados = int(duplicadas.sum())
    numerica, _ = ED.tabla_numerica(df, tokens)
    describe = ED.resumen_numerico(numerica)

    como_texto = tipos[tipos["revisar"] != ""]["columna"].tolist()
    checklist = [
        {"n": 1, "punto": "Inspección de tamaño", "pregunta": "¿Cuántas filas y columnas reales hay?",
         "resultado": f"{len(df):,} filas × {len(df.columns)} columnas", "estado": OK},
        {"n": 2, "punto": "Mapeo de tipos de datos",
         "pregunta": "¿Los números y las fechas están como tales, o Python leyó todo como texto?",
         "resultado": (f"{len(como_texto)} columnas que deberían ser número o fecha se leyeron como texto: "
                       + ", ".join(como_texto[:6]) + (" …" if len(como_texto) > 6 else "")) if como_texto
         else "Los tipos leídos coinciden con lo esperado",
         "estado": REVISAR if como_texto else OK},
        {"n": 3, "punto": "Detección de nulos", "pregunta": "¿Cuántos valores faltan por columna?",
         "resultado": (f"{int(nulos['nulos'].sum()):,} celdas nulas en {len(nulos)} columnas"
                       if len(nulos) else "Sin valores nulos"),
         "estado": REVISAR if len(nulos) else OK},
        {"n": 4, "punto": "Detección de duplicados", "pregunta": "¿Hay transacciones clonadas por error del sistema?",
         "resultado": f"{n_duplicados:,} filas duplicadas" if n_duplicados else "Sin filas duplicadas",
         "estado": REVISAR if n_duplicados else OK},
        {"n": 5, "punto": "Análisis de lógica de negocio",
         "pregunta": "¿Existen valores físicamente imposibles (precios negativos, edades de 150 años) o totales "
                     "que no cuadran con cantidad × precio?",
         "resultado": "; ".join(
             ([f"{len(imposibles)} valores imposibles en {imposibles['columna'].nunique()} columnas"]
              if len(imposibles) else ["No se encontraron valores imposibles"])
             + ([f"{len(formulas)} totales no coinciden con {texto_formula(regla_formula)}"]
                if len(formulas) else ([f"{regla_formula['total']} cuadra con {texto_formula(regla_formula)}"]
                                       if regla_formula else []))),
         "estado": REVISAR if (len(imposibles) or len(formulas)) else OK},
    ]
    return AuditoriaPrevia(
        filas=len(df), columnas=len(df.columns), tipos=tipos, nulos=nulos, duplicados=n_duplicados,
        ejemplos_duplicados=df[duplicadas].head(MAX_EJEMPLOS), imposibles=imposibles.head(MAX_EJEMPLOS * 4),
        describe=describe, checklist=checklist, formula=regla_formula, formulas=formulas.head(MAX_EJEMPLOS * 4))


def checklist_a_texto(auditoria: AuditoriaPrevia) -> str:
    """Los cinco puntos en texto plano (para la consola)."""
    marca = {OK: "✅", REVISAR: "⚠️ "}
    return "\n".join(f"  {marca[c['estado']]} {c['n']}. {c['punto']}: {c['resultado']}" for c in auditoria.checklist)

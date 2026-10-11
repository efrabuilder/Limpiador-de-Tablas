# -*- coding: utf-8 -*-
"""
correccion_guiada.py
====================
Limites de valores imposibles y atipicos para la limpieza guiada.

La limpieza clasica ya detecta valores fisicamente imposibles (una edad de 150, un precio o una cantidad
negativa) y atipicos estadisticos (regla del IQR). Este modulo reutiliza esa misma logica del analizador
pero, en lugar de marcar celda por celda, entrega una REGLA por columna: «[minimo, maximo]». Una regla es
lo que se puede escribir en el script de pandas, asi la correccion queda programada y se repite en cada
actualizacion de los datos (si se parchara a mano en Power BI, el error seguiria en la base original).

    limites_imposibles(df)            -> {columna: (minimo, maximo)} segun el tipo de dato conocido
    limites_iqr(df, columnas, 1.5)    -> {columna: (limite_inferior, limite_superior)}
    combinar_limites(a, b)            -> la interseccion de dos conjuntos de reglas
    contar_fuera(df, limites)         -> {columna: cuantos valores caen fuera}
    detectar_regla_formula(df)        -> la columna «total» y las que la calculan (cantidad x precio...)
    filas_formula_incorrecta(df, r)   -> las filas donde el total no coincide con lo que dicen las otras

No modifica la tabla; quien la corrige es limpieza_guiada.paso_atipicos.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from .analyzer import (
    _PATRONES_CANTIDAD,
    _PATRONES_DESCUENTO,
    _PATRONES_ENVIO,
    _PATRONES_IMPUESTO,
    _PATRONES_PRECIO,
    _PATRONES_TOTAL,
    _a_numero_tolerante,
    _columnas_numericas_o_potenciales,
    _columnas_por_patron,
    _es_columna_no_negativa,
    _mejor_combinacion_formula,
    _rango_plausible_fijo,
    _techo_probable_conteo_acotado,
)

TOLERANCIA_FORMULA = 0.01   # relativa, con piso absoluto de 0.01 (la misma de la limpieza clasica)
TASA_MINIMA_FORMULA = 0.5   # si la mejor combinacion de columnas no cuadra ni en la mitad de las filas, no se usa

Limites = Dict[str, Tuple[Optional[float], Optional[float]]]


def limites_imposibles(df: pd.DataFrame, columnas: Optional[Sequence[str]] = None) -> Limites:
    """{columna: (minimo, maximo)} de lo fisicamente posible, segun lo que el nombre de la columna dice:
    una edad va de 0 a 120; un conteo o cantidad (quejas, unidades, horas, precio...) no es negativo y, si el
    resto de los datos sugiere una escala acotada (1-5, 1-10), tampoco pasa de su techo. `None` = sin limite de
    ese lado. Solo aparecen las columnas a las que aplica una de esas reglas."""
    limites: Limites = {}
    for col in (list(columnas) if columnas else _columnas_numericas_o_potenciales(df)):
        if col not in df.columns:
            continue
        serie = _a_numero_tolerante(df[col])
        if serie.notna().sum() == 0:
            continue
        rango = _rango_plausible_fijo(col)
        if rango is not None:
            limites[col] = (rango[0], rango[1])
        elif _es_columna_no_negativa(col):
            limites[col] = (0, _techo_probable_conteo_acotado(serie))
    return limites


def limites_iqr(df: pd.DataFrame, columnas: Sequence[str], factor: float = 1.5) -> Limites:
    """{columna: (Q1 - factor*IQR, Q3 + factor*IQR)}: el bigote de un boxplot. Se salta las columnas sin
    variacion (IQR = 0) o sin numeros. Los limites se calculan con los datos de este momento y quedan fijos
    en el script."""
    limites: Limites = {}
    for col in columnas:
        if col not in df.columns:
            continue
        serie = _a_numero_tolerante(df[col])
        q1, q3 = serie.quantile(0.25), serie.quantile(0.75)
        iqr = q3 - q1
        if pd.isna(iqr) or iqr == 0:
            continue
        limites[col] = (round(float(q1 - factor * iqr), 6), round(float(q3 + factor * iqr), 6))
    return limites


def combinar_limites(a: Limites, b: Limites) -> Limites:
    """Une dos conjuntos de reglas. Si una columna esta en los dos, vale la mas estricta de cada lado."""
    salida: Limites = dict(a)
    for col, (minimo_b, maximo_b) in b.items():
        if col not in salida:
            salida[col] = (minimo_b, maximo_b)
            continue
        minimo_a, maximo_a = salida[col]
        minimos = [m for m in (minimo_a, minimo_b) if m is not None]
        maximos = [m for m in (maximo_a, maximo_b) if m is not None]
        salida[col] = (max(minimos) if minimos else None, min(maximos) if maximos else None)
    return salida


def contar_fuera(df: pd.DataFrame, limites: Limites) -> Dict[str, int]:
    """{columna: cuantos valores caen fuera de su [minimo, maximo]} (solo las que tienen alguno)."""
    conteos: Dict[str, int] = {}
    for col, (minimo, maximo) in limites.items():
        if col not in df.columns:
            continue
        serie = _a_numero_tolerante(df[col])
        fuera = pd.Series(False, index=serie.index)
        if minimo is not None:
            fuera |= serie < minimo
        if maximo is not None:
            fuera |= serie > maximo
        if int(fuera.sum()):
            conteos[col] = int(fuera.sum())
    return conteos


def texto_limite(minimo: Optional[float], maximo: Optional[float]) -> str:
    """'[0, 120]', '[0, sin tope]'... para mostrar una regla."""
    def _n(v: Optional[float], vacio: str) -> str:
        return vacio if v is None else f"{v:g}"
    return f"[{_n(minimo, 'sin piso')}, {_n(maximo, 'sin tope')}]"


# =============================================================================
# Formula: total = cantidad x precio (- descuento + impuesto + envio)
# =============================================================================

def columnas_de_formula(df: pd.DataFrame) -> List[str]:
    """Las columnas cuyo nombre sugiere que intervienen en «total = cantidad x precio» (total, cantidad, precio,
    descuento, impuesto, envio). Sirve para convertir a numero SOLO esas columnas antes de buscar la regla, en
    lugar de leer toda la tabla."""
    patrones = (_PATRONES_TOTAL, _PATRONES_CANTIDAD, _PATRONES_PRECIO, _PATRONES_DESCUENTO, _PATRONES_IMPUESTO,
                _PATRONES_ENVIO)
    nombres = {c for patron in patrones for c in _columnas_por_patron(df, patron)}
    return [c for c in df.columns if c in nombres and not pd.api.types.is_bool_dtype(df[c])]


def detectar_regla_formula(df: pd.DataFrame, tolerancia: float = TOLERANCIA_FORMULA) -> Optional[Dict]:
    """Busca por nombre la columna «total» y las que la calculan (cantidad, precio y, si ayudan, descuento, impuesto
    y envio) y se queda con la combinacion que de verdad cuadra en los datos: la misma deteccion de la limpieza
    clasica. Devuelve {'total', 'cantidad', 'precio', 'descuento', 'impuesto', 'envio', 'tolerancia'} o None si no
    hay candidatas o ni la mejor combinacion cuadra en la mitad de las filas (lo mas probable es que sean las
    columnas equivocadas). Las columnas de `df` deben ser numeros; con texto como «₡7,650» pasarlas antes por
    a_numero."""
    cand_total = _columnas_por_patron(df, _PATRONES_TOTAL)
    cand_cant = _columnas_por_patron(df, _PATRONES_CANTIDAD)
    cand_precio = _columnas_por_patron(df, _PATRONES_PRECIO)
    if not (cand_total and cand_cant and cand_precio):
        return None
    usadas = set(cand_total) | set(cand_cant) | set(cand_precio)
    ajustes = [[c for c in _columnas_por_patron(df, patron) if c not in usadas]
               for patron in (_PATRONES_DESCUENTO, _PATRONES_IMPUESTO, _PATRONES_ENVIO)]
    total, cantidad, precio, descuento, impuesto, envio, tasa = _mejor_combinacion_formula(
        df, cand_total, cand_cant, cand_precio, *ajustes, tolerancia)
    if tasa < TASA_MINIMA_FORMULA:
        return None
    return {"total": total, "cantidad": cantidad, "precio": precio, "descuento": descuento,
            "impuesto": impuesto, "envio": envio, "tolerancia": tolerancia}


def texto_formula(regla: Dict) -> str:
    """'cantidad × precio − descuento + impuesto + envio' para mostrar una regla."""
    texto = f"{regla['cantidad']} × {regla['precio']}"
    for clave, signo in (("descuento", "−"), ("impuesto", "+"), ("envio", "+")):
        if regla.get(clave):
            texto += f" {signo} {regla[clave]}"
    return texto


def filas_formula_incorrecta(df: pd.DataFrame, regla: Dict) -> pd.DataFrame:
    """Las filas donde el total no coincide con la formula de `regla` (con su tolerancia relativa y el piso de
    0.01): columnas fila (desde 1), total, esperado. Vacia si no hay ninguna."""
    columnas = ["fila", "total", "esperado", "formula"]
    if not all(regla.get(k) in df.columns for k in ("total", "cantidad", "precio")):
        return pd.DataFrame(columns=columnas)
    num = lambda c: _a_numero_tolerante(df[c])  # noqa: E731 - lectura corta de una columna
    esperado = num(regla["cantidad"]) * num(regla["precio"])
    if regla.get("descuento") in df.columns:
        esperado = esperado - num(regla["descuento"]).fillna(0)
    for clave in ("impuesto", "envio"):
        if regla.get(clave) in df.columns:
            esperado = esperado + num(regla[clave]).fillna(0)
    total = num(regla["total"])
    limite = (esperado.abs() * regla.get("tolerancia", TOLERANCIA_FORMULA)).clip(lower=0.01)
    mal = ((total - esperado).abs() > limite) & total.notna() & esperado.notna()
    return pd.DataFrame({"fila": [int(i) + 1 for i in df.index[mal]], "total": total[mal].tolist(),
                         "esperado": esperado[mal].round(2).tolist(), "formula": texto_formula(regla)},
                        columns=columnas)

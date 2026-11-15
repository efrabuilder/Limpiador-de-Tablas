# -*- coding: utf-8 -*-
"""
relaciones.py
=============
Detecta, mirando los DATOS, cuando una columna numerica sale de otras dos de la misma tabla:

    resta       A = B - C        (ganancia = ingreso - costo)
    producto    A = B x C        (total = precio unitario x cantidad)
    cociente    A = B / C        (margen = ganancia / ingreso; precio unitario = total / cantidad)

Sirve para describir bien cada campo en el diccionario («Ganancia» es un monto, «Margen» una
proporcion, «PrecioTotal» el total de la venta) sin adivinar por el nombre. No depende del dominio:
funciona con cualquier tabla que tenga columnas calculadas.
"""
from __future__ import annotations

from itertools import combinations, permutations
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .limpieza_guiada import a_numero

MAX_COLUMNAS = 25      # con mas columnas numericas solo se revisan las primeras (el costo crece rapido)
MIN_FILAS = 8          # con menos filas una coincidencia puede ser casualidad
MIN_ACIERTO = 0.9      # fraccion de filas donde la relacion se cumple
TOLERANCIA_REL = 0.005  # 0,5 % de error relativo (los datos suelen venir redondeados)
TOLERANCIA_ABS = 0.011  # o hasta un centavo de redondeo

# Palabras del nombre que desempatan entre «A = B x C» y «B = A / C» (las dos son ciertas a la vez).
_NOMBRES_RESULTADO_TOTAL = {"total", "importe", "monto", "ingreso", "ingresos", "ventas", "venta", "revenue",
                            "subtotal", "bruto", "neto", "sum", "suma"}
_NOMBRES_DIFERENCIA = {"ganancia", "utilidad", "profit", "margen", "diferencia", "diff", "neto", "saldo", "balance",
                       "resultado", "beneficio", "ahorro", "variacion", "delta", "gain"}
_NOMBRES_UNITARIO = {"unit", "unitario", "unitaria", "unidad", "promedio", "avg", "medio", "por", "per", "tasa",
                     "ratio", "margen", "pct", "porcentaje", "proporcion", "rate", "indice"}


def _tokens(nombre) -> set:
    import re
    texto = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(nombre)).lower()
    return set(re.findall(r"[a-záéíóúñ0-9]+", texto))


def _numericas(df: pd.DataFrame) -> Dict[str, np.ndarray]:
    """Columnas numericas utiles: con variacion, sin ser un identificador consecutivo ni una marca Si/No."""
    resultado: Dict[str, np.ndarray] = {}
    for col in df.columns:
        serie = df[col]
        if pd.api.types.is_bool_dtype(serie) or pd.api.types.is_datetime64_any_dtype(serie):
            continue
        numeros = serie if pd.api.types.is_numeric_dtype(serie) else a_numero(serie)
        validos = numeros.dropna()
        if len(validos) < MIN_FILAS or validos.nunique() < 3:
            continue
        if len(validos) > 0 and validos.nunique() == len(validos) and (validos.diff().dropna() == 1).all():
            continue  # 1, 2, 3...: un numero de fila, no una medida
        resultado[str(col)] = numeros.astype(float).to_numpy()
        if len(resultado) >= MAX_COLUMNAS:
            break
    return resultado


def _cumple(real: np.ndarray, esperado: np.ndarray) -> float:
    ok = np.isfinite(real) & np.isfinite(esperado)
    if ok.sum() < MIN_FILAS:
        return 0.0
    cerca = np.isclose(real[ok], esperado[ok], rtol=TOLERANCIA_REL, atol=TOLERANCIA_ABS)
    return float(cerca.mean())


def detectar_relaciones(df: pd.DataFrame) -> Dict[str, Dict]:
    """{columna: {"op": "resta"|"producto"|"cociente", "a": col, "b": col, "acierto": 0-1}} para las
    columnas que son el resultado de una operacion entre otras dos. Cada columna aparece una sola vez
    (se queda con la relacion mas simple y de mayor acierto)."""
    datos = _numericas(df)
    nombres = list(datos)
    posicion = {str(c): i for i, c in enumerate(df.columns)}
    if len(nombres) < 3:
        return {}
    candidatas: List[Dict] = []
    with np.errstate(all="ignore"):
        for objetivo in nombres:
            real = datos[objetivo]
            otras = [n for n in nombres if n != objetivo]
            for b, c in permutations(otras, 2):  # resta y cociente: el orden importa
                dif = _cumple(real, datos[b] - datos[c])
                if dif >= MIN_ACIERTO and not np.allclose(datos[b], datos[c], equal_nan=True):
                    candidatas.append({"col": objetivo, "op": "resta", "a": b, "b": c, "acierto": dif, "orden": 0})
                cociente = np.where(datos[c] != 0, datos[b] / datos[c], np.nan)
                coc = _cumple(real, cociente)
                if coc >= MIN_ACIERTO:
                    candidatas.append({"col": objetivo, "op": "cociente", "a": b, "b": c, "acierto": coc, "orden": 2})
            for b, c in combinations(otras, 2):
                prod = _cumple(real, datos[b] * datos[c])
                if prod >= MIN_ACIERTO:
                    candidatas.append({"col": objetivo, "op": "producto", "a": b, "b": c, "acierto": prod, "orden": 1})
    if not candidatas:
        return {}

    # «Total = unitario x cantidad» y «unitario = total / cantidad» son la misma ecuacion: el nombre decide
    # cual de las columnas es el resultado. Primero se arma la lista de ecuaciones distintas.
    ecuaciones: Dict[frozenset, List[Dict]] = {}
    for cand in candidatas:
        # Una ecuacion se identifica por las tres columnas que participan.
        ecuaciones.setdefault(frozenset((cand["col"], cand["a"], cand["b"])), []).append(cand)

    elegidas: Dict[str, Dict] = {}
    # Primero las ecuaciones con resta: son las que no se pueden confundir con otra (la ganancia es
    # ingreso - costo aunque el margen, que sale de ella, tambien la reproduzca como producto).
    orden = sorted(ecuaciones.items(), key=lambda kv: 0 if any(c["op"] == "resta" for c in kv[1]) else 1)
    for trio, lista in orden:
        restas = [c for c in lista if c["op"] == "resta"]
        if restas:  # la resta no tiene ambiguedad con las otras: A = B - C (se prefiere el orden con B > C)
            # Varias restas describen la misma ecuacion (G = P - C, C = P - G): el resultado es la columna con
            # nombre de diferencia o, si no hay pista, la que esta mas a la derecha (las calculadas van al final).
            mejor = max(restas, key=lambda c: (c["acierto"] > 0.99, len(_tokens(c["col"]) & _NOMBRES_DIFERENCIA),
                                               posicion.get(c["col"], 0),
                                               float(np.nanmean(datos[c["a"]]) > np.nanmean(datos[c["b"]]))))
            elegidas.setdefault(mejor["col"], mejor)
            continue
        productos = [c for c in lista if c["op"] == "producto"]
        cocientes = [c for c in lista if c["op"] == "cociente"]
        # Entre las tres columnas, el resultado del producto es el que tiene nombre de total, o el mayor.
        if productos:
            def puntaje_total(c):
                t = _tokens(c["col"])
                return (len(t & _NOMBRES_RESULTADO_TOTAL) - len(t & _NOMBRES_UNITARIO),
                        posicion.get(c["col"], 0),  # sin pista en el nombre, la calculada es la ultima
                        float(np.nanmean(np.abs(datos[c["col"]]))))
            mejor_prod = max(productos, key=puntaje_total)
            elegidas.setdefault(mejor_prod["col"], mejor_prod)
            # Las otras dos columnas son cocientes de ese producto: el que tenga nombre de unitario/tasa
            # se describe como cociente (y el otro factor queda como dato base).
            for cand in cocientes:
                if cand["col"] != mejor_prod["col"] and cand["b"] in (mejor_prod["a"], mejor_prod["b"]) \
                        and cand["a"] == mejor_prod["col"]:
                    tk = _tokens(cand["col"])
                    if tk & _NOMBRES_UNITARIO or float(np.nanmax(np.abs(datos[cand["col"]]))) <= 1.0:
                        elegidas.setdefault(cand["col"], cand)
        elif cocientes:
            mejor_coc = max(cocientes, key=lambda c: (c["acierto"], len(_tokens(c["col"]) & _NOMBRES_UNITARIO)))
            elegidas.setdefault(mejor_coc["col"], mejor_coc)

    # Un cociente cuyo numerador es un producto/resta ya detectado y cuyo denominador es otra columna
    # (margen = ganancia / total) tambien cuenta aunque la ecuacion tenga tres columnas distintas.
    return {col: {k: v for k, v in rel.items() if k != "orden" and k != "col"} for col, rel in elegidas.items()}


def _fraccion(df: pd.DataFrame, col: str) -> bool:
    """True si los valores de la columna estan entre 0 y 1 (proporcion, no porcentaje de 0 a 100)."""
    numeros = pd.to_numeric(df[col], errors="coerce") if pd.api.types.is_numeric_dtype(df[col]) else a_numero(df[col])
    numeros = numeros.dropna()
    return bool(len(numeros) and numeros.min() >= -1.0 and numeros.max() <= 1.0)


def describir_relacion(col: str, rel: Dict, df: Optional[pd.DataFrame] = None) -> str:
    """Frase para el diccionario: que es la columna y de cuales se calcula."""
    a, b, op = rel["a"], rel["b"], rel["op"]
    if op == "resta":
        return (f"Monto calculado: «{a}» menos «{b}» en cada fila ({a} − {b}). Es un monto en la misma unidad "
                f"que esas columnas, no un porcentaje.")
    if op == "producto":
        return (f"Total de la fila, calculado como «{a}» por «{b}» ({a} × {b}). Es el valor acumulado de la fila, "
                f"no un valor unitario.")
    # cociente
    es_proporcion = df is not None and col in df.columns and _fraccion(df, col)
    if es_proporcion:
        return (f"Proporción calculada como «{a}» entre «{b}» ({a} ÷ {b}). Sus valores van de 0 a 1: se leen como "
                f"fracción (0,28 equivale a 28 %), no como monto.")
    return f"Valor calculado como «{a}» entre «{b}» ({a} ÷ {b}); queda expresado por cada unidad de «{b}»."

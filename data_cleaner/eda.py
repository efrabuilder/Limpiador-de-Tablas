# -*- coding: utf-8 -*-
"""
eda.py
======
EDA estadistico de una tabla (la base cientifica antes de abrir Power BI):

    1. Estadisticos descriptivos       (el equivalente de df.describe())
    2. Matriz de correlacion           (que variables numericas se mueven juntas)
    3. Valores atipicos con diagramas de caja (boxplots, regla del IQR)

Sin ninguna dependencia de Streamlit, para que lo use cualquier interfaz (web, CLI, API).
La correccion de un atipico que sea un error (una venta de un millon por un cero de mas) NO se hace
aqui ni en Power BI: se programa en el script de limpieza para que sea reproducible. Por eso este
modulo solo MIDE y muestra; no modifica la tabla.

    resultado = analizar_eda(df)
    resultado.numerico        # describe() en espanol, mas nulos y asimetria
    resultado.correlacion     # matriz de correlacion (o None si hay menos de 2 columnas)
    resultado.atipicos        # limites del IQR y cuantos valores caen fuera, por columna
    figura_boxplots(resultado.tabla_numerica)
    figura_correlacion(resultado.correlacion)
    guardar_eda(resultado, "salida", "ventas")   # CSV + PNG + script de pandas/seaborn
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .limpieza_guiada import TOKENS_NULOS_BASE, a_numero, es_nulo
from .patrones import coincide_patron, es_columna_coordenada, es_columna_id

METODOS_CORRELACION = ("pearson", "spearman", "kendall")
UMBRAL_CORRELACION = 0.8   # |r| a partir del cual se avisa de variables muy parecidas
FACTOR_IQR = 1.5           # el bigote del boxplot: Q1 - 1.5*IQR y Q3 + 1.5*IQR
MAX_BOXPLOTS = 12          # columnas por figura (mas de eso no se lee)
PORCENTAJE_NUMERICA = 0.9  # una columna de texto cuenta como numero si el 90% se lee como tal
# Mes y anio son codigos de calendario (el 12 no es «mas» que el 1): un promedio o un atipico de ellos no dice nada.
PATRONES_PERIODO = ("mes", "anio", "ano", "year", "month")

LECTURA_EDA = (
    "Qué hacer con lo que encuentre aquí: si un valor atípico es un error de captura (una venta de un "
    "millón por un cero de más), corrija la regla en la limpieza para que quede en el script y se repita "
    "en cada actualización; no lo parche en Power BI, porque el error seguiría en la base original. Si es "
    "un dato real pero extremo, déjelo y documéntelo."
)


# =============================================================================
# Columnas numericas
# =============================================================================

def tabla_numerica(df: pd.DataFrame, tokens=TOKENS_NULOS_BASE) -> Tuple[pd.DataFrame, Dict[str, str]]:
    """(tabla, excluidas): las columnas de `df` que se pueden analizar como numeros, ya convertidas a
    float, y {columna: motivo} de las que se dejaron fuera.

    Cuenta como numerica una columna numerica de verdad o una de texto donde al menos el 90% de los
    datos se lee como numero (entiende «1.234,56» y simbolos de moneda). Se dejan fuera los
    identificadores, las coordenadas y las columnas de mes o anio (un promedio de ids no dice
    nada), los booleanos, las columnas vacias y las constantes."""
    columnas, excluidas = {}, {}
    for col in df.columns:
        serie = df[col]
        if es_columna_id(col):
            excluidas[col] = "identificador"
            continue
        if es_columna_coordenada(col):
            excluidas[col] = "coordenada"
            continue
        if coincide_patron(col, PATRONES_PERIODO):
            excluidas[col] = "mes o año (código de calendario)"
            continue
        if pd.api.types.is_bool_dtype(serie) or pd.api.types.is_datetime64_any_dtype(serie):
            excluidas[col] = "no numérica"
            continue
        if pd.api.types.is_numeric_dtype(serie):
            numeros = pd.to_numeric(serie, errors="coerce").astype("float64")
        else:
            con_dato = ~es_nulo(serie, tokens)
            if not con_dato.any():
                excluidas[col] = "vacía"
                continue
            convertida = a_numero(serie.where(con_dato))
            if convertida.notna().sum() / con_dato.sum() < PORCENTAJE_NUMERICA:
                excluidas[col] = "no numérica"
                continue
            numeros = convertida.astype("float64")
        numeros = numeros.replace([np.inf, -np.inf], np.nan)
        if numeros.notna().sum() == 0:
            excluidas[col] = "vacía"
        elif numeros.nunique(dropna=True) < 2:
            excluidas[col] = "constante (un solo valor)"
        else:
            columnas[col] = numeros
    return pd.DataFrame(columnas, index=df.index), excluidas


# =============================================================================
# Estadisticos
# =============================================================================

def resumen_numerico(num: pd.DataFrame) -> pd.DataFrame:
    """Lo mismo que num.describe().T, con nombres en espanol, mas nulos, % de nulos y asimetria
    (skew: cerca de 0 es simetrica; muy positiva, cola larga a la derecha, tipica de ventas)."""
    if num.empty:
        return pd.DataFrame()
    d = num.describe().T
    salida = pd.DataFrame({
        "n": d["count"].astype(int), "media": d["mean"], "desv_est": d["std"], "minimo": d["min"],
        "p25": d["25%"], "mediana": d["50%"], "p75": d["75%"], "maximo": d["max"],
    })
    salida["nulos"] = len(num) - salida["n"]
    salida["pct_nulos"] = (salida["nulos"] / max(len(num), 1) * 100).round(1)
    salida["asimetria"] = num.skew()
    salida.index.name = "columna"
    return salida.round(4)


def resumen_categorico(df: pd.DataFrame, excluir: Sequence[str] = (), tokens=TOKENS_NULOS_BASE) -> pd.DataFrame:
    """describe() de las columnas que no son numericas: cuantos datos, cuantos valores distintos y el
    mas frecuente. Los textos vacios o tipo «nan» cuentan como nulos."""
    filas = []
    for col in df.columns:
        if col in excluir or pd.api.types.is_numeric_dtype(df[col]):
            continue
        valida = df[col].where(~es_nulo(df[col], tokens))
        con_dato = valida.dropna()
        frecuentes = con_dato.astype(str).value_counts()
        filas.append({
            "columna": col, "n": len(con_dato), "nulos": len(df) - len(con_dato),
            "unicos": int(con_dato.nunique()),
            "mas_frecuente": frecuentes.index[0] if len(frecuentes) else "",
            "frecuencia": int(frecuentes.iloc[0]) if len(frecuentes) else 0,
        })
    return pd.DataFrame(filas, columns=["columna", "n", "nulos", "unicos", "mas_frecuente", "frecuencia"])


def atipicos_iqr(num: pd.DataFrame, factor: float = FACTOR_IQR) -> pd.DataFrame:
    """Valores atipicos por columna con la regla del boxplot: fuera de [Q1 - factor*IQR, Q3 + factor*IQR].
    Una fila por columna con los limites, cuantos valores caen fuera, su porcentaje y el valor mas
    extremo de cada lado. Ordenada de mas a menos atipicos."""
    filas = []
    for col in num.columns:
        v = num[col].dropna()
        q1, q3 = v.quantile(0.25), v.quantile(0.75)
        iqr = q3 - q1
        bajo, alto = q1 - factor * iqr, q3 + factor * iqr
        fuera_bajo, fuera_alto = v[v < bajo], v[v > alto]
        filas.append({
            "columna": col, "q1": q1, "q3": q3, "limite_inferior": bajo, "limite_superior": alto,
            "n_atipicos": len(fuera_bajo) + len(fuera_alto),
            "pct_atipicos": round((len(fuera_bajo) + len(fuera_alto)) / max(len(v), 1) * 100, 2),
            "menor_atipico": fuera_bajo.min() if len(fuera_bajo) else np.nan,
            "mayor_atipico": fuera_alto.max() if len(fuera_alto) else np.nan,
        })
    salida = pd.DataFrame(filas, columns=[
        "columna", "q1", "q3", "limite_inferior", "limite_superior", "n_atipicos", "pct_atipicos",
        "menor_atipico", "mayor_atipico"])
    return salida.sort_values("n_atipicos", ascending=False, ignore_index=True).round(4)


def matriz_correlacion(num: pd.DataFrame, metodo: str = "pearson") -> Optional[pd.DataFrame]:
    """Matriz de correlacion de las columnas numericas (None si hay menos de dos). `metodo`: pearson
    (relacion lineal), spearman o kendall (por rangos, menos sensibles a los atipicos)."""
    if metodo not in METODOS_CORRELACION:
        raise ValueError(f"metodo de correlacion invalido: '{metodo}' (use {', '.join(METODOS_CORRELACION)})")
    if num.shape[1] < 2:
        return None
    corr = num.corr(method=metodo, min_periods=3)
    return corr.dropna(axis=0, how="all").dropna(axis=1, how="all").round(4)


def _fuerza(r: float) -> str:
    r = abs(r)
    return "muy fuerte" if r >= 0.9 else "fuerte" if r >= 0.8 else "moderada" if r >= 0.5 else "débil"


def pares_correlacionados(corr: Optional[pd.DataFrame], umbral: float = UMBRAL_CORRELACION) -> pd.DataFrame:
    """Pares de columnas con |correlacion| >= umbral, de la mas a la menos fuerte. Una correlacion muy
    alta puede ser una columna casi repetida (total, subtotal, cantidad x precio) o una relacion real;
    no prueba que una cause la otra."""
    columnas = ["columna_a", "columna_b", "correlacion", "fuerza", "sentido"]
    if corr is None or corr.empty:
        return pd.DataFrame(columns=columnas)
    filas = []
    nombres = list(corr.columns)
    for i, a in enumerate(nombres):
        for b in nombres[i + 1:]:
            r = corr.loc[a, b]
            if pd.notna(r) and abs(r) >= umbral:
                filas.append({"columna_a": a, "columna_b": b, "correlacion": r, "fuerza": _fuerza(r),
                              "sentido": "positiva" if r > 0 else "negativa"})
    salida = pd.DataFrame(filas, columns=columnas)
    if salida.empty:
        return salida
    return salida.reindex(salida["correlacion"].abs().sort_values(ascending=False).index).reset_index(drop=True)


# =============================================================================
# Resultado completo
# =============================================================================

@dataclass
class ResultadoEDA:
    filas: int
    tabla_numerica: pd.DataFrame
    numerico: pd.DataFrame
    categorico: pd.DataFrame
    atipicos: pd.DataFrame
    correlacion: Optional[pd.DataFrame]
    pares: pd.DataFrame
    excluidas: Dict[str, str] = field(default_factory=dict)
    avisos: List[str] = field(default_factory=list)
    metodo: str = "pearson"
    umbral: float = UMBRAL_CORRELACION
    factor_iqr: float = FACTOR_IQR


def analizar_eda(df: pd.DataFrame, tokens=TOKENS_NULOS_BASE, metodo: str = "pearson",
                 umbral: float = UMBRAL_CORRELACION, factor_iqr: float = FACTOR_IQR) -> ResultadoEDA:
    """EDA estadistico completo de `df`: descriptivos, atipicos (IQR) y correlaciones. No modifica la tabla.
    `tokens`: textos que cuentan como nulo (por defecto los de la limpieza guiada)."""
    if df is None or df.empty:
        raise ValueError("La tabla está vacía: no hay nada que explorar.")
    num, excluidas = tabla_numerica(df, tokens)
    avisos = []
    if num.empty:
        avisos.append("No hay columnas numéricas para analizar (los identificadores, coordenadas y columnas "
                      "constantes se dejan fuera).")
    elif num.shape[1] < 2:
        avisos.append("Con una sola columna numérica no hay matriz de correlación.")
    if len(df) < 8 and not num.empty:
        avisos.append(f"Solo hay {len(df)} filas: los atípicos y las correlaciones no son confiables.")
    corr = matriz_correlacion(num, metodo)
    return ResultadoEDA(
        filas=len(df), tabla_numerica=num, numerico=resumen_numerico(num),
        categorico=resumen_categorico(df, excluir=list(num.columns), tokens=tokens),
        atipicos=atipicos_iqr(num, factor_iqr) if not num.empty else pd.DataFrame(),
        correlacion=corr, pares=pares_correlacionados(corr, umbral), excluidas=excluidas,
        avisos=avisos, metodo=metodo, umbral=umbral, factor_iqr=factor_iqr)


# =============================================================================
# Graficos (matplotlib + seaborn). Se usa Figure directamente, sin pyplot, para que
# funcione igual en Streamlit, en la CLI y en un servidor sin pantalla.
# =============================================================================

def _seaborn():
    try:
        import seaborn as sns
        return sns
    except ImportError:  # sin seaborn se dibuja con matplotlib a secas
        return None


def figura_boxplots(num: pd.DataFrame, columnas: Optional[Sequence[str]] = None, por_fila: int = 3,
                    max_columnas: int = MAX_BOXPLOTS):
    """Un diagrama de caja por columna numerica (cada uno con su propia escala, porque ventas y edades no
    se pueden comparar en el mismo eje). Devuelve una Figure de matplotlib, o None si no hay columnas."""
    from matplotlib.figure import Figure

    elegidas = [c for c in (columnas if columnas is not None else num.columns) if c in num.columns]
    elegidas = elegidas[:max_columnas]
    if not elegidas:
        return None
    por_fila = max(1, min(por_fila, len(elegidas)))
    filas = -(-len(elegidas) // por_fila)
    fig = Figure(figsize=(4.2 * por_fila, 3.4 * filas), layout="constrained")
    ejes = np.atleast_1d(fig.subplots(filas, por_fila, squeeze=False)).ravel()
    sns = _seaborn()
    for eje, col in zip(ejes, elegidas):
        datos = num[col].dropna()
        if sns is not None:
            sns.boxplot(y=datos, ax=eje, color="#8ecae6", flierprops={"markerfacecolor": "#d62828",
                                                                     "markeredgecolor": "#d62828"})
        else:
            eje.boxplot(datos, flierprops={"markerfacecolor": "#d62828", "markeredgecolor": "#d62828"})
        eje.set_title(str(col), fontsize=10)
        eje.set_ylabel("")
    for eje in ejes[len(elegidas):]:
        eje.axis("off")
    return fig


def figura_correlacion(corr: Optional[pd.DataFrame]):
    """Mapa de calor de la matriz de correlacion (-1 a 1). Con mas de 12 columnas no se escriben los
    numeros dentro de las celdas. None si no hay matriz."""
    from matplotlib.figure import Figure

    if corr is None or corr.empty:
        return None
    n = len(corr)
    lado = min(max(4.5, 0.7 * n + 2), 14)
    fig = Figure(figsize=(lado + 1, lado), layout="constrained")
    eje = fig.subplots()
    sns = _seaborn()
    if sns is not None:
        sns.heatmap(corr, ax=eje, vmin=-1, vmax=1, center=0, cmap="coolwarm", annot=n <= 12, fmt=".2f",
                    square=True, linewidths=0.5)
    else:
        imagen = eje.imshow(corr.values, vmin=-1, vmax=1, cmap="coolwarm")
        fig.colorbar(imagen, ax=eje)
        eje.set_xticks(range(n), corr.columns, rotation=90)
        eje.set_yticks(range(n), corr.index)
    eje.set_title("Matriz de correlación")
    return fig


def figura_a_png(fig) -> bytes:
    """PNG (bytes) de una Figure, listo para descargar o guardar."""
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=150)
    return buffer.getvalue()


# =============================================================================
# Script reproducible y guardado
# =============================================================================

def generar_script_eda(nombre_archivo: str = "tabla_limpia.csv", metodo: str = "pearson",
                       umbral: float = UMBRAL_CORRELACION, factor_iqr: float = FACTOR_IQR) -> str:
    """Script de pandas + seaborn que repite el EDA estadistico sobre la tabla limpia (describe, matriz de
    correlacion y boxplots). Es independiente de este paquete: solo necesita pandas, matplotlib y seaborn."""
    es_excel = nombre_archivo.lower().endswith((".xlsx", ".xlsm", ".xls"))
    carga = ("df = pd.read_excel(ARCHIVO)" if es_excel else "df = pd.read_csv(ARCHIVO)")
    return f'''# -*- coding: utf-8 -*-
# Script generado por Limpiador de Tablas (EDA estadistico).
# Repite sobre la tabla limpia: describe(), matriz de correlacion y boxplots. Solo mide: si encuentra un
# error de captura, corrijalo en el script de LIMPIEZA (no en Power BI) para que sea reproducible.
import re

import matplotlib
matplotlib.use("Agg")  # guarda las figuras en archivos, sin abrir ventanas
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

ARCHIVO = {nombre_archivo!r}  # cambie por la ruta de su tabla limpia
METODO_CORRELACION = {metodo!r}  # pearson, spearman o kendall
UMBRAL_CORRELACION = {umbral!r}
FACTOR_IQR = {factor_iqr!r}

{carga}
print(f"Cargado: {{df.shape[0]}} filas x {{df.shape[1]}} columnas")

# Solo columnas numericas, sin identificadores, coordenadas ni mes/anio (un promedio de ids no dice nada)
PALABRAS_EXCLUIDAS = {{"id", "codigo", "folio", "clave", "identificador", "lat", "lon", "lng", "latitud", "longitud",
                      "mes", "anio", "año", "ano", "year", "month"}}  # ids, coordenadas y mes/anio
excluir = [c for c in df.columns if PALABRAS_EXCLUIDAS & set(re.split(r"[\\W_]+", str(c).lower()))]
num = df.select_dtypes("number").drop(columns=excluir, errors="ignore")
num = num.loc[:, num.nunique() > 1]  # sin columnas constantes

# ---------------- 1. Estadisticos descriptivos ----------------
print(num.describe().T)
num.describe().T.to_csv("eda_describe.csv", encoding="utf-8-sig")

# ---------------- 2. Matriz de correlacion ----------------
corr = num.corr(method=METODO_CORRELACION)
print(corr.round(2))
corr.to_csv("eda_correlacion.csv", encoding="utf-8-sig")
plt.figure(figsize=(max(5, 0.7 * len(corr) + 2),) * 2)
sns.heatmap(corr, vmin=-1, vmax=1, center=0, cmap="coolwarm", annot=len(corr) <= 12, fmt=".2f", square=True)
plt.title("Matriz de correlacion")
plt.tight_layout()
plt.savefig("eda_correlacion.png", dpi=150)
plt.close()

# ---------------- 3. Valores atipicos (boxplots + regla del IQR) ----------------
filas = []
for col in num.columns:
    q1, q3 = num[col].quantile(0.25), num[col].quantile(0.75)
    iqr = q3 - q1
    bajo, alto = q1 - FACTOR_IQR * iqr, q3 + FACTOR_IQR * iqr
    fuera = num[(num[col] < bajo) | (num[col] > alto)][col]
    filas.append({{"columna": col, "limite_inferior": bajo, "limite_superior": alto, "n_atipicos": len(fuera)}})
atipicos = pd.DataFrame(filas).sort_values("n_atipicos", ascending=False)
print(atipicos)
atipicos.to_csv("eda_atipicos.csv", index=False, encoding="utf-8-sig")

por_fila = min(3, max(len(num.columns), 1))
filas_fig = -(-max(len(num.columns), 1) // por_fila)
fig, ejes = plt.subplots(filas_fig, por_fila, figsize=(4.2 * por_fila, 3.4 * filas_fig), squeeze=False)
for eje, col in zip(ejes.ravel(), num.columns):
    sns.boxplot(y=num[col].dropna(), ax=eje, color="#8ecae6")
    eje.set_title(col)
    eje.set_ylabel("")
for eje in ejes.ravel()[len(num.columns):]:
    eje.axis("off")
fig.tight_layout()
fig.savefig("eda_boxplots.png", dpi=150)
print("Listo: eda_describe.csv, eda_correlacion.csv/.png, eda_atipicos.csv y eda_boxplots.png")
'''


def guardar_eda(resultado: ResultadoEDA, carpeta: str, base: str = "tabla",
                nombre_archivo: Optional[str] = None) -> Dict[str, str]:
    """Guarda en `carpeta` el EDA: CSV de descriptivos, atipicos y correlacion, los PNG de boxplots y de
    la matriz, y el script que lo repite (`nombre_archivo`: tabla limpia que leera el script; por
    defecto «<base>.csv»). Devuelve {tipo: ruta} solo de lo que se pudo generar."""
    os.makedirs(carpeta, exist_ok=True)
    rutas: Dict[str, str] = {}

    def escribir(tipo: str, nombre: str, contenido) -> None:
        ruta = os.path.join(carpeta, nombre)
        modo = "wb" if isinstance(contenido, bytes) else "w"
        with open(ruta, modo, **({} if modo == "wb" else {"encoding": "utf-8"})) as archivo:
            archivo.write(contenido)
        rutas[tipo] = ruta

    def csv(tabla: pd.DataFrame, indice: bool) -> bytes:
        return tabla.to_csv(index=indice).encode("utf-8-sig")

    if not resultado.numerico.empty:
        escribir("describe", f"eda_{base}_describe.csv", csv(resultado.numerico, True))
        escribir("atipicos", f"eda_{base}_atipicos.csv", csv(resultado.atipicos, False))
        escribir("boxplots", f"eda_{base}_boxplots.png", figura_a_png(figura_boxplots(resultado.tabla_numerica)))
    if len(resultado.categorico):
        escribir("categoricas", f"eda_{base}_categoricas.csv", csv(resultado.categorico, False))
    if resultado.correlacion is not None:
        escribir("correlacion", f"eda_{base}_correlacion.csv", csv(resultado.correlacion, True))
        escribir("correlacion_png", f"eda_{base}_correlacion.png",
                 figura_a_png(figura_correlacion(resultado.correlacion)))
    escribir("script_eda", f"eda_{base}_script.py",
             generar_script_eda(nombre_archivo or f"{base}.csv", resultado.metodo, resultado.umbral,
                                resultado.factor_iqr))
    return rutas

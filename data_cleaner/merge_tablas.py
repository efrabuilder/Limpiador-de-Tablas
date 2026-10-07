# -*- coding: utf-8 -*-
"""
merge_tablas.py
===============
Unir dos tablas (merge / JOIN) con los chequeos del cheat sheet de pandas y
del notebook LimpiezadeDatos.ipynb:

    - Las columnas llave: ¿se parecen?, ¿mismo tipo?, ¿cuantas se repiten?
    - Normalizar la llave en ambas tablas antes de cruzar ('10115.0',
      ' 10115 ' y '10115' pasan a ser la misma llave).
    - ¿Que tabla manda? (left join por defecto, el mas seguro).
    - Auditoria del resultado: cuantas filas cruzaron, cuantas se perdieron
      (inner) o se multiplicaron (llaves repetidas en la tabla B).
    - Plan B: rellenar una columna con otra cuando el cruce principal no
      encontro dato.

Las funciones normalizar_llave y colapsar_por_llave no dependen del resto
del paquete: generar_script_merge() las copia dentro del script, asi el
script hace lo mismo que la app.
"""
from __future__ import annotations

import inspect
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .limpieza_guiada import a_numero
from .patrones import normalizar_nombre

MODOS_LLAVE = {
    "texto": "Texto (minúsculas, sin espacios de más)",
    "codigo": "Código numérico (solo dígitos, quita el .0 de Excel)",
    "sin_cambios": "Sin cambios",
}

TIPOS_UNION = {
    "left": "Left — conserva TODAS las filas de A (recomendado)",
    "inner": "Inner — solo las filas que están en A y en B",
    "right": "Right — conserva TODAS las filas de B",
    "outer": "Outer — conserva todas las filas de ambas",
}

VALIDACIONES = {
    "": "Sin validar",
    "many_to_one": "Muchos a uno (cada llave de B aparece una sola vez)",
    "one_to_one": "Uno a uno (llaves únicas en ambas)",
    "one_to_many": "Uno a muchos (llaves únicas en A)",
    "many_to_many": "Muchos a muchos",
}

AGREGACIONES = ("first", "sum", "mean", "median", "max", "min", "count", "nunique")
_AGREGACIONES_NUMERICAS = ("sum", "mean", "median", "max", "min")


# =============================================================================
# Funciones autocontenidas (se copian al script generado)
# =============================================================================

def normalizar_llave(serie, modo="texto", ancho=0):
    """Deja la columna llave en una forma comparable.
    modo='texto': sin espacios a los lados ni repetidos, en minusculas.
    modo='codigo': quita el '.0' que mete Excel y deja solo digitos; con
    `ancho` rellena con ceros a la izquierda (ej. 1067 -> '01067').
    modo='sin_cambios': devuelve la columna igual.
    Los vacios quedan como nulos."""
    if modo == "sin_cambios":
        return serie
    s = serie.astype("string").str.strip()
    if modo == "codigo":
        s = s.str.replace(r"\.0+$", "", regex=True).str.replace(r"[^\d]", "", regex=True)
    else:
        s = s.str.lower().str.replace(r"\s+", " ", regex=True)
    s = s.mask(s.isin(["", "nan", "none", "null"]))  # vacios -> nulos
    if modo == "codigo" and ancho:
        s = s.str.zfill(int(ancho))
    return s.astype(object).where(s.notna(), np.nan)


def colapsar_por_llave(df, claves, agregaciones):
    """Deja UNA fila por llave. `agregaciones` es {columna: funcion} con
    funciones como 'first', 'sum', 'mean', 'max'... (las numericas leen la
    columna como numero antes de agregar). Las columnas que no esten en
    `agregaciones` se descartan."""
    df = df.copy()
    for columna, funcion in agregaciones.items():
        if funcion in ("sum", "mean", "median", "max", "min"):
            df[columna] = a_numero(df[columna])
    return df.groupby(list(claves), as_index=False, dropna=True, sort=False).agg(agregaciones)


_FUNCIONES_SCRIPT = (a_numero, normalizar_llave, colapsar_por_llave)


# =============================================================================
# Diagnostico de llaves
# =============================================================================

def semaforo_match(pct: float) -> str:
    """Icono segun el % de filas de A que encontraron su fila en B."""
    if pct >= 95:
        return "✅"
    if pct >= 80:
        return "🟢"
    if pct >= 50:
        return "🟡"
    return "🔴"


def _llaves_normalizadas(df: pd.DataFrame, claves: List[str], modo: str, ancho: int) -> pd.DataFrame:
    return pd.DataFrame({c: normalizar_llave(df[c], modo, ancho) for c in claves}, index=df.index)


def _pct_filas_con_match(ka: pd.DataFrame, kb: pd.DataFrame) -> float:
    """% de filas de A cuya llave (sin nulos) existe en B."""
    if len(ka) == 0:
        return 0.0
    unicas_b = kb.dropna().drop_duplicates()
    unicas_b.columns = list(ka.columns)
    marcado = ka.dropna().merge(unicas_b.assign(_en_b=1), how="left", on=list(ka.columns))
    return round(float(marcado["_en_b"].notna().sum()) / len(ka) * 100, 2)


def diagnosticar_llaves(df_a: pd.DataFrame, df_b: pd.DataFrame, claves_a: List[str],
                        claves_b: List[str], modo: str = "texto", ancho: int = 0) -> Dict:
    """Revisa las llaves antes de unir (la lista de chequeo del cheat sheet).
    Devuelve un diccionario con: nulos, repetidas y unicas de cada lado, si
    los nombres y tipos coinciden, % de filas de A que encuentran pareja en
    B (con y sin normalizar), la cardinalidad (1:1, N:1...), la validacion
    sugerida y ejemplos de llaves de A que no estan en B."""
    if len(claves_a) != len(claves_b) or not claves_a:
        raise ValueError("Elija la misma cantidad de columnas llave en A y en B (al menos una).")

    ka = _llaves_normalizadas(df_a, claves_a, modo, ancho)
    kb = _llaves_normalizadas(df_b, claves_b, modo, ancho)
    kb_a = kb.copy()
    kb_a.columns = list(ka.columns)  # mismos nombres para poder cruzar

    ka_ok, kb_ok = ka.dropna(), kb_a.dropna()
    dup_a, dup_b = int(ka_ok.duplicated().sum()), int(kb_ok.duplicated().sum())
    unicas_a, unicas_b = ka_ok.drop_duplicates(), kb_ok.drop_duplicates()
    en_b = unicas_a.merge(unicas_b, how="inner", on=list(ka.columns))

    if dup_b == 0:
        cardinalidad = "uno a uno" if dup_a == 0 else "muchos a uno"
    else:
        cardinalidad = "uno a muchos" if dup_a == 0 else "muchos a muchos"
    validacion = {"uno a uno": "one_to_one", "muchos a uno": "many_to_one",
                  "uno a muchos": "one_to_many", "muchos a muchos": "many_to_many"}[cardinalidad]

    pct = _pct_filas_con_match(ka, kb_a)
    pct_sin_normalizar = _pct_filas_con_match(
        _llaves_normalizadas(df_a, claves_a, "sin_cambios", 0),
        _llaves_normalizadas(df_b, claves_b, "sin_cambios", 0))

    # llaves de A que no estan en B, de la mas a la menos frecuente
    marcado = ka_ok.merge(unicas_b.assign(_en_b=1), how="left", on=list(ka.columns))
    sin_pareja = marcado[marcado["_en_b"].isna()].drop(columns="_en_b")
    ejemplos = (sin_pareja.value_counts().head(10).reset_index(name="filas_en_A")
                if len(sin_pareja) else pd.DataFrame(columns=list(ka.columns) + ["filas_en_A"]))

    return {
        "filas_a": len(df_a), "filas_b": len(df_b),
        "nulos_a": int(ka.isna().any(axis=1).sum()), "nulos_b": int(kb.isna().any(axis=1).sum()),
        "unicas_a": len(unicas_a), "unicas_b": len(unicas_b),
        "repetidas_a": dup_a, "repetidas_b": dup_b,
        "mismos_nombres_exactos": [a == b for a, b in zip(claves_a, claves_b)],
        "mismos_nombres": [normalizar_nombre(a) == normalizar_nombre(b)
                           for a, b in zip(claves_a, claves_b)],
        "mismos_tipos": [str(df_a[a].dtype) == str(df_b[b].dtype)
                         for a, b in zip(claves_a, claves_b)],
        "llaves_de_a_en_b": len(en_b),
        "pct_filas_con_pareja": pct,
        "pct_sin_normalizar": pct_sin_normalizar,
        "semaforo": semaforo_match(pct),
        "cardinalidad": cardinalidad,
        "validacion_sugerida": validacion,
        "ejemplos_sin_pareja": ejemplos,
    }


def _clave_flexible(valor) -> Optional[str]:
    """Forma muy tolerante de un valor, solo para sugerir llaves: sin
    espacios, minusculas, sin '.0' final y sin ceros a la izquierda."""
    if valor is None or (not isinstance(valor, str) and pd.isna(valor)):
        return None
    texto = str(valor).strip().lower()
    if texto.endswith(".0"):
        texto = texto[:-2]
    if texto.isdigit():
        texto = texto.lstrip("0") or "0"
    return texto or None


def sugerir_llaves(df_a: pd.DataFrame, df_b: pd.DataFrame, maximo: int = 8) -> pd.DataFrame:
    """Pares de columnas (una de A, una de B) que podrian ser la llave del
    cruce: las que se llaman igual y las que comparten muchos valores.
    Devuelve columna_a, columna_b, mismo_nombre y coincidencia_pct (que
    porcentaje de los valores distintos de la mas pequeña esta en la otra)."""
    def valores(df, col):
        muestra = df[col].dropna().head(5000).map(_clave_flexible).dropna()
        return set(muestra.unique())

    conjuntos_a = {c: valores(df_a, c) for c in df_a.columns}
    conjuntos_b = {c: valores(df_b, c) for c in df_b.columns}
    filas = []
    for ca, va in conjuntos_a.items():
        if len(va) < 2:
            continue
        for cb, vb in conjuntos_b.items():
            if len(vb) < 2:
                continue
            comunes = len(va & vb)
            coincidencia = comunes / min(len(va), len(vb)) * 100
            mismo_nombre = normalizar_nombre(ca) == normalizar_nombre(cb)
            if coincidencia >= 30 or (mismo_nombre and comunes > 0):
                filas.append({"columna_a": ca, "columna_b": cb, "mismo_nombre": mismo_nombre,
                              "coincidencia_pct": round(coincidencia, 1)})
    if not filas:
        return pd.DataFrame(columns=["columna_a", "columna_b", "mismo_nombre", "coincidencia_pct"])
    tabla = pd.DataFrame(filas)
    return (tabla.sort_values(["coincidencia_pct", "mismo_nombre"], ascending=False, kind="stable")
            .head(maximo).reset_index(drop=True))


# =============================================================================
# Preparar B, unir y auditar
# =============================================================================

def renombrar_columnas_b(df_b: pd.DataFrame, df_a: pd.DataFrame, claves_b: List[str],
                         prefijo: str = "", solo_repetidas: bool = True) -> Tuple[pd.DataFrame, Dict]:
    """Agrega `prefijo` a las columnas de B (menos las llaves). Con
    `solo_repetidas=True` solo a las que tambien existen en A, para evitar
    columnas duplicadas tipo 'state_x' / 'state_y'. Devuelve (df, {viejo: nuevo})."""
    if not prefijo:
        return df_b, {}
    mapa = {}
    for c in df_b.columns:
        if c in claves_b:
            continue
        if solo_repetidas and c not in df_a.columns:
            continue
        mapa[c] = f"{prefijo}{c}"
    return df_b.rename(columns=mapa), mapa


def hacer_merge(df_a: pd.DataFrame, df_b: pd.DataFrame, claves_a: List[str], claves_b: List[str],
                how: str = "left", validate: Optional[str] = None, modo: str = "texto",
                ancho: int = 0, prefijo_b: str = "", solo_repetidas: bool = True,
                agregaciones: Optional[Dict[str, str]] = None, sufijos=("", "_b"),
                conservar_indicador: bool = False) -> Tuple[pd.DataFrame, Dict]:
    """Une A con B. Orden de los pasos (el mismo del script generado):
      1. normaliza las llaves de las dos tablas
      2. descarta de B las filas sin llave (pandas cruzaria nulo con nulo)
      3. si hay `agregaciones`, deja una fila por llave en B
      4. renombra columnas de B con `prefijo_b`
      5. merge
    Devuelve (resultado, auditoria). La auditoria trae filas antes/despues,
    el conteo left_only/both/right_only, el % de filas de A con pareja y
    una lista de advertencias."""
    if how not in TIPOS_UNION:
        raise ValueError(f"Tipo de union desconocido: {how}")
    if len(claves_a) != len(claves_b) or not claves_a:
        raise ValueError("Elija la misma cantidad de columnas llave en A y en B (al menos una).")

    a, b = df_a.copy(), df_b.copy()
    for ca, cb in zip(claves_a, claves_b):  # 1. llaves normalizadas
        a[ca] = normalizar_llave(a[ca], modo, ancho)
        b[cb] = normalizar_llave(b[cb], modo, ancho)

    antes_b = len(b)
    b = b.dropna(subset=list(claves_b))  # 2. B sin llave nula
    descartadas_b = antes_b - len(b)

    if agregaciones:  # 3. una fila por llave
        b = colapsar_por_llave(b, claves_b, agregaciones)

    b, renombradas = renombrar_columnas_b(b, a, list(claves_b), prefijo_b, solo_repetidas)

    ka = _llaves_normalizadas(a, list(claves_a), "sin_cambios", 0)
    kb = _llaves_normalizadas(b, list(claves_b), "sin_cambios", 0)
    pct = _pct_filas_con_match(ka, kb)

    try:  # 5. merge
        resultado = pd.merge(a, b, left_on=list(claves_a), right_on=list(claves_b), how=how,
                             validate=validate or None, suffixes=tuple(sufijos), indicator=True)
    except pd.errors.MergeError as exc:
        detalle = str(exc).splitlines()[0]  # primera linea del aviso de pandas
        raise ValueError(
            f"La validación '{validate}' falló: {detalle}. Revise si las llaves se repiten "
            f"(en B conviene una fila por llave: use «Dejar una fila por llave»)."
        ) from exc

    conteo = resultado["_merge"].value_counts().to_dict()
    conteo = {str(k): int(v) for k, v in conteo.items() if v}
    if not conservar_indicador:
        resultado = resultado.drop(columns=["_merge"])

    advertencias = []
    if descartadas_b:
        advertencias.append(f"Se descartaron {descartadas_b} filas de B por no tener llave.")
    if how == "inner" and len(resultado) < len(a):
        advertencias.append(
            f"El inner join dejó afuera {len(a) - len(resultado)} filas de A que no tienen "
            f"pareja en B. Si quiere conservarlas, use left.")
    if how in ("left", "inner") and len(resultado) > len(a):
        advertencias.append(
            f"El resultado tiene {len(resultado) - len(a)} filas MÁS que A: B tiene llaves "
            f"repetidas y cada fila de A se multiplicó. Deje una fila por llave en B.")
    if how in ("right", "outer") and conteo.get("right_only"):
        advertencias.append(f"{conteo['right_only']} filas de B no tienen pareja en A.")
    if pct < 50:
        advertencias.append("Menos de la mitad de las filas de A encontraron pareja: "
                            "revise que las columnas llave sean las correctas.")

    auditoria = {
        "filas_a": len(df_a), "filas_b": len(df_b), "filas_resultado": len(resultado),
        "columnas_resultado": resultado.shape[1],
        "conteo_merge": conteo, "pct_filas_con_pareja": pct, "semaforo": semaforo_match(pct),
        "descartadas_b": descartadas_b, "renombradas": renombradas, "advertencias": advertencias,
    }
    return resultado, auditoria


def rellenar_con_respaldo(df: pd.DataFrame, destino: str, respaldo: str,
                          eliminar_respaldo: bool = True) -> pd.DataFrame:
    """Plan B: donde `destino` quedo vacio, usa el valor de `respaldo`
    (ej. la poblacion del municipio, y si no hay, la del estado)."""
    df = df.copy()
    df[destino] = df[destino].where(df[destino].notna(), df[respaldo])
    if eliminar_respaldo and respaldo != destino:
        df = df.drop(columns=[respaldo])
    return df


# =============================================================================
# Script
# =============================================================================

def generar_script_merge(nombre_a: str, nombre_b: str, claves_a: List[str], claves_b: List[str],
                         hoja_a: Optional[str] = None, hoja_b: Optional[str] = None,
                         how: str = "left", validate: Optional[str] = None, modo: str = "texto",
                         ancho: int = 0, prefijo_b: str = "", solo_repetidas: bool = True,
                         agregaciones: Optional[Dict[str, str]] = None, sufijos=("", "_b"),
                         conservar_indicador: bool = False,
                         rellenos: Optional[List[Tuple[str, str]]] = None,
                         formato_salida: str = "csv") -> str:
    """Script pandas que repite el merge hecho en la app (mismos pasos y en
    el mismo orden que hacer_merge). Trae copiadas las funciones auxiliares."""
    def carga(variable, constante, nombre, hoja):
        if nombre.lower().endswith((".xlsx", ".xlsm", ".xls")):
            return (f'{variable} = pd.read_excel({constante}, sheet_name={hoja!r}, dtype=str)'
                    f'.fillna("")  # hoja: {hoja}')
        return f'{variable} = pd.read_csv({constante}, dtype=str, keep_default_na=False)'

    partes = [
        "# -*- coding: utf-8 -*-",
        "# Script generado por Limpiador de Tablas (Merge).",
        "# Repite paso a paso el cruce hecho en la app. Solo necesita pandas y numpy.",
        "import re",
        "import numpy as np",
        "import pandas as pd",
        "",
        f"ARCHIVO_A = {nombre_a!r}  # tabla A: la que manda, cambie por su ruta",
        f"ARCHIVO_B = {nombre_b!r}  # tabla B: la que enriquece, cambie por su ruta",
        f"CLAVES_A = {list(claves_a)!r}",
        f"CLAVES_B = {list(claves_b)!r}",
        f"MODO_LLAVE = {modo!r}  # texto | codigo | sin_cambios",
        f"ANCHO_LLAVE = {int(ancho)}  # ceros a la izquierda (solo modo codigo)",
        "",
        "# ---------------- Funciones auxiliares ----------------",
    ]
    for funcion in _FUNCIONES_SCRIPT:
        partes += [inspect.getsource(funcion).rstrip(), ""]
    partes += [
        "",
        "# ---------------- Carga ----------------",
        carga("df_a", "ARCHIVO_A", nombre_a, hoja_a),
        carga("df_b", "ARCHIVO_B", nombre_b, hoja_b),
        'print(f"A: {df_a.shape[0]} filas | B: {df_b.shape[0]} filas")',
        "",
        "# ---------------- 1. Normalizar las llaves en ambas tablas ----------------",
        "for col_a, col_b in zip(CLAVES_A, CLAVES_B):",
        "    df_a[col_a] = normalizar_llave(df_a[col_a], MODO_LLAVE, ANCHO_LLAVE)",
        "    df_b[col_b] = normalizar_llave(df_b[col_b], MODO_LLAVE, ANCHO_LLAVE)",
        "",
        "# ---------------- 2. Descartar de B las filas sin llave ----------------",
        "df_b = df_b.dropna(subset=CLAVES_B)",
        "",
    ]
    if agregaciones:
        partes += [
            "# ---------------- 3. Una fila por llave en B ----------------",
            f"AGREGACIONES = {dict(agregaciones)!r}",
            "df_b = colapsar_por_llave(df_b, CLAVES_B, AGREGACIONES)",
            "",
        ]
    if prefijo_b:
        solo = "solo_repetidas" if solo_repetidas else "todas"
        partes += [
            f"# ---------------- 4. Prefijo en las columnas de B ({solo}) ----------------",
            f"PREFIJO_B = {prefijo_b!r}",
            "renombres = {c: PREFIJO_B + c for c in df_b.columns if c not in CLAVES_B"
            + (" and c in df_a.columns}" if solo_repetidas else "}"),
            "df_b = df_b.rename(columns=renombres)",
            "",
        ]
    partes += [
        "# ---------------- 5. Merge ----------------",
        "df = pd.merge(",
        "    df_a, df_b,",
        "    left_on=CLAVES_A, right_on=CLAVES_B,",
        f"    how={how!r},",
        f"    validate={(validate or None)!r},",
        f"    suffixes={tuple(sufijos)!r},",
        "    indicator=True,",
        ")",
        'print("Auditoria del cruce:")',
        'print(df["_merge"].value_counts().to_string())',
        f'print(f"Filas: A={{len(df_a)}} -> resultado={{len(df)}}")',
    ]
    if not conservar_indicador:
        partes.append('df = df.drop(columns=["_merge"])')
    partes.append("")
    if rellenos:
        partes.append("# ---------------- 6. Plan B: rellenar con una columna de respaldo ----------------")
        for destino, respaldo in rellenos:
            partes.append(f'df[{destino!r}] = df[{destino!r}].where(df[{destino!r}].notna(), df[{respaldo!r}])')
            if respaldo != destino:
                partes.append(f'df = df.drop(columns=[{respaldo!r}])')
        partes.append("")
    partes += ["# ---------------- Guardar ----------------"]
    partes.append('df.to_excel("resultado.xlsx", index=False)' if formato_salida == "xlsx"
                  else 'df.to_csv("resultado.csv", index=False, encoding="utf-8-sig")')
    partes.append("")
    return "\n".join(partes)

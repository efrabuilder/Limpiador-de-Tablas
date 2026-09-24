"""
Aplica acciones de corrección sobre el DataFrame según la configuración
elegida por el usuario para cada tipo de problema.

Acciones disponibles:
  - 'eliminar_fila'      : elimina la fila completa
  - 'reemplazar_media'   : reemplaza el valor por la media de la columna
  - 'reemplazar_mediana' : reemplaza el valor por la mediana de la columna
  - 'reemplazar_moda'    : reemplaza el valor por la moda de la columna
  - 'limitar' (winsorize): recorta atípicos al límite del rango válido (IQR)
  - 'marcar_solo'        : no modifica el dato, solo queda registrado en el reporte
  - 'valor_fijo'         : reemplaza por un valor fijo dado (para faltantes)
  - 'usar_sugerido'      : reemplaza por el valor sugerido calculado por el
                            analizador (ej. total correcto, forma canónica de
                            un texto); solo aplica a 'formula_incorrecta' y
                            'texto_inconsistente', cuando el hallazgo trae
                            valor_sugerido

Tipos de hallazgo nuevos (además de faltante/duplicado/atipico/tipo_invalido):
  - 'fecha_invalida', 'email_invalido', 'telefono_invalido', 'id_duplicado',
    'formula_incorrecta', 'texto_inconsistente', 'estado_invalido',
    'capitalizacion_incorrecta'
  Por defecto se dejan en 'marcar_solo' (corregirlos automáticamente es
  riesgoso: un email o teléfono "corregido" a ciegas puede quedar mal); se
  pueden pasar a 'valor_fijo', 'usar_sugerido' o 'eliminar_fila' vía config.
"""
from __future__ import annotations
import re
import pandas as pd
import numpy as np
from typing import Dict, List
from .analyzer import Issue, detectar_atipicos_iqr, _es_valor_vacio, _serie_no_vacios
from .patrones import (
    formato_fecha_python, FORMATO_FECHA_POR_DEFECTO,
    rango_plausible_fijo, es_columna_no_negativa, a_numero_tolerante,
)

ACCIONES_VALIDAS = {
    "eliminar_fila", "reemplazar_media", "reemplazar_mediana",
    "reemplazar_moda", "limitar", "marcar_solo", "valor_fijo", "usar_sugerido",
    "editar_individualmente", "normalizar_formato_fecha",
}

DEFAULT_CONFIG = {
    "faltante": "reemplazar_mediana",
    "duplicado": "eliminar_fila",
    "atipico": "limitar",
    "tipo_invalido": "marcar_solo",
    "fecha_invalida": "marcar_solo",
    "email_invalido": "marcar_solo",
    "telefono_invalido": "marcar_solo",
    "id_duplicado": "marcar_solo",
    "formula_incorrecta": "marcar_solo",
    "texto_inconsistente": "marcar_solo",
    "estado_invalido": "marcar_solo",
    "capitalizacion_incorrecta": "marcar_solo",
    "espacio_extra": "usar_sugerido",
}

# Tipos nuevos para los que 'valor_fijo' reemplaza directamente el valor
# de la celda (no requieren cálculo de media/mediana/moda).
_TIPOS_VALOR_FIJO_DIRECTO = {
    "fecha_invalida", "email_invalido", "telefono_invalido",
    "id_duplicado", "formula_incorrecta", "texto_inconsistente",
    "estado_invalido", "capitalizacion_incorrecta", "espacio_extra",
}
# Tipos para los que existe un valor_sugerido calculado por el analizador.
_TIPOS_CON_SUGERENCIA = {"formula_incorrecta", "texto_inconsistente", "capitalizacion_incorrecta", "espacio_extra"}
# Tipos para los que 'editar_individualmente' tiene sentido: cada hallazgo
# tiene una sola columna + un solo valor de celda que corregir uno por uno.
# 'duplicado' queda afuera porque su Issue no trae columna/valor puntual
# (columna=None, valor_original=la fila completa).
_TIPOS_EDITAR_INDIVIDUAL = _TIPOS_VALOR_FIJO_DIRECTO | {"faltante", "tipo_invalido", "atipico"}


def _asignar(df: pd.DataFrame, fila: int, columna: str, valor) -> None:
    """
    Asigna 'valor' en df.at[fila, columna], convirtiendo la columna a dtype
    'object' si el valor no es compatible con el dtype actual. Cubre ambos
    sentidos: escribir texto en una columna numérica (p. ej. un
    'valor_fijo' de texto sobre una columna float) y escribir un número en
    una columna de texto estricta (StringDtype).
    """
    try:
        df.at[fila, columna] = valor
    except (TypeError, ValueError):
        df[columna] = df[columna].astype(object)
        df.at[fila, columna] = valor


def _interpretar_valor_fijo(valor):
    """Si el usuario escribio literalmente "null" (sin importar mayusculas)
    como valor fijo, lo tratamos como el valor vacio real (None/NaN) en vez
    del texto "null" -- util, por ejemplo, para forzar a null los datos que
    no encajan al convertir una columna a booleano."""
    if isinstance(valor, str) and valor.strip().lower() == "null":
        return None
    return valor


def _a_numero(serie: pd.Series) -> pd.Series:
    return a_numero_tolerante(serie)


def _es_columna_numerica(serie: pd.Series, serie_num: pd.Series) -> bool:
    """True si la mayoria de los valores no vacios de la columna se pueden
    leer como numero (ya sea numeros reales o texto tipo "₡7,650")."""
    no_vacios = _serie_no_vacios(serie)
    if no_vacios.empty:
        return False
    return serie_num.notna().sum() >= 0.5 * len(no_vacios)


def _admite_estadistico(df: pd.DataFrame, columna: str, accion: str) -> bool:
    """False cuando se pidio media/mediana sobre una columna de texto (ej.
    'cancelo', 'encuesta_salida'): no existe media ni mediana de un texto,
    asi que en ese caso se usa la moda (ver _valor_reemplazo)."""
    if accion not in ("reemplazar_media", "reemplazar_mediana"):
        return True
    return _es_columna_numerica(df[columna], _a_numero(df[columna]))


def _valor_reemplazo(df: pd.DataFrame, columna: str, accion: str, valor_fijo=None):
    if accion == "valor_fijo":
        return _interpretar_valor_fijo(valor_fijo)
    if accion not in ("reemplazar_media", "reemplazar_mediana", "reemplazar_moda"):
        return None

    serie = df[columna]
    serie_num = _a_numero(serie)
    if _es_columna_numerica(serie, serie_num):
        if accion == "reemplazar_media":
            valor = serie_num.mean()
        elif accion == "reemplazar_mediana":
            valor = serie_num.median()
        else:
            moda = serie_num.mode(dropna=True)
            valor = moda.iloc[0] if not moda.empty else None
        return None if valor is None or pd.isna(valor) else valor

    # Columna de texto: media y mediana no existen -> se usa la moda (el
    # valor mas frecuente entre las celdas NO vacias). Antes, la mediana de
    # un texto daba NaN y el reporte dejaba la celda en null.
    moda = _serie_no_vacios(serie).mode()
    return moda.iloc[0] if not moda.empty else None


def _limites_para_limitar(df: pd.DataFrame, columna: str):
    """Limites (inferior, superior) para la accion 'limitar' de una columna.

    Parte del rango IQR y lo cruza con el rango logico de la columna (edad
    en [0, 120], conteos/cantidades >= 0), para que un valor imposible (ej.
    -2 quejas) se lleve al minimo valido en vez de saltar al otro extremo
    del rango. Si la columna solo tiene enteros, los limites se redondean
    hacia adentro (no existen 60.5 anios ni 85.75 meses)."""
    serie = _a_numero(df[columna])
    q1, q3 = serie.quantile(0.25), serie.quantile(0.75)
    iqr = q3 - q1
    lim_inf, lim_sup = q1 - 1.5 * iqr, q3 + 1.5 * iqr

    rango = rango_plausible_fijo(columna)
    if rango is not None:
        minimo, maximo = rango
        lim_inf = max(lim_inf, minimo) if not pd.isna(lim_inf) else minimo
        lim_sup = min(lim_sup, maximo) if not pd.isna(lim_sup) else maximo
    elif es_columna_no_negativa(columna):
        lim_inf = max(lim_inf, 0) if not pd.isna(lim_inf) else 0

    no_nulos = serie.dropna()
    es_entera = len(no_nulos) > 0 and bool((no_nulos % 1 == 0).all())
    if es_entera:
        if not pd.isna(lim_inf):
            lim_inf = int(np.ceil(lim_inf))
        if not pd.isna(lim_sup):
            lim_sup = int(np.floor(lim_sup))
        if not pd.isna(lim_inf) and not pd.isna(lim_sup) and lim_inf > lim_sup:
            lim_inf = lim_sup = int(round(serie.median()))
    return lim_inf, lim_sup


def _normalizar_fechas_columna(serie: pd.Series, formato_python: str) -> pd.Series:
    """Reescribe como texto, en `formato_python` (strftime), cada valor de
    `serie` que se pueda interpretar como fecha -- así una columna que
    mezcla '2024-01-15' con '20/02/2024' queda con un único formato
    consistente. Las celdas vacías (ver _es_valor_vacio) y las que de
    verdad no se puedan interpretar como fecha se dejan tal cual, para no
    inventar una fecha donde no la hay."""
    parseado = pd.to_datetime(serie, errors="coerce")
    resultado = serie.astype(object).copy()
    for idx, val in serie.items():
        if _es_valor_vacio(val):
            continue
        fecha = parseado.loc[idx]
        if pd.isna(fecha):
            fecha = pd.to_datetime(val, errors="coerce", dayfirst=True)
        if pd.isna(fecha):
            continue
        resultado.loc[idx] = fecha.strftime(formato_python)
    return resultado


def _buscar_valor_fijo(valores_fijos: Dict, tipo: str, columna: str):
    """Busca el valor fijo especifico para (tipo, columna). Si no esta ahi,
    cae al valor fijo generico por columna (compatibilidad con dicts viejos
    que no distinguian el tipo de problema, ej. desde cli.py o api.py)."""
    if (tipo, columna) in valores_fijos:
        return valores_fijos[(tipo, columna)]
    return valores_fijos.get(columna)


def limpiar(df: pd.DataFrame, issues: List[Issue], config: Dict[str, str] = None,
            valores_fijos: Dict[str, object] = None,
            correcciones_individuales: Dict[tuple, object] = None,
            formatos_fecha: Dict[str, str] = None) -> tuple[pd.DataFrame, List[dict]]:
    """
    Aplica las acciones configuradas por tipo de problema.

    `correcciones_individuales` es para la acción 'editar_individualmente'
    (ej. corregir cada teléfono inválido por separado en vez de un único
    valor fijo para todos): dict con clave (tipo, columna, fila) -> valor
    corregido. Los hallazgos de ese tipo que no tengan una entrada aquí
    quedan con su valor original (igual que 'marcar_solo').

    `formatos_fecha` es para la acción 'normalizar_formato_fecha' del tipo
    'fecha_invalida': dict columna -> clave de
    patrones.FORMATOS_FECHA_DISPONIBLES (ej. {"fecha_venta": "dd/mm/aaaa"}).
    Cuando esa acción está activa para una columna, TODA la columna se
    reescribe con el formato elegido (no solo las celdas marcadas como
    hallazgo), para que quede consistente de punta a punta -- una columna
    que mezcla '2024-01-15' con '20/02/2024' termina con un único formato.
    Las columnas sin entrada en `formatos_fecha` usan el formato por
    defecto (patrones.FORMATO_FECHA_POR_DEFECTO).

    Devuelve (df_limpio, registro_acciones) donde registro_acciones es una
    lista de dicts lista para construir el reporte detallado.
    """
    config = {**DEFAULT_CONFIG, **(config or {})}
    valores_fijos = valores_fijos or {}
    correcciones_individuales = correcciones_individuales or {}
    formatos_fecha = formatos_fecha or {}
    df_limpio = df.copy()
    registro = []
    filas_a_eliminar = set()

    # Pre-calcular límites por columna para la acción "limitar" (IQR cruzado
    # con el rango lógico de la columna, ver _limites_para_limitar)
    limites_iqr = {}
    for issue in issues:
        if issue.tipo == "atipico" and issue.columna and issue.columna not in limites_iqr:
            limites_iqr[issue.columna] = _limites_para_limitar(df, issue.columna)

    # Cache de media/mediana/moda por (columna, accion): son iguales para
    # todos los hallazgos de la misma columna, no hace falta recalcularlas.
    _cache_repl: Dict[tuple, object] = {}
    _cache_admite: Dict[tuple, bool] = {}

    def _reemplazo(columna, accion, valor_fijo=None):
        if accion == "valor_fijo":
            return _valor_reemplazo(df, columna, accion, valor_fijo)
        clave = (columna, accion)
        if clave not in _cache_repl:
            _cache_repl[clave] = _valor_reemplazo(df, columna, accion)
        return _cache_repl[clave]

    def _admite(columna, accion):
        clave = (columna, accion)
        if clave not in _cache_admite:
            _cache_admite[clave] = _admite_estadistico(df, columna, accion)
        return _cache_admite[clave]

    for issue in issues:
        accion = config.get(issue.tipo, "marcar_solo")
        valor_nuevo = None

        if accion == "marcar_solo":
            valor_nuevo = issue.valor_original

        elif accion == "eliminar_fila":
            filas_a_eliminar.add(issue.fila)
            valor_nuevo = "(fila eliminada)"

        elif issue.tipo in ("faltante", "tipo_invalido") and accion in (
            "reemplazar_media", "reemplazar_mediana", "reemplazar_moda", "valor_fijo"
        ):
            valor_nuevo = _reemplazo(
                issue.columna, accion, _buscar_valor_fijo(valores_fijos, issue.tipo, issue.columna)
            )
            _asignar(df_limpio, issue.fila, issue.columna, valor_nuevo)

        elif issue.tipo == "atipico" and accion == "limitar":
            lim_inf, lim_sup = limites_iqr.get(issue.columna, (None, None))
            valor_original_num = _a_numero(pd.Series([issue.valor_original]))[0]
            if lim_inf is not None and not pd.isna(valor_original_num):
                valor_nuevo = valor_original_num
                if not pd.isna(lim_inf) and valor_nuevo < lim_inf:
                    valor_nuevo = lim_inf
                if not pd.isna(lim_sup) and valor_nuevo > lim_sup:
                    valor_nuevo = lim_sup
                _asignar(df_limpio, issue.fila, issue.columna, valor_nuevo)

        elif issue.tipo == "atipico" and accion in (
            "reemplazar_media", "reemplazar_mediana", "reemplazar_moda"
        ):
            valor_nuevo = _reemplazo(issue.columna, accion)
            _asignar(df_limpio, issue.fila, issue.columna, valor_nuevo)

        elif issue.tipo in _TIPOS_CON_SUGERENCIA and accion == "usar_sugerido" \
                and issue.valor_sugerido is not None:
            valor_nuevo = issue.valor_sugerido
            _asignar(df_limpio, issue.fila, issue.columna, valor_nuevo)

        elif issue.tipo in _TIPOS_VALOR_FIJO_DIRECTO and accion == "valor_fijo":
            valor_nuevo = _interpretar_valor_fijo(_buscar_valor_fijo(valores_fijos, issue.tipo, issue.columna))
            _asignar(df_limpio, issue.fila, issue.columna, valor_nuevo)

        elif issue.tipo in _TIPOS_EDITAR_INDIVIDUAL and accion == "editar_individualmente":
            clave = (issue.tipo, issue.columna, issue.fila)
            if clave in correcciones_individuales:
                valor_nuevo = correcciones_individuales[clave]
                _asignar(df_limpio, issue.fila, issue.columna, valor_nuevo)
            else:
                valor_nuevo = issue.valor_original

        else:
            valor_nuevo = issue.valor_original

        detalle = issue.detalle
        if issue.columna and issue.columna in df.columns \
                and accion in ("reemplazar_media", "reemplazar_mediana") \
                and issue.tipo in ("faltante", "tipo_invalido", "atipico") \
                and not _admite(issue.columna, accion):
            detalle += " | columna de texto: no admite media/mediana, se usó la moda"

        registro.append({
            "tipo": issue.tipo,
            "columna": issue.columna or "(fila completa)",
            "fila": issue.fila,
            "valor_original": issue.valor_original,
            "accion_aplicada": accion,
            "valor_nuevo": valor_nuevo,
            "detalle": detalle,
        })

    # 'normalizar_formato_fecha' no reemplaza celda por celda dentro del
    # bucle de arriba (no es un valor fijo puntual): reescribe la columna
    # de fecha COMPLETA en el formato elegido, incluyendo las celdas que
    # no tenían hallazgo (para que el resultado quede consistente en toda
    # la columna, no solo en las filas marcadas). Se hace aparte, al final,
    # y luego se corrige el registro para que el reporte muestre el valor
    # ya normalizado en vez del valor original sin tocar.
    if config.get("fecha_invalida") == "normalizar_formato_fecha":
        columnas_fecha_config = {
            issue.columna for issue in issues
            if issue.tipo == "fecha_invalida" and issue.columna and issue.columna in df_limpio.columns
        }
        for col in columnas_fecha_config:
            clave_formato = formatos_fecha.get(col, FORMATO_FECHA_POR_DEFECTO)
            df_limpio[col] = _normalizar_fechas_columna(df_limpio[col], formato_fecha_python(clave_formato))
        for entry in registro:
            if entry["tipo"] == "fecha_invalida" and entry["columna"] in columnas_fecha_config \
                    and entry["fila"] in df_limpio.index:
                entry["valor_nuevo"] = df_limpio.at[entry["fila"], entry["columna"]]

    # Las celdas con accion 'marcar_solo' no se modifican, así que sin una
    # marca explícita quedan indistinguibles del resto de la tabla. Se agrega
    # una columna con las etiquetas de los problemas detectados en cada fila
    # (solo para las filas que tuvieron al menos un hallazgo marcado así).
    marcas_calidad: Dict[int, List[str]] = {}
    for entry in registro:
        sin_corregir = (
            entry["accion_aplicada"] == "marcar_solo"
            or (entry["accion_aplicada"] == "editar_individualmente"
                and entry["valor_nuevo"] == entry["valor_original"])
        )
        if not sin_corregir:
            continue
        etiqueta = (
            entry["tipo"] if entry["columna"] == "(fila completa)"
            else f"{entry['tipo']}:{entry['columna']}"
        )
        marcas_calidad.setdefault(entry["fila"], []).append(etiqueta)

    if marcas_calidad:
        columna_marca = "_revisar_calidad"
        while columna_marca in df_limpio.columns:
            columna_marca += "_"
        df_limpio[columna_marca] = ""
        for fila, etiquetas in marcas_calidad.items():
            if fila in df_limpio.index:
                df_limpio.at[fila, columna_marca] = "; ".join(etiquetas)

    if filas_a_eliminar:
        df_limpio = df_limpio.drop(index=list(filas_a_eliminar), errors="ignore").reset_index(drop=True)

    return df_limpio, registro

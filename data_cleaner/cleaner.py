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
    'formula_incorrecta', 'texto_inconsistente'
  Por defecto se dejan en 'marcar_solo' (corregirlos automáticamente es
  riesgoso: un email o teléfono "corregido" a ciegas puede quedar mal); se
  pueden pasar a 'valor_fijo', 'usar_sugerido' o 'eliminar_fila' vía config.
"""
from __future__ import annotations
import pandas as pd
import numpy as np
from typing import Dict, List
from .analyzer import Issue, detectar_atipicos_iqr

ACCIONES_VALIDAS = {
    "eliminar_fila", "reemplazar_media", "reemplazar_mediana",
    "reemplazar_moda", "limitar", "marcar_solo", "valor_fijo", "usar_sugerido",
    "editar_individualmente",
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
}

# Tipos nuevos para los que 'valor_fijo' reemplaza directamente el valor
# de la celda (no requieren cálculo de media/mediana/moda).
_TIPOS_VALOR_FIJO_DIRECTO = {
    "fecha_invalida", "email_invalido", "telefono_invalido",
    "id_duplicado", "formula_incorrecta", "texto_inconsistente",
}
# Tipos para los que existe un valor_sugerido calculado por el analizador.
_TIPOS_CON_SUGERENCIA = {"formula_incorrecta", "texto_inconsistente"}


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


def _valor_reemplazo(df: pd.DataFrame, columna: str, accion: str, valor_fijo=None):
    serie_num = pd.to_numeric(df[columna], errors="coerce")
    if accion == "reemplazar_media":
        return serie_num.mean()
    if accion == "reemplazar_mediana":
        return serie_num.median()
    if accion == "reemplazar_moda":
        moda = df[columna].mode(dropna=True)
        return moda.iloc[0] if not moda.empty else None
    if accion == "valor_fijo":
        return valor_fijo
    return None


def limpiar(df: pd.DataFrame, issues: List[Issue], config: Dict[str, str] = None,
            valores_fijos: Dict[str, object] = None,
            correcciones_individuales: Dict[tuple, object] = None) -> tuple[pd.DataFrame, List[dict]]:
    """
    Aplica las acciones configuradas por tipo de problema.

    `correcciones_individuales` es para la acción 'editar_individualmente'
    (ej. corregir cada teléfono inválido por separado en vez de un único
    valor fijo para todos): dict con clave (tipo, columna, fila) -> valor
    corregido. Los hallazgos de ese tipo que no tengan una entrada aquí
    quedan con su valor original (igual que 'marcar_solo').

    Devuelve (df_limpio, registro_acciones) donde registro_acciones es una
    lista de dicts lista para construir el reporte detallado.
    """
    config = {**DEFAULT_CONFIG, **(config or {})}
    valores_fijos = valores_fijos or {}
    correcciones_individuales = correcciones_individuales or {}
    df_limpio = df.copy()
    registro = []
    filas_a_eliminar = set()

    # Pre-calcular límites IQR por columna para la acción "limitar"
    limites_iqr = {}
    for issue in issues:
        if issue.tipo == "atipico" and issue.columna and issue.columna not in limites_iqr:
            serie = pd.to_numeric(df[issue.columna], errors="coerce")
            q1, q3 = serie.quantile(0.25), serie.quantile(0.75)
            iqr = q3 - q1
            limites_iqr[issue.columna] = (q1 - 1.5 * iqr, q3 + 1.5 * iqr)

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
            valor_nuevo = _valor_reemplazo(
                df, issue.columna, accion, valores_fijos.get(issue.columna)
            )
            _asignar(df_limpio, issue.fila, issue.columna, valor_nuevo)

        elif issue.tipo == "atipico" and accion == "limitar":
            lim_inf, lim_sup = limites_iqr.get(issue.columna, (None, None))
            valor_original_num = pd.to_numeric(pd.Series([issue.valor_original]), errors="coerce")[0]
            if lim_inf is not None and not pd.isna(valor_original_num):
                valor_nuevo = lim_inf if valor_original_num < lim_inf else lim_sup
                _asignar(df_limpio, issue.fila, issue.columna, valor_nuevo)

        elif issue.tipo == "atipico" and accion in (
            "reemplazar_media", "reemplazar_mediana", "reemplazar_moda"
        ):
            valor_nuevo = _valor_reemplazo(df, issue.columna, accion)
            _asignar(df_limpio, issue.fila, issue.columna, valor_nuevo)

        elif issue.tipo in _TIPOS_CON_SUGERENCIA and accion == "usar_sugerido" \
                and issue.valor_sugerido is not None:
            valor_nuevo = issue.valor_sugerido
            _asignar(df_limpio, issue.fila, issue.columna, valor_nuevo)

        elif issue.tipo in _TIPOS_VALOR_FIJO_DIRECTO and accion == "valor_fijo":
            valor_nuevo = valores_fijos.get(issue.columna)
            _asignar(df_limpio, issue.fila, issue.columna, valor_nuevo)

        elif issue.tipo in _TIPOS_VALOR_FIJO_DIRECTO and accion == "editar_individualmente":
            clave = (issue.tipo, issue.columna, issue.fila)
            if clave in correcciones_individuales:
                valor_nuevo = correcciones_individuales[clave]
                _asignar(df_limpio, issue.fila, issue.columna, valor_nuevo)
            else:
                valor_nuevo = issue.valor_original

        else:
            valor_nuevo = issue.valor_original

        registro.append({
            "tipo": issue.tipo,
            "columna": issue.columna or "(fila completa)",
            "fila": issue.fila,
            "valor_original": issue.valor_original,
            "accion_aplicada": accion,
            "valor_nuevo": valor_nuevo,
            "detalle": issue.detalle,
        })

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

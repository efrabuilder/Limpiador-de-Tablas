# -*- coding: utf-8 -*-
"""
limpiador_universal.py
=======================
Script de limpieza de datos AUTOCONTENIDO (solo requiere pandas y numpy,
NO depende del paquete data_cleaner). Pensado para usarse como paso de
Python dentro de cualquier herramienta de ETL/BI que soporte scripts de
Python sobre un DataFrame, por ejemplo:

  - Tableau Prep (nodo de script Python vía TabPy)
  - Alteryx (herramienta "Python" / Jupyter embebido)
  - Qlik (extensión de script Python / Qlik Application Automation)
  - Cualquier notebook, cron job o pipeline propio

También funciona como script de línea de comandos independiente.

LÓGICA (idéntica a la del proyecto original data_cleaner) — 10 reglas:

  Las 4 originales:
  - faltante              -> por defecto: reemplazar por la MEDIANA de la columna
  - duplicado             -> por defecto: ELIMINAR la fila duplicada
  - atipico               -> por defecto: LIMITAR (winsorize) al borde del rango IQR
  - tipo_invalido         -> por defecto: MARCAR solamente (no se toca el valor)

  Las 6 nuevas (paridad con la librería Python `limpiar()`):
  - fecha_invalida        -> fecha no parseable, o fuera de [fecha_min, fecha_max]
  - email_invalido        -> no cumple el formato usuario@dominio.tld
  - telefono_invalido     -> formato/longitud de dígitos inválida
  - id_duplicado          -> valor repetido en una columna identificadora
                             (distinto de 'duplicado', que exige fila completa igual)
  - formula_incorrecta    -> una columna no coincide con el resultado de una
                             operación sobre otras (ej. Total ≠ Cantidad × Precio)
  - texto_inconsistente   -> variantes / errores de tipeo de un mismo valor
                             categórico (ej. "San Jose" vs "San José")

  Por defecto, las 6 reglas nuevas quedan en 'marcar_solo': corregirlas
  automáticamente es riesgoso (un email o teléfono "corregido" a ciegas
  puede quedar mal). Se pueden pasar a 'valor_fijo', 'eliminar_fila' o,
  para 'formula_incorrecta'/'texto_inconsistente', a 'usar_sugerido' (usa
  el valor correcto/canónico que el propio detector calculó).

  Las columnas candidatas para cada regla nueva se auto-detectan por
  nombre (fecha, email/correo, teléfono/celular, id/código/folio,
  total/cantidad/precio, columnas de texto de baja cardinalidad) si no se
  pasan explícitamente — ver parámetros de limpiar_tabla().

Cada fila con un hallazgo "marcado" (marcar_solo) queda señalada en una
columna nueva '_revisar_calidad' para que se pueda auditar sin perder el
dato original.

------------------------------------------------------------------
USO COMO LIBRERÍA (recomendado dentro de TabPy / Alteryx / Qlik):
------------------------------------------------------------------
    from limpiador_universal import limpiar_tabla
    df_limpio, df_reporte = limpiar_tabla(df)

    # Con las reglas nuevas activadas explícitamente, por ejemplo:
    df_limpio, df_reporte = limpiar_tabla(
        df,
        email_invalido="marcar_solo",
        telefono_invalido="marcar_solo",
        id_duplicado="marcar_solo",
        formula_incorrecta="usar_sugerido",
        columna_total="total", columna_cantidad="cantidad", columna_precio="precio",
        texto_inconsistente="usar_sugerido",
    )

------------------------------------------------------------------
USO POR LÍNEA DE COMANDOS:
------------------------------------------------------------------
    python limpiador_universal.py entrada.csv
    # Genera:
    #   entrada_limpio.csv
    #   entrada_reporte_limpieza.csv

    Opcional: elegir acciones distintas a las de por defecto
    python limpiador_universal.py entrada.csv --faltante reemplazar_media \
        --duplicado marcar_solo --atipico reemplazar_mediana \
        --email_invalido marcar_solo --telefono_invalido marcar_solo \
        --id_duplicado marcar_solo --formula_incorrecta usar_sugerido \
        --texto_inconsistente usar_sugerido

------------------------------------------------------------------
USO EN TABLEAU PREP / TABPY:
------------------------------------------------------------------
    1. Levantar el servicio TabPy (pip install tabpy && tabpy).
    2. En Tableau Prep, agregar un paso "Script" y conectarlo al servidor
       TabPy.
    3. Copiar este archivo a la carpeta que TabPy usa para scripts, o pegar
       el cuerpo de limpiar_tabla() en el editor del paso, y usar como
       función de entrada algo como:

        import pandas as pd
        from limpiador_universal import limpiar_tabla

        def limpiar(df):
            df_limpio, _reporte = limpiar_tabla(df)
            return df_limpio

------------------------------------------------------------------
USO EN ALTERYX:
------------------------------------------------------------------
    1. Arrastrar la herramienta "Python" (usa Jupyter internamente).
    2. En la celda de código:

        from ayx import Alteryx
        from limpiador_universal import limpiar_tabla

        df = Alteryx.read("#1")
        df_limpio, df_reporte = limpiar_tabla(df)
        Alteryx.write(df_limpio, 1)
        Alteryx.write(df_reporte, 2)

------------------------------------------------------------------
USO EN QLIK (script de carga con extensión Python, p. ej. vía
Qlik Application Automation o un conector Python-Qlik):
------------------------------------------------------------------
    1. Exportar la tabla origen como CSV o pasarla como DataFrame al paso
       de Python.
    2. Llamar a limpiar_tabla(df) igual que en los ejemplos anteriores y
       cargar el resultado (df_limpio) de vuelta al modelo de datos.
"""
from __future__ import annotations
import argparse
import os
import re
import sys
import difflib
import unicodedata
import pandas as pd
import numpy as np

ACCIONES_VALIDAS = {
    "eliminar_fila", "reemplazar_media", "reemplazar_mediana",
    "reemplazar_moda", "limitar", "marcar_solo", "valor_fijo", "usar_sugerido",
}

# Tipos nuevos para los que 'valor_fijo' reemplaza directamente el valor de
# la celda (no requieren cálculo de media/mediana/moda).
_TIPOS_VALOR_FIJO_DIRECTO = {
    "fecha_invalida", "email_invalido", "telefono_invalido",
    "id_duplicado", "formula_incorrecta", "texto_inconsistente",
}
# Tipos para los que existe un valor_sugerido calculado por el detector.
_TIPOS_CON_SUGERENCIA = {"formula_incorrecta", "texto_inconsistente"}

_REGEX_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_REGEX_TELEFONO_FORMATO = re.compile(r"^[\d\s\-\+\(\)]+$")

_PATRONES_EMAIL = ("email", "correo", "e-mail", "mail")
_PATRONES_TELEFONO = ("telefono", "teléfono", "phone", "celular", "movil", "móvil", "whatsapp")
_PATRONES_FECHA = ("fecha", "date")
_PATRONES_TOTAL = ("total",)
_PATRONES_CANTIDAD = ("cantidad", "qty", "cant_")
_PATRONES_PRECIO = ("precio", "price")
_PATRONES_EXCLUIR_TEXTO = _PATRONES_EMAIL + _PATRONES_TELEFONO + _PATRONES_FECHA + \
    ("nombre", "cliente", "direccion", "dirección", "observacion", "observación", "comentario")


# -----------------------------------------------------------------------------
# Utilidades comunes
# -----------------------------------------------------------------------------

def _columna_numerica_potencial(serie: pd.Series) -> bool:
    """True si una columna de texto en realidad contiene números (>70% convertibles)."""
    valores = serie.dropna()
    if len(valores) == 0:
        return False
    convertibles = pd.to_numeric(valores, errors="coerce")
    return convertibles.notna().mean() > 0.7


def _columnas_para_atipicos(df: pd.DataFrame) -> list:
    columnas = df.select_dtypes(include=[np.number]).columns.tolist()
    for col in df.columns:
        if col in columnas:
            continue
        serie = df[col]
        if (pd.api.types.is_object_dtype(serie) or pd.api.types.is_string_dtype(serie)) \
                and _columna_numerica_potencial(serie):
            columnas.append(col)
    return columnas


def _columnas_por_patron(df: pd.DataFrame, patrones: tuple) -> list:
    return [col for col in df.columns if any(p in str(col).lower() for p in patrones)]


def _es_columna_id(col) -> bool:
    low = str(col).lower()
    if low == "id":
        return True
    if low.startswith("id_") or low.endswith("_id") or "_id_" in low:
        return True
    if any(p in low for p in ("codigo", "código", "folio")):
        return True
    return False


def _normalizar_texto(valor) -> str:
    s = str(valor).strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return " ".join(s.split())


def _asignar(df: pd.DataFrame, fila: int, columna: str, valor) -> None:
    """Asigna un valor en una celda, ampliando el dtype de la columna si hace falta."""
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


# -----------------------------------------------------------------------------
# Detección: 4 chequeos originales
# -----------------------------------------------------------------------------

def _detectar_hallazgos_originales(df: pd.DataFrame, factor_iqr: float = 1.5) -> list:
    hallazgos = []

    # Valores faltantes
    for col in df.columns:
        for idx in df[df[col].isna()].index:
            hallazgos.append({"tipo": "faltante", "columna": col, "fila": int(idx),
                               "valor_original": None, "detalle": "Valor vacío/nulo",
                               "valor_sugerido": None})

    # Filas duplicadas (idénticas a una anterior)
    mask_dup = df.duplicated(keep="first")
    for idx in df[mask_dup].index:
        hallazgos.append({"tipo": "duplicado", "columna": None, "fila": int(idx),
                           "valor_original": df.loc[idx].to_dict(),
                           "detalle": "Fila duplicada (idéntica a una anterior)",
                           "valor_sugerido": None})

    # Tipo inválido: texto no numérico en columnas mayormente numéricas
    for col in df.columns:
        serie = df[col]
        es_texto = pd.api.types.is_object_dtype(serie) or pd.api.types.is_string_dtype(serie)
        if es_texto and _columna_numerica_potencial(serie):
            convertidos = pd.to_numeric(serie, errors="coerce")
            malos = serie[(convertidos.isna()) & (serie.notna())]
            for idx, val in malos.items():
                hallazgos.append({"tipo": "tipo_invalido", "columna": col, "fila": int(idx),
                                   "valor_original": val, "detalle": "Se esperaba un valor numérico",
                                   "valor_sugerido": None})

    # Valores atípicos (método IQR)
    for col in _columnas_para_atipicos(df):
        serie = pd.to_numeric(df[col], errors="coerce")
        q1, q3 = serie.quantile(0.25), serie.quantile(0.75)
        iqr = q3 - q1
        if iqr == 0 or pd.isna(iqr):
            continue
        lim_inf, lim_sup = q1 - factor_iqr * iqr, q3 + factor_iqr * iqr
        atipicos = serie[(serie < lim_inf) | (serie > lim_sup)]
        for idx, val in atipicos.items():
            hallazgos.append({"tipo": "atipico", "columna": col, "fila": int(idx),
                               "valor_original": val,
                               "detalle": f"Fuera de rango [{lim_inf:.2f}, {lim_sup:.2f}] (IQR)",
                               "valor_sugerido": None})

    return hallazgos


# -----------------------------------------------------------------------------
# Detección: 6 chequeos nuevos
# -----------------------------------------------------------------------------

def _detectar_fechas_invalidas(df, columnas, fecha_min, fecha_max, auto):
    hallazgos = []
    cols = columnas if columnas is not None else (_columnas_por_patron(df, _PATRONES_FECHA) if auto else [])
    lim_min = pd.Timestamp(fecha_min) if fecha_min is not None else None
    lim_max = pd.Timestamp(fecha_max) if fecha_max is not None else None
    for col in cols:
        if col not in df.columns:
            continue
        serie = df[col]
        parseado = pd.to_datetime(serie, errors="coerce")
        for idx, val in serie.items():
            if pd.isna(val):
                continue
            fecha = parseado.loc[idx]
            if pd.isna(fecha):
                fecha = pd.to_datetime(val, errors="coerce", dayfirst=True)
            if pd.isna(fecha):
                hallazgos.append({"tipo": "fecha_invalida", "columna": col, "fila": int(idx),
                                   "valor_original": val, "detalle": "Formato de fecha no reconocido",
                                   "valor_sugerido": None})
                continue
            if lim_min is not None and fecha < lim_min:
                hallazgos.append({"tipo": "fecha_invalida", "columna": col, "fila": int(idx),
                                   "valor_original": val,
                                   "detalle": f"Fecha fuera de rango (anterior a {lim_min.date()})",
                                   "valor_sugerido": None})
            elif lim_max is not None and fecha > lim_max:
                hallazgos.append({"tipo": "fecha_invalida", "columna": col, "fila": int(idx),
                                   "valor_original": val,
                                   "detalle": f"Fecha fuera de rango (posterior a {lim_max.date()})",
                                   "valor_sugerido": None})
    return hallazgos


def _detectar_emails_invalidos(df, columnas, auto):
    hallazgos = []
    cols = columnas if columnas is not None else (_columnas_por_patron(df, _PATRONES_EMAIL) if auto else [])
    for col in cols:
        if col not in df.columns:
            continue
        for idx, val in df[col].items():
            if pd.isna(val):
                continue
            if not _REGEX_EMAIL.match(str(val).strip()):
                hallazgos.append({"tipo": "email_invalido", "columna": col, "fila": int(idx),
                                   "valor_original": val, "detalle": "Formato de correo electrónico inválido",
                                   "valor_sugerido": None})
    return hallazgos


def _detectar_telefonos_invalidos(df, columnas, patron, min_digitos, max_digitos, auto):
    hallazgos = []
    cols = columnas if columnas is not None else (_columnas_por_patron(df, _PATRONES_TELEFONO) if auto else [])
    regex_formato = re.compile(patron) if patron else _REGEX_TELEFONO_FORMATO
    for col in cols:
        if col not in df.columns:
            continue
        for idx, val in df[col].items():
            if pd.isna(val):
                continue
            texto = str(val).strip()
            solo_digitos = re.sub(r"\D", "", texto)
            formato_ok = bool(regex_formato.match(texto))
            longitud_ok = min_digitos <= len(solo_digitos) <= max_digitos
            if not (formato_ok and longitud_ok):
                rango = f"{min_digitos}" if min_digitos == max_digitos else f"{min_digitos}-{max_digitos}"
                hallazgos.append({"tipo": "telefono_invalido", "columna": col, "fila": int(idx),
                                   "valor_original": val,
                                   "detalle": f"Formato/longitud de teléfono inválido (se esperaban {rango} dígitos)",
                                   "valor_sugerido": None})
    return hallazgos


def _detectar_ids_duplicados(df, columnas, auto):
    cols = columnas if columnas is not None else ([c for c in df.columns if _es_columna_id(c)] if auto else [])
    hallazgos = []
    for col in cols:
        if col not in df.columns:
            continue
        serie = df[col]
        mask = serie.duplicated(keep="first") & serie.notna()
        for idx in serie[mask].index:
            hallazgos.append({"tipo": "id_duplicado", "columna": col, "fila": int(idx),
                               "valor_original": serie.loc[idx],
                               "detalle": f"Valor repetido en columna identificadora '{col}' "
                                          "(la fila completa no es idéntica)",
                               "valor_sugerido": None})
    return hallazgos


def _detectar_formula_incorrecta(df, columna_total, columna_cantidad, columna_precio, tolerancia, auto):
    hallazgos = []
    if auto and not (columna_total and columna_cantidad and columna_precio):
        cand_total = _columnas_por_patron(df, _PATRONES_TOTAL)
        cand_cant = _columnas_por_patron(df, _PATRONES_CANTIDAD)
        cand_precio = _columnas_por_patron(df, _PATRONES_PRECIO)
        if cand_total and cand_cant and cand_precio:
            columna_total = columna_total or cand_total[0]
            columna_cantidad = columna_cantidad or cand_cant[0]
            columna_precio = columna_precio or cand_precio[0]

    if not (columna_total and columna_cantidad and columna_precio):
        return hallazgos
    if not all(c in df.columns for c in (columna_total, columna_cantidad, columna_precio)):
        return hallazgos

    total = pd.to_numeric(df[columna_total], errors="coerce")
    cantidad = pd.to_numeric(df[columna_cantidad], errors="coerce")
    precio = pd.to_numeric(df[columna_precio], errors="coerce")
    esperado = cantidad * precio
    diferencia = (total - esperado).abs()
    limite = (esperado.abs() * tolerancia).clip(lower=0.01)
    mal = (diferencia > limite) & total.notna() & esperado.notna()

    for idx in df[mal].index:
        valor_correcto = esperado.loc[idx]
        hallazgos.append({"tipo": "formula_incorrecta", "columna": columna_total, "fila": int(idx),
                           "valor_original": df.loc[idx, columna_total],
                           "detalle": f"{columna_total} no coincide con {columna_cantidad} × {columna_precio} "
                                      f"(esperado ≈ {valor_correcto:.2f})",
                           "valor_sugerido": round(float(valor_correcto), 2) if pd.notna(valor_correcto) else None})
    return hallazgos


def _detectar_texto_inconsistente(df, columnas, auto, umbral_similitud, max_cardinalidad_ratio,
                                   min_apariciones_canonica=1):
    if columnas is not None:
        cols = columnas
    elif auto:
        cols = []
        for col in df.columns:
            serie = df[col]
            es_texto = pd.api.types.is_object_dtype(serie) or pd.api.types.is_string_dtype(serie)
            if not es_texto or _es_columna_id(col):
                continue
            if any(p in str(col).lower() for p in _PATRONES_EXCLUIR_TEXTO):
                continue
            no_nulos = serie.dropna()
            if len(no_nulos) == 0:
                continue
            ratio = no_nulos.map(_normalizar_texto).nunique() / len(no_nulos)
            if ratio <= max_cardinalidad_ratio:
                cols.append(col)
    else:
        cols = []

    hallazgos = []
    for col in cols:
        if col not in df.columns:
            continue
        serie = df[col].dropna()
        if serie.empty:
            continue

        conteo_por_texto = serie.astype(str).value_counts()
        textos_por_norm: dict = {}
        for texto, cuenta in conteo_por_texto.items():
            norm = _normalizar_texto(texto)
            textos_por_norm.setdefault(norm, []).append((texto, int(cuenta)))

        normalizados = list(textos_por_norm.keys())
        visitados = set()
        grupos = []
        for i, n1 in enumerate(normalizados):
            if n1 in visitados:
                continue
            grupo = [n1]
            visitados.add(n1)
            for n2 in normalizados[i + 1:]:
                if n2 in visitados:
                    continue
                if difflib.SequenceMatcher(None, n1, n2).ratio() >= umbral_similitud:
                    grupo.append(n2)
                    visitados.add(n2)
            grupos.append(grupo)

        for grupo in grupos:
            candidatas = [par for n in grupo for par in textos_por_norm[n]]
            if len(candidatas) < 2:
                continue
            canonica_texto, canonica_cuenta = max(candidatas, key=lambda t: t[1])
            if canonica_cuenta < min_apariciones_canonica:
                continue
            for texto_variante, _ in candidatas:
                if texto_variante == canonica_texto:
                    continue
                for idx, val in serie.items():
                    if str(val) == texto_variante:
                        hallazgos.append({"tipo": "texto_inconsistente", "columna": col, "fila": int(idx),
                                           "valor_original": val,
                                           "detalle": f"Posible variante/error de tipeo de '{canonica_texto}'",
                                           "valor_sugerido": canonica_texto})
    return hallazgos


# -----------------------------------------------------------------------------
# Función principal
# -----------------------------------------------------------------------------

def limpiar_tabla(df: pd.DataFrame,
                   faltante: str = "reemplazar_mediana",
                   duplicado: str = "eliminar_fila",
                   atipico: str = "limitar",
                   tipo_invalido: str = "marcar_solo",
                   factor_iqr: float = 1.5,
                   valores_fijos: dict = None,
                   # --- las 6 reglas nuevas (por defecto: solo marcar) ---
                   fecha_invalida: str = "marcar_solo",
                   email_invalido: str = "marcar_solo",
                   telefono_invalido: str = "marcar_solo",
                   id_duplicado: str = "marcar_solo",
                   formula_incorrecta: str = "marcar_solo",
                   texto_inconsistente: str = "marcar_solo",
                   # --- configuración fina de las reglas nuevas ---
                   auto_detectar_columnas: bool = True,
                   columnas_fecha: list = None, fecha_min=None, fecha_max=None,
                   columnas_email: list = None,
                   columnas_telefono: list = None, patron_telefono: str = None,
                   digitos_telefono: tuple = (8, 8),
                   columnas_id: list = None,
                   columna_total: str = None, columna_cantidad: str = None,
                   columna_precio: str = None, tolerancia_formula: float = 0.01,
                   columnas_texto: list = None, umbral_similitud_texto: float = 0.85,
                   max_cardinalidad_ratio_texto: float = 0.5):
    """
    Analiza y corrige la tabla en un solo paso, aplicando las 10 reglas
    (4 originales + 6 nuevas).

    Parámetros
    ----------
    df : pandas.DataFrame
        Tabla original a corregir. No se modifica in-place.
    faltante, duplicado, atipico, tipo_invalido, fecha_invalida,
    email_invalido, telefono_invalido, id_duplicado, formula_incorrecta,
    texto_inconsistente : str
        Acción a aplicar para cada tipo de problema. Debe ser una de:
        'eliminar_fila', 'reemplazar_media', 'reemplazar_mediana',
        'reemplazar_moda', 'limitar', 'marcar_solo', 'valor_fijo',
        'usar_sugerido'.
        ('limitar' solo tiene efecto sobre 'atipico'; 'reemplazar_media/
        mediana/moda' solo sobre 'faltante'/'tipo_invalido'/'atipico';
        'usar_sugerido' solo sobre 'formula_incorrecta'/'texto_inconsistente',
        cuando el detector pudo calcular un valor correcto/canónico;
        'valor_fijo' reemplaza por valores_fijos[columna] en cualquier tipo).
    factor_iqr : float
        Factor multiplicador del rango intercuartílico para atípicos (1.5).
    valores_fijos : dict {columna: valor}
        Valores a usar cuando la acción de una columna es 'valor_fijo'.
    auto_detectar_columnas : bool
        Si True (por defecto), las columnas de cada regla nueva se
        adivinan por el nombre cuando no se indican explícitamente.
    columnas_fecha, fecha_min, fecha_max :
        Columnas de fecha a validar y rango aceptado (opcional).
    columnas_email, columnas_telefono, patron_telefono, digitos_telefono :
        Columnas a validar y, para teléfono, un patrón regex propio y el
        rango de cantidad de dígitos esperado (por defecto 8, formato CR).
    columnas_id :
        Columnas identificadoras en las que un valor repetido (sin que la
        fila completa sea idéntica) se marca como 'id_duplicado'.
    columna_total, columna_cantidad, columna_precio, tolerancia_formula :
        Columnas para validar columna_total ≈ columna_cantidad × columna_precio
        (tolerancia relativa, 1% por defecto, con piso absoluto de 0.01).
    columnas_texto, umbral_similitud_texto, max_cardinalidad_ratio_texto :
        Columnas categóricas a revisar por variantes/errores de tipeo, el
        umbral de similitud (0-1, difflib) y el ratio máximo de
        cardinalidad para que una columna se auto-detecte como categórica.

    Devuelve
    --------
    (df_limpio, df_reporte) : tuple[pandas.DataFrame, pandas.DataFrame]
        df_limpio: la tabla corregida (con columna '_revisar_calidad'
        si quedaron hallazgos sin modificar).
        df_reporte: un registro (una fila por hallazgo) de qué se detectó
        y qué acción se aplicó, para auditoría.
    """
    config = {
        "faltante": faltante, "duplicado": duplicado, "atipico": atipico, "tipo_invalido": tipo_invalido,
        "fecha_invalida": fecha_invalida, "email_invalido": email_invalido,
        "telefono_invalido": telefono_invalido, "id_duplicado": id_duplicado,
        "formula_incorrecta": formula_incorrecta, "texto_inconsistente": texto_inconsistente,
    }
    for tipo, accion in config.items():
        if accion not in ACCIONES_VALIDAS:
            raise ValueError(f"Acción no válida para '{tipo}': '{accion}'. Use una de: {sorted(ACCIONES_VALIDAS)}")

    valores_fijos = valores_fijos or {}
    df_limpio = df.copy()

    hallazgos = _detectar_hallazgos_originales(df, factor_iqr=factor_iqr)
    hallazgos += _detectar_fechas_invalidas(df, columnas_fecha, fecha_min, fecha_max, auto_detectar_columnas)
    hallazgos += _detectar_emails_invalidos(df, columnas_email, auto_detectar_columnas)
    hallazgos += _detectar_telefonos_invalidos(df, columnas_telefono, patron_telefono,
                                                digitos_telefono[0], digitos_telefono[1], auto_detectar_columnas)
    hallazgos += _detectar_ids_duplicados(df, columnas_id, auto_detectar_columnas)
    hallazgos += _detectar_formula_incorrecta(df, columna_total, columna_cantidad, columna_precio,
                                               tolerancia_formula, auto_detectar_columnas)
    hallazgos += _detectar_texto_inconsistente(df, columnas_texto, auto_detectar_columnas,
                                                umbral_similitud_texto, max_cardinalidad_ratio_texto)

    limites_iqr = {}
    for h in hallazgos:
        if h["tipo"] == "atipico" and h["columna"] not in limites_iqr:
            serie = pd.to_numeric(df[h["columna"]], errors="coerce")
            q1, q3 = serie.quantile(0.25), serie.quantile(0.75)
            iqr = q3 - q1
            limites_iqr[h["columna"]] = (q1 - factor_iqr * iqr, q3 + factor_iqr * iqr)

    registro = []
    filas_a_eliminar = set()

    for h in hallazgos:
        accion = config.get(h["tipo"], "marcar_solo")
        valor_nuevo = h["valor_original"]

        if accion == "marcar_solo":
            pass

        elif accion == "eliminar_fila":
            filas_a_eliminar.add(h["fila"])
            valor_nuevo = "(fila eliminada)"

        elif h["tipo"] in ("faltante", "tipo_invalido") and accion in (
                "reemplazar_media", "reemplazar_mediana", "reemplazar_moda", "valor_fijo"):
            valor_nuevo = _valor_reemplazo(df, h["columna"], accion, valores_fijos.get(h["columna"]))
            _asignar(df_limpio, h["fila"], h["columna"], valor_nuevo)

        elif h["tipo"] == "atipico" and accion == "limitar":
            lim_inf, lim_sup = limites_iqr.get(h["columna"], (None, None))
            val_num = pd.to_numeric(pd.Series([h["valor_original"]]), errors="coerce")[0]
            if lim_inf is not None and not pd.isna(val_num):
                valor_nuevo = lim_inf if val_num < lim_inf else lim_sup
                _asignar(df_limpio, h["fila"], h["columna"], valor_nuevo)

        elif h["tipo"] == "atipico" and accion in (
                "reemplazar_media", "reemplazar_mediana", "reemplazar_moda"):
            valor_nuevo = _valor_reemplazo(df, h["columna"], accion)
            _asignar(df_limpio, h["fila"], h["columna"], valor_nuevo)

        elif h["tipo"] in _TIPOS_CON_SUGERENCIA and accion == "usar_sugerido" \
                and h.get("valor_sugerido") is not None:
            valor_nuevo = h["valor_sugerido"]
            _asignar(df_limpio, h["fila"], h["columna"], valor_nuevo)

        elif h["tipo"] in _TIPOS_VALOR_FIJO_DIRECTO and accion == "valor_fijo":
            valor_nuevo = valores_fijos.get(h["columna"])
            _asignar(df_limpio, h["fila"], h["columna"], valor_nuevo)

        registro.append({
            "tipo": h["tipo"],
            "columna": h["columna"] or "(fila completa)",
            "fila": h["fila"],
            "valor_original": str(h["valor_original"]),
            "accion_aplicada": accion,
            "valor_nuevo": str(valor_nuevo),
            "detalle": h["detalle"],
        })

    # Señalar en la tabla las filas con hallazgos que quedaron sin modificar
    marcas: dict = {}
    for r in registro:
        if r["accion_aplicada"] != "marcar_solo":
            continue
        etiqueta = r["tipo"] if r["columna"] == "(fila completa)" else f"{r['tipo']}:{r['columna']}"
        marcas.setdefault(r["fila"], []).append(etiqueta)

    if marcas:
        col_marca = "_revisar_calidad"
        while col_marca in df_limpio.columns:
            col_marca += "_"
        df_limpio[col_marca] = ""
        for fila, etiquetas in marcas.items():
            if fila in df_limpio.index:
                df_limpio.at[fila, col_marca] = "; ".join(etiquetas)

    if filas_a_eliminar:
        df_limpio = df_limpio.drop(index=list(filas_a_eliminar), errors="ignore").reset_index(drop=True)

    df_reporte = pd.DataFrame(registro) if registro else pd.DataFrame(
        columns=["tipo", "columna", "fila", "valor_original", "accion_aplicada", "valor_nuevo", "detalle"]
    )

    return df_limpio, df_reporte


def _main_cli():
    parser = argparse.ArgumentParser(
        description="Limpia un archivo CSV/Excel y genera la tabla corregida más un reporte de auditoría "
                     "(10 reglas: faltante, duplicado, atipico, tipo_invalido, fecha_invalida, "
                     "email_invalido, telefono_invalido, id_duplicado, formula_incorrecta, texto_inconsistente)."
    )
    parser.add_argument("archivo", help="Ruta al archivo de entrada (.csv, .xlsx)")
    parser.add_argument("--faltante", default="reemplazar_mediana", choices=sorted(ACCIONES_VALIDAS))
    parser.add_argument("--duplicado", default="eliminar_fila", choices=sorted(ACCIONES_VALIDAS))
    parser.add_argument("--atipico", default="limitar", choices=sorted(ACCIONES_VALIDAS))
    parser.add_argument("--tipo_invalido", default="marcar_solo", choices=sorted(ACCIONES_VALIDAS))
    parser.add_argument("--fecha_invalida", default="marcar_solo", choices=sorted(ACCIONES_VALIDAS))
    parser.add_argument("--email_invalido", default="marcar_solo", choices=sorted(ACCIONES_VALIDAS))
    parser.add_argument("--telefono_invalido", default="marcar_solo", choices=sorted(ACCIONES_VALIDAS))
    parser.add_argument("--id_duplicado", default="marcar_solo", choices=sorted(ACCIONES_VALIDAS))
    parser.add_argument("--formula_incorrecta", default="marcar_solo", choices=sorted(ACCIONES_VALIDAS))
    parser.add_argument("--texto_inconsistente", default="marcar_solo", choices=sorted(ACCIONES_VALIDAS))
    parser.add_argument("--factor_iqr", type=float, default=1.5)
    parser.add_argument("--salida_dir", default=None, help="Carpeta de salida (por defecto, la misma del archivo)")
    args = parser.parse_args()

    if args.archivo.lower().endswith((".xlsx", ".xls", ".xlsm")):
        df = pd.read_excel(args.archivo)
    else:
        df = pd.read_csv(args.archivo)

    df_limpio, df_reporte = limpiar_tabla(
        df,
        faltante=args.faltante,
        duplicado=args.duplicado,
        atipico=args.atipico,
        tipo_invalido=args.tipo_invalido,
        fecha_invalida=args.fecha_invalida,
        email_invalido=args.email_invalido,
        telefono_invalido=args.telefono_invalido,
        id_duplicado=args.id_duplicado,
        formula_incorrecta=args.formula_incorrecta,
        texto_inconsistente=args.texto_inconsistente,
        factor_iqr=args.factor_iqr,
    )

    base, _ext = os.path.splitext(os.path.basename(args.archivo))
    carpeta = args.salida_dir or os.path.dirname(os.path.abspath(args.archivo))
    ruta_limpio = os.path.join(carpeta, f"{base}_limpio.csv")
    ruta_reporte = os.path.join(carpeta, f"{base}_reporte_limpieza.csv")

    df_limpio.to_csv(ruta_limpio, index=False)
    df_reporte.to_csv(ruta_reporte, index=False)

    print(f"Filas originales: {len(df)} | Filas finales: {len(df_limpio)}")
    print(f"Hallazgos detectados: {len(df_reporte)}")
    print(f"Tabla limpia guardada en:  {ruta_limpio}")
    print(f"Reporte guardado en:       {ruta_reporte}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _main_cli()
    else:
        print(__doc__)

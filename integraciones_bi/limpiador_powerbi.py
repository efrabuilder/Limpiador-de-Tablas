# -*- coding: utf-8 -*-
# =============================================================================
# limpiador_powerbi.py
# -----------------------------------------------------------------------------
# CÓMO USAR ESTE SCRIPT EN POWER BI:
#
#   1. En Power Query (Editor de Power Query), seleccioná la tabla a corregir.
#   2. Menú "Transformar" -> "Ejecutar script de Python".
#   3. Pegá TODO el contenido de este archivo en el cuadro de diálogo y
#      aceptá.
#   4. Power BI ejecuta el script y agrega un paso nuevo. En el panel de
#      resultados vas a ver dos tablas disponibles: "dataset_limpio" (la
#      tabla corregida) y "reporte_limpieza" (el detalle de qué se cambió).
#      Elegí la que necesites y hacé clic en la flechita para expandirla.
#   5. Ese paso queda registrado en el Editor avanzado como una llamada
#      Python.Execute(...) — ver el archivo "editor_avanzado_powerbi.m"
#      adjunto para el fragmento M exacto que Power BI genera/necesita.
#
# REQUISITOS: en Configuración de opciones -> Python scripting, Power BI
# Desktop debe tener configurada una instalación local de Python que tenga
# instalados pandas y numpy (pip install pandas numpy).
#
# IMPORTANTE: Power BI inyecta automáticamente la tabla de entrada en la
# variable "dataset" (un DataFrame de pandas). Este script NO debe importar
# el paquete "data_cleaner" porque ese entorno de Python normalmente no lo
# tiene instalado: por eso toda la lógica está reescrita aquí mismo, sin
# dependencias externas más que pandas y numpy.
#
# 10 REGLAS INCLUIDAS (paridad completa con la librería Python `limpiar()`):
#   Las 4 originales: faltante, duplicado, atipico, tipo_invalido.
#   Las 6 nuevas: fecha_invalida, email_invalido, telefono_invalido,
#   id_duplicado, formula_incorrecta, texto_inconsistente.
#   Las columnas de cada regla nueva se auto-detectan por nombre (fecha,
#   email/correo, teléfono/celular, id/código/folio, total/cantidad/precio,
#   texto de baja cardinalidad); si tu tabla usa otros nombres, indicalos
#   explícitamente en las listas COLUMNAS_* de la sección de configuración.
# =============================================================================

import re
import difflib
import unicodedata
import pandas as pd
import numpy as np

# -----------------------------------------------------------------------------
# CONFIGURACIÓN: ajustá según cómo querés corregir cada tipo de problema.
# Opciones válidas: 'eliminar_fila', 'reemplazar_media', 'reemplazar_mediana',
# 'reemplazar_moda', 'limitar', 'marcar_solo', 'valor_fijo', 'usar_sugerido'.
# ('limitar' solo aplica a ACCION_ATIPICO; 'reemplazar_media/mediana/moda'
# solo a faltante/tipo_invalido/atipico; 'usar_sugerido' solo a
# ACCION_FORMULA_INCORRECTA / ACCION_TEXTO_INCONSISTENTE, cuando el
# detector pudo calcular un valor correcto/canónico).
# -----------------------------------------------------------------------------
ACCION_FALTANTE = "reemplazar_mediana"
ACCION_DUPLICADO = "eliminar_fila"
ACCION_ATIPICO = "limitar"
ACCION_TIPO_INVALIDO = "marcar_solo"
FACTOR_IQR = 1.5
VALORES_FIJOS = {}   # ej: {"columna_x": 0} si alguna acción es 'valor_fijo'

# Las 6 reglas nuevas quedan en 'marcar_solo' por defecto: corregirlas
# automáticamente es riesgoso (un email o teléfono "corregido" a ciegas
# puede quedar mal). Cambiá cualquiera de estas 6 líneas si querés que se
# corrijan solas (por ejemplo a 'valor_fijo', 'eliminar_fila', o
# 'usar_sugerido' para las dos que traen un valor sugerido calculado).
ACCION_FECHA_INVALIDA = "marcar_solo"
ACCION_EMAIL_INVALIDO = "marcar_solo"
ACCION_TELEFONO_INVALIDO = "marcar_solo"
ACCION_ID_DUPLICADO = "marcar_solo"
ACCION_FORMULA_INCORRECTA = "marcar_solo"
ACCION_TEXTO_INCONSISTENTE = "marcar_solo"

# Configuración fina de las 6 reglas nuevas. Dejá en None / True para
# auto-detectar columnas por nombre; completá explícitamente si tu tabla
# usa nombres de columna distintos a los patrones habituales.
AUTO_DETECTAR_COLUMNAS = True
COLUMNAS_FECHA = None          # ej: ["fecha_venta"]
FECHA_MIN = None               # ej: "2023-01-01"
FECHA_MAX = None               # ej: "2026-12-31"
COLUMNAS_EMAIL = None          # ej: ["correo_cliente"]
COLUMNAS_TELEFONO = None       # ej: ["telefono_cliente"]
PATRON_TELEFONO = None         # regex propio si el formato no es CR estándar
DIGITOS_TELEFONO = (8, 8)      # (min, max) dígitos esperados
COLUMNAS_ID = None             # ej: ["id_producto"]
COLUMNA_TOTAL = None           # ej: "total"
COLUMNA_CANTIDAD = None        # ej: "cantidad"
COLUMNA_PRECIO = None          # ej: "precio_unitario"
TOLERANCIA_FORMULA = 0.01      # 1% relativo, con piso absoluto de 0.01
COLUMNAS_TEXTO = None          # ej: ["categoria", "metodo_pago"]
UMBRAL_SIMILITUD_TEXTO = 0.85
MAX_CARDINALIDAD_RATIO_TEXTO = 0.5

ACCIONES_VALIDAS = {
    "eliminar_fila", "reemplazar_media", "reemplazar_mediana",
    "reemplazar_moda", "limitar", "marcar_solo", "valor_fijo", "usar_sugerido",
}
_TIPOS_VALOR_FIJO_DIRECTO = {
    "fecha_invalida", "email_invalido", "telefono_invalido",
    "id_duplicado", "formula_incorrecta", "texto_inconsistente",
}
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


def _columna_numerica_potencial(serie):
    valores = serie.dropna()
    if len(valores) == 0:
        return False
    convertibles = pd.to_numeric(valores, errors="coerce")
    return convertibles.notna().mean() > 0.7


def _columnas_para_atipicos(df):
    columnas = df.select_dtypes(include=[np.number]).columns.tolist()
    for col in df.columns:
        if col in columnas:
            continue
        serie = df[col]
        if (pd.api.types.is_object_dtype(serie) or pd.api.types.is_string_dtype(serie)) \
                and _columna_numerica_potencial(serie):
            columnas.append(col)
    return columnas


def _columnas_por_patron(df, patrones):
    return [col for col in df.columns if any(p in str(col).lower() for p in patrones)]


def _es_columna_id(col):
    low = str(col).lower()
    if low == "id":
        return True
    if low.startswith("id_") or low.endswith("_id") or "_id_" in low:
        return True
    if any(p in low for p in ("codigo", "código", "folio")):
        return True
    return False


def _normalizar_texto(valor):
    s = str(valor).strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return " ".join(s.split())


def _detectar_hallazgos_originales(df, factor_iqr=1.5):
    hallazgos = []

    for col in df.columns:
        for idx in df[df[col].isna()].index:
            hallazgos.append({"tipo": "faltante", "columna": col, "fila": int(idx),
                               "valor_original": None, "detalle": "Valor vacío/nulo",
                               "valor_sugerido": None})

    mask_dup = df.duplicated(keep="first")
    for idx in df[mask_dup].index:
        hallazgos.append({"tipo": "duplicado", "columna": None, "fila": int(idx),
                           "valor_original": df.loc[idx].to_dict(),
                           "detalle": "Fila duplicada (idéntica a una anterior)",
                           "valor_sugerido": None})

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
        textos_por_norm = {}
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


def _asignar(df, fila, columna, valor):
    try:
        df.at[fila, columna] = valor
    except (TypeError, ValueError):
        df[columna] = df[columna].astype(object)
        df.at[fila, columna] = valor


def _valor_reemplazo(df, columna, accion, valor_fijo=None):
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


def limpiar_tabla(df, faltante, duplicado, atipico, tipo_invalido, factor_iqr, valores_fijos,
                   fecha_invalida, email_invalido, telefono_invalido, id_duplicado,
                   formula_incorrecta, texto_inconsistente,
                   auto_detectar_columnas, columnas_fecha, fecha_min, fecha_max,
                   columnas_email, columnas_telefono, patron_telefono, digitos_telefono,
                   columnas_id, columna_total, columna_cantidad, columna_precio, tolerancia_formula,
                   columnas_texto, umbral_similitud_texto, max_cardinalidad_ratio_texto):
    config = {
        "faltante": faltante, "duplicado": duplicado, "atipico": atipico, "tipo_invalido": tipo_invalido,
        "fecha_invalida": fecha_invalida, "email_invalido": email_invalido,
        "telefono_invalido": telefono_invalido, "id_duplicado": id_duplicado,
        "formula_incorrecta": formula_incorrecta, "texto_inconsistente": texto_inconsistente,
    }
    for tipo, accion in config.items():
        if accion not in ACCIONES_VALIDAS:
            raise ValueError(f"Acción no válida para '{tipo}': '{accion}'. Use una de: {sorted(ACCIONES_VALIDAS)}")

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

    marcas = {}
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


# -----------------------------------------------------------------------------
# PUNTO DE ENTRADA que Power BI ejecuta automáticamente.
# "dataset" ya viene definido por Power Query con la tabla seleccionada.
# Las variables de salida que terminan siendo pandas DataFrame son las que
# Power BI ofrece como tablas para expandir en el panel de resultados.
# -----------------------------------------------------------------------------
dataset_limpio, reporte_limpieza = limpiar_tabla(
    dataset,
    faltante=ACCION_FALTANTE,
    duplicado=ACCION_DUPLICADO,
    atipico=ACCION_ATIPICO,
    tipo_invalido=ACCION_TIPO_INVALIDO,
    factor_iqr=FACTOR_IQR,
    valores_fijos=VALORES_FIJOS,
    fecha_invalida=ACCION_FECHA_INVALIDA,
    email_invalido=ACCION_EMAIL_INVALIDO,
    telefono_invalido=ACCION_TELEFONO_INVALIDO,
    id_duplicado=ACCION_ID_DUPLICADO,
    formula_incorrecta=ACCION_FORMULA_INCORRECTA,
    texto_inconsistente=ACCION_TEXTO_INCONSISTENTE,
    auto_detectar_columnas=AUTO_DETECTAR_COLUMNAS,
    columnas_fecha=COLUMNAS_FECHA, fecha_min=FECHA_MIN, fecha_max=FECHA_MAX,
    columnas_email=COLUMNAS_EMAIL,
    columnas_telefono=COLUMNAS_TELEFONO, patron_telefono=PATRON_TELEFONO,
    digitos_telefono=DIGITOS_TELEFONO,
    columnas_id=COLUMNAS_ID,
    columna_total=COLUMNA_TOTAL, columna_cantidad=COLUMNA_CANTIDAD, columna_precio=COLUMNA_PRECIO,
    tolerancia_formula=TOLERANCIA_FORMULA,
    columnas_texto=COLUMNAS_TEXTO, umbral_similitud_texto=UMBRAL_SIMILITUD_TEXTO,
    max_cardinalidad_ratio_texto=MAX_CARDINALIDAD_RATIO_TEXTO,
)

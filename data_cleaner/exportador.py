# -*- coding: utf-8 -*-
"""
exportador.py
=============
Genera scripts Python autocontenidos (sin depender del paquete
``data_cleaner``) y código M para Power BI, reproduciendo EXACTAMENTE la
configuración de limpieza (acción por tipo de hallazgo, factor IQR y
valores fijos) que el usuario eligió en la interfaz (web, escritorio o
API).

Los scripts generados solo requieren pandas, numpy y la librería estándar
de Python (re, difflib, unicodedata), porque los entornos externos
(Power BI, Tableau Prep/TabPy, Alteryx, Qlik) normalmente no tienen
instalado ``data_cleaner``.

Nota: por ahora solo soportan detección de atípicos por método IQR (no
Z-score), que es el único método implementado de forma autocontenida.
"""
from __future__ import annotations
import re
from typing import Dict, Optional

DEFAULT_CONFIG_EXPORT = {
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

# -----------------------------------------------------------------------------
# Núcleo de lógica compartido entre el script para Power BI y el universal.
# Es una copia autocontenida (solo pandas/numpy) de data_cleaner/cleaner.py.
# -----------------------------------------------------------------------------
_NUCLEO_LOGICA = '''# --- Deteccion de columnas: por nombre (normalizado) y, si eso no
# encuentra nada, por contenido real. Mismo enfoque que data_cleaner/patrones.py,
# copiado aqui en forma autocontenida porque este script corre fuera del
# proyecto (Power BI, Tableau, Alteryx, etc.) y no puede importarlo. -----
_PATRONES_EMAIL = ("email", "correo", "e_mail", "mail", "correo_electronico")
_PATRONES_TELEFONO = ("telefono", "phone", "celular", "movil", "whatsapp",
                       "numero_telefono", "phone_number", "tel", "mobile", "fax")
_PATRONES_FECHA = ("fecha", "date", "fec", "dob", "birth", "nacimiento",
                    "created_at", "updated_at", "timestamp", "vencimiento",
                    "expiry", "ingreso", "egreso")
_PATRONES_TOTAL = ("total", "monto", "importe", "amount", "subtotal",
                    "salario", "sueldo", "costo", "cost")
_PATRONES_CANTIDAD = ("cantidad", "qty", "quantity", "cant", "unidades",
                       "horas", "hours", "peso", "weight")
_PATRONES_PRECIO = ("precio", "price", "tarifa", "rate", "valor_unitario", "unit_price")
_PATRONES_EXCLUIR_TEXTO = _PATRONES_EMAIL + _PATRONES_TELEFONO + _PATRONES_FECHA + \\
    ("nombre", "cliente", "direccion", "dirección", "observacion", "observación", "comentario")
_REGEX_EMAIL = re.compile(r"^[^@\\s]+@[^@\\s]+\\.[^@\\s]{2,}$")
_REGEX_TELEFONO_FORMATO = re.compile(r"^[\\d\\s\\-\\+\\(\\)]+$")


def _normalizar_nombre_columna(col):
    s = str(col).strip()
    s = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', s)
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = s.lower()
    return re.sub(r'[^a-z0-9]+', '_', s).strip('_')


def _coincide_patron_columna(col, patrones):
    norm = _normalizar_nombre_columna(col)
    tokens = [t for t in norm.split('_') if t]
    for p in patrones:
        p_norm = _normalizar_nombre_columna(p)
        if not p_norm:
            continue
        if '_' in p_norm:
            if p_norm in norm:
                return True
            continue
        for tok in tokens:
            if tok == p_norm:
                return True
            if re.fullmatch(re.escape(p_norm) + r'\\d+', tok) or re.fullmatch(r'\\d+' + re.escape(p_norm), tok):
                return True
    return False


def _columnas_por_patron(df, patrones):
    return [col for col in df.columns if _coincide_patron_columna(col, patrones)]


def _es_columna_id(col):
    norm = _normalizar_nombre_columna(col)
    tokens = norm.split('_')
    if norm == "id" or "id" in tokens:
        return True
    if any(p in norm for p in ("codigo", "folio", "identificador", "clave")):
        return True
    return False


def _muestra_serie(serie, n=200):
    no_nulos = serie.dropna()
    if len(no_nulos) == 0:
        return no_nulos
    return no_nulos.sample(min(n, len(no_nulos)), random_state=0) if len(no_nulos) > n else no_nulos


def _parece_email_col(serie, umbral=0.6):
    m = _muestra_serie(serie).astype(str)
    if len(m) == 0:
        return False
    return (m.str.match(_REGEX_EMAIL)).mean() >= umbral


def _parece_telefono_col(serie, umbral=0.7, min_d=7, max_d=15):
    m = _muestra_serie(serie).astype(str)
    if len(m) == 0:
        return False
    con_anio = m.str.contains(r'\\b(?:19|20)\\d{2}\\b', regex=True)
    m = m[~con_anio]
    if len(m) == 0:
        return False
    solo_digitos = m.str.replace(r"\\D", "", regex=True)
    return solo_digitos.str.len().between(min_d, max_d).mean() >= umbral


def _parece_fecha_col(serie, umbral=0.7):
    m = _muestra_serie(serie)
    if len(m) == 0:
        return False
    if pd.api.types.is_datetime64_any_dtype(serie):
        return True
    parseado = pd.to_datetime(m.astype(str), errors="coerce", dayfirst=True)
    return parseado.notna().mean() >= umbral


def _detectar_columnas_combinado(df, patrones, detector_contenido):
    """Nivel 1 (nombre normalizado) primero; si no encuentra nada, Nivel 2
    revisa el contenido real de las columnas de texto no-ID como respaldo
    (cubre datasets con columnas mal nombradas: "col_1", "campo_7", etc.)."""
    por_nombre = _columnas_por_patron(df, patrones)
    if por_nombre:
        return por_nombre
    candidatas = []
    for col in df.columns:
        if _es_columna_id(col):
            continue
        try:
            if detector_contenido(df[col]):
                candidatas.append(col)
        except Exception:
            continue
    return candidatas


def _columna_numerica_potencial(serie):
    valores = serie.dropna()
    if len(valores) == 0:
        return False
    convertibles = pd.to_numeric(valores, errors="coerce")
    return convertibles.notna().mean() > 0.7


def _columnas_excluir_de_atipicos(df):
    """Columnas que no deben pasar por IQR: identificadores y telefono/fax
    (no son magnitudes continuas, no existe una "distribucion normal"
    esperada para ellas -- ver la version completa en data_cleaner/patrones.py)."""
    cols_id = [col for col in df.columns if _es_columna_id(col)]
    cols_tel = _detectar_columnas_combinado(df, _PATRONES_TELEFONO, _parece_telefono_col)
    return set(cols_id) | set(cols_tel)


def _columnas_para_atipicos(df):
    excluidas = _columnas_excluir_de_atipicos(df)
    columnas = [c for c in df.select_dtypes(include=[np.number]).columns if c not in excluidas]
    for col in df.columns:
        if col in columnas or col in excluidas:
            continue
        serie = df[col]
        es_texto = pd.api.types.is_object_dtype(serie) or pd.api.types.is_string_dtype(serie)
        if es_texto and _columna_numerica_potencial(serie):
            columnas.append(col)
    return columnas


def _es_numero_con_sufijo(serie):
    no_nulos = serie.dropna().astype(str)
    if len(no_nulos) == 0:
        return False
    con_numero_inicial = no_nulos.str.match(r"^\\s*-?\\d+([.,]\\d+)?\\b")
    return con_numero_inicial.mean() > 0.7


def _normalizar_texto(valor):
    s = str(valor).strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return " ".join(s.split())


def _detectar_fechas_invalidas(df):
    hallazgos = []
    for col in _detectar_columnas_combinado(df, _PATRONES_FECHA, _parece_fecha_col):
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
                                   "valor_original": val, "detalle": "Formato de fecha no reconocido"})
    return hallazgos


def _detectar_emails_invalidos(df):
    hallazgos = []
    for col in _detectar_columnas_combinado(df, _PATRONES_EMAIL, _parece_email_col):
        for idx, val in df[col].items():
            if pd.isna(val):
                continue
            if not _REGEX_EMAIL.match(str(val).strip()):
                hallazgos.append({"tipo": "email_invalido", "columna": col, "fila": int(idx),
                                   "valor_original": val, "detalle": "Formato de correo electronico invalido"})
    return hallazgos


def _detectar_telefonos_invalidos(df, min_digitos=8, max_digitos=8):
    hallazgos = []
    for col in _detectar_columnas_combinado(df, _PATRONES_TELEFONO, _parece_telefono_col):
        for idx, val in df[col].items():
            if pd.isna(val):
                continue
            texto = str(val).strip()
            solo_digitos = re.sub(r"\\D", "", texto)
            formato_ok = bool(_REGEX_TELEFONO_FORMATO.match(texto))
            longitud_ok = min_digitos <= len(solo_digitos) <= max_digitos
            if not (formato_ok and longitud_ok):
                hallazgos.append({"tipo": "telefono_invalido", "columna": col, "fila": int(idx),
                                   "valor_original": val,
                                   "detalle": "Formato/longitud de telefono invalido"})
    return hallazgos


def _detectar_ids_duplicados(df, umbral_unicidad=0.9):
    hallazgos = []
    for col in df.columns:
        if not _es_columna_id(col):
            continue
        serie_no_nula = df[col].dropna()
        if len(serie_no_nula) == 0:
            continue
        if serie_no_nula.nunique() / len(serie_no_nula) < umbral_unicidad:
            continue
        serie = df[col]
        mask = serie.duplicated(keep="first") & serie.notna()
        for idx in serie[mask].index:
            hallazgos.append({"tipo": "id_duplicado", "columna": col, "fila": int(idx),
                               "valor_original": serie.loc[idx],
                               "detalle": f"Valor repetido en columna identificadora '{col}'"})
    return hallazgos


def _detectar_formula_incorrecta(df, tolerancia=0.01):
    hallazgos = []
    cand_total = _columnas_por_patron(df, _PATRONES_TOTAL)
    cand_cant = _columnas_por_patron(df, _PATRONES_CANTIDAD)
    cand_precio = _columnas_por_patron(df, _PATRONES_PRECIO)
    if not (cand_total and cand_cant and cand_precio):
        return hallazgos
    columna_total, columna_cantidad, columna_precio = cand_total[0], cand_cant[0], cand_precio[0]

    total = pd.to_numeric(df[columna_total], errors="coerce")
    cantidad = pd.to_numeric(df[columna_cantidad], errors="coerce")
    precio = pd.to_numeric(df[columna_precio], errors="coerce")
    esperado = cantidad * precio
    diferencia = (total - esperado).abs()
    limite = (esperado.abs() * tolerancia).clip(lower=0.01)
    mal = (diferencia > limite) & total.notna() & esperado.notna()

    for idx in df[mal].index:
        valor_correcto = esperado.loc[idx]
        hallazgos.append({
            "tipo": "formula_incorrecta", "columna": columna_total, "fila": int(idx),
            "valor_original": df.loc[idx, columna_total],
            "detalle": f"{columna_total} no coincide con {columna_cantidad} x {columna_precio}",
            "valor_sugerido": round(float(valor_correcto), 2) if pd.notna(valor_correcto) else None,
        })
    return hallazgos


def _detectar_texto_inconsistente(df, umbral_similitud=0.85, max_cardinalidad_ratio=0.5,
                                   min_apariciones_canonica=1):
    cols = []
    for col in df.columns:
        serie = df[col]
        es_texto = pd.api.types.is_object_dtype(serie) or pd.api.types.is_string_dtype(serie)
        if not es_texto or _es_columna_id(col):
            continue
        if any(p in str(col).lower() for p in _PATRONES_EXCLUIR_TEXTO):
            continue
        no_nulos = serie.dropna()
        if _columna_numerica_potencial(no_nulos) or _es_numero_con_sufijo(no_nulos):
            continue
        if len(no_nulos) == 0:
            continue
        ratio = no_nulos.map(_normalizar_texto).nunique() / len(no_nulos)
        if ratio <= max_cardinalidad_ratio:
            cols.append(col)

    hallazgos = []
    for col in cols:
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
                        hallazgos.append({
                            "tipo": "texto_inconsistente", "columna": col, "fila": int(idx),
                            "valor_original": val,
                            "detalle": f"Posible variante/error de tipeo de '{canonica_texto}'",
                            "valor_sugerido": canonica_texto,
                        })
    return hallazgos


def _detectar_hallazgos(df, factor_iqr=1.5):
    hallazgos = []

    for col in df.columns:
        for idx in df[df[col].isna()].index:
            hallazgos.append({"tipo": "faltante", "columna": col, "fila": int(idx),
                               "valor_original": None, "detalle": "Valor vacio/nulo"})

    mask_dup = df.duplicated(keep="first")
    for idx in df[mask_dup].index:
        hallazgos.append({"tipo": "duplicado", "columna": None, "fila": int(idx),
                           "valor_original": df.loc[idx].to_dict(),
                           "detalle": "Fila duplicada (identica a una anterior)"})

    for col in df.columns:
        serie = df[col]
        es_texto = pd.api.types.is_object_dtype(serie) or pd.api.types.is_string_dtype(serie)
        if es_texto and _columna_numerica_potencial(serie):
            convertidos = pd.to_numeric(serie, errors="coerce")
            malos = serie[(convertidos.isna()) & (serie.notna())]
            for idx, val in malos.items():
                hallazgos.append({"tipo": "tipo_invalido", "columna": col, "fila": int(idx),
                                   "valor_original": val, "detalle": "Se esperaba un valor numerico"})

    hallazgos += _detectar_fechas_invalidas(df)
    hallazgos += _detectar_emails_invalidos(df)
    hallazgos += _detectar_telefonos_invalidos(df)
    hallazgos += _detectar_ids_duplicados(df)
    hallazgos += _detectar_formula_incorrecta(df)
    hallazgos += _detectar_texto_inconsistente(df)

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
                               "detalle": f"Fuera de rango [{lim_inf:.2f}, {lim_sup:.2f}] (IQR)"})

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


_TIPOS_VALOR_FIJO_DIRECTO = {
    "fecha_invalida", "email_invalido", "telefono_invalido",
    "id_duplicado", "formula_incorrecta", "texto_inconsistente",
}
_TIPOS_CON_SUGERENCIA = {"formula_incorrecta", "texto_inconsistente"}


def limpiar_tabla(df, faltante, duplicado, atipico, tipo_invalido, factor_iqr, valores_fijos,
                   fecha_invalida="marcar_solo", email_invalido="marcar_solo",
                   telefono_invalido="marcar_solo", id_duplicado="marcar_solo",
                   formula_incorrecta="marcar_solo", texto_inconsistente="marcar_solo"):
    config = {"faltante": faltante, "duplicado": duplicado,
              "atipico": atipico, "tipo_invalido": tipo_invalido,
              "fecha_invalida": fecha_invalida, "email_invalido": email_invalido,
              "telefono_invalido": telefono_invalido, "id_duplicado": id_duplicado,
              "formula_incorrecta": formula_incorrecta, "texto_inconsistente": texto_inconsistente}

    df_limpio = df.copy()
    hallazgos = _detectar_hallazgos(df, factor_iqr=factor_iqr)

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

        elif h["tipo"] in _TIPOS_CON_SUGERENCIA and accion == "usar_sugerido" \\
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
'''


def _bloque_config(config: Dict[str, str], factor_iqr: float, valores_fijos: Optional[dict]) -> str:
    cfg = {**DEFAULT_CONFIG_EXPORT, **(config or {})}
    valores_fijos = valores_fijos or {}
    return (
        f"ACCION_FALTANTE = {cfg['faltante']!r}\n"
        f"ACCION_DUPLICADO = {cfg['duplicado']!r}\n"
        f"ACCION_ATIPICO = {cfg['atipico']!r}\n"
        f"ACCION_TIPO_INVALIDO = {cfg['tipo_invalido']!r}\n"
        f"ACCION_FECHA_INVALIDA = {cfg['fecha_invalida']!r}\n"
        f"ACCION_EMAIL_INVALIDO = {cfg['email_invalido']!r}\n"
        f"ACCION_TELEFONO_INVALIDO = {cfg['telefono_invalido']!r}\n"
        f"ACCION_ID_DUPLICADO = {cfg['id_duplicado']!r}\n"
        f"ACCION_FORMULA_INCORRECTA = {cfg['formula_incorrecta']!r}\n"
        f"ACCION_TEXTO_INCONSISTENTE = {cfg['texto_inconsistente']!r}\n"
        f"FACTOR_IQR = {factor_iqr!r}\n"
        f"VALORES_FIJOS = {valores_fijos!r}\n"
    )


def generar_script_powerbi(config: Dict[str, str], factor_iqr: float = 1.5,
                            valores_fijos: Optional[dict] = None) -> str:
    """Script Python autocontenido para pegar en Power Query (Transformar -> Ejecutar script de Python)."""
    cabecera = '''# -*- coding: utf-8 -*-
# =============================================================================
# Script generado automaticamente por Limpiador de Tablas.
# Reproduce, sin depender del paquete data_cleaner, la MISMA configuracion
# de limpieza elegida en la interfaz (web/escritorio/API).
#
# COMO USARLO EN POWER BI:
#   1. Power Query -> clic derecho en la consulta -> Transformar ->
#      "Ejecutar script de Python".
#   2. Pegue todo este archivo en el cuadro de dialogo.
#   3. Power BI inyecta la tabla de entrada en la variable "dataset" y
#      ofrece dos resultados para expandir: "dataset_limpio" y
#      "reporte_limpieza".
#
# Requiere Python local con pandas y numpy (Power BI Desktop -> Opciones ->
# Python scripting).
# =============================================================================
import re
import difflib
import unicodedata
import pandas as pd
import numpy as np

'''
    pie = '''

dataset_limpio, reporte_limpieza = limpiar_tabla(
    dataset,
    ACCION_FALTANTE, ACCION_DUPLICADO, ACCION_ATIPICO, ACCION_TIPO_INVALIDO,
    FACTOR_IQR, VALORES_FIJOS,
    fecha_invalida=ACCION_FECHA_INVALIDA, email_invalido=ACCION_EMAIL_INVALIDO,
    telefono_invalido=ACCION_TELEFONO_INVALIDO, id_duplicado=ACCION_ID_DUPLICADO,
    formula_incorrecta=ACCION_FORMULA_INCORRECTA, texto_inconsistente=ACCION_TEXTO_INCONSISTENTE,
)
'''
    return cabecera + _bloque_config(config, factor_iqr, valores_fijos) + "\n\n" + _NUCLEO_LOGICA + pie


def generar_script_universal(config: Dict[str, str], factor_iqr: float = 1.5,
                              valores_fijos: Optional[dict] = None) -> str:
    """Script autocontenido para usar como libreria (Tableau Prep/TabPy, Alteryx, Qlik,
    notebooks) o como script de linea de comandos, con la misma configuracion elegida
    en la interfaz ya puesta como valor por defecto."""
    cabecera = '''# -*- coding: utf-8 -*-
# =============================================================================
# Script generado automaticamente por Limpiador de Tablas.
# Version autocontenida (solo pandas + numpy) con la MISMA configuracion de
# limpieza elegida en la interfaz.
#
# USO COMO LIBRERIA (Tableau Prep/TabPy, Alteryx, Qlik, notebooks):
#     from limpiador_universal_generado import limpiar_tabla, ACCION_FALTANTE, \\
#         ACCION_DUPLICADO, ACCION_ATIPICO, ACCION_TIPO_INVALIDO, FACTOR_IQR, VALORES_FIJOS
#     df_limpio, df_reporte = limpiar_tabla(df, ACCION_FALTANTE, ACCION_DUPLICADO,
#                                            ACCION_ATIPICO, ACCION_TIPO_INVALIDO,
#                                            FACTOR_IQR, VALORES_FIJOS)
#
# USO POR LINEA DE COMANDOS:
#     python limpiador_universal_generado.py entrada.csv
#     # genera entrada_limpio.csv y entrada_reporte_limpieza.csv junto al original
# =============================================================================
import sys
import os
import re
import difflib
import unicodedata
import pandas as pd
import numpy as np

'''
    pie = '''

def _main_cli():
    if len(sys.argv) < 2:
        print("Uso: python limpiador_universal_generado.py archivo.csv")
        return
    ruta = sys.argv[1]
    if ruta.lower().endswith((".xlsx", ".xls", ".xlsm")):
        df = pd.read_excel(ruta)
    else:
        df = pd.read_csv(ruta)

    df_limpio, df_reporte = limpiar_tabla(
        df, ACCION_FALTANTE, ACCION_DUPLICADO, ACCION_ATIPICO, ACCION_TIPO_INVALIDO,
        FACTOR_IQR, VALORES_FIJOS,
        fecha_invalida=ACCION_FECHA_INVALIDA, email_invalido=ACCION_EMAIL_INVALIDO,
        telefono_invalido=ACCION_TELEFONO_INVALIDO, id_duplicado=ACCION_ID_DUPLICADO,
        formula_incorrecta=ACCION_FORMULA_INCORRECTA, texto_inconsistente=ACCION_TEXTO_INCONSISTENTE,
    )

    base, _ext = os.path.splitext(os.path.basename(ruta))
    carpeta = os.path.dirname(os.path.abspath(ruta))
    ruta_limpio = os.path.join(carpeta, base + "_limpio.csv")
    ruta_reporte = os.path.join(carpeta, base + "_reporte_limpieza.csv")
    df_limpio.to_csv(ruta_limpio, index=False)
    df_reporte.to_csv(ruta_reporte, index=False)

    print(f"Filas originales: {len(df)} | Filas finales: {len(df_limpio)}")
    print(f"Hallazgos detectados: {len(df_reporte)}")
    print(f"Tabla limpia guardada en:  {ruta_limpio}")
    print(f"Reporte guardado en:       {ruta_reporte}")


if __name__ == "__main__":
    _main_cli()
'''
    return cabecera + _bloque_config(config, factor_iqr, valores_fijos) + "\n\n" + _NUCLEO_LOGICA + pie


def _escapar_m(texto: str) -> str:
    """Escapa un string Python para que quede valido dentro de un literal de texto M."""
    return texto.replace('"', '""').replace("\n", "#(lf)")


_IDENTIFICADOR_M_SIMPLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _referencia_m(nombre: str) -> str:
    """Devuelve el nombre de un paso listo para usarse como identificador M.
    Los identificadores M con espacios u otros caracteres especiales (el caso
    normal para pasos de Power Query, ej. "Tipo de columna cambiado") deben
    escribirse entre comillas con el prefijo #, o Power Query los rechaza
    como error de sintaxis."""
    nombre = nombre.strip()
    if _IDENTIFICADOR_M_SIMPLE.match(nombre):
        return nombre
    return f'#"{nombre.replace(chr(34), chr(34) * 2)}"'


def generar_editor_m(config: Dict[str, str], factor_iqr: float = 1.5,
                      valores_fijos: Optional[dict] = None,
                      nombre_paso_anterior: str = "TuPasoAnterior") -> str:
    """Codigo M listo para pegar en el Editor avanzado de Power Query."""
    script_python = generar_script_powerbi(config, factor_iqr, valores_fijos)
    script_m = _escapar_m(script_python)
    referencia_paso_anterior = _referencia_m(nombre_paso_anterior)
    return f'''// =============================================================================
// Codigo M generado automaticamente por Limpiador de Tablas.
// Pegar en Power Query -> clic derecho en la consulta -> "Editor avanzado",
// reemplazando el contenido existente (o insertando este paso dentro de tu
// secuencia, antes del "in" final). Ajuste "{nombre_paso_anterior}" al
// nombre real de su ultimo paso.
// =============================================================================

let
    Origen = {referencia_paso_anterior},

    EjecutarLimpieza = Python.Execute("{script_m}", [dataset=Origen]),

    // Para auditar los cambios en vez de obtener la tabla limpia, cambie
    // "dataset_limpio" por "reporte_limpieza" en la linea de abajo.
    TablaSeleccionada = EjecutarLimpieza{{[Name="dataset_limpio"]}}[Value]
in
    TablaSeleccionada
'''

"""
Analizador de tablas: detecta valores nulos, duplicados, atípicos y errores
de tipo/formato. Devuelve una lista de "hallazgos" (issues) estandarizada
que luego usan cleaner.py y report.py.

Además de los 4 chequeos genéricos originales (faltante, duplicado,
tipo_invalido, atipico), incluye 6 chequeos adicionales orientados a
reglas de negocio comunes en datasets tabulares:

    fecha_invalida       Fechas no parseables o fuera de un rango dado
    email_invalido       Correos que no cumplen el formato usuario@dominio.tld
    telefono_invalido     Teléfonos con formato/longitud de dígitos inválida
    id_duplicado          Valor repetido en una columna identificadora
                           (distinto de 'duplicado', que exige fila completa igual)
    formula_incorrecta    Una columna no coincide con el resultado de una
                           operación sobre otras (ej. Total ≠ Cantidad × Precio)
    texto_inconsistente   Variantes / errores de tipeo de un mismo valor
                           categórico (ej. "San Jose" vs "San José")

Todos estos chequeos son opcionales y auto-detectan columnas candidatas por
el nombre (auto_detectar_columnas=True) si no se indican explícitamente,
para poder reutilizar el mismo motor en datasets distintos sin reescribir
código — solo pasando los nombres de columna cuando el nombre no sea obvio.
"""
from __future__ import annotations
import re
import difflib
import unicodedata
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


@dataclass
class Issue:
    tipo: str            # 'faltante' | 'duplicado' | 'atipico' | 'tipo_invalido' |
                          # 'fecha_invalida' | 'email_invalido' | 'telefono_invalido' |
                          # 'id_duplicado' | 'formula_incorrecta' | 'texto_inconsistente'
    columna: Optional[str]
    fila: int             # índice original del DataFrame (0-based)
    valor_original: Any
    detalle: str = ""
    valor_sugerido: Any = None   # valor correcto propuesto, cuando se puede calcular
                                   # (ej. total correcto, forma canónica de un texto)


@dataclass
class AnalysisResult:
    filas_analizadas: int
    columnas_analizadas: int
    issues: List[Issue] = field(default_factory=list)

    def por_tipo(self) -> dict:
        resumen = {}
        for issue in self.issues:
            resumen[issue.tipo] = resumen.get(issue.tipo, 0) + 1
        return resumen

    def por_columna(self) -> dict:
        resumen = {}
        for issue in self.issues:
            key = issue.columna or "(fila completa)"
            resumen[key] = resumen.get(key, 0) + 1
        return resumen


def _es_columna_numerica_potencial(serie: pd.Series) -> bool:
    """Determina si una columna 'object' en realidad contiene números como texto."""
    convertibles = pd.to_numeric(serie.dropna(), errors="coerce")
    return convertibles.notna().mean() > 0.7 if len(serie.dropna()) else False


def detectar_faltantes(df: pd.DataFrame) -> List[Issue]:
    issues = []
    for col in df.columns:
        nulos = df[df[col].isna()]
        for idx in nulos.index:
            issues.append(Issue("faltante", col, int(idx), None, "Valor vacío/nulo"))
    return issues


def detectar_duplicados(df: pd.DataFrame) -> List[Issue]:
    issues = []
    mask = df.duplicated(keep="first")
    for idx in df[mask].index:
        issues.append(Issue("duplicado", None, int(idx), df.loc[idx].to_dict(),
                             "Fila duplicada (idéntica a una anterior)"))
    return issues


def detectar_tipo_invalido(df: pd.DataFrame) -> List[Issue]:
    """Detecta celdas de texto no numérico dentro de columnas mayormente numéricas.

    Excluye columnas de telefono/fax: aunque sean mayormente dígitos, un
    formato como "(33)5120578961" o "2 9261-2433" es un teléfono válido
    con paréntesis/espacios/guiones, no un dato corrupto. La validación
    correcta para esas columnas ya la hace detectar_telefonos_invalidos
    (que sí entiende ese formato), no "se esperaba un valor numérico".
    """
    issues = []
    cols_telefono = set(_detectar_columnas_por_contenido(df, _PATRONES_TELEFONO, _parece_telefono,
                                                          excluir_por_nombre=_PATRONES_NO_TELEFONO))
    for col in df.columns:
        if col in cols_telefono:
            continue
        serie = df[col]
        es_texto = pd.api.types.is_object_dtype(serie) or pd.api.types.is_string_dtype(serie)
        if es_texto and _es_columna_numerica_potencial(serie):
            convertidos = pd.to_numeric(serie, errors="coerce")
            malos = serie[(convertidos.isna()) & (serie.notna())]
            for idx, val in malos.items():
                issues.append(Issue("tipo_invalido", col, int(idx), val,
                                     "Se esperaba un valor numérico"))
    return issues


def _columnas_numericas_o_potenciales(df: pd.DataFrame) -> List[str]:
    """Columnas numéricas reales + columnas de texto que son mayormente numéricas.

    Excluye columnas identificadoras (id, codigo, folio...) y de
    telefono/fax (ver patrones.columnas_excluir_de_atipicos): no son
    magnitudes continuas, asi que aplicarles IQR/Z-score solo genera
    falsos positivos.
    """
    excluidas = set(_columnas_excluir_de_atipicos(df))
    cols = [c for c in df.select_dtypes(include=[np.number]).columns if c not in excluidas]
    for col in df.columns:
        if col in cols or col in excluidas:
            continue
        serie = df[col]
        es_texto = pd.api.types.is_object_dtype(serie) or pd.api.types.is_string_dtype(serie)
        if es_texto and _es_columna_numerica_potencial(serie):
            cols.append(col)
    return cols


def detectar_atipicos_iqr(df: pd.DataFrame, factor: float = 1.5,
                           columnas: Optional[List[str]] = None) -> List[Issue]:
    """Detecta valores atípicos usando el método de rango intercuartílico (IQR)."""
    issues = []
    columnas = columnas or _columnas_numericas_o_potenciales(df)
    for col in columnas:
        if col not in df.columns:
            continue
        serie = pd.to_numeric(df[col], errors="coerce")
        q1, q3 = serie.quantile(0.25), serie.quantile(0.75)
        iqr = q3 - q1
        if iqr == 0 or pd.isna(iqr):
            continue
        lim_inf, lim_sup = q1 - factor * iqr, q3 + factor * iqr
        atipicos = serie[(serie < lim_inf) | (serie > lim_sup)]
        for idx, val in atipicos.items():
            issues.append(Issue(
                "atipico", col, int(idx), val,
                f"Fuera de rango [{lim_inf:.2f}, {lim_sup:.2f}] (método IQR)"
            ))
    return issues


def detectar_atipicos_zscore(df: pd.DataFrame, umbral: float = 3.0,
                              columnas: Optional[List[str]] = None) -> List[Issue]:
    """Detecta valores atípicos usando puntuación Z (desviaciones estándar)."""
    issues = []
    columnas = columnas or _columnas_numericas_o_potenciales(df)
    for col in columnas:
        if col not in df.columns:
            continue
        serie = pd.to_numeric(df[col], errors="coerce")
        media, std = serie.mean(), serie.std()
        if std == 0 or pd.isna(std):
            continue
        z = (serie - media) / std
        atipicos = serie[z.abs() > umbral]
        for idx, val in atipicos.items():
            issues.append(Issue(
                "atipico", col, int(idx), val,
                f"Z-score={((val - media) / std):.2f} (umbral={umbral})"
            ))
    return issues


# ---------------------------------------------------------------------------
# Detección de columnas candidatas por nombre (para auto_detectar_columnas=True)
# ---------------------------------------------------------------------------

from data_cleaner.patrones import (
    PATRONES_EMAIL as _PATRONES_EMAIL,
    PATRONES_TELEFONO as _PATRONES_TELEFONO,
    PATRONES_FECHA as _PATRONES_FECHA,
    PATRONES_TOTAL as _PATRONES_TOTAL,
    PATRONES_CANTIDAD as _PATRONES_CANTIDAD,
    PATRONES_PRECIO as _PATRONES_PRECIO,
    PATRONES_DESCUENTO as _PATRONES_DESCUENTO,
    PATRONES_IMPUESTO as _PATRONES_IMPUESTO,
    PATRONES_ENVIO as _PATRONES_ENVIO,
    PATRONES_NO_TELEFONO as _PATRONES_NO_TELEFONO,
    columna_admite_fecha_pendiente as _columna_admite_fecha_pendiente,
    es_valor_fecha_pendiente as _es_valor_fecha_pendiente,
    columnas_por_patron as _columnas_por_patron,
    es_columna_id as _es_columna_id,
    detectar_columnas as _detectar_columnas_por_contenido,
    parece_email as _parece_email,
    parece_telefono as _parece_telefono,
    parece_fecha as _parece_fecha,
    rango_digitos_telefono as _rango_digitos_telefono,
    columnas_excluir_de_atipicos as _columnas_excluir_de_atipicos,
)
# _PATRONES_*, _columnas_por_patron y _es_columna_id ahora vienen del modulo
# compartido data_cleaner.patrones (mismo que usa exportador_m.py), en vez
# de una copia local: normalizan acentos/mayusculas, exigen coincidencia de
# palabra completa (antes "date" podia matchear dentro de "Update"), traen
# mas vocabulario (ingles + sinonimos de nomina/salud/logistica), y
# _detectar_columnas_por_contenido permite reconocer una columna por sus
# VALORES cuando el nombre no da ninguna pista (ej. "campo_7").
_PATRONES_EXCLUIR_TEXTO = _PATRONES_EMAIL + _PATRONES_TELEFONO + _PATRONES_FECHA + \
    ("nombre", "cliente", "direccion", "dirección", "observacion", "observación", "comentario")


def _es_numero_con_sufijo(serie: pd.Series) -> bool:
    """Detecta columnas tipo '7 unidades', '20 unidades' (número + texto fijo).
    _es_columna_numerica_potencial no las detecta porque no son 100% numéricas
    (pd.to_numeric falla por el sufijo de texto), pero para efectos de
    'texto_inconsistente' deben tratarse como numéricas: si no se excluyen,
    valores con números distintos y el mismo sufijo (ej. '7 unidades' y
    '20 unidades') quedan muy similares como cadena y se marcan por error
    como variantes/typos entre sí.
    """
    no_nulos = serie.dropna().astype(str)
    if len(no_nulos) == 0:
        return False
    con_numero_inicial = no_nulos.str.match(r"^\s*-?\d+([.,]\d+)?\b")
    return con_numero_inicial.mean() > 0.7


# ---------------------------------------------------------------------------
# Fechas
# ---------------------------------------------------------------------------

def detectar_fechas_invalidas(df: pd.DataFrame, columnas: Optional[List[str]] = None,
                               fecha_min=None, fecha_max=None, auto: bool = True) -> List[Issue]:
    """
    Marca celdas cuyo valor no se puede interpretar como fecha, o que caen
    fuera de [fecha_min, fecha_max] cuando se indican esos límites.
    Los valores vacíos no se reportan aquí (ya los cubre detectar_faltantes).
    """
    issues = []
    cols = columnas if columnas is not None else (_detectar_columnas_por_contenido(df, _PATRONES_FECHA, _parece_fecha) if auto else [])
    lim_min = pd.Timestamp(fecha_min) if fecha_min is not None else None
    lim_max = pd.Timestamp(fecha_max) if fecha_max is not None else None

    for col in cols:
        if col not in df.columns:
            continue
        serie = df[col]
        # Fechas de cobro/pago/entrega/ingreso: "Pendiente", "No aplica",
        # "Sin fecha", etc. son un estado valido (la transaccion aun no
        # ocurre), no un formato de fecha corrupto -- se dejan pasar sin
        # generar hallazgo para esas columnas especificas.
        admite_pendiente = _columna_admite_fecha_pendiente(col)
        parseado = pd.to_datetime(serie, errors="coerce")
        for idx, val in serie.items():
            if pd.isna(val):
                continue
            if admite_pendiente and _es_valor_fecha_pendiente(val):
                continue
            fecha = parseado.loc[idx]
            if pd.isna(fecha):
                # Reintento con dayfirst por si el formato es dd/mm/aaaa
                fecha = pd.to_datetime(val, errors="coerce", dayfirst=True)
            if pd.isna(fecha):
                issues.append(Issue("fecha_invalida", col, int(idx), val,
                                     "Formato de fecha no reconocido"))
                continue
            if lim_min is not None and fecha < lim_min:
                issues.append(Issue("fecha_invalida", col, int(idx), val,
                                     f"Fecha fuera de rango (anterior a {lim_min.date()})"))
            elif lim_max is not None and fecha > lim_max:
                issues.append(Issue("fecha_invalida", col, int(idx), val,
                                     f"Fecha fuera de rango (posterior a {lim_max.date()})"))
    return issues


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

_REGEX_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def detectar_emails_invalidos(df: pd.DataFrame, columnas: Optional[List[str]] = None,
                               auto: bool = True) -> List[Issue]:
    issues = []
    cols = columnas if columnas is not None else (_detectar_columnas_por_contenido(df, _PATRONES_EMAIL, _parece_email) if auto else [])
    for col in cols:
        if col not in df.columns:
            continue
        for idx, val in df[col].items():
            if pd.isna(val):
                continue
            if not _REGEX_EMAIL.match(str(val).strip()):
                issues.append(Issue("email_invalido", col, int(idx), val,
                                     "Formato de correo electrónico inválido"))
    return issues


# ---------------------------------------------------------------------------
# Teléfono
# ---------------------------------------------------------------------------

_REGEX_TELEFONO_FORMATO = re.compile(r"^[\d\s\-\+\(\)]+$")


def detectar_telefonos_invalidos(df: pd.DataFrame, columnas: Optional[List[str]] = None,
                                  patron: Optional[str] = None,
                                  min_digitos: Optional[int] = None, max_digitos: Optional[int] = None,
                                  paises: Optional[List[str]] = None,
                                  permitir_codigo_pais: bool = True,
                                  auto: bool = True) -> List[Issue]:
    """
    Un teléfono se considera inválido si contiene caracteres fuera de
    dígitos/espacios/guiones/paréntesis/'+', o si la cantidad de dígitos no
    cae en el rango esperado.

    El rango esperado se resuelve así: si se dan min_digitos/max_digitos
    explícitos, mandan sobre todo lo demás. Si no, se calcula por país(es)
    (union de los rangos típicos de celular de esos países — ver
    patrones.DIGITOS_TELEFONO_PAIS); sin países tampoco, se usa el rango
    internacional amplio (7-15 dígitos, estándar E.164) en vez de asumir un
    solo país. Con permitir_codigo_pais=True (por defecto) también se acepta
    el mismo número con 1-3 dígitos extra al inicio (código de país sin "+").
    """
    if min_digitos is not None and max_digitos is not None:
        min_d, max_d = min_digitos, max_digitos
    else:
        min_d, max_d = _rango_digitos_telefono(paises)
    issues = []
    cols = columnas if columnas is not None else (_detectar_columnas_por_contenido(
        df, _PATRONES_TELEFONO, _parece_telefono, excluir_por_nombre=_PATRONES_NO_TELEFONO) if auto else [])
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
            n = len(solo_digitos)
            longitud_ok = min_d <= n <= max_d
            if not longitud_ok and permitir_codigo_pais:
                longitud_ok = (min_d + 1) <= n <= (max_d + 3)
            if not (formato_ok and longitud_ok):
                rango = f"{min_d}" if min_d == max_d else f"{min_d}-{max_d}"
                issues.append(Issue("telefono_invalido", col, int(idx), val,
                                     f"Formato/longitud de teléfono inválido (se esperaban {rango} dígitos)"))
    return issues


# ---------------------------------------------------------------------------
# ID duplicado (distinto de fila duplicada completa)
# ---------------------------------------------------------------------------

def detectar_ids_duplicados(df: pd.DataFrame, columnas: Optional[List[str]] = None,
                             auto: bool = True, umbral_unicidad: float = 0.9) -> List[Issue]:
    """
    'umbral_unicidad' evita que columnas tipo llave foránea (ej. ID_Cliente,
    que se repite legítimamente cada vez que un cliente vuelve a comprar) se
    autodetecten como identificador único: en modo automático solo se
    consideran columnas cuyo nombre matchea un patrón de id (_es_columna_id)
    Y cuya proporción de valores distintos es alta (>= umbral_unicidad),
    es decir que se comportan como llave primaria y no como llave foránea.
    """
    if columnas is not None:
        cols = columnas
    elif auto:
        cols = []
        for c in df.columns:
            if not _es_columna_id(c):
                continue
            serie_no_nula = df[c].dropna()
            if len(serie_no_nula) == 0:
                continue
            if serie_no_nula.nunique() / len(serie_no_nula) >= umbral_unicidad:
                cols.append(c)
    else:
        cols = []
    issues = []
    for col in cols:
        if col not in df.columns:
            continue
        serie = df[col]
        mask = serie.duplicated(keep="first") & serie.notna()
        for idx in serie[mask].index:
            issues.append(Issue("id_duplicado", col, int(idx), serie.loc[idx],
                                 f"Valor repetido en columna identificadora '{col}' "
                                 "(la fila completa no es idéntica)"))
    return issues


# ---------------------------------------------------------------------------
# Fórmula / regla de negocio entre columnas (ej. Total = Cantidad × Precio)
# ---------------------------------------------------------------------------

def _mejor_combinacion_formula(df: pd.DataFrame, cand_total: List[str], cand_cant: List[str],
                                cand_precio: List[str],
                                cand_descuento: Optional[List[str]] = None,
                                cand_impuesto: Optional[List[str]] = None,
                                cand_envio: Optional[List[str]] = None,
                                tolerancia: float = 0.01
                                ) -> Tuple[Optional[str], Optional[str], Optional[str],
                                           Optional[str], Optional[str], Optional[str], float]:
    """
    Cuando el nombre de columna da varios candidatos para 'total' (ej.
    'Costo_Unitario_USD' matchea el patrón "costo" igual que 'Monto_...'),
    tomar siempre el primero por orden de columna es arbitrario y puede
    comparar un valor por-unidad contra un total, generando que el 100%
    de las filas parezcan "incorrectas". En vez de adivinar por nombre,
    se prueba cada combinación total/cantidad/precio y se elige la que
    de verdad cuadra (mayor % de filas donde total ≈ esperado).

    La fórmula base es cantidad × precio, pero muchas tablas de negocio
    no son tan simples (total = cantidad×precio − descuento + impuesto +
    envío). Por eso también se prueba, para cada candidata de descuento/
    impuesto/envío detectada por nombre, si INCLUIRLA en la fórmula mejora
    la tasa de coincidencia frente a no incluirla (representado por None);
    así se descubre automáticamente qué ajustes forman parte de la fórmula
    real de esta tabla en particular, sin asumir que siempre aplican.

    Devuelve también esa tasa de coincidencia, para que quien llama pueda
    decidir si la combinación elegida es lo bastante confiable como para
    usarla (ver 'tasa_minima' en detectar_formula_incorrecta).
    """
    opciones_descuento = list(cand_descuento or []) + [None]
    opciones_impuesto = list(cand_impuesto or []) + [None]
    opciones_envio = list(cand_envio or []) + [None]

    mejor = (cand_total[0], cand_cant[0], cand_precio[0], None, None, None)
    mejor_tasa = -1.0
    for t in cand_total:
        for c in cand_cant:
            for p in cand_precio:
                if len({t, c, p}) < 3:
                    continue
                total = pd.to_numeric(df[t], errors="coerce")
                base = pd.to_numeric(df[c], errors="coerce") * pd.to_numeric(df[p], errors="coerce")
                for d in opciones_descuento:
                    for i in opciones_impuesto:
                        for e in opciones_envio:
                            usados = {x for x in (d, i, e) if x is not None}
                            if usados & {t, c, p} or len(usados) != len({x for x in (d, i, e) if x is not None}):
                                continue
                            esperado = base.copy()
                            if d is not None:
                                esperado = esperado - pd.to_numeric(df[d], errors="coerce").fillna(0)
                            if i is not None:
                                esperado = esperado + pd.to_numeric(df[i], errors="coerce").fillna(0)
                            if e is not None:
                                esperado = esperado + pd.to_numeric(df[e], errors="coerce").fillna(0)
                            valido = total.notna() & esperado.notna()
                            if valido.sum() == 0:
                                continue
                            diferencia = (total - esperado).abs()
                            limite = (esperado.abs() * tolerancia).clip(lower=0.01)
                            coincide = (diferencia <= limite) & valido
                            tasa = coincide.sum() / valido.sum()
                            if tasa > mejor_tasa:
                                mejor_tasa = tasa
                                mejor = (t, c, p, d, i, e)
    return (*mejor, max(mejor_tasa, 0.0))


def detectar_formula_incorrecta(df: pd.DataFrame, columna_total: Optional[str] = None,
                                 columna_cantidad: Optional[str] = None,
                                 columna_precio: Optional[str] = None,
                                 columna_descuento: Optional[str] = None,
                                 columna_impuesto: Optional[str] = None,
                                 columna_envio: Optional[str] = None,
                                 tolerancia: float = 0.01, auto: bool = True,
                                 tasa_minima: float = 0.5) -> List[Issue]:
    """
    Verifica columna_total ≈ columna_cantidad × columna_precio, ajustado
    opcionalmente por descuento (se resta), impuesto y envío/flete (se
    suman): esperado = cantidad×precio − descuento + impuesto + envío.
    Las tres columnas de ajuste son opcionales — si no aplican a la tabla,
    simplemente no se usan. 'tolerancia' es relativa (1% por defecto) con
    un piso absoluto de 0.01 para evitar falsos positivos por redondeo.

    Si no se indican las columnas y auto=True, intenta adivinarlas por
    nombre (incluidas las de ajuste), y si hay varios candidatos posibles
    para alguna, se elige la combinación que mejor cuadra con los datos
    reales, incluyendo si conviene o no usar cada ajuste (ver
    _mejor_combinacion_formula).

    'tasa_minima' es una salvaguarda: si ni siquiera la mejor combinación
    de columnas autodetectada (con o sin ajustes) cuadra en al menos esa
    fracción de filas (50% por defecto), lo más probable es que se hayan
    detectado las columnas equivocadas, o que la fórmula real de esta
    tabla sea distinta a "cantidad×precio ± ajustes" — en ese caso no se
    reportan hallazgos, en vez de inundar con "errores" que son un falso
    positivo de detección. Solo aplica cuando las columnas se autodetectan
    por nombre; si se indican explícitamente, tasa_minima no se evalúa.
    """
    issues = []
    if auto and not (columna_total and columna_cantidad and columna_precio):
        cand_total = _columnas_por_patron(df, _PATRONES_TOTAL)
        cand_cant = _columnas_por_patron(df, _PATRONES_CANTIDAD)
        cand_precio = _columnas_por_patron(df, _PATRONES_PRECIO)
        if cand_total and cand_cant and cand_precio:
            usadas = set(cand_total) | set(cand_cant) | set(cand_precio)
            cand_descuento = [columna_descuento] if columna_descuento else \
                [c for c in _columnas_por_patron(df, _PATRONES_DESCUENTO) if c not in usadas]
            cand_impuesto = [columna_impuesto] if columna_impuesto else \
                [c for c in _columnas_por_patron(df, _PATRONES_IMPUESTO) if c not in usadas]
            cand_envio = [columna_envio] if columna_envio else \
                [c for c in _columnas_por_patron(df, _PATRONES_ENVIO) if c not in usadas]
            t, c, p, d, i, e, tasa = _mejor_combinacion_formula(
                df, cand_total, cand_cant, cand_precio, cand_descuento, cand_impuesto, cand_envio, tolerancia,
            )
            if tasa < tasa_minima:
                return issues
            columna_total = columna_total or t
            columna_cantidad = columna_cantidad or c
            columna_precio = columna_precio or p
            columna_descuento = columna_descuento or d
            columna_impuesto = columna_impuesto or i
            columna_envio = columna_envio or e

    if not (columna_total and columna_cantidad and columna_precio):
        return issues
    if not all(c in df.columns for c in (columna_total, columna_cantidad, columna_precio)):
        return issues

    total = pd.to_numeric(df[columna_total], errors="coerce")
    cantidad = pd.to_numeric(df[columna_cantidad], errors="coerce")
    precio = pd.to_numeric(df[columna_precio], errors="coerce")
    esperado = cantidad * precio
    detalle_formula = f"{columna_cantidad} × {columna_precio}"
    if columna_descuento and columna_descuento in df.columns:
        esperado = esperado - pd.to_numeric(df[columna_descuento], errors="coerce").fillna(0)
        detalle_formula += f" − {columna_descuento}"
    if columna_impuesto and columna_impuesto in df.columns:
        esperado = esperado + pd.to_numeric(df[columna_impuesto], errors="coerce").fillna(0)
        detalle_formula += f" + {columna_impuesto}"
    if columna_envio and columna_envio in df.columns:
        esperado = esperado + pd.to_numeric(df[columna_envio], errors="coerce").fillna(0)
        detalle_formula += f" + {columna_envio}"

    diferencia = (total - esperado).abs()
    limite = (esperado.abs() * tolerancia).clip(lower=0.01)
    mal = (diferencia > limite) & total.notna() & esperado.notna()

    for idx in df[mal].index:
        valor_correcto = esperado.loc[idx]
        issues.append(Issue(
            "formula_incorrecta", columna_total, int(idx), df.loc[idx, columna_total],
            f"{columna_total} no coincide con {detalle_formula} "
            f"(esperado ≈ {valor_correcto:.2f})",
            valor_sugerido=round(float(valor_correcto), 2) if pd.notna(valor_correcto) else None,
        ))
    return issues


# ---------------------------------------------------------------------------
# Texto inconsistente: variantes / errores de tipeo de un mismo valor categórico
# ---------------------------------------------------------------------------

def _normalizar_texto(valor) -> str:
    s = str(valor).strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return " ".join(s.split())


def detectar_texto_inconsistente(df: pd.DataFrame, columnas: Optional[List[str]] = None,
                                  auto: bool = True, umbral_similitud: float = 0.85,
                                  max_cardinalidad_ratio: float = 0.5,
                                  min_apariciones_canonica: int = 1) -> List[Issue]:
    """
    Agrupa valores de texto cuya forma normalizada (sin tildes/mayúsculas/
    espacios extra) es muy similar entre sí (umbral_similitud, método
    difflib), y marca como 'texto_inconsistente' las variantes menos
    frecuentes dentro de cada grupo, sugiriendo la más frecuente como forma
    canónica. Pensado para columnas categóricas de baja cardinalidad
    (categoría, método de pago, vendedor, ciudad, etc.) — por eso, en modo
    automático, solo se consideran columnas de texto cuya proporción de
    valores únicos es baja (<= max_cardinalidad_ratio) y que no parezcan
    columnas de email/teléfono/fecha/nombre propio/dirección.
    """
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
            # Excluye columnas que en realidad son numéricas guardadas como
            # texto (ej. "20 unidades", "7 unidades"): sin este chequeo,
            # valores numéricos distintos con el mismo sufijo textual se
            # agrupan por similitud y se reportan como si fueran errores de
            # tipeo entre sí (ej. "7 unidades" marcado como typo de
            # "20 unidades"), cuando en realidad son cantidades válidas y
            # distintas. Ya las cubre detectar_tipo_invalido si corresponde.
            if _es_columna_numerica_potencial(no_nulos) or _es_numero_con_sufijo(no_nulos):
                continue
            if len(no_nulos) == 0:
                continue
            # Cardinalidad medida sobre el valor NORMALIZADO (sin tildes,
            # mayúsculas ni espacios extra): así una columna con muchas
            # variantes/errores de tipeo de las mismas pocas categorías
            # sigue calificando como "categórica" y no queda excluida por
            # parecer de alta cardinalidad debido justo a esos errores.
            ratio = no_nulos.map(_normalizar_texto).nunique() / len(no_nulos)
            if ratio <= max_cardinalidad_ratio:
                cols.append(col)
    else:
        cols = []

    issues = []
    for col in cols:
        if col not in df.columns:
            continue
        serie = df[col].dropna()
        if serie.empty:
            continue

        conteo_por_texto = serie.astype(str).value_counts()
        # Todas las grafías originales que comparten una misma forma
        # normalizada (ej. "Jardineria", "jardineria", "jardineria " son 3
        # grafías distintas para el mismo norm "jardineria").
        textos_por_norm: dict[str, list[tuple[str, int]]] = {}
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
            # Todas las grafías originales (una por norm o varias, si varias
            # grafías comparten el mismo norm) que caen en este grupo.
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
                        issues.append(Issue(
                            "texto_inconsistente", col, int(idx), val,
                            f"Posible variante/error de tipeo de '{canonica_texto}'",
                            valor_sugerido=canonica_texto,
                        ))
    return issues


def analizar(df: pd.DataFrame, metodo_atipicos: str = "iqr",
             factor_iqr: float = 1.5, umbral_zscore: float = 3.0,
             columnas_numericas: Optional[List[str]] = None,
             auto_detectar_columnas: bool = True,
             detectar_fechas: bool = True, columnas_fecha: Optional[List[str]] = None,
             fecha_min=None, fecha_max=None,
             detectar_emails: bool = True, columnas_email: Optional[List[str]] = None,
             detectar_telefonos: bool = True, columnas_telefono: Optional[List[str]] = None,
             patron_telefono: Optional[str] = None,
             digitos_telefono: Optional[Tuple[int, int]] = None,
             paises_telefono: Optional[List[str]] = None,
             permitir_codigo_pais_telefono: bool = True,
             detectar_ids: bool = True, columnas_id: Optional[List[str]] = None,
             detectar_formula: bool = True, columna_total: Optional[str] = None,
             columna_cantidad: Optional[str] = None, columna_precio: Optional[str] = None,
             columna_descuento: Optional[str] = None, columna_impuesto: Optional[str] = None,
             columna_envio: Optional[str] = None,
             tolerancia_formula: float = 0.01,
             detectar_texto: bool = True, columnas_texto: Optional[List[str]] = None,
             umbral_similitud_texto: float = 0.85) -> AnalysisResult:
    """
    Ejecuta todas las detecciones y consolida los resultados.

    Los 6 chequeos nuevos (fechas, emails, teléfonos, ids duplicados,
    fórmula y texto inconsistente) son opcionales y se pueden desactivar
    individualmente (detectar_*=False) o apuntar a columnas específicas
    (columnas_*=[...]); si no se indican columnas y auto_detectar_columnas
    es True, se intentan adivinar por el nombre de columna — así el mismo
    motor sirve para datasets distintos sin cambiar código.
    """
    issues: List[Issue] = []
    issues += detectar_faltantes(df)
    issues += detectar_duplicados(df)
    issues += detectar_tipo_invalido(df)

    if detectar_fechas:
        issues += detectar_fechas_invalidas(df, columnas=columnas_fecha, fecha_min=fecha_min,
                                             fecha_max=fecha_max, auto=auto_detectar_columnas)
    if detectar_emails:
        issues += detectar_emails_invalidos(df, columnas=columnas_email, auto=auto_detectar_columnas)
    if detectar_telefonos:
        issues += detectar_telefonos_invalidos(df, columnas=columnas_telefono, patron=patron_telefono,
                                                min_digitos=digitos_telefono[0] if digitos_telefono else None,
                                                max_digitos=digitos_telefono[1] if digitos_telefono else None,
                                                paises=paises_telefono,
                                                permitir_codigo_pais=permitir_codigo_pais_telefono,
                                                auto=auto_detectar_columnas)
    if detectar_ids:
        issues += detectar_ids_duplicados(df, columnas=columnas_id, auto=auto_detectar_columnas)
    if detectar_formula:
        issues += detectar_formula_incorrecta(df, columna_total=columna_total, columna_cantidad=columna_cantidad,
                                               columna_precio=columna_precio, columna_descuento=columna_descuento,
                                               columna_impuesto=columna_impuesto, columna_envio=columna_envio,
                                               tolerancia=tolerancia_formula, auto=auto_detectar_columnas)
    if detectar_texto:
        issues += detectar_texto_inconsistente(df, columnas=columnas_texto, auto=auto_detectar_columnas,
                                                umbral_similitud=umbral_similitud_texto)

    if metodo_atipicos == "iqr":
        issues += detectar_atipicos_iqr(df, factor=factor_iqr, columnas=columnas_numericas)
    elif metodo_atipicos == "zscore":
        issues += detectar_atipicos_zscore(df, umbral=umbral_zscore, columnas=columnas_numericas)
    elif metodo_atipicos == "ambos":
        iqr_issues = detectar_atipicos_iqr(df, factor=factor_iqr, columnas=columnas_numericas)
        zscore_issues = detectar_atipicos_zscore(df, umbral=umbral_zscore, columnas=columnas_numericas)
        # Una misma celda puede salir atípica por ambos métodos a la vez: se
        # fusiona en un único hallazgo para no inflar conteos ni aplicar la
        # corrección dos veces sobre el mismo dato.
        combinados: dict[tuple, Issue] = {(i.columna, i.fila): i for i in iqr_issues}
        for issue in zscore_issues:
            clave = (issue.columna, issue.fila)
            if clave in combinados:
                combinados[clave].detalle += f" | también atípico por Z-score ({issue.detalle})"
            else:
                combinados[clave] = issue
        issues += list(combinados.values())

    return AnalysisResult(
        filas_analizadas=len(df),
        columnas_analizadas=len(df.columns),
        issues=issues,
    )

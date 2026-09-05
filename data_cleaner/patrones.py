# -*- coding: utf-8 -*-
"""
patrones.py
===========
Detección de "qué tipo de columna es esta" (email, teléfono, fecha, ID,
importe/cantidad/precio), pensada para funcionar en CUALQUIER dataset —
no solo el de ventas de jardinería con el que se probó originalmente.

Reemplaza los patrones duplicados que antes vivían por separado en
analyzer.py y exportador_m.py. Antes de este módulo, cada uno tenía su
propia tupla de palabras (en español/inglés limitado) y las comparaba con
`palabra in nombre_columna.lower()` — lo cual:
  1. Se desincroniza fácil (cambiar un patrón en un lugar y no en el otro).
  2. Da falsos positivos: "date" es substring de "Update_At" o "Validated".
  3. No reconoce columnas si el nombre viene con acentos/mayúsculas en una
     combinación que no estaba en la lista (ej. "TELÉFONO" vs "telefono").
  4. Se queda corto en datasets de otros rubros (nómina, salud, logística)
     o en otro idioma, donde el nombre de columna no es "email"/"telefono".

ESTRATEGIA EN DOS NIVELES (para no perder cobertura ni precisión):
  Nivel 1 — por NOMBRE de columna (rápido, es lo que ya existía, pero
            mejorado): se normaliza el nombre (sin acentos, minúsculas,
            separado en "tokens" por _/-/espacio/camelCase) y se compara
            por palabra completa, no por substring crudo.
  Nivel 2 — por CONTENIDO (nuevo, solo como respaldo): si el Nivel 1 no
            encontró ninguna columna para una regla dada, se revisan los
            VALORES reales de las columnas candidatas (texto, no ID, no
            ya clasificadas) para ver si "parecen" email/teléfono/fecha/ID
            aunque el nombre de columna no lo delate (ej. "contacto_1",
            "campo_7", nombres en otro idioma).

Esto es aditivo: cualquier columna que ya se detectaba por nombre se
sigue detectando igual. El Nivel 2 solo agrega cobertura donde antes no
había ninguna.
"""
from __future__ import annotations
import re
import unicodedata
from typing import Iterable, List, Optional, Tuple

import pandas as pd

# -----------------------------------------------------------------------------
# Normalización de nombres de columna
# -----------------------------------------------------------------------------
def normalizar_nombre(col) -> str:
    """'Teléfono_Cliente' -> 'telefono_cliente'; 'FechaVenta' -> 'fecha_venta'."""
    s = str(col).strip()
    # separar camelCase: "FechaVenta" -> "Fecha Venta"
    s = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r'[^a-z0-9]+', '_', s).strip('_')
    return s


def _tokens(nombre_normalizado: str) -> List[str]:
    return [t for t in nombre_normalizado.split('_') if t]


def coincide_patron(col, patrones: Iterable[str]) -> bool:
    """True si algun patron aparece como palabra completa en el nombre de
    columna normalizado (o pegado solo a digitos, ej. "telefono1",
    "email_2"). Exige coincidencia de TOKEN completo, no substring dentro
    de otra palabra — así "date" no matchea dentro de "update" o
    "validated", ni "id" dentro de "rapido" o "video"."""
    norm = normalizar_nombre(col)
    tokens = _tokens(norm)
    for p in patrones:
        p_norm = normalizar_nombre(p)
        if not p_norm:
            continue
        if '_' in p_norm:
            # patron multi-palabra (ej. "correo_electronico"): basta con
            # que la secuencia completa aparezca en el nombre normalizado.
            if p_norm in norm:
                return True
            continue
        for tok in tokens:
            if tok == p_norm:
                return True
            # variantes con numero pegado: "telefono1", "email_2" (aunque
            # el "_2" ya seria un token separado, se cubre por si acaso).
            if re.fullmatch(re.escape(p_norm) + r'\d+', tok) or re.fullmatch(r'\d+' + re.escape(p_norm), tok):
                return True
    return False


def columnas_por_patron(df: pd.DataFrame, patrones: Iterable[str]) -> List[str]:
    return [c for c in df.columns if coincide_patron(c, patrones)]


def es_columna_id(col) -> bool:
    norm = normalizar_nombre(col)
    tokens = _tokens(norm)
    if norm == "id" or "id" in tokens:
        return True
    if any(p in norm for p in ("codigo", "folio", "identificador", "clave")):
        return True
    return False


# -----------------------------------------------------------------------------
# Identificadores alfanumericos/numericos que NO son telefono aunque su
# CONTENIDO tenga una cantidad de digitos parecida (7-15) a un numero de
# celular: numero de chasis (VIN), numero de motor, placa, numero de serie,
# lote, SKU, IMEI, numero de cuenta/poliza/factura, etc. Sin esta lista, la
# deteccion por CONTENIDO de telefono (Nivel 2 de detectar_columnas, el
# respaldo que se usa cuando ninguna columna del dataset se llama "telefono"
# o similar) podia marcar por error una de estas columnas como telefono solo
# porque, al quitarle los caracteres no numericos, la cantidad de digitos
# quedaba dentro del rango esperado. Se organiza por rubro para que sea facil
# agregar mas terminos sin tener que revisar toda la lista de una vez.
# -----------------------------------------------------------------------------
PATRONES_IDENTIFICADORES_NO_TELEFONO: dict[str, Tuple[str, ...]] = {
    "vehiculo": (
        "chasis", "vin", "motor", "numero_motor", "serie_motor",
        "placa", "matricula", "patente",
    ),
    "producto_inventario": (
        "sku", "codigo_barras", "ean", "upc", "gtin", "numero_serie",
        "serie", "numero_lote", "lote", "numero_parte", "part_number",
        "numero_pieza", "referencia",
    ),
    "documento_financiero": (
        "numero_cuenta", "cuenta", "iban", "swift", "numero_poliza",
        "poliza", "numero_contrato", "contrato", "numero_factura",
        "factura", "expediente", "numero_expediente",
    ),
    "dispositivo": (
        "imei", "mac", "numero_serial", "serial",
    ),
    "pedidos_logistica": (
        "codigo_producto", "numero_orden", "orden_compra", "numero_pedido",
        "pedido", "codigo_pedido", "tracking", "numero_tracking",
        "codigo_tracking", "guia", "numero_guia", "guia_envio", "awb",
        "conocimiento_embarque", "folio_pago", "numero_folio",
        "codigo_rastreo", "numero_seguimiento", "seguimiento",
        "numero_referencia", "referencia_pago",
        # Agregar aqui mas terminos de este rubro segun se detecten nuevos
        # falsos positivos (columnas que un dataset llama "celular" solo
        # porque su contenido tiene una cantidad de digitos parecida).
    ),
}

# Version "aplanada" (todos los rubros juntos) para usar directo como
# `excluir_por_nombre` en detectar_columnas().
PATRONES_NO_TELEFONO: Tuple[str, ...] = tuple(
    p for patrones in PATRONES_IDENTIFICADORES_NO_TELEFONO.values() for p in patrones
)


# -----------------------------------------------------------------------------
# Valores de texto que, dentro de una columna de fecha de COBRO, PAGO,
# ENTREGA o INGRESO, significan "todavia no ocurrio" en vez de un dato
# corrupto: la transaccion esta pendiente, no aplica, o simplemente no tiene
# fecha aun. Sin esta lista, detectar_fechas_invalidas() marcaba "Pendiente",
# "No aplica", etc. como "Formato de fecha no reconocido" -- un falso
# positivo, porque esas columnas legitimamente no tienen fecha hasta que se
# cobra/paga/entrega. Se organiza por concepto para poder agregar mas
# variantes sin revisar toda la lista de una vez. Los valores se comparan ya
# normalizados (sin acentos, minusculas, espacios colapsados) via
# `es_valor_fecha_pendiente`.
# -----------------------------------------------------------------------------
VALORES_FECHA_PENDIENTE_POR_CONCEPTO: dict[str, Tuple[str, ...]] = {
    "pendiente": (
        "pendiente", "pendiente de pago", "pendiente de cobro",
        "pendiente de entrega", "por cobrar", "por pagar", "por entregar",
    ),
    "no_aplica": (
        "no aplica", "n/a", "na", "no corresponde",
    ),
    "sin_fecha": (
        "sin fecha", "no fecha", "no definido", "no definida", "sin definir",
        "null", "none", "-",
    ),
}

VALORES_FECHA_PENDIENTE: Tuple[str, ...] = tuple(
    v for valores in VALORES_FECHA_PENDIENTE_POR_CONCEPTO.values() for v in valores
)

# Columnas de fecha donde tiene sentido el concepto de "pendiente" (una
# transaccion que aun no ocurre) -- a diferencia de, por ejemplo, una fecha
# de nacimiento, donde "pendiente" no aplica y un valor asi seguiria siendo
# un dato invalido.
PATRONES_FECHA_CON_PENDIENTE: Tuple[str, ...] = ("cobro", "pago", "entrega", "ingreso")


def _normalizar_valor_texto(valor) -> str:
    """Normaliza un valor de celda (sin acentos, minusculas, espacios
    colapsados) para compararlo contra VALORES_FECHA_PENDIENTE u otras
    listas de valores de texto conocidos."""
    s = str(valor).strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.split())


def es_valor_fecha_pendiente(valor) -> bool:
    """True si `valor` es un texto tipo 'Pendiente', 'No aplica', 'Sin fecha',
    etc. (ver VALORES_FECHA_PENDIENTE) -- un estado valido para una fecha de
    cobro/pago/entrega/ingreso que aun no ocurre, no un dato corrupto."""
    if valor is None:
        return False
    return _normalizar_valor_texto(valor) in VALORES_FECHA_PENDIENTE


def columna_admite_fecha_pendiente(col) -> bool:
    """True si el NOMBRE de la columna corresponde a un concepto de fecha
    donde 'pendiente'/'no aplica'/'sin fecha' es un valor legitimo (cobro,
    pago, entrega, ingreso)."""
    return coincide_patron(col, PATRONES_FECHA_CON_PENDIENTE)


# -----------------------------------------------------------------------------
# Vocabulario ampliado (bilingue espanol/ingles + sinonimos comunes de
# distintos rubros: retail, nomina, salud, logistica, educacion).
# -----------------------------------------------------------------------------
PATRONES_EMAIL = (
    "email", "correo", "e_mail", "mail", "correo_electronico",
)
PATRONES_TELEFONO = (
    "telefono", "phone", "celular", "movil", "whatsapp",
    "numero_telefono", "phone_number", "tel", "mobile", "fax",
)
PATRONES_FECHA_FUERTE = (
    "fecha", "date", "fec", "dob", "birth", "nacimiento",
    "created_at", "updated_at", "timestamp", "vencimiento", "expiry",
)
# Palabras "debiles": por si solas son un indicio ambiguo de fecha, porque
# tambien aparecen en nombres de columna que NO son fechas (ej.
# "folio_pago", "codigo_cobro", "numero_entrega" -- identificadores, no
# fechas). Solo cuentan como fecha si la columna, ademas, no es ya un
# identificador (ver es_columna_id); si lo es, se asume que "pago"/"cobro"/
# "entrega"/"ingreso"/"egreso" se refieren al folio/codigo de la
# transaccion, no a su fecha. Antes esta distincion no existia y una
# columna como "folio_pago" se detectaba como fecha solo por el token
# "pago", marcando cada numero de folio como "Formato de fecha no
# reconocido" (falso positivo).
PATRONES_FECHA_DEBIL = ("ingreso", "egreso", "cobro", "pago", "entrega")
PATRONES_FECHA = PATRONES_FECHA_FUERTE + PATRONES_FECHA_DEBIL
PATRONES_TOTAL = (
    "total", "monto", "importe", "amount", "subtotal", "salario", "sueldo",
    "costo", "cost",
)
PATRONES_CANTIDAD = (
    "cantidad", "qty", "quantity", "cant", "unidades", "horas", "hours",
    "peso", "weight",
)
PATRONES_PRECIO = (
    "precio", "price", "tarifa", "rate", "valor_unitario", "unit_price",
)
PATRONES_DESCUENTO = (
    "descuento", "discount", "rebaja", "bonificacion", "dcto",
)
PATRONES_IMPUESTO = (
    "impuesto", "iva", "tax", "itbis", "vat",
)
PATRONES_ENVIO = (
    "envio", "flete", "shipping", "freight", "logistico",
)


# -----------------------------------------------------------------------------
# Nivel 2: deteccion por CONTENIDO (respaldo cuando el nombre no dice nada)
# -----------------------------------------------------------------------------
_REGEX_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def _muestra(serie: pd.Series, n: int = 200) -> pd.Series:
    no_nulos = serie.dropna()
    if len(no_nulos) == 0:
        return no_nulos
    return no_nulos.sample(min(n, len(no_nulos)), random_state=0) if len(no_nulos) > n else no_nulos


def parece_email(serie: pd.Series, umbral: float = 0.6) -> bool:
    m = _muestra(serie).astype(str)
    if len(m) == 0:
        return False
    return (m.str.match(_REGEX_EMAIL)).mean() >= umbral


def parece_telefono(serie: pd.Series, umbral: float = 0.7, min_d=7, max_d=15) -> bool:
    m = _muestra(serie).astype(str)
    if len(m) == 0:
        return False
    # Se descartan valores con un grupo de 4 digitos tipo anio (19xx/20xx):
    # una fecha como "2024-01-15" queda en 8 digitos al quitarle los
    # guiones y se confundiria con un telefono valido si no se excluye.
    con_anio = m.str.contains(r'\b(?:19|20)\d{2}\b', regex=True)
    m = m[~con_anio]
    if len(m) == 0:
        return False
    solo_digitos = m.str.replace(r"\D", "", regex=True)
    ok = solo_digitos.str.len().between(min_d, max_d)
    return ok.mean() >= umbral


def parece_fecha(serie: pd.Series, umbral: float = 0.7) -> bool:
    m = _muestra(serie)
    if len(m) == 0:
        return False
    if pd.api.types.is_datetime64_any_dtype(serie):
        return True
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        parseado = pd.to_datetime(m.astype(str), errors="coerce", dayfirst=True)
    return parseado.notna().mean() >= umbral


def parece_id(serie: pd.Series, umbral_unicidad: float = 0.95) -> bool:
    no_nulos = serie.dropna()
    if len(no_nulos) < 5:
        return False
    return no_nulos.nunique() / len(no_nulos) >= umbral_unicidad


# -----------------------------------------------------------------------------
# Rango de digitos de telefono/celular por pais (numero nacional, SIN codigo
# de pais). Antes el proyecto asumia siempre 8 digitos (formato de Costa
# Rica) sin importar el dataset. Esta tabla permite aceptar la mayoria de
# formatos de celular reales pais por pais, o -si no se especifica ninguno-
# caer en el rango internacional amplio (estandar E.164: 7 a 15 digitos)
# en vez de un unico pais fijo.
# -----------------------------------------------------------------------------
DIGITOS_TELEFONO_PAIS: dict[str, Tuple[int, int]] = {
    "cr": (8, 8), "costa_rica": (8, 8),
    "mx": (10, 10), "mexico": (10, 10),
    "co": (10, 10), "colombia": (10, 10),
    "ar": (10, 11), "argentina": (10, 11),
    "es": (9, 9), "espana": (9, 9), "spain": (9, 9),
    "us": (10, 10), "usa": (10, 10), "estados_unidos": (10, 10),
    "pa": (7, 8), "panama": (7, 8),
    "gt": (8, 8), "guatemala": (8, 8),
    "hn": (8, 8), "honduras": (8, 8),
    "ni": (8, 8), "nicaragua": (8, 8),
    "sv": (8, 8), "el_salvador": (8, 8),
    "cl": (9, 9), "chile": (9, 9),
    "pe": (9, 9), "peru": (9, 9),
    "ec": (9, 9), "ecuador": (9, 9),
    "ve": (10, 11), "venezuela": (10, 11),
    "br": (10, 11), "brasil": (10, 11), "brazil": (10, 11),
    "uy": (8, 9), "uruguay": (8, 9),
    "bo": (8, 8), "bolivia": (8, 8),
    "do": (10, 10), "republica_dominicana": (10, 10),
    "gb": (10, 10), "uk": (10, 10), "reino_unido": (10, 10),
    "de": (10, 11), "alemania": (10, 11), "germany": (10, 11),
    "fr": (9, 9), "francia": (9, 9), "france": (9, 9),
    "ca": (10, 10), "canada": (10, 10),
    "au": (9, 9), "australia": (9, 9),
}

# Rango "por defecto" cuando no se especifica ningun pais: estandar
# internacional E.164 (numero nacional significativo de 7 a 15 digitos).
DIGITOS_TELEFONO_INTERNACIONAL: Tuple[int, int] = (7, 15)

# -----------------------------------------------------------------------------
# Nombre visible -> clave de DIGITOS_TELEFONO_PAIS, para poblar el selector de
# paises en las interfaces (app.py, desktop_app.py). Antes cada interfaz tenia
# su propia copia de este diccionario (uno en app.py y otro en desktop_app.py,
# ambos con la misma lista escrita a mano dos veces): quedaba facil que se
# desincronizaran si se agregaba un pais en un lado y no en el otro (como paso
# con Australia). Centralizado aqui, ambas interfaces importan la misma
# fuente y siempre muestran el mismo listado.
# -----------------------------------------------------------------------------
PAISES_TELEFONO_DISPONIBLES: dict[str, str] = {
    "Costa Rica": "cr", "México": "mexico", "Colombia": "colombia",
    "Argentina": "argentina", "España": "espana", "Estados Unidos": "us",
    "Panamá": "panama", "Guatemala": "guatemala", "Honduras": "honduras",
    "Nicaragua": "nicaragua", "El Salvador": "el_salvador", "Chile": "chile",
    "Perú": "peru", "Ecuador": "ecuador", "Venezuela": "venezuela",
    "Brasil": "brasil", "Uruguay": "uruguay", "Bolivia": "bolivia",
    "República Dominicana": "republica_dominicana", "Reino Unido": "reino_unido",
    "Alemania": "alemania", "Francia": "francia", "Canadá": "canada",
    "Australia": "australia",
}


def rango_digitos_telefono(paises: Optional[Iterable[str]] = None) -> Tuple[int, int]:
    """(min, max) de digitos aceptados para telefono/celular.

    Sin `paises`: rango internacional amplio (7-15). Con `paises` (nombres o
    codigos ISO en cualquier combinacion de mayusculas/acentos, ej. "CR",
    "México", "Costa Rica"): la UNION de sus rangos tipicos, para aceptar en
    una misma columna la mayoria de formatos de celular de esos paises a la
    vez. Si ninguno de los paises dados se reconoce, cae al rango
    internacional en vez de fallar.
    """
    if not paises:
        return DIGITOS_TELEFONO_INTERNACIONAL
    mins, maxs = [], []
    for p in paises:
        clave = normalizar_nombre(p)
        if clave in DIGITOS_TELEFONO_PAIS:
            mn, mx = DIGITOS_TELEFONO_PAIS[clave]
            mins.append(mn)
            maxs.append(mx)
    if not mins:
        return DIGITOS_TELEFONO_INTERNACIONAL
    return (min(mins), max(maxs))


def detectar_columnas(
    df: pd.DataFrame,
    patrones_nombre: Iterable[str],
    detector_contenido=None,
    excluir: Optional[Iterable[str]] = None,
    excluir_por_nombre: Optional[Iterable[str]] = None,
) -> List[str]:
    """Nivel 1 (nombre) primero; si no encuentra NADA y hay un
    `detector_contenido` (funcion serie->bool), intenta Nivel 2 sobre las
    columnas de texto que no estén ya excluidas (ej. columnas ID, u otras
    ya asignadas a otra regla).

    `excluir_por_nombre` (opcional) es una lista de patrones de NOMBRE
    (ej. PATRONES_NO_TELEFONO) que descartan una columna del Nivel 2 aunque
    su contenido parezca calzar: sirve para que un "chasis", "motor" o
    "numero_de_serie" con muchos digitos no se confunda con un telefono
    solo por la cantidad de digitos que tiene. No afecta al Nivel 1: si la
    columna ya se detecto por nombre como la regla que se esta buscando
    (ej. una columna que de verdad se llama "telefono"), este parametro no
    la excluye."""
    excluir = set(excluir or [])
    por_nombre = [c for c in columnas_por_patron(df, patrones_nombre) if c not in excluir]
    if por_nombre or detector_contenido is None:
        return por_nombre
    candidatas = []
    for c in df.columns:
        if c in excluir or es_columna_id(c):
            continue
        if excluir_por_nombre is not None and coincide_patron(c, excluir_por_nombre):
            continue
        # Nivel 2 es un respaldo pensado para columnas de TEXTO (nombres
        # de columna que no dicen nada, ej. "contacto_1", "campo_7").
        # Las columnas numéricas (montos, cantidades, precios) quedan
        # fuera: un monto grande en colones puede tener 7-15 dígitos y
        # confundirse con un teléfono si no se excluye por tipo de dato.
        if pd.api.types.is_numeric_dtype(df[c]):
            continue
        try:
            if detector_contenido(df[c]):
                candidatas.append(c)
        except Exception:
            continue
    return candidatas



def columnas_fecha_por_nombre(df: pd.DataFrame) -> List[str]:
    """Nivel 1 de deteccion de columnas de fecha por nombre, con una
    salvedad respecto a columnas_por_patron(df, PATRONES_FECHA): las
    palabras "debiles" (ver PATRONES_FECHA_DEBIL) solo cuentan si la
    columna no es ademas un identificador (folio, codigo, clave...) --
    evita marcar "folio_pago" o "codigo_cobro" como columna de fecha.
    Las palabras "fuertes" (fecha, date, dob...) siempre cuentan."""
    cols = []
    for c in df.columns:
        if coincide_patron(c, PATRONES_FECHA_FUERTE):
            cols.append(c)
        elif coincide_patron(c, PATRONES_FECHA_DEBIL) and not es_columna_id(c):
            cols.append(c)
    return cols


def detectar_columnas_fecha(df: pd.DataFrame, detector_contenido=None,
                             excluir: Optional[Iterable[str]] = None) -> List[str]:
    """Igual que detectar_columnas(df, PATRONES_FECHA, ...) pero usando
    columnas_fecha_por_nombre() para el Nivel 1 (evita el falso positivo
    de "folio_pago"/"codigo_cobro" descrito ahi). El Nivel 2 (por
    contenido) es identico al de detectar_columnas()."""
    excluir = set(excluir or [])
    por_nombre = [c for c in columnas_fecha_por_nombre(df) if c not in excluir]
    if por_nombre or detector_contenido is None:
        return por_nombre
    candidatas = []
    for c in df.columns:
        if c in excluir or es_columna_id(c):
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            continue
        try:
            if detector_contenido(df[c]):
                candidatas.append(c)
        except Exception:
            continue
    return candidatas



# Columnas a excluir del chequeo estadistico de atipicos (IQR / Z-score)
# -----------------------------------------------------------------------------
def columnas_excluir_de_atipicos(df: pd.DataFrame) -> List[str]:
    """Columnas que NO deben pasar por el chequeo de atipicos por IQR/Z-score:
    identificadores (id, codigo, folio, clave...) y telefono/fax.

    Ninguna de ellas es una magnitud continua -- no existe una
    "distribucion normal" esperada para un codigo postal o un numero de
    telefono -- asi que aplicarles IQR/Z-score solo genera falsos
    positivos (ej. un codigo postal valido de otra ciudad, o un telefono
    con codigo de pais, marcados como "atipicos" por estar numericamente
    lejos del grueso de los datos). El chequeo correcto para telefonos ya
    existe en detectar_telefonos_invalidos (valida formato/longitud, no
    cercania estadistica a la media).
    """
    cols_id = [c for c in df.columns if es_columna_id(c)]
    cols_tel = detectar_columnas(df, PATRONES_TELEFONO, parece_telefono, excluir=cols_id,
                                  excluir_por_nombre=PATRONES_NO_TELEFONO)
    return list(dict.fromkeys(cols_id + cols_tel))

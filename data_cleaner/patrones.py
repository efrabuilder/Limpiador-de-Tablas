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
from typing import Iterable, List, Optional

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
# Vocabulario ampliado (bilingue espanol/ingles + sinonimos comunes de
# distintos rubros: retail, nomina, salud, logistica, educacion).
# -----------------------------------------------------------------------------
PATRONES_EMAIL = (
    "email", "correo", "e_mail", "mail", "correo_electronico",
)
PATRONES_TELEFONO = (
    "telefono", "phone", "celular", "movil", "whatsapp",
    "numero_telefono", "phone_number", "tel", "mobile",
)
PATRONES_FECHA = (
    "fecha", "date", "fec", "dob", "birth", "nacimiento",
    "created_at", "updated_at", "timestamp", "vencimiento", "expiry",
    "ingreso", "egreso",
)
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


def detectar_columnas(
    df: pd.DataFrame,
    patrones_nombre: Iterable[str],
    detector_contenido=None,
    excluir: Optional[Iterable[str]] = None,
) -> List[str]:
    """Nivel 1 (nombre) primero; si no encuentra NADA y hay un
    `detector_contenido` (funcion serie->bool), intenta Nivel 2 sobre las
    columnas de texto que no estén ya excluidas (ej. columnas ID, u otras
    ya asignadas a otra regla)."""
    excluir = set(excluir or [])
    por_nombre = [c for c in columnas_por_patron(df, patrones_nombre) if c not in excluir]
    if por_nombre or detector_contenido is None:
        return por_nombre
    candidatas = []
    for c in df.columns:
        if c in excluir or es_columna_id(c):
            continue
        try:
            if detector_contenido(df[c]):
                candidatas.append(c)
        except Exception:
            continue
    return candidatas

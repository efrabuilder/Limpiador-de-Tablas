# -*- coding: utf-8 -*-
"""
limpieza_guiada.py
==================
Limpieza paso a paso de una tabla, con foco en los nulos. Sigue el mismo
orden del notebook LimpiezadeDatos.ipynb:

    1. Diagnostico de nulos y vacios (con semaforo por columna)
    2. Estandarizar nombres de columna (snake_case)
    3. Vacios -> nulos reales (NaN)
    4. Estandarizar texto (minusculas, sin espacios de mas, sin comillas)
    5. Convertir columnas numericas (coma decimal, simbolos de moneda)
    6. Parsear coordenadas (latitud / longitud)
    7. Fechas en un solo formato (AAAA-MM-DD por defecto)
    8. Unir valores que significan lo mismo (M / male -> M, f / female -> F...)
    9. Regla de nulos por columna (rellenar, mediana, eliminar fila...)
   10. Asegurar que no queden vacios: los nulos validos se rellenan con una sola
       palabra elegida («No indica» por defecto); numeros con mediana, 0 o la palabra
   11. Auditoria final (antes / despues)

Reusa los patrones de patrones.py para reconocer el rol de cada columna
(id, email, telefono, fecha, coordenada, numerica, texto) y sugerir una
regla de nulos para cada una.

Las funciones auxiliares (es_nulo, a_numero, parsear_coordenada,
nombres_snake, limpiar_texto, unificar_fechas, aplicar_regla, rellenar_restantes) no dependen del resto del
paquete a proposito: generar_script_limpieza() las copia tal cual dentro
del script que entrega, asi el script hace exactamente lo mismo que la app.
"""
from __future__ import annotations

import inspect
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .patrones import (
    PATRONES_EMAIL,
    PATRONES_TELEFONO,
    PATRONES_NOMBRE_PROPIO,
    coincide_patron,
    columnas_fecha_por_nombre,
    es_columna_coordenada,
    es_columna_id,
    tipo_coordenada,
)

# Textos que cuentan como "nulo" ademas de la celda realmente vacia. "n/a"
# (no aplica / no disponible) ya viene incluido: casi nunca es un dato real. Los
# de la lista extra son opcionales porque "na", "-" o "unknown" podrian ser datos
# reales; la app avisa si los encuentra en la tabla.
TOKENS_NULOS_BASE = ("", "nan", "none", "null", "n/a")
TOKENS_NULOS_EXTRA = ("na", "-", "--", "s/d", "sin dato", "unknown", "desconocido",
                      "not available", "no disponible")

# Valores que significan lo mismo, para proponer su union (destino -> variantes, ya sin
# tildes ni mayusculas). Un grupo solo se propone si cubre casi toda la columna.
SINONIMOS = {
    "genero": {
        "M": ("m", "male", "masculino", "hombre", "man", "h"),
        "F": ("f", "female", "femenino", "mujer", "woman"),
        "Other": ("other", "o", "otro", "divers", "diverse", "non binary", "nonbinary",
                  "no binario"),
    },
    "si_no": {
        "Sí": ("si", "yes", "y", "true", "verdadero", "s"),
        "No": ("no", "n", "false", "falso"),
    },
}
# Textos que dentro de una de esas columnas quieren decir «sin dato».
TEXTOS_SIN_DATO = ("no indica", "sin dato", "desconocido", "unknown", "prefiero no decir",
                   "prefiero no indicar", "not available", "no disponible", "na", "n a")

# Texto con el que se rellenan los nulos validos (los que no son un error: no hay
# reclamo, no hubo encuesta, la venta fue directa...).
TEXTO_NO_INDICA = "No indica"

# Reglas de nulos que se pueden aplicar a una columna.
REGLAS_NULOS = {
    "dejar": "Dejar como está",
    "no_indica": "Rellenar con «No indica» (nulo válido)",
    "valor_fijo": "Rellenar con un valor fijo",
    "cero": "Rellenar con 0",
    "mediana": "Rellenar con la mediana",
    "media": "Rellenar con la media",
    "moda": "Rellenar con el valor más frecuente",
    "mediana_por_grupo": "Mediana por grupo (elegir columna de grupo)",
    "eliminar_fila": "Eliminar las filas con nulo",
    "eliminar_columna": "Eliminar la columna",
}

ROLES = {
    "id": "identificador / llave",
    "coordenada": "coordenada",
    "email": "email",
    "telefono": "teléfono",
    "fecha": "fecha",
    "numerica": "número",
    "texto": "texto",
}

_ROL_POR_TEXTO = {texto: clave for clave, texto in ROLES.items()}

# Palabras que marcan una columna como llave aunque no se llame "id".
_PATRONES_LLAVE_EXTRA = ("postal", "zip", "zipcode", "cp", "codigo_postal", "postal_code")


# =============================================================================
# Funciones auxiliares autocontenidas (se copian al script generado).
# Sin anotaciones de tipo para que el script funcione en cualquier Python 3.
# =============================================================================

def es_nulo(serie, tokens=TOKENS_NULOS_BASE):
    """Mascara booleana: True donde la celda es nula (NaN) o es un texto
    vacio / uno de los `tokens` (sin importar mayusculas ni espacios)."""
    texto = serie.astype("string").str.strip().str.lower()  # texto normalizado
    return (serie.isna() | texto.isin(list(tokens))).fillna(False).astype(bool)


def a_numero(serie):
    """Convierte a numero tolerando simbolos de moneda y separadores:
    '1,234.56' -> 1234.56, '1.234,56' -> 1234.56, '7,5' -> 7.5.
    Lo que trae letras (como un codigo 'P10') o no se puede leer queda como NaN."""
    if pd.api.types.is_datetime64_any_dtype(serie):  # una fecha no es un numero
        return pd.Series(float("nan"), index=serie.index, dtype="float64")

    def limpiar(v):
        if not isinstance(v, str):
            return v
        v = re.sub(r"[\s$€£₡¥]|crc|usd|eur", "", v.strip(), flags=re.I)  # fuera moneda y espacios
        if not re.fullmatch(r"-?[\d.,]+", v):  # con letras ('P10') no es un numero
            return None
        i_coma, i_punto = v.rfind(","), v.rfind(".")
        if i_coma != -1 and i_punto != -1:  # trae los dos: el ultimo es el decimal
            if i_coma > i_punto:
                v = v.replace(".", "").replace(",", ".")
            else:
                v = v.replace(",", "")
        elif i_coma != -1:  # solo coma: miles si son grupos de 3 digitos
            partes = v.split(",")
            if all(len(p) == 3 for p in partes[1:]):
                v = v.replace(",", "")
            else:
                v = v.replace(",", ".")
        return v

    return pd.to_numeric(serie.map(limpiar), errors="coerce")


def parsear_coordenada(valor, tipo, minimo=None, maximo=None):
    """Latitud (tipo='latitud') o longitud (tipo='longitud') como numero.
    Acepta coma decimal. El valor debe caer entre `minimo` y `maximo`
    (por defecto -90..90 para latitud y -180..180 para longitud; se puede
    pasar el rango del pais para ser mas preciso, ej. 5.5..15.5 en
    Alemania). Si viene fuera de rango porque le falta el punto decimal
    (ej. 9953281 en vez de 9.953281), se divide entre 10 hasta que entre.
    Lo que no es un numero, o no entra en rango ni asi, queda como NaN."""
    if valor is None or (not isinstance(valor, str) and pd.isna(valor)):
        return np.nan
    try:
        numero = float(str(valor).strip().replace(",", "."))
    except ValueError:
        return np.nan
    if not np.isfinite(numero):
        return np.nan
    limite = 180 if tipo == "longitud" else 90
    minimo = -limite if minimo is None else minimo
    maximo = limite if maximo is None else maximo
    for _ in range(12):  # hasta 12 divisiones entre 10
        if minimo <= numero <= maximo:
            return numero
        numero /= 10
    return np.nan


def nombres_snake(columnas):
    """Nombres de columna en snake_case: minusculas, sin puntuacion, con
    guion bajo en vez de espacios. Si dos quedan iguales, agrega _2, _3..."""
    serie = (
        pd.Series([str(c) for c in columnas], dtype="object")
        .str.strip()
        .str.lower()
        .str.replace(r"[^\w\s]", "", regex=True)
        .str.replace(r"\s+", "_", regex=True)
        .str.replace(r"_+", "_", regex=True)
        .str.strip("_")
    )
    nombres, usados = [], {}
    for n, original in zip(serie.tolist(), columnas):
        n = n or "columna"
        if n in usados:
            usados[n] += 1
            n = f"{n}_{usados[n]}"
        else:
            usados[n] = 1
        nombres.append(n)
    return nombres


def limpiar_texto(serie, minusculas=True, espacios=True, comillas=True):
    """Estandariza una columna de texto sin tocar los nulos: quita espacios
    a los lados, comillas de los extremos, espacios repetidos y, si se pide,
    pasa a minusculas."""
    nulos = serie.isna()
    s = serie.astype("string").str.strip()
    if comillas:
        s = s.str.strip('"').str.strip()  # comillas dobles de los extremos
    if minusculas:
        s = s.str.lower()
    if espacios:
        s = s.str.replace(r"\s+", " ", regex=True)  # un solo espacio entre palabras
    return s.astype(object).where(~nulos, np.nan)


def unificar_fechas(serie, formato="%Y-%m-%d", dia_primero=True):
    """Pasa una columna de fechas escritas de varias maneras (2025-01-31, 31/01/2025,
    31-1-2025, Jan 31 2025...) a un solo formato de texto, `formato` (por defecto
    AAAA-MM-DD). Las que empiezan por el anio se leen siempre como anio-mes-dia; las demas
    como dia/mes/anio si `dia_primero`, o mes/dia/anio si no. Lo que no se pueda leer
    como fecha queda como nulo."""
    if pd.api.types.is_datetime64_any_dtype(serie):
        fechas = serie
    else:
        texto = serie.astype("string").str.strip()
        anio_primero = texto.str.match(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", na=False)
        fechas = pd.to_datetime(texto.where(~anio_primero), errors="coerce",
                                dayfirst=dia_primero, format="mixed")
        fechas = fechas.fillna(pd.to_datetime(texto.where(anio_primero), errors="coerce",
                                              dayfirst=False, format="mixed"))
    return fechas.dt.strftime(formato).astype(object).where(fechas.notna(), np.nan)


def decimales_de(serie, maximo=6):
    """Cuantos decimales usan los datos de la serie (de 0 a `maximo`). Sirve para
    redondear lo que se calcula (media, mediana) y no dejar ruido como 9.942499999999999."""
    datos = serie.dropna()
    for d in range(maximo + 1):
        if (datos.round(d) == datos).all():
            return d
    return maximo


def aplicar_regla(df, columna, regla, valor="", grupo="", tokens=TOKENS_NULOS_BASE):
    """Aplica una regla de nulos a una columna. Devuelve (df_nuevo, info).
    Los nulos son los de es_nulo(): NaN, vacios y los `tokens`.

    Reglas: dejar, no_indica (rellena con `valor`, o con «No indica» si no se da),
    valor_fijo, cero, mediana, media, moda, mediana_por_grupo (usa la columna
    `grupo`), eliminar_fila, eliminar_columna. Para mediana/media la columna debe
    ser numerica; si hay valores que no se pueden leer como numero, avisa en vez
    de perderlos en silencio. Lo que se calcula (media, mediana) se redondea a los
    decimales de los datos (minimo 2, maximo 6) para no dejar ruido decimal."""
    df = df.copy()
    mascara = es_nulo(df[columna], tokens)
    n_nulos = int(mascara.sum())
    info = {"afectadas": 0, "eliminadas": 0, "mensaje": ""}

    if regla == "dejar" or n_nulos == 0:
        info["mensaje"] = f"{columna}: sin cambios ({n_nulos} nulos)"
        return df, info

    if regla == "eliminar_columna":
        df = df.drop(columns=[columna])
        info["mensaje"] = f"{columna}: columna eliminada ({n_nulos} nulos)"
        return df, info

    if regla == "eliminar_fila":
        df = df.loc[~mascara].reset_index(drop=True)
        info["eliminadas"] = n_nulos
        info["mensaje"] = f"{columna}: {n_nulos} filas eliminadas"
        return df, info

    if regla in ("valor_fijo", "no_indica", "cero", "moda"):
        if regla == "cero":
            relleno = 0
        elif regla == "no_indica":
            relleno = valor if str(valor).strip() else "No indica"
        elif regla == "moda":
            frecuentes = df.loc[~mascara, columna].mode()
            if len(frecuentes) == 0:
                raise ValueError(f"'{columna}' no tiene ningun valor para calcular la moda.")
            relleno = frecuentes.iloc[0]
        else:
            relleno = valor
        serie = df[columna]
        if pd.api.types.is_numeric_dtype(serie):  # columna numerica real
            try:
                relleno = float(relleno)
                if relleno.is_integer() and (serie.dropna() % 1 == 0).all():
                    relleno = int(relleno)
            except (TypeError, ValueError):
                serie = serie.astype(object)  # el valor es texto: la columna pasa a texto
        elif regla == "cero":
            relleno = "0"  # columna de texto: el cero se escribe como texto
        df[columna] = serie.where(~mascara, relleno)
        info["afectadas"] = n_nulos
        info["mensaje"] = f"{columna}: {n_nulos} nulos -> {relleno!r}"
        return df, info

    if regla in ("mediana", "media", "mediana_por_grupo"):
        numeros = a_numero(df[columna].where(~mascara))
        perdidos = (~mascara) & numeros.isna()  # tenia dato pero no es numero
        if perdidos.any():
            ejemplos = df.loc[perdidos, columna].astype(str).unique()[:3].tolist()
            raise ValueError(
                f"'{columna}' tiene {int(perdidos.sum())} valores que no son numeros "
                f"(ej. {ejemplos}). Conviertala a numerica antes o use otra regla."
            )
        if numeros.notna().sum() == 0:
            raise ValueError(f"'{columna}' no tiene ningun numero para calcular la {regla}.")
        decimales = min(max(decimales_de(numeros), 2), 6)  # sin ruido tipo 9.942499999999999
        if regla == "media":
            completa = numeros.fillna(round(float(numeros.mean()), decimales))
        elif regla == "mediana":
            completa = numeros.fillna(round(float(numeros.median()), decimales))
        else:
            if grupo not in df.columns:
                raise ValueError("Elija una columna de grupo valida para la mediana por grupo.")
            por_grupo = numeros.groupby(df[grupo], dropna=False).transform(
                lambda s: s.fillna(round(float(s.median()), decimales)))
            completa = por_grupo.fillna(round(float(numeros.median()), decimales))  # grupos sin datos: mediana global
        if (completa % 1 == 0).all() and completa.abs().max() < 2 ** 53:  # todo entero: sin decimales
            completa = completa.astype("int64")
        df[columna] = completa
        info["afectadas"] = n_nulos
        info["mensaje"] = f"{columna}: {n_nulos} nulos -> {regla}"
        return df, info

    raise ValueError(f"Regla desconocida: {regla}")


def rellenar_restantes(df, columnas_texto, columnas_numericas, numeros="mediana",
                       texto="No indica", tokens=TOKENS_NULOS_BASE):
    """Cierre sin vacios: rellena los nulos que sobren. Las columnas de texto
    con `texto` («No indica») y las numericas con la mediana, con 0
    (numeros='cero') o tambien con `texto` (numeros='palabra': la columna pasa a
    texto). Si una columna numerica no se puede resolver como numero, se rellena
    con `texto`."""
    for col in columnas_texto:
        if col in df.columns:
            df, _ = aplicar_regla(df, col, "no_indica", valor=texto, tokens=tokens)
    for col in columnas_numericas:
        if col in df.columns:
            if numeros == "palabra":
                df, _ = aplicar_regla(df, col, "no_indica", valor=texto, tokens=tokens)
                continue
            try:
                df, _ = aplicar_regla(df, col, "cero" if numeros == "cero" else "mediana",
                                      tokens=tokens)
            except ValueError:
                df, _ = aplicar_regla(df, col, "no_indica", valor=texto, tokens=tokens)
    return df


# Orden en que se copian al script generado (cada una puede usar las anteriores).
_FUNCIONES_SCRIPT = (es_nulo, a_numero, parsear_coordenada, nombres_snake,
                     limpiar_texto, unificar_fechas, decimales_de, aplicar_regla, rellenar_restantes)


# =============================================================================
# Diagnostico
# =============================================================================

def semaforo(pct: float) -> str:
    """Icono segun el porcentaje de problemas de la columna."""
    if pct == 0:
        return "✅"
    if pct < 5:
        return "🟢"
    if pct < 30:
        return "🟡"
    if pct < 70:
        return "🟠"
    return "🔴"


def _tipo_legible(serie: pd.Series) -> str:
    if pd.api.types.is_datetime64_any_dtype(serie):
        return "fecha"
    if pd.api.types.is_bool_dtype(serie):
        return "booleano"
    if pd.api.types.is_numeric_dtype(serie):
        return "número"
    return "texto"


def _parece_numerica(serie: pd.Series, mascara_nulos: pd.Series, umbral: float = 0.9) -> bool:
    """True si la columna es numerica, o texto donde casi todo (por defecto
    90%) se lee como numero (mira solo una muestra para ir rapido)."""
    if pd.api.types.is_numeric_dtype(serie) and not pd.api.types.is_bool_dtype(serie):
        return True
    muestra = serie[~mascara_nulos].head(2000)
    if len(muestra) == 0:
        return False
    return bool(a_numero(muestra).notna().mean() >= umbral)


def rol_columna(df: pd.DataFrame, col, cols_fecha=None, tokens=TOKENS_NULOS_BASE,
                umbral_numerico: float = 0.9) -> str:
    """Que es esta columna segun su nombre y contenido: id, coordenada,
    email, telefono, fecha, numerica o texto (ver ROLES)."""
    if cols_fecha is None:
        cols_fecha = columnas_fecha_por_nombre(df)
    if es_columna_coordenada(col):
        return "coordenada"
    if es_columna_id(col) or coincide_patron(col, _PATRONES_LLAVE_EXTRA):
        return "id"
    if coincide_patron(col, PATRONES_EMAIL):
        return "email"
    if coincide_patron(col, PATRONES_TELEFONO):
        return "telefono"
    if col in cols_fecha or pd.api.types.is_datetime64_any_dtype(df[col]):
        return "fecha"
    if _parece_numerica(df[col], es_nulo(df[col], tokens), umbral_numerico):
        return "numerica"
    return "texto"


def sugerir_regla(rol: str, pct_nulos: float) -> Tuple[str, str]:
    """(regla, valor) sugerida para una columna segun su rol. Es solo un
    punto de partida: se puede cambiar en la tabla."""
    if pct_nulos == 0:
        return "dejar", ""
    if pct_nulos >= 100:
        return "eliminar_columna", ""
    if rol == "id" and pct_nulos >= 5:
        # Muchos vacios en un codigo: casi seguro es un nulo valido (sin reclamo,
        # sin broker...), no un error. Borrar tantas filas seria un desastre.
        return "no_indica", ""
    if rol in ("id", "coordenada"):
        return "eliminar_fila", ""  # sin llave o sin ubicacion la fila no sirve
    if rol == "numerica":
        return "mediana", ""
    # correo, telefono, fecha y texto: el nulo suele ser valido (no dio el dato, aun no
    # ocurre...), asi que se rellena con la palabra elegida («No indica» por defecto).
    return "no_indica", ""  # nulo valido en texto: «No indica»


def diagnostico_nulos(df: pd.DataFrame, tokens=TOKENS_NULOS_BASE) -> pd.DataFrame:
    """Tabla con una fila por columna: tipo, cuantas celdas estan vacias
    (texto vacio o token), cuantas son nulos reales (NaN), total, porcentaje,
    semaforo y rol detectado. Ordenada de la mas a la menos sucia."""
    cols_fecha = columnas_fecha_por_nombre(df)
    filas = []
    total_filas = max(len(df), 1)
    for col in df.columns:
        serie = df[col]
        reales = serie.isna()
        total = es_nulo(serie, tokens)
        pct = round(float(total.sum()) / total_filas * 100, 2)
        filas.append({
            "columna": col,
            "tipo": _tipo_legible(serie),
            "rol": ROLES[rol_columna(df, col, cols_fecha, tokens)],
            "vacios_texto": int((total & ~reales).sum()),
            "nulos_reales": int(reales.sum()),
            "total": int(total.sum()),
            "porcentaje": pct,
            "semaforo": semaforo(pct),
        })
    tabla = pd.DataFrame(filas).set_index("columna")
    return tabla.sort_values("total", ascending=False, kind="stable")


def tabla_de_reglas(df: pd.DataFrame, tokens=TOKENS_NULOS_BASE,
                    palabra: str = TEXTO_NO_INDICA) -> pd.DataFrame:
    """Tabla editable de reglas: una fila por columna con su rol, sus nulos
    y la regla sugerida (columnas regla / valor / grupo editables). En las
    reglas «no_indica» el valor sugerido es `palabra`."""
    diag = diagnostico_nulos(df, tokens)
    filas = []
    for col in df.columns:
        rol_clave = _ROL_POR_TEXTO[diag.loc[col, "rol"]]
        pct = float(diag.loc[col, "porcentaje"])
        regla, valor = sugerir_regla(rol_clave, pct)
        if regla == "no_indica":
            valor = palabra
        filas.append({
            "columna": col,
            "rol": diag.loc[col, "rol"],
            "nulos": int(diag.loc[col, "total"]),
            "%": pct,
            "semaforo": diag.loc[col, "semaforo"],
            "regla": regla,
            "valor": valor,
            "grupo": "",
        })
    return pd.DataFrame(filas)


def contar_coordenadas_fuera_de_rango(serie: pd.Series, tipo: str, minimo=None,
                                      maximo=None) -> int:
    """Cuantos valores de la columna caen fuera del rango (por defecto
    -90..90 para latitud y -180..180 para longitud), por ejemplo por venir
    sin punto decimal. Los que no son numeros no se cuentan."""
    limite = 180 if tipo == "longitud" else 90
    minimo = -limite if minimo is None else minimo
    maximo = limite if maximo is None else maximo
    return int(serie.map(lambda v: _fuera_de_rango(v, minimo, maximo)).sum())


def _fuera_de_rango(v, minimo, maximo) -> bool:
    try:
        numero = float(str(v).strip().replace(",", "."))
    except ValueError:
        return False
    return bool(np.isfinite(numero) and not (minimo <= numero <= maximo))


def detectar_textos_tipo_nulo(df: pd.DataFrame, tokens=TOKENS_NULOS_BASE,
                              candidatos=TOKENS_NULOS_EXTRA) -> pd.DataFrame:
    """Textos que suelen significar «sin dato» (unknown, -, na...), que estan en la
    tabla y todavia NO cuentan como nulo. Una fila por texto: cuantas celdas y en
    que columnas. Sirve para avisar antes de que pasen como datos validos."""
    buscados = [t for t in candidatos if t not in tokens]
    cuentas, columnas = {}, {}
    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]) or pd.api.types.is_datetime64_any_dtype(df[col]):
            continue
        texto = df[col].dropna().astype("string").str.strip().str.lower()
        for valor, n in texto[texto.isin(buscados)].value_counts().items():
            cuentas[valor] = cuentas.get(valor, 0) + int(n)
            columnas.setdefault(valor, []).append(f"{col} ({int(n)})")
    filas = [{"texto": t, "celdas": n, "columnas": ", ".join(columnas[t][:5])
              + (" …" if len(columnas[t]) > 5 else "")} for t, n in cuentas.items()]
    return pd.DataFrame(filas, columns=["texto", "celdas", "columnas"]).sort_values(
        "celdas", ascending=False, ignore_index=True)


# =============================================================================
# Pasos de limpieza
# =============================================================================

@dataclass
class Paso:
    """Un paso ejecutado: titulo, lo que paso (para mostrar) y el codigo
    pandas equivalente (para el script)."""
    titulo: str
    detalle: str
    codigo: str
    advertencia: str = ""  # aviso para mostrar destacado (algo que conviene revisar)
    reglas: Optional[List[Dict]] = field(default=None)  # reglas aplicadas (para el diccionario)


def columnas_texto_sugeridas(df: pd.DataFrame, tokens=TOKENS_NULOS_BASE) -> List[str]:
    """Columnas de texto que conviene estandarizar: texto y email, sin
    identificadores, telefonos, fechas ni nombres propios (personas,
    empresas, lugares) para no perder sus mayusculas."""
    cols_fecha = columnas_fecha_por_nombre(df)
    sugeridas = []
    for col in df.columns:
        rol = rol_columna(df, col, cols_fecha, tokens, umbral_numerico=0.6)
        if rol not in ("texto", "email"):
            continue
        if coincide_patron(col, PATRONES_NOMBRE_PROPIO):
            continue
        sugeridas.append(col)
    return sugeridas


def paso_nombres_columnas(df: pd.DataFrame) -> Tuple[pd.DataFrame, Paso]:
    nuevos = nombres_snake(list(df.columns))
    cambios = [(a, b) for a, b in zip(df.columns, nuevos) if a != b]
    df = df.copy()
    df.columns = nuevos
    detalle = (", ".join(f"{a} → {b}" for a, b in cambios[:8]) +
               (f" (y {len(cambios) - 8} más)" if len(cambios) > 8 else "")) if cambios \
        else "Los nombres ya estaban en snake_case."
    return df, Paso("Nombres de columna en snake_case", detalle,
                    "df.columns = nombres_snake(df.columns)")


def paso_vacios_a_nan(df: pd.DataFrame, tokens=TOKENS_NULOS_BASE) -> Tuple[pd.DataFrame, Paso]:
    df = df.copy()
    convertidas = {}
    for col in df.columns:
        mascara = es_nulo(df[col], tokens)
        ya_nan = df[col].isna()
        n = int((mascara & ~ya_nan).sum())
        if n:
            convertidas[col] = n
        if mascara.any():
            df[col] = df[col].where(~mascara)
    detalle = (f"{sum(convertidas.values())} celdas vacías pasaron a nulo real en "
               f"{len(convertidas)} columnas.") if convertidas else "No había celdas vacías como texto."
    codigo = ("for col in df.columns:\n"
              "    df[col] = df[col].where(~es_nulo(df[col], TOKENS_NULOS))")
    return df, Paso("Vacíos a nulos reales (NaN)", detalle, codigo)


def paso_texto(df: pd.DataFrame, columnas: List[str], minusculas=True, espacios=True,
               comillas=True) -> Tuple[pd.DataFrame, Paso]:
    df = df.copy()
    for col in columnas:
        df[col] = limpiar_texto(df[col], minusculas, espacios, comillas)
    detalle = f"Aplicado a {len(columnas)} columnas: " + ", ".join(columnas[:10]) + \
        (" …" if len(columnas) > 10 else "")
    codigo = (f"COLUMNAS_TEXTO = {list(columnas)!r}\n"
              f"for col in COLUMNAS_TEXTO:\n"
              f"    df[col] = limpiar_texto(df[col], minusculas={minusculas}, "
              f"espacios={espacios}, comillas={comillas})")
    return df, Paso("Estandarizar texto", detalle, codigo)


def paso_numericas(df: pd.DataFrame, columnas: List[str],
                   tokens=TOKENS_NULOS_BASE) -> Tuple[pd.DataFrame, Paso]:
    df = df.copy()
    lineas = []
    for col in columnas:
        antes = int((~es_nulo(df[col], tokens)).sum())  # celdas con dato real
        convertida = a_numero(df[col])
        despues = int(convertida.notna().sum())
        perdidos = antes - despues
        lineas.append(f"{col}: {perdidos} valores no se pudieron leer como número y quedaron nulos"
                      if perdidos else f"{col}: convertida sin perder datos")
        df[col] = convertida
    codigo = (f"COLUMNAS_NUMERICAS = {list(columnas)!r}\n"
              f"for col in COLUMNAS_NUMERICAS:\n"
              f"    df[col] = a_numero(df[col])")
    return df, Paso("Convertir columnas numéricas", "\n".join(lineas), codigo)


def paso_coordenadas(df: pd.DataFrame, col_latitud: Optional[str] = None,
                     col_longitud: Optional[str] = None, rango_latitud=None,
                     rango_longitud=None) -> Tuple[pd.DataFrame, Paso]:
    """Convierte latitud/longitud a numero y corrige las que vienen sin
    punto decimal. `rango_latitud` / `rango_longitud` son (minimo, maximo)
    opcionales con el rango esperado de los datos."""
    df = df.copy()
    lineas, codigo, avisos = [], [], []
    for col, tipo, rango in ((col_latitud, "latitud", rango_latitud),
                             (col_longitud, "longitud", rango_longitud)):
        if not col:
            continue
        limite = 180 if tipo == "longitud" else 90
        minimo, maximo = rango if rango else (-limite, limite)
        tenian_dato = int(df[col].notna().sum())
        fuera = contar_coordenadas_fuera_de_rango(df[col], tipo, minimo, maximo)
        originales = df[col].copy()
        df[col] = df[col].map(lambda v, t=tipo, a=minimo, b=maximo: parsear_coordenada(v, t, a, b))
        nulos_nuevos = tenian_dato - int(df[col].notna().sum())
        reescaladas = originales.map(lambda v: _fuera_de_rango(v, minimo, maximo)) & df[col].notna()
        if reescaladas.any():
            ejemplos = ", ".join(f"{o} → {n:g}" for o, n in
                                 zip(originales[reescaladas].head(3), df.loc[reescaladas, col].head(3)))
            avisos.append(f"«{col}»: {int(reescaladas.sum())} valores fuera de rango se corrigieron "
                          f"dividiéndolos entre 10 hasta entrar en el rango ({ejemplos}). Es lo esperado "
                          f"si venían sin punto decimal; si no, revise esos valores.")
        minimo_real, maximo_real = df[col].min(), df[col].max()
        valores = (f"mín {minimo_real:.4f} / máx {maximo_real:.4f}"
                   if pd.notna(minimo_real) else "sin valores")
        lineas.append(f"{col} ({tipo}, rango {minimo}..{maximo}): {fuera} fuera de rango, "
                      f"{nulos_nuevos} no se pudieron arreglar y quedaron nulos; {valores}")
        codigo.append(f'df["{col}"] = df["{col}"].map('
                      f'lambda v: parsear_coordenada(v, "{tipo}", {minimo!r}, {maximo!r}))')
    return df, Paso("Parsear coordenadas", "\n".join(lineas), "\n".join(codigo),
                    advertencia="\n".join(avisos))


def paso_regla(df: pd.DataFrame, columna: str, regla: str, valor: str = "", grupo: str = "",
               tokens=TOKENS_NULOS_BASE) -> Tuple[pd.DataFrame, Optional[Paso]]:
    df_nuevo, info = aplicar_regla(df, columna, regla, valor, grupo, tokens)
    if regla == "dejar":
        return df_nuevo, None
    codigo = (f"df, _ = aplicar_regla(df, {columna!r}, {regla!r}, valor={valor!r}, "
              f"grupo={grupo!r}, tokens=TOKENS_NULOS)")
    return df_nuevo, Paso(f"Nulos de «{columna}»: {REGLAS_NULOS[regla]}", info["mensaje"], codigo)


FORMATOS_FECHA = {
    "AAAA-MM-DD  (2025-12-31)": "%Y-%m-%d",
    "DD/MM/AAAA  (31/12/2025)": "%d/%m/%Y",
    "MM/DD/AAAA  (12/31/2025)": "%m/%d/%Y",
    "DD-MM-AAAA  (31-12-2025)": "%d-%m-%Y",
}


def paso_fechas(df: pd.DataFrame, columnas: List[str], formato: str = "%Y-%m-%d",
                dia_primero: bool = True, tokens=TOKENS_NULOS_BASE) -> Tuple[pd.DataFrame, Paso]:
    """Deja las columnas de fecha en un solo formato (texto). Cuenta cuantas maneras
    de escribirlas habia y avisa de las que no se pudieron leer (quedan nulas)."""
    df = df.copy()
    lineas, codigo, avisos = [], [], []
    for col in columnas:
        con_dato = ~es_nulo(df[col], tokens)
        texto = df.loc[con_dato, col].astype(str)
        formas = texto.str.replace(r"\d", "9", regex=True).str.replace(r"[A-Za-z]+", "a", regex=True)
        nuevo = unificar_fechas(df[col].where(con_dato), formato, dia_primero)
        ilegibles = con_dato & nuevo.isna()
        df[col] = nuevo
        lineas.append(f"{col}: {formas.nunique()} formas de escribir la fecha → un solo formato "
                      f"({formato}); {int(ilegibles.sum())} no se pudieron leer")
        if ilegibles.any():
            ejemplos = ", ".join(repr(v) for v in texto[ilegibles[con_dato]].unique()[:3])
            avisos.append(f"«{col}»: {int(ilegibles.sum())} valores no se pudieron leer como fecha y "
                          f"quedaron nulos (ej. {ejemplos}).")
        codigo.append(f'df["{col}"] = unificar_fechas(df["{col}"].where(~es_nulo(df["{col}"], TOKENS_NULOS)), '
                      f'{formato!r}, {dia_primero!r})')
    return df, Paso("Fechas en un solo formato", "\n".join(lineas), "\n".join(codigo),
                    advertencia="\n".join(avisos))


def _clave_texto(valor) -> str:
    """Texto sin tildes, minusculas y sin signos: 'N/A' -> 'n a', 'München' -> 'munchen'."""
    s = unicodedata.normalize("NFKD", str(valor)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _destinos_sugeridos(conteo: pd.Series, palabra: str) -> Dict[str, str]:
    destinos: Dict[str, str] = {}
    claves = {v: _clave_texto(v) for v in conteo.index}
    total = max(int(conteo.sum()), 1)
    for grupo in SINONIMOS.values():  # 1) grupos conocidos (genero, si/no)
        por_clave = {k: destino for destino, ks in grupo.items() for k in ks}
        miembros = {v: por_clave[c] for v, c in claves.items() if c in por_clave}
        sin_dato = [v for v, c in claves.items() if c in TEXTOS_SIN_DATO and v not in miembros]
        cubiertos = sum(int(conteo[v]) for v in list(miembros) + sin_dato)
        if len(miembros) >= 2 and cubiertos >= 0.8 * total:
            destinos.update(miembros)
            destinos.update({v: palabra for v in sin_dato})
            break
    por_clave: Dict[str, List[str]] = {}  # 2) lo que solo cambia en tildes, signos o espacios
    for v in conteo.index:
        if v not in destinos:
            por_clave.setdefault(claves[v], []).append(v)
    for variantes in por_clave.values():
        if len(variantes) > 1:
            principal = max(variantes, key=lambda x: int(conteo[x]))
            destinos.update({v: principal for v in variantes if v != principal})
    return destinos


def sugerir_equivalencias(df: pd.DataFrame, columnas: Optional[List[str]] = None,
                          palabra: str = TEXTO_NO_INDICA, tokens=TOKENS_NULOS_BASE,
                          max_valores: int = 30) -> pd.DataFrame:
    """Tabla (columna, valor, filas, unificar_a) para unir valores que significan lo
    mismo: M / male / masculino -> M, si / yes -> Sí, 'München' / 'Munchen'... Sin
    `columnas` revisa todas las de texto con 2 a `max_valores` valores distintos y
    deja solo las que tienen algo que unir. `unificar_a` vacio = no cambiar."""
    filas = []
    for col in (columnas if columnas is not None else df.columns):
        serie = df[col]
        if pd.api.types.is_numeric_dtype(serie) or pd.api.types.is_datetime64_any_dtype(serie):
            continue
        conteo = serie[~es_nulo(serie, tokens)].astype(str).value_counts()
        if not 2 <= len(conteo) <= max_valores:
            continue
        destinos = _destinos_sugeridos(conteo, palabra)
        if columnas is None and not destinos:
            continue
        for valor, n in conteo.items():
            destino = destinos.get(valor, "")
            filas.append({"columna": col, "valor": valor, "filas": int(n),
                          "unificar_a": "" if destino == valor else destino})
    return pd.DataFrame(filas, columns=["columna", "valor", "filas", "unificar_a"])


def paso_unificar(df: pd.DataFrame, equivalencias: List[Dict]) -> Tuple[pd.DataFrame, Optional[Paso]]:
    """Aplica la union de valores: [{columna, valor, unificar_a}]. Los que tienen
    `unificar_a` vacio (o igual al valor) no cambian. Devuelve (df, None) si no hay nada."""
    df = df.copy()
    mapeos: Dict[str, Dict[str, str]] = {}
    for e in equivalencias:
        destino, valor = str(e.get("unificar_a") or "").strip(), str(e["valor"])
        if destino and destino != valor and e["columna"] in df.columns:
            mapeos.setdefault(e["columna"], {})[valor] = destino
    if not mapeos:
        return df, None
    lineas = []
    for col, mapeo in mapeos.items():
        cambiadas = int(df[col].isin(list(mapeo)).sum())
        df[col] = df[col].replace(mapeo)
        pares = "; ".join(f"«{a}» → «{b}»" for a, b in list(mapeo.items())[:8])
        lineas.append(f"{col}: {cambiadas} celdas unificadas ({pares}"
                      f"{' …' if len(mapeo) > 8 else ''})")
    codigo = (f"MAPEOS = {mapeos!r}\n"
              "for col, mapeo in MAPEOS.items():\n"
              "    df[col] = df[col].replace(mapeo)")
    return df, Paso("Unir valores equivalentes", "\n".join(lineas), codigo)


def paso_asegurar_sin_vacios(df: pd.DataFrame, tokens=TOKENS_NULOS_BASE, numeros: str = "mediana",
                             incluir_fechas: bool = True,
                             texto: str = TEXTO_NO_INDICA) -> Tuple[pd.DataFrame, Paso]:
    """Cierre sin vacios: despues de las reglas, rellena lo que sobre. Los nulos
    validos (texto, correos, telefonos, codigos y fechas) quedan con la misma palabra
    `texto` («No indica» por defecto). Numeros: mediana, 0 (numeros='cero') o la
    palabra (numeros='palabra', la columna pasa a texto). Con incluir_fechas=False las
    fechas se dejan vacias. Avisa de lo que no pudo rellenar."""
    cols_fecha = columnas_fecha_por_nombre(df)
    textos, numericas, omitidas, nulos = [], [], [], {}
    for col in df.columns:
        n = int(es_nulo(df[col], tokens).sum())
        if not n:
            continue
        nulos[col] = n
        rol = rol_columna(df, col, cols_fecha, tokens)
        if rol == "fecha" and not incluir_fechas:
            omitidas.append(col)
        elif rol == "numerica":
            numericas.append(col)
        else:
            textos.append(col)
    df_nuevo = rellenar_restantes(df, textos, numericas, numeros, texto, tokens)
    reglas = [{"columna": c, "regla": "no_indica", "valor": texto, "grupo": "", "nulos": nulos[c]}
              for c in textos]
    for c in numericas:
        convertida = numeros != "palabra" and (
            pd.api.types.is_numeric_dtype(df_nuevo[c]) or
            a_numero(df_nuevo[c]).notna().sum() == df_nuevo[c].notna().sum())
        reglas.append({"columna": c, "regla": ("cero" if numeros == "cero" else "mediana")
                       if convertida else "no_indica", "valor": "" if convertida else texto,
                       "grupo": "", "nulos": nulos[c]})
    if nulos:
        detalle = (f"{sum(nulos[c] for c in textos)} celdas de {len(textos)} columnas de texto → «{texto}»; "
                   f"{sum(nulos[c] for c in numericas)} celdas de {len(numericas)} columnas numéricas → "
                   f"{ {'cero': '0', 'palabra': '«' + texto + '»'}.get(numeros, 'mediana') }.")
    else:
        detalle = "No quedaba ningún vacío."
    advertencia = ("Quedan vacías estas fechas (un texto las rompería como fecha): "
                   + ", ".join(f"{c} ({nulos[c]})" for c in omitidas)) if omitidas else ""
    codigo = (f"df = rellenar_restantes(df, {textos!r}, {numericas!r}, numeros={numeros!r}, "
              f"texto={texto!r}, tokens=TOKENS_NULOS)")
    return df_nuevo, Paso("Asegurar que no queden vacíos", detalle, codigo, advertencia, reglas)


def ejecutar_pasos_globales(df: pd.DataFrame, config: Dict) -> Tuple[pd.DataFrame, List[Paso]]:
    """Corre los pasos que afectan a toda la tabla, en el orden del
    notebook: nombres, vacios a NaN, texto, numericas, coordenadas.
    `config` (todas las claves son opcionales):
      tokens, nombres_snake (bool), vacios_a_nan (bool),
      texto (dict con columnas, minusculas, espacios, comillas),
      numericas (lista), latitud (col), longitud (col),
      rango_latitud / rango_longitud ((minimo, maximo), opcionales),
      fechas (dict con columnas, formato, dia_primero).
    Los nombres de columna del config son los de DESPUES de renombrar."""
    tokens = tuple(config.get("tokens", TOKENS_NULOS_BASE))
    pasos: List[Paso] = []
    if config.get("nombres_snake"):
        df, paso = paso_nombres_columnas(df)
        pasos.append(paso)
    if config.get("vacios_a_nan"):
        df, paso = paso_vacios_a_nan(df, tokens)
        pasos.append(paso)
    texto = config.get("texto")
    if texto and texto.get("columnas"):
        df, paso = paso_texto(df, [c for c in texto["columnas"] if c in df.columns],
                              texto.get("minusculas", True), texto.get("espacios", True),
                              texto.get("comillas", True))
        pasos.append(paso)
    numericas = [c for c in config.get("numericas", []) if c in df.columns]
    if numericas:
        df, paso = paso_numericas(df, numericas, tokens)
        pasos.append(paso)
    lat, lon = config.get("latitud"), config.get("longitud")
    lat = lat if lat in df.columns else None
    lon = lon if lon in df.columns else None
    if lat or lon:
        df, paso = paso_coordenadas(df, lat, lon, config.get("rango_latitud"),
                                    config.get("rango_longitud"))
        pasos.append(paso)
    fechas = config.get("fechas")
    if fechas and fechas.get("columnas"):
        df, paso = paso_fechas(df, [c for c in fechas["columnas"] if c in df.columns],
                               fechas.get("formato", "%Y-%m-%d"), fechas.get("dia_primero", True),
                               tokens)
        pasos.append(paso)
    return df, pasos


def ejecutar_reglas(df: pd.DataFrame, reglas: List[Dict], tokens=TOKENS_NULOS_BASE
                    ) -> Tuple[pd.DataFrame, List[Paso]]:
    """Aplica en orden una lista de reglas [{columna, regla, valor, grupo}].
    Salta las de 'dejar' y las de columnas que ya no existen (por ejemplo
    si una regla anterior la elimino)."""
    pasos: List[Paso] = []
    for r in reglas:
        if r["regla"] == "dejar" or r["columna"] not in df.columns:
            continue
        df, paso = paso_regla(df, r["columna"], r["regla"], r.get("valor", ""),
                              r.get("grupo", ""), tokens)
        if paso:
            pasos.append(paso)
    return df, pasos


# =============================================================================
# Auditoria final y script
# =============================================================================

def auditoria_final(df_antes: pd.DataFrame, df_despues: pd.DataFrame,
                    tokens=TOKENS_NULOS_BASE) -> Dict:
    """Comparativa antes / despues: filas, columnas, nulos totales,
    duplicados, y la tabla de nulos que quedan por columna."""
    def total_nulos(d):
        return int(sum(es_nulo(d[c], tokens).sum() for c in d.columns))

    restantes = diagnostico_nulos(df_despues, tokens)
    restantes = restantes[restantes["total"] > 0][["tipo", "total", "porcentaje", "semaforo"]]
    tipos = pd.DataFrame({"tipo_pandas": df_despues.dtypes.astype(str)})
    return {
        "filas_antes": len(df_antes), "filas_despues": len(df_despues),
        "columnas_antes": df_antes.shape[1], "columnas_despues": df_despues.shape[1],
        "nulos_antes": total_nulos(df_antes), "nulos_despues": total_nulos(df_despues),
        "duplicados_despues": int(df_despues.duplicated().sum()),
        "nulos_restantes": restantes,
        "tipos": tipos,
    }


def generar_script_limpieza(pasos: List[Paso], tokens=TOKENS_NULOS_BASE,
                            nombre_archivo: str = "tu_archivo.csv",
                            hoja: Optional[str] = None) -> str:
    """Script pandas autocontenido que repite la limpieza hecha en la app.
    Trae copiadas las funciones auxiliares, asi no necesita este paquete."""
    es_excel = nombre_archivo.lower().endswith((".xlsx", ".xlsm", ".xls"))
    if es_excel:
        carga = (f'HOJA = {hoja!r}  # hoja a limpiar\n'
                 f'df = pd.read_excel(ARCHIVO, sheet_name=HOJA, dtype=str).fillna("")\n'
                 f'# Si la hoja trae un titulo arriba, agregue header=N a read_excel.')
        salida = ('df.to_excel("limpio.xlsx", index=False)')
    else:
        carga = 'df = pd.read_csv(ARCHIVO, dtype=str, keep_default_na=False)'
        salida = 'df.to_csv("limpio.csv", index=False, encoding="utf-8-sig")'

    partes = [
        "# -*- coding: utf-8 -*-",
        "# Script generado por Limpiador de Tablas (Limpieza guiada).",
        "# Repite paso a paso la limpieza hecha en la app. Solo necesita pandas y numpy.",
        "import re",
        "import numpy as np",
        "import pandas as pd",
        "",
        f"ARCHIVO = {nombre_archivo!r}  # cambie por la ruta de su archivo",
        f"TOKENS_NULOS = {tuple(tokens)!r}  # textos que cuentan como nulo",
        "TOKENS_NULOS_BASE = TOKENS_NULOS  # valor por defecto de las funciones de abajo",
        "",
        "# ---------------- Funciones auxiliares ----------------",
    ]
    for funcion in _FUNCIONES_SCRIPT:
        partes.append(inspect.getsource(funcion).rstrip())
        partes.append("")
    partes += ["", "# ---------------- Carga ----------------", carga,
               'print(f"Cargado: {df.shape[0]} filas x {df.shape[1]} columnas")', ""]
    for i, paso in enumerate(pasos, 1):
        partes.append(f"# ---------------- {i}. {paso.titulo} ----------------")
        partes.append(paso.codigo)
        partes.append("")
    partes += [
        "# ---------------- Auditoria final ----------------",
        'print(f"Filas finales: {len(df)}")',
        'print("Nulos restantes por columna:")',
        "print(df.isna().sum()[df.isna().sum() > 0])",
        'print(f"Duplicados: {df.duplicated().sum()}")',
        "",
        "# ---------------- Guardar ----------------",
        salida,
        "",
    ]
    return "\n".join(partes)

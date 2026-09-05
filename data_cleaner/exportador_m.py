# -*- coding: utf-8 -*-
"""
exportador_m.py
================
Genera código M PURO (sin Python.Execute) para pegar en el Editor avanzado
de Power Query, con las 10 reglas de `limpiador_powerbi.py` traducidas a
pasos nativos de M.

POR QUÉ EXISTE ESTE ARCHIVO
---------------------------
`exportador.generar_editor_m()` produce un paso `Python.Execute(...)` que
Power BI ejecuta con un motor de Python local. Eso trae dos problemas
recurrentes:
  1. Requiere que la máquina tenga Python + pandas/numpy configurados en
     Power BI Desktop (Opciones -> Python scripting).
  2. Power BI serializa el resultado de Python de vuelta a M, y si una
     columna queda con TIPOS MEZCLADOS (ej. `datetime.datetime` y
     `datetime.date` en la misma columna "object"), Power BI descarta en
     silencio los valores que no reconoce -> columnas que se ven "vacías"
     aunque el script de Python no tiró ningún error.

Este módulo evita el problema de raíz: NO ejecuta Python dentro de Power
BI. En vez de eso, esta función corre la MISMA lógica de detección de
columnas (fecha/email/teléfono/id/fórmula/texto) que ya usa
`integraciones_bi/limpiador_powerbi.py`, pero la corre AHORA (al generar
el código, con el DataFrame ya cargado en memoria) para decidir qué
columnas concretas necesitan cada regla, y qué correcciones de texto
("San Jose" -> "San José", etc.) hacen falta. El resultado es una
consulta M que solo usa funciones nativas de Power Query, sin depender de
ningún motor externo.

LIMITACIONES CONOCIDAS (documentarlas es mejor que fingir que no existen):
  - `texto_inconsistente` usa coincidencia difusa (difflib) en Python
    porque M no tiene una función nativa de similitud de texto. Por eso
    la tabla de correcciones queda "congelada" con los valores vistos en
    el momento de generar el código: si el origen agrega variantes nuevas
    más adelante, hay que volver a generar el M (no se recalcula solo en
    cada refresh, a diferencia de las demás reglas).
  - Las acciones soportadas por regla son un subconjunto razonable de las
    8 acciones de `limpiador_powerbi.py` (ver ACCIONES_SOPORTADAS_M más
    abajo). Si se pide una acción no soportada para una regla, se genera
    con 'marcar_solo' y se dice explícitamente en un comentario dentro
    del M generado.
"""
from __future__ import annotations
import re
import difflib
import unicodedata
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

# -----------------------------------------------------------------------------
# Mismos patrones/heurísticas de auto-detección que integraciones_bi/limpiador_powerbi.py
# -----------------------------------------------------------------------------
from data_cleaner.patrones import (
    PATRONES_EMAIL as _PATRONES_EMAIL,
    PATRONES_TELEFONO as _PATRONES_TELEFONO,
    PATRONES_FECHA as _PATRONES_FECHA,
    PATRONES_TOTAL as _PATRONES_TOTAL,
    PATRONES_CANTIDAD as _PATRONES_CANTIDAD,
    PATRONES_PRECIO as _PATRONES_PRECIO,
    columnas_por_patron as _columnas_por_patron,
    es_columna_id as _es_columna_id,
    detectar_columnas as _detectar_columnas,
    parece_email as _parece_email,
    parece_telefono as _parece_telefono,
    parece_fecha as _parece_fecha,
    rango_digitos_telefono as _rango_digitos_telefono,
)
_PATRONES_EXCLUIR_TEXTO = _PATRONES_EMAIL + _PATRONES_TELEFONO + _PATRONES_FECHA + \
    ("nombre", "cliente", "direccion", "dirección", "observacion", "observación", "comentario")

ACCIONES_SOPORTADAS_M = {
    "faltante": {"reemplazar_mediana", "reemplazar_media", "reemplazar_moda", "valor_fijo", "marcar_solo", "eliminar_fila", "editar_individualmente"},
    "duplicado": {"eliminar_fila", "marcar_solo"},
    "atipico": {"limitar", "reemplazar_mediana", "reemplazar_media", "reemplazar_moda", "marcar_solo", "eliminar_fila", "editar_individualmente"},
    "tipo_invalido": {"marcar_solo", "valor_fijo", "eliminar_fila", "editar_individualmente"},
    "fecha_invalida": {"valor_fijo", "marcar_solo", "eliminar_fila", "editar_individualmente"},
    "email_invalido": {"valor_fijo", "marcar_solo", "eliminar_fila", "editar_individualmente"},
    "telefono_invalido": {"valor_fijo", "marcar_solo", "eliminar_fila", "editar_individualmente"},
    "id_duplicado": {"marcar_solo", "eliminar_fila", "editar_individualmente"},
    "formula_incorrecta": {"usar_sugerido", "marcar_solo", "eliminar_fila", "editar_individualmente"},
    "texto_inconsistente": {"usar_sugerido", "marcar_solo", "eliminar_fila", "editar_individualmente"},
}

# _columnas_por_patron y _es_columna_id ahora vienen de data_cleaner.patrones
# (importadas arriba), en vez de una copia local — asi los tres consumidores
# del proyecto (analyzer.py, exportador_m.py, y el futuro que se agregue)
# comparten exactamente la misma logica de deteccion.


def _columna_numerica_potencial(serie):
    valores = serie.dropna()
    if len(valores) == 0:
        return False
    convertibles = pd.to_numeric(valores, errors="coerce")
    return convertibles.notna().mean() > 0.7


def _normalizar_texto(valor):
    s = str(valor).strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return " ".join(s.split())


def _m_str(valor) -> str:
    """Escapa un valor como literal de texto M (comillas dobles duplicadas)."""
    return '"' + str(valor).replace('"', '""') + '"'


def _m_ident(nombre: str) -> str:
    """Devuelve el identificador de paso M, citado con #"..." si hace falta."""
    if re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', nombre):
        return nombre
    return '#' + _m_str(nombre)


def _accion_o_fallback(regla: str, accion: str, comentarios: List[str]) -> str:
    if accion in ACCIONES_SOPORTADAS_M.get(regla, set()):
        return accion
    comentarios.append(
        f'  // AVISO: la accion "{accion}" pedida para "{regla}" no esta soportada en '
        f'generacion 100% M; se genero como "marcar_solo".'
    )
    return "marcar_solo"


def _mapa_texto_inconsistente(df, columnas, umbral_similitud=0.85, max_cardinalidad_ratio=0.5,
                               min_apariciones_canonica=1) -> Dict[str, Dict[str, str]]:
    """Calcula, columna por columna, el diccionario {variante_original: forma_canonica}
    usando la MISMA coincidencia difusa (difflib) que analyzer.py. Esto se hace UNA VEZ
    aqui, en Python, para poder "hornear" el resultado como una tabla estatica dentro
    del M generado (M no tiene una funcion nativa de similitud de texto)."""
    resultado = {}
    for col in columnas:
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

        mapa_col = {}
        for grupo in grupos:
            candidatas = [par for n in grupo for par in textos_por_norm[n]]
            if len(candidatas) < 2:
                continue
            canonica_texto, canonica_cuenta = max(candidatas, key=lambda t: t[1])
            if canonica_cuenta < min_apariciones_canonica:
                continue
            for texto_variante, _ in candidatas:
                if texto_variante != canonica_texto:
                    mapa_col[texto_variante] = canonica_texto
        if mapa_col:
            resultado[col] = mapa_col
    return resultado


def _columnas_candidatas_texto(df, umbral_max_cardinalidad=0.5):
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
        if ratio <= umbral_max_cardinalidad:
            cols.append(col)
    return cols


# =============================================================================
# FUNCIÓN AUXILIAR M (fija, no depende del dataset): parser de fechas
# multi-formato + función de percentil (para IQR), inyectadas al inicio del
# query cuando hacen falta.
# =============================================================================
_M_FUNCION_FECHA = '''  // Interpreta una fecha en texto sin importar cual de los 5 formatos use.
  // Devuelve null si el texto no calza con ninguno o si la fecha no existe
  // (ej. 31 de febrero).
  FechaDesdeTexto = (t as text) as nullable date =>
    let
        limpio = Text.Trim(t),
        MesesEN = {"January","February","March","April","May","June","July","August","September","October","November","December"},
        porPuntos = try (
            let p = Text.Split(limpio, ".") in
            if List.Count(p) = 3 then #date(Number.From(p{0}), Number.From(p{1}), Number.From(p{2})) else error "na"
        ) otherwise null,
        conDe = if porPuntos <> null then porPuntos else try (
            let p = Text.Split(limpio, " de ") in
            if List.Count(p) = 3 and List.Contains(MesesEN, p{1}) then
                #date(Number.From(p{2}), List.PositionOf(MesesEN, p{1}) + 1, Number.From(p{0}))
            else error "na"
        ) otherwise null,
        porSlash = if conDe <> null then conDe else try (
            let p = Text.Split(limpio, "/") in
            if List.Count(p) = 3 then #date(Number.From(p{2}), Number.From(p{1}), Number.From(p{0})) else error "na"
        ) otherwise null,
        porGuion = if porSlash <> null then porSlash else try (
            let p = Text.Split(limpio, "-") in
            if List.Count(p) <> 3 then error "na"
            else if Text.Length(p{2}) = 2 then #date(2000 + Number.From(p{2}), Number.From(p{1}), Number.From(p{0}))
            else #date(Number.From(p{2}), Number.From(p{0}), Number.From(p{1}))
        ) otherwise null
    in
        porGuion,
'''

_M_FUNCION_PERCENTIL = '''  // Percentil con interpolacion lineal (igual convencion que pandas .quantile()).
  Percentil = (lista as list, p as number) as nullable number =>
    let
        ordenada = List.Sort(List.RemoveNulls(lista)),
        n = List.Count(ordenada)
    in
        if n = 0 then null else
        let
            posicion = p * (n - 1),
            piso = Number.RoundDown(posicion),
            techo = Number.RoundUp(posicion),
            fraccion = posicion - piso,
            valorPiso = ordenada{piso},
            valorTecho = ordenada{techo}
        in
            valorPiso + fraccion * (valorTecho - valorPiso),
'''

def _m_valor_id(valor) -> str:
    """Renderiza un valor de ID como literal M: numero si es numero, texto citado si no.
    OJO: los valores numericos de un DataFrame de pandas son numpy.int64/float64, no
    int/float nativos de Python -- por eso se usa pd.api.types.is_number en vez de
    isinstance(valor, (int, float)), que los dejaria pasar como texto citado y
    rompería silenciosamente la comparacion [ID] = idFila en M (texto vs numero
    nunca son iguales, asi que la correccion nunca se aplicaria)."""
    if isinstance(valor, bool) or isinstance(valor, np.bool_):
        return "true" if bool(valor) else "false"
    if pd.api.types.is_number(valor):
        f = float(valor)
        if f.is_integer():
            return str(int(f))
        return repr(f)
    return _m_str(valor)


def _preparar_correcciones_telefono(df, correcciones_individuales, col, id_ref_col):
    """A partir de `correcciones_individuales` (dict {(tipo, columna, fila): valor_corregido},
    con 'fila' = indice 0-based del DataFrame, misma convencion que Issue.fila en
    analyzer.py) calcula las filas (ID, Columna, Posicion, DigitoCorrecto) que hay que
    hornear dentro de TablaCorreccionesDigitosTelefono, comparando cada valor corregido
    (el que se edito en el paso "Editar cada uno por separado" de la app) contra el
    valor ORIGINAL que tenia esa celda en el DataFrame analizado. Si la longitud del
    valor corregido cambia respecto al original, se usa Posicion = 0 (reemplazo del
    valor completo) porque un reemplazo caracter-por-caracter no puede insertar ni
    quitar digitos."""
    filas: List[Tuple[object, str, int, str]] = []
    if not correcciones_individuales:
        return filas
    for (tipo, columna, fila_idx), valor_corr in correcciones_individuales.items():
        if tipo != "telefono_invalido" or columna != col:
            continue
        if valor_corr is None or fila_idx is None or fila_idx < 0 or fila_idx >= len(df):
            continue
        valor_original = df.iloc[fila_idx][col]
        original_txt = "" if pd.isna(valor_original) else str(valor_original)
        corregido_txt = str(valor_corr)
        if corregido_txt == original_txt:
            continue  # se dejo igual al original -> no necesita cambio
        id_valor = df.iloc[fila_idx][id_ref_col] if id_ref_col in df.columns else int(fila_idx)
        if len(corregido_txt) != len(original_txt):
            filas.append((id_valor, col, 0, corregido_txt))
        else:
            for pos, (c_orig, c_new) in enumerate(zip(original_txt, corregido_txt), start=1):
                if c_orig != c_new:
                    filas.append((id_valor, col, pos, c_new))
    return filas


def _m_funcion_correccion_digitos(filas_horneadas: List[Tuple[object, str, int, str]]) -> str:
    if filas_horneadas:
        cuerpo_filas = ",\n          ".join(
            "{" + _m_valor_id(idv) + ", " + _m_str(colv) + ", " + str(pos) + ", " + _m_str(dig) + "}"
            for (idv, colv, pos, dig) in filas_horneadas
        )
    else:
        cuerpo_filas = (
            '// Ejemplo (quite el // de la linea para activarlo):\n'
            '          // {12, "Telefono", 3, "7"}   -> en el registro ID=12, columna "Telefono", cambia el caracter en la posicion 3 por "7"'
        )
    return f'''  // Tabla para corregir telefonos invalidos sin tocar el dato de origen. Si
  // elegiste "Editar cada uno por separado" en la app, esta tabla ya viene
  // con las correcciones que escribiste ahi horneadas como filas; tambien
  // puedes seguir agregando o ajustando filas a mano aqui. ID = valor de la
  // columna identificadora (o del indice de fila, 0-based, si el dataset no
  // tiene ID) del registro a corregir; Columna = nombre EXACTO de la columna
  // de telefono; Posicion = 1 es el primer caracter del texto, 0 significa
  // "reemplazar el valor COMPLETO" (se usa cuando la correccion cambia de
  // longitud); DigitoCorrecto = el caracter (o el valor completo si
  // Posicion = 0). Esta tabla sobrevive a los refrescos de datos porque esta
  // escrita aqui, no calculada a partir del origen.
  TablaCorreccionesDigitosTelefono = #table(
      type table [ID = any, Columna = text, Posicion = Int64.Type, DigitoCorrecto = text],
      {{
          {cuerpo_filas}
      }}
  ),

  // Aplica, en orden, todas las correcciones de TablaCorreccionesDigitosTelefono
  // que apliquen a un registro+columna dados. Posicion = 0 reemplaza el valor
  // completo; cualquier otro valor reemplaza solo ese caracter, dejando el
  // resto del texto intacto.
  AplicarCorreccionDigito = (idFila as any, columna as text, valorOriginal as nullable text) as nullable text =>
    let
        correcciones = Table.SelectRows(TablaCorreccionesDigitosTelefono, each [ID] = idFila and [Columna] = columna),
        resultado = List.Accumulate(
            Table.ToRecords(correcciones),
            valorOriginal,
            (acumulado, correccion) =>
                if acumulado = null then acumulado
                else if correccion[Posicion] = 0 then correccion[DigitoCorrecto]
                else
                    let
                        pos = correccion[Posicion],
                        antes = Text.Start(acumulado, pos - 1),
                        despues = Text.Range(acumulado, pos)
                    in
                        antes & correccion[DigitoCorrecto] & despues
        )
    in
        resultado,
'''


def _preparar_correcciones_individuales_generico(df, correcciones_individuales, tipo, col, id_ref_col):
    """Igual que `_preparar_correcciones_telefono` pero generico (para
    cualquier tipo de hallazgo excepto 'telefono_invalido', que sigue usando
    su propia tabla de digitos): compara cada valor corregido contra el
    ORIGINAL de esa celda y arma (id_valor, columna, valor_corregido) para
    hornear en TablaCorreccionesIndividuales. A diferencia del telefono, aqui
    siempre se reemplaza el valor COMPLETO (no tiene sentido "corregir un
    digito" en un texto, una fecha o un id)."""
    filas: List[Tuple[object, str, object]] = []
    if not correcciones_individuales:
        return filas
    for (t, columna, fila_idx), valor_corr in correcciones_individuales.items():
        if t != tipo or columna != col:
            continue
        if fila_idx is None or fila_idx < 0 or fila_idx >= len(df):
            continue
        valor_original = df.iloc[fila_idx][col]
        original_txt = "" if pd.isna(valor_original) else str(valor_original)
        corregido_txt = "" if valor_corr is None else str(valor_corr)
        if corregido_txt == original_txt:
            continue  # se dejo igual al original -> no necesita cambio
        id_valor = df.iloc[fila_idx][id_ref_col] if id_ref_col in df.columns else int(fila_idx)
        filas.append((id_valor, col, valor_corr))
    return filas


def _m_valor_generico(valor) -> str:
    """Renderiza un ValorCorregido para TablaCorreccionesIndividuales: null si
    es None, o texto citado (las correcciones se escriben como texto en la
    app; si la columna destino es numerica o fecha, el paso de conversion
    correspondiente ya se encarga de interpretarlo, igual que con cualquier
    valor que venga del origen)."""
    if valor is None:
        return "null"
    return _m_str(valor)


def _m_funcion_correccion_individual(filas_horneadas: List[Tuple[str, object, str, object]]) -> str:
    if filas_horneadas:
        cuerpo_filas = ",\n          ".join(
            "{" + _m_str(tipo) + ", " + _m_valor_id(idv) + ", " + _m_str(colv) + ", " + _m_valor_generico(val) + "}"
            for (tipo, idv, colv, val) in filas_horneadas
        )
    else:
        cuerpo_filas = (
            '// Ejemplo (quite el // de la linea para activarlo):\n'
            '          // {"faltante", 12, "apellido_contacto", "Rojas"}   -> en el registro ID=12, columna "apellido_contacto", usa "Rojas"'
        )
    return f'''  // Tabla generica para corregir, registro por registro, cualquier hallazgo
  // cuya accion se dejo en "Editar cada uno por separado" (el telefono usa
  // su propia tabla de digitos, mas arriba). Si elegiste esa opcion en la
  // app, esta tabla ya viene con las correcciones horneadas como filas;
  // tambien puedes seguir agregando o ajustando filas a mano. Tipo = el
  // nombre interno del hallazgo (ej. "faltante", "atipico", "email_invalido",
  // igual que en el detalle de hallazgos de la app); ID = valor de la columna
  // identificadora (o del indice de fila, 0-based, si el dataset no tiene
  // ID); Columna = nombre EXACTO de la columna; ValorCorregido = el valor a
  // usar (texto, o null para dejar vacio). Esta tabla sobrevive a los
  // refrescos de datos porque esta escrita aqui, no calculada a partir del
  // origen. OJO: si "Columna" es la misma que se usa como ID de referencia,
  // edite esa fila de ultimo (una correccion sobre la propia columna ID hace
  // que las demas correcciones de ese registro dejen de encontrarlo, porque
  // el ID ya cambio).
  TablaCorreccionesIndividuales = #table(
      type table [Tipo = text, ID = any, Columna = text, ValorCorregido = any],
      {{
          {cuerpo_filas}
      }}
  ),

  // Busca si hay una correccion para (tipo, idFila, columna) en
  // TablaCorreccionesIndividuales; si la hay, usa ese valor en vez del
  // original (sin tocar el dato de origen ni perderse en cada refresh).
  AplicarCorreccionIndividual = (tipo as text, idFila as any, columna as text, valorOriginal as any) as any =>
    let
        correccion = Table.SelectRows(TablaCorreccionesIndividuales, each [Tipo] = tipo and [ID] = idFila and [Columna] = columna)
    in
        if Table.RowCount(correccion) > 0 then correccion{{0}}[ValorCorregido] else valorOriginal,
'''


class _ConstructorM:
    """Acumula pasos (nombre, formula) M en orden y arma el `let ... in` final."""

    def __init__(self, paso_inicial: str):
        self.pasos: List[Tuple[str, str]] = []
        self.ultimo = paso_inicial
        self.banderas: List[str] = []  # nombres de columnas Revisar_* generadas

    def agregar(self, nombre: str, formula_con_placeholder: str) -> str:
        """formula_con_placeholder puede usar el texto literal {prev} para referirse
        al paso anterior. Se usa un simple .replace() (no .format()) porque el
        propio codigo M esta lleno de llaves { } que .format() interpretaria mal."""
        formula = formula_con_placeholder.replace("{prev}", _m_ident(self.ultimo))
        self.pasos.append((nombre, formula))
        self.ultimo = nombre
        return nombre

    def bandera(self, nombre_col: str):
        self.banderas.append(nombre_col)

    def construir(self, funciones_extra: str = "") -> str:
        cuerpo = ",\n\n".join(f"  {_m_ident(n)} = {f}" for n, f in self.pasos)
        return f"let\n{funciones_extra}{cuerpo}\nin\n  {_m_ident(self.ultimo)}\n"


def generar_editor_m_puro(
    df: pd.DataFrame,
    config: Optional[Dict[str, str]] = None,
    factor_iqr: float = 1.5,
    valores_fijos: Optional[dict] = None,
    nombre_paso_anterior: str = "TuPasoAnterior",
    # --- reglas nuevas: mismos parametros que limpiador_powerbi.py ---
    fecha_invalida: str = "marcar_solo",
    email_invalido: str = "marcar_solo",
    telefono_invalido: str = "marcar_solo",
    id_duplicado: str = "marcar_solo",
    formula_incorrecta: str = "marcar_solo",
    texto_inconsistente: str = "marcar_solo",
    columnas_fecha: Optional[List[str]] = None,
    fecha_min: Optional[str] = None,
    fecha_max: Optional[str] = None,
    columnas_email: Optional[List[str]] = None,
    columnas_telefono: Optional[List[str]] = None,
    digitos_telefono: Optional[Tuple[int, int]] = None,
    paises_telefono: Optional[List[str]] = None,
    permitir_codigo_pais_telefono: bool = True,
    desglosar_digitos_telefono: bool = True,
    primeros_digitos_telefono_validos: Optional[List[str]] = None,
    columnas_id: Optional[List[str]] = None,
    columna_total: Optional[str] = None,
    columna_cantidad: Optional[str] = None,
    columna_precio: Optional[str] = None,
    tolerancia_formula: float = 0.01,
    columnas_texto: Optional[List[str]] = None,
    umbral_similitud_texto: float = 0.85,
    max_cardinalidad_ratio_texto: float = 0.5,
    # --- correcciones puntuales (accion 'editar_individualmente') y control
    # de las columnas Revisar_* (banderas de "marcar_solo") ---
    correcciones_individuales: Optional[Dict[tuple, object]] = None,
    agregar_columnas_revision: bool = True,
    columnas_excluir_revision: Optional[List[str]] = None,
) -> str:
    """Genera codigo M 100% nativo (sin Python.Execute) equivalente a
    integraciones_bi/limpiador_powerbi.py, usando `df` (los datos YA cargados
    en el notebook/app) para decidir que columnas concretas necesita cada regla.

    `df` debe ser el mismo DataFrame que se analizo/limpio en la interfaz,
    para que la deteccion de columnas y la tabla de correcciones de texto
    coincidan con lo que el usuario ya vio en el reporte de calidad.

    Telefono:
      - `digitos_telefono`: si se da explicito (min, max), manda sobre todo
        lo demas (ej. para un dataset de un solo pais que ya se conoce bien).
      - `paises_telefono`: lista de paises/codigos (ej. ["cr", "mexico"]);
        si `digitos_telefono` es None, el rango aceptado es la UNION de los
        rangos tipicos de esos paises (ver patrones.DIGITOS_TELEFONO_PAIS).
      - Si ninguno de los dos se da, se usa el rango internacional amplio
        (7-15 digitos, E.164) en vez de asumir un solo pais.
      - `permitir_codigo_pais_telefono`: si True (por defecto), tambien se
        acepta el mismo numero con 1-3 digitos extra al inicio (codigo de
        pais sin "+"), para no rechazar numeros que vienen con codigo de
        pais incluido.
      - `desglosar_digitos_telefono`: si True (por defecto), por cada
        columna de telefono se agregan columnas "<col>_Digito_1..N" (un
        caracter cada una) y "<col>_Posiciones_Invalidas", para visualizar
        exactamente donde esta el error. Ademas se inyecta al inicio del
        query una tabla editable a mano (`TablaCorreccionesDigitosTelefono`)
        donde se puede agregar una fila por cada digito puntual a corregir
        (identificando el registro por ID, o por un indice de fila si el
        dataset no tiene columna ID), sin tocar el dato de origen ni
        perderse en cada refresh.
    """
    cfg_basico = {"faltante": "reemplazar_mediana", "duplicado": "eliminar_fila",
                  "atipico": "limitar", "tipo_invalido": "marcar_solo", **(config or {})}
    valores_fijos = valores_fijos or {}
    comentarios: List[str] = []
    _columnas_excluir_revision = set(columnas_excluir_revision or [])

    def _incluir_revision(nombre_bandera: str) -> bool:
        """Decide si se agrega una columna Revisar_* concreta: False si el
        interruptor general esta apagado, o si ese nombre puntual esta en la
        lista de exclusion."""
        return agregar_columnas_revision and nombre_bandera not in _columnas_excluir_revision

    a_faltante = _accion_o_fallback("faltante", cfg_basico["faltante"], comentarios)
    a_duplicado = _accion_o_fallback("duplicado", cfg_basico["duplicado"], comentarios)
    a_atipico = _accion_o_fallback("atipico", cfg_basico["atipico"], comentarios)
    a_tipo_invalido = _accion_o_fallback("tipo_invalido", cfg_basico["tipo_invalido"], comentarios)
    a_fecha = _accion_o_fallback("fecha_invalida", fecha_invalida, comentarios)
    a_email = _accion_o_fallback("email_invalido", email_invalido, comentarios)
    a_tel = _accion_o_fallback("telefono_invalido", telefono_invalido, comentarios)
    a_id = _accion_o_fallback("id_duplicado", id_duplicado, comentarios)
    a_formula = _accion_o_fallback("formula_incorrecta", formula_incorrecta, comentarios)
    a_texto = _accion_o_fallback("texto_inconsistente", texto_inconsistente, comentarios)

    # Deteccion en 2 niveles: por nombre de columna primero (rapido); si
    # eso no encuentra nada, se revisan los VALORES reales como respaldo
    # (cubre datasets con columnas mal nombradas: "col_1", "campo_7", etc.).
    # El orden fecha -> email -> telefono importa: cada deteccion excluye
    # las columnas que ya reclamo una regla anterior, para que una columna
    # de fecha con muchos digitos no se confunda con telefono, etc.
    cols_fecha = columnas_fecha if columnas_fecha is not None else \
        _detectar_columnas(df, _PATRONES_FECHA, _parece_fecha)
    cols_email = columnas_email if columnas_email is not None else \
        _detectar_columnas(df, _PATRONES_EMAIL, _parece_email, excluir=cols_fecha)
    cols_tel = columnas_telefono if columnas_telefono is not None else \
        _detectar_columnas(df, _PATRONES_TELEFONO, _parece_telefono, excluir=cols_fecha + cols_email)
    cols_id = columnas_id if columnas_id is not None else [c for c in df.columns if _es_columna_id(c)]
    cols_texto = columnas_texto if columnas_texto is not None else _columnas_candidatas_texto(df, max_cardinalidad_ratio_texto)
    col_total = columna_total or (_columnas_por_patron(df, _PATRONES_TOTAL) or [None])[0]
    col_cant = columna_cantidad or (_columnas_por_patron(df, _PATRONES_CANTIDAD) or [None])[0]
    col_precio = columna_precio or (_columnas_por_patron(df, _PATRONES_PRECIO) or [None])[0]
    hay_formula = bool(col_total and col_cant and col_precio and
                        all(c in df.columns for c in (col_total, col_cant, col_precio)))

    # OJO: el M generado aplica Text.Trim() a TODAS las columnas de texto
    # antes de la correccion de texto_inconsistente (ver mas abajo). El mapa
    # de correcciones tiene que calcularse sobre los valores YA recortados,
    # o las claves con espacios sobrantes nunca harian match en tiempo de
    # ejecucion (el Trim ya las habria eliminado antes de llegar ahi).
    df_para_mapa = df.copy()
    for c in df_para_mapa.columns:
        if pd.api.types.is_object_dtype(df_para_mapa[c]) or pd.api.types.is_string_dtype(df_para_mapa[c]):
            df_para_mapa[c] = df_para_mapa[c].apply(lambda v: v.strip() if isinstance(v, str) else v)
    mapa_texto = _mapa_texto_inconsistente(df_para_mapa, cols_texto, umbral_similitud_texto,
                                            max_cardinalidad_ratio_texto) if cols_texto else {}

    cb = _ConstructorM(nombre_paso_anterior)
    necesita_fecha_fn = bool(cols_fecha)
    necesita_percentil_fn = (a_atipico in {"limitar"})
    necesita_correccion_digitos_fn = bool(cols_tel) and (desglosar_digitos_telefono or a_tel == "editar_individualmente")

    # -- 1) Duplicados de fila completa --------------------------------------
    if a_duplicado == "eliminar_fila":
        cb.agregar("SinDuplicados", "Table.Distinct({prev})")
    elif a_duplicado == "marcar_solo" and _incluir_revision("Revisar_Duplicado"):
        cb.agregar("ConteoDupFila",
                    "Table.Group({prev}, Table.ColumnNames(" + _m_ident(cb.ultimo) + "), "
                    '{{"_conteo_dup_fila", each Table.RowCount(_), each Table.FirstN(_,1){0}, Table.ColumnNames(' + _m_ident(cb.ultimo) + ')}})')
        # Nota: Table.Group con GroupKind.Local por defecto agrupa filas identicas;
        # se anexa el conteo y se re-expande para no perder columnas originales.
        # Implementacion simplificada: se marca via join en vez de Group+expand
        # para evitar reordenar columnas.
        cb.pasos.pop()  # descartar el intento anterior (queda mas simple con NestedJoin)
        cb.ultimo = nombre_paso_anterior if not cb.pasos else cb.pasos[-1][0]
        base_dup = _m_ident(cb.ultimo)
        cb.agregar("ConteoPorFila",
                    "Table.Group(" + base_dup + ", Table.ColumnNames(" + base_dup + "), "
                    '{{"_conteo_fila", each Table.RowCount(_)}})')
        cb.agregar("Revisar_Duplicado_col",
                    "Table.AddColumn(Table.Join(" + base_dup + ", Table.ColumnNames(" + base_dup + "), "
                    "{prev}, Table.ColumnNames(" + base_dup + ")), \"Revisar_Duplicado\", each [_conteo_fila] > 1, type logical)")
        cb.agregar("SinConteoFila", 'Table.RemoveColumns({prev}, {"_conteo_fila"})')
        cb.bandera("Revisar_Duplicado")

    # -- 1.5) ID de referencia + correcciones individuales genericas ------------
    # Se resuelve aqui (despues de Duplicados, antes de cualquier otra regla)
    # el ID/indice de fila que van a usar tanto el telefono como esta tabla
    # generica, y se aplican YA las correcciones de "Editar cada uno por
    # separado" sobre el valor CRUDO de origen (antes de convertir a numero,
    # parsear fecha, etc.) para que el resto del pipeline (relleno de
    # mediana, winsorizing, validacion de email, formula, etc.) trate el
    # valor corregido igual que si viniera del origen. OJO: el indice de fila
    # se agrega DESPUES de Table.Distinct (arriba) a proposito -- si se
    # agregara antes, cada fila quedaria con un indice unico y
    # Table.Distinct ya no podria detectar filas duplicadas.
    _accion_por_tipo_individual = {
        "faltante": a_faltante, "atipico": a_atipico, "tipo_invalido": a_tipo_invalido,
        "fecha_invalida": a_fecha, "email_invalido": a_email, "id_duplicado": a_id,
        "formula_incorrecta": a_formula, "texto_inconsistente": a_texto,
    }
    columnas_edicion_individual: List[Tuple[str, str]] = []
    if correcciones_individuales:
        _vistos_edicion = set()
        for (_tipo_ci, _columna_ci, _fila_ci) in correcciones_individuales.keys():
            if (_tipo_ci, _columna_ci) in _vistos_edicion:
                continue
            if (_accion_por_tipo_individual.get(_tipo_ci) == "editar_individualmente"
                    and _columna_ci and _columna_ci in df.columns):
                _vistos_edicion.add((_tipo_ci, _columna_ci))
        columnas_edicion_individual = sorted(_vistos_edicion)

    necesita_id_correccion_tel = bool(cols_tel) and (desglosar_digitos_telefono or a_tel == "editar_individualmente")
    id_ref_col = cols_id[0] if cols_id else None
    if (necesita_id_correccion_tel or columnas_edicion_individual) and id_ref_col is None:
        cb.agregar("IndiceParaCorreccion", 'Table.AddIndexColumn({prev}, "_IndiceFila", 0, 1, Int64.Type)')
        id_ref_col = "_IndiceFila"

    filas_correccion_individual: List[Tuple[str, object, str, object]] = []
    for _tipo_ci, _col_ci in columnas_edicion_individual:
        for (idv, colv, val) in _preparar_correcciones_individuales_generico(
                df, correcciones_individuales, _tipo_ci, _col_ci, id_ref_col):
            filas_correccion_individual.append((_tipo_ci, idv, colv, val))

    necesita_correccion_individual_fn = bool(columnas_edicion_individual)
    for _tipo_ci, _col_ci in columnas_edicion_individual:
        _nombre_col_id = re.sub(r'[^A-Za-z0-9]', '', _col_ci)
        _tipo_id = re.sub(r'[^A-Za-z0-9]', '', _tipo_ci)
        cb.agregar(f"IndCorr_{_tipo_id}_{_nombre_col_id}",
                   "Table.AddColumn({prev}, \"_ind_corr_" + _nombre_col_id + "\", each "
                   f"AplicarCorreccionIndividual({_m_str(_tipo_ci)}, [{id_ref_col}], {_m_str(_col_ci)}, [{_col_ci}]), type any)")
        cb.agregar(f"IndSinOriginal_{_tipo_id}_{_nombre_col_id}", 'Table.RemoveColumns({prev}, {' + _m_str(_col_ci) + '})')
        cb.agregar(f"IndRenombrado_{_tipo_id}_{_nombre_col_id}",
                   'Table.RenameColumns({prev}, {{"_ind_corr_' + _nombre_col_id + '", ' + _m_str(_col_ci) + '}})')

    # -- 2) Limpieza basica de texto (trim) ----------------------------------
    columnas_texto_trim = [c for c in df.columns
                            if pd.api.types.is_object_dtype(df[c]) or pd.api.types.is_string_dtype(df[c])]
    if columnas_texto_trim:
        pares = ", ".join(f'{{{_m_str(c)}, each if _ = null then null else Text.Trim(_)}}' for c in columnas_texto_trim)
        cb.agregar("EspaciosRecortados", "Table.TransformColumns({prev}, {" + pares + "})")

    # -- 3) Texto inconsistente (tabla de correccion horneada) ---------------
    if mapa_texto and a_texto in ("usar_sugerido", "marcar_solo"):
        paso_prev_texto = cb.ultimo
        for col, mapa in mapa_texto.items():
            if a_texto == "usar_sugerido":
                # Cadena de "if v = X then Y else if ... else v"
                cadena = "_"
                for variante, canonica in mapa.items():
                    cadena = f'if _ = {_m_str(variante)} then {_m_str(canonica)} else ({cadena})'
                formula = f'{{{_m_str(col)}, each if _ = null then null else {cadena}}}'
                cb.agregar(f"Corregido_{re.sub(r"[^A-Za-z0-9]", "", col)}",
                           "Table.TransformColumns({prev}, {" + formula + "})")
            else:  # marcar_solo -> columna Revisar_Texto_<col>
                nombre_bandera = f"Revisar_Texto_{col}"
                if _incluir_revision(nombre_bandera):
                    variantes_lista = "{" + ", ".join(_m_str(v) for v in mapa.keys()) + "}"
                    cb.agregar(f"Revisar_{re.sub(r'[^A-Za-z0-9]', '', col)}",
                               "Table.AddColumn({prev}, " + _m_str(nombre_bandera) +
                               f", each List.Contains({variantes_lista}, [{col}]), type logical)")
                    cb.bandera(nombre_bandera)

    # -- 4) Fechas ------------------------------------------------------------
    for col in cols_fecha:
        if col not in df.columns:
            continue
        rango_check = ""
        if fecha_min:
            rango_check += f' or _v < #date({",".join(str(int(x)) for x in str(fecha_min).split("-"))})'
        if fecha_max:
            rango_check += f' or _v > #date({",".join(str(int(x)) for x in str(fecha_max).split("-"))})'
        # OJO: dentro de Table.TransformColumns el "each" recibe el VALOR de
        # la celda como "_" (no la fila completa) — usar [col] aqui (como se
        # hacia antes) revienta con error en el 100% de las filas porque "_"
        # no es un record. Por eso se usa "_" en todo este bloque, igual que
        # en el M de referencia (FechaDesdeTexto la recibe como texto plano).
        cuerpo_parse = (
            f'each if _ = null then null '
            f'else let _v = if Value.Is(_, type text) then FechaDesdeTexto(_) else Date.From(_) '
            f'in if _v = null then null'
            + (f' else if (false{rango_check}) then null else _v' if rango_check else ' else _v')
        )
        if a_fecha in ("valor_fijo", "marcar_solo"):
            cb.agregar(f"FechaCorregida_{re.sub(r'[^A-Za-z0-9]', '', col)}",
                       "Table.TransformColumns({prev}, {{" + _m_str(col) + f", {cuerpo_parse}, type nullable date}}}})")
            if a_fecha == "marcar_solo":
                nombre_bandera = f"Revisar_Fecha_{col}" if len(cols_fecha) > 1 else "Revisar_Fecha"
                if _incluir_revision(nombre_bandera):
                    cb.agregar(f"RevisarFecha_{re.sub(r'[^A-Za-z0-9]', '', col)}",
                               "Table.AddColumn({prev}, " + _m_str(nombre_bandera) +
                               f", each [{col}] = null, type logical)")
                    cb.bandera(nombre_bandera)

    # -- 5) ID duplicado --------------------------------------------------------
    for col in cols_id:
        if col not in df.columns:
            continue
        nombre_bandera_id = f"Revisar_ID_Duplicado_{col}" if len(cols_id) > 1 else "Revisar_ID_Duplicado"
        if a_id != "eliminar_fila" and not _incluir_revision(nombre_bandera_id):
            continue
        base = _m_ident(cb.ultimo)
        cb.agregar(f"ConteoID_{re.sub(r'[^A-Za-z0-9]', '', col)}",
                   f"Table.Group({{prev}}, {{{_m_str(col)}}}, " + '{{"_conteo_id", each Table.RowCount(_), type number}})')
        conteo_paso = cb.ultimo
        cb.pasos.pop(); cb.ultimo = base if not cb.pasos else cb.pasos[-1][0]
        cb.agregar(f"ConteoID_{re.sub(r'[^A-Za-z0-9]', '', col)}",
                   f"Table.Group({_m_ident(cb.ultimo)}, {{{_m_str(col)}}}, "
                   '{{"_conteo_id", each Table.RowCount(_), type number}})')
        conteo_nombre = cb.ultimo
        cb.agregar(f"UnionID_{re.sub(r'[^A-Za-z0-9]', '', col)}",
                   f"Table.NestedJoin({base}, {{{_m_str(col)}}}, {conteo_nombre}, {{{_m_str(col)}}}, \"_infoID\", JoinKind.LeftOuter)")
        cb.agregar(f"ExpandID_{re.sub(r'[^A-Za-z0-9]', '', col)}",
                   'Table.ExpandTableColumn({prev}, "_infoID", {"_conteo_id"})')
        if a_id == "eliminar_fila":
            cb.agregar(f"SinIDDup_{re.sub(r'[^A-Za-z0-9]', '', col)}",
                       'Table.Distinct(Table.RemoveColumns(Table.Sort({prev}, {{"_conteo_id", Order.Ascending}}), {"_conteo_id"}), {' + _m_str(col) + '})')
        else:
            cb.agregar(f"RevisarIDDup_{re.sub(r'[^A-Za-z0-9]', '', col)}",
                       "Table.AddColumn({prev}, " + _m_str(nombre_bandera_id) + ", each [_conteo_id] > 1, type logical)")
            cb.agregar(f"SinConteoID_{re.sub(r'[^A-Za-z0-9]', '', col)}", 'Table.RemoveColumns({prev}, {"_conteo_id"})')
            cb.bandera(nombre_bandera_id)

    # -- 6) Numericos: faltante / tipo_invalido / atipico ------------------------
    # Se excluyen las columnas que ya tienen su propia regla especializada
    # (ID y Telefono): esas NO deben pasar por conversion generica a numero,
    # relleno con mediana ni recorte por IQR, o se pisaria/rompería la
    # limpieza especifica de esa regla (ej. Telefono quedaria convertido a
    # numero, perdiendo ceros a la izquierda, antes de poder validarlo).
    columnas_excluidas_numerico = set(cols_id) | set(cols_tel)
    columnas_numericas_potenciales = [c for c in df.columns
                                       if c not in columnas_excluidas_numerico
                                       and _columna_numerica_potencial(df[c])]
    for col in columnas_numericas_potenciales:
        cb.agregar(f"NumConvertido_{re.sub(r'[^A-Za-z0-9]', '', col)}",
                   "Table.TransformColumns({prev}, {{" + _m_str(col) +
                   f", each try Number.FromText(Text.Select(Text.From(_), {{\"0\"..\"9\",\".\",\"-\"}})) otherwise null, type number}}}})")
        if a_faltante != "marcar_solo" or a_tipo_invalido != "marcar_solo":
            pass  # el relleno de nulos ocurre mas abajo, comun a ambas reglas
        nombre_col_id = re.sub(r'[^A-Za-z0-9]', '', col)
        if a_faltante in ("reemplazar_mediana", "reemplazar_media", "reemplazar_moda", "valor_fijo"):
            if a_faltante == "reemplazar_mediana":
                expr_relleno = f"List.Median(List.RemoveNulls(Table.Column({{prev}}, {_m_str(col)})))"
            elif a_faltante == "reemplazar_media":
                expr_relleno = f"List.Average(List.RemoveNulls(Table.Column({{prev}}, {_m_str(col)})))"
            elif a_faltante == "reemplazar_moda":
                expr_relleno = f"List.Mode(List.RemoveNulls(Table.Column({{prev}}, {_m_str(col)})))"
            else:
                valor_fijo_col = valores_fijos.get(col)
                expr_relleno = repr(valor_fijo_col) if isinstance(valor_fijo_col, (int, float)) else _m_str(valor_fijo_col)
            cb.agregar(f"SinFaltantes_{nombre_col_id}",
                       "Table.ReplaceValue({prev}, null, " + expr_relleno +
                       f", Replacer.ReplaceValue, {{{_m_str(col)}}})")
        elif a_faltante == "marcar_solo" and _incluir_revision(f"Revisar_Faltante_{col}"):
            cb.agregar(f"RevisarFaltante_{nombre_col_id}",
                       "Table.AddColumn({prev}, " + _m_str(f"Revisar_Faltante_{col}") +
                       f", each [{col}] = null, type logical)")
            cb.bandera(f"Revisar_Faltante_{col}")

    # -- 6b) Texto: faltante (valor_fijo / marcar_solo / reemplazar_moda) -------
    # Las columnas de puro texto (ej. linea_direccion2, region) no entran en
    # columnas_numericas_potenciales, asi que sin este bloque quedaban fuera
    # del bloque 6) de arriba por completo: elegir "Reemplazar por un valor
    # fijo" (ej. "No aplica") para una columna de texto no generaba NINGUN
    # paso M, y el valor fijo nunca se aplicaba al pegar el codigo en Power
    # BI (los nulos de esa columna quedaban intactos).
    columnas_excluidas_texto_faltante = columnas_excluidas_numerico | set(cols_fecha) | set(cols_email)
    columnas_texto_faltante = [c for c in df.columns
                                if c not in columnas_excluidas_texto_faltante
                                and c not in columnas_numericas_potenciales]
    for col in columnas_texto_faltante:
        nombre_col_id = re.sub(r'[^A-Za-z0-9]', '', col)
        if a_faltante == "valor_fijo":
            valor_fijo_col = valores_fijos.get(col)
            expr_relleno = repr(valor_fijo_col) if isinstance(valor_fijo_col, (int, float)) else _m_str(valor_fijo_col)
            cb.agregar(f"SinFaltantesTexto_{nombre_col_id}",
                       "Table.ReplaceValue({prev}, null, " + expr_relleno +
                       f", Replacer.ReplaceValue, {{{_m_str(col)}}})")
        elif a_faltante == "reemplazar_moda":
            expr_relleno = f"List.Mode(List.RemoveNulls(Table.Column({{prev}}, {_m_str(col)})))"
            cb.agregar(f"SinFaltantesTexto_{nombre_col_id}",
                       "Table.ReplaceValue({prev}, null, " + expr_relleno +
                       f", Replacer.ReplaceValue, {{{_m_str(col)}}})")
        elif a_faltante == "marcar_solo" and _incluir_revision(f"Revisar_Faltante_{col}"):
            cb.agregar(f"RevisarFaltanteTexto_{nombre_col_id}",
                       "Table.AddColumn({prev}, " + _m_str(f"Revisar_Faltante_{col}") +
                       f", each [{col}] = null, type logical)")
            cb.bandera(f"Revisar_Faltante_{col}")

    if necesita_percentil_fn:
        pass  # la funcion se inyecta al final del bloque de atipicos

    if a_atipico != "marcar_solo" or True:
        for col in columnas_numericas_potenciales:
            nombre_col_id = re.sub(r'[^A-Za-z0-9]', '', col)
            if a_atipico == "limitar":
                cb.agregar(
                    f"LimitesAtipico_{nombre_col_id}",
                    (f"let _lista = List.RemoveNulls(Table.Column({{prev}}, {_m_str(col)})), "
                     f"_q1 = Percentil(_lista, 0.25), _q3 = Percentil(_lista, 0.75), "
                     f"_iqr = if _q1 = null or _q3 = null then null else _q3 - _q1, "
                     f"_li = if _iqr = null then null else _q1 - {factor_iqr} * _iqr, "
                     f"_ls = if _iqr = null then null else _q3 + {factor_iqr} * _iqr "
                     f"in Table.TransformColumns({{prev}}, {{{{{_m_str(col)}, each "
                     f"if _ = null or _li = null then _ else if _ < _li then _li else if _ > _ls then _ls else _"
                     f", type number}}}})")
                )
            elif a_atipico == "marcar_solo" and _incluir_revision(f"Revisar_Atipico_{col}"):
                cb.agregar(
                    f"RevisarAtipico_{nombre_col_id}",
                    (f"let _lista = List.RemoveNulls(Table.Column({{prev}}, {_m_str(col)})), "
                     f"_q1 = Percentil(_lista, 0.25), _q3 = Percentil(_lista, 0.75), "
                     f"_iqr = if _q1 = null or _q3 = null then null else _q3 - _q1, "
                     f"_li = if _iqr = null then null else _q1 - {factor_iqr} * _iqr, "
                     f"_ls = if _iqr = null then null else _q3 + {factor_iqr} * _iqr "
                     f"in Table.AddColumn({{prev}}, {_m_str(f'Revisar_Atipico_{col}')}, each "
                     f"[{col}] <> null and _li <> null and ([{col}] < _li or [{col}] > _ls), type logical)")
                )
                cb.bandera(f"Revisar_Atipico_{col}")
                necesita_percentil_fn = True
            if a_atipico == "limitar":
                necesita_percentil_fn = True

    # -- 7) Formula (Total = Cantidad x Precio) -------------------------------
    if hay_formula:
        cb.agregar("FormulaEsperada",
                   "Table.AddColumn({prev}, \"_total_esperado\", each "
                   f"if [{col_cant}] = null or [{col_precio}] = null then null else [{col_cant}] * [{col_precio}], type number)")
        if a_formula == "usar_sugerido":
            # Se reemplaza la columna de total por el valor recalculado
            # (Cantidad x Precio) manteniendo el nombre original, en vez de
            # intentar "editar en el lugar" con Table.TransformRows (que
            # perderia los tipos de columna al reconstruir la tabla).
            cb.agregar("SinTotalOriginal", 'Table.RemoveColumns({prev}, {' + _m_str(col_total) + '})')
            cb.agregar("FormulaCorregida",
                       'Table.RenameColumns({prev}, {{"_total_esperado", ' + _m_str(col_total) + '}})')
        else:
            if _incluir_revision("Revisar_Formula"):
                tolerancia_check = (
                    f"[_total_esperado] <> null and [{col_total}] <> null and "
                    f"Number.Abs([{col_total}] - [_total_esperado]) > (Number.Abs([_total_esperado]) * {tolerancia_formula} + 0.01)"
                )
                cb.agregar("RevisarFormula", "Table.AddColumn({prev}, \"Revisar_Formula\", each " + tolerancia_check + ", type logical)")
                cb.bandera("Revisar_Formula")
            cb.agregar("SinTotalEsperado", 'Table.RemoveColumns({prev}, {"_total_esperado"})')

    # -- 8) Telefono -----------------------------------------------------------
    # min_d_tel/max_d_tel: digitos_telefono explicito manda; si no, se calcula
    # por pais(es) (union de rangos, para aceptar la mayoria de formatos de
    # celular de esos paises a la vez); si no se da ninguno de los dos, cae
    # al rango internacional amplio (7-15 digitos) en vez de un solo pais.
    min_d_tel, max_d_tel = digitos_telefono if digitos_telefono is not None \
        else _rango_digitos_telefono(paises_telefono)

    # Columna/indice usado para identificar cada registro en
    # TablaCorreccionesDigitosTelefono: se prefiere una columna ID real ya
    # detectada; si el dataset no tiene, se usa el indice de fila agregado en
    # el bloque 1.5 (compartido con TablaCorreccionesIndividuales, para no
    # agregar la misma columna de indice dos veces). OJO: un indice de fila
    # asume que el orden de los datos no cambia entre refrescos; si el origen
    # puede reordenarse, es preferible tener una columna ID real.

    # Filas horneadas para TablaCorreccionesDigitosTelefono a partir de lo
    # editado en el paso "Editar cada uno por separado" de la app (si lo hubo).
    filas_correccion_tel: List[Tuple[object, str, int, str]] = []
    if necesita_id_correccion_tel:
        for _col_tel in cols_tel:
            if _col_tel in df.columns:
                filas_correccion_tel.extend(
                    _preparar_correcciones_telefono(df, correcciones_individuales, _col_tel, id_ref_col)
                )

    for col in cols_tel:
        if col not in df.columns:
            continue
        nombre_col_id = re.sub(r'[^A-Za-z0-9]', '', col)

        # 8a) Aplicar primero las correcciones manuales de digitos puntuales
        # (si las hay en TablaCorreccionesDigitosTelefono), para que un
        # registro corregido deje de marcarse como invalido de aqui en
        # adelante. Se hace via AddColumn + remove + rename (en vez de
        # TransformColumns) porque hace falta el ID de la fila, no solo el
        # valor de la celda.
        if desglosar_digitos_telefono or a_tel == "editar_individualmente":
            cb.agregar(f"TelCorreccion_{nombre_col_id}",
                       "Table.AddColumn({prev}, \"_tel_corr_" + nombre_col_id + "\", each "
                       f"AplicarCorreccionDigito([{id_ref_col}], {_m_str(col)}, "
                       f"if [{col}] = null then null else Text.From([{col}])), type text)")
            cb.agregar(f"TelSinOriginal_{nombre_col_id}", 'Table.RemoveColumns({prev}, {' + _m_str(col) + '})')
            cb.agregar(f"TelRenombrado_{nombre_col_id}",
                       'Table.RenameColumns({prev}, {{"_tel_corr_' + nombre_col_id + '", ' + _m_str(col) + '}})')

        # 8b) Chequeo de formato/longitud. Con permitir_codigo_pais_telefono
        # activo tambien se acepta el mismo numero con 1-3 digitos extra al
        # inicio (codigo de pais sin "+"), para no rechazar numeros que
        # vienen con codigo de pais incluido.
        chequeo_largo_base = f"Text.Length(_soloDigitos) >= {min_d_tel} and Text.Length(_soloDigitos) <= {max_d_tel}"
        if min_d_tel == max_d_tel:
            chequeo_largo_base = f"Text.Length(_soloDigitos) = {min_d_tel}"
        if permitir_codigo_pais_telefono:
            chequeo_largo = (f"({chequeo_largo_base}) or "
                              f"(Text.Length(_soloDigitos) >= {min_d_tel + 1} and Text.Length(_soloDigitos) <= {max_d_tel + 3})")
        else:
            chequeo_largo = chequeo_largo_base

        if a_tel == "valor_fijo":
            chequeo_primer_digito = ""
            if primeros_digitos_telefono_validos:
                lista_d = "{" + ", ".join(_m_str(d) for d in primeros_digitos_telefono_validos) + "}"
                chequeo_primer_digito = f" and List.Contains({lista_d}, Text.Start(_soloDigitos, 1))"
            cb.agregar(f"TelefonoLimpio_{nombre_col_id}",
                       "Table.TransformColumns({prev}, {{" + _m_str(col) +
                       f", each if _ = null then null else let _soloDigitos = Text.Select(_, {{\"0\"..\"9\"}}) "
                       f"in if ({chequeo_largo}{chequeo_primer_digito}) then _soloDigitos else null, type text}}}})")
        else:
            lista_d_flag = ""
            if primeros_digitos_telefono_validos:
                lista_d = "{" + ", ".join(_m_str(d) for d in primeros_digitos_telefono_validos) + "}"
                lista_d_flag = f" or not List.Contains({lista_d}, Text.Start(Text.Select(Text.From([{col}]), {{\"0\"..\"9\"}}), 1))"
            chequeo_largo_col = chequeo_largo.replace('_soloDigitos', f'Text.Select(Text.From([{col}]), {{"0".."9"}})')
            if _incluir_revision(f"Revisar_Telefono_{col}"):
                cb.agregar(f"RevisarTelefono_{nombre_col_id}",
                           "Table.AddColumn({prev}, " + _m_str(f"Revisar_Telefono_{col}") +
                           f", each [{col}] = null or not ({chequeo_largo_col}){lista_d_flag}, type logical)")
                cb.bandera(f"Revisar_Telefono_{col}")

        # 8c) Desglose caracter por caracter, para VISUALIZAR exactamente en
        # que posicion esta el error (ej. una letra en vez de un digito, o
        # un digito extra/faltante) y poder corregirlo agregando una fila en
        # TablaCorreccionesDigitosTelefono (definida al inicio del query).
        # Nota: se usan variables con nombre (_v, _t) en vez de "_" dentro de
        # los "each" anidados, porque "_" se reasigna al elemento de la
        # lista en List.Select/List.Transform y pisaria el valor de la fila.
        if desglosar_digitos_telefono:
            for n in range(1, max_d_tel + 1):
                cb.agregar(f"{nombre_col_id}_Digito_{n}",
                           "Table.AddColumn({prev}, " + _m_str(f"{col}_Digito_{n}") +
                           f", each let _v = [{col}] in if _v = null then null else "
                           f"let _t = Text.From(_v) in if Text.Length(_t) >= {n} "
                           f"then Text.Range(_t, {n - 1}, 1) else null, type text)")
            cb.agregar(f"{nombre_col_id}_PosicionesInvalidas",
                       "Table.AddColumn({prev}, " + _m_str(f"{col}_Posiciones_Invalidas") +
                       f", each let _v = [{col}] in if _v = null then null else "
                       f"let _t = Text.From(_v) in Text.Combine(List.Transform(List.Select("
                       f"List.Numbers(1, Text.Length(_t)), each not List.Contains({{\"0\"..\"9\"}}, "
                       f"Text.Range(_t, _ - 1, 1))), Text.From), \", \"), type text)")

    # -- 9) Email ---------------------------------------------------------------
    for col in cols_email:
        if col not in df.columns:
            continue
        nombre_col_id = re.sub(r'[^A-Za-z0-9]', '', col)
        chequeo_email = (
            f"[{col}] = null or not Text.Contains([{col}], \"@\") "
            f"or Text.StartsWith([{col}], \"@\") or Text.EndsWith([{col}], \"@\") "
            f"or not Text.Contains(Text.AfterDelimiter([{col}], \"@\"), \".\") "
            f"or Text.EndsWith([{col}], \".\")"
        )
        if a_email == "valor_fijo":
            valor_fijo_email = valores_fijos.get(col, None)
            reemplazo = _m_str(valor_fijo_email) if valor_fijo_email is not None else "null"
            cb.agregar(f"EmailLimpio_{nombre_col_id}",
                       "Table.TransformColumns({prev}, {{" + _m_str(col) +
                       f", each if _ = null then null else Text.Lower(Text.Remove(Text.Trim(_), \" \")), type text}}}})")
            cb.agregar(f"EmailValidado_{nombre_col_id}",
                       "Table.TransformColumns({prev}, {{" + _m_str(col) +
                       f", each if _ <> null and ({chequeo_email.replace(f'[{col}]', '_')}) then {reemplazo} else _, type text}}}})")
        else:
            cb.agregar(f"EmailLimpio_{nombre_col_id}",
                       "Table.TransformColumns({prev}, {{" + _m_str(col) +
                       f", each if _ = null then null else Text.Lower(Text.Remove(Text.Trim(_), \" \")), type text}}}})")
            if _incluir_revision(f"Revisar_Email_{col}"):
                cb.agregar(f"RevisarEmail_{nombre_col_id}",
                           "Table.AddColumn({prev}, " + _m_str(f"Revisar_Email_{col}") + f", each {chequeo_email}, type logical)")
                cb.bandera(f"Revisar_Email_{col}")

    # -- 10) Columna final Requiere_Revision -------------------------------------
    if cb.banderas:
        expr = " or ".join(f"[{b}]" for b in cb.banderas)
        cb.agregar("RevisionFinal", "Table.AddColumn({prev}, \"Requiere_Revision\", each " + expr + ", type logical)")

    funciones_extra = ""
    if necesita_fecha_fn:
        funciones_extra += _M_FUNCION_FECHA + "\n"
    if necesita_percentil_fn:
        funciones_extra += _M_FUNCION_PERCENTIL + "\n"
    if necesita_correccion_digitos_fn:
        funciones_extra += _m_funcion_correccion_digitos(filas_correccion_tel) + "\n"
    if necesita_correccion_individual_fn:
        funciones_extra += _m_funcion_correccion_individual(filas_correccion_individual) + "\n"

    cuerpo_m = cb.construir(funciones_extra)
    encabezado = (
        "// =============================================================================\n"
        "// Codigo M 100% nativo generado por Limpiador de Tablas (sin Python.Execute).\n"
        "// Reemplaza en el Editor avanzado, ajustando el nombre del primer paso\n"
        f'// ("{nombre_paso_anterior}") por el nombre real de tu ultimo paso previo.\n'
        + ("".join(c + "\n" for c in comentarios) if comentarios else "")
        + "// =============================================================================\n\n"
    )
    return encabezado + cuerpo_m

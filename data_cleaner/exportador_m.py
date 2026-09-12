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
    PATRONES_NO_TELEFONO as _PATRONES_NO_TELEFONO,
    PATRONES_ESTADO as _PATRONES_ESTADO,
    VALORES_ESTADO_VALIDOS as _VALORES_ESTADO_VALIDOS,
    PATRONES_NOMBRE_PROPIO as _PATRONES_NOMBRE_PROPIO,
    columnas_por_patron as _columnas_por_patron,
    es_columna_id as _es_columna_id,
    detectar_columnas as _detectar_columnas,
    detectar_columnas_fecha as _detectar_columnas_fecha,
    parece_email as _parece_email,
    parece_telefono as _parece_telefono,
    parece_fecha as _parece_fecha,
    rango_digitos_telefono as _rango_digitos_telefono,
)
_PATRONES_EXCLUIR_TEXTO = _PATRONES_EMAIL + _PATRONES_TELEFONO + _PATRONES_FECHA + _PATRONES_ESTADO + \
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
    "estado_invalido": {"valor_fijo", "marcar_solo", "eliminar_fila", "editar_individualmente"},
    "capitalizacion_incorrecta": {"usar_sugerido", "marcar_solo", "eliminar_fila", "editar_individualmente"},
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


def _buscar_valor_fijo(valores_fijos: dict, tipo: str, columna: str):
    """Busca el valor fijo especifico para (tipo, columna). Si no esta ahi,
    cae al valor fijo generico por columna (compatibilidad con dicts viejos
    que no distinguian el tipo de problema, ej. desde cli.py o api.py)."""
    if (tipo, columna) in valores_fijos:
        return valores_fijos[(tipo, columna)]
    return valores_fijos.get(columna)


def _paso_relleno_valor_fijo(cb, comentarios, nombre_paso: str, col: str,
                              valores_fijos: dict, tipo: str) -> None:
    """Agrega un paso Table.ReplaceValue que rellena nulos de 'col' con el
    valor fijo configurado para (tipo, col). Si no hay ningun valor fijo
    guardado (la app deberia haberlo exigido, pero por si llega vacio via
    API/CLI), no genera un paso roto con el texto literal "None": deja un
    aviso explicando que falta configurar el valor.
    """
def _es_null_explicito(valor) -> bool:
    """True si el usuario escribio literalmente "null" (sin importar
    mayusculas) como valor fijo -- lo interpretamos como el null real de M,
    no como el texto "null". Util, por ejemplo, para forzar a null los datos
    que no encajan al convertir una columna a booleano."""
    return isinstance(valor, str) and valor.strip().lower() == "null"


def _expr_valor_fijo_m(valor_fijo_col) -> str:
    """Literal M para un valor fijo: null real si el usuario escribio
    "null", numero sin comillas si es numerico, o texto citado si no."""
    if _es_null_explicito(valor_fijo_col):
        return "null"
    return repr(valor_fijo_col) if isinstance(valor_fijo_col, (int, float)) else _m_str(valor_fijo_col)


def _paso_relleno_valor_fijo(cb, comentarios, nombre_paso: str, col: str,
                              valores_fijos: dict, tipo: str) -> None:
    """Agrega un paso Table.ReplaceValue que rellena nulos de 'col' con el
    valor fijo configurado para (tipo, col). Si no hay ningun valor fijo
    guardado (la app deberia haberlo exigido, pero por si llega vacio via
    API/CLI), no genera un paso roto con el texto literal "None": deja un
    aviso explicando que falta configurar el valor.
    """
    valor_fijo_col = _buscar_valor_fijo(valores_fijos, tipo, col)
    if valor_fijo_col is None:
        comentarios.append(
            f'  // AVISO: no hay un valor fijo configurado para "{tipo}" en la columna '
            f'"{col}"; no se genero ningun paso de relleno para esta columna.'
        )
        return
    expr_relleno = _expr_valor_fijo_m(valor_fijo_col)
    cb.agregar(nombre_paso,
               "Table.ReplaceValue({prev}, null, " + expr_relleno +
               f", Replacer.ReplaceValue, {{{_m_str(col)}}})")


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

_M_FUNCION_RECORTAR_TEXTO = '''  // Recorta espacios solo si el valor YA es texto en tiempo de ejecucion.
  // Necesaria porque el trim se aplica a TODAS las columnas del origen: si
  // se decidiera por columna segun el dtype visto por pandas al analizar
  // (heuristica anterior), una columna que en Power Query ya llega tipada
  // como numero/fecha (ej. por un Excel.Workbook + Int64.Type/type date
  // previo) podia terminar recibiendo Text.Trim() y tronar en tiempo de
  // ejecucion. Con este chequeo, los valores no-texto (numero, fecha,
  // logico, null) se dejan intactos sin importar lo que haya detectado
  // pandas.
  RecortarSiEsTexto = (valor as any) as any =>
    if valor = null then null
    else if Value.Is(valor, type text) then Text.Trim(valor)
    else valor,
'''

_M_FUNCION_CAPITALIZAR = '''  // Convierte un texto a "Formato Nombre Propio" -- ver
  // capitalizar_nombre_propio() en data_cleaner/patrones.py (misma regla:
  // conectores en minuscula salvo la primera palabra, siglas conocidas en
  // mayuscula). Simplificacion frente a la version Python: aqui las
  // palabras se separan solo por espacio (no por guion), asi que un
  // apellido compuesto con guion ("rodriguez-solano") se capitaliza como
  // una sola palabra ("Rodriguez-solano") en vez de por cada mitad.
  ConectoresNombrePropio = {"de","del","la","las","los","y","e","en","a","al","con","para","por","van","von","der","da","do","dos","das"},
  SiglasNombrePropio = {"sa","srl","ltda","llc","inc","corp","sac","eirl","cia","sl","sau","spa","gmbh","plc","ii","iii","iv","vi","vii","viii","ix","jr","sr","md","phd"},
  CapitalizarPalabraNombre = (p as text) as text =>
    if p = "" then p
    else
        let
            limpia = Text.Lower(Text.Select(p, {"A".."Z", "a".."z"})),
            esSigla = List.Contains(SiglasNombrePropio, limpia)
        in
            if esSigla then Text.Upper(p)
            else if Text.Contains(p, "'") then
                Text.Combine(
                    List.Transform(Text.Split(p, "'"),
                        each if _ = "" then _ else Text.Upper(Text.Start(_, 1)) & Text.Lower(Text.Range(_, 1))),
                    "'"
                )
            else Text.Upper(Text.Start(p, 1)) & Text.Lower(Text.Range(p, 1)),
  CapitalizarNombrePropio = (t as nullable text) as nullable text =>
    if t = null then null
    else
        let
            palabras = Text.Split(Text.Trim(t), " "),
            procesadas = List.Transform(
                List.Positions(palabras),
                (i) => let p = palabras{i} in
                    if i > 0 and List.Contains(ConectoresNombrePropio, Text.Lower(p)) then Text.Lower(p)
                    else CapitalizarPalabraNombre(p)
            )
        in
            Text.Combine(procesadas, " "),
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

    def agregar(self, nombre: str, formula_con_placeholder: str) -> str:
        """formula_con_placeholder puede usar el texto literal {prev} para referirse
        al paso anterior. Se usa un simple .replace() (no .format()) porque el
        propio codigo M esta lleno de llaves { } que .format() interpretaria mal."""
        formula = formula_con_placeholder.replace("{prev}", _m_ident(self.ultimo))
        self.pasos.append((nombre, formula))
        self.ultimo = nombre
        return nombre

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
    estado_invalido: str = "marcar_solo",
    capitalizacion_incorrecta: str = "marcar_solo",
    columnas_fecha: Optional[List[str]] = None,
    fecha_min: Optional[str] = None,
    fecha_max: Optional[str] = None,
    columnas_email: Optional[List[str]] = None,
    columnas_telefono: Optional[List[str]] = None,
    digitos_telefono: Optional[Tuple[int, int]] = None,
    paises_telefono: Optional[List[str]] = None,
    permitir_codigo_pais_telefono: bool = True,
    primeros_digitos_telefono_validos: Optional[List[str]] = None,
    columnas_id: Optional[List[str]] = None,
    columna_total: Optional[str] = None,
    columna_cantidad: Optional[str] = None,
    columna_precio: Optional[str] = None,
    tolerancia_formula: float = 0.01,
    columnas_texto: Optional[List[str]] = None,
    umbral_similitud_texto: float = 0.85,
    max_cardinalidad_ratio_texto: float = 0.5,
    columnas_estado: Optional[List[str]] = None,
    valores_estado_validos: Optional[List[str]] = None,
    columnas_capitalizacion: Optional[List[str]] = None,
    # --- correcciones puntuales (accion 'editar_individualmente') ---
    correcciones_individuales: Optional[Dict[tuple, object]] = None,
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

    IMPORTANTE: este generador NUNCA agrega columnas nuevas al resultado
    final (ni columnas Revisar_*, ni Requiere_Revision, ni el desglose de
    telefono por digito). Cuando una regla queda en "marcar_solo" (o en
    cualquier accion que antes se resolvia agregando una columna de
    bandera), simplemente no se genera ningun paso para esa columna y se
    deja un comentario aclarandolo en el M generado; el dato de origen no
    se toca. Las columnas de correccion puntual usadas internamente
    ("editar_individualmente") siguen funcionando porque reemplazan la
    columna existente (via RemoveColumns + RenameColumns), no agregan una
    nueva.
    """
    cfg_basico = {"faltante": "reemplazar_mediana", "duplicado": "eliminar_fila",
                  "atipico": "limitar", "tipo_invalido": "marcar_solo", **(config or {})}
    valores_fijos = valores_fijos or {}
    comentarios: List[str] = []

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
    a_estado = _accion_o_fallback("estado_invalido", estado_invalido, comentarios)
    a_capitalizacion = _accion_o_fallback("capitalizacion_incorrecta", capitalizacion_incorrecta, comentarios)

    # Deteccion en 2 niveles: por nombre de columna primero (rapido); si
    # eso no encuentra nada, se revisan los VALORES reales como respaldo
    # (cubre datasets con columnas mal nombradas: "col_1", "campo_7", etc.).
    # El orden fecha -> email -> telefono importa: cada deteccion excluye
    # las columnas que ya reclamo una regla anterior, para que una columna
    # de fecha con muchos digitos no se confunda con telefono, etc.
    cols_fecha = columnas_fecha if columnas_fecha is not None else \
        _detectar_columnas_fecha(df, _parece_fecha)
    cols_email = columnas_email if columnas_email is not None else \
        _detectar_columnas(df, _PATRONES_EMAIL, _parece_email, excluir=cols_fecha)
    cols_tel = columnas_telefono if columnas_telefono is not None else \
        _detectar_columnas(df, _PATRONES_TELEFONO, _parece_telefono, excluir=cols_fecha + cols_email,
                            excluir_por_nombre=_PATRONES_NO_TELEFONO)
    cols_id = columnas_id if columnas_id is not None else [c for c in df.columns if _es_columna_id(c)]
    cols_texto = columnas_texto if columnas_texto is not None else _columnas_candidatas_texto(df, max_cardinalidad_ratio_texto)
    cols_estado = columnas_estado if columnas_estado is not None else _columnas_por_patron(df, _PATRONES_ESTADO)
    valores_estado = valores_estado_validos if valores_estado_validos is not None else _VALORES_ESTADO_VALIDOS
    cols_capitalizacion = columnas_capitalizacion if columnas_capitalizacion is not None else \
        [c for c in _columnas_por_patron(df, _PATRONES_NOMBRE_PROPIO)
         if pd.api.types.is_object_dtype(df[c]) or pd.api.types.is_string_dtype(df[c])]
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
    necesita_correccion_digitos_fn = bool(cols_tel) and a_tel == "editar_individualmente"
    necesita_recortar_fn = False

    # -- 1) Duplicados de fila completa --------------------------------------
    if a_duplicado == "eliminar_fila":
        cb.agregar("SinDuplicados", "Table.Distinct({prev})")
    elif a_duplicado == "marcar_solo":
        comentarios.append(
            '  // AVISO: "duplicado" quedo en "marcar_solo", pero el codigo M puro '
            'no agrega columnas nuevas (ni siquiera Revisar_Duplicado); no se genero '
            'ningun paso para esta regla.'
        )

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
        "estado_invalido": a_estado, "capitalizacion_incorrecta": a_capitalizacion,
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

    necesita_id_correccion_tel = bool(cols_tel) and a_tel == "editar_individualmente"
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
    # Se aplica a TODAS las columnas: el chequeo de "es texto de verdad" pasa
    # a la funcion RecortarSiEsTexto, que corre en tiempo de ejecucion dentro
    # de Power Query. Antes se decidia que columnas incluir segun el dtype
    # que pandas veia al analizar (is_object_dtype / is_string_dtype), lo que
    # rompia el paso si la columna real en Power Query ya llegaba tipada
    # como numero/fecha desde un origen previo (ej. Excel.Workbook +
    # Int64.Type/type date) aunque pandas la hubiera visto como texto.
    if len(df.columns) > 0:
        necesita_recortar_fn = True
        pares = ", ".join(f'{{{_m_str(c)}, each RecortarSiEsTexto(_)}}' for c in df.columns)
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
            else:  # marcar_solo -> el M puro ya no agrega columnas de bandera
                comentarios.append(
                    f'  // AVISO: "texto_inconsistente" quedo en "marcar_solo" para la '
                    f'columna "{col}", pero el codigo M puro no agrega columnas nuevas '
                    f'(ni Revisar_Texto_{col}); no se genero ningun paso para esta columna.'
                )

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
                comentarios.append(
                    f'  // AVISO: "fecha_invalida" quedo en "marcar_solo" para la columna '
                    f'"{col}", pero el codigo M puro no agrega columnas nuevas (ni '
                    f'{nombre_bandera}); solo se convirtieron las fechas validas.'
                )

        # 4b) Faltantes: igual que con email, esta columna queda excluida del
        # bloque generico de texto (6b) porque el relleno debe pasar por un
        # #date(...) en vez de un simple string. OJO: esto solo es seguro si
        # la columna ya quedo convertida a tipo fecha en el paso 4a de arriba
        # (a_fecha en valor_fijo/marcar_solo); si "fecha_invalida" usa otra
        # accion, la columna sigue en su tipo original y forzar un #date(...)
        # ahi genera un choque de tipos en Power Query.
        columna_ya_es_fecha = a_fecha in ("valor_fijo", "marcar_solo")
        nombre_col_id_fecha = re.sub(r'[^A-Za-z0-9]', '', col)
        if not columna_ya_es_fecha and a_faltante in ("valor_fijo", "reemplazar_moda"):
            comentarios.append(
                f'  // AVISO: "faltante" no se pudo aplicar a la columna de fecha "{col}" '
                f'porque "fecha_invalida" no esta en valor_fijo/marcar_solo, asi que la '
                f'columna nunca se convierte a tipo fecha; para rellenar los vacios, elija '
                f'valor_fijo o marcar_solo para "fecha_invalida" en esta columna.'
            )
        elif a_faltante == "valor_fijo":
            valor_fijo_col = _buscar_valor_fijo(valores_fijos, "faltante", col)
            if _es_null_explicito(valor_fijo_col):
                pass  # el usuario quiere dejarlo en null real: no hace falta ningun paso
            else:
                fecha_partes = None
                if valor_fijo_col not in (None, ""):
                    try:
                        fecha_partes = [int(x) for x in str(valor_fijo_col).split("-")]
                    except ValueError:
                        fecha_partes = None
                if fecha_partes and len(fecha_partes) == 3:
                    expr_relleno = f"#date({fecha_partes[0]}, {fecha_partes[1]}, {fecha_partes[2]})"
                    cb.agregar(f"SinFaltantesFecha_{nombre_col_id_fecha}",
                               "Table.ReplaceValue({prev}, null, " + expr_relleno +
                               f", Replacer.ReplaceValue, {{{_m_str(col)}}})")
                else:
                    comentarios.append(
                        f'  // AVISO: el valor fijo configurado para "faltante" en la columna de '
                        f'fecha "{col}" ({valor_fijo_col!r}) no se pudo interpretar como fecha '
                        f'AAAA-MM-DD; no se genero ningun paso de relleno para esta columna.'
                    )
        elif a_faltante == "reemplazar_moda":
            expr_relleno = f"List.Mode(List.RemoveNulls(Table.Column({{prev}}, {_m_str(col)})))"
            cb.agregar(f"SinFaltantesFecha_{nombre_col_id_fecha}",
                       "Table.ReplaceValue({prev}, null, " + expr_relleno +
                       f", Replacer.ReplaceValue, {{{_m_str(col)}}})")
        elif a_faltante == "marcar_solo":
            comentarios.append(
                f'  // AVISO: "faltante" quedo en "marcar_solo" para la columna "{col}", '
                f'pero el codigo M puro no agrega columnas nuevas (ni Revisar_Faltante_{col}); '
                f'no se genero ningun paso para esta columna.'
            )

    # -- 5) ID duplicado --------------------------------------------------------
    for col in cols_id:
        if col not in df.columns:
            continue
        nombre_bandera_id = f"Revisar_ID_Duplicado_{col}" if len(cols_id) > 1 else "Revisar_ID_Duplicado"
        if a_id != "eliminar_fila":
            comentarios.append(
                f'  // AVISO: "id_duplicado" quedo en "{a_id}" para la columna "{col}", '
                f'pero el codigo M puro no agrega columnas nuevas (ni {nombre_bandera_id}); '
                f'no se genero ningun paso para esta columna.'
            )
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
        # Solo "eliminar_fila" llega hasta aqui (ver el "continue" de arriba
        # para cualquier otra accion, ya que las demas requerian agregar una
        # columna Revisar_ID_Duplicado_* que este generador ya no crea).
        cb.agregar(f"SinIDDup_{re.sub(r'[^A-Za-z0-9]', '', col)}",
                   'Table.Distinct(Table.RemoveColumns(Table.Sort({prev}, {{"_conteo_id", Order.Ascending}}), {"_conteo_id"}), {' + _m_str(col) + '})')

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
        if a_faltante in ("reemplazar_mediana", "reemplazar_media", "reemplazar_moda"):
            if a_faltante == "reemplazar_mediana":
                expr_relleno = f"List.Median(List.RemoveNulls(Table.Column({{prev}}, {_m_str(col)})))"
            elif a_faltante == "reemplazar_media":
                expr_relleno = f"List.Average(List.RemoveNulls(Table.Column({{prev}}, {_m_str(col)})))"
            else:
                expr_relleno = f"List.Mode(List.RemoveNulls(Table.Column({{prev}}, {_m_str(col)})))"
            cb.agregar(f"SinFaltantes_{nombre_col_id}",
                       "Table.ReplaceValue({prev}, null, " + expr_relleno +
                       f", Replacer.ReplaceValue, {{{_m_str(col)}}})")
        elif a_faltante == "valor_fijo":
            _paso_relleno_valor_fijo(cb, comentarios, f"SinFaltantes_{nombre_col_id}", col, valores_fijos, "faltante")
        elif a_faltante == "marcar_solo":
            comentarios.append(
                f'  // AVISO: "faltante" quedo en "marcar_solo" para la columna "{col}", '
                f'pero el codigo M puro no agrega columnas nuevas (ni Revisar_Faltante_{col}); '
                f'no se genero ningun paso para esta columna.'
            )

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
            _paso_relleno_valor_fijo(cb, comentarios, f"SinFaltantesTexto_{nombre_col_id}", col, valores_fijos, "faltante")
        elif a_faltante == "reemplazar_moda":
            expr_relleno = f"List.Mode(List.RemoveNulls(Table.Column({{prev}}, {_m_str(col)})))"
            cb.agregar(f"SinFaltantesTexto_{nombre_col_id}",
                       "Table.ReplaceValue({prev}, null, " + expr_relleno +
                       f", Replacer.ReplaceValue, {{{_m_str(col)}}})")
        elif a_faltante == "marcar_solo":
            comentarios.append(
                f'  // AVISO: "faltante" quedo en "marcar_solo" para la columna "{col}", '
                f'pero el codigo M puro no agrega columnas nuevas (ni Revisar_Faltante_{col}); '
                f'no se genero ningun paso para esta columna.'
            )

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
            elif a_atipico == "marcar_solo":
                comentarios.append(
                    f'  // AVISO: "atipico" quedo en "marcar_solo" para la columna "{col}", '
                    f'pero el codigo M puro no agrega columnas nuevas (ni Revisar_Atipico_{col}); '
                    f'no se genero ningun paso para esta columna.'
                )
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
            comentarios.append(
                f'  // AVISO: "formula_incorrecta" quedo en "{a_formula}", pero el codigo '
                f'M puro no agrega columnas nuevas (ni Revisar_Formula); no se genero '
                f'ningun paso de marcado para esta regla.'
            )
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

        # 7a) Faltantes: igual que email/fecha, esta columna queda excluida
        # del bloque numerico (para no perder ceros a la izquierda) y del de
        # texto (6b), asi que sin este paso la accion configurada para
        # "faltante" nunca se aplicaba a los telefonos vacios.
        if a_faltante == "valor_fijo":
            _paso_relleno_valor_fijo(cb, comentarios, f"SinFaltantesTelefono_{nombre_col_id}", col, valores_fijos, "faltante")
        elif a_faltante == "reemplazar_moda":
            expr_relleno = f"List.Mode(List.RemoveNulls(Table.Column({{prev}}, {_m_str(col)})))"
            cb.agregar(f"SinFaltantesTelefono_{nombre_col_id}",
                       "Table.ReplaceValue({prev}, null, " + expr_relleno +
                       f", Replacer.ReplaceValue, {{{_m_str(col)}}})")
        elif a_faltante == "marcar_solo":
            comentarios.append(
                f'  // AVISO: "faltante" quedo en "marcar_solo" para la columna "{col}", '
                f'pero el codigo M puro no agrega columnas nuevas (ni Revisar_Faltante_{col}); '
                f'no se genero ningun paso para esta columna.'
            )

        # 8a) Aplicar primero las correcciones manuales de digitos puntuales
        # (si las hay en TablaCorreccionesDigitosTelefono), para que un
        # registro corregido deje de marcarse como invalido de aqui en
        # adelante. Se hace via AddColumn + remove + rename (en vez de
        # TransformColumns) porque hace falta el ID de la fila, no solo el
        # valor de la celda.
        if a_tel == "editar_individualmente":
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
            comentarios.append(
                f'  // AVISO: "telefono_invalido" quedo en "{a_tel}" para la columna '
                f'"{col}", pero el codigo M puro no agrega columnas nuevas (ni '
                f'Revisar_Telefono_{col}); no se genero ningun paso para esta columna.'
            )

        # 8c) El desglose caracter-por-caracter (columnas "<col>_Digito_N" y
        # "<col>_Posiciones_Invalidas") se elimino: el codigo M puro ya no
        # agrega columnas nuevas al resultado final, ni siquiera para
        # visualizar donde esta el error de un telefono.

    # -- 9) Email ---------------------------------------------------------------
    for col in cols_email:
        if col not in df.columns:
            continue
        nombre_col_id = re.sub(r'[^A-Za-z0-9]', '', col)

        # 9a) Faltantes: el email quedaba excluido del bloque 6b (texto) porque
        # necesita su propio orden (rellenar antes de normalizar/validar), pero
        # eso hacia que la accion configurada para "faltante" (ej. "Reemplazar
        # por un valor fijo") nunca se aplicara a esta columna: el bloque de
        # abajo solo miraba "email_invalido", no "faltante".
        if a_faltante == "valor_fijo":
            _paso_relleno_valor_fijo(cb, comentarios, f"SinFaltantesEmail_{nombre_col_id}", col, valores_fijos, "faltante")
        elif a_faltante == "reemplazar_moda":
            expr_relleno = f"List.Mode(List.RemoveNulls(Table.Column({{prev}}, {_m_str(col)})))"
            cb.agregar(f"SinFaltantesEmail_{nombre_col_id}",
                       "Table.ReplaceValue({prev}, null, " + expr_relleno +
                       f", Replacer.ReplaceValue, {{{_m_str(col)}}})")
        elif a_faltante == "marcar_solo":
            comentarios.append(
                f'  // AVISO: "faltante" quedo en "marcar_solo" para la columna "{col}", '
                f'pero el codigo M puro no agrega columnas nuevas (ni Revisar_Faltante_{col}); '
                f'no se genero ningun paso para esta columna.'
            )

        chequeo_email = (
            f"[{col}] = null or not Text.Contains([{col}], \"@\") "
            f"or Text.StartsWith([{col}], \"@\") or Text.EndsWith([{col}], \"@\") "
            f"or not Text.Contains(Text.AfterDelimiter([{col}], \"@\"), \".\") "
            f"or Text.EndsWith([{col}], \".\")"
        )
        if a_email == "valor_fijo":
            valor_fijo_email = _buscar_valor_fijo(valores_fijos, "email_invalido", col)
            reemplazo = _expr_valor_fijo_m(valor_fijo_email) if valor_fijo_email is not None else "null"
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
            comentarios.append(
                f'  // AVISO: "email_invalido" quedo en "marcar_solo" para la columna '
                f'"{col}", pero el codigo M puro no agrega columnas nuevas (ni '
                f'Revisar_Email_{col}); solo se normalizo el formato del correo.'
            )

    # -- 10) Estado/status -------------------------------------------------------
    lista_estado_m = "{" + ", ".join(_m_str(v.lower()) for v in valores_estado) + "}"
    for col in cols_estado:
        if col not in df.columns:
            continue
        nombre_col_id = re.sub(r'[^A-Za-z0-9]', '', col)
        chequeo_estado_invalido = (
            f"_ <> null and not List.Contains({lista_estado_m}, Text.Lower(Text.Trim(Text.From(_))))"
        )
        if a_estado == "valor_fijo":
            valor_fijo_estado = _buscar_valor_fijo(valores_fijos, "estado_invalido", col)
            reemplazo = _expr_valor_fijo_m(valor_fijo_estado) if valor_fijo_estado is not None else "null"
            cb.agregar(f"EstadoValidado_{nombre_col_id}",
                       "Table.TransformColumns({prev}, {{" + _m_str(col) +
                       f", each if {chequeo_estado_invalido} then {reemplazo} else _, type text}}}})")
        else:
            comentarios.append(
                f'  // AVISO: "estado_invalido" quedo en "{a_estado}" para la columna '
                f'"{col}", pero el codigo M puro no agrega columnas nuevas (ni '
                f'Revisar_Estado_{col}); no se genero ningun paso de marcado para esta columna. '
                f'Valores validos configurados: {", ".join(valores_estado)}.'
            )

    # -- 10b) Capitalizacion de nombres propios ----------------------------------
    # A diferencia de "texto_inconsistente" (que necesita una tabla horneada
    # porque depende de similitud/frecuencia entre valores), la correccion de
    # capitalizacion es una funcion pura del texto de cada celda: se genera
    # como una funcion M nativa (CapitalizarNombrePropio, ver arriba) que se
    # recalcula solita en cada refresh, sin quedar "congelada" con los
    # valores vistos al generar el codigo.
    if cols_capitalizacion:
        necesita_capitalizar_fn = True
        for col in cols_capitalizacion:
            if col not in df.columns:
                continue
            nombre_col_id = re.sub(r'[^A-Za-z0-9]', '', col)
            if a_capitalizacion == "usar_sugerido":
                cb.agregar(f"Capitalizado_{nombre_col_id}",
                           "Table.TransformColumns({prev}, {{" + _m_str(col) +
                           ", each CapitalizarNombrePropio(_), type text}})")
            elif a_capitalizacion == "eliminar_fila":
                cb.agregar(f"SinCapitalizacionInvalida_{nombre_col_id}",
                           "Table.SelectRows({prev}, each [" + col + "] = null or [" + col +
                           "] = CapitalizarNombrePropio([" + col + "]))")
            else:
                comentarios.append(
                    f'  // AVISO: "capitalizacion_incorrecta" quedo en "{a_capitalizacion}" '
                    f'para la columna "{col}", pero el codigo M puro no agrega columnas '
                    f'nuevas (ni Revisar_Capitalizacion_{col}); no se genero ningun paso '
                    f'para esta columna.'
                )
    else:
        necesita_capitalizar_fn = False

    funciones_extra = ""
    if necesita_recortar_fn:
        funciones_extra += _M_FUNCION_RECORTAR_TEXTO + "\n"
    if necesita_capitalizar_fn:
        funciones_extra += _M_FUNCION_CAPITALIZAR + "\n"
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

# -*- coding: utf-8 -*-
"""
flujos_guiados.py
=================
Los tres flujos nuevos (limpieza guiada de nulos, merge y diccionario de
datos) sin ninguna dependencia de Streamlit, para que los use cualquier
interfaz: CLI, API, main.py, escritorio y notebook.

Es una capa fina sobre limpieza_guiada.py, merge_tablas.py y
diccionario_datos.py: repite el mismo orden de pasos que la pagina de
Streamlit (paginas_guiadas.py), asi todas las interfaces dan el mismo
resultado.

    Limpieza guiada:  configurar_limpieza_guiada -> ejecutar_limpieza_guiada
                      -> guardar_limpieza_guiada
    Merge:            ejecutar_merge -> guardar_merge
    Diccionario:      generar_diccionario (+ generar_diccionario_tecnico y
                      generar_documento_alcance)

La limpieza guiada sigue los mismos pasos que la app web:
estandarizar -> fechas en un solo formato -> unir valores equivalentes
(M / male -> M) -> regla de nulos por columna -> cierre sin vacios (todos los
nulos validos con una misma palabra, «No indica» por defecto).

Cada guardado deja, ademas de la tabla y el script, los dos documentos del
diccionario: el tecnico (diccionario_datos.xlsx) y el documento de alcance
(Word, si esta instalado python-docx), mas el Excel basico de siempre.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from . import diccionario_datos as DD
from . import limpieza_guiada as LG
from . import merge_tablas as MT
from .loaders import leer_tabla_subida, listar_hojas, tabla_a_bytes

# Textos que cuentan como nulo ademas de la celda vacia: la misma lista base de la app.
TOKENS_EXTRA_DEFECTO: Tuple[str, ...] = tuple(t for t in LG.TOKENS_NULOS_BASE if t)


# =============================================================================
# Utilidades comunes
# =============================================================================

def nombre_base(nombre: str) -> str:
    """Nombre de archivo sin carpeta, extension ni simbolos (igual que la app web)."""
    base = os.path.splitext(os.path.basename(str(nombre)))[0]
    return re.sub(r"[^\w\-]+", "_", base).strip("_") or "tabla"


def leer_tabla_guiada(ruta: str, hoja: Optional[str] = None, como_texto: bool = True,
                      detectar_encabezado: bool = True) -> pd.DataFrame:
    """Lee un CSV o una hoja de Excel como lo hace la app web (todo como texto
    por defecto, para ver el archivo tal cual esta)."""
    return leer_tabla_subida(ruta, nombre=ruta, hoja=hoja, como_texto=como_texto,
                             detectar_encabezado=detectar_encabezado)


def hojas_de_archivo(ruta: str) -> List[str]:
    """Hojas de un libro Excel (lista vacia para un CSV)."""
    return listar_hojas(ruta)


def tokens_nulos(extra: Optional[Sequence[str]] = None) -> Tuple[str, ...]:
    """Textos que cuentan como nulo: la celda vacia mas los `extra` elegidos."""
    elegidos = TOKENS_EXTRA_DEFECTO if extra is None else tuple(extra)
    return ("",) + tuple(str(t).strip().lower() for t in elegidos)


def parsear_pares(textos: Optional[Sequence[str]], opcion: str) -> Dict[str, str]:
    """['a=1', 'b=2'] -> {'a': '1', 'b': '2'}. `opcion` solo sirve para el mensaje."""
    pares: Dict[str, str] = {}
    for texto in textos or []:
        clave, separador, valor = str(texto).partition("=")
        if not separador or not clave.strip():
            raise ValueError(f"{opcion} invalido: '{texto}' (use clave=valor)")
        pares[clave.strip()] = valor.strip()
    return pares


def _validar_columnas(columnas: Sequence[str], disponibles: Sequence[str], que: str) -> None:
    for columna in columnas:
        if columna not in disponibles:
            raise ValueError(f"{que}: la columna '{columna}' no existe. "
                             f"Columnas: {', '.join(map(str, disponibles))}")


# =============================================================================
# Limpieza guiada
# =============================================================================

@dataclass
class ResultadoLimpiezaGuiada:
    df: pd.DataFrame
    pasos: list
    reglas: List[Dict]
    auditoria: Dict
    script: str
    tokens: Tuple[str, ...]
    nombre_archivo: str
    hoja: Optional[str]
    palabra: str = LG.TEXTO_NO_INDICA
    avisos: List[str] = field(default_factory=list)  # lo que conviene revisar (fechas ambiguas, etc.)


def _traductor_de_nombres(df_original: pd.DataFrame, df_renombrado: pd.DataFrame):
    """Acepta el nombre original o el nuevo (snake_case) de una columna."""
    mapa = dict(zip(df_original.columns, df_renombrado.columns))
    disponibles = list(df_renombrado.columns)

    def traducir(nombres: Sequence[str], que: str) -> List[str]:
        resultado = []
        for nombre in nombres:
            if nombre in disponibles:
                resultado.append(nombre)
            elif nombre in mapa:
                resultado.append(mapa[nombre])
            else:
                _validar_columnas([nombre], disponibles, que)
        return resultado

    return traducir


def configurar_limpieza_guiada(
        df: pd.DataFrame, tokens_extra: Optional[Sequence[str]] = None,
        nombres_snake: bool = True, vacios_a_nan: bool = True,
        estandarizar_texto: bool = True, columnas_texto: Optional[Sequence[str]] = None,
        minusculas: bool = True, espacios: bool = True, comillas: bool = True,
        numericas: Optional[Sequence[str]] = None,
        latitud: Optional[str] = None, longitud: Optional[str] = None,
        rango_latitud: Optional[Tuple[float, float]] = None,
        rango_longitud: Optional[Tuple[float, float]] = None,
        fechas: Optional[Sequence[str]] = None, formato_fecha: str = "%Y-%m-%d",
        dia_primero: bool = True) -> Dict:
    """Arma el `config` de pasos globales con las mismas sugerencias de la app web.

    None = elegir automaticamente (columnas de texto, numericas, fechas y
    coordenadas por nombre y contenido). Lista vacia o cadena vacia = ninguna.
    Los nombres pueden ser los originales o los de snake_case.

    tokens_extra=None: cuenta como nulo la lista base («nan», «none», «null»,
    «n/a») mas los textos tipo «sin dato» que aparezcan en la tabla (unknown, -,
    not available...), igual que la app. Con una lista propia se usa solo esa.

    fechas: columnas que se dejan en un solo `formato_fecha` (AAAA-MM-DD por
    defecto). `dia_primero` decide como leer 05/06/2025 cuando no hay pistas.
    """
    if "%" not in formato_fecha:
        raise ValueError(f"formato_fecha invalido: '{formato_fecha}' (ej. %Y-%m-%d)")
    if tokens_extra is None:
        base = tokens_nulos(None)
        tokens = base + tuple(LG.detectar_textos_tipo_nulo(df, base)["texto"])
    else:
        tokens = tokens_nulos(tokens_extra)
    df_ren = LG.paso_nombres_columnas(df)[0] if nombres_snake else df
    traducir = _traductor_de_nombres(df, df_ren)

    if columnas_texto is None:
        cols_texto = LG.columnas_texto_sugeridas(df_ren, tokens)
    else:
        cols_texto = traducir(columnas_texto, "columnas de texto")

    cols_fecha_nombre = list(LG.columnas_fecha_por_nombre(df_ren))
    if numericas is None:
        cols_num = [c for c in df_ren.columns
                    if not pd.api.types.is_numeric_dtype(df_ren[c])
                    and LG.rol_columna(df_ren, c, cols_fecha_nombre, tokens) == "numerica"]
    else:
        cols_num = traducir(numericas, "columnas numericas")

    if fechas is None:
        cols_fecha = [c for c in cols_fecha_nombre if c in df_ren.columns]
    else:
        cols_fecha = traducir(fechas, "columnas de fecha")

    if latitud is None:
        latitud = next((c for c in df_ren.columns if LG.tipo_coordenada(c) == "latitud"), None)
    elif latitud:
        latitud = traducir([latitud], "latitud")[0]
    if longitud is None:
        longitud = next((c for c in df_ren.columns if LG.tipo_coordenada(c) == "longitud"), None)
    elif longitud:
        longitud = traducir([longitud], "longitud")[0]

    texto = {"columnas": cols_texto, "minusculas": minusculas, "espacios": espacios,
             "comillas": comillas} if estandarizar_texto else None
    return {
        "tokens": tokens, "nombres_snake": nombres_snake, "vacios_a_nan": vacios_a_nan,
        "texto": texto, "numericas": cols_num,
        "latitud": latitud or None, "longitud": longitud or None,
        "rango_latitud": rango_latitud, "rango_longitud": rango_longitud,
        "fechas": {"columnas": cols_fecha, "formato": formato_fecha,
                   "dia_primero": dia_primero} if cols_fecha else None,
    }


def parsear_ajustes_reglas(textos: Optional[Sequence[str]]) -> Dict[str, Dict[str, str]]:
    """['col=mediana', 'otra=valor_fijo:Sin dato', 'monto=mediana_por_grupo:zona',
    'nota=no_indica:Sin nota'] -> {columna: {regla, valor, grupo}}. Lo que va despues
    de «:» es el valor (valor_fijo, no_indica; en no_indica es opcional: sin valor se
    usa la palabra de los nulos validos) o la columna de grupo (mediana_por_grupo)."""
    ajustes: Dict[str, Dict[str, str]] = {}
    for columna, texto in parsear_pares(textos, "regla").items():
        regla, _, parametro = texto.partition(":")
        regla, parametro = regla.strip(), parametro.strip()
        if regla not in LG.REGLAS_NULOS:
            raise ValueError(f"Regla invalida para '{columna}': '{regla}'. "
                             f"Validas: {', '.join(LG.REGLAS_NULOS)}")
        if parametro and regla not in ("valor_fijo", "no_indica", "mediana_por_grupo"):
            raise ValueError(f"La regla '{regla}' no lleva parametro (columna '{columna}').")
        if regla == "mediana_por_grupo" and not parametro:
            raise ValueError(f"mediana_por_grupo necesita la columna de grupo: "
                             f"{columna}=mediana_por_grupo:columna_grupo")
        ajustes[columna] = {"regla": regla,
                            "valor": parametro if regla in ("valor_fijo", "no_indica") else "",
                            "grupo": parametro if regla == "mediana_por_grupo" else ""}
    return ajustes


def reglas_sugeridas(df_base: pd.DataFrame, tokens: Sequence[str],
                     palabra: str = LG.TEXTO_NO_INDICA) -> List[Dict]:
    """La tabla de reglas por columna que propone la app, como lista de dicts.
    En las reglas «no_indica» el valor sugerido es `palabra`."""
    return LG.tabla_de_reglas(df_base, tokens, palabra).fillna("").to_dict("records")


def ejecutar_limpieza_guiada(df: pd.DataFrame, config: Dict,
                             ajustes: Optional[Dict[str, Dict[str, str]]] = None,
                             nombre_archivo: str = "tabla.csv",
                             hoja: Optional[str] = None,
                             palabra: str = LG.TEXTO_NO_INDICA,
                             unir_equivalentes: bool = True,
                             equivalencias: Optional[List[Dict]] = None,
                             asegurar_sin_vacios: bool = True,
                             numeros_cierre: str = "mediana",
                             incluir_fechas_cierre: bool = True) -> ResultadoLimpiezaGuiada:
    """Pasos globales + union de valores equivalentes + una regla de nulos por
    columna (la sugerida, salvo que `ajustes` indique otra) + cierre sin vacios +
    auditoria final + script de pandas. Es el mismo recorrido de la app web.

    - palabra: texto de los nulos validos («No indica» por defecto); lo usan las
      reglas «no_indica» sin valor propio y el cierre sin vacios.
    - unir_equivalentes: M / male / masculino -> M, si / yes -> Sí, etc. Con
      `equivalencias` ([{columna, valor, unificar_a}]) se usa esa lista en lugar
      de la sugerida (unificar_a vacio = no cambiar).
    - asegurar_sin_vacios: tras las reglas, rellena lo que sobre: texto y fechas con
      `palabra`; numeros con la mediana, 0 (numeros_cierre='cero') o la palabra
      (numeros_cierre='palabra', la columna pasa a texto).
    """
    if numeros_cierre not in ("mediana", "cero", "palabra"):
        raise ValueError("numeros_cierre debe ser 'mediana', 'cero' o 'palabra'")
    palabra = str(palabra).strip() or LG.TEXTO_NO_INDICA
    tokens = tuple(config.get("tokens", LG.TOKENS_NULOS_BASE))
    df_pre, pasos_globales = LG.ejecutar_pasos_globales(df, config)
    pasos_globales = list(pasos_globales)

    df_base = df_pre
    if unir_equivalentes:
        if equivalencias is None:
            tabla_eq = LG.sugerir_equivalencias(df_pre, None, palabra, tokens).to_dict("records")
        else:
            tabla_eq = list(equivalencias)
            _validar_columnas([e["columna"] for e in tabla_eq], list(df_pre.columns), "Equivalencias")
        df_base, paso_eq = LG.paso_unificar(df_pre, tabla_eq)
        if paso_eq:
            pasos_globales.append(paso_eq)

    reglas = reglas_sugeridas(df_base, tokens, palabra)
    traducir = _traductor_de_nombres(df, df_base)
    for columna, cambio in (ajustes or {}).items():
        destino = traducir([columna], "regla")[0]
        if cambio.get("grupo"):
            cambio = dict(cambio, grupo=traducir([cambio["grupo"]], "grupo")[0])
        for regla in reglas:
            if regla["columna"] == destino:
                regla.update(cambio)
    for regla in reglas:  # «no_indica» sin valor = la palabra elegida (el diccionario la muestra)
        if regla["regla"] == "no_indica" and not str(regla.get("valor", "")).strip():
            regla["valor"] = palabra

    df_final, pasos_reglas = LG.ejecutar_reglas(df_base, reglas, tokens)
    if asegurar_sin_vacios:
        df_final, paso_cierre = LG.paso_asegurar_sin_vacios(
            df_final, tokens, numeros_cierre, incluir_fechas_cierre, palabra)
        pasos_reglas.append(paso_cierre)
        por_columna = {r["columna"]: r for r in reglas}
        for r in paso_cierre.reglas or []:  # el diccionario debe decir lo que se hizo
            por_columna[r["columna"]] = {**por_columna.get(r["columna"], {}), **r}
        reglas = list(por_columna.values())

    pasos = pasos_globales + pasos_reglas
    return ResultadoLimpiezaGuiada(
        df=df_final, pasos=pasos, reglas=reglas,
        auditoria=LG.auditoria_final(df, df_final, tokens),
        script=LG.generar_script_limpieza(pasos, tokens, nombre_archivo, hoja),
        tokens=tokens, nombre_archivo=nombre_archivo, hoja=hoja, palabra=palabra,
        avisos=[f"{p.titulo}: {p.advertencia}" for p in pasos if getattr(p, "advertencia", "")],
    )


def auditoria_a_dict(auditoria: Dict) -> Dict:
    """La auditoria final lista para JSON o para mostrar (sin DataFrames)."""
    salida = {k: v for k, v in auditoria.items() if k not in ("nulos_restantes", "tipos")}
    salida["nulos_restantes"] = auditoria["nulos_restantes"].reset_index().to_dict("records")
    salida["tipos"] = auditoria["tipos"]["tipo_pandas"].to_dict()
    return salida


def resumen_pasos(pasos: list) -> List[Dict[str, str]]:
    """Titulo y detalle de cada paso; `advertencia` trae lo que conviene revisar."""
    return [{"titulo": p.titulo, "detalle": p.detalle, "advertencia": getattr(p, "advertencia", "")}
            for p in pasos]


def _escribir(ruta: str, contenido) -> str:
    modo = "wb" if isinstance(contenido, bytes) else "w"
    with open(ruta, modo, **({} if modo == "wb" else {"encoding": "utf-8"})) as archivo:
        archivo.write(contenido)
    return ruta


def _guardar_tabla(df: pd.DataFrame, carpeta: str, base: str, formato: str) -> Dict[str, str]:
    if formato not in ("csv", "xlsx", "ambos"):
        raise ValueError("formato debe ser 'csv', 'xlsx' o 'ambos'")
    rutas = {}
    for ext in (("csv", "xlsx") if formato == "ambos" else (formato,)):
        rutas[ext] = _escribir(os.path.join(carpeta, f"{base}.{ext}"), tabla_a_bytes(df, ext))
    return rutas


def _guardar_diccionarios(carpeta: str, nombre_excel: str, base_documento: str, df: pd.DataFrame,
                          nombre: str, reglas: Optional[List[Dict]] = None,
                          origenes: Optional[Dict[str, str]] = None,
                          fuentes: Optional[List[str]] = None,
                          eliminadas: Optional[List[str]] = None,
                          llaves: Optional[Sequence[str]] = None,
                          cruces: Optional[List[Dict]] = None,
                          textos: Optional[Dict[str, str]] = None,
                          etapas: Optional[List[tuple]] = None,
                          controles_extra: Optional[List[tuple]] = None,
                          pendientes_extra: Optional[List[tuple]] = None) -> Dict[str, str]:
    """Escribe los tres archivos del diccionario: el Excel basico, el diccionario
    tecnico (diccionario_datos.xlsx, el que enlaza el documento de alcance) y el
    documento de alcance en Word (se omite si falta python-docx)."""
    diccionario, resumen, excel = generar_diccionario(df, nombre, reglas, origenes, fuentes,
                                                      eliminadas, llaves)
    rutas = {"diccionario": _escribir(os.path.join(carpeta, nombre_excel), excel),
             "diccionario_tecnico": _escribir(
                 os.path.join(carpeta, DD.NOMBRE_TECNICO),
                 generar_diccionario_tecnico(df, diccionario, resumen))}
    try:
        documento = generar_documento_alcance(
            df, diccionario, nombre, fuentes, cruces, eliminadas, textos, etapas,
            controles_extra, pendientes_extra)
    except ImportError:  # sin python-docx no hay documento de alcance
        return rutas
    rutas["documento_alcance"] = _escribir(
        os.path.join(carpeta, f"documento_alcance_{base_documento}.docx"), documento)
    return rutas


def guardar_limpieza_guiada(resultado: ResultadoLimpiezaGuiada, carpeta: str,
                            formato: str = "csv",
                            textos_alcance: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Guarda tabla limpia, script de pandas y los diccionarios en `carpeta`
    (Excel basico, diccionario tecnico y documento de alcance en Word).
    `textos_alcance` rellena el documento de alcance (proyecto, autor, proposito,
    conclusion, alcance, unidad_analisis, preparacion, recomendacion,
    fuentes_documentales). Devuelve {tipo: ruta}."""
    os.makedirs(carpeta, exist_ok=True)
    base = f"{nombre_base(resultado.nombre_archivo)}_limpio"
    rutas = _guardar_tabla(resultado.df, carpeta, base, formato)
    rutas["script"] = _escribir(os.path.join(carpeta, f"{base}_script.py"), resultado.script)
    eliminadas = [r["columna"] for r in resultado.reglas
                  if r["regla"] == "eliminar_columna" and r.get("nulos")]
    a = resultado.auditoria
    etapas = [("Tabla original", f"{DD._n(a['filas_antes'])} filas y {a['columnas_antes']} columnas"),
              ("Tabla limpia final", f"{DD._n(a['filas_despues'])} filas y {a['columnas_despues']} columnas"),
              ("Celdas nulas", f"{DD._n(a['nulos_antes'])} antes y {DD._n(a['nulos_despues'])} después")]
    rutas.update(_guardar_diccionarios(
        carpeta, f"diccionario_{base}.xlsx", base, resultado.df, base, resultado.reglas,
        fuentes=[resultado.nombre_archivo], eliminadas=eliminadas, textos=textos_alcance,
        etapas=etapas))
    return rutas


# =============================================================================
# Merge
# =============================================================================

@dataclass
class ResultadoMerge:
    df: pd.DataFrame
    auditoria: Dict
    diagnostico: Dict
    script: str
    params: Dict
    rellenos: List[Tuple[str, str]]
    columnas_a: List[str]
    columnas_b: List[str] = field(default_factory=list)


def llaves_sugeridas(df_a: pd.DataFrame, df_b: pd.DataFrame) -> Tuple[List[str], List[str]]:
    """La mejor pareja de columnas llave segun el nombre y los valores."""
    sugerencias = MT.sugerir_llaves(df_a, df_b)
    if not len(sugerencias):
        return [], []
    return [sugerencias.iloc[0]["columna_a"]], [sugerencias.iloc[0]["columna_b"]]


def resolver_llaves(df_a: pd.DataFrame, df_b: pd.DataFrame, claves_a: Optional[Sequence[str]] = None,
                    claves_b: Optional[Sequence[str]] = None) -> Tuple[List[str], List[str]]:
    """Llaves a usar: las indicadas, o la pareja sugerida si no se indico ninguna.
    Valida que existan y que haya la misma cantidad en A y en B."""
    claves_a, claves_b = list(claves_a or []), list(claves_b or [])
    if not claves_a and not claves_b:
        claves_a, claves_b = llaves_sugeridas(df_a, df_b)
        if not claves_a:
            raise ValueError("No se encontraron llaves sugeridas: indique las columnas llave de A y de B.")
    if not claves_a or len(claves_a) != len(claves_b):
        raise ValueError("Indique la misma cantidad de columnas llave en A y en B.")
    _validar_columnas(claves_a, list(df_a.columns), "Llave en A")
    _validar_columnas(claves_b, list(df_b.columns), "Llave en B")
    return claves_a, claves_b


def diagnosticar_llaves(df_a: pd.DataFrame, df_b: pd.DataFrame,
                        claves_a: Optional[Sequence[str]] = None,
                        claves_b: Optional[Sequence[str]] = None, modo: str = "texto",
                        ancho: int = 0) -> Tuple[List[str], List[str], Dict]:
    """(llaves de A, llaves de B, diagnostico): cuantas filas de A encuentran
    pareja en B, llaves repetidas o vacias y la relacion entre las tablas."""
    if modo not in MT.MODOS_LLAVE:
        raise ValueError(f"Modo de llave invalido: '{modo}'. Validos: {', '.join(MT.MODOS_LLAVE)}")
    claves_a, claves_b = resolver_llaves(df_a, df_b, claves_a, claves_b)
    return claves_a, claves_b, MT.diagnosticar_llaves(df_a, df_b, claves_a, claves_b, modo, ancho)


def ejecutar_merge(df_a: pd.DataFrame, df_b: pd.DataFrame,
                   nombre_a: str = "tabla_a.csv", nombre_b: str = "tabla_b.csv",
                   claves_a: Optional[Sequence[str]] = None,
                   claves_b: Optional[Sequence[str]] = None,
                   how: str = "left", validate: str = "auto", modo: str = "texto",
                   ancho: int = 0, prefijo_b: str = "", solo_repetidas: bool = True,
                   colapsar_b: bool = True, agregaciones: Optional[Dict[str, str]] = None,
                   sufijo_b: str = "_b", conservar_indicador: bool = False,
                   rellenos: Optional[Sequence[Tuple[str, str]]] = None,
                   hoja_a: Optional[str] = None, hoja_b: Optional[str] = None) -> ResultadoMerge:
    """Une A con B con los mismos pasos que la app web.

    - claves vacias: usa la pareja sugerida.
    - validate='auto': many_to_one si B no repite llaves (o ya se colapso);
      '' = sin validar; o cualquiera de merge_tablas.VALIDACIONES.
    - colapsar_b: si B repite llaves, deja una fila por llave ('first' por
      columna, salvo lo indicado en `agregaciones`).
    - rellenos: [(destino, respaldo)], el «Plan B» de la app.
    """
    if how not in MT.TIPOS_UNION:
        raise ValueError(f"Tipo de union invalido: '{how}'. Validos: {', '.join(MT.TIPOS_UNION)}")
    if modo not in MT.MODOS_LLAVE:
        raise ValueError(f"Modo de llave invalido: '{modo}'. Validos: {', '.join(MT.MODOS_LLAVE)}")
    if validate not in ("auto", *MT.VALIDACIONES):
        raise ValueError(f"Validacion invalida: '{validate}'. Validas: auto, "
                         f"{', '.join(k or 'vacio' for k in MT.VALIDACIONES)}")

    claves_a, claves_b, diagnostico = diagnosticar_llaves(df_a, df_b, claves_a, claves_b, modo, ancho)

    otras_b = [c for c in df_b.columns if c not in claves_b]
    if agregaciones:
        _validar_columnas(list(agregaciones), otras_b, "Agregacion en B")
        for columna, funcion in agregaciones.items():
            if funcion not in MT.AGREGACIONES:
                raise ValueError(f"Funcion invalida para '{columna}': '{funcion}'. "
                                 f"Validas: {', '.join(MT.AGREGACIONES)}")
    elif colapsar_b and diagnostico["repetidas_b"] > 0:
        agregaciones = {c: "first" for c in otras_b}
    else:
        agregaciones = None

    if validate == "auto":
        validate = "many_to_one" if (agregaciones is not None or diagnostico["repetidas_b"] == 0) else ""

    parametros = dict(
        nombre_a=nombre_a, nombre_b=nombre_b, claves_a=claves_a, claves_b=claves_b,
        hoja_a=hoja_a, hoja_b=hoja_b, how=how, validate=validate or None, modo=modo,
        ancho=ancho, prefijo_b=prefijo_b, solo_repetidas=solo_repetidas,
        agregaciones=agregaciones, sufijos=("", sufijo_b), conservar_indicador=conservar_indicador)
    df_res, auditoria = MT.hacer_merge(
        df_a, df_b, claves_a, claves_b, how=how, validate=validate or None, modo=modo,
        ancho=ancho, prefijo_b=prefijo_b, solo_repetidas=solo_repetidas,
        agregaciones=agregaciones, sufijos=("", sufijo_b), conservar_indicador=conservar_indicador)

    rellenos_aplicados: List[Tuple[str, str]] = []
    for destino, respaldo in rellenos or []:
        _validar_columnas([destino, respaldo], list(df_res.columns), "Plan B")
        if destino == respaldo:
            raise ValueError("Plan B: elija dos columnas distintas.")
        df_res = MT.rellenar_con_respaldo(df_res, destino, respaldo)
        rellenos_aplicados.append((destino, respaldo))

    return ResultadoMerge(
        df=df_res, auditoria=auditoria, diagnostico=diagnostico,
        script=MT.generar_script_merge(rellenos=rellenos_aplicados, **parametros),
        params=parametros, rellenos=rellenos_aplicados, columnas_a=list(df_a.columns),
        columnas_b=list(df_b.columns))


def diagnostico_a_dict(diagnostico: Dict) -> Dict:
    """Diagnostico de llaves listo para JSON (los ejemplos sin pareja como lista)."""
    salida = dict(diagnostico)
    salida["ejemplos_sin_pareja"] = diagnostico["ejemplos_sin_pareja"].to_dict("records")
    return salida


def origenes_de_columnas(resultado: ResultadoMerge) -> Dict[str, str]:
    """De que tabla viene cada columna del resultado (para el diccionario)."""
    return {c: ("Auditoría del merge" if c == "_merge" else
                "Tabla A" if c in resultado.columnas_a else "Tabla B")
            for c in resultado.df.columns}


def datos_alcance_merge(resultado: ResultadoMerge) -> Dict:
    """Lo que el documento de alcance necesita de un merge, igual que la app web:
    llaves de union, cruces hechos, etapas, controles y advertencias del cruce."""
    prm, aud = resultado.params, resultado.auditoria
    claves_a, claves_b = list(prm["claves_a"]), list(prm["claves_b"])
    llaves = claves_a + [c for c in claves_b if c not in claves_a and c in resultado.df.columns]
    nombre_a, nombre_b = prm["nombre_a"], prm["nombre_b"]
    cruces = [{"tabla_a": nombre_a, "col_a": ca, "tabla_b": nombre_b, "col_b": cb, "cruce": prm["how"]}
              for ca, cb in zip(claves_a, claves_b)]
    etapas = [(f"Tabla A: {nombre_a}", f"{DD._n(aud['filas_a'])} filas y {len(resultado.columnas_a)} columnas"),
              (f"Tabla B: {nombre_b}", f"{DD._n(aud['filas_b'])} filas y {len(resultado.columnas_b)} columnas"),
              ("Tabla maestra final",
               f"{DD._n(len(resultado.df))} filas y {resultado.df.shape[1]} columnas")]
    controles = [("Filas de A con pareja en B", f"{aud['pct_filas_con_pareja']} %",
                  "Porcentaje de filas de A que encontraron su fila en B; no demuestra que "
                  "cada cruce sea correcto.")]
    return {"llaves": llaves, "cruces": cruces, "etapas": etapas, "controles_extra": controles,
            "advertencias": list(aud.get("advertencias", []))}


def guardar_merge(resultado: ResultadoMerge, carpeta: str, base: str = "resultado_merge",
                  formato: str = "csv",
                  textos_alcance: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Guarda la tabla unida, el script de pandas y los diccionarios (Excel basico,
    diccionario tecnico y documento de alcance en Word, con las llaves de union)."""
    os.makedirs(carpeta, exist_ok=True)
    rutas = _guardar_tabla(resultado.df, carpeta, base, formato)
    rutas["script"] = _escribir(os.path.join(carpeta, f"{base}_script.py"), resultado.script)
    fuentes = [resultado.params["nombre_a"], resultado.params["nombre_b"]]
    alcance = datos_alcance_merge(resultado)
    rutas.update(_guardar_diccionarios(
        carpeta, "diccionario_tabla_maestra.xlsx", "tabla_maestra", resultado.df, "tabla_maestra",
        origenes=origenes_de_columnas(resultado), fuentes=fuentes, llaves=alcance["llaves"],
        cruces=alcance["cruces"], textos=textos_alcance, etapas=alcance["etapas"],
        controles_extra=alcance["controles_extra"],
        pendientes_extra=[("Advertencia del cruce", str(a)) for a in alcance["advertencias"]]))
    return rutas


# =============================================================================
# Diccionario de datos
# =============================================================================

def generar_diccionario(df: pd.DataFrame, nombre: str, reglas: Optional[List[Dict]] = None,
                        origenes: Optional[Dict[str, str]] = None,
                        fuentes: Optional[List[str]] = None,
                        eliminadas: Optional[List[str]] = None,
                        llaves: Optional[Sequence[str]] = None
                        ) -> Tuple[pd.DataFrame, pd.DataFrame, bytes]:
    """(diccionario, resumen, excel). Tipo, completitud y rangos salen de los
    datos; la descripcion se redacta sola (nombre, tipo, origen y tratamiento) y se puede
    reescribir; la justificacion y la clasificacion ejecutiva (KPI, variable
    transformada o llave) quedan para completarlas (las celdas vacias salen
    resaltadas en el Excel). `llaves`: columnas usadas para unir tablas;
    sin ellas se toman como llaves las de rol identificador."""
    diccionario = DD.construir_diccionario(df, reglas, origenes, llaves=llaves)
    resumen = DD.resumen_tabla(df, nombre, diccionario, fuentes, eliminadas)
    return diccionario, resumen, DD.diccionario_a_excel(diccionario, resumen)


def generar_diccionario_tecnico(df: pd.DataFrame, diccionario: pd.DataFrame,
                                resumen: pd.DataFrame) -> bytes:
    """Excel del diccionario tecnico («diccionario_datos.xlsx»): una fila por campo con
    tipo nativo, tipo sugerido y limites logicos, mas resumen y leyenda. Se puede
    importar a Power BI o a un catalogo de datos."""
    return DD.diccionario_tecnico_excel(df, diccionario, resumen)


def generar_documento_alcance(df: pd.DataFrame, diccionario: pd.DataFrame, nombre: str,
                              fuentes: Optional[List[str]] = None,
                              cruces: Optional[List[Dict]] = None,
                              eliminadas: Optional[List[str]] = None,
                              textos: Optional[Dict[str, str]] = None,
                              etapas: Optional[List[tuple]] = None,
                              controles_extra: Optional[List[tuple]] = None,
                              pendientes_extra: Optional[List[tuple]] = None) -> bytes:
    """Documento de alcance y diccionario ejecutivo en Word (bytes). Lo calculado
    sale solo; `textos` trae lo que solo sabe la persona (proyecto, autor, proposito,
    conclusion, alcance, unidad_analisis, preparacion, recomendacion,
    fuentes_documentales). Lanza ImportError si falta python-docx."""
    return DD.documento_alcance_docx(
        df, diccionario, nombre, fuentes, cruces, eliminadas, textos=textos, etapas=etapas,
        controles_extra=controles_extra, pendientes_extra=pendientes_extra)

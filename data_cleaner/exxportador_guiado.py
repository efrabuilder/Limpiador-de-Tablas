# -*- coding: utf-8 -*-
"""
exportador_guiado.py
====================
La limpieza guiada para Power BI, SIN cambiar el archivo original. Tres formas de repetir en Power Query
exactamente los pasos que se hicieron en la app:

    script_powerbi_python(res)  -> script Python para «Transformar > Ejecutar script de Python»
                                   (la tabla llega en `dataset` y el resultado sale en `dataset_limpio`)
    m_con_python(res)           -> consulta M que ejecuta ese mismo script con Python.Execute
    m_puro(res)                 -> consulta M 100% nativa, sin Python ni este paquete

`res` es el ResultadoLimpiezaGuiada (flujos_guiados.ejecutar_limpieza_guiada).

Python y «M con Python» comparten el mismo codigo, el de la limpieza guiada (script de pandas), asi que dan
el mismo resultado que la app. «M puro» traduce cada paso a funciones nativas de Power Query; no necesita
Python configurado en Power BI, pero M no tiene expresiones regulares ni la misma libreria numerica, asi que
algunas heuristicas se reescriben de forma equivalente (se listan en el encabezado del archivo generado).
Conviene comparar su resultado con la tabla limpia de la app la primera vez.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

import pandas as pd

from . import limpieza_guiada as LG
from .exportador_m import _ConstructorM, _m_ident

# =============================================================================
# Utilidades de texto M
# =============================================================================

_FORMATO_M = (("%Y", "yyyy"), ("%m", "MM"), ("%d", "dd"), ("%H", "HH"), ("%M", "mm"), ("%S", "ss"))


def _m_txt(valor) -> str:
    """Literal de texto M: comillas dobles duplicadas y saltos de linea, tabulaciones y «#(» escapados."""
    t = str(valor).replace('"', '""').replace("#(", "#(#)(")
    return '"' + t.replace("\r", "#(cr)").replace("\n", "#(lf)").replace("\t", "#(tab)") + '"'


def _m_num(valor) -> str:
    """Literal numerico M (sin notacion cientifica)."""
    numero = float(valor)
    return str(int(numero)) if numero.is_integer() else repr(numero)


def _m_valor(valor) -> str:
    """Literal M de un numero o un texto de la limpieza (los booleanos de Python no se esperan aqui)."""
    return _m_num(valor) if isinstance(valor, (int, float)) and not isinstance(valor, bool) else _m_txt(valor)


def _m_lista(items) -> str:
    return "{" + ", ".join(_m_txt(i) for i in items) + "}"


def _fmt_m(formato_python: str) -> str:
    """'%Y-%m-%d %H:%M:%S' -> 'yyyy-MM-dd HH:mm:ss' (formato de fecha de M)."""
    for origen, destino in _FORMATO_M:
        formato_python = formato_python.replace(origen, destino)
    return formato_python


def _campo(columna: str) -> str:
    """Lee una columna de la fila actual de un `each`: Record.Field(_, "columna") (vale para cualquier nombre)."""
    return f"Record.Field(_, {_m_txt(columna)})"


# =============================================================================
# Biblioteca de funciones M (cada una equivale a su funcion de limpieza_guiada.py)
# =============================================================================

_M_FUNCIONES: Dict[str, Tuple[Tuple[str, ...], str]] = {
    "fnATexto": ((), '''    fnATexto = (v as any) as nullable text =>
        if v = null then null
        else if v is text then v
        else if v is datetime then DateTime.ToText(v, "yyyy-MM-dd HH:mm:ss", "en-US")
        else if v is date then Date.ToText(v, "yyyy-MM-dd", "en-US")
        else if v is datetimezone then DateTimeZone.ToText(v, "yyyy-MM-dd HH:mm:ss", "en-US")
        else Text.From(v, "en-US"),'''),
    "fnEsNulo": ((), '''    fnEsNulo = (v as any) as logical =>
        v = null or List.Contains(TokensNulos, Text.Lower(Text.Trim(Text.From(v, "en-US")))),'''),
    "fnReemplazarColumna": ((), '''    fnReemplazarColumna = (t as table, col as text, lista as list) as table =>
        let
            nombres = Table.ColumnNames(t),
            columnas = Table.ToColumns(t),
            pos = List.PositionOf(nombres, col)
        in
            Table.FromColumns(List.ReplaceRange(columnas, pos, 1, {lista}), nombres),'''),
    "fnValores": (("fnEsNulo",), '''    fnValores = (t as table, col as text) as list =>
        List.Transform(Table.Column(t, col), each if fnEsNulo(_) then null else _),'''),
    "fnLimpiarTexto": ((), '''    fnLimpiarTexto = (t as nullable text, minusculas as logical, espacios as logical, comillas as logical) as nullable text =>
        if t = null then null
        else
            let
                a = Text.Trim(t),
                b = if comillas then Text.Trim(Text.Trim(a, """")) else a,
                c = if minusculas then Text.Lower(b) else b,
                d = if espacios then Text.Combine(List.Select(Text.SplitAny(c, " #(tab)#(lf)#(cr)"), each _ <> ""), " ") else c
            in
                d,'''),
    "fnANumero": ((), '''    fnANumero = (v as any) as nullable number =>
        if v = null then null
        else if v is number then v
        else if not (v is text) then null
        else
            let
                t1 = Text.Remove(Text.Trim(v), {"$", "€", "£", "₡", "¥", " ", "#(tab)", "#(lf)", "#(cr)", "#(00A0)"}),
                t2 = Text.Replace(Text.Replace(Text.Replace(Text.Lower(t1), "crc", ""), "usd", ""), "eur", ""),
                cuerpo = if Text.StartsWith(t2, "-") then Text.Middle(t2, 1) else t2,
                valido = cuerpo <> "" and Text.Select(cuerpo, {"0".."9", ".", ","}) = cuerpo,
                iComa = Text.PositionOf(t2, ",", Occurrence.Last),
                iPunto = Text.PositionOf(t2, ".", Occurrence.Last),
                partes = Text.Split(t2, ","),
                r =
                    if iComa >= 0 and iPunto >= 0 then
                        (if iComa > iPunto then Text.Replace(Text.Replace(t2, ".", ""), ",", ".") else Text.Replace(t2, ",", ""))
                    else if iComa >= 0 then
                        (if List.AllTrue(List.Transform(List.Skip(partes, 1), each Text.Length(_) = 3)) then Text.Replace(t2, ",", "") else Text.Replace(t2, ",", "."))
                    else t2
            in
                if valido then (try Number.FromText(r, "en-US") otherwise null) else null,'''),
    "fnNum": (("fnANumero",), '''    fnNum = (v as any) as nullable number =>
        if v is number then v else fnANumero(v),'''),
    "fnCoord": ((), '''    fnCoord = (v as any, minimo as number, maximo as number) as nullable number =>
        if v = null then null
        else
            let
                n = try Number.FromText(Text.Replace(Text.Trim(Text.From(v, "en-US")), ",", "."), "en-US") otherwise null,
                candidatos = if n = null then {} else List.Select(List.Transform({0..12}, each n / Number.Power(10, _)), each _ >= minimo and _ <= maximo)
            in
                if List.Count(candidatos) = 0 then null else candidatos{0},'''),
    "fnSoloDigitos": ((), '''    fnSoloDigitos = (t as text) as logical =>
        t <> "" and Text.Select(t, {"0".."9"}) = t,'''),
    "fnPartesFecha": (("fnSoloDigitos",), '''    fnPartesFecha = (s as nullable text) as nullable record =>
        if s = null then null
        else
            let
                x = Text.Trim(s),
                toks = Text.SplitAny(x, "/.- "),
                ok = List.Count(toks) >= 3 and Text.Length(toks{0}) <= 2 and fnSoloDigitos(toks{0}) and Text.Length(toks{1}) <= 2 and fnSoloDigitos(toks{1}) and Text.Length(toks{2}) = 4 and fnSoloDigitos(toks{2}),
                sep = if ok then Text.Middle(x, Text.Length(toks{0}), 1) else null,
                largo = if ok then Text.Length(toks{0}) + 1 + Text.Length(toks{1}) + 1 + 4 else 0
            in
                if not ok or not List.Contains({"/", ".", "-"}, sep) then null
                else [a = Number.From(toks{0}), b = Number.From(toks{1}), y = Number.From(toks{2}), sep = sep, resto = Text.Trim(Text.Middle(x, largo))],'''),
    "fnUnificarFechas": (("fnPartesFecha",), '''    fnUnificarFechas = (valoresEntrada as list, formato as text, diaPrimero as logical) as list =>
        let
            valores = List.Buffer(valoresEntrada),
            partes = List.Buffer(List.Transform(valores, each fnPartesFecha(_))),
            conPartes = List.Buffer(List.Select(partes, each _ <> null)),
            separadores = {"/", ".", "-"},
            // Se decide UNA vez por separador (no por fila): si el primer numero pasa de 12 es el dia, si el segundo pasa de 12 es el mes
            usarDiaPorSep = List.Transform(separadores, (s) =>
                let
                    delSep = List.Select(conPartes, each [sep] = s),
                    hd = List.AnyTrue(List.Transform(delSep, each [a] > 12)),
                    hm = List.AnyTrue(List.Transform(delSep, each [b] > 12))
                in
                    (hd and not hm) or (not hm and not hd and diaPrimero) or (hd and hm and diaPrimero)),
            resolver = (p as record) as nullable datetime =>
                let
                    usarDia = usarDiaPorSep{List.PositionOf(separadores, p[sep])},
                    diaAntes = p[a] > 12 or (p[b] <= 12 and usarDia),
                    dia = if diaAntes then p[a] else p[b],
                    mes = if diaAntes then p[b] else p[a],
                    fecha = try #date(p[y], mes, dia) otherwise null,
                    hora = if p[resto] = "" then #duration(0, 0, 0, 0)
                        else (try (let t = Time.FromText(p[resto]) in #duration(0, Time.Hour(t), Time.Minute(t), Time.Second(t))) otherwise #duration(0, 0, 0, 0))
                in
                    if fecha = null then null else DateTime.From(fecha) + hora,
            otras = (s as text) as nullable datetime => try DateTime.FromText(Text.Trim(s), "en-US") otherwise null,
            convertir = (i as number) as nullable datetime =>
                if valores{i} = null then null
                else if partes{i} <> null then resolver(partes{i})
                else otras(valores{i}),
            fechas = List.Transform(List.Positions(valores), each convertir(_))
        in
            List.Transform(fechas, each if _ = null then null else DateTime.ToText(_, formato, "en-US")),'''),
    "fnAFecha": ((), '''    fnAFecha = (v as any, formato as text, largo as number) as nullable date =>
        if v = null then null
        else if v is date then v
        else if v is datetime then DateTime.Date(v)
        else try Date.FromText(Text.Start(Text.From(v, "en-US"), largo), [Format = formato, Culture = "en-US"]) otherwise null,'''),
    "fnDecimales": ((), '''    fnDecimales = (lista as list) as number =>
        let
            ds = List.Transform(List.RemoveNulls(lista), each
                let
                    t = Number.ToText(_, "0.######", "en-US"),
                    p = Text.PositionOf(t, ".")
                in
                    if p < 0 then 0 else Text.Length(t) - p - 1),
            maximo = if List.Count(ds) = 0 then 0 else List.Max(ds)
        in
            List.Min({List.Max({maximo, 2}), 6}),'''),
    "fnMedianaPorGrupo": (("fnDecimales",), '''    fnMedianaPorGrupo = (gEntrada as list, vEntrada as list) as list =>
        let
            g = List.Buffer(gEntrada),
            v = List.Buffer(vEntrada),
            dec = fnDecimales(v),
            tabla = Table.FromColumns({g, v}, {"g", "v"}),
            medianas = Table.Group(tabla, {"g"}, {{"m", each List.Median(List.RemoveNulls([v])), type nullable number}}),
            claves = List.Buffer(Table.Column(medianas, "g")),
            valores = List.Buffer(Table.Column(medianas, "m")),
            global = List.Median(List.RemoveNulls(v)),
            buscar = (x as any) as nullable number =>
                let i = List.PositionOf(claves, x)
                in if i >= 0 and valores{i} <> null then Number.Round(valores{i}, dec) else Number.Round(global, dec)
        in
            List.Transform(List.Positions(v), each if v{_} <> null then v{_} else buscar(g{_})),'''),
}


def _funciones_necesarias(tipos: List[str]) -> List[str]:
    """Nombres de las funciones M que usan los pasos, con sus dependencias, en orden de definicion."""
    por_tipo = {
        "vacios": ["fnEsNulo"], "texto": ["fnLimpiarTexto"], "numericas": ["fnANumero"],
        "coordenadas": ["fnCoord"], "fechas": ["fnUnificarFechas", "fnReemplazarColumna"],
        "limites": ["fnNum"], "formulas": ["fnNum"], "filtros": ["fnNum"],
        "regla": ["fnValores", "fnANumero", "fnDecimales", "fnReemplazarColumna", "fnMedianaPorGrupo", "fnEsNulo"],
        "cierre": ["fnValores", "fnANumero", "fnDecimales", "fnReemplazarColumna", "fnEsNulo"],
        "partes": ["fnAFecha"],
    }
    pedidas = ["fnATexto"]
    for t in tipos:
        pedidas += por_tipo.get(t, [])
    orden: List[str] = []

    def agregar(nombre: str) -> None:
        if nombre in orden:
            return
        for dependencia in _M_FUNCIONES[nombre][0]:
            agregar(dependencia)
        orden.append(nombre)

    for nombre in pedidas:
        agregar(nombre)
    return orden


# =============================================================================
# Traductores: un paso de la limpieza guiada -> pasos M
# =============================================================================

def _m_nombres(cb: _ConstructorM, titulo: str, m: Dict) -> None:
    pares = [(a, b) for a, b in m["mapa"] if a != b]
    if pares:
        cambios = ", ".join("{" + _m_txt(a) + ", " + _m_txt(b) + "}" for a, b in pares)
        cb.agregar(titulo, f"Table.RenameColumns({{prev}}, {{{cambios}}}, MissingField.Ignore)")


def _m_vacios(cb: _ConstructorM, titulo: str, m: Dict) -> None:
    cb.agregar(titulo, "Table.TransformColumns({prev}, List.Transform(Table.ColumnNames({prev}), "
                       "each {_, each if fnEsNulo(_) then null else _}))")


def _m_texto(cb: _ConstructorM, titulo: str, m: Dict) -> None:
    flags = ", ".join(str(m[k]).lower() for k in ("minusculas", "espacios", "comillas"))
    cols = ", ".join("{" + _m_txt(c) + f", each fnLimpiarTexto(_, {flags}), type nullable text}}" for c in m["columnas"])
    if cols:
        cb.agregar(titulo, f"Table.TransformColumns({{prev}}, {{{cols}}})")


def _m_numericas(cb: _ConstructorM, titulo: str, m: Dict) -> None:
    cols = ", ".join("{" + _m_txt(c) + ", each fnANumero(_), type nullable number}" for c in m["columnas"])
    if cols:
        cb.agregar(titulo, f"Table.TransformColumns({{prev}}, {{{cols}}})")


def _m_coordenadas(cb: _ConstructorM, titulo: str, m: Dict) -> None:
    cols = ", ".join("{" + _m_txt(c) + f", each fnCoord(_, {_m_num(a)}, {_m_num(b)}), type nullable number}}"
                     for c, _t, a, b in m["items"])
    if cols:
        cb.agregar(titulo, f"Table.TransformColumns({{prev}}, {{{cols}}})")


def _m_fechas(cb: _ConstructorM, titulo: str, m: Dict) -> None:
    for i, (col, fmt) in enumerate(m["formatos"].items(), 1):
        cb.agregar(f"{titulo} ({col})", f"fnReemplazarColumna({{prev}}, {_m_txt(col)}, fnUnificarFechas("
                                        f"Table.Column({{prev}}, {_m_txt(col)}), {_m_txt(_fmt_m(fmt))}, "
                                        f"{str(m['dia_primero']).lower()}))")


def _m_duplicados(cb: _ConstructorM, titulo: str, m: Dict) -> None:
    cb.agregar(titulo, "Table.Distinct({prev})")


def _cond_fuera(minimo, maximo, expr: str = "n") -> str:
    partes = ([f"{expr} < {_m_num(minimo)}"] if minimo is not None else []) \
        + ([f"{expr} > {_m_num(maximo)}"] if maximo is not None else [])
    return "(" + " or ".join(partes) + ")" if partes else "false"


def _m_limites(cb: _ConstructorM, titulo: str, m: Dict) -> None:
    accion, limites = m["accion"], m["limites"]
    if not limites:
        return
    if accion == "eliminar_fila":
        condiciones = " or ".join(
            f"(let n = fnNum({_campo(c)}) in n <> null and {_cond_fuera(a, b)})" for c, (a, b) in limites.items())
        cb.agregar(titulo, f"Table.SelectRows({{prev}}, each not ({condiciones}))")
    elif accion == "marcar":
        for c, (a, b) in limites.items():
            cb.agregar(f"{titulo} (marca {c})", f"Table.AddColumn({{prev}}, {_m_txt(c + '_atipico')}, each "
                                                f"let n = fnNum({_campo(c)}) in n <> null and {_cond_fuera(a, b)}, type logical)")
    else:
        cols = []
        for c, (a, b) in limites.items():
            if accion == "limitar":
                if a is not None and b is not None:
                    reemplazo = f"(if n < {_m_num(a)} then {_m_num(a)} else {_m_num(b)})"
                else:
                    reemplazo = f"{_m_num(a if a is not None else b)}"
            else:
                reemplazo = "null"
            cols.append("{" + _m_txt(c) + f", each let n = fnNum(_) in if n <> null and {_cond_fuera(a, b)} "
                                          f"then {reemplazo} else _}}")
        cb.agregar(titulo, f"Table.TransformColumns({{prev}}, {{{', '.join(cols)}}})")


def _m_formulas(cb: _ConstructorM, titulo: str, m: Dict) -> None:
    for r in m["reglas"]:
        def leer(clave: str) -> str:
            return f"fnNum({_campo(r[clave])})" if r.get(clave) else "null"
        esperado = (f"let t = fnNum({_campo(r['total'])}), c = {leer('cantidad')}, p = {leer('precio')}, "
                    f"d = {leer('descuento')}, i = {leer('impuesto')}, e = {leer('envio')}, "
                    f"esperado = if c = null or p = null then null else c * p - (if d = null then 0 else d) "
                    f"+ (if i = null then 0 else i) + (if e = null then 0 else e), "
                    f"mal = t <> null and esperado <> null and Number.Abs(t - esperado) > "
                    f"List.Max({{Number.Abs(esperado) * {_m_num(r['tolerancia'])}, 0.01}})")
        total = r["total"]
        if m["accion"] == "marcar":
            cb.agregar(titulo, f"Table.AddColumn({{prev}}, {_m_txt(total + '_revisar')}, each {esperado} in mal, type logical)")
        elif m["accion"] == "eliminar_fila":
            cb.agregar(titulo, f"Table.SelectRows({{prev}}, each not ({esperado} in mal))")
        else:  # recalcular: columna nueva con el total corregido, y se pone en el lugar del original
            nuevo = "__total_corregido"
            cb.agregar(titulo, f"let nombres = Table.ColumnNames({{prev}}), "
                               f"t1 = Table.AddColumn({{prev}}, {_m_txt(nuevo)}, each {esperado} in "
                               f"if mal then Number.Round(esperado, 2) else t, type nullable number), "
                               f"t2 = Table.RemoveColumns(t1, {{{_m_txt(total)}}}), "
                               f"t3 = Table.RenameColumns(t2, {{{{{_m_txt(nuevo)}, {_m_txt(total)}}}}}) "
                               f"in Table.ReorderColumns(t3, nombres)")


_OPERADOR_M = {">": ">", ">=": ">=", "<": "<", "<=": "<=", "==": "=", "!=": "<>"}


def _m_filtros(cb: _ConstructorM, titulo: str, m: Dict) -> None:
    condiciones = []
    for col, op, valor in m["filtros"]:
        if isinstance(valor, (int, float)) and not isinstance(valor, bool):
            cuerpo = f"let n = fnNum({_campo(col)}) in "
            nulo = "n = null or " if op == "!=" else "n <> null and "
            condiciones.append("(" + cuerpo + nulo + f"n {_OPERADOR_M[op]} {_m_num(valor)})")
        else:
            cuerpo = (f"let s = {_campo(col)}, t = if s = null then null else "
                      f"Text.Lower(Text.Trim(Text.From(s, \"en-US\"))) in ")
            nulo = "t = null or " if op == "!=" else "t <> null and "
            condiciones.append("(" + cuerpo + nulo + f"t {_OPERADOR_M[op]} {_m_txt(str(valor).strip().lower())})")
    cb.agregar(titulo, f"Table.SelectRows({{prev}}, each {' and '.join(condiciones)})")


def _m_unificar(cb: _ConstructorM, titulo: str, m: Dict) -> None:
    cols = []
    for col, mapeo in m["mapeos"].items():
        pares = ", ".join("{" + _m_txt(a) + ", " + _m_txt(b) + "}" for a, b in mapeo.items())
        cols.append("{" + _m_txt(col) + f", each let r = List.First(List.Select({{{pares}}}, (p) => p{{0}} = _), null) "
                                         f"in if r = null then _ else r{{1}}}}")
    cb.agregar(titulo, f"Table.TransformColumns({{prev}}, {{{', '.join(cols)}}})")


def _relleno_literal(valor, numerica: bool) -> str:
    """Valor con el que se rellenan los nulos: numero si la columna es numerica y el valor se lee como numero."""
    if numerica:
        try:
            return _m_num(float(valor))
        except (TypeError, ValueError):
            return _m_txt(valor)
    return _m_txt(valor)


def _m_rellenar_columna(col: str, regla: str, valor: str, grupo: str, numerica: bool, palabra: str) -> str:
    """Expresion M que devuelve la tabla {prev} con los nulos de `col` tratados segun la regla."""
    c = _m_txt(col)
    if regla == "eliminar_columna":
        return f"Table.RemoveColumns({{prev}}, {{{c}}}, MissingField.Ignore)"
    if regla == "eliminar_fila":
        return f"Table.SelectRows({{prev}}, each not fnEsNulo({_campo(col)}))"
    if regla in ("valor_fijo", "no_indica", "cero"):
        if regla == "cero":
            relleno = "0" if numerica else '"0"'
        else:
            texto = valor if str(valor).strip() or regla == "valor_fijo" else palabra
            relleno = _relleno_literal(texto, numerica)
        return (f"fnReemplazarColumna({{prev}}, {c}, List.Transform(fnValores({{prev}}, {c}), "
                f"each if _ = null then {relleno} else _))")
    if regla == "moda":
        return (f"let lista = fnValores({{prev}}, {c}), "
                f"moda = List.Sort(List.Modes(List.RemoveNulls(lista))){{0}} in "
                f"fnReemplazarColumna({{prev}}, {c}, List.Transform(lista, each if _ = null then moda else _))")
    if regla in ("mediana", "media"):
        funcion = "List.Median" if regla == "mediana" else "List.Average"
        return (f"let lista = List.Transform(fnValores({{prev}}, {c}), fnANumero), "
                f"relleno = Number.Round({funcion}(List.RemoveNulls(lista)), fnDecimales(lista)) in "
                f"fnReemplazarColumna({{prev}}, {c}, List.Transform(lista, each if _ = null then relleno else _))")
    if regla == "mediana_por_grupo":
        return (f"fnReemplazarColumna({{prev}}, {c}, fnMedianaPorGrupo(Table.Column({{prev}}, {_m_txt(grupo)}), "
                f"List.Transform(fnValores({{prev}}, {c}), fnANumero)))")
    raise ValueError(f"regla de nulos sin traduccion a M: {regla}")


def _m_regla(cb: _ConstructorM, titulo: str, m: Dict, palabra: str) -> None:
    cb.agregar(titulo, _m_rellenar_columna(m["columna"], m["regla"], m.get("valor", ""), m.get("grupo", ""),
                                           bool(m.get("numerica")), palabra))


def _m_cierre(cb: _ConstructorM, titulo: str, m: Dict, palabra: str) -> None:
    texto, numeros = m["texto"], m["numeros"]
    for col in m["textos"]:
        cb.agregar(f"{titulo} ({col})", _m_rellenar_columna(col, "no_indica", texto, "", False, palabra))
    for col in m["numericas"]:
        if numeros == "palabra":
            cb.agregar(f"{titulo} ({col})", _m_rellenar_columna(col, "no_indica", texto, "", False, palabra))
            continue
        c = _m_txt(col)
        if numeros == "cero":
            cb.agregar(f"{titulo} ({col})", _m_rellenar_columna(col, "cero", "", "", True, palabra))
        else:  # mediana; si la columna no tiene ningun numero, se rellena con la palabra
            cb.agregar(f"{titulo} ({col})",
                       f"let lista = List.Transform(fnValores({{prev}}, {c}), fnANumero), "
                       f"hay = List.NonNullCount(lista) > 0, "
                       f"relleno = if hay then Number.Round(List.Median(List.RemoveNulls(lista)), fnDecimales(lista)) "
                       f"else {_m_txt(texto)} in "
                       f"fnReemplazarColumna({{prev}}, {c}, List.Transform(lista, each if _ = null then relleno else _))")


def _largo_fecha(formato_python: str) -> int:
    base = formato_python.split(" %H")[0]
    return len(pd.Timestamp(2000, 12, 28).strftime(base))


def _m_datetime(cb: _ConstructorM, titulo: str, m: Dict) -> None:
    base = m["formato"].split(" %H")[0]
    largo = _largo_fecha(m["formato"])
    if "%H" in m["formato"]:  # el texto trae hora: queda fecha y hora (datetime), como en pandas
        cols = ", ".join("{" + _m_txt(c) + f", each if _ = null then null else (try DateTime.FromText(_, "
                         f"[Format = {_m_txt(_fmt_m(m['formato']))}, Culture = \"en-US\"]) otherwise null), "
                         f"type nullable datetime}}" for c in m["columnas"])
    else:
        cols = ", ".join("{" + _m_txt(c) + f", each fnAFecha(_, {_m_txt(_fmt_m(base))}, {largo}), type nullable date}}"
                         for c in m["columnas"])
    if cols:
        cb.agregar(titulo, f"Table.TransformColumns({{prev}}, {{{cols}}})")


def _m_partes(cb: _ConstructorM, titulo: str, m: Dict) -> None:
    base = m["formato"].split(" %H")[0]
    largo = _largo_fecha(m["formato"])
    for col, nuevas in m["items"]:
        c = _m_txt(col)
        lectura = f"fnAFecha({_campo(col)}, {_m_txt(_fmt_m(base))}, {largo})"
        agregados, anterior = [], "{prev}"
        for i, nombre in enumerate(nuevas, 1):
            funcion = "Date.Month" if nombre.endswith("_mes") else "Date.Year"
            anterior_i = f"a{i}"
            agregados.append(f"{anterior_i} = Table.AddColumn({anterior}, {_m_txt(nombre)}, each "
                             f"let d = {lectura} in if d = null then null else {funcion}(d), Int64.Type)")
            anterior = anterior_i
        cb.agregar(f"{titulo} ({col})",
                   f"let {', '.join(agregados)}, nombres = Table.ColumnNames({{prev}}), "
                   f"orden = List.InsertRange(nombres, List.PositionOf(nombres, {c}) + 1, {_m_lista(nuevas)}) in "
                   f"Table.ReorderColumns({anterior}, orden)")


_TRADUCTORES = {"nombres": _m_nombres, "vacios": _m_vacios, "texto": _m_texto, "numericas": _m_numericas,
                "coordenadas": _m_coordenadas, "fechas": _m_fechas, "duplicados": _m_duplicados,
                "limites": _m_limites, "formulas": _m_formulas, "filtros": _m_filtros, "unificar": _m_unificar,
                "datetime": _m_datetime, "partes": _m_partes}
_TRADUCTORES_CON_PALABRA = {"regla": _m_regla, "cierre": _m_cierre}


def _tipo_m(serie: pd.Series, columna: str, formatos_fecha: Dict[str, str]) -> str:
    """Tipo M de una columna de la tabla final (segun su dtype en pandas)."""
    if pd.api.types.is_bool_dtype(serie):
        return "type logical"
    if pd.api.types.is_datetime64_any_dtype(serie):
        return "type date" if "%H" not in formatos_fecha.get(columna, "") else "type datetime"
    if pd.api.types.is_integer_dtype(serie):
        return "Int64.Type"
    if pd.api.types.is_numeric_dtype(serie):
        return "type number"
    return "type text"


_ESCAPA_PYTHON = str.maketrans({'"': '""', "\n": "#(lf)", "\r": "#(cr)", "\t": "#(tab)"})


# =============================================================================
# Las tres salidas
# =============================================================================

def script_powerbi_python(resultado) -> str:
    """Script Python para «Transformar > Ejecutar script de Python» en Power Query: la tabla llega en
    `dataset` y el resultado sale en `dataset_limpio`. No lee ni escribe archivos."""
    return LG.generar_script_limpieza(resultado.pasos, resultado.tokens, resultado.nombre_archivo, resultado.hoja,
                                      modo="powerbi")


def m_con_python(resultado, nombre_paso_anterior: str = "TuPasoAnterior") -> str:
    """Consulta M que ejecuta el script de Python anterior con Python.Execute sobre el paso anterior."""
    script = script_powerbi_python(resultado).replace("#(", "#(#)(").translate(_ESCAPA_PYTHON)
    referencia = _m_ident(nombre_paso_anterior.strip() or "TuPasoAnterior")
    return f'''// =============================================================================
// Limpieza guiada para Power BI: M con Python (Python.Execute).
// Generado por Limpiador de Tablas. NO cambia el archivo original: la limpieza se repite en cada
// actualizacion de la consulta.
//
// Como usarlo: Power Query -> clic derecho en la consulta -> "Editor avanzado" -> pegue esto
// (o inserte los pasos antes del "in"). Ajuste "{nombre_paso_anterior}" al nombre real de su ultimo paso.
// Requiere Python con pandas y numpy (Power BI Desktop -> Opciones -> Python scripting).
// Si no quiere depender de Python, use la version de M puro.
// =============================================================================

let
    Origen = {referencia},

    EjecutarLimpieza = Python.Execute("{script}", [dataset = Origen]),

    TablaLimpia = EjecutarLimpieza{{[Name = "dataset_limpio"]}}[Value]
in
    TablaLimpia
'''


_NOTAS_M_PURO = '''// Equivalencias con la limpieza de la app (M no tiene expresiones regulares ni la libreria de pandas):
//  - Numeros: se leen igual que en la app ("₡1.234,56", "1,234.56", "7,5"); lo que trae letras queda nulo.
//  - Fechas: se reconocen dd/mm/aaaa, mm/dd/aaaa, aaaa-mm-dd, y formas en ingles ("Jan 31 2025"). Un 05/06/2025
//    ambiguo se resuelve como en la app (mirando las demas fechas de la columna y, si no hay pistas, el
//    criterio elegido). Formatos muy raros pueden quedar nulos aqui y leerse en la app.
//  - Coordenadas: solo decimales (con punto o coma); no se leen grados/minutos/segundos.
//  - Medianas y medias se redondean a los decimales de los datos (minimo 2, maximo 6), como en la app.
//  - Los tipos de las columnas se fijan al final segun la tabla limpia de la app.
// La primera vez, compare filas, columnas y valores con la tabla limpia que guardo la app.
'''


def m_puro(resultado, nombre_paso_anterior: str = "TuPasoAnterior") -> str:
    """Consulta M 100% nativa (sin Python) con los mismos pasos de la limpieza guiada, para pegar en el Editor
    avanzado de Power Query. Los datos se leen del paso anterior, asi el archivo original no cambia."""
    pasos = [p for p in resultado.pasos if getattr(p, "m", None)]
    tipos = [p.m["tipo"] for p in pasos]
    palabra = getattr(resultado, "palabra", LG.TEXTO_NO_INDICA)
    referencia = _m_ident(nombre_paso_anterior.strip() or "TuPasoAnterior")

    cb = _ConstructorM("Origen")
    cb.agregar("Origen", referencia)
    cb.agregar("Todo a texto (como al leer un CSV)",
               "Table.TransformColumns({prev}, List.Transform(Table.ColumnNames({prev}), "
               "each {_, fnATexto, type nullable text}))")
    formatos_fecha: Dict[str, str] = {}
    for numero, paso in enumerate(pasos, 1):
        m, titulo = paso.m, f"{numero}. {paso.titulo}"
        if m["tipo"] == "fechas":
            formatos_fecha.update(m["formatos"])
        if m["tipo"] == "datetime":
            formatos_fecha.update({c: m["formato"] for c in m["columnas"]})
        if m["tipo"] in _TRADUCTORES_CON_PALABRA:
            _TRADUCTORES_CON_PALABRA[m["tipo"]](cb, titulo, m, palabra)
        else:
            _TRADUCTORES[m["tipo"]](cb, titulo, m)

    final = resultado.df
    tipos_columnas = ", ".join("{" + _m_txt(c) + ", " + _tipo_m(final[c], c, formatos_fecha) + "}" for c in final.columns)
    cb.agregar("Tipos de columna finales",
               f"Table.TransformColumnTypes({{prev}}, {{{tipos_columnas}}}, \"en-US\")")

    funciones = "".join(_M_FUNCIONES[n][1] + "\n\n" for n in _funciones_necesarias(tipos))
    tokens = ", ".join(_m_txt(t) for t in resultado.tokens)
    preambulo = (f"    // ---- Textos que cuentan como nulo ----\n    TokensNulos = {{{tokens}}},\n\n"
                 f"    // ---- Funciones auxiliares ----\n{funciones}    // ---- Pasos de la limpieza ----\n")
    cuerpo = cb.construir(preambulo)
    return (f"// =============================================================================\n"
            f"// Limpieza guiada para Power BI: M puro (sin Python).\n"
            f"// Generado por Limpiador de Tablas. NO cambia el archivo original: la limpieza se repite en cada\n"
            f"// actualizacion de la consulta.\n"
            f"//\n"
            f"// Como usarlo: Power Query -> clic derecho en la consulta -> \"Editor avanzado\" -> pegue esto.\n"
            f"// Ajuste \"{nombre_paso_anterior}\" al nombre real de su ultimo paso (o deje que la consulta lea\n"
            f"// directamente su origen de datos).\n"
            f"//\n{_NOTAS_M_PURO}"
            f"// =============================================================================\n\n{cuerpo}")


def exportar_powerbi(resultado, nombre_paso_anterior: str = "TuPasoAnterior") -> Dict[str, str]:
    """Las tres salidas: {'script_powerbi': .py, 'm_python': .m, 'm_puro': .m}."""
    return {"script_powerbi": script_powerbi_python(resultado),
            "m_python": m_con_python(resultado, nombre_paso_anterior),
            "m_puro": m_puro(resultado, nombre_paso_anterior)}

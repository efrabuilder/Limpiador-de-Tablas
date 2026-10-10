# -*- coding: utf-8 -*-
"""
negocio.py
==========
Llena las cuatro columnas «de negocio» del diccionario de datos para CUALQUIER tabla:

    Origen                    de dónde sale el campo (archivo, hoja, tabla, cruce o limpieza)
    Justificación de negocio  por qué importa el campo, con los datos reales de esa columna
    Clasificación ejecutiva   KPI, Variable transformada, Llave / identificador,
                              Atributo descriptivo o Metadato de control
    Modelo de datos           papel del campo en un modelo analítico (PK/FK, medida y cómo se
                              agrega, atributo de dimensión, tiempo, fuera del modelo)

Nada es una frase fija: cada texto se arma con el nombre del campo (en español o inglés, con
abreviaturas y nombres pegados), su tipo, su contenido (rango, grupos, unicidad, vacíos, valores
atípicos), la tabla a la que pertenece y sus columnas vecinas (por ejemplo, de qué KPIs y por qué
ejes se puede analizar). Lo que la persona ya escribió en el diccionario no se toca: solo se
llenan las celdas vacías.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .descripciones import _ENTIDADES, _ID, _con_tildes, normalizar_nombre
from .limpieza_guiada import ROLES, TOKENS_NULOS_BASE, a_numero, es_nulo

# --------------------------------------------------------------------------
# Nombres de columnas y valores permitidos
# --------------------------------------------------------------------------

COLUMNA_ORIGEN = "Origen"
COLUMNA_JUSTIFICACION = "Justificación de negocio"
COLUMNA_CLASIFICACION = "Clasificación ejecutiva"
COLUMNA_MODELO = "Modelo de datos"
COLUMNAS_NEGOCIO = (COLUMNA_ORIGEN, COLUMNA_JUSTIFICACION, COLUMNA_CLASIFICACION, COLUMNA_MODELO)

CLASIFICACION_KPI = "KPI"
CLASIFICACION_TRANSFORMADA = "Variable transformada"
CLASIFICACION_LLAVE = "Llave / identificador"
CLASIFICACION_ATRIBUTO = "Atributo descriptivo"
CLASIFICACION_METADATO = "Metadato de control"
CLASIFICACIONES = (CLASIFICACION_KPI, CLASIFICACION_TRANSFORMADA, CLASIFICACION_LLAVE,
                   CLASIFICACION_ATRIBUTO, CLASIFICACION_METADATO)

TRATAMIENTOS_QUE_TRANSFORMAN = ("Rellenados", "Celdas vacías convertidas", "Se eliminaron")

_ROL_POR_TEXTO = {texto: clave for clave, texto in ROLES.items()}
_FECHA_LATINA = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}")
_BOOLEANOS = {"si", "sí", "no", "s", "n", "yes", "y", "true", "false", "verdadero", "falso", "1", "0", "x",
              "t", "f", "1.0", "0.0"}


# --------------------------------------------------------------------------
# Formato en español
# --------------------------------------------------------------------------

def _num(valor, decimales: int = 2) -> str:
    """Número en prosa española: 1234567.891 -> '1.234.567,89'; 12.0 -> '12'."""
    valor = float(valor)
    if not np.isfinite(valor):
        return ""
    if valor.is_integer() and abs(valor) < 1e15:
        texto = f"{int(valor):,}"
    else:
        texto = f"{valor:,.{decimales}f}"
        if "." in texto:
            texto = texto.rstrip("0").rstrip(".")
    return texto.replace(",", "§").replace(".", ",").replace("§", ".")


def _pct(parte: float) -> str:
    """0.975 -> '97,5 %'; 1 -> '100 %'."""
    valor = round(float(parte) * 100, 1)
    return (f"{int(valor)} %" if valor.is_integer() else f"{valor:.1f}".replace(".", ",") + " %")


def _lista(valores: Sequence[str], conector: str = "y") -> str:
    valores = [str(v) for v in valores if str(v)]
    if len(valores) <= 1:
        return "".join(valores)
    return ", ".join(valores[:-1]) + f" {conector} " + valores[-1]


def _comillas(texto: str, largo: int = 40) -> str:
    texto = str(texto).strip()
    return f"«{texto if len(texto) <= largo else texto[:largo - 1] + '…'}»"


def _legible(nombre) -> str:
    return _con_tildes(normalizar_nombre(nombre) or str(nombre))


def _duracion_texto(dias: float) -> str:
    if dias < 1:
        return "un solo día"
    if dias < 60:
        return f"{int(round(dias))} días"
    if dias < 730:
        return f"{int(round(dias / 30.4))} meses"
    return f"{_num(dias / 365.25, 1)} años"


# --------------------------------------------------------------------------
# Conceptos: qué significa el nombre del campo para el negocio
# --------------------------------------------------------------------------

class _Concepto:
    def __init__(self, clave: str, palabras: str, dimension: str = "", agregacion: str = "",
                 medida: str = "", categoria: str = "") -> None:
        self.clave = clave
        self.palabras = frozenset(palabras.split())
        self.dimension = dimension
        self.agregacion = agregacion
        self.medida = medida      # frase cuando el campo es una medida numérica
        self.categoria = categoria  # frase cuando el campo agrupa registros


# El orden importa: gana el primero que coincide (lo más específico va antes).
_CONCEPTOS: Tuple[_Concepto, ...] = (
    _Concepto("resultado",
              "churn cancelacion cancelo cancel abandono abandon fuga fraude fraud mora morosidad default impago "
              "conversion convirtio retencion retuvo survived supervivencia sobrevivio renovo renovacion recompra "
              "desercion deserto",
              "Resultado", "CONTEO o porcentaje de registros con valor Sí/1",
              categoria="Es el resultado que el negocio quiere medir o anticipar (retención, cancelación, "
                        "fraude, conversión…)"),
    _Concepto("contacto", "email correo mail telefono tel celular movil phone whatsapp", "Contacto"),
    _Concepto("geografia", "pais country ciudad city provincia province canton distrito region municipio localidad "
                           "barrio zona direccion address calle postal zip", "Geografía",
              categoria="Ubica dónde ocurre cada registro: permite el análisis territorial y detectar en qué "
                        "zonas se concentra la actividad"),
    _Concepto("edad", "edad age", "Perfil demográfico", "PROMEDIO o rangos de edad; no se suma",
              medida="Edad de cada registro: permite segmentar por grupos de edad y ver cómo cambia el "
                     "comportamiento entre ellos"),
    _Concepto("tasa", "porcentaje pct percent tasa rate ratio proporcion indice index",
              "", "PROMEDIO (idealmente ponderado); no se suma porque es una tasa o proporción",
              medida="Expresa una proporción o tasa, por lo que permite comparar el desempeño entre grupos sin "
                     "que influya su tamaño"),
    _Concepto("puntaje", "score rating puntaje puntuacion calificacion nota satisfaccion nps evaluacion ranking",
              "", "PROMEDIO (junto con su distribución); no se suma",
              medida="Resume una calificación o nivel de satisfacción: permite comparar la calidad percibida "
                     "entre grupos y su evolución"),
    _Concepto("duracion",
              "duracion antiguedad tenure plazo demora retraso delay dias horas minutos segundos meses anios "
              "tiempo days hours minutes seconds months years",
              "", "PROMEDIO o MEDIANA; sumarlo solo tiene sentido como tiempo total acumulado",
              medida="Mide cuánto dura o tarda algo: permite detectar demoras, comparar eficiencia y anticipar "
                     "cargas de trabajo"),
    _Concepto("tiempo_parte", "anio ano year mes month dia day semana week trimestre quarter hora hour periodo "
                              "period", "Tiempo",
              categoria="Es una parte de la fecha: permite agrupar por período y ver estacionalidad"),
    _Concepto("cantidad",
              "cantidad qty quantity unidad unit pieza volumen stock inventario existencia conteo count n num "
              "nro numero visita intento reclamo hijo compra pedido queja devolucion llamada falla accidente "
              "multa incidente dependiente",
              "", "SUMA (total acumulado); el PROMEDIO sirve para ver el valor típico por registro",
              medida="Mide volumen o frecuencia (unidades, eventos, registros): sirve para dimensionar la "
                     "actividad y calcular totales"),
    _Concepto("dinero",
              "precio price costo cost monto amount importe total ingreso income revenue venta sale pago payment "
              "tarifa fare fee comision salario salary sueldo saldo balance margen utilidad profit ganancia deuda "
              "debt gasto expense descuento discount impuesto tax iva prima premium cargo charge valor value "
              "presupuesto budget flete credito deposito retiro propina tip",
              "", "SUMA (total en dinero); el PROMEDIO sirve para el valor típico por registro",
              medida="Es una cifra monetaria: base para calcular ingresos, costos o márgenes y comparar el "
                     "resultado económico"),
    _Concepto("fisica",
              "peso weight altura height talla ancho largo profundidad temperatura temp presion velocidad "
              "distancia distance superficie capacidad potencia consumo kilometraje km humedad",
              "", "PROMEDIO, MÍNIMO y MÁXIMO; no se suma",
              medida="Es una medición física: permite caracterizar cada registro, comparar grupos y detectar "
                     "lecturas fuera de lo normal"),
    _Concepto("organizacion",
              "sucursal tienda store branch departamento department area equipo team empresa company sede "
              "oficina almacen bodega agencia", "Organización",
              categoria="Indica la unidad de la organización a la que pertenece el registro: permite comparar "
                        "el desempeño entre sucursales, áreas o equipos"),
    _Concepto("producto",
              "producto product articulo categoria category marca brand modelo model linea familia plato receta "
              "servicio service plan", "Producto",
              categoria="Clasifica lo que se vende o gestiona: permite saber qué productos o categorías pesan "
                        "más y cuáles rinden menos"),
    _Concepto("persona",
              "cliente customer usuario user empleado employee paciente patient estudiante student alumno "
              "proveedor supplier vendedor seller agente agent socio miembro member persona person apellido "
              "solicitante beneficiario asegurado conductor medico doctor profesor teacher", "Persona",
              categoria="Indica a quién corresponde cada registro: permite ver la concentración por cliente, "
                        "responsable o grupo de personas"),
    _Concepto("demografia", "sexo sex genero gender nacionalidad ocupacion profesion escolaridad educacion "
                            "education civil marital idioma", "Perfil demográfico",
              categoria="Describe el perfil de las personas: permite segmentar y comparar comportamientos por "
                        "grupo"),
    _Concepto("canal", "canal channel metodo method medio forma modalidad fuente source plataforma platform "
                       "dispositivo device campana campaign", "Canal / método",
              categoria="Indica por qué canal o método ocurre cada registro: permite comparar la efectividad de "
                        "cada uno"),
    _Concepto("proceso",
              "estado status etapa fase situacion resultado result condicion motivo razon reason causa cause "
              "prioridad priority severidad riesgo nivel level grado tipo type clase class", "Estado / clasificación",
              categoria="Indica en qué situación, etapa o clase está cada registro: permite medir cuántos se "
                        "completan, quedan pendientes o se pierden"),
    _Concepto("texto_libre",
              "comentario comment observacion observation descripcion description detalle detail mensaje message "
              "opinion resena review texto text asunto subject nota", "Descriptivo"),
    _Concepto("nombre", "nombre name titulo title etiqueta label", "Descriptivo"),
)

_CONCEPTO_GEO = next(c for c in _CONCEPTOS if c.clave == "geografia")
_CONCEPTO_PROCESO = next(c for c in _CONCEPTOS if c.clave == "proceso")

_MARCAS_CONTEO = {"n", "num", "nro", "numero", "conteo", "count", "cantidad", "qty", "quantity", "number"}
_PALABRAS_VALOR_UNITARIO = {"precio", "price", "tarifa", "fare", "unitario", "unit", "unidad", "promedio",
                            "average", "avg", "medio"}

_ATRIBUTOS_NUMERICOS = {"anio", "ano", "year", "mes", "month", "dia", "day", "semana", "week", "trimestre",
                        "quarter", "hora", "hour", "edad", "age", "grado", "ranking", "posicion", "orden",
                        "indice", "index", "fila", "row", "pclass", "clase", "nivel", "level"}


def _tokens(nombre) -> List[str]:
    return normalizar_nombre(nombre).split()


def _formas(token: str) -> set:
    """El token y sus posibles singulares: ciudades -> ciudad, clientes -> cliente."""
    formas = {token}
    if token.endswith("s") and len(token) > 3:
        formas.add(token[:-1])
    if token.endswith("es") and len(token) > 4:
        formas.add(token[:-2])
    if token.endswith("ones") and len(token) > 5:
        formas.add(token[:-2])
    return formas


def concepto_de(nombre, vecinas: Optional[set] = None) -> Optional[_Concepto]:
    """Concepto de negocio que sugiere el nombre del campo (None si no dice nada)."""
    tokens = _tokens(nombre)
    if not tokens:
        return None
    vecinas = vecinas or set()
    if tokens == ["estado"] or tokens == ["state"]:
        return _CONCEPTO_GEO if vecinas & _CONCEPTO_GEO.palabras else _CONCEPTO_PROCESO
    formas = [_formas(t) for t in tokens]
    if len(tokens) >= 2 and any(t in _MARCAS_CONTEO for t in tokens):  # «num_visitas_12m», «cantidad_total»
        return next(c for c in _CONCEPTOS if c.clave == "cantidad")
    for concepto in _CONCEPTOS:
        if any(f & concepto.palabras for f in formas):
            return concepto
    if len(tokens) == 1 and len(tokens[0]) >= 6:  # nombres pegados: «totalventas», «fechaventa»
        pegado = tokens[0]
        for concepto in _CONCEPTOS:
            if any(len(p) >= 5 and p in pegado for p in concepto.palabras):
                return concepto
    return None


def _semantica_fecha(tokens: Sequence[str]) -> str:
    texto = " ".join(tokens)
    reglas = (
        ("nacimiento", ("nacimiento", "nac ", "birth", "born")),
        ("vencimiento", ("vencimiento", "vence", "expir", "caduc", "due")),
        ("actualizacion", ("actualiz", "update", "modific")),
        ("cierre", ("fin", "end", "cierre", "entrega", "delivery", "salida", "close", "termin")),
        ("alta", ("creacion", "creado", "create", "registro", "alta", "inicio", "start", "ingreso", "apertura")),
        ("operacion", ("venta", "compra", "pedido", "orden", "transaccion", "factura", "pago", "order", "sale",
                       "purchase", "payment", "operacion", "reserva", "visita", "consulta")),
    )
    for clave, marcas in reglas:
        if any(m in texto for m in marcas):
            return clave
    return ""


# --------------------------------------------------------------------------
# Perfil de cada columna (todo sale de los datos)
# --------------------------------------------------------------------------

def _es_metadato(nombre) -> bool:
    nombre = str(nombre)
    return (nombre.startswith("_revisar_calidad") or nombre in ("_merge", "_hoja")
            or nombre.endswith("_original"))


def _fechas_de(con_dato: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(con_dato):
        return con_dato.dropna()
    texto = con_dato.astype(str).str.strip()
    dayfirst = bool(texto.str.match(_FECHA_LATINA).any())
    return pd.to_datetime(texto, errors="coerce", dayfirst=dayfirst).dropna()


def _perfil(serie: pd.Series, tipo: str, tokens=TOKENS_NULOS_BASE) -> Dict:
    n = len(serie)
    con_dato = serie[~es_nulo(serie, tokens)]
    validos = len(con_dato)
    p: Dict = {"n": n, "validos": validos, "nulos": n - validos,
               "completitud": (validos / n) if n else 1.0, "unicos": 0, "unicidad": 0.0, "top": []}
    if validos == 0:
        return p
    texto = con_dato.astype(str).str.strip()
    conteo = texto.value_counts()
    p["unicos"] = int(len(conteo))
    p["unicidad"] = len(conteo) / validos
    p["top"] = [(str(v), c / validos) for v, c in conteo.head(3).items()]
    p["valores"] = {str(v).lower() for v in conteo.index[:20]}
    p["longitud_max"] = int(texto.str.len().max())
    if tipo in ("Entero", "Decimal"):
        numeros = (con_dato if pd.api.types.is_numeric_dtype(serie) and not pd.api.types.is_bool_dtype(serie)
                   else a_numero(con_dato)).dropna().astype(float)
        p["no_numericos"] = int(validos - len(numeros))  # texto o símbolos que no se leen como número
        if len(numeros):
            p.update(minimo=float(numeros.min()), maximo=float(numeros.max()), media=float(numeros.mean()),
                     enteros=bool((numeros % 1 == 0).all()), distintos=int(numeros.nunique()))
            if len(numeros) >= 20:
                q1, q3 = numeros.quantile([0.25, 0.75])
                if q3 > q1:
                    fuera = ((numeros < q1 - 1.5 * (q3 - q1)) | (numeros > q3 + 1.5 * (q3 - q1))).mean()
                    p["atipicos"] = float(fuera)
    elif tipo == "Fecha":
        fechas = _fechas_de(con_dato)
        if len(fechas):
            p.update(f_min=fechas.min(), f_max=fechas.max())
    return p


def _es_binario(p: Dict) -> bool:
    return p.get("unicos") == 2 and p.get("valores", set()) <= _BOOLEANOS | {"si", "sí", "no"}


def _tipo_campo(nombre, rol: str, tipo: str, p: Dict, es_llave: bool, concepto: Optional[_Concepto]) -> str:
    """metadato, llave, vacio, constante, contacto, coordenada, fecha, indicador, medida, categoria o texto."""
    if _es_metadato(nombre):
        return "metadato"
    if es_llave:
        return "llave"
    if tipo == "Vacío" or p["validos"] == 0:
        return "vacio"
    if rol == "email" or rol == "telefono":
        return "contacto"
    if rol == "coordenada":
        return "coordenada"
    if p["unicos"] == 1 and p["validos"] > 1:
        return "constante"
    if tipo == "Fecha":
        return "fecha"
    if tipo == "Sí/No" or _es_binario(p):
        return "indicador"
    clave = concepto.clave if concepto else ""
    if tipo in ("Entero", "Decimal"):
        if clave == "tiempo_parte":
            return "categoria"
        pocos = p.get("enteros") and p["unicos"] <= 10 and p["unicidad"] <= 0.5
        if clave in ("dinero", "cantidad", "tasa", "puntaje", "duracion", "fisica", "edad"):
            return "medida"
        return "categoria" if pocos else "medida"
    if tipo == "Categoría":
        return "categoria"
    if clave in ("texto_libre", "nombre"):
        return "texto"
    if concepto is not None and concepto.categoria and p["unicidad"] < 0.95:
        return "categoria"  # «ciudad», «sucursal», «producto»: agrupan aunque la tabla sea chica
    if tipo == "Texto (código)":
        return "categoria" if p["unicidad"] <= 0.5 else "texto"
    return "categoria" if (p["unicos"] <= 50 and p["unicidad"] <= 0.2) else "texto"


# --------------------------------------------------------------------------
# Clasificación ejecutiva
# --------------------------------------------------------------------------

_CONCEPTOS_KPI = {"dinero", "tasa", "puntaje", "cantidad", "duracion"}


def _clasificar(nombre, kind: str, concepto: Optional[_Concepto], tratamiento: str, p: Dict) -> str:
    if kind == "metadato":
        return CLASIFICACION_METADATO
    if kind == "llave":
        return CLASIFICACION_LLAVE
    clave = concepto.clave if concepto else ""
    es_kpi_fuerte = kind == "medida" and clave in _CONCEPTOS_KPI
    if kind == "indicador" and clave == "resultado":
        es_kpi_fuerte = True
    if es_kpi_fuerte:
        return CLASIFICACION_KPI
    if str(tratamiento).startswith(TRATAMIENTOS_QUE_TRANSFORMAN) and kind not in ("vacio", "constante"):
        return CLASIFICACION_TRANSFORMADA
    if kind == "medida" and clave not in ("edad", "fisica") and not (_ATRIBUTOS_NUMERICOS & set(_tokens(nombre))):
        return CLASIFICACION_KPI
    return CLASIFICACION_ATRIBUTO


# --------------------------------------------------------------------------
# Origen
# --------------------------------------------------------------------------

def _separar_fuente(fuente: str) -> Tuple[str, str]:
    """'datos.xlsx::Ventas' -> ('datos.xlsx', 'Ventas')."""
    archivo, _, hoja = str(fuente).partition("::")
    return archivo.strip(), hoja.strip()


def _origen(nombre, posicion: int, total: int, kind: str, tratamiento: str, nombre_tabla: str,
            fuentes: Sequence[str], origen_dado: str, cruce: Optional[Dict] = None,
            cruces: Sequence[Dict] = ()) -> str:
    nombre = str(nombre)
    if nombre == "_merge":
        return "Generada por el cruce de tablas: auditoría que indica de qué tabla viene cada fila."
    if nombre == "_hoja":
        return "Generada al cargar el libro de Excel: nombre de la hoja de la que viene cada fila."
    if nombre.startswith("_revisar_calidad"):
        return "Generada por la limpieza de datos: marca con los hallazgos de calidad dejados solo señalados."
    if nombre.endswith("_original"):
        return (f"Generada por la limpieza de datos: copia de «{nombre[:-len('_original')]}» tomada antes de "
                "modificarla.")
    transformada = str(tratamiento).startswith(TRATAMIENTOS_QUE_TRANSFORMAN)
    cola = " Transformada en la limpieza." if transformada else ""
    ubicacion = f"columna «{nombre}» (posición {posicion} de {total})"
    if origen_dado or cruce:
        primero = cruces[0] if cruces else {}
        nombres = {"Tabla A": primero.get("tabla_a", ""), "Tabla B": primero.get("tabla_b", "")}
        if cruce:  # llave de unión: viene de las dos tablas
            texto = (f"Tabla A («{cruce['tabla_a']}») y Tabla B («{cruce['tabla_b']}»), llave de unión del "
                     f"cruce {cruce['cruce']}")
        elif origen_dado in nombres and nombres[origen_dado]:
            texto = f"{origen_dado} («{nombres[origen_dado]}»)"
        else:
            texto = origen_dado.rstrip(". ")
        return f"{texto}, {ubicacion}.{cola}"
    if len(fuentes) == 1:
        archivo, hoja = _separar_fuente(fuentes[0])
        if archivo:
            partes = f"archivo «{archivo}»" + (f", hoja «{hoja}»" if hoja else "")
            if nombre_tabla and nombre_tabla != archivo.rsplit(".", 1)[0] and not nombre_tabla.startswith(
                    archivo.rsplit(".", 1)[0]):
                partes += f" (tabla «{nombre_tabla}»)"
            return f"Viene del {partes}, {ubicacion}.{cola}"
    if len(fuentes) > 1:
        return (f"Viene de las fuentes {_lista([_comillas(_separar_fuente(f)[0]) for f in fuentes])} "
                f"unidas en la tabla «{nombre_tabla or 'maestra'}», {ubicacion}.{cola}")
    if nombre_tabla:
        return f"Viene de la tabla «{nombre_tabla}», {ubicacion}.{cola}"
    return f"Viene de la tabla cargada, {ubicacion}.{cola}"


# --------------------------------------------------------------------------
# Justificación de negocio
# --------------------------------------------------------------------------

def _entidad_llave(tokens: Sequence[str]) -> str:
    """'id_cliente' -> 'cliente'; 'customer_id' -> 'cliente' (entidades conocidas en español e inglés)."""
    resto = [t for t in tokens if t not in _ID and t not in ("de", "del", "la", "el", "los", "las")]
    texto = " ".join(resto)
    for patron, sustantivo in _ENTIDADES:
        if texto and patron.search(texto):
            return sustantivo
    return _con_tildes(texto)


def _nota_nulos(p: Dict, tratamiento: str, detalle: str) -> str:
    if not p["nulos"]:
        return ""
    base = f" Le faltan {_num(p['nulos'])} de {_num(p['n'])} valores ({_pct(1 - p['completitud'])})"
    if str(tratamiento).startswith(TRATAMIENTOS_QUE_TRANSFORMAN):
        return f"{base}; se trataron en la limpieza ({str(tratamiento)[0].lower() + str(tratamiento)[1:]})."
    return f"{base}: {detalle}"


def _justificar(info: Dict, ctx: Dict) -> str:
    kind, p, concepto = info["kind"], info["p"], info["concepto"]
    nombre, n_legible, tratamiento = info["nombre"], info["legible"], info["tratamiento"]
    t = f"«{ctx['tabla']}»" if ctx["tabla"] else "esta tabla"
    kpis, ejes = ctx["kpis"], ctx["ejes"]
    por_ejes = f" Permite comparar su comportamiento {ejes}." if ejes else ""
    nombre_q = f"«{n_legible}»"
    nulos_lectura = "conviene confirmar si la ausencia es esperada antes de interpretar los resultados."

    if kind == "metadato":
        if nombre == "_merge":
            return ("Auditoría del cruce: permite comprobar cuántos registros se emparejaron y cuáles quedaron "
                    "solo en una de las tablas; no es una variable de negocio.")
        if nombre == "_hoja":
            return ("Indica de qué hoja del libro viene cada fila: permite separar o auditar los datos por hoja; "
                    "no es una variable de negocio.")
        if nombre.startswith("_revisar_calidad"):
            return ("Marca de calidad: lista los hallazgos que se dejaron solo señalados en cada fila, para "
                    "priorizar la revisión manual; no es una variable de negocio.")
        return (f"Respaldo del valor de «{nombre[:-len('_original')]}» antes de limpiar: permite "
                "auditar y revertir los cambios; no se usa en el análisis.")

    if kind == "vacio":
        return (f"Sin datos: las {_num(p['n'])} filas de {nombre_q} están vacías, por lo que hoy no aporta al "
                "análisis; conviene recuperar la fuente o excluirla del modelo.")

    if kind == "constante":
        valor = p["top"][0][0] if p["top"] else ""
        return (f"Tiene un único valor ({_comillas(valor)}) en {_num(p['validos'])} filas: no permite distinguir "
                "ni comparar registros, así que no aporta a decisiones salvo como dato fijo de la fuente."
                + _nota_nulos(p, tratamiento, nulos_lectura))

    if kind == "llave":
        tokens = _tokens(nombre)
        entidad = _entidad_llave(tokens)
        cruce = ctx["cruces"].get(str(nombre))
        if p["unicidad"] >= 0.999:
            texto = (f"Identifica de forma única cada registro de {t} ({_num(p['unicos'])} valores distintos en "
                     f"{_num(p['validos'])} filas): permite localizar un caso, detectar duplicados y unir esta "
                     "tabla con otras sin ambigüedad.")
        elif p["unicidad"] >= 0.9 and not entidad:
            repetidos = p["validos"] - p["unicos"]
            texto = (f"Identifica cada registro de {t} ({_num(p['unicos'])} valores distintos en "
                     f"{_num(p['validos'])} filas), pero {_num(repetidos)} "
                     f"{'valor se repite' if repetidos == 1 else 'valores se repiten'}: hay que revisar duplicados "
                     "antes de usarlo para unir esta tabla con otras.")
        else:
            veces = p["validos"] / max(p["unicos"], 1)
            sujeto = f"el {entidad}" if entidad else f"el valor de {nombre_q}"
            destino = f"la tabla de {entidad}" if entidad else "otra tabla que use la misma llave"
            texto = (f"Vincula cada registro con {sujeto}: {_num(p['unicos'])} valores distintos en "
                     f"{_num(p['validos'])} filas (cada uno aparece unas {_num(veces, 1)} veces), por lo que "
                     f"permite agrupar los registros y unirlos con {destino}.")
        if cruce:
            texto += (f" Es la llave del cruce entre «{cruce['tabla_a']}» y «{cruce['tabla_b']}»: de su calidad "
                      "depende que las filas se emparejen bien.")
        if p["nulos"]:
            texto += (f" Tiene {_num(p['nulos'])} vacíos ({_pct(1 - p['completitud'])}): esas filas no se podrán "
                      "unir con otras tablas.")
        return texto

    if kind == "contacto":
        medio = "correo electrónico" if info["rol"] == "email" else "teléfono"
        return (f"Permite ubicar y contactar a la persona de cada registro por {medio} "
                f"({_pct(p['completitud'])} de las filas con dato) y detectar contactos repetidos. Es un dato "
                "personal: restringir su acceso y no usarlo en análisis agregados."
                + _nota_nulos(p, tratamiento, "esos registros no se podrán contactar por este medio."))

    if kind == "coordenada":
        rango = f" ({_num(p['minimo'], 4)} a {_num(p['maximo'], 4)} grados)" if "minimo" in p else ""
        return (f"Posición geográfica{rango}: permite ubicar los registros en un mapa, medir distancias y "
                "agruparlos por zona." + _nota_nulos(p, tratamiento, "esos registros no aparecerán en el mapa."))

    if kind == "fecha":
        marcas = _tokens(nombre)
        semantica = _semantica_fecha(marcas)
        rango = ""
        if "f_min" in p:
            dias = (p["f_max"] - p["f_min"]).days
            rango = (f"Cubre del {p['f_min']:%d/%m/%Y} al {p['f_max']:%d/%m/%Y} ({_duracion_texto(dias)}). ")
        frases = {
            "nacimiento": "Permite calcular la edad y segmentar a las personas por grupo de edad o generación.",
            "vencimiento": "Permite anticipar vencimientos, gestionar renovaciones y medir cuánto falta para cada uno.",
            "alta": "Marca cuándo entró cada registro: sirve para medir altas por período y la antigüedad.",
            "cierre": "Marca cuándo termina o se entrega cada registro: junto con la fecha de inicio permite "
                      "medir duraciones y demoras.",
            "actualizacion": "Indica cuándo se modificó por última vez cada registro: permite saber qué tan "
                             "actualizada está la información.",
        }
        if semantica in frases:
            cuerpo = frases[semantica]
        else:
            base = "Es la fecha de la operación" if semantica == "operacion" else "Ubica cada registro en el tiempo"
            cuerpo = (f"{base}: permite analizar tendencia y estacionalidad y comparar períodos"
                      + (f" de {kpis}." if kpis else "."))
        return rango + cuerpo + _nota_nulos(p, tratamiento, "esos registros no se podrán ubicar en el tiempo.")

    if kind == "indicador":
        (a, pa), (b, pb) = (p["top"] + [("", 0.0), ("", 0.0)])[:2]
        reparto = f"«{a}» en {_pct(pa)} de los registros" + (f" y «{b}» en {_pct(pb)}" if b else "")
        if concepto is not None and concepto.clave == "resultado":
            texto = (f"Es el resultado que el negocio quiere medir o anticipar: {reparto}. Contra esta variable se "
                     "evalúa el desempeño y se explican las demás.")
        else:
            texto = (f"Separa los registros en dos grupos ({reparto}): sirve como filtro y para comparar "
                     f"{kpis or 'el comportamiento de los demás campos'} entre ambos.")
        return texto + _nota_nulos(p, tratamiento, nulos_lectura)

    if kind == "medida":
        uso = (concepto.medida if concepto and concepto.medida else
               f"Medida numérica de {nombre_q}: permite sumar, promediar y comparar entre registros, y detectar "
               "valores fuera de lo normal")
        rango = ""
        if "minimo" in p:
            rango = f" Va de {_num(p['minimo'])} a {_num(p['maximo'])} (promedio {_num(p['media'])})."
        if p.get("no_numericos"):
            rango += (f" {_num(p['no_numericos'])} {'valor no es numérico' if p['no_numericos'] == 1 else 'valores no son numéricos'}"
                      " y no entra en los cálculos.")
        atipicos = ""
        if p.get("atipicos", 0) >= 0.01:
            atipicos = (f" Cerca del {_pct(p['atipicos'])} de los valores se aleja mucho del resto y puede "
                        "distorsionar los promedios.")
        transformada = ""
        if str(tratamiento).startswith(TRATAMIENTOS_QUE_TRANSFORMAN):
            transformada = (f" Sus vacíos se trataron en la limpieza ({str(tratamiento)[0].lower()}"
                            f"{str(tratamiento)[1:]}), por lo que parte de sus valores es estimada.")
            return uso + "." + rango + atipicos + transformada + por_ejes
        return (uso + "." + rango + atipicos + por_ejes
                + _nota_nulos(p, tratamiento, "los totales y promedios solo reflejan las filas con dato."))

    if kind == "categoria":
        grupos = p["unicos"]
        lista = ", ".join(_comillas(v, 25) for v, _ in p["top"][:3])
        resto = " entre otros" if grupos > 3 else ""
        if concepto is not None and concepto.categoria:
            uso = concepto.categoria
        else:
            uso = "Segmenta los registros en grupos comparables"
        texto = f"{uso}. Hay {_num(grupos)} {'grupo' if grupos == 1 else 'grupos'} ({lista}{resto})"
        if p["top"] and p["top"][0][1] >= 0.5 and grupos > 1:
            texto += f"; {_comillas(p['top'][0][0], 25)} concentra {_pct(p['top'][0][1])} de las filas"
        texto += "."
        if kpis and not (concepto and concepto.clave == "tiempo_parte" and False):
            texto += f" Sirve para comparar {kpis} entre grupos."
        return texto + _nota_nulos(p, tratamiento, nulos_lectura)

    # texto
    largo = p.get("longitud_max", 0)
    if concepto is not None and concepto.clave == "texto_libre":
        texto = (f"Aporta contexto en palabras de cada registro ({_num(p['unicos'])} textos distintos, hasta "
                 f"{_num(largo)} caracteres): sirve para entender un caso puntual, no para sumar ni agrupar; "
                 "si se analiza, conviene homologar variantes de escritura.")
    elif concepto is not None and concepto.clave == "nombre":
        texto = (f"Etiqueta legible de cada registro ({_num(p['unicos'])} nombres distintos): permite reconocer "
                 "los casos en informes y tableros y detectar nombres repetidos o escritos de varias formas.")
    elif p["unicidad"] >= 0.99:
        texto = (f"Texto casi único por registro ({_num(p['unicos'])} valores en {_num(p['validos'])} filas, hasta "
                 f"{_num(largo)} caracteres): funciona como etiqueta o descripción individual de {nombre_q}.")
    else:
        texto = (f"Texto de {nombre_q} con {_num(p['unicos'])} valores distintos en {_num(p['validos'])} filas "
                 f"(hasta {_num(largo)} caracteres): aporta detalle para consultar cada registro, pero por su "
                 "variedad no sirve para agrupar sin antes normalizarlo.")
    return texto + _nota_nulos(p, tratamiento, nulos_lectura)


# --------------------------------------------------------------------------
# Modelo de datos
# --------------------------------------------------------------------------

def _agregacion(info: Dict) -> str:
    concepto, p = info["concepto"], info["p"]
    tokens = set(_tokens(info["nombre"]))
    if concepto is not None and concepto.clave == "dinero" and tokens & _PALABRAS_VALOR_UNITARIO:
        return "PROMEDIO (es un valor unitario; no se suma entre registros)"
    if concepto is not None and concepto.agregacion:
        return concepto.agregacion
    if p.get("enteros"):
        return "SUMA y PROMEDIO (valores enteros: conteo o cantidad; confirmar cuál es antes de totalizar)"
    return "PROMEDIO y SUMA (valores decimales; sumar solo si es un monto o cantidad acumulable)"


def _modelo(info: Dict, ctx: Dict) -> str:
    kind, p, concepto, nombre = info["kind"], info["p"], info["concepto"], info["nombre"]
    t = f"«{ctx['tabla']}»" if ctx["tabla"] else "la tabla"
    if kind == "metadato":
        return ("Fuera del modelo analítico: columna técnica de control; se conserva para auditoría y se oculta "
                "en los informes.")
    if kind == "vacio":
        return "Excluir del modelo: la columna no tiene datos."
    if kind == "constante":
        return "Excluir del modelo: tiene un único valor, no segmenta ni mide."
    if kind == "llave":
        cruce = ctx["cruces"].get(str(nombre))
        if cruce:
            return (f"Llave de unión: relaciona «{cruce['tabla_a']}».{cruce['col_a']} con "
                    f"«{cruce['tabla_b']}».{cruce['col_b']} (cruce {cruce['cruce']}); define la relación entre "
                    "ambas tablas del modelo.")
        entidad = _entidad_llave(_tokens(nombre))
        if p["unicidad"] >= 0.999 and not p["nulos"]:
            return (f"Llave primaria (PK) de {t}: identifica cada fila y otras tablas pueden referenciarla como "
                    "llave foránea (FK).")
        if p["unicidad"] >= 0.9 and not entidad:
            return (f"Llave primaria candidata (PK) de {t}: los valores repetidos o vacíos impiden declararla "
                    "como PK hasta depurarlos.")
        destino = f"la dimensión {entidad}" if entidad else "su tabla de referencia"
        return (f"Llave foránea (FK): se repite ({_num(p['unicos'])} valores en {_num(p['validos'])} filas), "
                f"relación de muchos a uno con {destino}.")
    clave = concepto.clave if concepto else ""
    if kind == "fecha":
        return ("Atributo de tiempo: se relaciona con una dimensión calendario (año, trimestre, mes, día) para "
                "analizar por período.")
    if kind == "medida":
        if info["clasif"] == CLASIFICACION_ATRIBUTO:
            return (f"Atributo numérico de {t}: se usa para segmentar en rangos o resumir con "
                    f"{_agregacion(info)}. No es una de las medidas principales.")
        return f"Medida de {t} ({ctx['tipo_tabla']}): se resume con {_agregacion(info)}."
    if kind == "indicador":
        return ("Atributo indicador (dos valores): se usa como filtro o segmentador; contarlo o calcular su "
                "porcentaje lo convierte en una medida.")
    if kind == "categoria":
        dimension = f"«{concepto.dimension}»" if concepto and concepto.dimension else f"descriptiva de {t}"
        return f"Atributo de dimensión {dimension}: agrupa y filtra las medidas ({_num(p['unicos'])} valores)."
    if kind == "contacto":
        return ("Atributo descriptivo sensible (dato personal): mantenerlo en una dimensión aparte con acceso "
                "restringido.")
    if kind == "coordenada":
        return ("Atributo geográfico: se marca como latitud o longitud (categoría de datos) para ubicar los "
                "registros en mapas.")
    return ("Atributo descriptivo (etiqueta): se muestra como detalle o ayuda emergente; no se agrega ni se usa "
            "para agrupar." if clave != "texto_libre" else
            "Atributo descriptivo de texto libre: se consulta como detalle del registro; no se agrega ni se usa "
            "para agrupar.")


# --------------------------------------------------------------------------
# Función pública
# --------------------------------------------------------------------------

def _vacia(serie: pd.Series) -> pd.Series:
    return serie.isna() | (serie.astype(str).str.strip().isin(["", "nan", "None", "<NA>"]))


def _serie(df: pd.DataFrame, posicion: int, campo) -> Optional[pd.Series]:
    if posicion < df.shape[1] and str(df.columns[posicion]) == str(campo):
        return df.iloc[:, posicion]
    if campo in df.columns:
        extraida = df[campo]
        return extraida.iloc[:, 0] if isinstance(extraida, pd.DataFrame) else extraida
    return None


def completar_campos_negocio(diccionario: pd.DataFrame, df: pd.DataFrame, nombre_tabla: str = "",
                             fuentes: Optional[Sequence[str]] = None,
                             llaves: Optional[Sequence[str]] = None,
                             cruces: Optional[Sequence[Dict]] = None,
                             origenes: Optional[Dict[str, str]] = None,
                             tokens=TOKENS_NULOS_BASE) -> pd.DataFrame:
    """Copia del diccionario con Origen, Justificación de negocio, Clasificación ejecutiva y Modelo de datos
    llenos en todas las filas donde la celda está vacía. Lo que la persona ya escribió no se toca.
    `nombre_tabla` y `fuentes`: nombre de la tabla y archivos/hojas de donde sale. `llaves`: columnas de unión
    (sin ellas se toman las de rol identificador o las ya clasificadas como llave). `cruces`: uniones hechas
    [{tabla_a, col_a, tabla_b, col_b, cruce}]. `origenes`: {columna: texto} de qué tabla viene cada campo."""
    if "Campo" not in diccionario.columns:
        return diccionario
    resultado = diccionario.copy()
    for columna in COLUMNAS_NEGOCIO:
        if columna not in resultado.columns:
            resultado[columna] = ""
        resultado[columna] = resultado[columna].astype(object)
    vacias = {c: _vacia(resultado[c]) for c in COLUMNAS_NEGOCIO}
    if not any(v.any() for v in vacias.values()):
        return resultado

    fuentes = [str(f) for f in (fuentes or []) if str(f).strip()]
    origenes = origenes or {}
    total = len(resultado)
    nombres = [str(c) for c in resultado["Campo"]]
    vecinas = {t for c in nombres for t in _tokens(c)}
    llaves_set = {str(c) for c in llaves} if llaves is not None else None
    mapa_cruces: Dict[str, Dict] = {}
    for c in cruces or []:
        mapa_cruces[str(c["col_a"])] = c
        mapa_cruces.setdefault(str(c["col_b"]), c)

    # ---- primera pasada: perfil, tipo de campo y clasificación de cada columna
    infos: List[Dict] = []
    for posicion, idx in enumerate(resultado.index):
        fila = resultado.loc[idx]
        campo = fila["Campo"]
        serie = _serie(df, posicion, campo)
        tipo = str(fila.get("Tipo de dato", "Texto"))
        rol = _ROL_POR_TEXTO.get(str(fila.get("Rol", "")), "texto")
        tratamiento = str(fila.get("Tratamiento de nulos", "") or "")
        if rol == "numerica" and tipo in ("Texto", "Categoría", "Texto (código)"):
            tipo = "Decimal"  # casi todo es número: unos pocos textos sueltos no la vuelven texto
        p = _perfil(serie, tipo, tokens) if serie is not None else {
            "n": 0, "validos": 0, "nulos": 0, "completitud": 1.0, "unicos": 0, "unicidad": 0.0, "top": []}
        concepto = concepto_de(campo, vecinas)
        clasif_previa = str(fila.get(COLUMNA_CLASIFICACION, "") or "").strip()
        if clasif_previa:
            es_llave = clasif_previa == CLASIFICACION_LLAVE
        elif llaves_set is not None:
            es_llave = str(campo) in llaves_set or str(campo) in mapa_cruces
        else:
            es_llave = rol == "id"
        kind = _tipo_campo(campo, rol, tipo, p, es_llave, concepto)
        clasif = clasif_previa or _clasificar(campo, kind, concepto, tratamiento, p)
        if clasif_previa and kind == "llave" and clasif_previa != CLASIFICACION_LLAVE:
            kind = _tipo_campo(campo, rol, tipo, p, False, concepto)
        infos.append({"idx": idx, "nombre": str(campo), "legible": str(campo), "rol": rol, "tipo": tipo,
                      "tratamiento": tratamiento, "p": p, "concepto": concepto, "kind": kind, "clasif": clasif,
                      "posicion": posicion + 1})

    # ---- contexto de la tabla: qué mide y por qué ejes se puede analizar
    kpis = [i for i in infos if i["clasif"] == CLASIFICACION_KPI and i["kind"] in ("medida", "indicador")]
    medidas = [i for i in infos if i["kind"] == "medida" and i["clasif"] != CLASIFICACION_ATRIBUTO]
    ejes_cand = [i for i in infos if i["kind"] == "categoria" and 2 <= i["p"]["unicos"] <= 30]
    prioridad = {"geografia": 0, "producto": 1, "organizacion": 2, "persona": 3, "proceso": 4, "canal": 5,
                 "demografia": 6}
    ejes_cand.sort(key=lambda i: prioridad.get(i["concepto"].clave if i["concepto"] else "", 9))
    fechas = [i for i in infos if i["kind"] == "fecha"]
    ejes = [f"«{i['legible']}»" for i in ejes_cand[:2]] + [f"«{i['legible']}»" for i in fechas[:1]]
    ctx_base = {
        "tabla": nombre_tabla,
        "kpis": _lista([f"«{i['legible']}»" for i in kpis[:2]]),
        "ejes": ("por " + _lista(ejes)) if ejes else "",
        "tipo_tabla": "tabla de hechos" if medidas else "tabla de dimensión",
        "cruces": mapa_cruces,
    }

    # ---- segunda pasada: texto de cada celda vacía
    for info in infos:
        idx, nombre = info["idx"], info["nombre"]
        ctx = dict(ctx_base)
        if info["clasif"] == CLASIFICACION_KPI:  # no se lista a sí mismo como «el otro KPI»
            otros = [i for i in kpis if i["nombre"] != nombre]
            ctx["kpis"] = _lista([f"«{i['legible']}»" for i in otros[:2]])
        if vacias[COLUMNA_CLASIFICACION].loc[idx]:
            resultado.at[idx, COLUMNA_CLASIFICACION] = info["clasif"]
        if vacias[COLUMNA_ORIGEN].loc[idx]:
            resultado.at[idx, COLUMNA_ORIGEN] = _origen(
                nombre, info["posicion"], total, info["kind"], info["tratamiento"], nombre_tabla, fuentes,
                str(origenes.get(nombre, "") or ""), mapa_cruces.get(nombre), cruces or ())
        if vacias[COLUMNA_JUSTIFICACION].loc[idx]:
            resultado.at[idx, COLUMNA_JUSTIFICACION] = _justificar(info, ctx)
        if vacias[COLUMNA_MODELO].loc[idx]:
            resultado.at[idx, COLUMNA_MODELO] = _modelo(info, ctx)
    return resultado

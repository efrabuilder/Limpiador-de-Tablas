# -*- coding: utf-8 -*-
"""
descripciones.py
================
Redacta la «Descripción» de cada campo del diccionario de datos, para que nunca
quede vacía cuando una tabla se limpia y se analiza.

La descripción sale de lo que se puede saber sin adivinar el negocio:
    1. el nombre del campo (glosario de términos frecuentes en español e inglés:
       identificadores, fechas, montos, estados, puntajes, territorio, etc.),
    2. su rol y tipo de dato (cuando el nombre no dice nada),
    3. si es una copia o una versión normalizada de otro campo,
    4. de qué tabla viene y qué se hizo con sus vacíos (si se sabe).

Es un borrador sensato: la persona puede reescribirlo en el diccionario y lo
que escriba se respeta (solo se rellenan las celdas vacías).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Optional, Tuple

import pandas as pd

# --------------------------------------------------------------------------
# Nombres
# --------------------------------------------------------------------------


def _sin_acentos(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def normalizar_nombre(nombre) -> str:
    """'MontoPrima_Anual' -> 'monto prima anual' (minúsculas, sin acentos, palabras sueltas)."""
    texto = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(nombre))  # camelCase
    texto = _sin_acentos(texto).lower()
    texto = re.sub(r"[^a-z0-9]+", " ", texto)
    return texto.strip()


def nombre_legible(nombre) -> str:
    """Nombre de la columna en palabras: 'monto_prima' -> 'monto prima'."""
    return normalizar_nombre(nombre) or str(nombre)


def _primera_mayuscula(texto: str) -> str:
    return texto[:1].upper() + texto[1:] if texto else texto


# --------------------------------------------------------------------------
# Glosario: (patrón sobre el nombre normalizado, plantilla). El primero que coincide gana,
# por eso van primero los más específicos. {n} = nombre legible del campo.
# --------------------------------------------------------------------------

_P = r"\b(?:{})\b"


def _p(*palabras: str) -> "re.Pattern":
    return re.compile(_P.format("|".join(palabras)))


_ID = ("id", "ids", "codigo", "cod", "clave", "key", "num", "numero", "nro", "no", "folio", "uuid")

_GLOSARIO: List[Tuple["re.Pattern", str]] = [
    # --- territorio
    (_p("codigo postal", "cp", "zip", "zipcode", "zip code", "postal", "postal code"),
     "Código postal. Llave territorial para enlazar con catálogos geográficos; se conserva como texto "
     "para no perder los ceros a la izquierda."),
    (_p("latitud", "latitude", "lat"), "Latitud en grados decimales."),
    (_p("longitud", "longitude", "lon", "lng"), "Longitud en grados decimales."),
    (_p("densidad", "densidad poblacional"),
     "Densidad poblacional del territorio. Al consolidar registros se promedia en lugar de sumarse; "
     "es contexto demográfico, no un indicador del negocio."),
    (_p("poblacion", "habitantes", "population"),
     "Población del territorio. Al consolidar por código postal se suma; es contexto demográfico, "
     "no un indicador del negocio."),
    (_p("pais", "country", "nacionalidad"), "País del registro; conviene homologar sus nombres entre fuentes."),
    (_p("provincia", "region", "departamento", "canton", "distrito", "municipio", "comuna", "barrio"),
     "Unidad territorial del registro ({n}); debe coincidir con el catálogo territorial para poder cruzar."),
    (_p("ciudad", "localidad", "city", "pueblo"), "Ciudad o localidad del registro."),
    (_p("direccion", "domicilio", "address", "calle"), "Dirección del registro (texto libre)."),
    # --- personas
    (_p("edad", "age"), "Edad en años."),
    (_p("sexo", "genero", "gender", "sex"), "Sexo o género registrado de la persona."),
    (_p("apellido", "apellidos", "lastname", "surname"), "Apellido de la persona."),
    (_p("nombre", "nombres", "name", "firstname"), "Nombre de la persona o entidad; texto libre, no sirve como llave."),
    (_p("email", "correo", "mail"), "Correo electrónico."),
    (_p("telefono", "celular", "movil", "phone", "tel"), "Número de teléfono."),
    # --- seguros y reclamos
    (_p("siniestralidad"),
     "Indicador de siniestralidad. Debe confirmarse su fórmula y su período antes de usarlo en decisiones."),
    (_p("liquidacion", "liquidado", "indemnizacion", "pagado", "pago reclamo"),
     "Monto de liquidación registrado. Puede apoyar un indicador de siniestralidad pagada cuando su "
     "definición y período estén confirmados."),
    (_p("solicitado", "reclamado", "requested"),
     "Importe solicitado en el reclamo. No equivale al importe aprobado y no deben combinarse como un único costo."),
    (_p("aprobado", "autorizado", "approved"),
     "Importe aprobado en el reclamo. No equivale al importe solicitado y no deben combinarse como un único costo."),
    (_p("prima", "premium"),
     "Monto de prima. Se necesita el período de pago para construir un denominador comparable "
     "durante un período definido."),
    (_p("periodicidad", "frecuencia pago", "forma pago", "frecuencia de pago"),
     "Periodicidad con que se paga la prima (mensual, anual, etc.); permite llevar los montos a un período comparable."),
    (_p("suma asegurada", "cobertura", "coverage", "deducible", "copago"),
     "Condición de cobertura de la póliza ({n}); define hasta dónde responde el seguro."),
    (_p("vigencia"), "Vigencia de la póliza o del contrato."),
    (_p("reclamo", "reclamos", "siniestro", "siniestros", "claim", "claims"),
     "Dato de los reclamos o siniestros asociados al registro ({n}); confirmar su definición antes de usarlo como indicador."),
    (_p("poliza", "polizas", "policy"),
     "Dato de la póliza ({n})."),
    # --- calidad, estado y puntajes
    (_p("puntaje", "puntuacion", "score", "rating", "calificacion", "nota"),
     "Puntaje de {n}. Su interpretación requiere conocer la escala de origen."),
    (_p("estado", "estatus", "status", "situacion"),
     "Estado o situación registrada ({n}). Debe homologarse entre fuentes (nombres e idioma) antes de cruzar o comparar."),
    (_p("tipo", "type", "clase", "categoria", "category", "segmento", "canal", "modalidad", "plan"),
     "Categoría que clasifica el registro ({n})."),
    (_p("motivo", "causa", "razon", "reason"), "Motivo o causa registrada ({n})."),
    (_p("observacion", "observaciones", "comentario", "comentarios", "notas", "descripcion", "detalle"),
     "Texto libre con observaciones o detalle del registro."),
    # --- comercial y financiero
    (_p("moneda", "currency", "divisa"), "Moneda en que están expresados los montos."),
    (_p("descuento", "discount"), "Descuento aplicado ({n})."),
    (_p("impuesto", "iva", "tax"), "Impuesto aplicado ({n})."),
    (_p("precio", "price", "tarifa"), "Precio o tarifa unitaria ({n})."),
    (_p("costo", "coste", "cost", "gasto"), "Costo o gasto registrado ({n})."),
    (_p("ingreso", "ingresos", "salario", "sueldo", "income", "salary"), "Ingreso o remuneración ({n})."),
    (_p("venta", "ventas", "sales", "facturacion", "facturado"), "Valor vendido o facturado ({n})."),
    (_p("monto", "importe", "valor", "amount", "saldo"), "Monto en moneda del campo {n}; requiere un período definido para compararse."),
    (_p("producto", "articulo", "item", "sku"), "Producto o artículo del registro ({n})."),
    (_p("sucursal", "tienda", "oficina", "agencia"), "Sucursal o punto de atención del registro."),
    (_p("cliente", "asegurado", "customer", "client", "socio", "usuario", "user"),
     "Cliente o persona asociada al registro ({n})."),
    # --- medidas
    (_p("porcentaje", "pct", "percent", "tasa", "ratio", "proporcion", "indice"),
     "Proporción o tasa ({n}); confirmar si está expresada en porcentaje o en fracción."),
    (_p("antiguedad", "duracion", "tiempo", "plazo", "dias", "meses", "anios", "anos"),
     "Duración o antigüedad ({n}); confirmar su unidad de medida."),
    (_p("cantidad", "unidades", "qty", "stock"), "Cantidad de unidades ({n})."),
    (_p("riesgo", "risk"), "Nivel o medida de riesgo ({n}); confirmar su escala de origen."),
]

# Fechas por tipo de evento.
_FECHAS: List[Tuple["re.Pattern", str]] = [
    (_p("nacimiento", "nac", "birth"), "Fecha de nacimiento."),
    (_p("inicio", "alta", "emision", "efectiva", "start"), "Fecha de inicio o de alta del registro ({n})."),
    (_p("fin", "vencimiento", "baja", "cancelacion", "termino", "end"), "Fecha de fin, vencimiento o baja ({n})."),
    (_p("pago", "payment"), "Fecha de pago."),
    (_p("reclamo", "siniestro", "claim", "evento", "ocurrencia"), "Fecha en que ocurrió o se reportó el evento ({n})."),
    (_p("venta", "compra", "pedido", "orden", "factura"), "Fecha de la transacción ({n})."),
    (_p("registro", "creacion", "ingreso", "carga"), "Fecha en que se registró el dato."),
    (_p("actualizacion", "modificacion", "update"), "Fecha de la última actualización."),
]

_PERIODICIDAD = _p("periodicidad", "frecuencia", "forma pago")

# Palabras que convierten un campo en una versión de otro.
_ORIGINAL = _p("original", "previo", "anterior", "antes", "bruto", "raw", "sin limpiar", "reasignacion")
_NORMALIZADO = _p("norm", "normalizado", "normalizada", "estandarizado", "homologado", "limpio", "limpia", "clean")

# Agregaciones: (patrón, frase). Se anteponen a la descripción del campo base.
_AGREGACIONES: List[Tuple["re.Pattern", str]] = [
    (_p("max", "maximo", "maxima"), "Valor máximo por registro de {base}."),
    (_p("min", "minimo", "minima"), "Valor mínimo por registro de {base}."),
    (_p("prom", "promedio", "avg", "mean", "media"), "Promedio por registro de {base}."),
    (_p("suma", "sum", "acumulado"), "Suma por registro de {base}."),
]

_CONTEO_FILAS = re.compile(r"\b(?:n|num|numero|cantidad|conteo|count|total)\b.*\b(?:filas|registros|rows|origen)\b|"
                           r"\b(?:filas|registros|rows)\b.*\b(?:origen|fuente|source)\b")
_CONTEO = _p("n", "num", "numero", "nro", "cantidad", "conteo", "count", "total")

_DESCRIPCION_POR_TIPO = {
    "Entero": "Valor entero de {n} por registro.",
    "Decimal": "Valor numérico (con decimales) de {n} por registro.",
    "Fecha": "Fecha de {n}.",
    "Sí/No": "Indicador Sí/No de {n}.",
    "Categoría": "Categoría de {n} (pocos valores distintos).",
    "Texto (código)": "Código de {n}; se conserva como texto para no perder ceros a la izquierda.",
    "Texto": "Texto de {n}.",
    "Vacío": "Campo {n} sin datos en esta tabla.",
}


def _glosario(nombre_norm: str, tabla=None) -> Optional[str]:
    for patron, plantilla in (tabla or _GLOSARIO):
        if patron.search(nombre_norm):
            return plantilla
    return None


def _es_solo_identificador(nombre_norm: str, extra: Tuple[str, ...]) -> bool:
    """True si todas las palabras del nombre son marcas de identificador o `extra`."""
    palabras = nombre_norm.split()
    return bool(palabras) and all(p in _ID or p in extra for p in palabras) and any(p in _ID for p in palabras)


def _base_sin_marca(nombre_norm: str, patron: "re.Pattern") -> str:
    return re.sub(r"\s+", " ", patron.sub(" ", nombre_norm)).strip()


def describir_campo(col, rol: str = "texto", tipo: str = "Texto", tratamiento: str = "",
                    origen: str = "", es_llave: bool = False) -> str:
    """Descripción redactada de un campo a partir de su nombre, rol y tipo.
    Siempre devuelve texto (nunca vacío)."""
    nombre = str(col)
    norm = normalizar_nombre(nombre)
    n = nombre_legible(nombre)
    texto: Optional[str] = None

    # Marcas y copias internas de la limpieza.
    if nombre.startswith("_revisar_calidad"):
        return ("Marca interna de la limpieza: lista los hallazgos que se dejaron solo "
                "marcados en esa fila (vacío si no hay ninguno).")
    if nombre == "_merge":
        return "Marca interna del cruce de tablas: indica si el registro viene de una o de ambas fuentes."
    if nombre.endswith("_original"):
        base = nombre[: -len("_original")]
        return (f"Copia de respaldo del valor de «{base}» antes de la limpieza; "
                "se conserva por trazabilidad.")

    # Rol especial (el nombre no hace falta).
    if rol == "email":
        texto = "Correo electrónico."
    elif rol == "telefono":
        texto = "Número de teléfono."
    elif rol == "coordenada":
        texto = _glosario(norm, [_GLOSARIO[1], _GLOSARIO[2]]) or "Coordenada geográfica en grados decimales."

    # Versión original o normalizada de otro campo.
    if texto is None:
        version = None
        if _ORIGINAL.search(norm):
            version, patron = "original", _ORIGINAL
        elif _NORMALIZADO.search(norm):
            version, patron = "normalizado", _NORMALIZADO
        if version:
            base = _base_sin_marca(norm, patron) or n
            base_txt = _glosario(base)
            if version == "original":
                texto = (f"Valor de «{base}» previo a su transformación o reasignación; "
                         "se conserva para mantener trazabilidad.")
            else:
                texto = (f"Valor de «{base}» normalizado (formato y nombres homologados) "
                         "para poder cruzarlo con otras fuentes.")
            if base_txt and base_txt.startswith(("Código postal", "Estado")):
                texto += " " + base_txt.split(". ", 1)[0] + "."

    # Conteo de filas de origen consolidadas.
    if texto is None and _CONTEO_FILAS.search(norm):
        texto = ("Cuenta las filas de origen consolidadas en cada registro. No equivale todavía a un "
                 "conteo validado de eventos reales.")

    # Agregación sobre otro campo (max_puntaje, prom_edad, ...).
    if texto is None:
        for patron, frase in _AGREGACIONES:
            if patron.search(norm):
                base = _base_sin_marca(norm, patron)
                if base:
                    detalle = _glosario(base)
                    texto = frase.format(base=f"«{base}»")
                    if detalle and detalle.startswith("Puntaje"):
                        texto += " Su interpretación requiere conocer las escalas de origen."
                    break

    # Identificadores.
    if texto is None and (es_llave or rol == "id" or _es_solo_identificador(norm, ("poliza", "policy", "cliente", "asegurado"))):
        if re.search(r"\b(poliza|polizas|policy)\b", norm):
            texto = ("Identifica una póliza y permite consolidar sus registros. "
                     "Evita contar repetidamente la misma póliza.")
        elif re.search(r"\b(cliente|asegurado|customer|client|socio|usuario|user)\b", norm):
            texto = "Identifica a un cliente y permite consolidar sus registros sin contarlo más de una vez."
        elif _glosario(norm, [_GLOSARIO[0]]):
            texto = _glosario(norm, [_GLOSARIO[0]])
        else:
            base = _base_sin_marca(norm, _p(*_ID)) or n
            texto = (f"Identificador de {base}. Sirve para enlazar registros entre tablas "
                     "y para detectar duplicados.")

    # Fechas.
    if texto is None and (rol == "fecha" or tipo == "Fecha") and not _PERIODICIDAD.search(norm):
        texto = _glosario(norm, _FECHAS) or "Fecha de {n}."

    # Conteos por registro (n_reclamos, cantidad_visitas...).
    if texto is None and _CONTEO.search(norm) and tipo in ("Entero", "Decimal"):
        base = _base_sin_marca(norm, _CONTEO)
        if base:
            texto = f"Cantidad de {base} por registro."

    # Glosario general por nombre.
    if texto is None:
        texto = _glosario(norm)

    # Por tipo de dato.
    if texto is None:
        texto = _DESCRIPCION_POR_TIPO.get(tipo, "Dato de {n}.")

    texto = _primera_mayuscula(texto.format(n=f"«{n}»") if "{n}" in texto else texto)
    if texto and texto[-1] not in ".!?":
        texto += "."

    extras = []
    if origen:
        extras.append(f"Viene de la tabla «{origen}».")
    if tratamiento and tratamiento.startswith(("Rellenados", "Celdas vacías convertidas", "Se eliminaron")):
        extras.append(f"Transformado en la limpieza: {tratamiento[0].lower() + tratamiento[1:]}.")
    return " ".join([texto] + extras)


def completar_descripciones(diccionario: pd.DataFrame, origenes: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    """Copia del diccionario con la «Descripción» redactada en las filas donde está vacía.
    Lo que la persona ya escribió no se toca."""
    if "Descripción" not in diccionario.columns or "Campo" not in diccionario.columns:
        return diccionario
    resultado = diccionario.copy()
    vacias = resultado["Descripción"].fillna("").astype(str).str.strip() == ""
    if not vacias.any():
        return resultado
    roles_inversos = {"identificador / llave": "id", "coordenada": "coordenada", "email": "email",
                      "teléfono": "telefono", "fecha": "fecha", "número": "numerica", "texto": "texto"}
    for idx in resultado.index[vacias]:
        fila = resultado.loc[idx]
        campo = fila["Campo"]
        origen = (origenes or {}).get(campo, "") or str(fila.get("Origen", "") or "")
        clasif = str(fila.get("Clasificación ejecutiva", "") or "")
        resultado.at[idx, "Descripción"] = describir_campo(
            campo, roles_inversos.get(str(fila.get("Rol", "")), "texto"),
            str(fila.get("Tipo de dato", "Texto")), str(fila.get("Tratamiento de nulos", "") or ""),
            origen, es_llave=clasif.startswith("Llave"))
    return resultado

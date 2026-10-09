# -*- coding: utf-8 -*-
"""
descripciones.py
================
Redacta la «Descripción» de cada campo del diccionario de datos, para que nunca
quede vacía cuando una tabla se limpia y se analiza.

La descripción sale de lo que se puede saber sin adivinar el negocio, en este orden:
    1. el nombre del campo, entendido de forma flexible:
         - camelCase, snake_case, números pegados (apellido1, direccion_2),
         - abreviaturas comunes (fec, cant, mto, cli, dir, tel, vta...),
         - plurales (ventas, clientes, ordenes),
         - errores de tipeo leves (direcion, telefon, cantidd),
         - nombres pegados sin separador (fechaventa, totalventas),
         - español e inglés (customer_id, order_date, created_at),
    2. el contenido de la columna cuando el nombre no dice nada (correo, URL, Sí/No,
       porcentaje, hora, identificador único, categorías con sus valores...),
    3. su rol y tipo de dato,
    4. si es una copia o una versión normalizada de otro campo,
    5. de qué tabla viene y qué se hizo con sus vacíos (si se sabe).

Es un borrador sensato: la persona puede reescribirlo en el diccionario y lo
que escriba se respeta (solo se rellenan las celdas vacías).
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from functools import lru_cache
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import pandas as pd

# --------------------------------------------------------------------------
# Nombres
# --------------------------------------------------------------------------


def _sin_acentos(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def normalizar_nombre(nombre) -> str:
    """'MontoPrima_Anual' -> 'monto prima anual'; 'apellido1' -> 'apellido 1'
    (minúsculas, sin acentos, palabras sueltas, letras y números separados)."""
    texto = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(nombre))  # camelCase
    texto = _sin_acentos(texto).lower()
    texto = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", texto)  # apellido1 -> apellido 1
    texto = re.sub(r"[^a-z0-9]+", " ", texto)
    return texto.strip()


def nombre_legible(nombre) -> str:
    """Nombre de la columna en palabras: 'monto_prima' -> 'monto prima'."""
    return normalizar_nombre(nombre) or str(nombre)


def _primera_mayuscula(texto: str) -> str:
    return texto[:1].upper() + texto[1:] if texto else texto


# --------------------------------------------------------------------------
# Abreviaturas y plurales
# --------------------------------------------------------------------------

# Palabra abreviada -> palabra completa (se aplica sobre cada palabra del nombre).
_ABREVIATURAS: Dict[str, str] = {
    # tiempo
    "fec": "fecha", "fch": "fecha", "dt": "fecha", "ts": "timestamp", "tstamp": "timestamp",
    "hrs": "horas", "yr": "anio", "yrs": "anio", "qtr": "trimestre", "wk": "semana", "mth": "mes",
    # cantidades y montos
    "cant": "cantidad", "cantd": "cantidad", "cntd": "cantidad", "qtd": "cantidad", "qty": "cantidad",
    "mto": "monto", "mnt": "monto", "amt": "monto", "imp": "importe", "tot": "total", "ttl": "total",
    "porc": "porcentaje", "prc": "porcentaje", "pct": "porcentaje", "uni": "unidades", "und": "unidades",
    "unid": "unidades", "prec": "precio", "pvp": "precio", "gto": "gasto", "stk": "stock",
    "vta": "venta", "vtas": "ventas", "cta": "cuenta", "fact": "factura", "ped": "pedido",
    # personas y organizaciones
    "cli": "cliente", "clte": "cliente", "cte": "cliente", "cust": "cliente",
    "emp": "empleado", "empl": "empleado", "usr": "usuario", "usu": "usuario",
    "nom": "nombre", "nomb": "nombre", "ape": "apellido", "apell": "apellido", "dob": "nacimiento",
    "suc": "sucursal", "dpto": "departamento", "depto": "departamento", "dep": "departamento",
    "dept": "departamento", "gte": "gerente", "resp": "responsable", "rep": "representante",
    "prod": "producto", "prd": "producto", "cat": "categoria", "categ": "categoria",
    "subcat": "subcategoria", "doc": "documento",
    # contacto y territorio
    "dir": "direccion", "addr": "direccion", "tel": "telefono", "telf": "telefono", "tlf": "telefono",
    "cel": "celular", "ext": "extension", "prov": "provincia", "pob": "poblacion", "ctry": "pais",
    # texto
    "desc": "descripcion", "descr": "descripcion", "dsc": "descripcion",
    "obs": "observacion", "observ": "observacion", "coment": "comentario", "comm": "comentario",
    "pwd": "contrasena", "passwd": "contrasena", "pass": "contrasena", "password": "contrasena",
    "sts": "status", "stat": "status",
}

# Si «desc» viene con alguna de estas palabras se entiende como descuento y no como descripción.
_CONTEXTO_DESCUENTO = {"porcentaje", "monto", "aplicado", "tasa", "importe", "total", "promo", "valor"}


def _singular_token(t: str) -> str:
    """'ventas' -> 'venta', 'ordenes' -> 'orden', 'regiones' -> 'region', 'ciudades' -> 'ciudad'."""
    if len(t) <= 3 or t.isdigit():
        return t
    for sufijo, reemplazo in (("ciones", "cion"), ("siones", "sion"), ("enes", "en"), ("ones", "on"),
                              ("ades", "ad"), ("ises", "is"), ("eses", "es"), ("ores", "or"),
                              ("ales", "al"), ("eles", "el")):
        if t.endswith(sufijo):
            return t[: -len(sufijo)] + reemplazo
    if t.endswith("s") and not t.endswith(("ss", "is", "us")):
        return t[:-1]
    return t


def _expandir_abreviaturas(tokens: List[str]) -> List[str]:
    salida: List[str] = []
    contexto = set(tokens)
    for t in tokens:
        if t == "desc" and contexto & _CONTEXTO_DESCUENTO:
            salida.append("descuento")
        elif t in _ABREVIATURAS:
            salida.extend(_ABREVIATURAS[t].split())
        else:
            salida.append(t)
    return salida


# --------------------------------------------------------------------------
# Glosario: (patrón sobre el nombre normalizado, plantilla). El primero que coincide gana,
# por eso van primero los más específicos. {n} = nombre legible del campo.
# --------------------------------------------------------------------------


class _Patron:
    """Conjunto de palabras (o frases) que activan una entrada del glosario."""

    __slots__ = ("palabras", "_re")

    def __init__(self, *palabras: str):
        self.palabras = tuple(palabras)
        self._re = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(p) for p in palabras))

    def search(self, texto: str):
        return self._re.search(texto)

    def en_compacto(self, compacto: str) -> bool:
        """Para nombres pegados sin separador ('fechaventa'): solo palabras largas y sin espacios."""
        return any(len(p) >= 5 and " " not in p and p in compacto for p in self.palabras)


def _p(*palabras: str) -> _Patron:
    return _Patron(*palabras)


_ID_FUERTE = ("id", "ids", "codigo", "cod", "clave", "key", "uuid", "guid", "folio", "cve", "pk", "fk", "idx")
_ID_NUMERO = ("num", "numero", "nro", "no", "consecutivo", "correlativo", "serie", "serial", "ref", "referencia")
_ID = _ID_FUERTE + _ID_NUMERO

_TRACKING = _p("tracking", "guia", "rastreo")
_POSTAL = _p("codigo postal", "cp", "zip", "postal")
_DOCUMENTO_IDENTIDAD = _p("cedula", "dni", "pasaporte", "rut", "nit", "ruc", "curp", "rfc", "ssn", "nif",
                          "identificacion", "documento identidad", "documento identificacion", "passport")

# Entidades del negocio: (patrón, sustantivo). Sirven para «Identificador de cliente» y para
# aclarar a qué se refiere un campo («nombre_cliente» -> «Corresponde al cliente»).
_ENTIDADES: List[Tuple[_Patron, str]] = [
    (_p("cliente", "customer", "client", "comprador", "consumidor", "asegurado", "socio"), "cliente"),
    (_p("empleado", "employee", "trabajador", "colaborador", "staff", "vendedor", "representante",
        "gerente", "jefe", "supervisor", "asesor", "agente", "responsable", "manager"), "empleado"),
    (_p("usuario", "user"), "usuario"),
    (_p("paciente", "patient"), "paciente"),
    (_p("alumno", "estudiante", "student"), "estudiante"),
    (_p("docente", "profesor", "teacher", "instructor"), "docente"),
    (_p("proveedor", "supplier", "vendor", "fabricante", "manufacturer"), "proveedor"),
    (_p("producto", "articulo", "item", "sku", "mercaderia"), "producto"),
    (_p("sucursal", "tienda", "store", "oficina", "agencia", "branch", "almacen", "bodega", "warehouse"),
     "sucursal"),
    (_p("pedido", "orden", "order", "compra", "purchase"), "pedido"),
    (_p("factura", "invoice", "boleta", "recibo", "comprobante", "ticket"), "factura"),
    (_p("venta", "sale"), "venta"),
    (_p("pago", "payment"), "pago"),
    (_p("transaccion", "transaction"), "transacción"),
    (_p("cuenta", "account"), "cuenta"),
    (_p("tarjeta", "card"), "tarjeta"),
    (_p("poliza", "policy", "contrato", "contract"), "póliza o contrato"),
    (_p("reclamo", "siniestro", "claim"), "reclamo"),
    (_p("envio", "shipment", "entrega", "delivery", "despacho"), "envío"),
    (_p("curso", "asignatura", "materia", "course"), "curso"),
    (_p("campana", "campaign"), "campaña"),
    (_p("reserva", "booking", "cita", "appointment", "vuelo", "flight"), "reserva o cita"),
    (_p("lote", "batch"), "lote"),
    (_p("caso", "expediente", "case"), "caso"),
]
_FEMENINOS = {"sucursal", "factura", "venta", "cuenta", "tarjeta", "póliza o contrato", "campaña",
              "reserva o cita", "transacción"}

_GLOSARIO: List[Tuple[_Patron, str]] = [
    # --- contraseñas y datos sensibles (primero, para no confundirlos con claves de enlace)
    (_p("contrasena", "clave acceso", "secret", "secreto"),
     "Credencial de acceso. No debería viajar en una tabla de análisis; conviene excluirla."),
    (_p("token", "api key", "apikey", "hash"),
     "Token o huella técnica del registro; no tiene significado de negocio y no debe compartirse."),
    (_DOCUMENTO_IDENTIDAD,
     "Número de documento de identificación de la persona. Es un dato personal: tratarlo con confidencialidad "
     "y conservarlo como texto."),
    (_p("numero tarjeta", "card number", "iban", "swift", "cvv"),
     "Dato bancario o de tarjeta. Es información financiera sensible: debe enmascararse antes de compartir."),
    # --- territorio
    (_p("zona horaria", "timezone", "time zone", "huso horario"), "Zona horaria del registro."),
    (_p("idioma", "language", "lenguaje", "lang", "locale"), "Idioma del registro."),
    (_p("ip", "direccion ip", "ip address"), "Dirección IP desde la que se originó el registro."),
    (_p("url", "link", "enlace", "website", "sitio web", "pagina web", "web", "dominio", "domain"),
     "Dirección web (URL o dominio) asociada al registro."),
    (_p("codigo postal", "cp", "zip", "zipcode", "zip code", "postal", "postal code"),
     "Código postal. Llave territorial para enlazar con catálogos geográficos; se conserva como texto "
     "para no perder los ceros a la izquierda."),
    (_p("latitud", "latitude", "lat"), "Latitud en grados decimales."),
    (_p("longitud", "longitude", "lon", "lng", "long"), "Longitud en grados decimales."),
    (_p("altitud", "elevacion", "altitude", "elevation"), "Altitud sobre el nivel del mar; confirmar si está en metros."),
    (_p("densidad", "densidad poblacional"),
     "Densidad poblacional del territorio. Al consolidar registros se promedia en lugar de sumarse; "
     "es contexto demográfico, no un indicador del negocio."),
    (_p("poblacion", "habitantes", "population"),
     "Población del territorio. Al consolidar por código postal se suma; es contexto demográfico, "
     "no un indicador del negocio."),
    (_p("pais", "country", "nacionalidad", "nation"), "País del registro; conviene homologar sus nombres entre fuentes."),
    (_p("continente", "continent", "hemisferio"), "Continente del registro."),
    (_p("departamento", "department"),
     "Departamento del registro ({n}); puede ser un área de la organización o una división territorial, "
     "confirmar cuál."),
    (_p("provincia", "region", "canton", "distrito", "municipio", "comuna", "barrio", "zona", "sector",
        "colonia", "urbanizacion", "vecindario", "district", "county", "province", "neighborhood", "parroquia"),
     "Unidad territorial del registro ({n}); debe coincidir con el catálogo territorial para poder cruzar."),
    (_p("ciudad", "localidad", "city", "pueblo", "town", "poblado"), "Ciudad o localidad del registro."),
    (_p("direccion", "domicilio", "address", "calle", "avenida", "linea direccion", "street", "residencia",
        "ubicacion", "location"),
     "Dirección o ubicación del registro (texto libre)."),
    # --- personas
    (_p("edad", "age"), "Edad en años."),
    (_p("estado civil", "marital status", "civil"), "Estado civil de la persona."),
    (_p("sexo", "genero", "gender", "sex"), "Sexo o género registrado de la persona."),
    (_p("etnia", "raza", "religion", "ethnicity", "race"),
     "Dato demográfico sensible ({n}); usarlo solo con base legal y un propósito claro."),
    (_p("hijos", "dependientes", "children", "dependents"), "Cantidad de hijos o dependientes de la persona."),
    (_p("escolaridad", "nivel educativo", "educacion", "education", "grado academico", "titulo academico"),
     "Nivel educativo de la persona."),
    (_p("puesto", "cargo", "ocupacion", "profesion", "job", "occupation", "position", "rol", "role", "title"),
     "Puesto, cargo u ocupación de la persona ({n})."),
    (_p("empresa", "compania", "company", "organizacion", "institucion", "razon social", "employer"),
     "Empresa u organización asociada al registro ({n})."),
    (_p("apellido", "apellidos", "lastname", "surname", "last name"), "Apellido de la persona."),
    (_p("nombre", "name", "firstname", "first name", "denominacion", "titulo"),
     "Nombre o denominación del registro; texto libre, no sirve como llave."),
    (_p("email", "correo", "mail", "e mail"), "Correo electrónico."),
    (_p("fax"), "Número de fax."),
    (_p("extension", "anexo"), "Extensión telefónica interna."),
    (_p("telefono", "celular", "movil", "phone", "tel", "whatsapp", "mobile", "cell"), "Número de teléfono."),
    (_p("usuario red", "username", "login", "nick", "nickname", "alias", "handle"),
     "Nombre de usuario o alias con el que se identifica la persona en el sistema."),
    # --- seguros y reclamos
    (_p("siniestralidad"),
     "Indicador de siniestralidad. Debe confirmarse su fórmula y su período antes de usarlo en decisiones."),
    (_p("liquidacion", "liquidado", "indemnizacion", "pago reclamo"),
     "Monto de liquidación registrado. Puede apoyar un indicador de siniestralidad pagada cuando su "
     "definición y período estén confirmados."),
    (_p("solicitado", "reclamado", "requested"),
     "Importe solicitado en el reclamo. No equivale al importe aprobado y no deben combinarse como un único costo."),
    (_p("aprobado", "autorizado", "approved"),
     "Importe aprobado en el reclamo. No equivale al importe solicitado y no deben combinarse como un único costo."),
    (_p("periodicidad", "frecuencia pago", "frecuencia de pago"),
     "Periodicidad con que se paga (mensual, anual, etc.); permite llevar los montos a un período comparable."),
    (_p("suma asegurada", "cobertura", "coverage", "deducible", "copago"),
     "Condición de cobertura de la póliza ({n}); define hasta dónde responde el seguro."),
    (_p("prima", "premium"),
     "Monto de prima. Se necesita el período de pago para construir un denominador comparable "
     "durante un período definido."),
    (_p("vigencia"), "Vigencia de la póliza o del contrato."),
    (_p("reclamo", "siniestro", "claim"),
     "Dato de los reclamos o siniestros asociados al registro ({n}); confirmar su definición antes de usarlo como indicador."),
    (_p("poliza", "policy"), "Dato de la póliza ({n})."),
    # --- salud (solo se nombra el tipo de dato; no se interpreta)
    (_p("diagnostico", "diagnosis", "tratamiento", "treatment", "medicamento", "medication", "dosis", "sintoma",
        "enfermedad", "padecimiento"),
     "Dato clínico ({n}). Es información sensible: restringir su acceso."),
    # --- banca y crédito
    (_p("limite credito", "credito", "credit", "linea credito"), "Crédito o límite de crédito ({n}); confirmar la moneda."),
    (_p("prestamo", "loan", "deuda", "debt", "mora", "adeudo", "morosidad"), "Dato de deuda o préstamo ({n}); confirmar la moneda."),
    (_p("interes", "interest", "apr"), "Interés aplicado ({n}); confirmar si es una tasa o un monto."),
    (_p("cuota", "abono", "installment", "mensualidad", "anticipo", "deposito", "deposit", "enganche"),
     "Cuota, abono o depósito ({n}); confirmar la moneda."),
    (_p("banco", "bank", "entidad financiera"), "Banco o entidad financiera asociada al registro."),
    (_p("tarjeta", "card"), "Tarjeta asociada al registro ({n}); no debe mostrar el número completo."),
    (_p("cuenta", "account"), "Cuenta asociada al registro ({n})."),
    # --- calidad, estado y puntajes
    (_p("puntaje", "puntuacion", "score", "rating", "calificacion", "valoracion", "estrellas", "stars"),
     "Puntaje de {n}. Su interpretación requiere conocer la escala de origen."),
    (_p("puntos", "points", "fidelidad", "loyalty", "millas", "miles"),
     "Puntos acumulados ({n}); confirmar la regla con que se ganan y se canjean."),
    (_p("estado", "estatus", "status", "situacion", "state", "condicion"),
     "Estado o situación registrada ({n}). Debe homologarse entre fuentes (nombres e idioma) antes de cruzar o comparar."),
    (_p("prioridad", "priority", "urgencia", "severidad", "severity", "nivel", "level", "ranking", "rank", "posicion"),
     "Nivel, prioridad u orden de importancia ({n}); confirmar su escala."),
    (_p("tipo", "type", "clase", "categoria", "category", "subcategoria", "segmento", "canal", "modalidad", "plan",
        "grupo", "group", "familia", "linea producto", "clasificacion", "class", "segment", "channel"),
     "Categoría que clasifica el registro ({n})."),
    (_p("motivo", "causa", "razon", "reason", "cause"), "Motivo o causa registrada ({n})."),
    (_p("observacion", "observaciones", "comentario", "comentarios", "notas", "descripcion", "detalle", "nota",
        "remark", "comment", "description", "notes", "details", "mensaje", "message", "texto libre"),
     "Texto libre con observaciones o detalle del registro."),
    # --- marcas de verdad (campos Sí/No que no empiezan con es_/tiene_)
    (_p("activo", "active", "vigente", "habilitado", "enabled", "disponible", "available", "bloqueado", "blocked",
        "eliminado", "deleted", "borrado", "verificado", "verified", "confirmado", "confirmed", "frecuente",
        "premium", "vip", "flag", "bandera", "suscrito", "subscribed"),
     "Indicador de si el registro cumple la condición {n}; confirmar cómo se codifican Sí y No."),
    # --- comercial y financiero
    (_p("moneda", "currency", "divisa"), "Moneda en que están expresados los montos."),
    (_p("forma pago", "metodo pago", "medio pago", "payment method", "payment type", "tipo pago", "metodo de pago"),
     "Forma o método con que se realizó el pago."),
    (_p("descuento", "discount", "rebaja", "promocion", "promotion", "cupon", "coupon"), "Descuento aplicado ({n})."),
    (_p("impuesto", "iva", "tax", "vat", "igv", "isr"), "Impuesto aplicado ({n})."),
    (_p("presupuesto", "budget", "meta", "objetivo", "target", "goal", "cuota ventas", "pronostico", "forecast"),
     "Presupuesto, meta u objetivo ({n}); confirmar el período y la moneda."),
    (_p("margen", "utilidad", "ganancia", "profit", "margin", "beneficio", "markup"),
     "Margen o utilidad ({n}); confirmar si es un monto o un porcentaje."),
    (_p("comision", "commission", "bono", "bonus", "propina", "tip"), "Comisión, bono o propina ({n})."),
    (_p("subtotal"), "Subtotal ({n}); confirmar qué conceptos incluye."),
    (_p("precio", "price", "tarifa", "pvp", "rate card"), "Precio o tarifa unitaria ({n})."),
    (_p("costo", "coste", "cost", "gasto", "expense", "expenditure"), "Costo o gasto registrado ({n})."),
    (_p("ingreso", "ingresos", "salario", "sueldo", "income", "salary", "wage", "remuneracion", "revenue",
        "paga", "pay"), "Ingreso o remuneración ({n})."),
    (_p("facturacion", "facturado", "billing", "billed", "venta", "sales", "sale", "vendido", "sold"),
     "Valor vendido o facturado ({n})."),
    (_p("pagado", "paid", "pago", "payment"), "Pago realizado o registrado ({n})."),
    (_p("total", "grand total", "importe total", "suma"), "Total del registro ({n}); confirmar qué conceptos suma y su moneda."),
    (_p("monto", "importe", "valor", "amount", "saldo", "balance", "value", "cobro", "recaudo"),
     "Monto en moneda del campo {n}; requiere un período definido para compararse."),
    # --- productos e inventario
    (_p("marca", "brand"), "Marca del producto."),
    (_p("modelo", "model"), "Modelo del producto."),
    (_p("color", "colour"), "Color del producto."),
    (_p("talla", "size", "tamano"), "Talla o tamaño ({n})."),
    (_p("material", "sabor", "flavor", "presentacion", "formato", "format", "acabado"),
     "Característica del producto ({n})."),
    (_p("dimension", "dimensiones", "largo", "ancho", "alto", "profundidad", "length", "width", "height", "depth"),
     "Dimensión física ({n}); confirmar la unidad de medida."),
    (_p("peso", "weight", "masa"), "Peso ({n}); confirmar la unidad de medida."),
    (_p("volumen", "volume", "capacidad", "capacity"), "Volumen o capacidad ({n}); confirmar la unidad de medida."),
    (_p("temperatura", "temperature", "temp"), "Temperatura ({n}); confirmar si está en °C o °F."),
    (_p("velocidad", "speed", "distancia", "distance", "kilometraje", "mileage", "recorrido", "superficie", "area",
        "potencia", "power", "voltaje", "voltage", "consumo", "consumption"),
     "Medida física o de uso ({n}); confirmar su unidad de medida."),
    (_p("existencias", "stock", "inventario", "inventory", "en stock"),
     "Existencias disponibles ({n}); depende de la fecha de corte del inventario."),
    (_p("lote", "batch", "lot"), "Lote de producción o de entrada ({n})."),
    (_p("garantia", "warranty"), "Garantía del producto ({n})."),
    (_p("devolucion", "devuelto", "return", "returned", "reembolso", "refund"), "Devolución o reembolso ({n})."),
    (_p("proveedor", "supplier", "vendor", "fabricante", "manufacturer"), "Proveedor o fabricante del registro ({n})."),
    (_p("producto", "articulo", "item", "sku", "mercaderia", "product"), "Producto o artículo del registro ({n})."),
    (_p("sucursal", "tienda", "oficina", "agencia", "store", "branch", "almacen", "bodega", "warehouse", "local"),
     "Sucursal o punto de atención del registro."),
    (_p("envio", "shipping", "entrega", "delivery", "despacho", "tracking", "guia", "rastreo", "transportista",
        "carrier", "flete", "courier", "shipment"),
     "Dato del envío o la entrega ({n})."),
    (_p("pedido", "orden", "order", "compra", "purchase"), "Dato del pedido o la compra ({n})."),
    (_p("factura", "invoice", "boleta", "recibo", "comprobante", "ticket", "receipt"),
     "Dato del comprobante de venta ({n})."),
    (_p("cliente", "asegurado", "customer", "client", "socio", "comprador", "consumidor"),
     "Cliente o persona asociada al registro ({n})."),
    (_p("usuario", "user"), "Usuario asociado al registro ({n})."),
    (_p("empleado", "employee", "trabajador", "colaborador", "staff", "vendedor", "representante", "gerente", "jefe",
        "supervisor", "asesor", "agente", "responsable", "manager"),
     "Empleado o responsable asociado al registro ({n})."),
    (_p("paciente", "patient"), "Paciente asociado al registro ({n})."),
    (_p("alumno", "estudiante", "student"), "Estudiante asociado al registro ({n})."),
    (_p("docente", "profesor", "teacher", "instructor"), "Docente asociado al registro ({n})."),
    (_p("curso", "asignatura", "materia", "course", "carrera", "programa", "escuela", "universidad", "colegio",
        "school", "ciclo", "grado", "grade", "creditos", "credits"),
     "Dato académico ({n})."),
    (_p("campana", "campaign", "utm", "fuente", "source", "medio", "medium", "referido", "referrer", "origen"),
     "Origen o campaña que generó el registro ({n})."),
    (_p("clics", "clicks", "click", "impresiones", "impressions", "visitas", "visits", "sesiones", "sessions",
        "vistas", "views", "descargas", "downloads", "likes", "seguidores", "followers", "reproducciones", "plays"),
     "Métrica de interacción digital ({n}); depende del período medido."),
    (_p("conversion", "ctr", "cpc", "cpa", "roi", "rebote", "bounce", "retencion", "churn", "nps", "csat"),
     "Indicador de desempeño ({n}); confirmar su fórmula y si está en porcentaje o en fracción."),
    (_p("version", "revision", "release", "build"), "Versión del registro o del producto ({n})."),
    (_p("dispositivo", "device", "navegador", "browser", "plataforma", "platform", "sistema operativo", "os"),
     "Dispositivo o plataforma desde la que se generó el registro ({n})."),
    (_p("turno", "shift", "jornada", "horas extra", "horas trabajadas", "vacaciones", "ausencia", "asistencia",
        "attendance"),
     "Dato de jornada laboral ({n}); confirmar su unidad (horas o días)."),
    # --- medidas
    (_p("porcentaje", "percent", "percentage", "tasa", "ratio", "proporcion", "indice", "index", "participacion",
        "share"),
     "Proporción o tasa ({n}); confirmar si está expresada en porcentaje o en fracción."),
    (_p("antiguedad", "duracion", "tiempo", "plazo", "dias", "meses", "anios", "anos", "duration", "tenure",
        "elapsed", "semanas", "horas", "minutos", "segundos", "days", "months", "years", "weeks", "hours",
        "minutes", "seconds"),
     "Duración o antigüedad ({n}); confirmar su unidad de medida."),
    (_p("cantidad", "unidades", "qty", "quantity", "count", "conteo", "numero de", "volumen ventas"),
     "Cantidad de unidades ({n})."),
    (_p("riesgo", "risk"), "Nivel o medida de riesgo ({n}); confirmar su escala de origen."),
    (_p("secuencia", "sequence", "linea", "line", "item numero", "consecutivo", "orden secuencia"),
     "Posición o número de secuencia dentro del registro ({n})."),
]

# Fechas por tipo de evento.
_FECHAS: List[Tuple[_Patron, str]] = [
    (_p("nacimiento", "nac", "birth", "birthday", "cumpleanos"), "Fecha de nacimiento."),
    (_p("esperada", "estimada", "prevista", "programada", "expected", "estimated", "scheduled", "planned"),
     "Fecha esperada o programada del evento ({n})."),
    (_p("entrega", "delivery", "despacho", "envio", "shipped", "shipping", "ship"), "Fecha de entrega o envío ({n})."),
    (_p("caducidad", "expiracion", "expiry", "expiration", "expires", "vencimiento", "vence"),
     "Fecha de caducidad o vencimiento."),
    (_p("inicio", "alta", "emision", "efectiva", "start", "desde", "from", "apertura", "ingreso", "contratacion",
        "hire"), "Fecha de inicio o de alta del registro ({n})."),
    (_p("fin", "baja", "cancelacion", "termino", "end", "hasta", "until", "cierre", "closing", "salida", "to"),
     "Fecha de fin, cierre o baja ({n})."),
    (_p("pago", "payment"), "Fecha de pago."),
    (_p("reclamo", "siniestro", "claim", "evento", "ocurrencia", "event", "incidente", "incident"),
     "Fecha en que ocurrió o se reportó el evento ({n})."),
    (_p("venta", "compra", "pedido", "orden", "factura", "order", "purchase", "invoice", "sale", "transaccion",
        "transaction"), "Fecha de la transacción ({n})."),
    (_p("actualizacion", "modificacion", "update", "updated", "modified", "modificado", "actualizado"),
     "Fecha de la última actualización."),
    (_p("registro", "creacion", "carga", "created", "creado", "load", "loaded", "insertado", "inserted"),
     "Fecha en que se registró el dato."),
    (_p("eliminacion", "deleted", "borrado", "eliminado"), "Fecha en que se eliminó el registro."),
]

_NOMBRE_DE_FECHA = _p("fecha", "date", "datetime", "timestamp", "created at", "updated at", "deleted at",
                      "modified at", "fecha hora")
_PALABRAS_RELLENO_FECHA = {"fecha", "date", "datetime", "timestamp", "texto", "text", "str", "string", "de", "del",
                           "fecha hora", "at", "hora"}
_RE_FECHA_VALOR = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}(?:[ T].*)?$")
_PERIODICIDAD = _p("periodicidad", "frecuencia", "forma pago")

# Calendario: campos que son solo una parte de la fecha (año, mes, día...).
_CALENDARIO: List[Tuple[_Patron, str]] = [
    (_p("dia semana", "weekday", "day of week"), "Día de la semana del registro."),
    (_p("anio", "ano", "year"), "Año calendario del registro."),
    (_p("trimestre", "quarter"), "Trimestre del año del registro."),
    (_p("semestre", "cuatrimestre", "bimestre"), "Semestre o cuatrimestre del año del registro."),
    (_p("mes", "month"), "Mes del registro (número o nombre)."),
    (_p("semana", "week"), "Semana del registro."),
    (_p("dia", "day"), "Día del mes del registro."),
    (_p("hora", "hour"), "Hora del registro."),
    (_p("periodo", "period"), "Período al que pertenece el registro (mes, trimestre, año u otro); confirmar su formato."),
]
_PALABRAS_DURACION = _p("antiguedad", "duracion", "tiempo", "plazo", "total", "promedio", "cantidad", "num",
                        "numero", "edad", "tenure", "count", "conteo", "transcurrido", "acumulado")

# Palabras que convierten un campo en una versión de otro.
_ORIGINAL = _p("original", "previo", "anterior", "antes", "raw", "sin limpiar", "reasignacion")
_NORMALIZADO = _p("norm", "normalizado", "normalizada", "estandarizado", "homologado", "limpio", "limpia", "clean")

# Agregaciones: (patrón, frase). Se anteponen a la descripción del campo base.
_AGREGACIONES: List[Tuple[_Patron, str]] = [
    (_p("max", "maximo", "maxima"), "Valor máximo por registro de {base}."),
    (_p("min", "minimo", "minima"), "Valor mínimo por registro de {base}."),
    (_p("prom", "promedio", "avg", "mean", "media", "average"), "Promedio por registro de {base}."),
    (_p("suma", "sum", "acumulado", "cumulative"), "Suma por registro de {base}."),
]

_CONTEO_FILAS = re.compile(r"\b(?:n|num|numero|cantidad|conteo|count|total)\b.*\b(?:filas|registros|rows|origen)\b|"
                           r"\b(?:filas|registros|rows)\b.*\b(?:origen|fuente|source)\b")
_CONTEO = _p("n", "num", "numero", "nro", "cantidad", "conteo", "count", "total", "nbr")

# Prefijos que vuelven un campo en un indicador Sí/No (es_frecuente, has_children, tiene_deuda).
_PREFIJO_BOOLEANO = ("es", "is", "has", "tiene", "ha", "fue", "was", "puede", "can", "flag", "ind", "bool",
                     "boolean", "bandera", "posee", "esta", "was", "hay", "tuvo", "aplica")

# Calificadores que matizan cualquier descripción.
_CALIFICADORES: List[Tuple[_Patron, str]] = [
    (_p("bruto", "bruta", "gross"), "Se expresa en valor bruto (antes de descuentos o deducciones)."),
    (_p("neto", "neta", "net"), "Se expresa en valor neto (después de descuentos o deducciones)."),
    (_p("unitario", "unitaria", "unit", "por unidad"), "Se expresa por unidad."),
    (_p("diario", "diaria", "daily"), "Se mide por día."),
    (_p("semanal", "weekly"), "Se mide por semana."),
    (_p("mensual", "monthly"), "Se mide por mes."),
    (_p("trimestral", "quarterly"), "Se mide por trimestre."),
    (_p("anual", "annual", "yearly"), "Se mide por año."),
    (_p("estimado", "estimada", "estimated", "proyectado", "forecast", "presupuestado", "budget"),
     "Es un valor estimado o presupuestado, no uno real."),
    (_p("acumulado", "acumulada", "cumulative", "ytd"), "Es un valor acumulado."),
]

# Unidades de medida que aparecen en el nombre (peso_kg, distancia_km, precio_usd).
_UNIDADES: List[Tuple[_Patron, str]] = [
    (_p("kg", "kilos", "kilogramos"), "kilogramos"), (_p("gr", "gramos"), "gramos"),
    (_p("lb", "lbs", "libras"), "libras"), (_p("oz", "onzas"), "onzas"), (_p("ton", "toneladas"), "toneladas"),
    (_p("lt", "lts", "litros"), "litros"), (_p("ml"), "mililitros"),
    (_p("cm"), "centímetros"), (_p("mm"), "milímetros"), (_p("mt", "mts", "metros"), "metros"),
    (_p("km"), "kilómetros"), (_p("m2"), "metros cuadrados"), (_p("m3"), "metros cúbicos"),
    (_p("usd", "dolares"), "dólares (USD)"), (_p("eur", "euros"), "euros"), (_p("crc", "colones"), "colones (CRC)"),
    (_p("mxn"), "pesos mexicanos"), (_p("pct"), "porcentaje"),
    (_p("months", "meses"), "meses"), (_p("days", "dias"), "días"), (_p("years", "anios"), "años"),
]


# --------------------------------------------------------------------------
# Traducción de palabras sueltas (inglés -> español) y nombres compuestos de seguros
# --------------------------------------------------------------------------

# palabra en inglés -> (español, género). Sirve para que un nombre en inglés no se describa a medias
# («Dato de «vehicle make»») y para armar artículos («del vehículo», «de la póliza»).
_TRAD_BASE: Dict[str, Tuple[str, str]] = {
    "vehicle": ("vehículo", "m"), "car": ("auto", "m"), "property": ("inmueble", "m"), "home": ("vivienda", "f"),
    "policy": ("póliza", "f"), "claim": ("reclamo", "m"), "adjuster": ("ajustador", "m"),
    "adjustment": ("ajuste", "m"), "settlement": ("liquidación", "f"), "renewal": ("renovación", "f"),
    "agent": ("agente", "m"), "broker": ("corredor", "m"), "customer": ("cliente", "m"),
    "client": ("cliente", "m"), "incident": ("incidente", "m"), "coverage": ("cobertura", "f"),
    "premium": ("prima", "f"), "deductible": ("deducible", "m"), "commission": ("comisión", "f"),
    "discount": ("descuento", "m"), "bonus": ("bonificación", "f"), "loyalty": ("fidelidad", "f"),
    "complaint": ("queja", "f"), "fraud": ("fraude", "m"), "risk": ("riesgo", "m"),
    "order": ("pedido", "m"), "product": ("producto", "m"), "invoice": ("factura", "f"),
    "sale": ("venta", "f"), "payment": ("pago", "m"), "account": ("cuenta", "f"), "user": ("usuario", "m"),
    "employee": ("empleado", "m"), "supplier": ("proveedor", "m"), "store": ("tienda", "f"),
    "branch": ("sucursal", "f"), "shipment": ("envío", "m"), "contract": ("contrato", "m"),
    "loan": ("préstamo", "m"), "card": ("tarjeta", "f"), "transaction": ("transacción", "f"),
    "patient": ("paciente", "m"), "student": ("estudiante", "m"), "course": ("curso", "m"),
    "project": ("proyecto", "m"), "task": ("tarea", "f"), "campaign": ("campaña", "f"),
    "item": ("artículo", "m"), "device": ("dispositivo", "m"), "session": ("sesión", "f"),
    "visit": ("visita", "f"), "police": ("policía", "f"), "balance": ("saldo", "m"), "report": ("informe", "m"),
    "decision": ("decisión", "f"), "location": ("ubicación", "f"), "city": ("ciudad", "f"),
    "address": ("dirección", "f"), "note": ("nota", "f"), "survey": ("encuesta", "f"),
}
_ES_PLURAL_IRREGULAR = {"transacción": "transacciones", "sesión": "sesiones", "comisión": "comisiones",
                        "liquidación": "liquidaciones", "renovación": "renovaciones", "decisión": "decisiones",
                        "ubicación": "ubicaciones", "dirección": "direcciones", "bonificación": "bonificaciones",
                        "ciudad": "ciudades", "informe": "informes"}
# Palabras sueltas sin género (adjetivos, verbos, otras): solo se traducen, no llevan artículo.
_TRAD_PALABRAS: Dict[str, str] = {
    "filed": "presentado", "approved": "aprobado", "denied": "rechazado", "paid": "pagado",
    "outstanding": "pendiente", "built": "construido", "created": "creado", "updated": "actualizado",
    "denial": "rechazo", "reason": "motivo", "frequency": "frecuencia", "probability": "probabilidad",
    "limit": "límite", "amount": "monto", "value": "valor", "total": "total", "make": "marca", "model": "modelo",
    "type": "tipo", "status": "estado", "state": "estado", "name": "nombre", "description": "descripción",
    "score": "puntaje", "satisfaction": "satisfacción", "tenure": "antigüedad", "lifetime": "vida",
    "notes": "notas", "multi": "múltiples", "start": "inicio", "end": "fin", "year": "año", "month": "mes",
    "underwriting": "suscripción", "churn": "abandono", "applied": "aplicado",
    "number": "número", "date": "fecha", "id": "identificador",
}


def _pluralizar_es(palabra: str) -> str:
    if palabra in _ES_PLURAL_IRREGULAR:
        return _ES_PLURAL_IRREGULAR[palabra]
    return palabra + ("s" if palabra[-1] in "aeiouáéíóú" else "es")


def _trad_sustantivo(token: str) -> Optional[Tuple[str, str, bool]]:
    """(español, género, es_plural) de un sustantivo en inglés, o None. Entiende «claims» y «policies»."""
    if token in _TRAD_BASE:
        return _TRAD_BASE[token] + (False,)
    for sufijo, base in (("ies", "y"), ("es", ""), ("s", "")):
        if token.endswith(sufijo) and len(token) > len(sufijo) + 2:
            raiz = token[: -len(sufijo)] + base
            if raiz in _TRAD_BASE:
                return _TRAD_BASE[raiz] + (True,)
    return None


def _traducir_tokens(tokens: Sequence[str]) -> str:
    """Palabras del nombre en español cuando se conocen (el resto queda tal cual)."""
    salida = []
    for t in tokens:
        sust = _trad_sustantivo(t)
        if sust:
            salida.append(_pluralizar_es(sust[0]) if sust[2] else sust[0])
        else:
            salida.append(_TRAD_PALABRAS.get(t, t))
    return " ".join(salida)


def _de_sintagma(tokens: Sequence[str]) -> str:
    """'de la póliza', 'del vehículo' (un sustantivo conocido) o 'de «x y»' (si no se conoce)."""
    if len(tokens) == 1:
        sust = _trad_sustantivo(tokens[0])
        if sust and not sust[2]:
            return f"de la {sust[0]}" if sust[1] == "f" else f"del {sust[0]}"
    return f"de «{_traducir_tokens(tokens)}»"


# Nombres compuestos de seguros y analítica de clientes que el glosario general describe a medias
# (primero el más específico). La descripción no menciona unidades: se agregan solas («Se expresa en euros»).
_SEGUROS: List[Tuple[_Patron, str]] = [
    # --- pólizas
    (_p("policy number", "numero poliza"), "Número de la póliza tal como lo ve el cliente (distinto del identificador interno)."),
    (_p("policy status", "estado poliza"), "Estado de la póliza (por ejemplo activa, vencida o cancelada)."),
    (_p("policy start date", "policy start", "policy effective date", "inicio vigencia"),
     "Fecha de inicio de vigencia de la póliza."),
    (_p("policy end date", "policy end", "policy expiry date", "policy expiration", "fin vigencia"),
     "Fecha de fin de vigencia de la póliza."),
    (_p("renewal date", "fecha renovacion"), "Fecha de renovación de la póliza."),
    (_p("insurance type", "tipo seguro", "ramo"), "Tipo o ramo de seguro (por ejemplo vida, auto u hogar)."),
    (_p("coverage type", "tipo cobertura"), "Tipo de cobertura contratada; define hasta dónde responde el seguro."),
    (_p("coverage limit", "limite cobertura"), "Límite de cobertura: monto máximo que paga el seguro."),
    (_p("deductible", "deducible"), "Deducible: monto que asume el cliente antes de que el seguro pague."),
    (_p("payment frequency", "frecuencia pago"),
     "Periodicidad con que se paga la prima (por ejemplo mensual, trimestral o anual)."),
    (_p("total paid", "total pagado"),
     "Total pagado acumulado; confirmar si corresponde a primas o a reclamos."),
    (_p("outstanding balance", "saldo pendiente"), "Saldo pendiente de pago."),
    # --- bienes asegurados
    (_p("vehicle make"), "Marca del vehículo asegurado."),
    (_p("vehicle model"), "Modelo del vehículo asegurado."),
    (_p("vehicle value"), "Valor del vehículo asegurado."),
    (_p("vehicle year", "model year"), "Año del vehículo asegurado (modelo o fabricación)."),
    (_p("property type"), "Tipo de inmueble asegurado."),
    (_p("property value"), "Valor del inmueble asegurado."),
    (_p("property year built", "year built", "anio construccion"), "Año de construcción del inmueble asegurado."),
    # --- reclamos
    (_p("claim status", "estado reclamo"), "Estado en que se encuentra el reclamo."),
    (_p("claim amount", "monto reclamado"),
     "Monto reclamado (solicitado) por el cliente. No equivale al monto aprobado y no deben combinarse "
     "como un único costo."),
    (_p("claim description"), "Descripción del reclamo o de lo ocurrido."),
    (_p("claim denial reason", "denial reason", "motivo rechazo"), "Motivo por el que se rechazó el reclamo (si aplica)."),
    (_p("incident location"), "Lugar donde ocurrió el incidente."),
    (_p("police report filed"), "Indicador Sí/No: señala si se presentó denuncia o informe policial."),
    (_p("police report number"), "Número del informe o denuncia policial."),
    (_p("adjuster id"), "Identificador del ajustador (perito) que revisó el reclamo. Sirve para enlazar registros "
                        "entre tablas."),
    (_p("adjuster name"), "Nombre del ajustador (perito) que revisó el reclamo."),
    (_p("adjustment date"), "Fecha en que se ajustó o peritó el reclamo."),
    (_p("settlement date"), "Fecha de liquidación (pago) del reclamo."),
    (_p("settlement amount"), "Monto liquidado (pagado) del reclamo; puede apoyar un indicador de siniestralidad "
                              "pagada cuando su definición y período estén confirmados."),
    # --- riesgo y fraude
    (_p("fraud flag"), "Indicador Sí/No: señala si el registro tiene sospecha de fraude."),
    (_p("fraud score"), "Puntaje de riesgo de fraude. Su interpretación requiere conocer la escala de origen."),
    (_p("risk score"), "Puntaje de riesgo asignado al cliente o a la póliza. Su interpretación requiere conocer "
                       "la escala de origen."),
    # --- ventas y comisiones
    (_p("commission amount"), "Monto de la comisión pagada al agente o corredor."),
    (_p("agent id"), "Identificador del agente que vendió la póliza. Sirve para enlazar registros entre tablas."),
    (_p("agent name"), "Nombre del agente que vendió la póliza."),
    (_p("agent commission"), "Comisión del agente sobre la venta."),
    (_p("broker id"), "Identificador del corredor (broker) que intermedió la póliza. Sirve para enlazar registros "
                      "entre tablas."),
    (_p("broker commission"), "Comisión del corredor (broker) sobre la venta."),
    (_p("discount applied"), "Descuento aplicado a la prima."),
    (_p("no claims bonus", "no claim bonus"), "Bonificación por no haber tenido reclamos."),
    (_p("loyalty discount"), "Descuento por fidelidad del cliente."),
    (_p("multi policy discount"), "Descuento por tener varias pólizas."),
    # --- clientes
    (_p("customer lifetime value", "clv", "ltv"),
     "Valor de vida del cliente (CLV): ingreso estimado que aporta durante toda su relación con la empresa."),
    (_p("customer tenure", "tenure"), "Antigüedad del cliente: tiempo transcurrido desde que lo es."),
    (_p("customer satisfaction", "satisfaction score", "satisfaccion cliente"),
     "Nota de satisfacción del cliente. Su interpretación requiere conocer la escala de origen."),
    (_p("nps score", "nps", "net promoter score"),
     "Net Promoter Score (NPS): qué tan probable es que el cliente recomiende la empresa. Su interpretación "
     "requiere conocer la escala de origen."),
    (_p("complaint filed"), "Indicador Sí/No: señala si el cliente presentó una queja."),
    (_p("complaint type"), "Tipo de queja presentada por el cliente."),
    (_p("churn risk"), "Riesgo de que el cliente abandone (churn); confirmar si es un nivel, una probabilidad o un porcentaje."),
]

# Para nombres que ninguna regla conoce: «{cabeza} de {algo}» cuando la última palabra dice qué es.
_CABEZAS: Dict[str, str] = {
    "amount": "Monto", "value": "Valor", "total": "Total", "balance": "Saldo", "limit": "Límite",
    "status": "Estado", "type": "Tipo", "reason": "Motivo", "name": "Nombre", "description": "Descripción",
    "score": "Puntaje", "rate": "Tasa", "level": "Nivel", "category": "Categoría", "location": "Ubicación",
}


def _componer(tokens: Sequence[str]) -> Optional[str]:
    """«order_status» -> «Estado del pedido». Solo cuando es una palabra de contexto conocida
    más una cabeza reconocida; en cualquier otro caso devuelve None (mejor genérico que equivocado)."""
    unidades = {"eur", "usd", "crc", "mxn", "pct", "percent"}
    base = [t for t in tokens if t not in unidades and t not in ("of", "de", "del", "the")]
    if len(base) != 2 or base[1] not in _CABEZAS:
        return None
    sust = _trad_sustantivo(base[0])
    if not sust or sust[2]:
        return None
    frase = f"{_CABEZAS[base[1]]} {_de_sintagma([base[0]])}"
    if base[1] == "name":
        frase += " (texto libre; no sirve como llave)"
    return frase + "."

_NOMBRE_GENERICO = re.compile(r"^(?:col|column|columna|campo|field|var|variable|feature|f|v|c|x|y|z|unnamed|"
                              r"attr|atributo|dato|data)(?: ?\d+)*(?: \d+)?$")

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

# --------------------------------------------------------------------------
# Búsqueda en el glosario
# --------------------------------------------------------------------------

Tabla = List[Tuple[_Patron, str]]


def _buscar(texto: str, compacto: str, tabla: Optional[Tabla] = None) -> Optional[Tuple[_Patron, str]]:
    """Primera entrada del glosario que coincide: primero por palabras completas, después
    (si no hubo nada) por palabras largas dentro de un nombre pegado."""
    tabla = tabla or _GLOSARIO
    for patron, plantilla in tabla:
        if patron.search(texto):
            return patron, plantilla
    if compacto:
        for patron, plantilla in tabla:
            if patron.en_compacto(compacto):
                return patron, plantilla
    return None


def _glosario(nombre_norm: str, tabla: Optional[Tabla] = None) -> Optional[str]:
    """Descripción del glosario para un nombre ya normalizado (None si no hay)."""
    encontrado = _buscar(_texto_busqueda(nombre_norm.split()), nombre_norm.replace(" ", ""), tabla)
    return encontrado[1] if encontrado else None


@lru_cache(maxsize=1)
def _vocabulario() -> Tuple[str, ...]:
    """Palabras sueltas de más de 4 letras del glosario, para corregir tipeos leves."""
    palabras = set(_ABREVIATURAS.values())
    for tabla in (_GLOSARIO, _FECHAS, _CALENDARIO, _AGREGACIONES, _CALIFICADORES):
        for patron, _ in tabla:
            palabras.update(patron.palabras)
    for patron, _ in _ENTIDADES:
        palabras.update(patron.palabras)
    for patron, _ in _SEGUROS:
        palabras.update(patron.palabras)
    palabras.update(_TRAD_BASE)
    palabras.update(_TRAD_PALABRAS)
    palabras.update(_DOCUMENTO_IDENTIDAD.palabras)
    return tuple(sorted(p for p in palabras if " " not in p and len(p) >= 5 and p.isalpha()))


_LETRAS = "abcdefghijklmnopqrstuvwxyz"


@lru_cache(maxsize=1)
def _vocabulario_set() -> frozenset:
    return frozenset(_vocabulario())


def _ediciones(token: str, con_inserciones: bool) -> Iterable[str]:
    """Palabras a una sola edición de `token` (letra intercambiada; y, en palabras largas,
    letra de más, de menos o cambiada)."""
    for i in range(len(token) - 1):
        yield token[:i] + token[i + 1] + token[i] + token[i + 2:]
    if con_inserciones:
        for i in range(len(token)):
            yield token[:i] + token[i + 1:]
            for c in _LETRAS:
                yield token[:i] + c + token[i + 1:]
        for i in range(len(token) + 1):
            for c in _LETRAS:
                yield token[:i] + c + token[i:]


@lru_cache(maxsize=4096)
def _corregir_tipeo(token: str) -> str:
    """'direcion' -> 'direccion', 'cantidd' -> 'cantidad', 'fehca' -> 'fecha'. Solo corrige si la palabra
    está a una edición de una palabra del glosario (y en palabras de 6+ letras); si no, la deja igual."""
    if len(token) < 5 or not token.isalpha():
        return token
    vocabulario = _vocabulario_set()
    if token in vocabulario or _singular_token(token) in vocabulario:
        return token
    for candidato in _ediciones(token, con_inserciones=len(token) >= 6):
        if candidato in vocabulario and candidato[0] == token[0]:
            return candidato
    return token


def _tokens_nombre(nombre: str) -> List[str]:
    """Palabras del nombre ya normalizadas: abreviaturas expandidas y tipeos corregidos."""
    tokens = normalizar_nombre(nombre).split()
    tokens = _expandir_abreviaturas(tokens)
    return [_corregir_tipeo(t) for t in tokens]


def _texto_busqueda(tokens: Sequence[str]) -> str:
    """Nombre + su versión en singular, para que 'ventas' encuentre 'venta' y viceversa."""
    plano = " ".join(tokens)
    singular = " ".join(_singular_token(t) for t in tokens)
    return plano if plano == singular else f"{plano} | {singular}"


def _base_sin_marca(texto: str, patron: _Patron) -> str:
    return re.sub(r"\s+", " ", patron._re.sub(" ", texto)).strip()


_NO_COMPONER = {"venta"}  # «precio_venta» es el precio de venta, no algo «de la venta»


def _entidad(texto: str, excluir: Iterable[str] = (), para_componer: bool = False) -> Optional[str]:
    """Entidad del negocio que menciona el nombre (cliente, producto...), si hay una."""
    palabras_excluidas = {w for frase in excluir for w in frase.split()}
    for patron, sustantivo in _ENTIDADES:
        if para_componer and sustantivo in _NO_COMPONER:
            continue
        encontrada = patron.search(texto)
        if encontrada and encontrada.group(0) not in palabras_excluidas:
            return sustantivo
    return None


def _a_la_entidad(sustantivo: str) -> str:
    return f"a la {sustantivo}" if sustantivo in _FEMENINOS else f"al {sustantivo}"


# --------------------------------------------------------------------------
# Pistas desde el contenido
# --------------------------------------------------------------------------

_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_RE_URL = re.compile(r"^(?:https?://|www\.)\S+$", re.I)
_RE_IP = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_RE_COLOR = re.compile(r"^#(?:[0-9a-f]{3}|[0-9a-f]{6})$", re.I)
_RE_HORA = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?(?:\s?[ap]\.?m\.?)?$", re.I)
_RE_PORCENTAJE = re.compile(r"^-?\d+(?:[.,]\d+)?\s?%$")
_RE_MONEDA = re.compile(r"^[-(]?\s?(?:[$€£¥₡]|US\$|CRC|USD|EUR)\s?\d[\d.,]*\)?$", re.I)
_BOOLEANOS = {"si", "sí", "no", "s", "n", "yes", "y", "true", "false", "verdadero", "falso", "1", "0", "x", "t", "f",
              "activo", "inactivo", "on", "off"}
_SEXOS = {"m", "f", "h", "masculino", "femenino", "hombre", "mujer", "male", "female", "otro", "other"}
_PERIODICIDADES = {"mensual", "anual", "trimestral", "semestral", "bimestral", "quincenal", "semanal", "diario",
                   "monthly", "yearly", "annual", "quarterly", "weekly"}
_NULOS_TEXTO = {"", "nan", "none", "null", "n/a", "na", "nat", "<na>", "-", "--", "sin dato", "s/d"}


def _muestra(serie: Optional[pd.Series], ejemplos: str = "", maximo: int = 300) -> List[str]:
    """Valores de ejemplo como texto, sin nulos. Con serie se toman de los datos; si solo se tiene el texto
    de «Rango o ejemplos» del diccionario, se usan esos ejemplos."""
    if serie is not None:
        texto = serie.dropna().astype(str).str.strip()
        return [v for v in texto.head(maximo * 4).tolist() if v.lower() not in _NULOS_TEXTO][:maximo]
    limpio = re.sub(r"\(\+\d+ más\)\s*$", "", re.sub(r"^Ej\.:\s*", "", str(ejemplos or ""))).strip()
    return [v.strip() for v in limpio.split(",") if v.strip() and v.strip().lower() not in _NULOS_TEXTO]


def _fraccion(valores: Sequence[str], criterio: Callable[[str], bool]) -> float:
    return sum(1 for v in valores if criterio(v)) / len(valores) if valores else 0.0


def _pista_contenido(valores: Sequence[str], tipo: str, n: str, unicos: Optional[int],
                     filas: Optional[int], serie: Optional[pd.Series] = None) -> Optional[str]:
    """Descripción deducida de los valores cuando el nombre no dice nada. Habla con cautela
    («parece») salvo cuando el formato es inequívoco."""
    if not valores:
        return None
    if _fraccion(valores, lambda v: bool(_RE_EMAIL.match(v))) >= 0.8:
        return "Correo electrónico."
    if _fraccion(valores, lambda v: bool(_RE_URL.match(v))) >= 0.8:
        return "Dirección web (URL) asociada al registro."
    if _fraccion(valores, lambda v: bool(_RE_IP.match(v))) >= 0.8:
        return "Dirección IP asociada al registro."
    if _fraccion(valores, lambda v: bool(_RE_COLOR.match(v))) >= 0.8:
        return "Color en formato hexadecimal."
    if _fraccion(valores, lambda v: bool(_RE_HORA.match(v))) >= 0.8:
        return "Hora del día del registro."
    if _fraccion(valores, lambda v: bool(_RE_PORCENTAJE.match(v))) >= 0.8:
        return f"Porcentaje de {n}, guardado con el símbolo %."
    if _fraccion(valores, lambda v: bool(_RE_MONEDA.match(v))) >= 0.8:
        return f"Monto monetario de {n}, guardado con símbolo de moneda; conviene convertirlo a número."
    bajos = {v.lower() for v in valores}
    if 2 <= len(bajos) <= 3 and bajos <= _BOOLEANOS and tipo != "Fecha":
        return f"Parece un indicador Sí/No de {n}."
    if len(bajos) <= 4 and bajos <= _SEXOS and len(bajos) >= 2:
        return "Parece el sexo o género de la persona."
    if len(bajos) <= 8 and len(bajos) >= 2 and bajos <= _PERIODICIDADES:
        return "Periodicidad (mensual, anual, etc.); permite llevar los montos a un período comparable."
    if serie is not None and tipo in ("Entero", "Decimal"):
        numeros = pd.to_numeric(serie, errors="coerce").dropna()
        if len(numeros):
            if tipo == "Entero" and numeros.between(1900, 2100).all() and numeros.nunique() > 1:
                return f"Parece un año ({n})."
            if tipo == "Decimal" and numeros.between(0, 1).all():
                return f"Parece una proporción entre 0 y 1 ({n}); confirmar qué mide."
    if filas and unicos and filas >= 10 and unicos >= 0.98 * filas:
        if tipo in ("Entero", "Texto (código)"):
            return f"Parece un identificador: tiene un valor distinto en casi todas las filas ({n})."
        if tipo == "Texto":
            largo = sum(len(v) for v in valores) / len(valores)
            if largo > 40:
                return f"Texto libre extenso de {n}, distinto en casi todas las filas."
            return f"Texto distinto en casi todos los registros ({n}); parece un nombre o un identificador."
    if tipo == "Texto":
        largo = sum(len(v) for v in valores) / len(valores)
        if largo > 60:
            return f"Texto libre extenso de {n}."
    if tipo == "Categoría":
        frecuentes = pd.Series(list(valores)).value_counts().index.tolist()[:4]
        corto = ", ".join(v if len(v) <= 25 else v[:22] + "..." for v in frecuentes)
        return f"Categoría de {n} con valores como: {corto}."
    return None


# --------------------------------------------------------------------------
# Descripción de un campo
# --------------------------------------------------------------------------


def _descripcion_identificador(tokens: List[str], texto: str, rol: str, es_llave: bool) -> Optional[str]:
    """Descripción de identificadores: id_cliente, cliente_id, codigo_producto, num_factura, tracking..."""
    ids_fuertes = [t for t in tokens if t in _ID_FUERTE]
    ids_numero = [t for t in tokens if t in _ID_NUMERO]
    resto = [t for t in tokens if t not in _ID]
    base = " ".join(resto)
    sustantivo = _entidad(" ".join(resto) + " | " + " ".join(_singular_token(t) for t in resto)) if resto else None
    es_id = False
    if rol == "id" or es_llave:
        es_id = True
    elif ids_fuertes and (tokens[0] in _ID_FUERTE or tokens[-1] in _ID_FUERTE):
        es_id = True
    elif ids_numero and sustantivo and (tokens[0] in _ID_NUMERO or tokens[-1] in _ID_NUMERO) \
            and not (ids_numero == ["no"] and len(resto) >= 2):  # «no_claims_bonus» no es un número
        es_id = True  # num_factura, numero_orden, no_poliza
    elif _TRACKING.search(texto) and not ids_numero:
        es_id = True
    if not es_id:
        return None
    if _p("poliza", "policy").search(texto):
        return "Identifica una póliza y permite consolidar sus registros. Evita contar repetidamente la misma póliza."
    if sustantivo == "cliente":
        return "Identifica a un cliente y permite consolidar sus registros sin contarlo más de una vez."
    if _TRACKING.search(texto):
        return "Número de seguimiento o guía del envío; sirve para rastrearlo y enlazar con el transportista."
    if _POSTAL.search(texto):
        return _GLOSARIO[_indice_codigo_postal()][1]
    if not resto:
        return "Identificador del registro. Sirve para enlazar registros entre tablas y para detectar duplicados."
    nombre_id = sustantivo or _traducir_tokens(resto)
    if ids_fuertes and tokens[0] in ("codigo", "cod", "clave", "cve") and not sustantivo:
        return (f"Código de {base}. Identifica o abrevia ese dato y sirve para enlazar con catálogos "
                "y para detectar duplicados.")
    return (f"Identificador de {nombre_id}. Sirve para enlazar registros entre tablas "
            "y para detectar duplicados.")


@lru_cache(maxsize=1)
def _indice_codigo_postal() -> int:
    for i, (patron, _) in enumerate(_GLOSARIO):
        if "codigo postal" in patron.palabras:
            return i
    return 0


def _calificadores(texto: str, norm_base: str) -> List[str]:
    frases: List[str] = []
    for patron, frase in _CALIFICADORES:
        if patron.search(texto) and frase not in frases:
            frases.append(frase)
    for patron, unidad in _UNIDADES:
        if patron.search(norm_base) or patron.search(texto):
            frases.append(f"Se expresa en {unidad}.")
            break
    return frases


def describir_campo(col, rol: str = "texto", tipo: str = "Texto", tratamiento: str = "",
                    origen: str = "", es_llave: bool = False, *,
                    serie: Optional[pd.Series] = None, ejemplos: str = "",
                    unicos: Optional[int] = None, filas: Optional[int] = None,
                    contexto: Optional[Sequence[str]] = None) -> str:
    """Descripción redactada de un campo a partir de su nombre, rol, tipo y (si se dan) sus valores.
    `serie` son los datos de la columna; sin ella se puede pasar `ejemplos` (texto de «Rango o ejemplos»)
    y `unicos`/`filas`. `contexto` son los nombres de las demás columnas de la tabla: ayudan a leer palabras
    ambiguas («state» junto a city y country es una región, no una situación). Siempre devuelve texto."""
    nombre = str(col)
    norm_base = normalizar_nombre(nombre)
    n = nombre_legible(nombre)
    tokens = _tokens_nombre(nombre)
    texto_norm = " ".join(tokens)
    texto = _texto_busqueda(tokens)
    compacto = "".join(tokens)
    resultado: Optional[str] = None
    ganador: Optional[_Patron] = None
    con_datos = False  # True si el texto incluye valores de la columna (no se tocan sus llaves {})
    valores = _muestra(serie, ejemplos)
    if serie is not None:
        unicos = int(serie.nunique(dropna=True)) if unicos is None else unicos
        filas = len(serie) if filas is None else filas

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
        resultado = "Correo electrónico."
    elif rol == "telefono":
        resultado = "Número de teléfono."
    elif rol == "coordenada":
        encontrado = _buscar(texto, compacto, [_GLOSARIO[_indice_por_palabra("latitud")],
                                               _GLOSARIO[_indice_por_palabra("longitud")]])
        resultado = encontrado[1] if encontrado else "Coordenada geográfica en grados decimales."

    # Columna sin nombre descriptivo (col1, Unnamed: 0, var_3...).
    n_q = f"«{n}»"
    if resultado is None and _NOMBRE_GENERICO.match(norm_base):
        if "unnamed" in norm_base and serie is not None and tipo == "Entero":
            numeros = pd.to_numeric(serie, errors="coerce").dropna()
            if len(numeros) > 1 and (numeros.diff().dropna() == 1).all():
                return ("Número de fila que se guardó al exportar la tabla; normalmente no aporta información "
                        "y se puede eliminar.")
        pista = _pista_contenido(valores, tipo, n_q, unicos, filas, serie)
        base = f"Columna sin nombre descriptivo ({n_q}); su significado debe confirmarse con la fuente de datos."
        resultado = base + (" " + pista if pista else "")
        con_datos = bool(pista)

    # Indicador Sí/No por prefijo (es_frecuente, has_children, tiene_deuda).
    if resultado is None and len(tokens) >= 2 and tokens[0] in _PREFIJO_BOOLEANO:
        resto = _traducir_tokens(tokens[1:])
        resultado = (f"Indicador Sí/No: señala si el registro cumple «{resto}»; "
                     "confirmar cómo se codifican Sí y No.")

    # Versión original o normalizada de otro campo.
    if resultado is None:
        version = None
        if _ORIGINAL.search(texto):
            version, patron = "original", _ORIGINAL
        elif _NORMALIZADO.search(texto):
            version, patron = "normalizado", _NORMALIZADO
        if version:
            base = _base_sin_marca(texto_norm, patron) or n
            base_txt = _glosario(base)
            if version == "original":
                resultado = (f"Valor de «{base}» previo a su transformación o reasignación; "
                             "se conserva para mantener trazabilidad.")
            else:
                resultado = (f"Valor de «{base}» normalizado (formato y nombres homologados) "
                             "para poder cruzarlo con otras fuentes.")
            if base_txt and base_txt.startswith(("Código postal", "Estado")):
                resultado += " " + base_txt.split(". ", 1)[0] + "."

    # Conteo de filas de origen consolidadas.
    if resultado is None and _CONTEO_FILAS.search(texto_norm):
        resultado = ("Cuenta las filas de origen consolidadas en cada registro. No equivale todavía a un "
                     "conteo validado de eventos reales.")

    # Agregación sobre otro campo (max_puntaje, prom_edad, ...).
    if resultado is None:
        for patron, frase in _AGREGACIONES:
            if patron.search(texto_norm):
                base = _base_sin_marca(texto_norm, patron)
                if base:
                    detalle = _glosario(base)
                    resultado = frase.format(base=f"«{base}»")
                    if detalle and detalle.startswith("Puntaje"):
                        resultado += " Su interpretación requiere conocer las escalas de origen."
                    break

    # Datos sensibles (contraseñas, documentos de identidad, tarjetas): antes que los identificadores.
    if resultado is None:
        encontrado = _buscar(texto, compacto, _GLOSARIO[:4])
        if encontrado:
            ganador, resultado = encontrado

    # «num_reclamos», «nro_visitas»: un número seguido de un sustantivo en plural es un conteo,
    # no un identificador («numero_poliza», en singular, sí lo es).
    conteo_plural = tipo in ("Entero", "Decimal") and any(t in ("num", "numero", "nro") for t in tokens) and any(
        t.endswith("s") and len(t) > 3 and not t.endswith(("ss", "us", "is")) for t in tokens)

    # Nombres compuestos de seguros y de analítica de clientes (antes de los identificadores para que
    # «adjuster_id» o «agent_id» digan quién es, no solo «Identificador de adjuster»).
    curado = False
    if resultado is None and tokens in (["state"], ["estado"]) and contexto:
        vecinas = {t for c in contexto for t in normalizar_nombre(c).split()}
        if vecinas & {"city", "ciudad", "country", "pais", "postal", "zip", "cp", "address", "direccion",
                      "municipio", "province", "provincia", "region"}:
            resultado, curado = ("Estado, provincia o región donde se ubica el registro; conviene homologar sus "
                                 "nombres (e idioma) entre fuentes antes de cruzar o comparar."), True
    if resultado is None:
        encontrado = _buscar(texto, compacto, _SEGUROS)
        if encontrado:
            resultado, curado = encontrado[1], True

    # Identificadores.
    if resultado is None and not conteo_plural:
        resultado = _descripcion_identificador(tokens, texto, rol, es_llave)

    # «nota» es un puntaje si es numérica y un comentario si es texto.
    if resultado is None and tipo in ("Entero", "Decimal") and _p("nota", "nota final").search(texto):
        resultado = "Puntaje de {n}. Su interpretación requiere conocer la escala de origen."

    # Campos que son solo una parte de la fecha (año, mes, día, hora...).
    if resultado is None and not _PALABRAS_DURACION.search(texto_norm) and tipo != "Fecha" \
            and not _NOMBRE_DE_FECHA.search(texto_norm):
        palabras_calendario = {w for p, _ in _CALENDARIO for w in p.palabras}
        sin_relleno = [t for t in tokens if t not in ("de", "del", "la", "el", "en")]
        if sin_relleno and sum(1 for t in sin_relleno if t in palabras_calendario) >= 1 and (
                len(sin_relleno) <= 2 or sin_relleno[0] in palabras_calendario):
            if _CALENDARIO[1][0].search(texto_norm) and _CALENDARIO[4][0].search(texto_norm):
                resultado = "Mes y año del registro."
            else:
                for patron, frase in _CALENDARIO:
                    if patron.search(texto_norm):
                        calificador = [t for t in sin_relleno if t not in palabras_calendario]
                        if calificador:  # «vehicle_year», «birth_year»: año de otra cosa, no del registro
                            sust = _de_sintagma(calificador)
                            resultado = (f"{frase.split()[0]} {sust}" + ("." if sust.startswith(("del ", "de la "))
                                         else "; confirmar a qué se refiere."))
                        else:
                            resultado = frase
                        break

    # Fechas.
    es_fecha_por_nombre = bool(_NOMBRE_DE_FECHA.search(texto_norm) or _NOMBRE_DE_FECHA.en_compacto(compacto))
    # El rol «fecha» puede venir solo del nombre («metodo_pago» contiene «pago»): se confirma con el tipo,
    # con el nombre o con los valores.
    rol_fecha = rol == "fecha" and (tipo == "Fecha" or es_fecha_por_nombre or not valores
                                    or _fraccion(valores, lambda v: bool(_RE_FECHA_VALOR.match(v))) >= 0.8)
    if resultado is None and (rol_fecha or tipo == "Fecha" or es_fecha_por_nombre) \
            and not _PERIODICIDAD.search(texto_norm):
        encontrado = _buscar(texto, compacto, _FECHAS)
        if encontrado:
            resultado = encontrado[1]
        else:
            base_fecha = _traducir_tokens([t for t in tokens if t not in _PALABRAS_RELLENO_FECHA])
            if base_fecha:
                resultado = f"Fecha de «{base_fecha}»."
            else:
                resultado = "Fecha y hora del registro." if "hora" in tokens or "time" in tokens else "Fecha del registro."

    # Conteos por registro (n_reclamos, num_hijos, cantidad_visitas...).
    if resultado is None and tipo in ("Entero", "Decimal"):
        marcas = {"n", "num", "numero", "nro", "conteo", "count", "nbr"}
        if "number" in tokens and "of" in tokens:  # number_of_policies
            marcas = marcas | {"number"}
        base = _traducir_tokens([t for t in tokens if t not in marcas and t not in ("de", "del", "of", "total",
                                                                                      "cantidad")])
        if any(t in marcas for t in tokens) and base:
            resultado = f"Cantidad de {base} por registro."
        elif any(t in ("total", "cantidad") for t in tokens) and base and not _buscar(base, base.replace(" ", "")):
            resultado = f"Cantidad de {base} por registro."

    # Forma de pago: método (PayPal, transferencia) o periodicidad (mensual, anual), según el contenido.
    if resultado is None and _p("forma pago", "metodo pago", "medio pago", "tipo pago").search(texto):
        bajos = {v.lower() for v in valores}
        if bajos and bajos <= _PERIODICIDADES:
            resultado = _GLOSARIO[_indice_por_palabra("periodicidad")][1]
        else:
            resultado = "Forma o método con que se realizó el pago."

    # «order_status» -> «Estado del pedido» (solo si la palabra de contexto y la cabeza son conocidas).
    if resultado is None:
        resultado = _componer(tokens)
        curado = resultado is not None

    # Glosario general por nombre.
    if resultado is None:
        encontrado = _buscar(texto, compacto)
        if encontrado:
            ganador, resultado = encontrado

    # Pista desde el contenido cuando el nombre no dijo nada.
    es_pista = False
    if resultado is None:
        pista = _pista_contenido(valores, tipo, n_q, unicos, filas, serie)
        if pista:
            resultado, es_pista, con_datos = pista, True, True

    # Por tipo de dato.
    if resultado is None:
        resultado = _DESCRIPCION_POR_TIPO.get(tipo, "Dato de {n}.")

    if not con_datos:
        resultado = resultado.replace("{n}", n_q)
    resultado = _primera_mayuscula(resultado)
    if resultado and resultado[-1] not in ".!?":
        resultado += "."

    # A qué se refiere el campo: «nombre_cliente» -> «Corresponde al cliente.»
    if ganador is not None and len(tokens) >= 2 and "Corresponde" not in resultado:
        sustantivo = _entidad(texto, excluir=ganador.palabras, para_componer=True)
        if sustantivo:
            resultado += f" Corresponde {_a_la_entidad(sustantivo)}."

    # Matices del nombre: bruto/neto, unitario, mensual, estimado, unidad de medida...
    if not es_pista:
        for frase in _calificadores(texto_norm, norm_base):
            if frase not in resultado:
                resultado += " " + frase

    extras = []
    if origen:
        extras.append(f"Viene de la tabla «{origen}».")
    if tratamiento and tratamiento.startswith(("Rellenados", "Celdas vacías convertidas", "Se eliminaron")):
        extras.append(f"Transformado en la limpieza: {tratamiento[0].lower() + tratamiento[1:]}.")
    return " ".join([resultado] + extras)


def _indice_por_palabra(palabra: str) -> int:
    for i, (patron, _) in enumerate(_GLOSARIO):
        if palabra in patron.palabras:
            return i
    raise KeyError(palabra)


def completar_descripciones(diccionario: pd.DataFrame, origenes: Optional[Dict[str, str]] = None,
                            df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Copia del diccionario con la «Descripción» redactada en las filas donde está vacía.
    Lo que la persona ya escribió no se toca. Si se pasa `df` (la tabla), las descripciones
    aprovechan también el contenido de cada columna."""
    if "Descripción" not in diccionario.columns or "Campo" not in diccionario.columns:
        return diccionario
    resultado = diccionario.copy()
    vacias = resultado["Descripción"].fillna("").astype(str).str.strip() == ""
    if not vacias.any():
        return resultado
    nombres_columnas = [str(c) for c in resultado["Campo"]]
    roles_inversos = {"identificador / llave": "id", "coordenada": "coordenada", "email": "email",
                      "teléfono": "telefono", "fecha": "fecha", "número": "numerica", "texto": "texto"}
    for idx in resultado.index[vacias]:
        fila = resultado.loc[idx]
        campo = fila["Campo"]
        origen = (origenes or {}).get(campo, "") or str(fila.get("Origen", "") or "")
        clasif = str(fila.get("Clasificación ejecutiva", "") or "")
        serie = df[campo] if df is not None and campo in df.columns else None
        unicos = fila.get("Valores únicos")
        resultado.at[idx, "Descripción"] = describir_campo(
            campo, roles_inversos.get(str(fila.get("Rol", "")), "texto"),
            str(fila.get("Tipo de dato", "Texto")), str(fila.get("Tratamiento de nulos", "") or ""),
            origen, es_llave=clasif.startswith("Llave"),
            serie=serie, ejemplos=str(fila.get("Rango o ejemplos", "") or ""),
            unicos=int(unicos) if pd.notna(unicos) and str(unicos).isdigit() else None,
            contexto=nombres_columnas)
    return resultado

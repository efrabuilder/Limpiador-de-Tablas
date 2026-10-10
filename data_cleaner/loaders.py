"""
Carga de datos desde distintas fuentes: CSV, Excel y SQL.
"""
from __future__ import annotations
import datetime
import io
import re
import pandas as pd


# -----------------------------------------------------------------------------
# Formatos aceptados en toda la app (web, escritorio, CLI, API y scripts).
# -----------------------------------------------------------------------------

EXTENSIONES_EXCEL = (".xlsx", ".xlsm", ".xltx", ".xltm", ".xls", ".xlsb", ".ods")
EXTENSIONES_CSV = (".csv", ".txt", ".tsv", ".tab")
EXTENSIONES_TABLA = EXTENSIONES_CSV + EXTENSIONES_EXCEL

# Motores de pandas para leer cada formato, en el orden en que se prueban.
_MOTORES_POR_EXTENSION = {
    ".xlsx": ("openpyxl",), ".xlsm": ("openpyxl",), ".xltx": ("openpyxl",), ".xltm": ("openpyxl",),
    ".xls": ("xlrd", "openpyxl"), ".xlsb": ("pyxlsb",), ".ods": ("odf",),
}
_PAQUETE_POR_MOTOR = {"xlrd": "xlrd", "pyxlsb": "pyxlsb", "odf": "odfpy", "openpyxl": "openpyxl"}


def tipos_para_selector(incluir_csv: bool = True, incluir_excel: bool = True) -> list:
    """Extensiones sin punto para st.file_uploader(type=...): ['csv', 'txt', 'xlsx', ...]."""
    extensiones = (EXTENSIONES_CSV if incluir_csv else ()) + (EXTENSIONES_EXCEL if incluir_excel else ())
    return [e.lstrip(".") for e in extensiones]


def patrones_para_dialogo(incluir_csv: bool = True, incluir_excel: bool = True) -> str:
    """Patrones para tkinter filedialog: '*.csv *.txt *.xlsx ...'."""
    extensiones = (EXTENSIONES_CSV if incluir_csv else ()) + (EXTENSIONES_EXCEL if incluir_excel else ())
    return " ".join(f"*{e}" for e in extensiones)


def load_csv(path, **kwargs) -> pd.DataFrame:
    """Carga un CSV (o TXT/TSV) a un DataFrame. Detecta solo la codificación (utf-8 con o sin BOM, utf-16,
    cp1252, latin-1) y el separador (coma, punto y coma, tabulador o barra vertical), salvo que se pase `sep`.
    `path` puede ser una ruta, un archivo abierto o un archivo subido."""
    texto = _leer_texto_csv(path)
    if "sep" not in kwargs and "delimiter" not in kwargs:
        texto, kwargs["sep"] = _texto_y_separador(texto)
    try:
        return pd.read_csv(io.StringIO(texto), **kwargs)
    except pd.errors.ParserError:  # filas con más celdas que el encabezado: el motor flexible las acepta
        kwargs.pop("low_memory", None)
        return pd.read_csv(io.StringIO(texto), engine="python", **kwargs)


# -----------------------------------------------------------------------------
# Deteccion de titulo(s) y notas al cargar una hoja de Excel: muchas hojas
# "reales" no empiezan la fila 0 con el encabezado -- traen antes un titulo
# de la hoja (ej. el nombre del negocio o del reporte, en una sola celda) y
# terminan con notas/pie de pagina (ej. "* Precios incluyen IVA") despues de
# la ultima fila de datos. Sin esto, pandas toma el titulo como encabezado
# (arruinando los nombres de columna) y las notas quedan como filas casi
# todas vacias -- ambos casos se reportaban como errores de calidad que en
# realidad no lo son.
# -----------------------------------------------------------------------------

def _fila_es_encabezado_candidata(fila: "pd.Series", total_columnas: int) -> bool:
    """True si `fila` (de un DataFrame leido con header=None) parece ser el
    encabezado real de la tabla, en vez de un titulo de la hoja.

    Un encabezado real normalmente: (a) tiene texto en al menos la mitad de
    las columnas de la tabla -- un titulo casi siempre ocupa una sola celda
    (o una celda combinada) y deja el resto de la fila vacia -- y (b) esos
    nombres son distintos entre si (los encabezados no se repiten).
    """
    no_nulos = fila.dropna()
    if len(no_nulos) < 2 or len(no_nulos) < max(2, total_columnas * 0.5):
        return False
    valores_texto = [str(v).strip() for v in no_nulos]
    if len(set(valores_texto)) != len(valores_texto):
        return False
    return True


def _detectar_fila_encabezado(crudo: pd.DataFrame, max_filas_buscar: int = 20) -> int:
    """Indice (0-based, dentro de `crudo`) de la fila que mas probablemente
    sea el encabezado real, saltandose titulos de la hoja al inicio. Si no
    se encuentra un candidato razonable en las primeras `max_filas_buscar`
    filas, se asume que la fila 0 ya es el encabezado (mismo comportamiento
    que antes), para no romper archivos que no traen titulo."""
    limite = min(max_filas_buscar, len(crudo))
    total_columnas = crudo.shape[1]
    for i in range(limite):
        if _fila_es_encabezado_candidata(crudo.iloc[i], total_columnas):
            return i
    return 0


def _recortar_notas_finales(df: pd.DataFrame) -> pd.DataFrame:
    """Elimina filas al FINAL de la tabla que parecen notas/pie de pagina
    (muy pocas celdas llenas comparadas con el resto de la tabla). Solo
    recorta filas sueltas al final: una fila dispersa en medio de los datos
    se deja intacta, porque ahi si puede ser un registro real con varios
    campos vacios."""
    if df.empty:
        return df
    conteo_no_nulos = df.notna().sum(axis=1)
    mediana = conteo_no_nulos.median()
    if not mediana or pd.isna(mediana):
        return df
    umbral = max(1, mediana * 0.3)
    ultimo_valido = len(df) - 1
    while ultimo_valido >= 0 and conteo_no_nulos.iloc[ultimo_valido] <= umbral:
        ultimo_valido -= 1
    return df.iloc[:ultimo_valido + 1]


def _limpiar_columnas_sin_nombre(df: pd.DataFrame) -> pd.DataFrame:
    """Renombra columnas sin nombre (celda de encabezado vacia, o el
    'Unnamed: N' que pandas asigna por defecto) a 'Columna_sin_nombre_N', y
    elimina las que ademas estan completamente vacias: normalmente son un
    artefacto de una celda de titulo combinada que "se corre" sobre
    columnas vecinas, no una columna real de datos."""
    df = df.copy()
    nuevas_columnas = []
    for i, col in enumerate(df.columns):
        nombre = "" if pd.isna(col) else str(col).strip()
        sin_nombre = nombre == "" or re.match(r'^unnamed:\s*\d+$', nombre, re.IGNORECASE)
        nuevas_columnas.append(f"Columna_sin_nombre_{i + 1}" if sin_nombre else nombre)
    vistos: dict = {}
    for i, nombre in enumerate(nuevas_columnas):  # encabezados repetidos: «precio», «precio_2», «precio_3»
        veces = vistos.get(nombre, 0) + 1
        vistos[nombre] = veces
        if veces > 1:
            nuevas_columnas[i] = f"{nombre}_{veces}"
    df.columns = nuevas_columnas
    columnas_vacias_sin_nombre = [
        c for c in df.columns
        if c.startswith("Columna_sin_nombre_") and df[c].isna().all()
    ]
    if columnas_vacias_sin_nombre:
        df = df.drop(columns=columnas_vacias_sin_nombre)
    return df


def _procesar_hoja_cruda(crudo: pd.DataFrame) -> pd.DataFrame:
    """Convierte una hoja leida sin encabezado (header=None) en la tabla
    final: detecta la fila de encabezado real (saltandose titulos), la usa
    como nombres de columna, limpia columnas sin nombre y recorta notas
    finales."""
    if crudo.empty:
        return crudo
    fila_encabezado = _detectar_fila_encabezado(crudo)
    encabezados = crudo.iloc[fila_encabezado]
    df = crudo.iloc[fila_encabezado + 1:].reset_index(drop=True)
    df.columns = encabezados
    df = _limpiar_columnas_sin_nombre(df)
    df = _recortar_notas_finales(df)
    return df.reset_index(drop=True)


def load_excel(path, sheet_name=None, detectar_encabezado: bool = True, **kwargs) -> pd.DataFrame:
    """
    Carga un archivo Excel (.xlsx/.xls/.xlsm) a un DataFrame.

    Por defecto (sheet_name=None) lee TODAS las hojas del archivo y las
    concatena en un único DataFrame, agregando la columna '_hoja' al
    inicio con el nombre de la hoja de origen de cada fila. Si se indica
    un sheet_name explícito, se conserva el comportamiento de pandas
    (una sola hoja, sin columna '_hoja').

    `detectar_encabezado=True` (por defecto): por cada hoja, antes de fijar
    los nombres de columna, se detecta si las primeras filas son en
    realidad un TÍTULO de la hoja (ej. el nombre del negocio o del reporte,
    en una sola celda) en vez del encabezado real, y si las últimas filas
    son NOTAS o pie de página (ej. "* Precios incluyen IVA") en vez de
    datos — en ambos casos se descartan para que no se marquen como
    errores de calidad (columna vacía, fila casi toda nula, etc.). Además,
    cualquier columna sin nombre (celda de encabezado vacía, o el
    "Unnamed: N" que pone pandas por defecto) se renombra a
    "Columna_sin_nombre_N", y si además está completamente vacía se
    elimina (suele ser un artefacto de una celda de título combinada que
    se extiende sobre columnas vecinas).

    Se desactiva automáticamente si se pasa un `header` explícito en
    kwargs (se respeta lo que pida quien llama, igual que antes de este
    parámetro).
    """
    if es_archivo_csv(_nombre_de(path)):  # un CSV en lugar de un libro: se lee como tabla de una sola hoja
        return load_csv(path)
    if "header" in kwargs:
        detectar_encabezado = False

    if not detectar_encabezado:
        hojas = _read_excel(path, sheet_name=sheet_name, **kwargs)
        if isinstance(hojas, dict):
            if not hojas:
                return pd.DataFrame()
            marcos = []
            for nombre_hoja, df_hoja in hojas.items():
                df_hoja = df_hoja.copy()
                df_hoja.insert(0, "_hoja", nombre_hoja)
                marcos.append(df_hoja)
            return pd.concat(marcos, ignore_index=True, sort=False)
        return hojas

    crudos = _read_excel(path, sheet_name=sheet_name, header=None, **kwargs)

    if not isinstance(crudos, dict):
        return _procesar_hoja_cruda(crudos)

    if not crudos:
        return pd.DataFrame()
    marcos = []
    for nombre_hoja, crudo in crudos.items():
        df_hoja = _procesar_hoja_cruda(crudo)
        df_hoja.insert(0, "_hoja", nombre_hoja)
        marcos.append(df_hoja)
    return pd.concat(marcos, ignore_index=True, sort=False)


def load_excel_hojas(path, hojas=None, detectar_encabezado: bool = True, **kwargs) -> dict:
    """
    Carga varias hojas de un Excel como DataFrames SEPARADOS (a diferencia
    de load_excel, que las concatena en una sola tabla). Util cuando cada
    hoja es una tabla distinta (ej. un libro con "Clientes", "Inventario",
    "Ventas") y se quiere escribir cada una a su propia tabla SQL.

    hojas=None (por defecto): carga TODAS las hojas del archivo.
    hojas=["Clientes", "Ventas"]: carga solo esas hojas, en ese orden.

    Devuelve un diccionario {nombre_hoja: DataFrame}, aplicando la misma
    deteccion de titulo/notas/columnas sin nombre que load_excel (ver
    detectar_encabezado). Si `path` es un CSV, devuelve {nombre_del_archivo: DataFrame}
    (una sola «hoja»).
    """
    if es_archivo_csv(_nombre_de(path)):
        return {nombre_tabla_de(path): load_csv(path)}
    if "header" in kwargs:
        detectar_encabezado = False

    if not detectar_encabezado:
        crudos = _read_excel(path, sheet_name=hojas, **kwargs)
        return crudos if isinstance(crudos, dict) else {hojas: crudos}

    crudos = _read_excel(path, sheet_name=hojas, header=None, **kwargs)
    if not isinstance(crudos, dict):
        crudos = {hojas: crudos}
    return {nombre: _procesar_hoja_cruda(df) for nombre, df in crudos.items()}


def load_tablas(origenes, hojas=None, detectar_encabezado: bool = True) -> dict:
    """Carga varias tablas como DataFrames SEPARADOS desde cualquier mezcla de archivos: de cada Excel
    (.xlsx, .xlsm, .xls, .xlsb, .ods...) sus hojas, y de cada CSV/TXT/TSV una tabla con el nombre del archivo.
    `origenes`: una ruta, un archivo subido, o una lista de ellos (también pares (nombre, archivo)).
    `hojas`: nombres a conservar (None = todas). Si dos archivos traen una hoja con el mismo nombre, se
    distinguen como «archivo__hoja». Devuelve {nombre: DataFrame}."""
    lista = origenes if isinstance(origenes, (list, tuple)) else [origenes]
    pares = [(o[0], o[1]) if isinstance(o, tuple) else (None, o) for o in lista]
    por_archivo = []
    for nombre, origen in pares:
        nombre = _nombre_de(origen, nombre)
        if es_archivo_csv(nombre):
            marcos = {nombre_tabla_de(origen, nombre): load_csv(origen)}
        else:
            marcos = load_excel_hojas(origen, hojas=None, detectar_encabezado=detectar_encabezado)
        por_archivo.append((nombre_tabla_de(origen, nombre), marcos))
    repetidos = {h for i, (_, m) in enumerate(por_archivo) for h in m
                 if any(h in m2 for j, (_, m2) in enumerate(por_archivo) if j != i)}
    salida = {}
    for archivo, marcos in por_archivo:
        for hoja, df in marcos.items():
            salida[f"{archivo}__{hoja}" if hoja in repetidos else hoja] = df
    if hojas is not None:
        faltan = [h for h in hojas if h not in salida]
        if faltan:
            raise ValueError(f"No se encontraron estas hojas o tablas: {', '.join(map(str, faltan))}. "
                             f"Disponibles: {', '.join(map(str, salida))}")
        salida = {h: salida[h] for h in hojas}
    return salida


def listar_tablas(origenes) -> list:
    """Nombres de las tablas que trae cada archivo (hojas de un Excel, o el nombre de un CSV), con el mismo
    criterio de nombres que load_tablas."""
    lista = origenes if isinstance(origenes, (list, tuple)) else [origenes]
    pares = [(o[0], o[1]) if isinstance(o, tuple) else (None, o) for o in lista]
    por_archivo = []
    for nombre, origen in pares:
        nombre = _nombre_de(origen, nombre)
        hojas = [nombre_tabla_de(origen, nombre)] if es_archivo_csv(nombre) else listar_hojas(origen, nombre)
        por_archivo.append((nombre_tabla_de(origen, nombre), hojas))
    repetidos = {h for i, (_, m) in enumerate(por_archivo) for h in m
                 if any(h in m2 for j, (_, m2) in enumerate(por_archivo) if j != i)}
    return [f"{archivo}__{h}" if h in repetidos else h for archivo, hojas in por_archivo for h in hojas]


# -----------------------------------------------------------------------------
# Lectura de archivos subidos (CSV o Excel) para las secciones de limpieza
# guiada y merge. Aceptan una ruta o un archivo subido de Streamlit.
# -----------------------------------------------------------------------------

def _como_buffer(origen):
    """Devuelve algo que pandas pueda leer varias veces: un BytesIO para
    archivos subidos, o la misma ruta si es un texto."""
    if hasattr(origen, "getvalue"):  # archivo subido de Streamlit
        return io.BytesIO(origen.getvalue())
    if hasattr(origen, "read") and hasattr(origen, "seek"):  # otro objeto tipo archivo
        origen.seek(0)
        return origen
    return origen  # ruta


def _nombre_de(origen, nombre=None) -> str:
    if nombre:
        return str(nombre)
    return str(getattr(origen, "name", origen))


def nombre_tabla_de(origen, nombre=None) -> str:
    """Nombre de una tabla a partir de su archivo: 'datos/ventas 2025.csv' -> 'ventas 2025'."""
    base = re.split(r"[\\/]", _nombre_de(origen, nombre))[-1]
    return base.rsplit(".", 1)[0] if "." in base else base


def es_archivo_excel(nombre: str) -> bool:
    """True para cualquier tipo de Excel: .xlsx, .xlsm, .xltx, .xltm, .xls, .xlsb y .ods."""
    return str(nombre).lower().endswith(EXTENSIONES_EXCEL)


def es_archivo_csv(nombre: str) -> bool:
    """True para .csv, .txt, .tsv y .tab."""
    return str(nombre).lower().endswith(EXTENSIONES_CSV)


def es_archivo_tabla(nombre: str) -> bool:
    return es_archivo_excel(nombre) or es_archivo_csv(nombre)


def _extension(nombre: str) -> str:
    nombre = str(nombre).lower()
    return "." + nombre.rsplit(".", 1)[-1] if "." in nombre else ""


def _bytes_de(origen) -> bytes:
    if isinstance(origen, bytes):
        return origen
    if isinstance(origen, str):
        with open(origen, "rb") as archivo:
            return archivo.read()
    if hasattr(origen, "getvalue"):
        return origen.getvalue()
    origen.seek(0)
    return origen.read()


def _sin_formato_excel(origen) -> bool:
    """True si el archivo no es un libro de Excel real (los «.xls» de algunos sistemas son HTML o texto)."""
    crudo = _bytes_de(origen)[:8]
    return not (crudo.startswith(b"PK") or crudo.startswith(b"\xd0\xcf\x11\xe0"))


def _leer_como_texto(origen, sheet_name=0, **kwargs):
    """Plan B para un «Excel» que en realidad es una página HTML o un texto separado: lo lee como tabla.
    Devuelve un DataFrame (o un dict {nombre: DataFrame} si sheet_name es None, como pd.read_excel)."""
    texto = _leer_texto_csv(origen)
    if re.search(r"<\s*table", texto, re.I):
        tablas = pd.read_html(io.StringIO(texto))
        if "header" in kwargs and kwargs["header"] is None:  # crudo: el encabezado pasa a ser la primera fila
            tablas = [pd.concat([pd.DataFrame([list(t.columns)]), t.set_axis(range(t.shape[1]), axis=1)],
                                ignore_index=True) for t in tablas]
        marcos = {f"Tabla{i + 1}": t for i, t in enumerate(tablas)}
    else:
        texto, separador = _texto_y_separador(texto)
        marcos = {"Hoja1": pd.read_csv(io.StringIO(texto), sep=separador, header=kwargs.get("header", 0))}
    if sheet_name is None:
        return marcos
    if isinstance(sheet_name, (list, tuple)):
        return {h: marcos[h] for h in sheet_name if h in marcos}
    if isinstance(sheet_name, int):
        return list(marcos.values())[sheet_name]
    return marcos[sheet_name]


def _read_excel(origen, sheet_name=0, **kwargs):
    """pd.read_excel que abre cualquier tipo de Excel: elige el motor según la extensión (.xls con xlrd,
    .xlsb con pyxlsb, .ods con odfpy, el resto con openpyxl), prueba los demás si el archivo no es lo que
    dice su extensión y, si ni siquiera es un libro (HTML o texto con extensión .xls), lo lee como tabla."""
    nombre = _nombre_de(origen)
    extension = _extension(nombre)
    motores = list(_MOTORES_POR_EXTENSION.get(extension, ())) or ["openpyxl", "xlrd", "pyxlsb", "odf"]
    motores += [m for m in ("openpyxl", "xlrd", "pyxlsb", "odf") if m not in motores] + [None]
    ultimo = None
    for motor in motores:
        try:
            fuente = (io.BytesIO(_bytes_de(origen)) if (motor == "openpyxl" or motor is None)
                      else _como_buffer(origen))
            return pd.read_excel(fuente, sheet_name=sheet_name, engine=motor, **kwargs)
        except ImportError as exc:
            ultimo = ImportError(f"Para leer este tipo de Excel instale «{_PAQUETE_POR_MOTOR.get(motor, motor)}»: "
                                 f"pip install {_PAQUETE_POR_MOTOR.get(motor, motor)}") if motor else exc
        except ValueError as exc:
            if "Worksheet named" in str(exc) or "Worksheet index" in str(exc) or "not found" in str(exc).lower() \
                    and "sheet" in str(exc).lower():
                raise
            ultimo = exc
        except Exception as exc:  # motor equivocado para este archivo: se prueba el siguiente
            ultimo = exc
    if _sin_formato_excel(origen):
        try:
            return _leer_como_texto(origen, sheet_name, **kwargs)
        except Exception as exc:
            ultimo = exc
    raise ValueError(f"No se pudo leer el archivo de Excel «{nombre}»: {ultimo}")


def listar_hojas(origen, nombre=None) -> list:
    """Nombres de las hojas de un libro Excel (cualquier tipo), en el orden del libro.
    Para un CSV no hay hojas, asi que devuelve una lista vacia."""
    nombre = _nombre_de(origen, nombre)
    if not es_archivo_excel(nombre):
        return []
    extension = _extension(nombre)
    motores = list(_MOTORES_POR_EXTENSION.get(extension, ())) + [m for m in ("openpyxl", "xlrd", "pyxlsb", "odf")]
    ultimo = None
    for motor in dict.fromkeys(motores):
        try:
            fuente = io.BytesIO(_bytes_de(origen)) if motor == "openpyxl" else _como_buffer(origen)
            with pd.ExcelFile(fuente, engine=motor) as libro:
                return list(libro.sheet_names)
        except Exception as exc:
            ultimo = exc
    if _sin_formato_excel(origen):
        try:
            return list(_leer_como_texto(origen, None, header=None))
        except Exception as exc:
            ultimo = exc
    raise ValueError(f"No se pudo abrir el archivo de Excel «{nombre}»: {ultimo}")


def _valor_a_texto(v) -> str:
    """Un valor de celda como texto. Vacio/nulo -> ''; 10115.0 -> '10115'
    (el .0 lo mete Excel); fechas sin hora -> 'aaaa-mm-dd'."""
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return ""
    if isinstance(v, datetime.datetime):  # incluye pd.Timestamp
        sin_hora = (v.hour, v.minute, v.second, v.microsecond) == (0, 0, 0, 0)
        return v.strftime("%Y-%m-%d" if sin_hora else "%Y-%m-%d %H:%M:%S")
    if isinstance(v, datetime.date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def tabla_a_texto(df: pd.DataFrame) -> pd.DataFrame:
    """Convierte TODAS las columnas a texto y deja los nulos como ''. Es el
    equivalente a leer un CSV con dtype=str y keep_default_na=False: se ve
    el archivo tal cual esta, sin que pandas adivine tipos ni nulos."""
    salida = df.copy()
    for col in salida.columns:
        serie = salida[col]
        if pd.api.types.is_datetime64_any_dtype(serie):
            con_valor = serie.dropna()
            solo_fecha = bool((con_valor.dt.normalize() == con_valor).all())
            serie = serie.dt.strftime("%Y-%m-%d" if solo_fecha else "%Y-%m-%d %H:%M:%S")
        salida[col] = serie.map(_valor_a_texto)
    return salida


def leer_tabla_subida(origen, nombre=None, hoja=None, como_texto: bool = True,
                      detectar_encabezado: bool = True) -> pd.DataFrame:
    """Lee un CSV o una hoja de un Excel como DataFrame.

    - CSV: prueba utf-8 (con o sin BOM) y luego latin-1; detecta solo el
      separador (coma, punto y coma, tabulador o barra vertical).
    - Excel: lee la hoja `hoja` (si el libro tiene una sola, no hace falta).
      Con `detectar_encabezado` se saltan titulos al inicio y notas al
      final de la hoja (ver load_excel).
    - `como_texto=True`: todo queda como texto y los nulos como '' (ver
      tabla_a_texto), lo recomendado para diagnosticar y limpiar.
    """
    nombre = _nombre_de(origen, nombre)

    if es_archivo_excel(nombre):
        hojas = listar_hojas(origen, nombre)
        if hoja is None:
            if len(hojas) != 1:
                raise ValueError("El libro tiene varias hojas: indique cual leer.")
            hoja = hojas[0]
        df = load_excel(_como_buffer(origen), sheet_name=hoja,
                        detectar_encabezado=detectar_encabezado)
        return tabla_a_texto(df) if como_texto else df

    # CSV: se decodifica el texto y se detecta el separador mirando el encabezado
    texto, separador = _texto_y_separador(_leer_texto_csv(origen))
    opciones = {"sep": separador}
    if como_texto:
        opciones.update(dtype=str, keep_default_na=False)
    try:
        try:
            return pd.read_csv(io.StringIO(texto), **opciones)
        except pd.errors.ParserError:
            return pd.read_csv(io.StringIO(texto), engine="python", **opciones)
    except Exception as exc:
        raise ValueError(f"No se pudo leer el CSV: {exc}") from exc


def _leer_texto_csv(origen) -> str:
    """Contenido del CSV como texto: reconoce utf-8 (con o sin BOM), utf-16 (los «Texto Unicode» de Excel),
    cp1252 y, si nada de eso, latin-1 (que nunca falla)."""
    crudo = _bytes_de(origen)
    if crudo.startswith((b"\xff\xfe", b"\xfe\xff")):
        return crudo.decode("utf-16")
    for codificacion in ("utf-8-sig", "cp1252"):
        try:
            return crudo.decode(codificacion)
        except UnicodeDecodeError:
            continue
    return crudo.decode("latin-1")


def _texto_y_separador(texto: str):
    """(texto, separador). Respeta la línea «sep=;» que Excel pone al inicio de algunos CSV (y la quita)."""
    lineas = texto.splitlines(keepends=True)
    if lineas and re.match(r"^sep=.$", lineas[0].strip(), re.I):
        return "".join(lineas[1:]), lineas[0].strip()[4]
    return texto, _detectar_separador(texto)


def _detectar_separador(texto: str) -> str:
    """Separador del CSV: el que mas se repite en la primera linea con
    contenido (coma, punto y coma, tabulador o barra vertical). Si ninguno
    aparece (una sola columna), usa coma."""
    primera = next((linea for linea in texto.splitlines() if linea.strip()), "")
    conteos = {sep: primera.count(sep) for sep in (",", ";", "\t", "|")}
    mejor = max(conteos, key=conteos.get)
    return mejor if conteos[mejor] > 0 else ","


def tabla_a_bytes(df: pd.DataFrame, formato: str = "csv", nombre_hoja: str = "Datos") -> bytes:
    """El DataFrame como bytes listos para descargar, en 'csv' (utf-8 con BOM,
    para que Excel abra bien los acentos) o 'xlsx'."""
    if formato == "csv":
        return df.to_csv(index=False).encode("utf-8-sig")
    if formato == "xlsx":
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as escritor:
            df.to_excel(escritor, index=False, sheet_name=str(nombre_hoja)[:31] or "Datos")
        return buffer.getvalue()
    raise ValueError(f"Formato no soportado: {formato}")


def load_sql(connection_string: str, query: str = None, table_name: str = None) -> pd.DataFrame:
    """
    Carga datos desde una base de datos SQL usando SQLAlchemy.

    Se debe indicar una consulta (query) o un nombre de tabla (table_name).
    Ejemplos de connection_string:
      - SQLite:   "sqlite:///ruta/al/archivo.db"
      - MySQL:    "mysql+pymysql://usuario:clave@host/basededatos"
      - Postgres: "postgresql+psycopg2://usuario:clave@host/basededatos"
    """
    from sqlalchemy import create_engine

    if not query and not table_name:
        raise ValueError("Debe indicar 'query' o 'table_name' para cargar datos SQL.")

    engine = create_engine(connection_string)
    with engine.connect() as conn:
        if query:
            return pd.read_sql_query(query, conn)
        return pd.read_sql_table(table_name, conn)


def load_table(path_or_conn: str, kind: str = "auto", **kwargs) -> pd.DataFrame:
    """
    Punto de entrada único: detecta o recibe el tipo de fuente ('csv', 'excel', 'sql')
    y delega en el loader correspondiente. Acepta CSV/TXT/TSV y cualquier Excel
    (.xlsx, .xlsm, .xls, .xlsb, .ods...); si `kind` no coincide con la extensión
    del archivo, manda la extensión.
    """
    if kind in ("auto", "csv", "excel") and isinstance(path_or_conn, str):
        if es_archivo_csv(path_or_conn):
            kind = "csv"
        elif es_archivo_excel(path_or_conn):
            kind = "excel"
    if kind == "auto":
        lower = path_or_conn.lower()
        if lower.startswith(("sqlite:", "mysql", "postgresql", "postgres", "mssql")):
            kind = "sql"
        else:
            raise ValueError(
                "No se pudo detectar el tipo de archivo automáticamente. Use un archivo "
                f"{', '.join(EXTENSIONES_TABLA)} o indique kind='csv' | 'excel' | 'sql'."
            )

    if kind == "csv":
        return load_csv(path_or_conn, **kwargs)
    if kind == "excel":
        return load_excel(path_or_conn, **kwargs)
    if kind == "sql":
        return load_sql(path_or_conn, **kwargs)

    raise ValueError(f"Tipo de fuente no soportado: {kind}")

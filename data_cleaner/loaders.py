"""
Carga de datos desde distintas fuentes: CSV, Excel y SQL.
"""
from __future__ import annotations
import datetime
import io
import re
import pandas as pd


def load_csv(path: str, **kwargs) -> pd.DataFrame:
    """Carga un archivo CSV a un DataFrame."""
    return pd.read_csv(path, **kwargs)


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
    if "header" in kwargs:
        detectar_encabezado = False

    if not detectar_encabezado:
        hojas = pd.read_excel(path, sheet_name=sheet_name, **kwargs)
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

    crudos = pd.read_excel(path, sheet_name=sheet_name, header=None, **kwargs)

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
    detectar_encabezado).
    """
    if "header" in kwargs:
        detectar_encabezado = False

    if not detectar_encabezado:
        crudos = pd.read_excel(path, sheet_name=hojas, **kwargs)
        return crudos if isinstance(crudos, dict) else {hojas: crudos}

    crudos = pd.read_excel(path, sheet_name=hojas, header=None, **kwargs)
    if not isinstance(crudos, dict):
        crudos = {hojas: crudos}
    return {nombre: _procesar_hoja_cruda(df) for nombre, df in crudos.items()}


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


def es_archivo_excel(nombre: str) -> bool:
    return str(nombre).lower().endswith((".xlsx", ".xlsm", ".xls"))


def es_archivo_csv(nombre: str) -> bool:
    return str(nombre).lower().endswith((".csv", ".txt"))


def listar_hojas(origen) -> list:
    """Nombres de las hojas de un libro Excel, en el orden del libro.
    Para un CSV no hay hojas, asi que devuelve una lista vacia."""
    nombre = _nombre_de(origen)
    if not es_archivo_excel(nombre):
        return []
    with pd.ExcelFile(_como_buffer(origen)) as libro:
        return list(libro.sheet_names)


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
        hojas = listar_hojas(origen)
        if hoja is None:
            if len(hojas) != 1:
                raise ValueError("El libro tiene varias hojas: indique cual leer.")
            hoja = hojas[0]
        df = load_excel(_como_buffer(origen), sheet_name=hoja,
                        detectar_encabezado=detectar_encabezado)
        return tabla_a_texto(df) if como_texto else df

    # CSV: se decodifica el texto y se detecta el separador mirando el encabezado
    texto = _leer_texto_csv(origen)
    opciones = {"sep": _detectar_separador(texto)}
    if como_texto:
        opciones.update(dtype=str, keep_default_na=False)
    try:
        return pd.read_csv(io.StringIO(texto), **opciones)
    except Exception as exc:
        raise ValueError(f"No se pudo leer el CSV: {exc}") from exc


def _leer_texto_csv(origen) -> str:
    """Contenido del CSV como texto: prueba utf-8 (con o sin BOM) y, si no
    se puede, latin-1 (que nunca falla)."""
    buffer = _como_buffer(origen)
    if isinstance(buffer, str):
        with open(buffer, "rb") as archivo:
            crudo = archivo.read()
    else:
        crudo = buffer.read()
    try:
        return crudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        return crudo.decode("latin-1")


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
    y delega en el loader correspondiente.
    """
    if kind == "auto":
        lower = path_or_conn.lower()
        if lower.endswith(".csv"):
            kind = "csv"
        elif lower.endswith((".xlsx", ".xls", ".xlsm")):
            kind = "excel"
        elif lower.startswith(("sqlite:", "mysql", "postgresql", "postgres")):
            kind = "sql"
        else:
            raise ValueError(
                "No se pudo detectar el tipo de archivo automáticamente. "
                "Indique kind='csv' | 'excel' | 'sql'."
            )

    if kind == "csv":
        return load_csv(path_or_conn, **kwargs)
    if kind == "excel":
        return load_excel(path_or_conn, **kwargs)
    if kind == "sql":
        return load_sql(path_or_conn, **kwargs)

    raise ValueError(f"Tipo de fuente no soportado: {kind}")

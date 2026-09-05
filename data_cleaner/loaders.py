"""
Carga de datos desde distintas fuentes: CSV, Excel y SQL.
"""
from __future__ import annotations
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

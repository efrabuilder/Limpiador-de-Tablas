"""
Carga de datos desde distintas fuentes: CSV, Excel y SQL.
"""
from __future__ import annotations
import pandas as pd


def load_csv(path: str, **kwargs) -> pd.DataFrame:
    """Carga un archivo CSV a un DataFrame."""
    return pd.read_csv(path, **kwargs)


def load_excel(path, sheet_name=None, **kwargs) -> pd.DataFrame:
    """
    Carga un archivo Excel (.xlsx/.xls/.xlsm) a un DataFrame.

    Por defecto (sheet_name=None) lee TODAS las hojas del archivo y las
    concatena en un único DataFrame, agregando la columna '_hoja' al
    inicio con el nombre de la hoja de origen de cada fila. Si se indica
    un sheet_name explícito, se conserva el comportamiento de pandas
    (una sola hoja, sin columna '_hoja').
    """
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

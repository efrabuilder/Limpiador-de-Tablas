"""
Exporta el DataFrame limpio al formato deseado: CSV, Excel o SQL.
"""
from __future__ import annotations
import pandas as pd


def exportar_csv(df: pd.DataFrame, ruta_salida: str) -> str:
    df.to_csv(ruta_salida, index=False)
    return ruta_salida


def exportar_excel(df: pd.DataFrame, ruta_salida: str, sheet_name: str = "Datos_Limpios") -> str:
    df.to_excel(ruta_salida, index=False, sheet_name=sheet_name)
    return ruta_salida


def exportar_sql(df: pd.DataFrame, connection_string: str, table_name: str,
                  if_exists: str = "replace") -> str:
    from sqlalchemy import create_engine
    engine = create_engine(connection_string)
    df.to_sql(table_name, engine, if_exists=if_exists, index=False)
    return f"Tabla '{table_name}' escrita en {connection_string}"


def exportar(df: pd.DataFrame, ruta_o_conn: str, kind: str = "auto", **kwargs) -> str:
    if kind == "auto":
        lower = ruta_o_conn.lower()
        if lower.endswith(".csv"):
            kind = "csv"
        elif lower.endswith((".xlsx", ".xls")):
            kind = "excel"
        elif lower.startswith(("sqlite:", "mysql", "postgresql", "postgres")):
            kind = "sql"
        else:
            raise ValueError("No se pudo detectar el formato de salida; indique kind explícitamente.")

    if kind == "csv":
        return exportar_csv(df, ruta_o_conn)
    if kind == "excel":
        return exportar_excel(df, ruta_o_conn, **kwargs)
    if kind == "sql":
        return exportar_sql(df, ruta_o_conn, **kwargs)
    raise ValueError(f"Formato de salida no soportado: {kind}")

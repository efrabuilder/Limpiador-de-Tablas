#!/usr/bin/env python3
"""
Limpiador de Tablas — CLI interactivo
======================================
Carga una tabla (CSV, Excel o SQL), detecta valores atípicos y errores,
permite elegir qué hacer con cada tipo de problema, genera un reporte
detallado y exporta el archivo limpio.

Uso:
    python main.py                 -> modo interactivo (recomendado)
    python main.py --demo          -> corre con datos de ejemplo, sin preguntas
"""
from __future__ import annotations
import os
import sys
import argparse
import pandas as pd

from data_cleaner import (
    load_table, analizar, limpiar, DEFAULT_CONFIG,
    construir_reporte, exportar_reporte_excel, imprimir_resumen_consola, exportar,
)

OPCIONES_ACCION = {
    "faltante": ["reemplazar_media", "reemplazar_mediana", "reemplazar_moda",
                 "valor_fijo", "eliminar_fila", "marcar_solo"],
    "duplicado": ["eliminar_fila", "marcar_solo"],
    "atipico": ["limitar", "reemplazar_mediana", "reemplazar_media",
                "eliminar_fila", "marcar_solo"],
    "tipo_invalido": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    # Los 6 chequeos siguientes (ver data_cleaner/analyzer.py) antes no
    # aparecian aqui: analizar() ya los ejecuta por defecto, pero al no
    # estar en este diccionario, elegir_config_interactiva() los saltaba en
    # silencio y quedaban siempre en "marcar_solo" (el valor por defecto de
    # DEFAULT_CONFIG) sin que la persona pudiera elegir otra accion, a
    # diferencia de cli.py/app.py/desktop_app.py que si las exponen.
    "fecha_invalida": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "email_invalido": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "telefono_invalido": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "id_duplicado": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "formula_incorrecta": ["usar_sugerido", "eliminar_fila", "valor_fijo", "marcar_solo"],
    "texto_inconsistente": ["usar_sugerido", "eliminar_fila", "valor_fijo", "marcar_solo"],
}

NOMBRES_TIPO = {
    "faltante": "Valores faltantes (vacíos/nulos)",
    "duplicado": "Filas duplicadas",
    "atipico": "Valores atípicos (outliers)",
    "tipo_invalido": "Errores de tipo (texto en columna numérica)",
    "fecha_invalida": "Fechas inválidas/fuera de rango",
    "email_invalido": "Correos inválidos",
    "telefono_invalido": "Teléfonos inválidos",
    "id_duplicado": "IDs duplicados",
    "formula_incorrecta": "Total ≠ Cantidad × Precio",
    "texto_inconsistente": "Variantes/errores de tipeo de texto",
}


def preguntar(mensaje: str, opciones: list = None, defecto: str = None) -> str:
    if opciones:
        print(f"\n{mensaje}")
        for i, op in enumerate(opciones, 1):
            marca = "  (por defecto)" if op == defecto else ""
            print(f"  {i}. {op}{marca}")
        while True:
            resp = input(f"Elija una opción [1-{len(opciones)}]"
                          f"{' (Enter = por defecto)' if defecto else ''}: ").strip()
            if not resp and defecto:
                return defecto
            if resp.isdigit() and 1 <= int(resp) <= len(opciones):
                return opciones[int(resp) - 1]
            print("Opción inválida, intente de nuevo.")
    else:
        resp = input(f"{mensaje} ").strip()
        return resp or defecto


def cargar_interactivo() -> tuple[pd.DataFrame, str]:
    print("\n¿Qué tipo de fuente desea analizar?")
    tipo = preguntar("", ["csv", "excel", "sql"], defecto="csv")

    if tipo in ("csv", "excel"):
        ruta = preguntar(f"Ingrese la ruta del archivo {tipo.upper()}:")
        df = load_table(ruta, kind=tipo)
        return df, ruta
    else:
        conn = preguntar("Ingrese el connection string "
                          "(ej: sqlite:///datos.db):")
        modo = preguntar("¿Cargar por 'query' o por 'tabla'?", ["query", "tabla"], defecto="tabla")
        if modo == "query":
            query = preguntar("Ingrese la consulta SQL:")
            df = load_table(conn, kind="sql", query=query)
        else:
            tabla = preguntar("Ingrese el nombre de la tabla:")
            df = load_table(conn, kind="sql", table_name=tabla)
        return df, f"{conn} [{modo}]"


def elegir_config_interactiva(resultado) -> tuple[dict, dict]:
    config = {}
    valores_fijos = {}
    resumen = resultado.por_tipo()
    print("\n--- Configuración de acciones por tipo de problema ---")
    for tipo, cantidad in resumen.items():
        if tipo not in OPCIONES_ACCION:
            continue
        print(f"\n> {NOMBRES_TIPO.get(tipo, tipo)}: {cantidad} encontrados")
        accion = preguntar(
            "¿Qué acción desea aplicar?",
            OPCIONES_ACCION[tipo],
            defecto=DEFAULT_CONFIG.get(tipo),
        )
        config[tipo] = accion

        if accion == "valor_fijo":
            columnas_afectadas = sorted({
                issue.columna for issue in resultado.issues
                if issue.tipo == tipo and issue.columna
            })
            for col in columnas_afectadas:
                if col in valores_fijos:
                    continue
                valor = preguntar(f"  Valor fijo de reemplazo para la columna '{col}':")
                valores_fijos[col] = valor

    return config, valores_fijos


def main():
    parser = argparse.ArgumentParser(description="Limpiador de tablas con detección de atípicos.")
    parser.add_argument("--demo", action="store_true", help="Ejecuta con datos de ejemplo, sin preguntas.")
    parser.add_argument("--input", help="Ruta del archivo de entrada (csv/xlsx).")
    parser.add_argument("--outdir", default="salida", help="Carpeta de salida.")
    args = parser.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    if args.demo:
        ruta = args.input or "ejemplo_datos.csv"
        df = load_table(ruta, kind="auto")
        nombre_fuente = ruta
        config = DEFAULT_CONFIG
        metodo_atipicos = "iqr"
        paises_telefono = None
    else:
        df, nombre_fuente = cargar_interactivo()
        metodo_atipicos = preguntar(
            "\n¿Qué método desea usar para detectar valores atípicos?",
            ["iqr", "zscore", "ambos"], defecto="iqr",
        )
        paises_telefono_txt = preguntar(
            "\nPaís(es) para validar el largo de los teléfonos/celulares "
            "(coma-separados, ej. 'cr,mexico'; Enter = rango internacional amplio, 7-15 dígitos):",
            defecto="",
        )
        paises_telefono = [p.strip() for p in paises_telefono_txt.split(",") if p.strip()] or None

    print(f"\nTabla cargada: {len(df)} filas x {len(df.columns)} columnas.")

    resultado = analizar(df, metodo_atipicos=metodo_atipicos, paises_telefono=paises_telefono)
    imprimir_resumen_consola(resultado)

    if not resultado.issues:
        print("No se encontraron problemas. No es necesario limpiar la tabla.")
        return

    valores_fijos = {}
    if not args.demo:
        config, valores_fijos = elegir_config_interactiva(resultado)

    df_limpio, registro = limpiar(df, resultado.issues, config=config, valores_fijos=valores_fijos)

    tablas_reporte = construir_reporte(resultado, registro, nombre_fuente=nombre_fuente)

    ruta_reporte = os.path.join(args.outdir, "reporte_calidad_datos.xlsx")
    exportar_reporte_excel(tablas_reporte, ruta_reporte)

    if not args.demo:
        formato_salida = preguntar(
            "\n¿En qué formato desea el archivo limpio?", ["csv", "excel"], defecto="excel"
        )
    else:
        formato_salida = "excel"

    ext = "csv" if formato_salida == "csv" else "xlsx"
    ruta_limpio = os.path.join(args.outdir, f"datos_limpios.{ext}")
    exportar(df_limpio, ruta_limpio, kind=formato_salida)

    print("\n✅ Proceso completado.")
    print(f"   Archivo limpio:  {ruta_limpio}")
    print(f"   Reporte:         {ruta_reporte}")
    print(f"   Filas finales:   {len(df_limpio)} (originales: {len(df)})")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nProceso cancelado por el usuario.")
        sys.exit(1)

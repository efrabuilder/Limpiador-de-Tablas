# -*- coding: utf-8 -*-
"""
generar_m.py
============
Script para generar el codigo M 100% nativo (sin Python.Execute) a partir
de un archivo de datos, usando data_cleaner/exportador_m.py.

COMO USARLO:
    1. Coloque este archivo en la raiz del proyecto (junto a app.py, cli.py).
    2. Edite las variables de configuracion mas abajo (ARCHIVO_ENTRADA,
       HOJA, NOMBRE_PASO_ANTERIOR, y las acciones por regla).
    3. Ejecute:  python generar_m.py
    4. Se crea "codigo_generado.m" en la misma carpeta. Abralo, copie todo
       el contenido y peguelo en el Editor avanzado de Power Query.
"""
import argparse
import pandas as pd
from data_cleaner.exportador_m import generar_editor_m_puro
from data_cleaner.loaders import load_table

# -----------------------------------------------------------------------------
# CONFIGURACION: ajuste segun su archivo y como quiere corregir cada cosa.
# -----------------------------------------------------------------------------
ARCHIVO_ENTRADA = "Jardineria_Datos_X.xlsx"   # su archivo CSV o cualquier Excel
HOJA = "Ventas_Jardineria"                     # nombre de la hoja (None si es .csv)
NOMBRE_PASO_ANTERIOR = "Tipo de columna cambiado"  # el paso de Power Query que entrega la tabla
ARCHIVO_SALIDA = "codigo_generado.m"

# Tambien se pueden pasar por la linea de comandos, sin editar este archivo:
#   python generar_m.py --input mis_datos.csv [--hoja Hoja1] [--paso "Tipo cambiado"] [--salida codigo.m]
_parser = argparse.ArgumentParser(description="Genera el codigo M (Power Query) de limpieza de una tabla.")
_parser.add_argument("--input", "-i", default=ARCHIVO_ENTRADA, help="CSV o Excel de entrada.")
_parser.add_argument("--hoja", default=HOJA, help="Hoja del libro Excel (se ignora en CSV).")
_parser.add_argument("--paso", default=NOMBRE_PASO_ANTERIOR, help="Paso anterior de Power Query.")
_parser.add_argument("--salida", "-o", default=ARCHIVO_SALIDA, help="Archivo .m a crear.")
_args = _parser.parse_args()
ARCHIVO_ENTRADA, HOJA, NOMBRE_PASO_ANTERIOR, ARCHIVO_SALIDA = _args.input, _args.hoja, _args.paso, _args.salida

# Acepta CSV/TXT/TSV y cualquier Excel (.xlsx, .xlsm, .xls, .xlsb, .ods)
_es_csv = ARCHIVO_ENTRADA.lower().endswith((".csv", ".txt", ".tsv", ".tab"))
df = load_table(ARCHIVO_ENTRADA) if (_es_csv or not HOJA) else load_table(ARCHIVO_ENTRADA, sheet_name=HOJA)

codigo_m = generar_editor_m_puro(
    df,
    config={
        "faltante": "reemplazar_mediana",
        "duplicado": "eliminar_fila",
        "atipico": "limitar",
        "tipo_invalido": "marcar_solo",
    },
    nombre_paso_anterior=NOMBRE_PASO_ANTERIOR,
    fecha_invalida="valor_fijo",
    email_invalido="marcar_solo",
    telefono_invalido="valor_fijo",
    # Antes el rango de digitos de telefono era fijo (8 digitos, CR) por
    # defecto en toda la libreria. Ahora el default es internacional amplio
    # (7-15 digitos), asi que para este dataset costarricense se especifica
    # el pais explicitamente (acepta 8 digitos locales, o 9-11 con codigo
    # de pais tipo "+506" adelante). Para otro proyecto: cambiar/agregar
    # paises aqui, ej. paises_telefono=["mexico"] o ["cr", "mexico"].
    paises_telefono=["cr"],
    primeros_digitos_telefono_validos=["2", "4", "5", "6", "7", "8"],  # None si no es Costa Rica
    id_duplicado="marcar_solo",
    formula_incorrecta="marcar_solo",
    texto_inconsistente="usar_sugerido",
)

with open(ARCHIVO_SALIDA, "w", encoding="utf-8") as f:
    f.write(codigo_m)

print(f"Listo. Se genero '{ARCHIVO_SALIDA}'.")
print(f"Filas analizadas: {len(df)} | Columnas: {len(df.columns)}")

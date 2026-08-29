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
import pandas as pd
from data_cleaner.exportador_m import generar_editor_m_puro

# -----------------------------------------------------------------------------
# CONFIGURACION: ajuste segun su archivo y como quiere corregir cada cosa.
# -----------------------------------------------------------------------------
ARCHIVO_ENTRADA = "Jardineria_Datos_X.xlsx"   # su archivo .xlsx o .csv
HOJA = "Ventas_Jardineria"                     # nombre de la hoja (None si es .csv)
NOMBRE_PASO_ANTERIOR = "Tipo de columna cambiado"  # el paso de Power Query que entrega la tabla

if ARCHIVO_ENTRADA.lower().endswith(".csv"):
    df = pd.read_csv(ARCHIVO_ENTRADA)
else:
    df = pd.read_excel(ARCHIVO_ENTRADA, sheet_name=HOJA)

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
    primeros_digitos_telefono_validos=["2", "4", "5", "6", "7", "8"],  # None si no es Costa Rica
    id_duplicado="marcar_solo",
    formula_incorrecta="marcar_solo",
    texto_inconsistente="usar_sugerido",
)

with open("codigo_generado.m", "w", encoding="utf-8") as f:
    f.write(codigo_m)

print("Listo. Se genero 'codigo_generado.m' junto a este script.")
print(f"Filas analizadas: {len(df)} | Columnas: {len(df.columns)}")

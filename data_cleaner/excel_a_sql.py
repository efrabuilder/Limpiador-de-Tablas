# -*- coding: utf-8 -*-
"""
excel_a_sql.py
==============
Carga varias hojas de un Excel y las escribe en SQL como un modelo de
datos en ESTRELLA o COPO DE NIEVE: una (o varias) tabla(s) de HECHOS
(fact) conectadas a tablas de DIMENSION mediante llave primaria (PK) /
llave foranea (FK).

COMO USARLO:
    1. Coloque este archivo en la raiz del proyecto (junto a app.py, cli.py,
       generar_m.py).
    2. Edite ARCHIVO_EXCEL, CADENA_CONEXION y el diccionario MODELO mas abajo
       (nombre de tabla SQL -> hoja de origen, llave primaria y llaves
       foraneas). Vea data_cleaner/modelo_sql.py para la forma exacta.
    3. Ejecute:  python excel_a_sql.py
    4. Se crea "diagrama_modelo.dot" junto a este script: pegue su
       contenido en https://dreampuf.github.io/GraphvizOnline para verlo,
       o ábralo con Graphviz si lo tiene instalado.
"""
from data_cleaner.loaders import load_excel_hojas
from data_cleaner.modelo_sql import aplicar_modelo_sql, generar_dot_modelo

# -----------------------------------------------------------------------------
# CONFIGURACION
# -----------------------------------------------------------------------------
ARCHIVO_EXCEL = "mi_archivo.xlsx"

# Cadena de conexion SQLAlchemy. Ejemplo SQL Server con autenticacion de
# Windows (mismo patron que ya usan app.py / desktop_app.py):
CADENA_CONEXION = (
    "mssql+pyodbc://@LENOVO-EFRAIN-S\\MSSQLSERVER2025/super_San_Pascual"
    "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes"
    "&TrustServerCertificate=yes&trusted_connection=yes"
)

# "replace" reemplaza la tabla si ya existe; "append" agrega filas encima.
# Con PK/FK conviene "replace": con "append" las restricciones pueden
# fallar si la tabla ya tiene valores repetidos o nulos en esa columna.
SI_EXISTE = "replace"

# -----------------------------------------------------------------------------
# MODELO: nombre de tabla SQL -> hoja de origen, llave primaria y llaves
# foraneas. Ejemplo de modelo ESTRELLA (una tabla de hechos, dimensiones
# alrededor). Ajuste los nombres de hoja/columna a los de su archivo.
# -----------------------------------------------------------------------------
MODELO = {
    "dim_clientes": {
        "hoja": "Clientes",
        "clave_primaria": "id_cliente",
    },
    "dim_productos": {
        "hoja": "Productos",
        "clave_primaria": "id_producto",
    },
    "fact_ventas": {
        "hoja": "Ventas",
        "clave_primaria": "id_venta",
        "claves_foraneas": [
            {"columna": "id_cliente", "tabla_referencia": "dim_clientes",
             "columna_referencia": "id_cliente"},
            {"columna": "id_producto", "tabla_referencia": "dim_productos",
             "columna_referencia": "id_producto"},
        ],
    },
    # Para COPO DE NIEVE: agregue una dimension que a su vez referencia a
    # OTRA dimension, ej.:
    #
    # "dim_categorias": {"hoja": "Categorias", "clave_primaria": "id_categoria"},
    #
    # y en dim_productos agregue su propia lista de claves_foraneas:
    #
    # "dim_productos": {
    #     "hoja": "Productos",
    #     "clave_primaria": "id_producto",
    #     "claves_foraneas": [
    #         {"columna": "id_categoria", "tabla_referencia": "dim_categorias",
    #          "columna_referencia": "id_categoria"},
    #     ],
    # },
}


if __name__ == "__main__":
    hojas_necesarias = sorted({definicion["hoja"] for definicion in MODELO.values()})
    hojas_cargadas = load_excel_hojas(ARCHIVO_EXCEL, hojas=hojas_necesarias)

    print(f"Hojas cargadas: {', '.join(hojas_necesarias)}\n")

    for mensaje in aplicar_modelo_sql(MODELO, hojas_cargadas, CADENA_CONEXION, if_exists=SI_EXISTE):
        print(mensaje)

    with open("diagrama_modelo.dot", "w", encoding="utf-8") as f:
        f.write(generar_dot_modelo(MODELO))

    print(
        "\nDiagrama guardado en 'diagrama_modelo.dot' "
        "(péguelo en https://dreampuf.github.io/GraphvizOnline para verlo)."
    )
    print("Listo.")

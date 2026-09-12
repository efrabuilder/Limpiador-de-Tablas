"""
Modelo de datos (estrella / copo de nieve) para escribir varias hojas de
Excel en SQL como tablas relacionadas, con llave primaria (PK) y llaves
foraneas (FK).

Un "modelo" es un diccionario con esta forma:

    modelo = {
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
    }

Cada CLAVE del diccionario es el nombre final de la tabla en SQL; "hoja" es
el nombre de la hoja de origen (debe existir en el diccionario de
DataFrames que se le pase a aplicar_modelo_sql, ej. el que devuelve
data_cleaner.loaders.load_excel_hojas). Una tabla es DIMENSION si no tiene
"claves_foraneas" (o la trae vacia) y HECHO (fact) si tiene al menos una.

Modelo ESTRELLA: las claves_foraneas de la(s) tabla(s) de hechos apuntan
directo a las dimensiones. Modelo COPO DE NIEVE: ademas, una dimension
puede tener su propia clave foranea hacia OTRA dimension (ej.
dim_productos -> dim_categorias) — no hay diferencia de sintaxis, solo se
agrega "claves_foraneas" tambien en esa dimension.
"""
from __future__ import annotations


def es_tabla_hecho(definicion: dict) -> bool:
    """True si la tabla tiene al menos una llave foranea (es un hecho/fact
    en vez de una dimension)."""
    return bool(definicion.get("claves_foraneas"))


def validar_modelo(modelo: dict, hojas_cargadas: dict) -> list:
    """
    Revisa el modelo antes de aplicarlo. Devuelve una lista de mensajes de
    error encontrados (vacia si todo esta bien): hojas que no se cargaron,
    columnas de PK/FK que no existen en el DataFrame correspondiente, o una
    FK que referencia una tabla que no esta en el modelo.
    """
    errores = []
    for tabla, definicion in modelo.items():
        hoja = definicion.get("hoja")
        if hoja not in hojas_cargadas:
            errores.append(f"'{tabla}': la hoja '{hoja}' no fue cargada.")
            continue
        df = hojas_cargadas[hoja]
        pk = definicion.get("clave_primaria")
        if pk and pk not in df.columns:
            errores.append(
                f"'{tabla}': la columna de llave primaria '{pk}' no existe en la hoja '{hoja}'."
            )
        for fk in definicion.get("claves_foraneas", []):
            if fk["columna"] not in df.columns:
                errores.append(
                    f"'{tabla}': la columna FK '{fk['columna']}' no existe en la hoja '{hoja}'."
                )
            tabla_ref = fk["tabla_referencia"]
            if tabla_ref not in modelo:
                errores.append(
                    f"'{tabla}': hace referencia a la tabla '{tabla_ref}', que no esta en el modelo."
                )
                continue
            col_ref = fk["columna_referencia"]
            hoja_ref = modelo[tabla_ref].get("hoja")
            df_ref = hojas_cargadas.get(hoja_ref)
            if df_ref is not None and col_ref not in df_ref.columns:
                errores.append(
                    f"'{tabla}': la FK referencia la columna '{col_ref}' de '{tabla_ref}', "
                    "que no existe en esa hoja."
                )
    return errores


def aplicar_modelo_sql(modelo: dict, hojas_cargadas: dict, connection_string: str,
                        if_exists: str = "replace") -> list:
    """
    Escribe cada tabla del modelo en la base de datos y agrega las
    restricciones de llave primaria (PK) y llave foranea (FK).

    Orden de trabajo (importante para que las FK no fallen):
      1. Se escriben los DATOS de todas las tablas (to_sql). "replace"
         recrea la tabla sin restricciones, por eso las PK/FK se agregan
         DESPUES, en pasos separados.
      2. Se agregan las PRIMARY KEY de todas las tablas.
      3. Se agregan las FOREIGN KEY (ya con todas las PK listas, para que
         una FK pueda referenciar la PK de cualquier otra tabla del modelo,
         sin importar el orden en que aparecen en el diccionario).

    Nota: "if_exists='append'" con PK/FK puede fallar si la tabla ya tiene
    valores repetidos o nulos en la columna de la llave; para crear el
    modelo desde cero conviene "replace".

    Devuelve una lista de mensajes (uno por paso), para imprimir en consola
    o mostrar en una interfaz.
    """
    from sqlalchemy import create_engine, text

    errores = validar_modelo(modelo, hojas_cargadas)
    if errores:
        raise ValueError("El modelo tiene errores:\n- " + "\n- ".join(errores))

    engine = create_engine(connection_string)
    mensajes = []

    # 1. Datos
    for tabla, definicion in modelo.items():
        df = hojas_cargadas[definicion["hoja"]]
        df.to_sql(tabla, engine, if_exists=if_exists, index=False)
        mensajes.append(f"Datos escritos: '{tabla}' ({len(df)} filas, {len(df.columns)} columnas).")

    # 2. Llaves primarias
    with engine.begin() as conn:
        for tabla, definicion in modelo.items():
            pk = definicion.get("clave_primaria")
            if not pk:
                continue
            nombre_restriccion = f"PK_{tabla}"
            try:
                conn.execute(text(
                    f'ALTER TABLE {tabla} ADD CONSTRAINT {nombre_restriccion} PRIMARY KEY ({pk})'
                ))
                mensajes.append(f"Llave primaria agregada: '{tabla}.{pk}'.")
            except Exception as exc:
                mensajes.append(f"⚠ No se pudo agregar la PK de '{tabla}': {exc}")

    # 3. Llaves foraneas (despues de que TODAS las PK ya existen)
    with engine.begin() as conn:
        for tabla, definicion in modelo.items():
            for fk in definicion.get("claves_foraneas", []):
                nombre_restriccion = f"FK_{tabla}_{fk['columna']}"
                try:
                    conn.execute(text(
                        f'ALTER TABLE {tabla} ADD CONSTRAINT {nombre_restriccion} '
                        f'FOREIGN KEY ({fk["columna"]}) REFERENCES '
                        f'{fk["tabla_referencia"]} ({fk["columna_referencia"]})'
                    ))
                    mensajes.append(
                        f"Llave foranea agregada: '{tabla}.{fk['columna']}' -> "
                        f"'{fk['tabla_referencia']}.{fk['columna_referencia']}'."
                    )
                except Exception as exc:
                    mensajes.append(
                        f"⚠ No se pudo agregar la FK de '{tabla}.{fk['columna']}': {exc}"
                    )

    return mensajes


def generar_dot_modelo(modelo: dict) -> str:
    """
    Genera el diagrama del modelo en formato Graphviz DOT: las tablas de
    HECHO se dibujan en naranja y las de DIMENSION en celeste, con una
    flecha por cada llave foranea (FK -> PK). Sirve para mostrarlo con
    st.graphviz_chart en Streamlit, o para pegar el resultado en
    https://dreampuf.github.io/GraphvizOnline si se usa desde un script.
    """
    lineas = [
        "digraph modelo {",
        '  rankdir="LR";',
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11];',
    ]
    for tabla, definicion in modelo.items():
        color = "#F4A261" if es_tabla_hecho(definicion) else "#A8DADC"
        pk = definicion.get("clave_primaria")
        etiqueta_pk = f"\\nPK: {pk}" if pk else ""
        lineas.append(f'  "{tabla}" [label="{tabla}{etiqueta_pk}", fillcolor="{color}"];')
    for tabla, definicion in modelo.items():
        for fk in definicion.get("claves_foraneas", []):
            lineas.append(
                f'  "{tabla}" -> "{fk["tabla_referencia"]}" '
                f'[label="{fk["columna"]} -> {fk["columna_referencia"]}", fontsize=9];'
            )
    lineas.append("}")
    return "\n".join(lineas)

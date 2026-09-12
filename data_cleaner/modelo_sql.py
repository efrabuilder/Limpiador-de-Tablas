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


MOTORES_CREAR_BASE_DATOS = ["sql_server", "mysql", "postgresql"]


def generar_script_crear_base_datos(nombre_base_datos: str, motor: str = "sql_server") -> str:
    """
    Genera el script SQL para crear la base de datos ANTES de aplicar el
    modelo. Hace falta porque una cadena de conexión normal (la que usa
    aplicar_modelo_sql) no puede apuntar a una base de datos que todavía
    no existe: primero hay que crearla desde una consulta en SSMS / mysql
    / psql (conectado al servidor o a una base por defecto), y DESPUÉS sí
    armar la cadena de conexión apuntando a esa base ya creada.

    motor: "sql_server" | "mysql" | "postgresql" (ver MOTORES_CREAR_BASE_DATOS).
    """
    nombre = (nombre_base_datos or "").strip()
    if not nombre:
        raise ValueError("Indique el nombre de la base de datos.")

    if motor == "sql_server":
        return (
            "-- Pegue esto en una consulta de SSMS (o sqlcmd) conectado al SERVIDOR,\n"
            "-- sin elegir ninguna base en particular (o conectado a 'master').\n"
            f"CREATE DATABASE {nombre};\n"
            "GO\n"
            f"USE {nombre};\n"
            "GO\n"
        )
    if motor == "mysql":
        return (
            "-- Pegue esto conectado al servidor MySQL/MariaDB (cualquier base).\n"
            f"CREATE DATABASE IF NOT EXISTS {nombre};\n"
            f"USE {nombre};\n"
        )
    if motor == "postgresql":
        return (
            "-- PostgreSQL NO permite crear y usar la base en la misma sesión/conexión.\n"
            "-- 1) Conectado a cualquier base existente (ej. 'postgres'), ejecute:\n"
            f"CREATE DATABASE {nombre};\n"
            "-- 2) Cierre esa conexión y vuelva a conectarse, esta vez directo a\n"
            f"--    '{nombre}', antes de correr el modelo (ahí sí puede usar\n"
            "--    'USE' o el selector de base de su cliente SQL).\n"
        )
    raise ValueError(
        f"Motor no soportado: '{motor}'. Use uno de: {', '.join(MOTORES_CREAR_BASE_DATOS)}."
    )


# -----------------------------------------------------------------------------
# Generación del script SQL completo SIN conectarse a ninguna base de datos.
#
# aplicar_modelo_sql() de arriba necesita una cadena de conexión real y
# ejecuta los pasos contra un servidor en vivo. generar_script_modelo_sql()
# hace lo mismo en texto: arma un .sql con CREATE TABLE + PK + FK + (si se
# pide) los INSERT con los datos, para que el usuario lo copie/descargue y
# lo corra donde quiera (SSMS, mysql, psql, un cliente en la nube, etc.).
# Útil cuando la app corre en un navegador/servidor que no tiene ruta de
# red hacia la base de datos de destino.
# -----------------------------------------------------------------------------

def _quote_ident(nombre: str, motor: str) -> str:
    """Delimita un identificador (tabla o columna) según el motor, para que
    nombres con espacios, acentos o mayúsculas/minúsculas mixtas no rompan
    el script."""
    if motor == "mysql":
        return f"`{nombre}`"
    if motor == "postgresql":
        return '"' + nombre.replace('"', '""') + '"'
    return f"[{nombre}]"  # sql_server


def _tipo_sql_columna(serie, motor: str) -> str:
    """Infiere un tipo de columna SQL razonable a partir del dtype de
    pandas. No pretende ser perfecto (eso lo decide quien revise el
    script), solo dejar una definición de tabla que funcione de entrada."""
    import pandas as pd

    if pd.api.types.is_bool_dtype(serie):
        return {"sql_server": "BIT", "mysql": "TINYINT(1)", "postgresql": "BOOLEAN"}[motor]
    if pd.api.types.is_integer_dtype(serie):
        return {"sql_server": "BIGINT", "mysql": "BIGINT", "postgresql": "BIGINT"}[motor]
    if pd.api.types.is_float_dtype(serie):
        return {"sql_server": "FLOAT", "mysql": "DOUBLE", "postgresql": "DOUBLE PRECISION"}[motor]
    if pd.api.types.is_datetime64_any_dtype(serie):
        return {"sql_server": "DATETIME2", "mysql": "DATETIME", "postgresql": "TIMESTAMP"}[motor]

    # Texto: se mide el largo real de los valores para no quedarse corto,
    # con un mínimo de 50 y, si hay textos muy largos, se pasa a un tipo
    # de texto libre en vez de un VARCHAR gigante.
    largo_max = serie.dropna().astype(str).map(len).max()
    largo_max = 50 if pd.isna(largo_max) else max(50, int(largo_max) + 20)
    if largo_max > 4000:
        return {"sql_server": "NVARCHAR(MAX)", "mysql": "TEXT", "postgresql": "TEXT"}[motor]
    return {
        "sql_server": f"NVARCHAR({largo_max})",
        "mysql": f"VARCHAR({largo_max})",
        "postgresql": f"VARCHAR({largo_max})",
    }[motor]


def _valor_sql_literal(valor, motor: str) -> str:
    """Convierte un valor de una celda a su representación literal en SQL
    (NULL, número tal cual, o texto entre comillas con escape de comillas
    simples). Fechas/horas se formatean como 'YYYY-MM-DD HH:MM:SS'."""
    import pandas as pd

    if valor is None or (isinstance(valor, float) and pd.isna(valor)) or pd.isna(valor):
        return "NULL"
    if isinstance(valor, bool):
        if motor == "postgresql":
            return "TRUE" if valor else "FALSE"
        return "1" if valor else "0"
    if isinstance(valor, (int, float)):
        return repr(valor)
    if isinstance(valor, pd.Timestamp) or hasattr(valor, "strftime"):
        return "'" + valor.strftime("%Y-%m-%d %H:%M:%S") + "'"
    texto = str(valor).replace("'", "''")
    return f"'{texto}'"


def generar_script_modelo_sql(modelo: dict, hojas_cargadas: dict, motor: str = "sql_server",
                               incluir_datos: bool = True, filas_por_insert: int = 500) -> str:
    """
    Genera el script SQL completo del modelo (CREATE TABLE con tipos
    inferidos, PRIMARY KEY, FOREIGN KEY y, si se pide, INSERT con los
    datos) como texto plano, SIN conectarse a ninguna base de datos.

    Respeta el mismo orden que aplicar_modelo_sql (datos/estructura
    primero, luego todas las PK, luego todas las FK) para que las llaves
    foráneas nunca fallen por apuntar a una tabla que todavía no existe.

    motor: "sql_server" | "mysql" | "postgresql".
    incluir_datos: si es False, el script solo crea la estructura (CREATE
    TABLE + PK + FK), sin ningún INSERT.
    filas_por_insert: cuántas filas se agrupan en cada sentencia INSERT
    (para no generar una sentencia gigante por fila).
    """
    if motor not in MOTORES_CREAR_BASE_DATOS:
        raise ValueError(
            f"Motor no soportado: '{motor}'. Use uno de: {', '.join(MOTORES_CREAR_BASE_DATOS)}."
        )

    errores = validar_modelo(modelo, hojas_cargadas)
    if errores:
        raise ValueError("El modelo tiene errores:\n- " + "\n- ".join(errores))

    q = lambda nombre: _quote_ident(nombre, motor)
    bloques = [
        "-- Script generado por Limpiador de Tablas.",
        "-- No requiere conexión para generarse: revíselo y córralo donde",
        "-- necesite (SSMS, mysql, psql, un cliente en la nube, etc.).",
        "",
    ]

    # 1. CREATE TABLE (estructura) de cada tabla del modelo.
    for tabla, definicion in modelo.items():
        df = hojas_cargadas[definicion["hoja"]]
        columnas_sql = [
            f"  {q(col)} {_tipo_sql_columna(df[col], motor)}" for col in df.columns
        ]
        bloques.append(f"DROP TABLE IF EXISTS {q(tabla)};")
        bloques.append(f"CREATE TABLE {q(tabla)} (\n" + ",\n".join(columnas_sql) + "\n);")
        bloques.append("")

    # 2. INSERT con los datos (opcional).
    if incluir_datos:
        for tabla, definicion in modelo.items():
            df = hojas_cargadas[definicion["hoja"]]
            if df.empty:
                continue
            columnas_txt = ", ".join(q(col) for col in df.columns)
            filas = df.to_dict(orient="records")
            for inicio in range(0, len(filas), filas_por_insert):
                lote = filas[inicio:inicio + filas_por_insert]
                valores = ",\n".join(
                    "  (" + ", ".join(_valor_sql_literal(fila[col], motor) for col in df.columns) + ")"
                    for fila in lote
                )
                bloques.append(
                    f"INSERT INTO {q(tabla)} ({columnas_txt}) VALUES\n{valores};"
                )
            bloques.append("")

    # 3. Llaves primarias (después de que TODAS las tablas ya existen).
    for tabla, definicion in modelo.items():
        pk = definicion.get("clave_primaria")
        if not pk:
            continue
        bloques.append(
            f"ALTER TABLE {q(tabla)} ADD CONSTRAINT {q('PK_' + tabla)} PRIMARY KEY ({q(pk)});"
        )
    bloques.append("")

    # 4. Llaves foráneas (después de que TODAS las PK ya existen).
    for tabla, definicion in modelo.items():
        for fk in definicion.get("claves_foraneas", []):
            nombre_restriccion = f"FK_{tabla}_{fk['columna']}"
            bloques.append(
                f"ALTER TABLE {q(tabla)} ADD CONSTRAINT {q(nombre_restriccion)} "
                f"FOREIGN KEY ({q(fk['columna'])}) REFERENCES "
                f"{q(fk['tabla_referencia'])} ({q(fk['columna_referencia'])});"
            )

    return "\n".join(bloques).rstrip() + "\n"

#!/usr/bin/env python3
"""
Limpiador de Tablas — CLI interactivo
======================================
Carga una tabla (CSV, Excel o SQL), detecta valores atípicos y errores,
permite elegir qué hacer con cada tipo de problema, genera un reporte
detallado y exporta el archivo limpio.

Uso:
    python main.py                 -> modo interactivo (recomendado); pregunta qué hacer:
                                      limpieza clásica, limpieza guiada de nulos,
                                      merge (unir dos tablas) o diccionario de datos
    python main.py --demo          -> corre con datos de ejemplo, sin preguntas
    python main.py --modo guiada   -> salta el menú (clasica | guiada | merge | diccionario)
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
from data_cleaner import flujos_guiados as FG
from data_cleaner import limpieza_guiada as LG
from data_cleaner import merge_tablas as MT
from data_cleaner.patrones import FORMATOS_FECHA_DISPONIBLES, FORMATO_FECHA_POR_DEFECTO

MODOS = {
    "clasica": "Limpieza de una tabla (detecta y corrige problemas de calidad)",
    "guiada": "Limpieza guiada de nulos (diagnóstico + una regla por columna)",
    "merge": "Merge (unir dos tablas con revisión de llaves)",
    "diccionario": "Diccionario de datos de una tabla",
}

OPCIONES_ACCION = {
    "faltante": ["reemplazar_media", "reemplazar_mediana", "reemplazar_moda",
                 "valor_fijo", "rellenar_nan", "eliminar_fila", "marcar_solo"],
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
    "fecha_invalida": ["eliminar_fila", "valor_fijo", "normalizar_formato_fecha", "marcar_solo"],
    "email_invalido": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "telefono_invalido": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "id_duplicado": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "formula_incorrecta": ["usar_sugerido", "eliminar_fila", "valor_fijo", "marcar_solo"],
    "texto_inconsistente": ["usar_sugerido", "eliminar_fila", "valor_fijo", "marcar_solo"],
    "estado_invalido": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "capitalizacion_incorrecta": ["usar_sugerido", "eliminar_fila", "valor_fijo", "marcar_solo"],
    "espacio_extra": ["usar_sugerido", "eliminar_fila", "valor_fijo", "marcar_solo"],
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
    "estado_invalido": "Estados/valores de estado no reconocidos",
    "capitalizacion_incorrecta": "Nombres/lugares con capitalización inconsistente",
    "espacio_extra": "Texto con espacios de más al inicio/final",
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


def elegir_config_interactiva(resultado) -> tuple[dict, dict, dict]:
    config = {}
    valores_fijos = {}
    formatos_fecha = {}
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
                if (tipo, col) in valores_fijos:
                    continue
                valor = preguntar(f"  Valor fijo de reemplazo para la columna '{col}':")
                valores_fijos[(tipo, col)] = valor

        elif accion == "normalizar_formato_fecha":
            columnas_afectadas = sorted({
                issue.columna for issue in resultado.issues
                if issue.tipo == tipo and issue.columna
            })
            claves_formato = list(FORMATOS_FECHA_DISPONIBLES.keys())
            for col in columnas_afectadas:
                clave = preguntar(
                    f"  Formato de fecha preferido para la columna '{col}':",
                    claves_formato,
                    defecto=FORMATO_FECHA_POR_DEFECTO,
                )
                formatos_fecha[col] = clave

    return config, valores_fijos, formatos_fecha


# =============================================================================
# Flujos guiados: limpieza guiada de nulos, merge y diccionario de datos
# =============================================================================

def _imprimir_rutas(rutas: dict) -> None:
    print("\n✅ Proceso completado.")
    for tipo, ruta in rutas.items():
        print(f"   {tipo:<12} {ruta}")


def _pedir_tabla(etiqueta: str, ruta_inicial: str = None) -> tuple[pd.DataFrame, str, str]:
    """Pide un CSV/Excel (y la hoja si el libro tiene varias). Devuelve (df, ruta, hoja)."""
    ruta = ruta_inicial or preguntar(f"Ruta del archivo CSV/Excel de {etiqueta}:")
    hojas = FG.hojas_de_archivo(ruta)
    hoja = preguntar("Hoja a usar:", hojas, defecto=hojas[0]) if len(hojas) > 1 else None
    df = FG.leer_tabla_guiada(ruta, hoja=hoja)
    print(f"{etiqueta}: {len(df)} filas x {len(df.columns)} columnas.")
    return df, ruta, hoja


def _lista_o_none(texto: str):
    """Enter = elegir automáticamente (None); '-' = ninguna ([])."""
    if not texto:
        return None
    if texto.strip() == "-":
        return []
    return [c.strip() for c in texto.split(",") if c.strip()]


def _pedir_formato_salida() -> str:
    return preguntar("\n¿En qué formato desea la tabla resultante?", ["csv", "xlsx", "ambos"], defecto="csv")


def flujo_limpieza_guiada(args) -> None:
    df, ruta, hoja = _pedir_tabla("la tabla", args.input)

    print("\nTextos que cuentan como nulo además de la celda vacía "
          f"(opciones: {', '.join(LG.TOKENS_NULOS_EXTRA)}).")
    extra_txt = preguntar("Escriba los textos separados por coma (Enter = nan,none,null; '-' = ninguno):", defecto="")
    config = FG.configurar_limpieza_guiada(df, tokens_extra=_lista_o_none(extra_txt))
    print("\n--- Diagnóstico de nulos y vacíos ---")
    print(LG.diagnostico_nulos(df, config["tokens"]).to_string())

    print("\n--- Estandarización sugerida ---")
    print(f"  Columnas de texto : {config['texto']['columnas'] if config['texto'] else []}")
    print(f"  A número          : {config['numericas']}")
    print(f"  Latitud / longitud: {config['latitud']} / {config['longitud']}")
    if preguntar("¿Aceptar estas sugerencias?", ["si", "no"], defecto="si") == "no":
        print("Enter = sugerencia · '-' = ninguna · coma-separadas.")
        config = FG.configurar_limpieza_guiada(
            df, tokens_extra=_lista_o_none(extra_txt),
            columnas_texto=_lista_o_none(preguntar("Columnas de texto:", defecto="")),
            numericas=_lista_o_none(preguntar("Columnas a convertir a número:", defecto="")),
            latitud=preguntar("Columna de latitud:", defecto=None) or None,
            longitud=preguntar("Columna de longitud:", defecto=None) or None)

    df_base, _ = LG.ejecutar_pasos_globales(df, config)
    print("\n--- Regla de nulos por columna (sugerida) ---")
    print(LG.tabla_de_reglas(df_base, config["tokens"]).to_string(index=False))
    print(f"Reglas: {', '.join(LG.REGLAS_NULOS)}")
    ajustes_txt = []
    if preguntar("¿Aceptar las reglas sugeridas?", ["si", "no"], defecto="si") == "no":
        print("Escriba una regla por línea: columna=regla[:parametro] (ej. email=valor_fijo:Sin correo, "
              "monto=mediana_por_grupo:zona). Enter vacío para terminar.")
        while True:
            linea = input("  > ").strip()
            if not linea:
                break
            ajustes_txt.append(linea)

    resultado = FG.ejecutar_limpieza_guiada(
        df, config, FG.parsear_ajustes_reglas(ajustes_txt), nombre_archivo=ruta, hoja=hoja)
    aud = resultado.auditoria
    print("\n--- Auditoría final ---")
    print(f"  Filas: {aud['filas_antes']} → {aud['filas_despues']} · "
          f"Celdas nulas: {aud['nulos_antes']} → {aud['nulos_despues']} · Duplicados: {aud['duplicados_despues']}")
    if len(aud["nulos_restantes"]):
        print("  Nulos que quedan:\n" + aud["nulos_restantes"].to_string())
    _imprimir_rutas(FG.guardar_limpieza_guiada(resultado, args.outdir, _pedir_formato_salida()))


def flujo_merge(args) -> None:
    df_a, ruta_a, hoja_a = _pedir_tabla("la tabla A (la que manda)", args.input)
    df_b, ruta_b, hoja_b = _pedir_tabla("la tabla B (la que enriquece)")

    sugerencias = MT.sugerir_llaves(df_a, df_b)
    if len(sugerencias):
        print("\nPares de columnas que podrían ser la llave:\n" + sugerencias.to_string(index=False))
    claves_a = _lista_o_none(preguntar("Columna(s) llave de A, coma-separadas (Enter = la sugerida):", defecto=""))
    claves_b = _lista_o_none(preguntar("Columna(s) llave de B, en el mismo orden (Enter = la sugerida):", defecto=""))
    modo = preguntar("Cómo comparar las llaves:", list(MT.MODOS_LLAVE), defecto="texto")
    ancho = int(preguntar("Rellenar con ceros a la izquierda hasta (0 = no):", defecto="0")) if modo == "codigo" else 0

    _, _, d = FG.diagnosticar_llaves(df_a, df_b, claves_a, claves_b, modo, ancho)
    print(f"\n--- Revisión de las llaves ---\n  Filas de A con pareja en B: {d['semaforo']} {d['pct_filas_con_pareja']}%"
          f"\n  Llaves repetidas en A / B: {d['repetidas_a']} / {d['repetidas_b']}"
          f"\n  Filas sin llave en A / B: {d['nulos_a']} / {d['nulos_b']}"
          f"\n  Relación entre las tablas: {d['cardinalidad']}")
    if d["repetidas_b"]:
        print("  ⚠ B repite llaves: se dejará una fila por llave (función 'first') para no multiplicar filas de A.")

    union = preguntar("¿Qué tabla manda?", [f"{k} — {v}" for k, v in MT.TIPOS_UNION.items()],
                      defecto=f"left — {MT.TIPOS_UNION['left']}").split(" — ")[0]
    prefijo = preguntar("Prefijo para las columnas de B (Enter = ninguno):", defecto="")

    resultado = FG.ejecutar_merge(
        df_a, df_b, ruta_a, ruta_b, claves_a, claves_b, how=union, modo=modo, ancho=ancho,
        prefijo_b=prefijo, hoja_a=hoja_a, hoja_b=hoja_b)
    aud = resultado.auditoria
    print(f"\n--- Resultado ---\n  Filas de A: {aud['filas_a']} · Filas de B: {aud['filas_b']} · "
          f"Filas del resultado: {len(resultado.df)} ({len(resultado.df) - aud['filas_a']:+d} vs A)")
    print(f"  A con pareja: {aud['semaforo']} {aud['pct_filas_con_pareja']}%")
    for aviso in aud["advertencias"]:
        print(f"  ⚠ {aviso}")
    _imprimir_rutas(FG.guardar_merge(resultado, args.outdir, formato=_pedir_formato_salida()))


def flujo_diccionario(args) -> None:
    df, ruta, _ = _pedir_tabla("la tabla", args.input)
    nombre = preguntar("Nombre de la tabla maestra:", defecto=FG.nombre_base(ruta))
    _, _, excel = FG.generar_diccionario(df, nombre, fuentes=[ruta])
    os.makedirs(args.outdir, exist_ok=True)
    destino = os.path.join(args.outdir, f"diccionario_{FG.nombre_base(nombre)}.xlsx")
    with open(destino, "wb") as archivo:
        archivo.write(excel)
    _imprimir_rutas({"diccionario": destino})


FLUJOS_GUIADOS = {"guiada": flujo_limpieza_guiada, "merge": flujo_merge, "diccionario": flujo_diccionario}


def correr_flujo_guiado(modo: str, args) -> None:
    """Corre un flujo guiado. Los errores de validación (columna inexistente,
    regla inválida...) salen como mensaje corto en vez de traza."""
    try:
        FLUJOS_GUIADOS[modo](args)
    except ValueError as exc:
        print(f"\n❌ {exc}")
        sys.exit(2)


def main():
    parser = argparse.ArgumentParser(description="Limpiador de tablas con detección de atípicos.")
    parser.add_argument("--demo", action="store_true", help="Ejecuta con datos de ejemplo, sin preguntas.")
    parser.add_argument("--input", help="Ruta del archivo de entrada (csv/xlsx).")
    parser.add_argument("--outdir", default="salida", help="Carpeta de salida.")
    parser.add_argument("--modo", choices=list(MODOS), default=None,
                        help="Qué hacer, sin pasar por el menú: " + ", ".join(MODOS) + ".")
    args = parser.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    modo = "clasica" if args.demo else (args.modo or preguntar(
        "¿Qué desea hacer?", [f"{k} — {v}" for k, v in MODOS.items()],
        defecto=f"clasica — {MODOS['clasica']}").split(" — ")[0])
    if modo != "clasica":
        correr_flujo_guiado(modo, args)
        return

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
    formatos_fecha = {}
    if not args.demo:
        config, valores_fijos, formatos_fecha = elegir_config_interactiva(resultado)

    df_limpio, registro = limpiar(df, resultado.issues, config=config, valores_fijos=valores_fijos,
                                   formatos_fecha=formatos_fecha)

    tablas_reporte = construir_reporte(resultado, registro, nombre_fuente=nombre_fuente)

    ruta_reporte = os.path.join(args.outdir, "reporte_calidad_datos.xlsx")
    exportar_reporte_excel(tablas_reporte, ruta_reporte)

    if not args.demo:
        formato_salida = preguntar(
            "\n¿En qué formato desea el archivo limpio?", ["csv", "excel", "sql"], defecto="excel"
        )
    else:
        formato_salida = "excel"

    if formato_salida == "sql":
        conn_salida = preguntar("Connection string de destino (ej: sqlite:///salida.db):")
        tabla_salida = preguntar("Nombre de la tabla destino:")
        si_existe = preguntar(
            "Si la tabla ya existe:", ["replace", "append", "fail"], defecto="replace"
        )
        from data_cleaner.exporters import exportar_sql
        mensaje_sql = exportar_sql(df_limpio, conn_salida, tabla_salida, if_exists=si_existe)
        print("\n✅ Proceso completado.")
        print(f"   {mensaje_sql}")
        print(f"   Reporte:         {ruta_reporte}")
        print(f"   Filas finales:   {len(df_limpio)} (originales: {len(df)})")
        return

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

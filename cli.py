#!/usr/bin/env python3
"""
Limpiador de Tablas — CLI no interactiva (flags)
==================================================
Pensada para automatización (scripts, cron, CI/CD): a diferencia de
main.py, no hace preguntas por consola — todo se indica por parámetros.

Ejemplos:
    python cli.py analizar --input ejemplo_datos.csv
    python cli.py analizar --input datos.xlsx --metodo-atipicos zscore

    python cli.py limpiar --input ejemplo_datos.csv --outdir salida \
        --faltante reemplazar_mediana --duplicado eliminar_fila \
        --atipico limitar --tipo-invalido marcar_solo

    python cli.py limpiar --input ejemplo_datos.csv --outdir salida \
        --faltante valor_fijo --valor-fijo edad=0 --valor-fijo salario=0
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table

from data_cleaner import (
    load_table, analizar, limpiar, DEFAULT_CONFIG,
    construir_reporte, exportar_reporte_excel, exportar,
)

app = typer.Typer(
    add_completion=False,
    help="Limpiador de Tablas — detección y corrección de calidad de datos por línea de comandos.",
)
console = Console()

ACCIONES_FALTANTE = ["reemplazar_media", "reemplazar_mediana", "reemplazar_moda",
                      "valor_fijo", "eliminar_fila", "marcar_solo"]
ACCIONES_DUPLICADO = ["eliminar_fila", "marcar_solo"]
ACCIONES_ATIPICO = ["limitar", "reemplazar_mediana", "reemplazar_media",
                     "eliminar_fila", "marcar_solo"]
ACCIONES_TIPO_INVALIDO = ["eliminar_fila", "valor_fijo", "marcar_solo"]
ACCIONES_FECHA = ["eliminar_fila", "valor_fijo", "marcar_solo"]
ACCIONES_EMAIL = ["eliminar_fila", "valor_fijo", "marcar_solo"]
ACCIONES_TELEFONO = ["eliminar_fila", "valor_fijo", "marcar_solo"]
ACCIONES_ID_DUPLICADO = ["eliminar_fila", "valor_fijo", "marcar_solo"]
ACCIONES_FORMULA = ["usar_sugerido", "eliminar_fila", "valor_fijo", "marcar_solo"]
ACCIONES_TEXTO = ["usar_sugerido", "eliminar_fila", "valor_fijo", "marcar_solo"]


def _parsear_valores_fijos(pares: Optional[List[str]]) -> dict:
    """Convierte ['columna=valor', ...] en {'columna': 'valor', ...}."""
    resultado = {}
    for par in pares or []:
        if "=" not in par:
            console.print(f"[red]--valor-fijo inválido: '{par}' (use columna=valor)[/red]")
            raise typer.Exit(code=2)
        col, val = par.split("=", 1)
        resultado[col.strip()] = val.strip()
    return resultado


def _imprimir_resumen(resultado) -> None:
    tabla = Table(title="Resumen del análisis")
    tabla.add_column("Indicador")
    tabla.add_column("Valor", justify="right")
    tabla.add_row("Filas analizadas", str(resultado.filas_analizadas))
    tabla.add_row("Columnas analizadas", str(resultado.columnas_analizadas))
    tabla.add_row("Total de hallazgos", str(len(resultado.issues)))
    for tipo, cantidad in resultado.por_tipo().items():
        tabla.add_row(f"  {tipo}", str(cantidad))
    console.print(tabla)


def _parsear_lista_columnas(valor: Optional[str]) -> Optional[List[str]]:
    """Convierte 'col1,col2' en ['col1','col2']. None si no se indicó nada
    (deja que el analizador auto-detecte esas columnas por nombre)."""
    if not valor:
        return None
    return [c.strip() for c in valor.split(",") if c.strip()]


@app.command("analizar")
def analizar_cmd(
    input: str = typer.Option(..., "--input", "-i", help="Ruta del archivo CSV/Excel a analizar."),
    metodo_atipicos: str = typer.Option("iqr", "--metodo-atipicos", "-m",
                                         help="Método de detección de atípicos: iqr | zscore | ambos."),
    sin_auto_columnas: bool = typer.Option(False, "--sin-auto-columnas",
                                            help="Desactiva la auto-detección de columnas por nombre "
                                                 "para fecha/email/teléfono/id/fórmula/texto."),
    columnas_fecha: Optional[str] = typer.Option(None, "--columnas-fecha", help="Columnas de fecha (coma-separadas)."),
    fecha_min: Optional[str] = typer.Option(None, "--fecha-min", help="Fecha mínima válida (AAAA-MM-DD)."),
    fecha_max: Optional[str] = typer.Option(None, "--fecha-max", help="Fecha máxima válida (AAAA-MM-DD)."),
    columnas_email: Optional[str] = typer.Option(None, "--columnas-email", help="Columnas de email (coma-separadas)."),
    columnas_telefono: Optional[str] = typer.Option(None, "--columnas-telefono", help="Columnas de teléfono (coma-separadas)."),
    digitos_telefono: str = typer.Option("8-8", "--digitos-telefono", help="Rango de dígitos válido, formato min-max."),
    columnas_id: Optional[str] = typer.Option(None, "--columnas-id", help="Columnas identificadoras (coma-separadas)."),
    total: Optional[str] = typer.Option(None, "--total", help="Columna de total (regla Total = Cantidad × Precio)."),
    cantidad: Optional[str] = typer.Option(None, "--cantidad", help="Columna de cantidad."),
    precio: Optional[str] = typer.Option(None, "--precio", help="Columna de precio unitario."),
    columnas_texto: Optional[str] = typer.Option(None, "--columnas-texto", help="Columnas categóricas (coma-separadas)."),
):
    """Analiza una tabla e imprime el resumen de hallazgos, sin modificar nada."""
    if not os.path.exists(input):
        console.print(f"[red]No existe el archivo: {input}[/red]")
        raise typer.Exit(code=1)

    min_dig, max_dig = (int(x) for x in digitos_telefono.split("-", 1))
    df = load_table(input, kind="auto")
    resultado = analizar(
        df, metodo_atipicos=metodo_atipicos,
        auto_detectar_columnas=not sin_auto_columnas,
        columnas_fecha=_parsear_lista_columnas(columnas_fecha), fecha_min=fecha_min, fecha_max=fecha_max,
        columnas_email=_parsear_lista_columnas(columnas_email),
        columnas_telefono=_parsear_lista_columnas(columnas_telefono), digitos_telefono=(min_dig, max_dig),
        columnas_id=_parsear_lista_columnas(columnas_id),
        columna_total=total, columna_cantidad=cantidad, columna_precio=precio,
        columnas_texto=_parsear_lista_columnas(columnas_texto),
    )
    console.print(f"Tabla cargada: [bold]{len(df)}[/bold] filas x [bold]{len(df.columns)}[/bold] columnas.")
    _imprimir_resumen(resultado)


@app.command("limpiar")
def limpiar_cmd(
    input: str = typer.Option(..., "--input", "-i", help="Ruta del archivo CSV/Excel a limpiar."),
    outdir: str = typer.Option("salida", "--outdir", "-o", help="Carpeta donde guardar los resultados."),
    metodo_atipicos: str = typer.Option("iqr", "--metodo-atipicos", "-m",
                                         help="Método de detección de atípicos: iqr | zscore | ambos."),
    faltante: str = typer.Option(DEFAULT_CONFIG["faltante"], "--faltante",
                                  help=f"Acción para valores faltantes: {', '.join(ACCIONES_FALTANTE)}."),
    duplicado: str = typer.Option(DEFAULT_CONFIG["duplicado"], "--duplicado",
                                   help=f"Acción para filas duplicadas: {', '.join(ACCIONES_DUPLICADO)}."),
    atipico: str = typer.Option(DEFAULT_CONFIG["atipico"], "--atipico",
                                 help=f"Acción para valores atípicos: {', '.join(ACCIONES_ATIPICO)}."),
    tipo_invalido: str = typer.Option(DEFAULT_CONFIG["tipo_invalido"], "--tipo-invalido",
                                       help=f"Acción para errores de tipo: {', '.join(ACCIONES_TIPO_INVALIDO)}."),
    fecha_invalida: str = typer.Option(DEFAULT_CONFIG["fecha_invalida"], "--fecha-invalida",
                                        help=f"Acción para fechas inválidas: {', '.join(ACCIONES_FECHA)}."),
    email_invalido: str = typer.Option(DEFAULT_CONFIG["email_invalido"], "--email-invalido",
                                        help=f"Acción para emails inválidos: {', '.join(ACCIONES_EMAIL)}."),
    telefono_invalido: str = typer.Option(DEFAULT_CONFIG["telefono_invalido"], "--telefono-invalido",
                                           help=f"Acción para teléfonos inválidos: {', '.join(ACCIONES_TELEFONO)}."),
    id_duplicado: str = typer.Option(DEFAULT_CONFIG["id_duplicado"], "--id-duplicado",
                                      help=f"Acción para IDs duplicados: {', '.join(ACCIONES_ID_DUPLICADO)}."),
    formula_incorrecta: str = typer.Option(DEFAULT_CONFIG["formula_incorrecta"], "--formula-incorrecta",
                                            help=f"Acción para Total≠Cantidad×Precio: {', '.join(ACCIONES_FORMULA)}."),
    texto_inconsistente: str = typer.Option(DEFAULT_CONFIG["texto_inconsistente"], "--texto-inconsistente",
                                             help=f"Acción para variantes de texto: {', '.join(ACCIONES_TEXTO)}."),
    valor_fijo: List[str] = typer.Option(
        [], "--valor-fijo", help="Valor fijo por columna, formato columna=valor. Repetible."
    ),
    formato_salida: str = typer.Option("excel", "--formato-salida", help="Formato del archivo limpio: csv | excel."),
    sin_auto_columnas: bool = typer.Option(False, "--sin-auto-columnas",
                                            help="Desactiva la auto-detección de columnas por nombre "
                                                 "para fecha/email/teléfono/id/fórmula/texto."),
    columnas_fecha: Optional[str] = typer.Option(None, "--columnas-fecha", help="Columnas de fecha (coma-separadas)."),
    fecha_min: Optional[str] = typer.Option(None, "--fecha-min", help="Fecha mínima válida (AAAA-MM-DD)."),
    fecha_max: Optional[str] = typer.Option(None, "--fecha-max", help="Fecha máxima válida (AAAA-MM-DD)."),
    columnas_email: Optional[str] = typer.Option(None, "--columnas-email", help="Columnas de email (coma-separadas)."),
    columnas_telefono: Optional[str] = typer.Option(None, "--columnas-telefono", help="Columnas de teléfono (coma-separadas)."),
    digitos_telefono: str = typer.Option("8-8", "--digitos-telefono", help="Rango de dígitos válido, formato min-max."),
    columnas_id: Optional[str] = typer.Option(None, "--columnas-id", help="Columnas identificadoras (coma-separadas)."),
    total: Optional[str] = typer.Option(None, "--total", help="Columna de total (regla Total = Cantidad × Precio)."),
    cantidad: Optional[str] = typer.Option(None, "--cantidad", help="Columna de cantidad."),
    precio: Optional[str] = typer.Option(None, "--precio", help="Columna de precio unitario."),
    columnas_texto: Optional[str] = typer.Option(None, "--columnas-texto", help="Columnas categóricas (coma-separadas)."),
):
    """Analiza, limpia y exporta la tabla + reporte, todo en un solo comando (sin preguntas)."""
    if not os.path.exists(input):
        console.print(f"[red]No existe el archivo: {input}[/red]")
        raise typer.Exit(code=1)

    acciones_validas = {
        "faltante": ACCIONES_FALTANTE, "duplicado": ACCIONES_DUPLICADO,
        "atipico": ACCIONES_ATIPICO, "tipo_invalido": ACCIONES_TIPO_INVALIDO,
        "fecha_invalida": ACCIONES_FECHA, "email_invalido": ACCIONES_EMAIL,
        "telefono_invalido": ACCIONES_TELEFONO, "id_duplicado": ACCIONES_ID_DUPLICADO,
        "formula_incorrecta": ACCIONES_FORMULA, "texto_inconsistente": ACCIONES_TEXTO,
    }
    config = {"faltante": faltante, "duplicado": duplicado,
              "atipico": atipico, "tipo_invalido": tipo_invalido,
              "fecha_invalida": fecha_invalida, "email_invalido": email_invalido,
              "telefono_invalido": telefono_invalido, "id_duplicado": id_duplicado,
              "formula_incorrecta": formula_incorrecta, "texto_inconsistente": texto_inconsistente}
    for tipo, accion in config.items():
        if accion not in acciones_validas[tipo]:
            console.print(f"[red]Acción inválida para {tipo}: '{accion}'. "
                           f"Válidas: {', '.join(acciones_validas[tipo])}[/red]")
            raise typer.Exit(code=2)
    if formato_salida not in ("csv", "excel"):
        console.print("[red]--formato-salida debe ser 'csv' o 'excel'[/red]")
        raise typer.Exit(code=2)

    valores_fijos = _parsear_valores_fijos(valor_fijo)

    min_dig, max_dig = (int(x) for x in digitos_telefono.split("-", 1))
    os.makedirs(outdir, exist_ok=True)
    df = load_table(input, kind="auto")
    console.print(f"Tabla cargada: [bold]{len(df)}[/bold] filas x [bold]{len(df.columns)}[/bold] columnas.")

    resultado = analizar(
        df, metodo_atipicos=metodo_atipicos,
        auto_detectar_columnas=not sin_auto_columnas,
        columnas_fecha=_parsear_lista_columnas(columnas_fecha), fecha_min=fecha_min, fecha_max=fecha_max,
        columnas_email=_parsear_lista_columnas(columnas_email),
        columnas_telefono=_parsear_lista_columnas(columnas_telefono), digitos_telefono=(min_dig, max_dig),
        columnas_id=_parsear_lista_columnas(columnas_id),
        columna_total=total, columna_cantidad=cantidad, columna_precio=precio,
        columnas_texto=_parsear_lista_columnas(columnas_texto),
    )
    _imprimir_resumen(resultado)

    if not resultado.issues:
        console.print("[green]No se encontraron problemas. No es necesario limpiar la tabla.[/green]")
        raise typer.Exit(code=0)

    faltan = [
        tipo for tipo, accion in config.items()
        if accion == "valor_fijo"
        and not any(i.columna in valores_fijos for i in resultado.issues if i.tipo == tipo)
    ]
    if faltan:
        console.print(f"[red]Falta --valor-fijo columna=valor para el/los tipo(s): {', '.join(faltan)}[/red]")
        raise typer.Exit(code=2)

    df_limpio, registro = limpiar(df, resultado.issues, config=config, valores_fijos=valores_fijos)
    tablas_reporte = construir_reporte(resultado, registro, nombre_fuente=input)

    ruta_reporte = os.path.join(outdir, "reporte_calidad_datos.xlsx")
    exportar_reporte_excel(tablas_reporte, ruta_reporte)

    ext = "csv" if formato_salida == "csv" else "xlsx"
    ruta_limpio = os.path.join(outdir, f"datos_limpios.{ext}")
    exportar(df_limpio, ruta_limpio, kind=formato_salida)

    console.print("\n[bold green]✅ Proceso completado.[/bold green]")
    console.print(f"   Archivo limpio:  {ruta_limpio}")
    console.print(f"   Reporte:         {ruta_reporte}")
    console.print(f"   Filas finales:   {len(df_limpio)} (originales: {len(df)})")


if __name__ == "__main__":
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[yellow]Proceso cancelado por el usuario.[/yellow]")
        sys.exit(1)

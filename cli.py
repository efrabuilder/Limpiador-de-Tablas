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


@app.command("analizar")
def analizar_cmd(
    input: str = typer.Option(..., "--input", "-i", help="Ruta del archivo CSV/Excel a analizar."),
    metodo_atipicos: str = typer.Option("iqr", "--metodo-atipicos", "-m",
                                         help="Método de detección de atípicos: iqr | zscore | ambos."),
):
    """Analiza una tabla e imprime el resumen de hallazgos, sin modificar nada."""
    if not os.path.exists(input):
        console.print(f"[red]No existe el archivo: {input}[/red]")
        raise typer.Exit(code=1)

    df = load_table(input, kind="auto")
    resultado = analizar(df, metodo_atipicos=metodo_atipicos)
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
    valor_fijo: List[str] = typer.Option(
        [], "--valor-fijo", help="Valor fijo por columna, formato columna=valor. Repetible."
    ),
    formato_salida: str = typer.Option("excel", "--formato-salida", help="Formato del archivo limpio: csv | excel."),
):
    """Analiza, limpia y exporta la tabla + reporte, todo en un solo comando (sin preguntas)."""
    if not os.path.exists(input):
        console.print(f"[red]No existe el archivo: {input}[/red]")
        raise typer.Exit(code=1)

    acciones_validas = {
        "faltante": ACCIONES_FALTANTE, "duplicado": ACCIONES_DUPLICADO,
        "atipico": ACCIONES_ATIPICO, "tipo_invalido": ACCIONES_TIPO_INVALIDO,
    }
    config = {"faltante": faltante, "duplicado": duplicado,
              "atipico": atipico, "tipo_invalido": tipo_invalido}
    for tipo, accion in config.items():
        if accion not in acciones_validas[tipo]:
            console.print(f"[red]Acción inválida para {tipo}: '{accion}'. "
                           f"Válidas: {', '.join(acciones_validas[tipo])}[/red]")
            raise typer.Exit(code=2)
    if formato_salida not in ("csv", "excel"):
        console.print("[red]--formato-salida debe ser 'csv' o 'excel'[/red]")
        raise typer.Exit(code=2)

    valores_fijos = _parsear_valores_fijos(valor_fijo)

    os.makedirs(outdir, exist_ok=True)
    df = load_table(input, kind="auto")
    console.print(f"Tabla cargada: [bold]{len(df)}[/bold] filas x [bold]{len(df.columns)}[/bold] columnas.")

    resultado = analizar(df, metodo_atipicos=metodo_atipicos)
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

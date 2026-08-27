"""
Genera el reporte de hallazgos: resumen general + detalle fila por fila,
columna por columna. Se exporta a Excel (2 hojas) y opcionalmente a CSV.
"""
from __future__ import annotations
import pandas as pd
from datetime import datetime
from typing import List, Dict
from .analyzer import AnalysisResult


def construir_reporte(resultado: AnalysisResult, registro_acciones: List[dict],
                       nombre_fuente: str = "") -> Dict[str, pd.DataFrame]:
    """
    Construye las tablas del reporte:
      - resumen: totales generales y por tipo/columna
      - detalle: una fila por cada hallazgo, con fila/columna/valor/accion
    """
    detalle = pd.DataFrame(registro_acciones)
    if not detalle.empty:
        detalle = detalle.sort_values(["columna", "fila"]).reset_index(drop=True)
        detalle.index += 1  # numeración amigable desde 1
        detalle.index.name = "N°"

    por_tipo = resultado.por_tipo()
    por_columna = resultado.por_columna()

    filas_resumen = [
        ("Fecha de análisis", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Archivo/fuente analizada", nombre_fuente or "N/D"),
        ("Filas analizadas", resultado.filas_analizadas),
        ("Columnas analizadas", resultado.columnas_analizadas),
        ("Total de hallazgos", len(resultado.issues)),
        ("", ""),
        ("--- Hallazgos por tipo ---", ""),
    ]
    for tipo, cantidad in por_tipo.items():
        filas_resumen.append((f"  {tipo}", cantidad))

    filas_resumen.append(("", ""))
    filas_resumen.append(("--- Hallazgos por columna ---", ""))
    for columna, cantidad in por_columna.items():
        filas_resumen.append((f"  {columna}", cantidad))

    resumen = pd.DataFrame(filas_resumen, columns=["Indicador", "Valor"])

    return {"resumen": resumen, "detalle": detalle}


def exportar_reporte_excel(tablas: Dict[str, pd.DataFrame], ruta_salida: str) -> str:
    """Exporta el resumen y el detalle a un único archivo Excel con 2 hojas."""
    with pd.ExcelWriter(ruta_salida, engine="openpyxl") as writer:
        tablas["resumen"].to_excel(writer, sheet_name="Resumen", index=False)
        if not tablas["detalle"].empty:
            tablas["detalle"].to_excel(writer, sheet_name="Detalle_Hallazgos")
        else:
            pd.DataFrame({"Mensaje": ["No se encontraron hallazgos."]}).to_excel(
                writer, sheet_name="Detalle_Hallazgos", index=False
            )
    return ruta_salida


def exportar_reporte_csv(tablas: Dict[str, pd.DataFrame], ruta_base: str) -> List[str]:
    """Exporta resumen y detalle como dos archivos CSV separados. Devuelve las rutas."""
    ruta_resumen = ruta_base.replace(".csv", "") + "_resumen.csv"
    ruta_detalle = ruta_base.replace(".csv", "") + "_detalle.csv"
    tablas["resumen"].to_csv(ruta_resumen, index=False)
    tablas["detalle"].to_csv(ruta_detalle)
    return [ruta_resumen, ruta_detalle]


def imprimir_resumen_consola(resultado: AnalysisResult) -> None:
    """Imprime un resumen corto y legible en la terminal."""
    print("\n" + "=" * 50)
    print("RESUMEN DEL ANÁLISIS")
    print("=" * 50)
    print(f"Filas analizadas:      {resultado.filas_analizadas}")
    print(f"Columnas analizadas:   {resultado.columnas_analizadas}")
    print(f"Total de hallazgos:    {len(resultado.issues)}")
    print("-" * 50)
    for tipo, cantidad in resultado.por_tipo().items():
        print(f"  {tipo:<15} {cantidad}")
    print("=" * 50 + "\n")

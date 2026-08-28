# -*- coding: utf-8 -*-
"""
exportador.py
=============
Genera scripts Python autocontenidos (sin depender del paquete
``data_cleaner``) y código M para Power BI, reproduciendo EXACTAMENTE la
configuración de limpieza (acción por tipo de hallazgo, factor IQR y
valores fijos) que el usuario eligió en la interfaz (web, escritorio o
API).

Los scripts generados solo requieren pandas y numpy, porque los entornos
externos (Power BI, Tableau Prep/TabPy, Alteryx, Qlik) normalmente no
tienen instalado ``data_cleaner``.

Nota: por ahora solo soportan detección de atípicos por método IQR (no
Z-score), que es el único método implementado de forma autocontenida.
"""
from __future__ import annotations
from typing import Dict, Optional

DEFAULT_CONFIG_EXPORT = {
    "faltante": "reemplazar_mediana",
    "duplicado": "eliminar_fila",
    "atipico": "limitar",
    "tipo_invalido": "marcar_solo",
}

# -----------------------------------------------------------------------------
# Núcleo de lógica compartido entre el script para Power BI y el universal.
# Es una copia autocontenida (solo pandas/numpy) de data_cleaner/cleaner.py.
# -----------------------------------------------------------------------------
_NUCLEO_LOGICA = '''def _columna_numerica_potencial(serie):
    valores = serie.dropna()
    if len(valores) == 0:
        return False
    convertibles = pd.to_numeric(valores, errors="coerce")
    return convertibles.notna().mean() > 0.7


def _columnas_para_atipicos(df):
    columnas = df.select_dtypes(include=[np.number]).columns.tolist()
    for col in df.columns:
        if col in columnas:
            continue
        serie = df[col]
        es_texto = pd.api.types.is_object_dtype(serie) or pd.api.types.is_string_dtype(serie)
        if es_texto and _columna_numerica_potencial(serie):
            columnas.append(col)
    return columnas


def _detectar_hallazgos(df, factor_iqr=1.5):
    hallazgos = []

    for col in df.columns:
        for idx in df[df[col].isna()].index:
            hallazgos.append({"tipo": "faltante", "columna": col, "fila": int(idx),
                               "valor_original": None, "detalle": "Valor vacio/nulo"})

    mask_dup = df.duplicated(keep="first")
    for idx in df[mask_dup].index:
        hallazgos.append({"tipo": "duplicado", "columna": None, "fila": int(idx),
                           "valor_original": df.loc[idx].to_dict(),
                           "detalle": "Fila duplicada (identica a una anterior)"})

    for col in df.columns:
        serie = df[col]
        es_texto = pd.api.types.is_object_dtype(serie) or pd.api.types.is_string_dtype(serie)
        if es_texto and _columna_numerica_potencial(serie):
            convertidos = pd.to_numeric(serie, errors="coerce")
            malos = serie[(convertidos.isna()) & (serie.notna())]
            for idx, val in malos.items():
                hallazgos.append({"tipo": "tipo_invalido", "columna": col, "fila": int(idx),
                                   "valor_original": val, "detalle": "Se esperaba un valor numerico"})

    for col in _columnas_para_atipicos(df):
        serie = pd.to_numeric(df[col], errors="coerce")
        q1, q3 = serie.quantile(0.25), serie.quantile(0.75)
        iqr = q3 - q1
        if iqr == 0 or pd.isna(iqr):
            continue
        lim_inf, lim_sup = q1 - factor_iqr * iqr, q3 + factor_iqr * iqr
        atipicos = serie[(serie < lim_inf) | (serie > lim_sup)]
        for idx, val in atipicos.items():
            hallazgos.append({"tipo": "atipico", "columna": col, "fila": int(idx),
                               "valor_original": val,
                               "detalle": f"Fuera de rango [{lim_inf:.2f}, {lim_sup:.2f}] (IQR)"})

    return hallazgos


def _asignar(df, fila, columna, valor):
    try:
        df.at[fila, columna] = valor
    except (TypeError, ValueError):
        df[columna] = df[columna].astype(object)
        df.at[fila, columna] = valor


def _valor_reemplazo(df, columna, accion, valor_fijo=None):
    serie_num = pd.to_numeric(df[columna], errors="coerce")
    if accion == "reemplazar_media":
        return serie_num.mean()
    if accion == "reemplazar_mediana":
        return serie_num.median()
    if accion == "reemplazar_moda":
        moda = df[columna].mode(dropna=True)
        return moda.iloc[0] if not moda.empty else None
    if accion == "valor_fijo":
        return valor_fijo
    return None


def limpiar_tabla(df, faltante, duplicado, atipico, tipo_invalido, factor_iqr, valores_fijos):
    config = {"faltante": faltante, "duplicado": duplicado,
              "atipico": atipico, "tipo_invalido": tipo_invalido}

    df_limpio = df.copy()
    hallazgos = _detectar_hallazgos(df, factor_iqr=factor_iqr)

    limites_iqr = {}
    for h in hallazgos:
        if h["tipo"] == "atipico" and h["columna"] not in limites_iqr:
            serie = pd.to_numeric(df[h["columna"]], errors="coerce")
            q1, q3 = serie.quantile(0.25), serie.quantile(0.75)
            iqr = q3 - q1
            limites_iqr[h["columna"]] = (q1 - factor_iqr * iqr, q3 + factor_iqr * iqr)

    registro = []
    filas_a_eliminar = set()

    for h in hallazgos:
        accion = config.get(h["tipo"], "marcar_solo")
        valor_nuevo = h["valor_original"]

        if accion == "marcar_solo":
            pass
        elif accion == "eliminar_fila":
            filas_a_eliminar.add(h["fila"])
            valor_nuevo = "(fila eliminada)"
        elif h["tipo"] in ("faltante", "tipo_invalido") and accion in (
                "reemplazar_media", "reemplazar_mediana", "reemplazar_moda", "valor_fijo"):
            valor_nuevo = _valor_reemplazo(df, h["columna"], accion, valores_fijos.get(h["columna"]))
            _asignar(df_limpio, h["fila"], h["columna"], valor_nuevo)
        elif h["tipo"] == "atipico" and accion == "limitar":
            lim_inf, lim_sup = limites_iqr.get(h["columna"], (None, None))
            val_num = pd.to_numeric(pd.Series([h["valor_original"]]), errors="coerce")[0]
            if lim_inf is not None and not pd.isna(val_num):
                valor_nuevo = lim_inf if val_num < lim_inf else lim_sup
                _asignar(df_limpio, h["fila"], h["columna"], valor_nuevo)
        elif h["tipo"] == "atipico" and accion in (
                "reemplazar_media", "reemplazar_mediana", "reemplazar_moda"):
            valor_nuevo = _valor_reemplazo(df, h["columna"], accion)
            _asignar(df_limpio, h["fila"], h["columna"], valor_nuevo)

        registro.append({
            "tipo": h["tipo"],
            "columna": h["columna"] or "(fila completa)",
            "fila": h["fila"],
            "valor_original": str(h["valor_original"]),
            "accion_aplicada": accion,
            "valor_nuevo": str(valor_nuevo),
            "detalle": h["detalle"],
        })

    marcas = {}
    for r in registro:
        if r["accion_aplicada"] != "marcar_solo":
            continue
        etiqueta = r["tipo"] if r["columna"] == "(fila completa)" else f"{r['tipo']}:{r['columna']}"
        marcas.setdefault(r["fila"], []).append(etiqueta)

    if marcas:
        col_marca = "_revisar_calidad"
        while col_marca in df_limpio.columns:
            col_marca += "_"
        df_limpio[col_marca] = ""
        for fila, etiquetas in marcas.items():
            if fila in df_limpio.index:
                df_limpio.at[fila, col_marca] = "; ".join(etiquetas)

    if filas_a_eliminar:
        df_limpio = df_limpio.drop(index=list(filas_a_eliminar), errors="ignore").reset_index(drop=True)

    df_reporte = pd.DataFrame(registro) if registro else pd.DataFrame(
        columns=["tipo", "columna", "fila", "valor_original", "accion_aplicada", "valor_nuevo", "detalle"]
    )
    return df_limpio, df_reporte
'''


def _bloque_config(config: Dict[str, str], factor_iqr: float, valores_fijos: Optional[dict]) -> str:
    cfg = {**DEFAULT_CONFIG_EXPORT, **(config or {})}
    valores_fijos = valores_fijos or {}
    return (
        f"ACCION_FALTANTE = {cfg['faltante']!r}\n"
        f"ACCION_DUPLICADO = {cfg['duplicado']!r}\n"
        f"ACCION_ATIPICO = {cfg['atipico']!r}\n"
        f"ACCION_TIPO_INVALIDO = {cfg['tipo_invalido']!r}\n"
        f"FACTOR_IQR = {factor_iqr!r}\n"
        f"VALORES_FIJOS = {valores_fijos!r}\n"
    )


def generar_script_powerbi(config: Dict[str, str], factor_iqr: float = 1.5,
                            valores_fijos: Optional[dict] = None) -> str:
    """Script Python autocontenido para pegar en Power Query (Transformar -> Ejecutar script de Python)."""
    cabecera = '''# -*- coding: utf-8 -*-
# =============================================================================
# Script generado automaticamente por Limpiador de Tablas.
# Reproduce, sin depender del paquete data_cleaner, la MISMA configuracion
# de limpieza elegida en la interfaz (web/escritorio/API).
#
# COMO USARLO EN POWER BI:
#   1. Power Query -> clic derecho en la consulta -> Transformar ->
#      "Ejecutar script de Python".
#   2. Pegue todo este archivo en el cuadro de dialogo.
#   3. Power BI inyecta la tabla de entrada en la variable "dataset" y
#      ofrece dos resultados para expandir: "dataset_limpio" y
#      "reporte_limpieza".
#
# Requiere Python local con pandas y numpy (Power BI Desktop -> Opciones ->
# Python scripting).
# =============================================================================
import pandas as pd
import numpy as np

'''
    pie = '''

dataset_limpio, reporte_limpieza = limpiar_tabla(
    dataset,
    ACCION_FALTANTE, ACCION_DUPLICADO, ACCION_ATIPICO, ACCION_TIPO_INVALIDO,
    FACTOR_IQR, VALORES_FIJOS,
)
'''
    return cabecera + _bloque_config(config, factor_iqr, valores_fijos) + "\n\n" + _NUCLEO_LOGICA + pie


def generar_script_universal(config: Dict[str, str], factor_iqr: float = 1.5,
                              valores_fijos: Optional[dict] = None) -> str:
    """Script autocontenido para usar como libreria (Tableau Prep/TabPy, Alteryx, Qlik,
    notebooks) o como script de linea de comandos, con la misma configuracion elegida
    en la interfaz ya puesta como valor por defecto."""
    cabecera = '''# -*- coding: utf-8 -*-
# =============================================================================
# Script generado automaticamente por Limpiador de Tablas.
# Version autocontenida (solo pandas + numpy) con la MISMA configuracion de
# limpieza elegida en la interfaz.
#
# USO COMO LIBRERIA (Tableau Prep/TabPy, Alteryx, Qlik, notebooks):
#     from limpiador_universal_generado import limpiar_tabla, ACCION_FALTANTE, \\
#         ACCION_DUPLICADO, ACCION_ATIPICO, ACCION_TIPO_INVALIDO, FACTOR_IQR, VALORES_FIJOS
#     df_limpio, df_reporte = limpiar_tabla(df, ACCION_FALTANTE, ACCION_DUPLICADO,
#                                            ACCION_ATIPICO, ACCION_TIPO_INVALIDO,
#                                            FACTOR_IQR, VALORES_FIJOS)
#
# USO POR LINEA DE COMANDOS:
#     python limpiador_universal_generado.py entrada.csv
#     # genera entrada_limpio.csv y entrada_reporte_limpieza.csv junto al original
# =============================================================================
import sys
import os
import pandas as pd
import numpy as np

'''
    pie = '''

def _main_cli():
    if len(sys.argv) < 2:
        print("Uso: python limpiador_universal_generado.py archivo.csv")
        return
    ruta = sys.argv[1]
    if ruta.lower().endswith((".xlsx", ".xls", ".xlsm")):
        df = pd.read_excel(ruta)
    else:
        df = pd.read_csv(ruta)

    df_limpio, df_reporte = limpiar_tabla(
        df, ACCION_FALTANTE, ACCION_DUPLICADO, ACCION_ATIPICO, ACCION_TIPO_INVALIDO,
        FACTOR_IQR, VALORES_FIJOS,
    )

    base, _ext = os.path.splitext(os.path.basename(ruta))
    carpeta = os.path.dirname(os.path.abspath(ruta))
    ruta_limpio = os.path.join(carpeta, base + "_limpio.csv")
    ruta_reporte = os.path.join(carpeta, base + "_reporte_limpieza.csv")
    df_limpio.to_csv(ruta_limpio, index=False)
    df_reporte.to_csv(ruta_reporte, index=False)

    print(f"Filas originales: {len(df)} | Filas finales: {len(df_limpio)}")
    print(f"Hallazgos detectados: {len(df_reporte)}")
    print(f"Tabla limpia guardada en:  {ruta_limpio}")
    print(f"Reporte guardado en:       {ruta_reporte}")


if __name__ == "__main__":
    _main_cli()
'''
    return cabecera + _bloque_config(config, factor_iqr, valores_fijos) + "\n\n" + _NUCLEO_LOGICA + pie


def _escapar_m(texto: str) -> str:
    """Escapa un string Python para que quede valido dentro de un literal de texto M."""
    return texto.replace('"', '""').replace("\n", "#(lf)")


def generar_editor_m(config: Dict[str, str], factor_iqr: float = 1.5,
                      valores_fijos: Optional[dict] = None,
                      nombre_paso_anterior: str = "TuPasoAnterior") -> str:
    """Codigo M listo para pegar en el Editor avanzado de Power Query."""
    script_python = generar_script_powerbi(config, factor_iqr, valores_fijos)
    script_m = _escapar_m(script_python)
    return f'''// =============================================================================
// Codigo M generado automaticamente por Limpiador de Tablas.
// Pegar en Power Query -> clic derecho en la consulta -> "Editor avanzado",
// reemplazando el contenido existente (o insertando este paso dentro de tu
// secuencia, antes del "in" final). Ajuste "{nombre_paso_anterior}" al
// nombre real de su ultimo paso.
// =============================================================================

let
    Origen = {nombre_paso_anterior},

    EjecutarLimpieza = Python.Execute("{script_m}", [dataset=Origen]),

    // Para auditar los cambios en vez de obtener la tabla limpia, cambie
    // "dataset_limpio" por "reporte_limpieza" en la linea de abajo.
    TablaSeleccionada = EjecutarLimpieza{{[Name="dataset_limpio"]}}[Value]
in
    TablaSeleccionada
'''

#!/usr/bin/env python3
"""
Limpiador de Tablas — API REST (FastAPI)
==========================================
Expone el análisis y la limpieza como endpoints HTTP, para integrarlo
en otras apps o un frontend separado.

Uso:
    uvicorn api:app --reload
    (documentación interactiva en http://localhost:8000/docs)

Endpoints:
    POST /analizar   -> sube un archivo, devuelve el resumen de hallazgos (JSON)
    POST /limpiar     -> sube un archivo + configuración, devuelve datos_limpios y reporte
    GET  /descargar/{id}/{tipo}  -> descarga los archivos generados por /limpiar
"""
from __future__ import annotations

import io
import json
import uuid
from typing import Optional

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from data_cleaner import (
    analizar, limpiar, DEFAULT_CONFIG,
    construir_reporte, exportar_reporte_excel, exportar,
)
from data_cleaner.exportador import (
    generar_script_powerbi, generar_script_universal, generar_editor_m,
)
from data_cleaner.exportador_m import generar_editor_m_puro

app = FastAPI(
    title="Limpiador de Tablas API",
    description="Detección y corrección de calidad de datos en tablas CSV/Excel.",
    version="1.0.0",
)

# Almacén en memoria de resultados de /limpiar, para permitir su descarga
# posterior por separado (archivo limpio y reporte).
_RESULTADOS: dict[str, dict] = {}


class HallazgoOut(BaseModel):
    tipo: str
    columna: Optional[str]
    fila: int
    valor_original: Optional[str]
    detalle: str


class AnalisisOut(BaseModel):
    filas_analizadas: int
    columnas_analizadas: int
    total_hallazgos: int
    por_tipo: dict
    por_columna: dict
    hallazgos: list[HallazgoOut]


class LimpiezaOut(BaseModel):
    id: str
    filas_originales: int
    filas_finales: int
    total_correcciones: int
    resumen_por_tipo: dict


def _leer_upload(archivo: UploadFile) -> pd.DataFrame:
    nombre = (archivo.filename or "").lower()
    contenido = archivo.file.read()
    if nombre.endswith(".csv"):
        return pd.read_csv(io.BytesIO(contenido))
    if nombre.endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(contenido))
    raise HTTPException(status_code=400, detail="Formato no soportado. Use .csv, .xlsx o .xls.")


@app.get("/")
def raiz():
    return {"servicio": "Limpiador de Tablas API", "docs": "/docs"}


@app.post("/analizar", response_model=AnalisisOut)
def analizar_endpoint(
    archivo: UploadFile = File(..., description="Archivo CSV o Excel a analizar."),
    metodo_atipicos: str = Form("iqr", description="iqr | zscore | ambos"),
):
    if metodo_atipicos not in ("iqr", "zscore", "ambos"):
        raise HTTPException(status_code=400, detail="metodo_atipicos debe ser iqr, zscore o ambos.")

    df = _leer_upload(archivo)
    resultado = analizar(df, metodo_atipicos=metodo_atipicos)

    hallazgos = [
        HallazgoOut(
            tipo=i.tipo, columna=i.columna, fila=i.fila,
            valor_original=None if i.valor_original is None else str(i.valor_original),
            detalle=i.detalle,
        )
        for i in resultado.issues
    ]
    return AnalisisOut(
        filas_analizadas=resultado.filas_analizadas,
        columnas_analizadas=resultado.columnas_analizadas,
        total_hallazgos=len(resultado.issues),
        por_tipo=resultado.por_tipo(),
        por_columna=resultado.por_columna(),
        hallazgos=hallazgos,
    )


@app.post("/limpiar", response_model=LimpiezaOut)
def limpiar_endpoint(
    archivo: UploadFile = File(..., description="Archivo CSV o Excel a limpiar."),
    metodo_atipicos: str = Form("iqr"),
    faltante: str = Form(DEFAULT_CONFIG["faltante"]),
    duplicado: str = Form(DEFAULT_CONFIG["duplicado"]),
    atipico: str = Form(DEFAULT_CONFIG["atipico"]),
    tipo_invalido: str = Form(DEFAULT_CONFIG["tipo_invalido"]),
    valores_fijos: str = Form(
        "{}", description='JSON con valores fijos por columna, ej: {"edad": "0"}'
    ),
):
    df = _leer_upload(archivo)
    try:
        valores_fijos_dict = json.loads(valores_fijos) if valores_fijos else {}
        if not isinstance(valores_fijos_dict, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="valores_fijos debe ser un JSON de objeto (columna: valor).")

    config = {
        "faltante": faltante, "duplicado": duplicado,
        "atipico": atipico, "tipo_invalido": tipo_invalido,
    }

    resultado = analizar(df, metodo_atipicos=metodo_atipicos)

    faltan = [
        tipo for tipo, accion in config.items()
        if accion == "valor_fijo"
        and not any(i.columna in valores_fijos_dict for i in resultado.issues if i.tipo == tipo)
    ]
    if faltan:
        raise HTTPException(
            status_code=400,
            detail=f"Falta valor fijo en 'valores_fijos' para el/los tipo(s): {', '.join(faltan)}",
        )

    df_limpio, registro = limpiar(df, resultado.issues, config=config, valores_fijos=valores_fijos_dict)
    tablas_reporte = construir_reporte(resultado, registro, nombre_fuente=archivo.filename or "")

    resultado_id = str(uuid.uuid4())
    _RESULTADOS[resultado_id] = {"df_limpio": df_limpio, "tablas_reporte": tablas_reporte}

    return LimpiezaOut(
        id=resultado_id,
        filas_originales=len(df),
        filas_finales=len(df_limpio),
        total_correcciones=len(registro),
        resumen_por_tipo=resultado.por_tipo(),
    )


@app.post("/exportar/script")
def exportar_script_endpoint(
    formato: str = Form(..., description="powerbi | universal | m"),
    faltante: str = Form(DEFAULT_CONFIG["faltante"]),
    duplicado: str = Form(DEFAULT_CONFIG["duplicado"]),
    atipico: str = Form(DEFAULT_CONFIG["atipico"]),
    tipo_invalido: str = Form(DEFAULT_CONFIG["tipo_invalido"]),
    factor_iqr: float = Form(1.5),
    valores_fijos: str = Form("{}", description='JSON con valores fijos por columna'),
    nombre_paso_anterior: str = Form("TuPasoAnterior", description="Solo aplica a formato=m"),
):
    """Genera un script Python autocontenido (Power BI o universal) o el código M,
    con la misma configuración de limpieza indicada, para usar en otras herramientas."""
    if formato not in ("powerbi", "universal", "m"):
        raise HTTPException(status_code=400, detail="formato debe ser 'powerbi', 'universal' o 'm'.")

    try:
        valores_fijos_dict = json.loads(valores_fijos) if valores_fijos else {}
        if not isinstance(valores_fijos_dict, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="valores_fijos debe ser un JSON de objeto (columna: valor).")

    config = {
        "faltante": faltante, "duplicado": duplicado,
        "atipico": atipico, "tipo_invalido": tipo_invalido,
    }

    if formato == "powerbi":
        contenido = generar_script_powerbi(config, factor_iqr, valores_fijos_dict)
        nombre_archivo, media_type = "limpiador_powerbi_generado.py", "text/x-python"
    elif formato == "universal":
        contenido = generar_script_universal(config, factor_iqr, valores_fijos_dict)
        nombre_archivo, media_type = "limpiador_universal_generado.py", "text/x-python"
    else:
        contenido = generar_editor_m(config, factor_iqr, valores_fijos_dict, nombre_paso_anterior)
        nombre_archivo, media_type = "editor_avanzado_powerbi_generado.m", "text/plain"

    return StreamingResponse(
        io.BytesIO(contenido.encode("utf-8")),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{nombre_archivo}"'},
    )


@app.post("/exportar/script-m-puro")
def exportar_script_m_puro_endpoint(
    archivo: UploadFile = File(..., description="Mismo archivo CSV/Excel ya analizado."),
    faltante: str = Form(DEFAULT_CONFIG["faltante"]),
    duplicado: str = Form(DEFAULT_CONFIG["duplicado"]),
    atipico: str = Form(DEFAULT_CONFIG["atipico"]),
    tipo_invalido: str = Form(DEFAULT_CONFIG["tipo_invalido"]),
    fecha_invalida: str = Form("marcar_solo"),
    email_invalido: str = Form("marcar_solo"),
    telefono_invalido: str = Form("marcar_solo"),
    digitos_telefono_min: int = Form(8),
    digitos_telefono_max: int = Form(8),
    primeros_digitos_telefono_validos: str = Form(
        "", description='Coma-separado, ej: "2,4,5,6,7,8". Vacio = no validar.'
    ),
    id_duplicado: str = Form("marcar_solo"),
    formula_incorrecta: str = Form("marcar_solo"),
    texto_inconsistente: str = Form("marcar_solo"),
    factor_iqr: float = Form(1.5),
    valores_fijos: str = Form("{}", description='JSON con valores fijos por columna'),
    nombre_paso_anterior: str = Form("TuPasoAnterior"),
):
    """Genera codigo M 100% nativo (sin Python.Execute), a diferencia de
    /exportar/script?formato=m que genera un paso Python.Execute(...).
    Requiere el archivo de datos (no solo la config) porque las columnas de
    fecha/email/telefono/id/formula/texto se auto-detectan sobre los datos
    reales al momento de generar el codigo."""
    df = _leer_upload(archivo)
    try:
        valores_fijos_dict = json.loads(valores_fijos) if valores_fijos else {}
        if not isinstance(valores_fijos_dict, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="valores_fijos debe ser un JSON de objeto (columna: valor).")

    config = {
        "faltante": faltante, "duplicado": duplicado,
        "atipico": atipico, "tipo_invalido": tipo_invalido,
    }
    lista_primeros_digitos = (
        [d.strip() for d in primeros_digitos_telefono_validos.split(",") if d.strip()]
        or None
    )

    contenido = generar_editor_m_puro(
        df,
        config=config,
        factor_iqr=factor_iqr,
        valores_fijos=valores_fijos_dict,
        nombre_paso_anterior=nombre_paso_anterior,
        fecha_invalida=fecha_invalida,
        email_invalido=email_invalido,
        telefono_invalido=telefono_invalido,
        digitos_telefono=(digitos_telefono_min, digitos_telefono_max),
        primeros_digitos_telefono_validos=lista_primeros_digitos,
        id_duplicado=id_duplicado,
        formula_incorrecta=formula_incorrecta,
        texto_inconsistente=texto_inconsistente,
    )

    return StreamingResponse(
        io.BytesIO(contenido.encode("utf-8")),
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="codigo_m_puro_generado.m"'},
    )


@app.get("/descargar/{resultado_id}/{tipo}")
def descargar_endpoint(resultado_id: str, tipo: str):
    if resultado_id not in _RESULTADOS:
        raise HTTPException(status_code=404, detail="No existe ese resultado (o ya expiró).")
    if tipo not in ("datos", "reporte"):
        raise HTTPException(status_code=400, detail="tipo debe ser 'datos' o 'reporte'.")

    datos = _RESULTADOS[resultado_id]
    buf = io.BytesIO()
    if tipo == "datos":
        exportar(datos["df_limpio"], buf, kind="excel")
        nombre = "datos_limpios.xlsx"
    else:
        exportar_reporte_excel(datos["tablas_reporte"], buf)
        nombre = "reporte_calidad_datos.xlsx"

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )

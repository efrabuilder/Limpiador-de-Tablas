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

Limpieza guiada de nulos, merge y diccionario de datos:
    POST /limpieza-guiada/diagnostico -> diagnóstico de nulos y reglas sugeridas por columna
    POST /limpieza-guiada             -> limpia (pasos globales + una regla por columna)
    POST /merge/diagnostico           -> sugiere y revisa las llaves de dos tablas
    POST /merge                       -> une A con B y audita el resultado
    POST /diccionario                 -> devuelve el diccionario de datos (Excel)
    GET  /descargar-guiado/{id}/{tipo} -> datos | script | diccionario de /limpieza-guiada o /merge
"""
from __future__ import annotations

import io
import json
import uuid
import zipfile
from typing import Callable, Optional

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
from data_cleaner import diccionario_datos as DD
from data_cleaner import flujos_guiados as FG
from data_cleaner import limpieza_guiada as LG
from data_cleaner import merge_tablas as MT
from data_cleaner.exportador_m import generar_editor_m_puro
from data_cleaner.loaders import load_excel, load_table, load_excel_hojas, leer_tabla_subida, tabla_a_bytes
from data_cleaner.exporters import exportar_sql
from data_cleaner.modelo_sql import aplicar_modelo_sql, generar_dot_modelo, generar_script_crear_base_datos
from data_cleaner.patrones import FORMATOS_FECHA_DISPONIBLES, formato_fecha_python, formato_fecha_m

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
    columnas_excluidas_atipicos_por_contenido: list[str] = []


class ExportarSqlIn(BaseModel):
    connection_string: str
    table_name: str
    if_exists: str = "replace"  # replace | append | fail


class AplicarModeloSqlOut(BaseModel):
    mensajes: list[str]


class DiagramaModeloIn(BaseModel):
    modelo: dict


class CrearBaseDatosIn(BaseModel):
    nombre: str
    motor: str = "sql_server"  # sql_server | mysql | postgresql


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
        return load_excel(io.BytesIO(contenido))
    raise HTTPException(status_code=400, detail="Formato no soportado. Use .csv, .xlsx o .xls.")


@app.get("/")
def raiz():
    return {"servicio": "Limpiador de Tablas API", "docs": "/docs"}


def _parsear_paises_telefono(valor: str) -> Optional[list[str]]:
    """'cr,mexico' -> ['cr', 'mexico']. None si viene vacío (rango
    internacional amplio de 7-15 dígitos por defecto)."""
    if not valor:
        return None
    return [p.strip() for p in valor.split(",") if p.strip()]


def _parsear_correcciones_individuales_json(texto: str) -> dict:
    """Convierte el JSON recibido por HTTP (una lista de objetos, ya que JSON
    no soporta tuplas como llave) al dict {(tipo, columna, fila): valor} que
    esperan generar_script_powerbi/universal/m y generar_editor_m_puro.
    Formato esperado: [{"tipo": "faltante", "columna": "edad", "fila": 3,
    "valor": "0"}, ...]."""
    if not texto:
        return {}
    try:
        lista = json.loads(texto)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="correcciones_individuales debe ser un JSON válido.")
    if not isinstance(lista, list):
        raise HTTPException(
            status_code=400,
            detail='correcciones_individuales debe ser una lista de objetos: '
                   '[{"tipo": ..., "columna": ..., "fila": ..., "valor": ...}].',
        )
    resultado = {}
    for item in lista:
        if not isinstance(item, dict) or not {"tipo", "columna", "fila", "valor"} <= item.keys():
            raise HTTPException(
                status_code=400,
                detail='Cada elemento de correcciones_individuales debe tener '
                       '"tipo", "columna", "fila" y "valor".',
            )
        resultado[(item["tipo"], item["columna"], int(item["fila"]))] = item["valor"]
    return resultado


def _parsear_formatos_fecha_json(texto: str) -> dict:
    """Convierte el JSON {"columna": "clave", ...} recibido por HTTP al dict
    que espera 'formatos_fecha' (solo aplica cuando fecha_invalida =
    "normalizar_formato_fecha"). 'clave' debe ser una de
    patrones.FORMATOS_FECHA_DISPONIBLES. Formato esperado:
    {"fecha_venta": "dd/mm/aaaa"}."""
    if not texto:
        return {}
    try:
        obj = json.loads(texto)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="formatos_fecha debe ser un JSON válido.")
    if not isinstance(obj, dict):
        raise HTTPException(status_code=400, detail="formatos_fecha debe ser un JSON de objeto (columna: clave).")
    claves_validas = ", ".join(FORMATOS_FECHA_DISPONIBLES.keys())
    for col, clave in obj.items():
        if clave not in FORMATOS_FECHA_DISPONIBLES:
            raise HTTPException(
                status_code=400,
                detail=f"formatos_fecha inválido para '{col}': '{clave}'. Válidas: {claves_validas}",
            )
    return obj


@app.post("/analizar", response_model=AnalisisOut)
def analizar_endpoint(
    archivo: UploadFile = File(..., description="Archivo CSV o Excel a analizar."),
    metodo_atipicos: str = Form("iqr", description="iqr | zscore | ambos"),
    paises_telefono: str = Form(
        "", description='País(es) para el rango de dígitos de celular, coma-separados '
                         '(ej. "cr,mexico"). Vacío = rango internacional amplio (7-15 dígitos).'
    ),
    digitos_telefono_min: Optional[int] = Form(
        None, description="Rango explícito de dígitos (anula paises_telefono si se indica junto con el max)."
    ),
    digitos_telefono_max: Optional[int] = Form(None),
    permitir_codigo_pais_telefono: bool = Form(
        True, description="Acepta el mismo número con 1-3 dígitos extra al inicio (código de país sin '+')."
    ),
):
    if metodo_atipicos not in ("iqr", "zscore", "ambos"):
        raise HTTPException(status_code=400, detail="metodo_atipicos debe ser iqr, zscore o ambos.")

    digitos_telefono = (
        (digitos_telefono_min, digitos_telefono_max)
        if digitos_telefono_min is not None and digitos_telefono_max is not None
        else None
    )
    df = _leer_upload(archivo)
    resultado = analizar(
        df, metodo_atipicos=metodo_atipicos,
        digitos_telefono=digitos_telefono,
        paises_telefono=_parsear_paises_telefono(paises_telefono),
        permitir_codigo_pais_telefono=permitir_codigo_pais_telefono,
    )

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
        columnas_excluidas_atipicos_por_contenido=resultado.columnas_excluidas_atipicos_por_contenido,
    )


@app.post("/analizar-sql", response_model=AnalisisOut)
def analizar_sql_endpoint(
    connection_string: str = Form(..., description="Cadena de conexión SQLAlchemy de origen."),
    table_name: Optional[str] = Form(None, description="Tabla a leer (o use query)."),
    query: Optional[str] = Form(None, description="Consulta SQL a ejecutar (o use table_name)."),
    metodo_atipicos: str = Form("iqr", description="iqr | zscore | ambos"),
    paises_telefono: str = Form(""),
    digitos_telefono_min: Optional[int] = Form(None),
    digitos_telefono_max: Optional[int] = Form(None),
    permitir_codigo_pais_telefono: bool = Form(True),
):
    """Igual que /analizar, pero leyendo la tabla desde una base de datos SQL
    en vez de un archivo subido."""
    if not table_name and not query:
        raise HTTPException(status_code=400, detail="Debe indicar table_name o query.")
    if metodo_atipicos not in ("iqr", "zscore", "ambos"):
        raise HTTPException(status_code=400, detail="metodo_atipicos debe ser iqr, zscore o ambos.")

    try:
        df = load_table(connection_string, kind="sql", table_name=table_name, query=query)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo leer de la base de datos: {exc}")

    digitos_telefono = (
        (digitos_telefono_min, digitos_telefono_max)
        if digitos_telefono_min is not None and digitos_telefono_max is not None
        else None
    )
    resultado = analizar(
        df, metodo_atipicos=metodo_atipicos,
        digitos_telefono=digitos_telefono,
        paises_telefono=_parsear_paises_telefono(paises_telefono),
        permitir_codigo_pais_telefono=permitir_codigo_pais_telefono,
    )
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
        columnas_excluidas_atipicos_por_contenido=resultado.columnas_excluidas_atipicos_por_contenido,
    )


@app.post("/limpiar", response_model=LimpiezaOut)
def limpiar_endpoint(
    archivo: UploadFile = File(..., description="Archivo CSV o Excel a limpiar."),
    metodo_atipicos: str = Form("iqr"),
    faltante: str = Form(DEFAULT_CONFIG["faltante"]),
    duplicado: str = Form(DEFAULT_CONFIG["duplicado"]),
    atipico: str = Form(DEFAULT_CONFIG["atipico"]),
    tipo_invalido: str = Form(DEFAULT_CONFIG["tipo_invalido"]),
    fecha_invalida: str = Form(DEFAULT_CONFIG["fecha_invalida"]),
    email_invalido: str = Form(DEFAULT_CONFIG["email_invalido"]),
    telefono_invalido: str = Form(DEFAULT_CONFIG["telefono_invalido"]),
    id_duplicado: str = Form(DEFAULT_CONFIG["id_duplicado"]),
    formula_incorrecta: str = Form(DEFAULT_CONFIG["formula_incorrecta"]),
    texto_inconsistente: str = Form(DEFAULT_CONFIG["texto_inconsistente"]),
    estado_invalido: str = Form(DEFAULT_CONFIG["estado_invalido"]),
    capitalizacion_incorrecta: str = Form(DEFAULT_CONFIG["capitalizacion_incorrecta"]),
    espacio_extra: str = Form(DEFAULT_CONFIG["espacio_extra"]),
    paises_telefono: str = Form(
        "", description='País(es) para el rango de dígitos de celular, coma-separados '
                         '(ej. "cr,mexico"). Vacío = rango internacional amplio (7-15 dígitos).'
    ),
    digitos_telefono_min: Optional[int] = Form(None),
    digitos_telefono_max: Optional[int] = Form(None),
    permitir_codigo_pais_telefono: bool = Form(True),
    valores_fijos: str = Form(
        "{}", description='JSON con valores fijos por columna, ej: {"edad": "0"}'
    ),
    formatos_fecha: str = Form(
        "{}", description='Solo si fecha_invalida="normalizar_formato_fecha". JSON '
                           'columna->clave de formato, ej: {"fecha_venta": "dd/mm/aaaa"}. '
                           f'Claves válidas: {", ".join(FORMATOS_FECHA_DISPONIBLES.keys())}.'
    ),
):
    df = _leer_upload(archivo)
    try:
        valores_fijos_dict = json.loads(valores_fijos) if valores_fijos else {}
        if not isinstance(valores_fijos_dict, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="valores_fijos debe ser un JSON de objeto (columna: valor).")
    formatos_fecha_dict = _parsear_formatos_fecha_json(formatos_fecha)

    config = {
        "faltante": faltante, "duplicado": duplicado,
        "atipico": atipico, "tipo_invalido": tipo_invalido,
        "fecha_invalida": fecha_invalida, "email_invalido": email_invalido,
        "telefono_invalido": telefono_invalido, "id_duplicado": id_duplicado,
        "formula_incorrecta": formula_incorrecta, "texto_inconsistente": texto_inconsistente,
        "estado_invalido": estado_invalido, "capitalizacion_incorrecta": capitalizacion_incorrecta,
        "espacio_extra": espacio_extra,
    }

    digitos_telefono = (
        (digitos_telefono_min, digitos_telefono_max)
        if digitos_telefono_min is not None and digitos_telefono_max is not None
        else None
    )
    resultado = analizar(
        df, metodo_atipicos=metodo_atipicos,
        digitos_telefono=digitos_telefono,
        paises_telefono=_parsear_paises_telefono(paises_telefono),
        permitir_codigo_pais_telefono=permitir_codigo_pais_telefono,
    )

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

    df_limpio, registro = limpiar(df, resultado.issues, config=config, valores_fijos=valores_fijos_dict,
                                   formatos_fecha=formatos_fecha_dict)
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


@app.post("/limpiar-sql", response_model=LimpiezaOut)
def limpiar_sql_endpoint(
    connection_string: str = Form(..., description="Cadena de conexión SQLAlchemy de origen."),
    table_name: Optional[str] = Form(None, description="Tabla a leer (o use query)."),
    query: Optional[str] = Form(None, description="Consulta SQL a ejecutar (o use table_name)."),
    metodo_atipicos: str = Form("iqr"),
    faltante: str = Form(DEFAULT_CONFIG["faltante"]),
    duplicado: str = Form(DEFAULT_CONFIG["duplicado"]),
    atipico: str = Form(DEFAULT_CONFIG["atipico"]),
    tipo_invalido: str = Form(DEFAULT_CONFIG["tipo_invalido"]),
    fecha_invalida: str = Form(DEFAULT_CONFIG["fecha_invalida"]),
    email_invalido: str = Form(DEFAULT_CONFIG["email_invalido"]),
    telefono_invalido: str = Form(DEFAULT_CONFIG["telefono_invalido"]),
    id_duplicado: str = Form(DEFAULT_CONFIG["id_duplicado"]),
    formula_incorrecta: str = Form(DEFAULT_CONFIG["formula_incorrecta"]),
    texto_inconsistente: str = Form(DEFAULT_CONFIG["texto_inconsistente"]),
    estado_invalido: str = Form(DEFAULT_CONFIG["estado_invalido"]),
    capitalizacion_incorrecta: str = Form(DEFAULT_CONFIG["capitalizacion_incorrecta"]),
    espacio_extra: str = Form(DEFAULT_CONFIG["espacio_extra"]),
    paises_telefono: str = Form(""),
    digitos_telefono_min: Optional[int] = Form(None),
    digitos_telefono_max: Optional[int] = Form(None),
    permitir_codigo_pais_telefono: bool = Form(True),
    valores_fijos: str = Form("{}"),
    formatos_fecha: str = Form(
        "{}", description='Solo si fecha_invalida="normalizar_formato_fecha". JSON '
                           'columna->clave de formato, ej: {"fecha_venta": "dd/mm/aaaa"}. '
                           f'Claves válidas: {", ".join(FORMATOS_FECHA_DISPONIBLES.keys())}.'
    ),
):
    """Igual que /limpiar, pero leyendo la tabla de origen desde SQL en vez
    de un archivo subido. El resultado queda guardado bajo un id, igual que
    /limpiar, para descargarlo por /descargar/{id}/{tipo} o escribirlo de
    vuelta a SQL con /exportar-sql/{id}."""
    if not table_name and not query:
        raise HTTPException(status_code=400, detail="Debe indicar table_name o query.")
    try:
        df = load_table(connection_string, kind="sql", table_name=table_name, query=query)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo leer de la base de datos: {exc}")

    try:
        valores_fijos_dict = json.loads(valores_fijos) if valores_fijos else {}
        if not isinstance(valores_fijos_dict, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="valores_fijos debe ser un JSON de objeto (columna: valor).")
    formatos_fecha_dict = _parsear_formatos_fecha_json(formatos_fecha)

    config = {
        "faltante": faltante, "duplicado": duplicado,
        "atipico": atipico, "tipo_invalido": tipo_invalido,
        "fecha_invalida": fecha_invalida, "email_invalido": email_invalido,
        "telefono_invalido": telefono_invalido, "id_duplicado": id_duplicado,
        "formula_incorrecta": formula_incorrecta, "texto_inconsistente": texto_inconsistente,
        "estado_invalido": estado_invalido, "capitalizacion_incorrecta": capitalizacion_incorrecta,
        "espacio_extra": espacio_extra,
    }
    digitos_telefono = (
        (digitos_telefono_min, digitos_telefono_max)
        if digitos_telefono_min is not None and digitos_telefono_max is not None
        else None
    )
    resultado = analizar(
        df, metodo_atipicos=metodo_atipicos,
        digitos_telefono=digitos_telefono,
        paises_telefono=_parsear_paises_telefono(paises_telefono),
        permitir_codigo_pais_telefono=permitir_codigo_pais_telefono,
    )

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

    df_limpio, registro = limpiar(df, resultado.issues, config=config, valores_fijos=valores_fijos_dict,
                                   formatos_fecha=formatos_fecha_dict)
    tablas_reporte = construir_reporte(resultado, registro, nombre_fuente=table_name or "consulta_sql")

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
    fecha_invalida: str = Form(DEFAULT_CONFIG["fecha_invalida"]),
    email_invalido: str = Form(DEFAULT_CONFIG["email_invalido"]),
    telefono_invalido: str = Form(DEFAULT_CONFIG["telefono_invalido"]),
    id_duplicado: str = Form(DEFAULT_CONFIG["id_duplicado"]),
    formula_incorrecta: str = Form(DEFAULT_CONFIG["formula_incorrecta"]),
    texto_inconsistente: str = Form(DEFAULT_CONFIG["texto_inconsistente"]),
    estado_invalido: str = Form(DEFAULT_CONFIG["estado_invalido"]),
    capitalizacion_incorrecta: str = Form(DEFAULT_CONFIG["capitalizacion_incorrecta"]),
    espacio_extra: str = Form(DEFAULT_CONFIG["espacio_extra"]),
    factor_iqr: float = Form(1.5),
    valores_fijos: str = Form("{}", description='JSON con valores fijos por columna'),
    correcciones_individuales: str = Form(
        "[]", description='JSON: lista de objetos [{"tipo":..,"columna":..,"fila":..,"valor":..}] (opcional).'
    ),
    formatos_fecha: str = Form(
        "{}", description='Solo si fecha_invalida="normalizar_formato_fecha". JSON '
                           'columna->clave de formato, ej: {"fecha_venta": "dd/mm/aaaa"}. '
                           f'Claves válidas: {", ".join(FORMATOS_FECHA_DISPONIBLES.keys())}.'
    ),
    nombre_paso_anterior: str = Form("TuPasoAnterior", description="Solo aplica a formato=m"),
):
    """Genera un script Python autocontenido (Power BI o universal) o el código M,
    con la misma configuración de limpieza indicada (las 13 categorías), para
    usar en otras herramientas."""
    if formato not in ("powerbi", "universal", "m"):
        raise HTTPException(status_code=400, detail="formato debe ser 'powerbi', 'universal' o 'm'.")

    try:
        valores_fijos_dict = json.loads(valores_fijos) if valores_fijos else {}
        if not isinstance(valores_fijos_dict, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="valores_fijos debe ser un JSON de objeto (columna: valor).")
    correcciones_individuales_dict = _parsear_correcciones_individuales_json(correcciones_individuales)
    formatos_fecha_dict = _parsear_formatos_fecha_json(formatos_fecha)
    # generar_script_powerbi/universal/m no conocen el catálogo de claves
    # (patrones.FORMATOS_FECHA_DISPONIBLES); reciben directamente el
    # formato ya resuelto (strftime de Python).
    formatos_fecha_python_dict = {c: formato_fecha_python(clave) for c, clave in formatos_fecha_dict.items()}

    config = {
        "faltante": faltante, "duplicado": duplicado,
        "atipico": atipico, "tipo_invalido": tipo_invalido,
        "fecha_invalida": fecha_invalida, "email_invalido": email_invalido,
        "telefono_invalido": telefono_invalido, "id_duplicado": id_duplicado,
        "formula_incorrecta": formula_incorrecta, "texto_inconsistente": texto_inconsistente,
        "estado_invalido": estado_invalido, "capitalizacion_incorrecta": capitalizacion_incorrecta,
        "espacio_extra": espacio_extra,
    }

    if formato == "powerbi":
        contenido = generar_script_powerbi(
            config, factor_iqr, valores_fijos_dict, correcciones_individuales=correcciones_individuales_dict,
            formatos_fecha=formatos_fecha_python_dict,
        )
        nombre_archivo, media_type = "limpiador_powerbi_generado.py", "text/x-python"
    elif formato == "universal":
        contenido = generar_script_universal(
            config, factor_iqr, valores_fijos_dict, correcciones_individuales=correcciones_individuales_dict,
            formatos_fecha=formatos_fecha_python_dict,
        )
        nombre_archivo, media_type = "limpiador_universal_generado.py", "text/x-python"
    else:
        contenido = generar_editor_m(
            config, factor_iqr, valores_fijos_dict, nombre_paso_anterior,
            correcciones_individuales=correcciones_individuales_dict,
            formatos_fecha=formatos_fecha_python_dict,
        )
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
    paises_telefono: str = Form(
        "", description='País(es) para el rango de dígitos de celular, coma-separados '
                         '(ej. "cr,mexico"). Ignorado si se indican digitos_telefono_min/max. '
                         'Si ninguno de los dos se indica: rango internacional amplio (7-15 dígitos).'
    ),
    digitos_telefono_min: Optional[int] = Form(None),
    digitos_telefono_max: Optional[int] = Form(None),
    permitir_codigo_pais_telefono: bool = Form(True),
    primeros_digitos_telefono_validos: str = Form(
        "", description='Coma-separado, ej: "2,4,5,6,7,8". Vacio = no validar.'
    ),
    id_duplicado: str = Form("marcar_solo"),
    formula_incorrecta: str = Form("marcar_solo"),
    texto_inconsistente: str = Form("marcar_solo"),
    estado_invalido: str = Form("marcar_solo"),
    capitalizacion_incorrecta: str = Form("marcar_solo"),
    espacio_extra: str = Form(DEFAULT_CONFIG["espacio_extra"]),
    factor_iqr: float = Form(1.5),
    valores_fijos: str = Form("{}", description='JSON con valores fijos por columna'),
    correcciones_individuales: str = Form(
        "[]", description='JSON: lista de objetos [{"tipo":..,"columna":..,"fila":..,"valor":..}] (opcional).'
    ),
    formatos_fecha: str = Form(
        "{}", description='Solo si fecha_invalida="normalizar_formato_fecha". JSON '
                           'columna->clave de formato, ej: {"fecha_venta": "dd/mm/aaaa"}. '
                           f'Claves válidas: {", ".join(FORMATOS_FECHA_DISPONIBLES.keys())}.'
    ),
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
    correcciones_individuales_dict = _parsear_correcciones_individuales_json(correcciones_individuales)

    config = {
        "faltante": faltante, "duplicado": duplicado,
        "atipico": atipico, "tipo_invalido": tipo_invalido,
    }
    lista_primeros_digitos = (
        [d.strip() for d in primeros_digitos_telefono_validos.split(",") if d.strip()]
        or None
    )

    digitos_telefono = (
        (digitos_telefono_min, digitos_telefono_max)
        if digitos_telefono_min is not None and digitos_telefono_max is not None
        else None
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
        digitos_telefono=digitos_telefono,
        paises_telefono=_parsear_paises_telefono(paises_telefono),
        permitir_codigo_pais_telefono=permitir_codigo_pais_telefono,
        primeros_digitos_telefono_validos=lista_primeros_digitos,
        id_duplicado=id_duplicado,
        formula_incorrecta=formula_incorrecta,
        texto_inconsistente=texto_inconsistente,
        estado_invalido=estado_invalido,
        capitalizacion_incorrecta=capitalizacion_incorrecta,
        espacio_extra=espacio_extra,
        correcciones_individuales=correcciones_individuales_dict,
        formatos_fecha={c: formato_fecha_m(clave) for c, clave in _parsear_formatos_fecha_json(formatos_fecha).items()},
    )

    return StreamingResponse(
        io.BytesIO(contenido.encode("utf-8")),
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="codigo_m_puro_generado.m"'},
    )


@app.get("/descargar/{resultado_id}/{tipo}")
def descargar_endpoint(resultado_id: str, tipo: str, formato: str = "excel"):
    if resultado_id not in _RESULTADOS:
        raise HTTPException(status_code=404, detail="No existe ese resultado (o ya expiró).")
    if tipo not in ("datos", "reporte"):
        raise HTTPException(status_code=400, detail="tipo debe ser 'datos' o 'reporte'.")
    if formato not in ("excel", "csv"):
        raise HTTPException(status_code=400, detail="formato debe ser 'excel' o 'csv'.")
    if tipo == "reporte" and formato == "csv":
        raise HTTPException(status_code=400, detail="El reporte de calidad solo está disponible en excel (tiene varias hojas).")

    datos = _RESULTADOS[resultado_id]
    buf = io.BytesIO()
    if tipo == "datos" and formato == "csv":
        exportar(datos["df_limpio"], buf, kind="csv")
        nombre, media_type = "datos_limpios.csv", "text/csv"
    elif tipo == "datos":
        exportar(datos["df_limpio"], buf, kind="excel")
        nombre = "datos_limpios.xlsx"
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        exportar_reporte_excel(datos["tablas_reporte"], buf)
        nombre = "reporte_calidad_datos.xlsx"
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@app.post("/exportar-sql/{resultado_id}")
def exportar_sql_endpoint(resultado_id: str, body: ExportarSqlIn):
    """Escribe el df_limpio de un resultado de /limpiar o /limpiar-sql de
    vuelta en una base de datos SQL destino (misma u otra que el origen)."""
    if resultado_id not in _RESULTADOS:
        raise HTTPException(status_code=404, detail="No existe ese resultado (o ya expiró).")
    if body.if_exists not in ("replace", "append", "fail"):
        raise HTTPException(status_code=400, detail="if_exists debe ser 'replace', 'append' o 'fail'.")

    df_limpio = _RESULTADOS[resultado_id]["df_limpio"]
    try:
        mensaje = exportar_sql(df_limpio, body.connection_string, body.table_name, if_exists=body.if_exists)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo escribir en la base de datos: {exc}")

    return {"mensaje": mensaje}


@app.post("/modelo-sql", response_model=AplicarModeloSqlOut)
def modelo_sql_endpoint(
    archivo: UploadFile = File(..., description="Excel con las hojas del modelo (una tabla por hoja)."),
    modelo: str = Form(
        ..., description='JSON con el modelo: {"nombre_tabla": {"hoja": "...", '
                          '"clave_primaria": "...", "claves_foraneas": [{"columna": "...", '
                          '"tabla_referencia": "...", "columna_referencia": "..."}]}, ...}. '
                          "Ver data_cleaner/modelo_sql.py.",
    ),
    connection_string: str = Form(..., description="Cadena de conexión SQLAlchemy destino."),
    if_exists: str = Form("replace", description="replace | append | fail."),
):
    """
    Carga varias hojas del Excel subido y las escribe en SQL como un
    modelo de datos en ESTRELLA o COPO DE NIEVE: escribe los datos y
    agrega las restricciones de llave primaria (PK) y llave foránea (FK).
    Con PK/FK conviene if_exists='replace': con 'append' las restricciones
    pueden fallar si la tabla ya tiene valores repetidos o nulos.
    """
    if if_exists not in ("replace", "append", "fail"):
        raise HTTPException(status_code=400, detail="if_exists debe ser 'replace', 'append' o 'fail'.")
    try:
        modelo_dict = json.loads(modelo)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="modelo debe ser un JSON válido.")
    if not isinstance(modelo_dict, dict) or not modelo_dict:
        raise HTTPException(status_code=400, detail="modelo debe ser un objeto JSON no vacío (tabla -> definición).")

    contenido = archivo.file.read()
    hojas_necesarias = sorted({definicion.get("hoja") for definicion in modelo_dict.values()})
    try:
        hojas_cargadas = load_excel_hojas(io.BytesIO(contenido), hojas=hojas_necesarias)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el Excel: {exc}")

    try:
        mensajes = aplicar_modelo_sql(modelo_dict, hojas_cargadas, connection_string, if_exists=if_exists)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo crear el modelo: {exc}")

    return AplicarModeloSqlOut(mensajes=mensajes)


@app.post("/modelo-sql/diagrama")
def modelo_sql_diagrama_endpoint(body: DiagramaModeloIn):
    """
    Devuelve el diagrama del modelo (formato Graphviz DOT), sin necesidad
    de subir el Excel ni tocar la base de datos — útil para previsualizar
    antes de aplicar /modelo-sql (péguelo en
    https://dreampuf.github.io/GraphvizOnline para verlo).
    """
    return {"dot": generar_dot_modelo(body.modelo)}


@app.post("/modelo-sql/crear-base-datos")
def modelo_sql_crear_base_datos_endpoint(body: CrearBaseDatosIn):
    """
    Genera el script SQL (CREATE DATABASE / USE) para cuando la base de
    datos destino TODAVÍA NO EXISTE — no requiere ninguna cadena de
    conexión. Péguelo en SSMS/mysql/psql; una vez creada la base, recién
    ahí use /modelo-sql con connection_string apuntando a esa base.
    """
    try:
        script = generar_script_crear_base_datos(body.nombre, body.motor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"script": script}


# =============================================================================
# Limpieza guiada, merge y diccionario de datos
# =============================================================================

MEDIA_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Resultados de /limpieza-guiada y /merge, para descargarlos por separado.
_RESULTADOS_GUIADOS: dict[str, dict] = {}


def _o_400(funcion: Callable, *args, **kwargs):
    """Llama a `funcion` y convierte los errores de validación (columna que no
    existe, regla inválida, hoja sin indicar...) en un 400 con el mensaje."""
    try:
        return funcion(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _a_json(valor):
    """Convierte numpy / DataFrames a tipos que FastAPI sabe serializar."""
    def convertir(objeto):
        if isinstance(objeto, pd.DataFrame):
            return objeto.to_dict("records")
        if hasattr(objeto, "item"):
            return objeto.item()
        return str(objeto)

    def sin_nan(objeto):
        if isinstance(objeto, float) and (objeto != objeto or objeto in (float("inf"), float("-inf"))):
            return None
        if isinstance(objeto, dict):
            return {k: sin_nan(v) for k, v in objeto.items()}
        if isinstance(objeto, list):
            return [sin_nan(v) for v in objeto]
        return objeto

    return sin_nan(json.loads(json.dumps(valor, default=convertir)))


def _leer_upload_guiado(archivo: UploadFile, hoja: Optional[str] = None) -> pd.DataFrame:
    """Lee el CSV / Excel subido como lo hace la app web (todo como texto)."""
    buffer = io.BytesIO(archivo.file.read())
    buffer.name = archivo.filename or ""
    return _o_400(leer_tabla_subida, buffer, nombre=buffer.name, hoja=hoja or None)


def _lista_o_none(valor: Optional[str]) -> Optional[list[str]]:
    """None = elegir automáticamente; cadena vacía = ninguna."""
    if valor is None:
        return None
    return [c.strip() for c in valor.split(",") if c.strip()]


def _rango(valor: Optional[str], nombre: str) -> Optional[tuple]:
    if not valor:
        return None
    partes = valor.split(",")
    if len(partes) != 2:
        raise HTTPException(status_code=400, detail=f"{nombre} debe ser 'minimo,maximo'.")
    return (float(partes[0]), float(partes[1]))


def _json_dict(texto: str, nombre: str) -> dict:
    if not texto.strip():
        return {}
    datos = _o_400(json.loads, texto) if texto.strip().startswith("{") else None
    if not isinstance(datos, dict):
        raise HTTPException(status_code=400, detail=f"{nombre} debe ser un objeto JSON, ej. {{\"col\": \"valor\"}}.")
    return datos


def _guardar_guiado(tipo: str, base: str, df: pd.DataFrame, script: str, argumentos_diccionario: dict) -> str:
    """Guarda un resultado guiado. Los diccionarios (Excel basico, tecnico y documento de alcance)
    no se arman aqui: tardan en tablas grandes y casi nunca se bajan los tres; se arman al
    pedirlos, con estos argumentos."""
    resultado_id = str(uuid.uuid4())
    _RESULTADOS_GUIADOS[resultado_id] = {
        "tipo": tipo, "base": base, "df": df, "script": script, "argumentos": argumentos_diccionario}
    return resultado_id


def _documentos_diccionario(datos: dict) -> dict:
    """{'basico', 'tecnico', 'alcance'} de un resultado guardado (se calcula una vez y se recuerda)."""
    if "documentos" not in datos:
        argumentos = dict(datos["argumentos"])
        nombre = argumentos.pop("nombre")
        datos["documentos"] = _o_400(FG.generar_documentos_diccionario, datos["df"], nombre, **argumentos)
    return datos["documentos"]


@app.post("/limpieza-guiada/diagnostico")
def limpieza_guiada_diagnostico_endpoint(
    archivo: UploadFile = File(...),
    hoja: Optional[str] = Form(None, description="Hoja del libro Excel (si tiene varias)."),
    tokens_extra: Optional[str] = Form(None, description="Textos que cuentan como nulo, coma-separados. "
                                       "Por defecto: nan,none,null,n/a y los textos tipo «sin dato» que aparezcan en la tabla (unknown, -, not available...)."),
    nombres_snake: bool = Form(True),
):
    """Diagnóstico de nulos por columna y las reglas que sugiere la app para cada una."""
    df = _leer_upload_guiado(archivo, hoja)
    config = _o_400(FG.configurar_limpieza_guiada, df, tokens_extra=_lista_o_none(tokens_extra),
                    nombres_snake=nombres_snake)
    df_base, _ = _o_400(LG.ejecutar_pasos_globales, df, config)
    return _a_json({
        "filas": len(df), "columnas": len(df.columns),
        "diagnostico": LG.diagnostico_nulos(df, config["tokens"]).reset_index(),
        "reglas_sugeridas": FG.reglas_sugeridas(df_base, config["tokens"]),
        "reglas_disponibles": LG.REGLAS_NULOS,
    })


@app.post("/limpieza-guiada")
def limpieza_guiada_endpoint(
    archivo: UploadFile = File(...),
    hoja: Optional[str] = Form(None),
    formato: str = Form("csv", description="csv | xlsx (formato de la tabla al descargar)."),
    tokens_extra: Optional[str] = Form(None, description="Coma-separados. Por defecto: nan,none,null,n/a y los textos tipo «sin dato» que aparezcan en la tabla (unknown, -, not available...)."),
    nombres_snake: bool = Form(True),
    vacios_a_nan: bool = Form(True),
    estandarizar_texto: bool = Form(True),
    columnas_texto: Optional[str] = Form(None, description="Coma-separadas. Sin enviar = automático; vacío = ninguna."),
    minusculas: bool = Form(True),
    espacios: bool = Form(True),
    comillas: bool = Form(True),
    numericas: Optional[str] = Form(None, description="Coma-separadas. Sin enviar = automático; vacío = ninguna."),
    latitud: Optional[str] = Form(None, description="Sin enviar = automática; vacío = ninguna."),
    longitud: Optional[str] = Form(None, description="Sin enviar = automática; vacío = ninguna."),
    rango_latitud: Optional[str] = Form(None, description="minimo,maximo esperado."),
    rango_longitud: Optional[str] = Form(None, description="minimo,maximo esperado."),
    fechas: Optional[str] = Form(None, description="Columnas de fecha a dejar en un solo formato, coma-separadas. "
                                 "Sin enviar = automático; vacío = ninguna."),
    formato_fecha: str = Form("%Y-%m-%d", description="Formato de salida de las fechas (strftime)."),
    dia_primero: bool = Form(True, description="05/06/2025 = día/mes/año cuando no hay pistas (false = mes/día)."),
    palabra: str = Form(LG.TEXTO_NO_INDICA, description="Palabra para los nulos válidos."),
    unir_equivalentes: bool = Form(True, description="Unir valores que significan lo mismo (m / male -> M)."),
    cierre: bool = Form(True, description="Al final, rellenar los nulos válidos que sobren (cierre sin vacíos)."),
    numeros_cierre: str = Form("mediana", description="mediana | cero | palabra (números que sigan vacíos)."),
    fechas_cierre: bool = Form(True, description="Incluir las fechas en el cierre sin vacíos."),
    proyecto: str = Form("", description="Proyecto, para el documento de alcance."),
    autor: str = Form("", description="Autor(a), para el documento de alcance."),
    reglas: str = Form("", description='JSON {"columna": "regla[:parametro]"}. Ej: '
                       '{"email": "valor_fijo:Sin correo", "monto": "mediana_por_grupo:zona"}. '
                       "Sin esto se usa la regla sugerida de cada columna."),
):
    """Limpieza guiada de nulos: estandariza y aplica una regla de nulos por columna.
    Devuelve la auditoría y un id para bajar la tabla, el script y el diccionario con
    GET /descargar-guiado/{id}/{tipo}."""
    if formato not in ("csv", "xlsx"):
        raise HTTPException(status_code=400, detail="formato debe ser 'csv' o 'xlsx'.")
    df = _leer_upload_guiado(archivo, hoja)
    nombre = archivo.filename or "tabla.csv"
    config = _o_400(
        FG.configurar_limpieza_guiada, df, tokens_extra=_lista_o_none(tokens_extra),
        nombres_snake=nombres_snake, vacios_a_nan=vacios_a_nan, estandarizar_texto=estandarizar_texto,
        columnas_texto=_lista_o_none(columnas_texto), minusculas=minusculas, espacios=espacios,
        comillas=comillas, numericas=_lista_o_none(numericas), latitud=latitud, longitud=longitud,
        rango_latitud=_rango(rango_latitud, "rango_latitud"), rango_longitud=_rango(rango_longitud, "rango_longitud"),
        fechas=_lista_o_none(fechas), formato_fecha=formato_fecha, dia_primero=dia_primero)
    ajustes = _o_400(FG.parsear_ajustes_reglas,
                     [f"{k}={v}" for k, v in _json_dict(reglas, "reglas").items()])
    resultado = _o_400(FG.ejecutar_limpieza_guiada, df, config, ajustes, nombre_archivo=nombre, hoja=hoja,
                       palabra=palabra, unir_equivalentes=unir_equivalentes, asegurar_sin_vacios=cierre,
                       numeros_cierre=numeros_cierre, incluir_fechas_cierre=fechas_cierre)

    base = f"{FG.nombre_base(nombre)}_limpio"
    resultado_id = _guardar_guiado("limpieza", base, resultado.df, resultado.script,
                                   FG.argumentos_diccionario_limpieza(
                                       resultado, {"proyecto": proyecto, "autor": autor}))
    _RESULTADOS_GUIADOS[resultado_id]["formato"] = formato
    return _a_json({
        "id": resultado_id,
        "auditoria": FG.auditoria_a_dict(resultado.auditoria),
        "pasos": FG.resumen_pasos(resultado.pasos),
        "avisos": resultado.avisos,
        "reglas_aplicadas": resultado.reglas,
    })


@app.post("/merge/diagnostico")
def merge_diagnostico_endpoint(
    archivo_a: UploadFile = File(..., description="Tabla A (la que manda)."),
    archivo_b: UploadFile = File(..., description="Tabla B (la que enriquece)."),
    hoja_a: Optional[str] = Form(None),
    hoja_b: Optional[str] = Form(None),
    llave_a: Optional[str] = Form(None, description="Columnas llave de A, coma-separadas. Sin enviar = sugerida."),
    llave_b: Optional[str] = Form(None, description="Columnas llave de B, en el mismo orden."),
    modo_llave: str = Form("texto", description="texto | codigo | sin_cambios"),
    ancho: int = Form(0),
):
    """Sugiere las columnas llave y revisa cuántas filas de A encuentran pareja en B."""
    df_a, df_b = _leer_upload_guiado(archivo_a, hoja_a), _leer_upload_guiado(archivo_b, hoja_b)
    claves_a, claves_b, diagnostico = _o_400(
        FG.diagnosticar_llaves, df_a, df_b, _lista_o_none(llave_a), _lista_o_none(llave_b), modo_llave, ancho)
    return _a_json({
        "llaves_sugeridas": MT.sugerir_llaves(df_a, df_b),
        "llaves_usadas": {"a": claves_a, "b": claves_b},
        "diagnostico": FG.diagnostico_a_dict(diagnostico),
    })


@app.post("/merge")
def merge_endpoint(
    archivo_a: UploadFile = File(..., description="Tabla A (la que manda)."),
    archivo_b: UploadFile = File(..., description="Tabla B (la que enriquece)."),
    hoja_a: Optional[str] = Form(None),
    hoja_b: Optional[str] = Form(None),
    llave_a: Optional[str] = Form(None, description="Coma-separadas. Sin enviar = sugerida."),
    llave_b: Optional[str] = Form(None, description="Coma-separadas, en el mismo orden que A."),
    union: str = Form("left", description="left | inner | right | outer"),
    validar: str = Form("auto", description="auto | vacio | many_to_one | one_to_one | one_to_many | many_to_many"),
    modo_llave: str = Form("texto", description="texto | codigo | sin_cambios"),
    ancho: int = Form(0),
    prefijo_b: str = Form(""),
    prefijo_todas: bool = Form(False, description="Aplicar el prefijo a todas las columnas de B."),
    sufijo_b: str = Form("_b"),
    colapsar_b: bool = Form(True, description="Dejar una fila por llave en B si B repite llaves."),
    agregaciones: str = Form("", description='JSON {"columna": "first|sum|mean|median|max|min|count|nunique"}.'),
    rellenos: str = Form("", description='JSON {"destino": "respaldo"} (Plan B).'),
    conservar_indicador: bool = Form(False),
    formato: str = Form("csv", description="csv | xlsx (formato de la tabla al descargar)."),
    proyecto: str = Form("", description="Proyecto, para el documento de alcance."),
    autor: str = Form("", description="Autor(a), para el documento de alcance."),
):
    """Une A con B, revisa las llaves y audita el resultado. Devuelve la auditoría y un
    id para bajar la tabla, el script y el diccionario con GET /descargar-guiado/{id}/{tipo}."""
    if formato not in ("csv", "xlsx"):
        raise HTTPException(status_code=400, detail="formato debe ser 'csv' o 'xlsx'.")
    df_a, df_b = _leer_upload_guiado(archivo_a, hoja_a), _leer_upload_guiado(archivo_b, hoja_b)
    resultado = _o_400(
        FG.ejecutar_merge, df_a, df_b, archivo_a.filename or "tabla_a.csv", archivo_b.filename or "tabla_b.csv",
        _lista_o_none(llave_a), _lista_o_none(llave_b), how=union,
        validate="" if validar == "vacio" else validar, modo=modo_llave, ancho=ancho,
        prefijo_b=prefijo_b, solo_repetidas=not prefijo_todas, colapsar_b=colapsar_b,
        agregaciones=_json_dict(agregaciones, "agregaciones") or None, sufijo_b=sufijo_b,
        conservar_indicador=conservar_indicador, rellenos=list(_json_dict(rellenos, "rellenos").items()),
        hoja_a=hoja_a, hoja_b=hoja_b)
    resultado_id = _guardar_guiado("merge", "resultado_merge", resultado.df, resultado.script,
                                   FG.argumentos_diccionario_merge(resultado, {"proyecto": proyecto, "autor": autor}))
    _RESULTADOS_GUIADOS[resultado_id]["formato"] = formato
    return _a_json({
        "id": resultado_id, "filas": len(resultado.df), "columnas": list(resultado.df.columns),
        "auditoria": resultado.auditoria, "diagnostico": FG.diagnostico_a_dict(resultado.diagnostico),
        "rellenos_aplicados": resultado.rellenos,
    })


MEDIA_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TIPOS_DICCIONARIO = ("basico", "tecnico", "alcance", "todos")


def _respuesta_diccionario(documentos: dict, base: str, tipo: str) -> StreamingResponse:
    """Devuelve el diccionario pedido: basico (Excel), tecnico (Excel), alcance (Word) o todos (zip)."""
    if tipo == "todos":
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"diccionario_{base}.xlsx", documentos["basico"])
            zf.writestr(DD.NOMBRE_TECNICO, documentos["tecnico"])
            if documentos["alcance"] is not None:
                zf.writestr(f"documento_alcance_{base}.docx", documentos["alcance"])
        return StreamingResponse(io.BytesIO(buffer.getvalue()), media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="diccionarios_{base}.zip"'})
    if tipo == "alcance" and documentos["alcance"] is None:
        raise HTTPException(status_code=501, detail="Falta python-docx para generar el documento de alcance "
                                                    "(pip install python-docx).")
    contenido, media, nombre = {
        "basico": (documentos["basico"], MEDIA_XLSX, f"diccionario_{base}.xlsx"),
        "tecnico": (documentos["tecnico"], MEDIA_XLSX, DD.NOMBRE_TECNICO),
        "alcance": (documentos["alcance"], MEDIA_DOCX, f"documento_alcance_{base}.docx"),
    }[tipo]
    return StreamingResponse(io.BytesIO(contenido), media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


@app.post("/diccionario")
def diccionario_endpoint(
    archivo: UploadFile = File(...),
    hoja: Optional[str] = Form(None),
    nombre: Optional[str] = Form(None, description="Nombre de la tabla maestra (por defecto, el del archivo)."),
    tipo: str = Form("basico", description="basico (Excel de siempre) | tecnico (Excel, una fila por campo) | "
                                           "alcance (Word) | todos (zip con los tres)."),
    proyecto: str = Form("", description="Proyecto, para el documento de alcance."),
    autor: str = Form("", description="Autor(a), para el documento de alcance."),
):
    """Diccionario de datos: tipo, completitud y rango salen de los datos; las descripciones se redactan
    solas y la justificación de negocio queda en blanco para completarla."""
    if tipo not in TIPOS_DICCIONARIO:
        raise HTTPException(status_code=400, detail=f"tipo debe ser uno de: {', '.join(TIPOS_DICCIONARIO)}.")
    df = _leer_upload_guiado(archivo, hoja)
    tabla = nombre or FG.nombre_base(archivo.filename or "tabla")
    documentos = _o_400(FG.generar_documentos_diccionario, df, tabla, fuentes=[archivo.filename or tabla],
                        textos={"proyecto": proyecto, "autor": autor})
    return _respuesta_diccionario(documentos, FG.nombre_base(tabla), tipo)


@app.get("/descargar-guiado/{resultado_id}/{tipo}")
def descargar_guiado_endpoint(resultado_id: str, tipo: str, formato: Optional[str] = None):
    """Baja lo generado por /limpieza-guiada o /merge. tipo: datos | script | diccionario (Excel de siempre) |
    diccionario_tecnico (Excel) | documento_alcance (Word) | diccionarios (zip con los tres).
    Para 'datos', formato = csv | xlsx (por defecto el elegido al crear el resultado)."""
    if resultado_id not in _RESULTADOS_GUIADOS:
        raise HTTPException(status_code=404, detail="No existe ese resultado (o ya expiró).")
    if tipo not in ("datos", "script", "diccionario", "diccionario_tecnico", "documento_alcance", "diccionarios"):
        raise HTTPException(status_code=400, detail="tipo debe ser 'datos', 'script', 'diccionario', "
                                                    "'diccionario_tecnico', 'documento_alcance' o 'diccionarios'.")
    datos = _RESULTADOS_GUIADOS[resultado_id]
    base = datos["base"]
    if tipo == "script":
        return StreamingResponse(io.BytesIO(datos["script"].encode("utf-8")), media_type="text/x-python",
                                 headers={"Content-Disposition": f'attachment; filename="{base}_script.py"'})
    if tipo in ("diccionario", "diccionario_tecnico", "documento_alcance", "diccionarios"):
        pedido = {"diccionario": "basico", "diccionario_tecnico": "tecnico", "documento_alcance": "alcance",
                  "diccionarios": "todos"}[tipo]
        return _respuesta_diccionario(_documentos_diccionario(datos), base, pedido)
    formato = formato or datos.get("formato", "csv")
    if formato not in ("csv", "xlsx"):
        raise HTTPException(status_code=400, detail="formato debe ser 'csv' o 'xlsx'.")
    media = "text/csv" if formato == "csv" else MEDIA_XLSX
    return StreamingResponse(io.BytesIO(tabla_a_bytes(datos["df"], formato)), media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{base}.{formato}"'})

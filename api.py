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
from data_cleaner.loaders import load_excel, load_table, load_excel_hojas
from data_cleaner.exporters import exportar_sql
from data_cleaner.modelo_sql import aplicar_modelo_sql, generar_dot_modelo, generar_script_crear_base_datos

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
        "fecha_invalida": fecha_invalida, "email_invalido": email_invalido,
        "telefono_invalido": telefono_invalido, "id_duplicado": id_duplicado,
        "formula_incorrecta": formula_incorrecta, "texto_inconsistente": texto_inconsistente,
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
    paises_telefono: str = Form(""),
    digitos_telefono_min: Optional[int] = Form(None),
    digitos_telefono_max: Optional[int] = Form(None),
    permitir_codigo_pais_telefono: bool = Form(True),
    valores_fijos: str = Form("{}"),
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

    config = {
        "faltante": faltante, "duplicado": duplicado,
        "atipico": atipico, "tipo_invalido": tipo_invalido,
        "fecha_invalida": fecha_invalida, "email_invalido": email_invalido,
        "telefono_invalido": telefono_invalido, "id_duplicado": id_duplicado,
        "formula_incorrecta": formula_incorrecta, "texto_inconsistente": texto_inconsistente,
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

    df_limpio, registro = limpiar(df, resultado.issues, config=config, valores_fijos=valores_fijos_dict)
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
    factor_iqr: float = Form(1.5),
    valores_fijos: str = Form("{}", description='JSON con valores fijos por columna'),
    correcciones_individuales: str = Form(
        "[]", description='JSON: lista de objetos [{"tipo":..,"columna":..,"fila":..,"valor":..}] (opcional).'
    ),
    nombre_paso_anterior: str = Form("TuPasoAnterior", description="Solo aplica a formato=m"),
):
    """Genera un script Python autocontenido (Power BI o universal) o el código M,
    con la misma configuración de limpieza indicada (las 12 categorías), para
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

    config = {
        "faltante": faltante, "duplicado": duplicado,
        "atipico": atipico, "tipo_invalido": tipo_invalido,
        "fecha_invalida": fecha_invalida, "email_invalido": email_invalido,
        "telefono_invalido": telefono_invalido, "id_duplicado": id_duplicado,
        "formula_incorrecta": formula_incorrecta, "texto_inconsistente": texto_inconsistente,
        "estado_invalido": estado_invalido, "capitalizacion_incorrecta": capitalizacion_incorrecta,
    }

    if formato == "powerbi":
        contenido = generar_script_powerbi(
            config, factor_iqr, valores_fijos_dict, correcciones_individuales=correcciones_individuales_dict,
        )
        nombre_archivo, media_type = "limpiador_powerbi_generado.py", "text/x-python"
    elif formato == "universal":
        contenido = generar_script_universal(
            config, factor_iqr, valores_fijos_dict, correcciones_individuales=correcciones_individuales_dict,
        )
        nombre_archivo, media_type = "limpiador_universal_generado.py", "text/x-python"
    else:
        contenido = generar_editor_m(
            config, factor_iqr, valores_fijos_dict, nombre_paso_anterior,
            correcciones_individuales=correcciones_individuales_dict,
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
    factor_iqr: float = Form(1.5),
    valores_fijos: str = Form("{}", description='JSON con valores fijos por columna'),
    correcciones_individuales: str = Form(
        "[]", description='JSON: lista de objetos [{"tipo":..,"columna":..,"fila":..,"valor":..}] (opcional).'
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
        correcciones_individuales=correcciones_individuales_dict,
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

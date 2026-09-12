#!/usr/bin/env python3
"""
Limpiador de Tablas — Interfaz de escritorio (Tkinter)
=========================================================
Ventana nativa (sin navegador ni servidor). Permite cargar un CSV/Excel,
analizarlo, elegir la acción por tipo de hallazgo y guardar el resultado.

Uso:
    python desktop_app.py
"""
from __future__ import annotations

import os
import re
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import pandas as pd

from data_cleaner import (
    load_table, analizar, limpiar, DEFAULT_CONFIG,
    construir_reporte, exportar_reporte_excel, exportar,
)
from data_cleaner.loaders import load_excel, load_excel_hojas
from data_cleaner.exportador import (
    generar_script_powerbi, generar_script_universal, generar_editor_m,
)
from data_cleaner.exportador_m import generar_editor_m_puro
from data_cleaner.patrones import PAISES_TELEFONO_DISPONIBLES
from data_cleaner.modelo_sql import (
    aplicar_modelo_sql, es_tabla_hecho, generar_script_crear_base_datos,
)

OPCIONES_ACCION = {
    "faltante": ["reemplazar_mediana", "reemplazar_media", "reemplazar_moda",
                 "valor_fijo", "editar_individualmente", "eliminar_fila", "marcar_solo"],
    "duplicado": ["eliminar_fila", "marcar_solo"],
    "atipico": ["limitar", "reemplazar_mediana", "reemplazar_media",
                "editar_individualmente", "eliminar_fila", "marcar_solo"],
    "tipo_invalido": ["eliminar_fila", "valor_fijo", "editar_individualmente", "marcar_solo"],
    "fecha_invalida": ["eliminar_fila", "valor_fijo", "editar_individualmente", "marcar_solo"],
    "email_invalido": ["eliminar_fila", "valor_fijo", "editar_individualmente", "marcar_solo"],
    "telefono_invalido": ["editar_individualmente", "eliminar_fila", "valor_fijo", "marcar_solo"],
    "id_duplicado": ["eliminar_fila", "valor_fijo", "editar_individualmente", "marcar_solo"],
    "formula_incorrecta": ["usar_sugerido", "eliminar_fila", "valor_fijo", "editar_individualmente", "marcar_solo"],
    "texto_inconsistente": ["usar_sugerido", "eliminar_fila", "valor_fijo", "editar_individualmente", "marcar_solo"],
    "estado_invalido": ["eliminar_fila", "valor_fijo", "editar_individualmente", "marcar_solo"],
}
# "duplicado" (fila completa) queda fuera de "editar_individualmente": un
# hallazgo de fila duplicada no tiene una sola columna/valor que editar (ver
# Issue en analyzer.py, columna=None y valor_original=la fila completa).
# Mantener sincronizado con OPCIONES_ACCION de app.py.

NOMBRES_TIPO = {
    "faltante": "Valores faltantes",
    "duplicado": "Filas duplicadas",
    "atipico": "Valores atípicos",
    "tipo_invalido": "Errores de tipo",
    "fecha_invalida": "Fechas inválidas/fuera de rango",
    "email_invalido": "Correos inválidos",
    "telefono_invalido": "Teléfonos inválidos",
    "id_duplicado": "IDs duplicados",
    "formula_incorrecta": "Total ≠ Cantidad × Precio",
    "texto_inconsistente": "Variantes de texto",
    "estado_invalido": "Estados no reconocidos",
}

# Nota: PAISES_TELEFONO_DISPONIBLES ahora vive en data_cleaner/patrones.py
# (importado arriba), compartido con app.py, para que ambas interfaces
# siempre muestren exactamente el mismo listado de países.


def _nombre_tabla_valido_modelo(nombre_hoja: str) -> str:
    """Convierte el nombre de una hoja en un nombre de tabla SQL válido:
    minúsculas, espacios/caracteres raros -> guion bajo. Compartido con la
    misma lógica de app.py y excel_a_sql.py."""
    limpio = re.sub(r"[^a-zA-Z0-9_]+", "_", nombre_hoja.strip().lower())
    limpio = re.sub(r"_+", "_", limpio).strip("_")
    return limpio or "hoja_sin_nombre"


def _dibujar_diagrama_modelo(canvas: tk.Canvas, modelo: dict) -> None:
    """Dibuja el modelo (dimensiones arriba, hechos abajo, flechas FK->PK)
    en un Canvas de Tkinter. Ver data_cleaner/modelo_sql.py para el mismo
    diagrama en formato Graphviz DOT (usado en app.py)."""
    canvas.delete("all")
    if not modelo:
        canvas.create_text(150, 80, text="(sin tablas definidas todavía)", fill="#888")
        return

    dimensiones = [t for t, d in modelo.items() if not es_tabla_hecho(d)]
    hechos = [t for t, d in modelo.items() if es_tabla_hecho(d)]

    ancho_caja, alto_caja, espacio_x = 150, 46, 40
    ancho_disponible = max(canvas.winfo_width(), 700)
    posiciones: dict[str, tuple[float, float, float, float]] = {}

    def _colocar_fila(tablas, y):
        if not tablas:
            return
        ancho_total = len(tablas) * ancho_caja + (len(tablas) - 1) * espacio_x
        x0 = max(20, (ancho_disponible - ancho_total) // 2)
        for i, tabla in enumerate(tablas):
            x = x0 + i * (ancho_caja + espacio_x)
            posiciones[tabla] = (x, y, x + ancho_caja, y + alto_caja)

    _colocar_fila(dimensiones, 25)
    _colocar_fila(hechos, 25 + alto_caja + 85)

    # Flechas FK -> PK primero, para que queden debajo de las cajas
    for tabla, definicion in modelo.items():
        for fk in definicion.get("claves_foraneas", []):
            origen = posiciones.get(tabla)
            destino = posiciones.get(fk["tabla_referencia"])
            if not origen or not destino:
                continue
            x1, y1 = (origen[0] + origen[2]) / 2, origen[1]
            x2, y2 = (destino[0] + destino[2]) / 2, destino[3]
            canvas.create_line(x1, y1, x2, y2, arrow=tk.LAST, fill="#555")
            canvas.create_text(
                (x1 + x2) / 2, (y1 + y2) / 2 - 8,
                text=f'{fk["columna"]} \u2192 {fk["columna_referencia"]}',
                fill="#333", font=("Helvetica", 8),
            )

    # Cajas de las tablas
    for tabla, definicion in modelo.items():
        x1, y1, x2, y2 = posiciones[tabla]
        color = "#F4A261" if es_tabla_hecho(definicion) else "#A8DADC"
        canvas.create_rectangle(x1, y1, x2, y2, fill=color, outline="#333")
        pk = definicion.get("clave_primaria")
        texto = tabla + (f"\nPK: {pk}" if pk else "")
        canvas.create_text((x1 + x2) / 2, (y1 + y2) / 2, text=texto, font=("Helvetica", 9, "bold"))


class LimpiadorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Limpiador de Tablas")
        self.geometry("1000x700")
        self.minsize(820, 560)

        self.df: pd.DataFrame | None = None
        self.ruta_actual: str | None = None
        self.resultado = None
        self.df_limpio: pd.DataFrame | None = None
        self.registro = None
        self.tablas_reporte = None
        self.config_aplicada: dict[str, str] = {}
        self.valores_fijos_aplicados: dict[str, object] = {}

        self.accion_vars: dict[str, tk.StringVar] = {}
        self.valor_fijo_vars: dict[str, tk.StringVar] = {}
        # (tipo, columna, fila) -> valor corregido, para la accion
        # "editar_individualmente" (ver _abrir_editor_individual).
        self.correcciones_individuales: dict[tuple, object] = {}

        # Configuracion de telefono para analizar_tabla (ver _configurar_telefono).
        self.paises_telefono: list[str] | None = ["cr"]
        self.digitos_telefono_manual: tuple[int, int] | None = None
        self.permitir_codigo_pais_telefono: bool = True

        self._construir_layout()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _construir_layout(self) -> None:
        barra = ttk.Frame(self, padding=10)
        barra.pack(fill="x")

        ttk.Button(barra, text="📂 Abrir archivo (CSV/Excel)", command=self.abrir_archivo).pack(side="left")
        ttk.Button(barra, text="🔌 Conectar a SQL...", command=self.conectar_sql).pack(side="left", padx=(6, 0))
        ttk.Button(barra, text="🗂️ Modelo de datos...", command=self.abrir_modelo_datos).pack(side="left", padx=(6, 0))
        self.lbl_archivo = ttk.Label(barra, text="Ningún archivo cargado.")
        self.lbl_archivo.pack(side="left", padx=10)

        ttk.Label(barra, text="Método atípicos:").pack(side="left", padx=(20, 4))
        self.metodo_var = tk.StringVar(value="iqr")
        ttk.Combobox(barra, textvariable=self.metodo_var, values=["iqr", "zscore", "ambos"],
                     width=8, state="readonly").pack(side="left")

        ttk.Button(barra, text="📞 Teléfono...", command=self.configurar_telefono).pack(side="left", padx=(10, 0))
        ttk.Button(barra, text="🔍 Analizar", command=self.analizar_tabla).pack(side="left", padx=10)

        # --- Panel central dividido: vista previa arriba, config abajo ---
        panel = ttk.PanedWindow(self, orient="vertical")
        panel.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        marco_preview = ttk.LabelFrame(panel, text="Vista previa / resultado", padding=5)
        panel.add(marco_preview, weight=3)

        self.notebook = ttk.Notebook(marco_preview)
        self.notebook.pack(fill="both", expand=True)

        self.tabla_datos = self._crear_tabla(self.notebook)
        self.notebook.add(self.tabla_datos.master, text="Datos")

        self.tabla_hallazgos = self._crear_tabla(self.notebook)
        self.notebook.add(self.tabla_hallazgos.master, text="Hallazgos")

        marco_config = ttk.LabelFrame(panel, text="Configurar corrección", padding=10)
        panel.add(marco_config, weight=2)

        self.marco_tipos = ttk.Frame(marco_config)
        self.marco_tipos.pack(fill="both", expand=True)

        botones = ttk.Frame(marco_config)
        botones.pack(fill="x", pady=(10, 0))
        ttk.Button(botones, text="🧽 Limpiar tabla y generar reporte",
                   command=self.limpiar_tabla).pack(side="left")
        ttk.Button(botones, text="💾 Guardar resultados...",
                   command=self.guardar_resultados).pack(side="left", padx=10)
        ttk.Button(botones, text="🗄️ Exportar a SQL...",
                   command=self.exportar_sql).pack(side="left", padx=(0, 10))
        ttk.Button(botones, text="📤 Exportar script portátil...",
                   command=self.exportar_script_portatil).pack(side="left")

        self.status_var = tk.StringVar(value="Listo.")
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w").pack(fill="x")

    @staticmethod
    def _crear_tabla(parent) -> ttk.Treeview:
        marco = ttk.Frame(parent)
        tree = ttk.Treeview(marco, show="headings")
        vsb = ttk.Scrollbar(marco, orient="vertical", command=tree.yview)
        hsb = ttk.Scrollbar(marco, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        marco.rowconfigure(0, weight=1)
        marco.columnconfigure(0, weight=1)
        return tree

    def _llenar_tabla(self, tree: ttk.Treeview, df: pd.DataFrame, max_filas: int = 300) -> None:
        tree.delete(*tree.get_children())
        tree["columns"] = list(df.columns)
        for col in df.columns:
            tree.heading(col, text=str(col))
            tree.column(col, width=110, anchor="w")
        for _, fila in df.head(max_filas).iterrows():
            tree.insert("", "end", values=[fila[c] for c in df.columns])

    # ------------------------------------------------------------------
    # Acciones
    # ------------------------------------------------------------------
    def _elegir_hoja(self, hojas: list[str]) -> str | None:
        """
        Diálogo modal para elegir qué hoja limpiar cuando el Excel tiene
        varias. Cada hoja puede ser una tabla con esquema distinto (ej. un
        libro Power BI con varias tablas); concatenarlas todas por defecto
        genera valores faltantes falsos en las columnas que no existen en
        cada hoja. Se pide elegir una sola hoja; "Todas las hojas" queda
        disponible solo si de verdad es la misma tabla repartida.
        """
        resultado: dict[str, str | None] = {"hoja": None}
        ventana = tk.Toplevel(self)
        ventana.title("Elegir hoja")
        ventana.resizable(False, False)
        ttk.Label(
            ventana,
            text="Este Excel tiene varias hojas. ¿Cuál desea limpiar?",
            wraplength=320, justify="left",
        ).pack(padx=15, pady=(15, 5))
        opciones = list(hojas) + ["Todas las hojas (concatenadas)"]
        var_hoja = tk.StringVar(value=opciones[0])
        combo = ttk.Combobox(ventana, textvariable=var_hoja, values=opciones,
                              state="readonly", width=40)
        combo.pack(padx=15, pady=5)

        def _confirmar() -> None:
            resultado["hoja"] = var_hoja.get()
            ventana.destroy()

        ttk.Button(ventana, text="Aceptar", command=_confirmar).pack(pady=(5, 15))
        ventana.protocol("WM_DELETE_WINDOW", ventana.destroy)
        ventana.transient(self)
        ventana.grab_set()
        self.wait_window(ventana)
        return resultado["hoja"]

    def _dataframe_cargado(self, df: pd.DataFrame, etiqueta: str) -> None:
        """Pasos comunes tras cargar datos, sin importar el origen (archivo o SQL)."""
        self.df = df
        self.resultado = None
        self.df_limpio = None
        self.correcciones_individuales.clear()
        self.lbl_archivo.config(text=f"{etiqueta}  ({len(df)} filas × {len(df.columns)} cols)")
        self._llenar_tabla(self.tabla_datos, df)
        for widget in self.marco_tipos.winfo_children():
            widget.destroy()
        self.status_var.set("Datos cargados. Presione Analizar.")

    def abrir_archivo(self) -> None:
        ruta = filedialog.askopenfilename(
            title="Seleccionar archivo",
            filetypes=[("CSV / Excel", "*.csv *.xlsx *.xls"), ("Todos", "*.*")],
        )
        if not ruta:
            return
        try:
            if ruta.lower().endswith((".xlsx", ".xls", ".xlsm")):
                hojas = pd.ExcelFile(ruta).sheet_names
                if len(hojas) == 1:
                    df = load_excel(ruta, sheet_name=hojas[0])
                else:
                    hoja_elegida = self._elegir_hoja(hojas)
                    if not hoja_elegida:
                        return
                    if hoja_elegida == "Todas las hojas (concatenadas)":
                        df = load_excel(ruta)
                    else:
                        df = load_excel(ruta, sheet_name=hoja_elegida)
            else:
                df = load_table(ruta, kind="csv")
        except Exception as exc:
            messagebox.showerror("Error al cargar", str(exc))
            return

        self.ruta_actual = ruta
        self._dataframe_cargado(df, os.path.basename(ruta))

    def conectar_sql(self) -> None:
        """
        Conecta a una base de datos existente via SQLAlchemy (load_table en
        data_cleaner/loaders.py) y trae una tabla o el resultado de una
        consulta al flujo normal de analisis/limpieza. Segun el motor
        elegido hace falta el driver correspondiente instalado (ver
        requirements.txt: psycopg2-binary/pymysql/pyodbc).
        """
        ventana = tk.Toplevel(self)
        ventana.title("Conectar a una base de datos SQL")
        ventana.geometry("440x420")
        ventana.transient(self)
        ventana.grab_set()

        motores = ["PostgreSQL", "MySQL", "SQL Server", "SQLite", "Otra (cadena de conexión manual)"]
        motor_var = tk.StringVar(value=motores[0])
        ttk.Label(ventana, text="Motor de base de datos:").pack(anchor="w", padx=15, pady=(15, 2))
        ttk.Combobox(ventana, textvariable=motor_var, values=motores, state="readonly").pack(fill="x", padx=15)

        marco_campos = ttk.Frame(ventana)
        marco_campos.pack(fill="x", padx=15, pady=10)

        campos_vars = {
            "host": tk.StringVar(value="localhost"), "puerto": tk.StringVar(),
            "usuario": tk.StringVar(), "clave": tk.StringVar(), "basedatos": tk.StringVar(),
            "ruta_sqlite": tk.StringVar(), "cadena_manual": tk.StringVar(),
        }
        _puertos_defecto = {"PostgreSQL": "5432", "MySQL": "3306", "SQL Server": "1433"}

        def _redibujar_campos(*_args):
            for w in marco_campos.winfo_children():
                w.destroy()
            motor = motor_var.get()
            if motor == "SQLite":
                ttk.Label(marco_campos, text="Ruta del archivo .db:").pack(anchor="w")
                ttk.Entry(marco_campos, textvariable=campos_vars["ruta_sqlite"]).pack(fill="x")
            elif motor == "Otra (cadena de conexión manual)":
                ttk.Label(marco_campos, text="Cadena de conexión SQLAlchemy completa:").pack(anchor="w")
                ttk.Entry(marco_campos, textvariable=campos_vars["cadena_manual"], show="•").pack(fill="x")
            else:
                campos_vars["puerto"].set(_puertos_defecto.get(motor, ""))
                for etiqueta, clave, oculto in [
                    ("Host", "host", False), ("Puerto", "puerto", False),
                    ("Usuario", "usuario", False), ("Contraseña", "clave", True),
                    ("Base de datos", "basedatos", False),
                ]:
                    ttk.Label(marco_campos, text=f"{etiqueta}:").pack(anchor="w")
                    ttk.Entry(marco_campos, textvariable=campos_vars[clave],
                              show="•" if oculto else "").pack(fill="x", pady=(0, 4))

        motor_var.trace_add("write", _redibujar_campos)
        _redibujar_campos()

        ttk.Separator(ventana, orient="horizontal").pack(fill="x", padx=15, pady=6)

        modo_var = tk.StringVar(value="tabla")
        ttk.Radiobutton(ventana, text="Nombre de tabla", variable=modo_var, value="tabla").pack(anchor="w", padx=15)
        ttk.Radiobutton(ventana, text="Consulta SQL personalizada", variable=modo_var, value="query").pack(anchor="w", padx=15)
        tabla_o_query_var = tk.StringVar()
        ttk.Entry(ventana, textvariable=tabla_o_query_var).pack(fill="x", padx=15, pady=(4, 15))

        def _conectar():
            motor = motor_var.get()
            if motor == "SQLite":
                if not campos_vars["ruta_sqlite"].get():
                    messagebox.showwarning("Falta la ruta", "Indique la ruta del archivo .db.")
                    return
                cadena = f"sqlite:///{campos_vars['ruta_sqlite'].get()}"
            elif motor == "Otra (cadena de conexión manual)":
                cadena = campos_vars["cadena_manual"].get()
                if not cadena:
                    messagebox.showwarning("Falta la cadena", "Ingrese la cadena de conexión.")
                    return
            else:
                driver = {"PostgreSQL": "postgresql+psycopg2", "MySQL": "mysql+pymysql",
                          "SQL Server": "mssql+pyodbc"}[motor]
                if not (campos_vars["host"].get() and campos_vars["basedatos"].get()):
                    messagebox.showwarning("Faltan datos", "Complete al menos host y base de datos.")
                    return
                if motor == "SQL Server":
                    # OJO: ODBC Driver 18 (no 17) + Encrypt/TrustServerCertificate, porque
                    # las instalaciones recientes de SQL Server exigen cifrado por
                    # defecto y muchas maquinas ya no traen el driver 17 instalado.
                    parametros_odbc = "driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=yes"
                    servidor = f"{campos_vars['host'].get()}:{campos_vars['puerto'].get()}" \
                        if campos_vars["puerto"].get() else campos_vars["host"].get()
                    if not campos_vars["usuario"].get() and not campos_vars["clave"].get():
                        # Sin usuario/clave: autenticacion de Windows (Trusted_Connection)
                        cadena = f"{driver}://@{servidor}/{campos_vars['basedatos'].get()}?{parametros_odbc}&trusted_connection=yes"
                    else:
                        cadena = (f"{driver}://{campos_vars['usuario'].get()}:{campos_vars['clave'].get()}"
                                  f"@{servidor}/{campos_vars['basedatos'].get()}?{parametros_odbc}")
                else:
                    if not campos_vars["usuario"].get():
                        messagebox.showwarning("Faltan datos", "Complete usuario para este motor.")
                        return
                    cadena = (f"{driver}://{campos_vars['usuario'].get()}:{campos_vars['clave'].get()}"
                              f"@{campos_vars['host'].get()}:{campos_vars['puerto'].get()}/{campos_vars['basedatos'].get()}")

            valor = tabla_o_query_var.get().strip()
            if not valor:
                messagebox.showwarning("Falta la tabla/consulta", "Indique una tabla o una consulta SQL.")
                return

            try:
                if modo_var.get() == "tabla":
                    df = load_table(cadena, kind="sql", table_name=valor)
                else:
                    df = load_table(cadena, kind="sql", query=valor)
            except Exception as exc:
                messagebox.showerror("Error de conexión", str(exc))
                return

            self.ruta_actual = f"sql::{motor}::{valor}"
            self._dataframe_cargado(df, f"SQL: {valor}")
            ventana.destroy()

        ttk.Button(ventana, text="Conectar y cargar", command=_conectar).pack(pady=(0, 10))

    def _pedir_cadena_conexion_sql(self, ventana_padre: tk.Toplevel) -> str | None:
        """
        Diálogo modal reutilizable para armar una cadena de conexión
        SQLAlchemy (mismo patrón de motores que conectar_sql/exportar_sql).
        Devuelve la cadena de conexión, o None si el usuario cancela.
        """
        resultado: dict[str, str | None] = {"cadena": None}
        ventana = tk.Toplevel(ventana_padre)
        ventana.title("Conexión a la base de datos")
        ventana.geometry("420x380")
        ventana.transient(ventana_padre)
        ventana.grab_set()

        motores = ["PostgreSQL", "MySQL", "SQL Server", "SQLite", "Otra (cadena de conexión manual)"]
        motor_var = tk.StringVar(value="SQL Server")
        ttk.Label(ventana, text="Motor de base de datos:").pack(anchor="w", padx=15, pady=(15, 2))
        ttk.Combobox(ventana, textvariable=motor_var, values=motores, state="readonly").pack(fill="x", padx=15)

        marco_campos = ttk.Frame(ventana)
        marco_campos.pack(fill="x", padx=15, pady=10)

        campos_vars = {
            "host": tk.StringVar(value="localhost"), "puerto": tk.StringVar(),
            "usuario": tk.StringVar(), "clave": tk.StringVar(), "basedatos": tk.StringVar(),
            "ruta_sqlite": tk.StringVar(), "cadena_manual": tk.StringVar(),
        }
        _puertos_defecto = {"PostgreSQL": "5432", "MySQL": "3306", "SQL Server": "1433"}

        def _redibujar_campos(*_args):
            for w in marco_campos.winfo_children():
                w.destroy()
            motor = motor_var.get()
            if motor == "SQLite":
                ttk.Label(marco_campos, text="Ruta del archivo .db:").pack(anchor="w")
                ttk.Entry(marco_campos, textvariable=campos_vars["ruta_sqlite"]).pack(fill="x")
            elif motor == "Otra (cadena de conexión manual)":
                ttk.Label(marco_campos, text="Cadena de conexión SQLAlchemy completa:").pack(anchor="w")
                ttk.Entry(marco_campos, textvariable=campos_vars["cadena_manual"], show="•").pack(fill="x")
            else:
                campos_vars["puerto"].set(_puertos_defecto.get(motor, ""))
                for etiqueta, clave, oculto in [
                    ("Host", "host", False), ("Puerto", "puerto", False),
                    ("Usuario (vacío = autenticación de Windows en SQL Server)", "usuario", False),
                    ("Contraseña", "clave", True), ("Base de datos", "basedatos", False),
                ]:
                    ttk.Label(marco_campos, text=f"{etiqueta}:").pack(anchor="w")
                    ttk.Entry(marco_campos, textvariable=campos_vars[clave],
                              show="•" if oculto else "").pack(fill="x", pady=(0, 4))

        motor_var.trace_add("write", _redibujar_campos)
        _redibujar_campos()

        def _aceptar():
            motor = motor_var.get()
            if motor == "SQLite":
                if not campos_vars["ruta_sqlite"].get():
                    messagebox.showwarning("Falta la ruta", "Indique la ruta del archivo .db.")
                    return
                cadena = f"sqlite:///{campos_vars['ruta_sqlite'].get()}"
            elif motor == "Otra (cadena de conexión manual)":
                cadena = campos_vars["cadena_manual"].get()
                if not cadena:
                    messagebox.showwarning("Falta la cadena", "Ingrese la cadena de conexión.")
                    return
            else:
                driver = {"PostgreSQL": "postgresql+psycopg2", "MySQL": "mysql+pymysql",
                          "SQL Server": "mssql+pyodbc"}[motor]
                if not (campos_vars["host"].get() and campos_vars["basedatos"].get()):
                    messagebox.showwarning("Faltan datos", "Complete al menos host y base de datos.")
                    return
                if motor == "SQL Server":
                    parametros_odbc = "driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=yes"
                    servidor = f"{campos_vars['host'].get()}:{campos_vars['puerto'].get()}" \
                        if campos_vars["puerto"].get() else campos_vars["host"].get()
                    if not campos_vars["usuario"].get() and not campos_vars["clave"].get():
                        cadena = f"{driver}://@{servidor}/{campos_vars['basedatos'].get()}?{parametros_odbc}&trusted_connection=yes"
                    else:
                        cadena = (f"{driver}://{campos_vars['usuario'].get()}:{campos_vars['clave'].get()}"
                                  f"@{servidor}/{campos_vars['basedatos'].get()}?{parametros_odbc}")
                else:
                    if not campos_vars["usuario"].get():
                        messagebox.showwarning("Faltan datos", "Complete usuario para este motor.")
                        return
                    cadena = (f"{driver}://{campos_vars['usuario'].get()}:{campos_vars['clave'].get()}"
                              f"@{campos_vars['host'].get()}:{campos_vars['puerto'].get()}/{campos_vars['basedatos'].get()}")

            resultado["cadena"] = cadena
            ventana.destroy()

        ttk.Button(ventana, text="Aceptar", command=_aceptar).pack(pady=(10, 15))
        ventana.protocol("WM_DELETE_WINDOW", ventana.destroy)
        ventana_padre.wait_window(ventana)
        return resultado["cadena"]

    def _abrir_generador_script_bd(self, ventana_padre: tk.Toplevel) -> None:
        """
        Ventanita para generar el script CREATE DATABASE / USE que hay que
        pegar en SSMS/mysql/psql cuando la base de datos destino todavía
        no existe (una cadena de conexión normal no puede apuntar a una
        base que no existe). Ver
        data_cleaner/modelo_sql.py:generar_script_crear_base_datos.
        """
        ventana = tk.Toplevel(ventana_padre)
        ventana.title("Generar script para crear la base de datos")
        ventana.geometry("560x420")
        ventana.transient(ventana_padre)

        ttk.Label(
            ventana,
            text="No se puede conectar a una base de datos que no existe: primero "
                 "hay que crearla desde una consulta en SSMS/mysql/psql, y recién "
                 "después conectar apuntando a esa base ya creada.",
            wraplength=520, justify="left",
        ).pack(anchor="w", padx=15, pady=(15, 10))

        fila = ttk.Frame(ventana)
        fila.pack(fill="x", padx=15)
        ttk.Label(fila, text="Nombre de la base de datos:").pack(side="left")
        var_nombre_bd = tk.StringVar()
        ttk.Entry(fila, textvariable=var_nombre_bd, width=24).pack(side="left", padx=(4, 16))
        ttk.Label(fila, text="Motor:").pack(side="left")
        var_motor_bd = tk.StringVar(value="sql_server")
        ttk.Combobox(
            fila, textvariable=var_motor_bd, state="readonly", width=14,
            values=["sql_server", "mysql", "postgresql"],
        ).pack(side="left", padx=(4, 0))

        texto_script = tk.Text(ventana, height=12, wrap="word")
        texto_script.pack(fill="both", expand=True, padx=15, pady=10)

        def _generar():
            try:
                script = generar_script_crear_base_datos(var_nombre_bd.get(), var_motor_bd.get())
            except ValueError as exc:
                messagebox.showwarning("Falta información", str(exc))
                return
            texto_script.delete("1.0", "end")
            texto_script.insert("1.0", script)

        def _copiar():
            contenido = texto_script.get("1.0", "end").strip()
            if not contenido:
                messagebox.showwarning("Nada que copiar", "Genere primero el script.")
                return
            ventana.clipboard_clear()
            ventana.clipboard_append(contenido)
            ventana.update()  # necesario en algunos sistemas para que quede disponible
            messagebox.showinfo(
                "Copiado", "Script copiado al portapapeles. Péguelo en su consulta de SSMS/mysql/psql.",
            )

        def _guardar():
            contenido = texto_script.get("1.0", "end").strip()
            if not contenido:
                messagebox.showwarning("Nada que guardar", "Genere primero el script.")
                return
            ruta = filedialog.asksaveasfilename(
                title="Guardar script SQL", defaultextension=".sql",
                filetypes=[("Script SQL", "*.sql"), ("Todos", "*.*")],
                initialfile=f"crear_{var_nombre_bd.get() or 'base_datos'}.sql",
            )
            if not ruta:
                return
            with open(ruta, "w", encoding="utf-8") as f:
                f.write(contenido + "\n")
            messagebox.showinfo("Guardado", f"Script guardado en:\n{ruta}")

        marco_botones = ttk.Frame(ventana)
        marco_botones.pack(fill="x", padx=15, pady=(0, 15))
        ttk.Button(marco_botones, text="Generar script", command=_generar).pack(side="left")
        ttk.Button(marco_botones, text="📋 Copiar", command=_copiar).pack(side="left", padx=(8, 0))
        ttk.Button(marco_botones, text="💾 Guardar como .sql...", command=_guardar).pack(side="left", padx=(8, 0))

        ventana.protocol("WM_DELETE_WINDOW", ventana.destroy)

    def abrir_modelo_datos(self) -> None:
        """
        Ventana para cargar varias hojas del mismo Excel y escribirlas en
        SQL como un modelo de datos en ESTRELLA o COPO DE NIEVE: define,
        por cada hoja, su llave primaria (PK) y sus llaves foráneas (FK)
        hacia otras hojas del modelo, muestra un diagrama y aplica todo en
        la base de datos (ver data_cleaner/modelo_sql.py).
        """
        ruta = filedialog.askopenfilename(
            title="Seleccionar Excel para el modelo",
            filetypes=[("Excel", "*.xlsx *.xls"), ("Todos", "*.*")],
        )
        if not ruta:
            return
        try:
            hojas_disponibles = pd.ExcelFile(ruta).sheet_names
        except Exception as exc:
            messagebox.showerror("Error al leer el archivo", str(exc))
            return

        ventana = tk.Toplevel(self)
        ventana.title("Modelo de datos (estrella / copo de nieve)")
        ventana.geometry("980x700")
        ventana.transient(self)

        ttk.Label(
            ventana, text="Elija las hojas a incluir en el modelo (una tabla por hoja):",
        ).pack(anchor="w", padx=12, pady=(10, 2))

        lista_hojas = tk.Listbox(
            ventana, selectmode="extended", height=min(8, len(hojas_disponibles) + 1),
            exportselection=False,
        )
        for h in hojas_disponibles:
            lista_hojas.insert("end", h)
        lista_hojas.selection_set(0, "end")
        lista_hojas.pack(fill="x", padx=12)

        contenedor_config = ttk.Frame(ventana)
        estado_modelo: dict[str, dict] = {}  # nombre_hoja -> config de esa tabla

        def _refrescar_scroll(_evt=None):
            canvas_cfg.configure(scrollregion=canvas_cfg.bbox("all"))

        def _cargar_hojas_elegidas():
            for w in contenedor_config.winfo_children():
                w.destroy()
            estado_modelo.clear()

            seleccion = [hojas_disponibles[i] for i in lista_hojas.curselection()]
            if not seleccion:
                messagebox.showwarning("Sin hojas", "Elija al menos una hoja.")
                return
            try:
                hojas_cargadas = load_excel_hojas(ruta, hojas=seleccion)
            except Exception as exc:
                messagebox.showerror("Error al cargar", str(exc))
                return

            for hoja in seleccion:
                df_hoja = hojas_cargadas[hoja]
                marco = ttk.LabelFrame(
                    contenedor_config,
                    text=f"{hoja}  ({len(df_hoja)} filas, {len(df_hoja.columns)} columnas)",
                )
                marco.pack(fill="x", padx=4, pady=6)

                fila1 = ttk.Frame(marco)
                fila1.pack(fill="x", padx=8, pady=4)
                ttk.Label(fila1, text="Tabla SQL:").pack(side="left")
                var_tabla = tk.StringVar(value=_nombre_tabla_valido_modelo(hoja))
                ttk.Entry(fila1, textvariable=var_tabla, width=22).pack(side="left", padx=(4, 16))

                ttk.Label(fila1, text="PK:").pack(side="left")
                var_pk = tk.StringVar(value="(ninguna)")
                ttk.Combobox(
                    fila1, textvariable=var_pk, values=["(ninguna)"] + list(df_hoja.columns),
                    state="readonly", width=16,
                ).pack(side="left", padx=(4, 0))

                ttk.Label(
                    marco,
                    text="Llaves foráneas hacia otra tabla del modelo (opcional; cualquier tabla puede "
                         "tener — una dimensión con FK hacia otra dimensión arma copo de nieve):",
                    wraplength=880, justify="left",
                ).pack(anchor="w", padx=8, pady=(2, 0))

                marco_fks = ttk.Frame(marco)
                marco_fks.pack(fill="x", padx=8, pady=(0, 4))

                info = {
                    "hoja": hoja, "df": df_hoja, "var_tabla": var_tabla,
                    "var_pk": var_pk, "marco_fks": marco_fks, "fks": [],
                }
                estado_modelo[hoja] = info

                def _agregar_fk(hoja=hoja, df_hoja=df_hoja, marco_fks=marco_fks, info=info):
                    fila_fk = ttk.Frame(marco_fks)
                    fila_fk.pack(fill="x", pady=2)
                    otras = [h for h in estado_modelo if h != hoja]

                    var_col_fk = tk.StringVar(value=df_hoja.columns[0] if len(df_hoja.columns) else "")
                    var_hoja_ref = tk.StringVar(value=otras[0] if otras else "")
                    var_col_ref = tk.StringVar()

                    ttk.Label(fila_fk, text="FK columna:").pack(side="left")
                    ttk.Combobox(
                        fila_fk, textvariable=var_col_fk, values=list(df_hoja.columns),
                        state="readonly", width=14,
                    ).pack(side="left", padx=(2, 10))
                    ttk.Label(fila_fk, text="→ tabla:").pack(side="left")
                    combo_hoja_ref = ttk.Combobox(
                        fila_fk, textvariable=var_hoja_ref, values=otras, state="readonly", width=14,
                    )
                    combo_hoja_ref.pack(side="left", padx=(2, 10))
                    ttk.Label(fila_fk, text="columna:").pack(side="left")
                    combo_col_ref = ttk.Combobox(fila_fk, textvariable=var_col_ref, state="readonly", width=14)
                    combo_col_ref.pack(side="left", padx=(2, 10))

                    def _actualizar_columnas_ref(*_a):
                        h_ref = var_hoja_ref.get()
                        if h_ref in estado_modelo:
                            cols = list(estado_modelo[h_ref]["df"].columns)
                            combo_col_ref["values"] = cols
                            if cols:
                                var_col_ref.set(cols[0])
                    combo_hoja_ref.bind("<<ComboboxSelected>>", _actualizar_columnas_ref)
                    _actualizar_columnas_ref()

                    registro_fk = {
                        "var_col_fk": var_col_fk, "var_hoja_ref": var_hoja_ref,
                        "var_col_ref": var_col_ref, "frame": fila_fk,
                    }

                    def _quitar():
                        fila_fk.destroy()
                        info["fks"].remove(registro_fk)
                        _refrescar_scroll()

                    ttk.Button(fila_fk, text="🗑", width=3, command=_quitar).pack(side="left")
                    info["fks"].append(registro_fk)
                    _refrescar_scroll()

                fila_btn_fk = ttk.Frame(marco)
                fila_btn_fk.pack(fill="x", padx=8, pady=(0, 6))
                ttk.Button(fila_btn_fk, text="+ Agregar llave foránea", command=_agregar_fk).pack(side="left")

            _refrescar_scroll()

        ttk.Button(
            ventana, text="Cargar hojas elegidas", command=_cargar_hojas_elegidas,
        ).pack(anchor="w", padx=12, pady=(4, 8))

        ttk.Separator(ventana, orient="horizontal").pack(fill="x", padx=12)

        marco_scroll = ttk.Frame(ventana)
        marco_scroll.pack(fill="both", expand=True, padx=12, pady=6)
        canvas_cfg = tk.Canvas(marco_scroll, height=260, highlightthickness=0)
        scrollbar_cfg = ttk.Scrollbar(marco_scroll, orient="vertical", command=canvas_cfg.yview)
        canvas_cfg.configure(yscrollcommand=scrollbar_cfg.set)
        canvas_cfg.pack(side="left", fill="both", expand=True)
        scrollbar_cfg.pack(side="right", fill="y")
        canvas_cfg.create_window((0, 0), window=contenedor_config, anchor="nw")
        contenedor_config.bind("<Configure>", _refrescar_scroll)

        ttk.Separator(ventana, orient="horizontal").pack(fill="x", padx=12)

        ttk.Label(ventana, text="Vista previa del modelo:").pack(anchor="w", padx=12, pady=(8, 2))
        canvas_diagrama = tk.Canvas(
            ventana, height=200, bg="white", highlightthickness=1, highlightbackground="#ccc",
        )
        canvas_diagrama.pack(fill="x", padx=12, pady=(0, 8))

        def _construir_modelo() -> dict | None:
            modelo = {}
            for hoja, info in estado_modelo.items():
                nombre_tabla = info["var_tabla"].get().strip()
                if not nombre_tabla:
                    messagebox.showwarning("Falta nombre", f"Indique el nombre de tabla para la hoja '{hoja}'.")
                    return None
                pk = info["var_pk"].get()
                claves_foraneas = []
                for fk in info["fks"]:
                    h_ref = fk["var_hoja_ref"].get()
                    if h_ref not in estado_modelo:
                        continue
                    claves_foraneas.append({
                        "columna": fk["var_col_fk"].get(),
                        "tabla_referencia": estado_modelo[h_ref]["var_tabla"].get().strip(),
                        "columna_referencia": fk["var_col_ref"].get(),
                    })
                modelo[nombre_tabla] = {
                    "hoja": hoja,
                    "clave_primaria": None if pk == "(ninguna)" else pk,
                    "claves_foraneas": claves_foraneas,
                }
            return modelo

        def _actualizar_diagrama():
            modelo = _construir_modelo()
            if modelo is not None:
                _dibujar_diagrama_modelo(canvas_diagrama, modelo)

        ttk.Button(
            ventana, text="🔄 Actualizar diagrama", command=_actualizar_diagrama,
        ).pack(anchor="w", padx=12, pady=(0, 8))

        ttk.Separator(ventana, orient="horizontal").pack(fill="x", padx=12)

        ttk.Button(
            ventana, text="🛠️ Generar script para crear la base de datos...",
            command=lambda: self._abrir_generador_script_bd(ventana),
        ).pack(anchor="w", padx=12, pady=(8, 4))

        def _crear_en_sql():
            if not estado_modelo:
                messagebox.showwarning("Sin tablas", "Cargue las hojas y defínalas primero.")
                return
            modelo = _construir_modelo()
            if modelo is None:
                return
            cadena = self._pedir_cadena_conexion_sql(ventana)
            if not cadena:
                return
            hojas_cargadas = {hoja: info["df"] for hoja, info in estado_modelo.items()}
            try:
                mensajes = aplicar_modelo_sql(modelo, hojas_cargadas, cadena, if_exists="replace")
            except Exception as exc:
                messagebox.showerror("Error al crear el modelo", str(exc))
                return
            messagebox.showinfo("Modelo creado", "\n".join(mensajes))

        ttk.Button(
            ventana, text="🚀 Crear modelo en SQL...", command=_crear_en_sql,
        ).pack(anchor="w", padx=12, pady=(6, 12))

        ventana.protocol("WM_DELETE_WINDOW", ventana.destroy)

    def configurar_telefono(self) -> None:
        """Ventana para elegir el rango de dígitos de teléfono a validar en
        el próximo Analizar: automático por país(es), o un rango manual."""
        ventana = tk.Toplevel(self)
        ventana.title("Configurar validación de teléfono")
        ventana.geometry("380x420")
        ventana.transient(self)
        ventana.grab_set()

        modo_var = tk.StringVar(value="pais")
        ttk.Radiobutton(ventana, text="Automático por país", variable=modo_var, value="pais").pack(anchor="w", padx=15, pady=(15, 0))
        ttk.Radiobutton(ventana, text="Rango manual", variable=modo_var, value="manual").pack(anchor="w", padx=15)

        ttk.Label(
            ventana,
            text="País(es) (Ctrl/Cmd+clic para elegir varios; ninguno = rango\ninternacional amplio, 7-15 dígitos):",
            justify="left",
        ).pack(anchor="w", padx=15, pady=(10, 2))
        lista_paises = tk.Listbox(ventana, selectmode="extended", height=8, exportselection=False)
        for nombre in PAISES_TELEFONO_DISPONIBLES:
            lista_paises.insert("end", nombre)
        for i, nombre in enumerate(PAISES_TELEFONO_DISPONIBLES):
            if PAISES_TELEFONO_DISPONIBLES[nombre] in (self.paises_telefono or []):
                lista_paises.selection_set(i)
        lista_paises.pack(fill="both", expand=True, padx=15)

        marco_manual = ttk.Frame(ventana)
        ttk.Label(marco_manual, text="Dígitos exactos esperados:").pack(side="left")
        digitos_var = tk.StringVar(value="8")
        ttk.Entry(marco_manual, textvariable=digitos_var, width=6).pack(side="left", padx=6)

        def _actualizar_modo(*_a):
            if modo_var.get() == "manual":
                marco_manual.pack(fill="x", padx=15, pady=8)
                lista_paises.config(state="disabled")
            else:
                marco_manual.pack_forget()
                lista_paises.config(state="normal")

        modo_var.trace_add("write", _actualizar_modo)
        _actualizar_modo()
        if self.digitos_telefono_manual is not None:
            modo_var.set("manual")
            digitos_var.set(str(self.digitos_telefono_manual[0]))

        permitir_codigo_pais_var = tk.BooleanVar(value=self.permitir_codigo_pais_telefono)
        ttk.Checkbutton(
            ventana, variable=permitir_codigo_pais_var,
            text="Aceptar el número con código de país adelante (ej. +506 ...)",
        ).pack(anchor="w", padx=15, pady=(4, 10))

        def _guardar():
            if modo_var.get() == "manual":
                try:
                    n = int(digitos_var.get())
                except ValueError:
                    messagebox.showwarning("Valor inválido", "Ingrese un número entero de dígitos.")
                    return
                self.digitos_telefono_manual = (n, n)
                self.paises_telefono = None
            else:
                nombres_sel = [lista_paises.get(i) for i in lista_paises.curselection()]
                self.paises_telefono = [PAISES_TELEFONO_DISPONIBLES[n] for n in nombres_sel] or None
                self.digitos_telefono_manual = None
            self.permitir_codigo_pais_telefono = permitir_codigo_pais_var.get()
            ventana.destroy()

        ttk.Button(ventana, text="Guardar", command=_guardar).pack(pady=(0, 15))

    def analizar_tabla(self) -> None:
        if self.df is None:
            messagebox.showwarning("Sin datos", "Primero cargue un archivo.")
            return
        self.status_var.set("Analizando...")
        self.update_idletasks()

        self.resultado = analizar(
            self.df, metodo_atipicos=self.metodo_var.get(),
            digitos_telefono=self.digitos_telefono_manual,
            paises_telefono=self.paises_telefono,
            permitir_codigo_pais_telefono=self.permitir_codigo_pais_telefono,
        )
        self.df_limpio = None

        filas_hallazgos = pd.DataFrame([
            {"tipo": i.tipo, "columna": i.columna or "(fila completa)", "fila": i.fila,
             "valor_original": i.valor_original, "detalle": i.detalle}
            for i in self.resultado.issues
        ])
        if filas_hallazgos.empty:
            filas_hallazgos = pd.DataFrame([{"mensaje": "No se encontraron problemas."}])
        self._llenar_tabla(self.tabla_hallazgos, filas_hallazgos)
        self.notebook.select(self.tabla_hallazgos.master)

        self._construir_panel_config()
        self.status_var.set(
            f"Análisis completo: {len(self.resultado.issues)} hallazgo(s) en "
            f"{len(self.df)} filas."
        )

    def _construir_panel_config(self) -> None:
        for widget in self.marco_tipos.winfo_children():
            widget.destroy()
        self.accion_vars.clear()
        self.valor_fijo_vars.clear()

        if not self.resultado or not self.resultado.issues:
            ttk.Label(self.marco_tipos, text="No hay hallazgos que configurar.").pack(anchor="w")
            return

        por_tipo = self.resultado.por_tipo()
        for fila_idx, (tipo, cantidad) in enumerate(por_tipo.items()):
            if tipo not in OPCIONES_ACCION:
                continue
            fila = ttk.Frame(self.marco_tipos)
            fila.pack(fill="x", pady=4)

            ttk.Label(fila, text=f"{NOMBRES_TIPO.get(tipo, tipo)} ({cantidad}):",
                      width=28).pack(side="left")

            var = tk.StringVar(value=DEFAULT_CONFIG.get(tipo, OPCIONES_ACCION[tipo][0]))
            self.accion_vars[tipo] = var
            combo = ttk.Combobox(fila, textvariable=var, values=OPCIONES_ACCION[tipo],
                                  width=22, state="readonly")
            combo.pack(side="left")

            columnas_afectadas = sorted({
                issue.columna for issue in self.resultado.issues
                if issue.tipo == tipo and issue.columna
            })

            marco_valor_fijo = ttk.Frame(fila)
            marco_valor_fijo.pack(side="left", padx=10)

            def _actualizar_visibilidad(*_args, tipo=tipo, var=var,
                                          marco=marco_valor_fijo, cols=columnas_afectadas):
                for w in marco.winfo_children():
                    w.destroy()
                if var.get() == "valor_fijo":
                    for col in cols:
                        clave = f"{tipo}::{col}"
                        v = tk.StringVar()
                        self.valor_fijo_vars[clave] = v
                        ttk.Label(marco, text=f"{col} =").pack(side="left")
                        ttk.Entry(marco, textvariable=v, width=8).pack(side="left", padx=(0, 6))
                elif var.get() == "editar_individualmente":
                    ttk.Button(
                        marco, text="✏️ Editar valores...",
                        command=lambda tipo=tipo: self._abrir_editor_individual(tipo),
                    ).pack(side="left")

            var.trace_add("write", _actualizar_visibilidad)
            _actualizar_visibilidad()

    def _abrir_editor_individual(self, tipo: str) -> None:
        """Ventana con un campo editable por cada hallazgo de 'tipo', para la
        acción 'editar_individualmente' (corregir uno por uno sin un único
        valor fijo para todos). Guarda en self.correcciones_individuales."""
        issues_tipo = [i for i in self.resultado.issues if i.tipo == tipo] if self.resultado else []
        if not issues_tipo:
            messagebox.showinfo("Sin hallazgos", "No hay registros de este tipo para editar.")
            return

        ventana = tk.Toplevel(self)
        ventana.title(f"Editar {NOMBRES_TIPO.get(tipo, tipo)} — {len(issues_tipo)} registro(s)")
        ventana.geometry("560x480")
        ventana.transient(self)
        ventana.grab_set()

        marco_scroll = ttk.Frame(ventana)
        marco_scroll.pack(fill="both", expand=True, padx=10, pady=10)
        canvas = tk.Canvas(marco_scroll, highlightthickness=0)
        scrollbar = ttk.Scrollbar(marco_scroll, orient="vertical", command=canvas.yview)
        marco_filas = ttk.Frame(canvas)
        marco_filas.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=marco_filas, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        ttk.Label(marco_filas, text="Fila", width=6, font=("TkDefaultFont", 9, "bold")).grid(row=0, column=0, padx=4, pady=2)
        ttk.Label(marco_filas, text="Columna", width=14, font=("TkDefaultFont", 9, "bold")).grid(row=0, column=1, padx=4, pady=2)
        ttk.Label(marco_filas, text="Valor original", width=20, font=("TkDefaultFont", 9, "bold")).grid(row=0, column=2, padx=4, pady=2)
        ttk.Label(marco_filas, text="Valor corregido", width=20, font=("TkDefaultFont", 9, "bold")).grid(row=0, column=3, padx=4, pady=2)

        entradas: list[tuple[tuple, tk.StringVar]] = []
        for r, issue in enumerate(issues_tipo, start=1):
            clave = (tipo, issue.columna, issue.fila)
            valor_original_txt = "" if issue.valor_original is None else str(issue.valor_original)
            v = tk.StringVar(value=self.correcciones_individuales.get(clave, valor_original_txt))
            ttk.Label(marco_filas, text=str(issue.fila)).grid(row=r, column=0, padx=4, pady=1)
            ttk.Label(marco_filas, text=issue.columna or "").grid(row=r, column=1, padx=4, pady=1)
            ttk.Label(marco_filas, text=valor_original_txt).grid(row=r, column=2, padx=4, pady=1)
            ttk.Entry(marco_filas, textvariable=v, width=22).grid(row=r, column=3, padx=4, pady=1)
            entradas.append((clave, v))

        def _guardar():
            for clave, v in entradas:
                valor = v.get()
                self.correcciones_individuales[clave] = None if valor == "" else valor
            ventana.destroy()

        ttk.Button(ventana, text="Guardar correcciones", command=_guardar).pack(pady=(0, 10))

    def limpiar_tabla(self) -> None:
        if self.resultado is None:
            messagebox.showwarning("Sin análisis", "Primero analice la tabla.")
            return
        if not self.resultado.issues:
            messagebox.showinfo("Nada que limpiar", "No se encontraron problemas en la tabla.")
            return

        config = {tipo: var.get() for tipo, var in self.accion_vars.items()}
        valores_fijos: dict[str, str] = {}
        faltan = []
        for tipo, accion in config.items():
            if accion != "valor_fijo":
                continue
            columnas_afectadas = sorted({
                issue.columna for issue in self.resultado.issues
                if issue.tipo == tipo and issue.columna
            })
            for col in columnas_afectadas:
                v = self.valor_fijo_vars.get(f"{tipo}::{col}")
                valor = v.get().strip() if v else ""
                if valor == "":
                    faltan.append(f"{NOMBRES_TIPO.get(tipo, tipo)} → columna '{col}'")
                else:
                    valores_fijos[(tipo, col)] = valor

        if faltan:
            messagebox.showwarning(
                "Faltan valores fijos",
                "Complete el valor de reemplazo para:\n- " + "\n- ".join(faltan),
            )
            return

        self.df_limpio, self.registro = limpiar(
            self.df, self.resultado.issues, config=config, valores_fijos=valores_fijos,
            correcciones_individuales=self.correcciones_individuales,
        )
        self.config_aplicada = config
        self.valores_fijos_aplicados = valores_fijos
        self.tablas_reporte = construir_reporte(
            self.resultado, self.registro,
            nombre_fuente=os.path.basename(self.ruta_actual or ""),
        )
        self._llenar_tabla(self.tabla_datos, self.df_limpio)
        self.notebook.select(self.tabla_datos.master)
        self.status_var.set(
            f"Limpieza completa: {len(self.df_limpio)} filas finales "
            f"(originales: {len(self.df)})."
        )
        messagebox.showinfo("Listo", "Tabla limpiada. Use 'Guardar resultados...' para exportar.")

    def guardar_resultados(self) -> None:
        if self.df_limpio is None:
            messagebox.showwarning("Nada que guardar", "Primero limpie la tabla.")
            return

        carpeta = filedialog.askdirectory(title="Carpeta donde guardar los resultados")
        if not carpeta:
            return

        formato = "excel"
        ext = "xlsx"
        ruta_limpio = os.path.join(carpeta, f"datos_limpios.{ext}")
        ruta_reporte = os.path.join(carpeta, "reporte_calidad_datos.xlsx")
        try:
            exportar(self.df_limpio, ruta_limpio, kind=formato)
            exportar_reporte_excel(self.tablas_reporte, ruta_reporte)
        except Exception as exc:
            messagebox.showerror("Error al guardar", str(exc))
            return

        self.status_var.set(f"Guardado en {carpeta}")
        messagebox.showinfo("Guardado", f"Archivos guardados en:\n{carpeta}")

    def exportar_sql(self) -> None:
        """
        Escribe self.df_limpio de vuelta en una base de datos SQL, via
        exporters.exportar_sql (ya existia en data_cleaner pero no estaba
        conectada a ninguna interfaz). Reutiliza el mismo esquema de
        conexion que conectar_sql, pero para el sentido contrario (escribir
        en vez de leer).
        """
        if self.df_limpio is None:
            messagebox.showwarning("Nada que exportar", "Primero limpie la tabla.")
            return

        ventana = tk.Toplevel(self)
        ventana.title("Exportar datos limpios a SQL")
        ventana.geometry("440x460")
        ventana.transient(self)
        ventana.grab_set()

        motores = ["PostgreSQL", "MySQL", "SQL Server", "SQLite", "Otra (cadena de conexión manual)"]
        motor_var = tk.StringVar(value=motores[0])
        ttk.Label(ventana, text="Motor de base de datos:").pack(anchor="w", padx=15, pady=(15, 2))
        ttk.Combobox(ventana, textvariable=motor_var, values=motores, state="readonly").pack(fill="x", padx=15)

        marco_campos = ttk.Frame(ventana)
        marco_campos.pack(fill="x", padx=15, pady=10)

        campos_vars = {
            "host": tk.StringVar(value="localhost"), "puerto": tk.StringVar(),
            "usuario": tk.StringVar(), "clave": tk.StringVar(), "basedatos": tk.StringVar(),
            "ruta_sqlite": tk.StringVar(), "cadena_manual": tk.StringVar(),
        }
        _puertos_defecto = {"PostgreSQL": "5432", "MySQL": "3306", "SQL Server": "1433"}

        def _redibujar_campos(*_args):
            for w in marco_campos.winfo_children():
                w.destroy()
            motor = motor_var.get()
            if motor == "SQLite":
                ttk.Label(marco_campos, text="Ruta del archivo .db:").pack(anchor="w")
                ttk.Entry(marco_campos, textvariable=campos_vars["ruta_sqlite"]).pack(fill="x")
            elif motor == "Otra (cadena de conexión manual)":
                ttk.Label(marco_campos, text="Cadena de conexión SQLAlchemy completa:").pack(anchor="w")
                ttk.Entry(marco_campos, textvariable=campos_vars["cadena_manual"], show="•").pack(fill="x")
            else:
                campos_vars["puerto"].set(_puertos_defecto.get(motor, ""))
                for etiqueta, clave, oculto in [
                    ("Host", "host", False), ("Puerto", "puerto", False),
                    ("Usuario", "usuario", False), ("Contraseña", "clave", True),
                    ("Base de datos", "basedatos", False),
                ]:
                    ttk.Label(marco_campos, text=f"{etiqueta}:").pack(anchor="w")
                    ttk.Entry(marco_campos, textvariable=campos_vars[clave],
                              show="•" if oculto else "").pack(fill="x", pady=(0, 4))

        motor_var.trace_add("write", _redibujar_campos)
        _redibujar_campos()

        ttk.Separator(ventana, orient="horizontal").pack(fill="x", padx=15, pady=6)

        ttk.Label(ventana, text="Tabla destino:").pack(anchor="w", padx=15)
        tabla_var = tk.StringVar()
        ttk.Entry(ventana, textvariable=tabla_var).pack(fill="x", padx=15, pady=(0, 8))

        ttk.Label(ventana, text="Si la tabla ya existe:").pack(anchor="w", padx=15)
        si_existe_var = tk.StringVar(value="replace")
        ttk.Combobox(ventana, textvariable=si_existe_var, values=["replace", "append", "fail"],
                     state="readonly").pack(fill="x", padx=15, pady=(0, 15))

        def _exportar():
            motor = motor_var.get()
            if motor == "SQLite":
                if not campos_vars["ruta_sqlite"].get():
                    messagebox.showwarning("Falta la ruta", "Indique la ruta del archivo .db.")
                    return
                cadena = f"sqlite:///{campos_vars['ruta_sqlite'].get()}"
            elif motor == "Otra (cadena de conexión manual)":
                cadena = campos_vars["cadena_manual"].get()
                if not cadena:
                    messagebox.showwarning("Falta la cadena", "Ingrese la cadena de conexión.")
                    return
            else:
                driver = {"PostgreSQL": "postgresql+psycopg2", "MySQL": "mysql+pymysql",
                          "SQL Server": "mssql+pyodbc"}[motor]
                if not (campos_vars["host"].get() and campos_vars["basedatos"].get()):
                    messagebox.showwarning("Faltan datos", "Complete al menos host y base de datos.")
                    return
                if motor == "SQL Server":
                    parametros_odbc = "driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=yes"
                    servidor = f"{campos_vars['host'].get()}:{campos_vars['puerto'].get()}" \
                        if campos_vars["puerto"].get() else campos_vars["host"].get()
                    if not campos_vars["usuario"].get() and not campos_vars["clave"].get():
                        cadena = f"{driver}://@{servidor}/{campos_vars['basedatos'].get()}?{parametros_odbc}&trusted_connection=yes"
                    else:
                        cadena = (f"{driver}://{campos_vars['usuario'].get()}:{campos_vars['clave'].get()}"
                                  f"@{servidor}/{campos_vars['basedatos'].get()}?{parametros_odbc}")
                else:
                    if not campos_vars["usuario"].get():
                        messagebox.showwarning("Faltan datos", "Complete usuario para este motor.")
                        return
                    cadena = (f"{driver}://{campos_vars['usuario'].get()}:{campos_vars['clave'].get()}"
                              f"@{campos_vars['host'].get()}:{campos_vars['puerto'].get()}/{campos_vars['basedatos'].get()}")

            tabla = tabla_var.get().strip()
            if not tabla:
                messagebox.showwarning("Falta la tabla", "Indique el nombre de la tabla destino.")
                return

            try:
                from data_cleaner.exporters import exportar_sql as _exportar_sql
                mensaje = _exportar_sql(self.df_limpio, cadena, tabla, if_exists=si_existe_var.get())
            except Exception as exc:
                messagebox.showerror("Error al exportar", str(exc))
                return

            self.status_var.set(mensaje)
            messagebox.showinfo("Exportado", mensaje)
            ventana.destroy()

        ttk.Button(ventana, text="Exportar", command=_exportar).pack(pady=(0, 10))

    def exportar_script_portatil(self) -> None:
        """Genera un script autocontenido (Power BI / código M / universal) con la
        misma configuración de limpieza ya aplicada, listo para pegar en otras
        herramientas de BI (Power BI, Tableau Prep, Alteryx, Qlik, etc.)."""
        if not self.config_aplicada:
            messagebox.showwarning(
                "Sin configuración",
                "Primero presione 'Limpiar tabla y generar reporte' para fijar la "
                "configuración que se va a exportar.",
            )
            return

        ventana = tk.Toplevel(self)
        ventana.title("Exportar script portátil")
        ventana.geometry("460x560")
        ventana.transient(self)
        ventana.grab_set()

        ttk.Label(
            ventana,
            text="Elija qué generar (usa la configuración de limpieza ya aplicada):",
            wraplength=420, justify="left",
        ).pack(padx=15, pady=(15, 10), anchor="w")

        ttk.Button(
            ventana, text="Script para Power BI (.py)",
            command=lambda: self._guardar_script(
                generar_script_powerbi(
                    self.config_aplicada, 1.5, self.valores_fijos_aplicados,
                    correcciones_individuales=self.correcciones_individuales,
                ),
                "limpiador_powerbi_generado.py", [("Python", "*.py")], ventana,
            ),
        ).pack(fill="x", padx=15, pady=4)

        ttk.Button(
            ventana, text="Código M (Editor avanzado de Power Query)",
            command=lambda: self._guardar_script(
                generar_editor_m(
                    self.config_aplicada, 1.5, self.valores_fijos_aplicados,
                    correcciones_individuales=self.correcciones_individuales,
                ),
                "editor_avanzado_powerbi_generado.m", [("M", "*.m"), ("Texto", "*.txt")], ventana,
            ),
        ).pack(fill="x", padx=15, pady=4)

        ttk.Button(
            ventana, text="Script universal (Tableau/Alteryx/Qlik) (.py)",
            command=lambda: self._guardar_script(
                generar_script_universal(
                    self.config_aplicada, 1.5, self.valores_fijos_aplicados,
                    correcciones_individuales=self.correcciones_individuales,
                ),
                "limpiador_universal_generado.py", [("Python", "*.py")], ventana,
            ),
        ).pack(fill="x", padx=15, pady=4)

        ttk.Separator(ventana, orient="horizontal").pack(fill="x", padx=15, pady=8)

        # -- Opciones de teléfono para el M puro ---------------------------------
        # Antes el rango de dígitos era fijo (8, formato de Costa Rica). Ahora se
        # puede elegir país(es) — la validación acepta la UNION de sus rangos
        # típicos de celular — o dejarlo vacío para el rango internacional amplio
        # (7-15 dígitos, E.164). El desglose por dígito (columnas Telefono_Digito_N)
        # se eliminó: el código M puro ya no agrega columnas nuevas.
        frame_tel = ttk.LabelFrame(ventana, text="Teléfono (solo aplica al M puro)")
        frame_tel.pack(fill="x", padx=15, pady=(0, 8))

        modo_tel_var = tk.StringVar(value="pais")
        ttk.Radiobutton(
            frame_tel, text="Automático por país", variable=modo_tel_var, value="pais",
        ).pack(anchor="w", padx=8, pady=(6, 0))
        ttk.Radiobutton(
            frame_tel, text="Rango manual", variable=modo_tel_var, value="manual",
        ).pack(anchor="w", padx=8)

        marco_pais = ttk.Frame(frame_tel)
        ttk.Label(
            marco_pais,
            text="País(es) (coma-separado, ej: cr,mexico — vacío = rango internacional amplio):",
            wraplength=420, justify="left",
        ).pack(anchor="w", pady=(4, 0))
        paises_tel_var = tk.StringVar(value="cr")
        ttk.Entry(marco_pais, textvariable=paises_tel_var).pack(fill="x", pady=(2, 4))

        marco_manual_tel = ttk.Frame(frame_tel)
        ttk.Label(marco_manual_tel, text="Dígitos exactos esperados:").pack(side="left")
        digitos_tel_var = tk.StringVar(value="8")
        ttk.Entry(marco_manual_tel, textvariable=digitos_tel_var, width=6).pack(side="left", padx=6)

        def _actualizar_modo_tel(*_a):
            if modo_tel_var.get() == "manual":
                marco_pais.pack_forget()
                marco_manual_tel.pack(fill="x", padx=8, pady=(4, 4))
            else:
                marco_manual_tel.pack_forget()
                marco_pais.pack(fill="x", padx=8)

        modo_tel_var.trace_add("write", _actualizar_modo_tel)
        _actualizar_modo_tel()

        ttk.Label(
            frame_tel,
            text="Primeros dígitos válidos (coma-separado; vacío = no validar):",
            wraplength=420, justify="left",
        ).pack(anchor="w", padx=8, pady=(4, 0))
        primeros_digitos_tel_var = tk.StringVar(value="2,4,5,6,7,8")
        ttk.Entry(frame_tel, textvariable=primeros_digitos_tel_var).pack(fill="x", padx=8, pady=(2, 6))

        permitir_codigo_pais_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame_tel, variable=permitir_codigo_pais_var,
            text="Aceptar el mismo número con código de país adelante (ej. +506 ...)",
        ).pack(anchor="w", padx=8)

        ttk.Label(
            frame_tel,
            text="Este código nunca agrega columnas nuevas (ni Revisar_*, ni "
                 "Requiere_Revision, ni desglose de teléfono por dígito). Lo marcado "
                 "como 'solo marcar' queda como comentario en el código, sin tocar la tabla.",
            wraplength=420, justify="left",
        ).pack(anchor="w", padx=8, pady=(0, 6))

        def _generar_m_puro():
            paises_lista = None
            digitos_manual = None
            if modo_tel_var.get() == "manual":
                try:
                    n = int(digitos_tel_var.get())
                except ValueError:
                    messagebox.showwarning("Valor inválido", "Ingrese un número entero de dígitos.")
                    return
                digitos_manual = (n, n)
            else:
                paises_lista = (
                    [p.strip() for p in paises_tel_var.get().split(",") if p.strip()]
                    if paises_tel_var.get().strip() else None
                )
            primeros_digitos_lista = (
                [d.strip() for d in primeros_digitos_tel_var.get().split(",") if d.strip()]
                if primeros_digitos_tel_var.get().strip() else None
            )
            self._guardar_script(
                generar_editor_m_puro(
                    self.df,
                    config=self.config_aplicada,
                    factor_iqr=1.5,
                    valores_fijos=self.valores_fijos_aplicados,
                    fecha_invalida=self.config_aplicada.get("fecha_invalida", "marcar_solo"),
                    email_invalido=self.config_aplicada.get("email_invalido", "marcar_solo"),
                    telefono_invalido=self.config_aplicada.get("telefono_invalido", "marcar_solo"),
                    correcciones_individuales=self.correcciones_individuales,
                    digitos_telefono=digitos_manual,
                    paises_telefono=paises_lista,
                    permitir_codigo_pais_telefono=permitir_codigo_pais_var.get(),
                    primeros_digitos_telefono_validos=primeros_digitos_lista,
                    id_duplicado=self.config_aplicada.get("id_duplicado", "marcar_solo"),
                    formula_incorrecta=self.config_aplicada.get("formula_incorrecta", "marcar_solo"),
                    texto_inconsistente=self.config_aplicada.get("texto_inconsistente", "marcar_solo"),
                    estado_invalido=self.config_aplicada.get("estado_invalido", "marcar_solo"),
                ),
                "codigo_m_puro_generado.m", [("M", "*.m"), ("Texto", "*.txt")], ventana,
            )

        ttk.Button(
            ventana, text="Código M PURO (sin Python.Execute) — recomendado",
            command=_generar_m_puro,
        ).pack(fill="x", padx=15, pady=4)

    def _guardar_script(self, contenido: str, nombre_sugerido: str, tipos_archivo, ventana) -> None:
        ruta = filedialog.asksaveasfilename(
            title="Guardar script", initialfile=nombre_sugerido, filetypes=tipos_archivo,
        )
        if not ruta:
            return
        try:
            with open(ruta, "w", encoding="utf-8") as f:
                f.write(contenido)
        except Exception as exc:
            messagebox.showerror("Error al guardar", str(exc))
            return
        ventana.destroy()
        self.status_var.set(f"Script exportado en {ruta}")
        messagebox.showinfo("Exportado", f"Script guardado en:\n{ruta}")


def main():
    app = LimpiadorApp()
    app.mainloop()


if __name__ == "__main__":
    main()

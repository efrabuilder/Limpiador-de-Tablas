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
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import pandas as pd

from data_cleaner import (
    load_table, analizar, limpiar, DEFAULT_CONFIG,
    construir_reporte, exportar_reporte_excel, exportar,
)
from data_cleaner.loaders import load_excel
from data_cleaner.exportador import (
    generar_script_powerbi, generar_script_universal, generar_editor_m,
)
from data_cleaner.exportador_m import generar_editor_m_puro

OPCIONES_ACCION = {
    "faltante": ["reemplazar_mediana", "reemplazar_media", "reemplazar_moda",
                 "valor_fijo", "eliminar_fila", "marcar_solo"],
    "duplicado": ["eliminar_fila", "marcar_solo"],
    "atipico": ["limitar", "reemplazar_mediana", "reemplazar_media",
                "eliminar_fila", "marcar_solo"],
    "tipo_invalido": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "fecha_invalida": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "email_invalido": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "telefono_invalido": ["editar_individualmente", "eliminar_fila", "valor_fijo", "marcar_solo"],
    "id_duplicado": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "formula_incorrecta": ["usar_sugerido", "eliminar_fila", "valor_fijo", "marcar_solo"],
    "texto_inconsistente": ["usar_sugerido", "eliminar_fila", "valor_fijo", "marcar_solo"],
}

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
}

# País(es) disponibles para el rango de dígitos de teléfono/celular (ver
# patrones.DIGITOS_TELEFONO_PAIS), reutilizado tanto para configurar el
# análisis como para la exportación de M puro.
PAISES_TELEFONO_DISPONIBLES = {
    "Costa Rica": "cr", "México": "mexico", "Colombia": "colombia",
    "Argentina": "argentina", "España": "espana", "Estados Unidos": "us",
    "Panamá": "panama", "Guatemala": "guatemala", "Honduras": "honduras",
    "Nicaragua": "nicaragua", "El Salvador": "el_salvador", "Chile": "chile",
    "Perú": "peru", "Ecuador": "ecuador", "Venezuela": "venezuela",
    "Brasil": "brasil", "Uruguay": "uruguay", "Bolivia": "bolivia",
    "República Dominicana": "republica_dominicana", "Reino Unido": "reino_unido",
    "Alemania": "alemania", "Francia": "francia", "Canadá": "canada",
}


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
                if not (campos_vars["host"].get() and campos_vars["usuario"].get() and campos_vars["basedatos"].get()):
                    messagebox.showwarning("Faltan datos", "Complete host, usuario y base de datos.")
                    return
                cadena = (f"{driver}://{campos_vars['usuario'].get()}:{campos_vars['clave'].get()}"
                          f"@{campos_vars['host'].get()}:{campos_vars['puerto'].get()}/{campos_vars['basedatos'].get()}")
                if motor == "SQL Server":
                    cadena += "?driver=ODBC+Driver+17+for+SQL+Server"

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
                    valores_fijos[col] = valor

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
        ventana.geometry("460x360")
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
                generar_script_powerbi(self.config_aplicada, 1.5, self.valores_fijos_aplicados),
                "limpiador_powerbi_generado.py", [("Python", "*.py")], ventana,
            ),
        ).pack(fill="x", padx=15, pady=4)

        ttk.Button(
            ventana, text="Código M (Editor avanzado de Power Query)",
            command=lambda: self._guardar_script(
                generar_editor_m(self.config_aplicada, 1.5, self.valores_fijos_aplicados),
                "editor_avanzado_powerbi_generado.m", [("M", "*.m"), ("Texto", "*.txt")], ventana,
            ),
        ).pack(fill="x", padx=15, pady=4)

        ttk.Button(
            ventana, text="Script universal (Tableau/Alteryx/Qlik) (.py)",
            command=lambda: self._guardar_script(
                generar_script_universal(self.config_aplicada, 1.5, self.valores_fijos_aplicados),
                "limpiador_universal_generado.py", [("Python", "*.py")], ventana,
            ),
        ).pack(fill="x", padx=15, pady=4)

        ttk.Separator(ventana, orient="horizontal").pack(fill="x", padx=15, pady=8)

        # -- Opciones de teléfono para el M puro ---------------------------------
        # Antes el rango de dígitos era fijo (8, formato de Costa Rica). Ahora se
        # puede elegir país(es) — la validación acepta la UNION de sus rangos
        # típicos de celular — o dejarlo vacío para el rango internacional amplio
        # (7-15 dígitos, E.164). También se puede activar/desactivar el desglose
        # por dígito (columnas Telefono_Digito_N + tabla de correcciones editable).
        frame_tel = ttk.LabelFrame(ventana, text="Teléfono (solo aplica al M puro)")
        frame_tel.pack(fill="x", padx=15, pady=(0, 8))

        ttk.Label(
            frame_tel,
            text="País(es) (coma-separado, ej: cr,mexico — vacío = rango internacional amplio):",
            wraplength=420, justify="left",
        ).pack(anchor="w", padx=8, pady=(6, 0))
        paises_tel_var = tk.StringVar(value="cr")
        ttk.Entry(frame_tel, textvariable=paises_tel_var).pack(fill="x", padx=8, pady=(2, 6))

        permitir_codigo_pais_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame_tel, variable=permitir_codigo_pais_var,
            text="Aceptar el mismo número con código de país adelante (ej. +506 ...)",
        ).pack(anchor="w", padx=8)

        desglosar_digitos_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame_tel, variable=desglosar_digitos_var,
            text="Agregar columnas para ver y corregir cada dígito por separado",
        ).pack(anchor="w", padx=8, pady=(0, 6))

        def _generar_m_puro():
            paises_lista = (
                [p.strip() for p in paises_tel_var.get().split(",") if p.strip()]
                if paises_tel_var.get().strip() else None
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
                    paises_telefono=paises_lista,
                    permitir_codigo_pais_telefono=permitir_codigo_pais_var.get(),
                    desglosar_digitos_telefono=desglosar_digitos_var.get(),
                    id_duplicado=self.config_aplicada.get("id_duplicado", "marcar_solo"),
                    formula_incorrecta=self.config_aplicada.get("formula_incorrecta", "marcar_solo"),
                    texto_inconsistente=self.config_aplicada.get("texto_inconsistente", "marcar_solo"),
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

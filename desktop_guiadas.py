# -*- coding: utf-8 -*-
"""
desktop_guiadas.py
==================
Las tres herramientas nuevas de la interfaz de escritorio (Tkinter), cada una
en su propia ventana:

    - VentanaLimpiezaGuiada: diagnostico de nulos, estandarizacion (con fechas
      en un solo formato y union de valores equivalentes), una regla por
      columna y cierre sin vacios (ver data_cleaner/limpieza_guiada.py)
    - VentanaMerge: unir dos tablas con revision de llaves y auditoria
      (ver data_cleaner/merge_tablas.py)
    - VentanaDiccionario: diccionario de datos en sus tres salidas: Excel basico,
      diccionario tecnico (Excel) y documento de alcance (Word)
      (ver data_cleaner/diccionario_datos.py)

La logica vive en data_cleaner/flujos_guiados.py (la misma que usan la app web,
la CLI, la API, main.py y el notebook); aqui solo esta la interfaz. Se abren
desde desktop_app.py, que les pasa `app` para compartir tablas entre ellas.
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import pandas as pd

from data_cleaner import diccionario_datos as DD
from data_cleaner import flujos_guiados as FG
from data_cleaner import limpieza_guiada as LG
from data_cleaner import merge_tablas as MT

TIPOS_ARCHIVO = [("Tablas (CSV / Excel)", "*.csv *.xlsx *.xlsm *.xls"), ("Todos los archivos", "*.*")]
FORMATOS_SALIDA = ["csv", "xlsx", "ambos"]
OPCIONES_NUMEROS = {"mediana": "números → mediana", "cero": "números → 0",
                    "palabra": "números → la palabra (pasan a texto)"}


# --------------------------------------------------------------------------
# Utilidades de interfaz
# --------------------------------------------------------------------------

def _crear_tabla(parent) -> ttk.Treeview:
    marco = ttk.Frame(parent)
    tree = ttk.Treeview(marco, show="headings", height=8)
    vsb = ttk.Scrollbar(marco, orient="vertical", command=tree.yview)
    hsb = ttk.Scrollbar(marco, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    marco.rowconfigure(0, weight=1)
    marco.columnconfigure(0, weight=1)
    return tree


def _llenar_tabla(tree: ttk.Treeview, df: pd.DataFrame, max_filas: int = 200) -> None:
    tree.delete(*tree.get_children())
    tree["columns"] = [str(c) for c in df.columns]
    for col in df.columns:
        tree.heading(str(col), text=str(col))
        tree.column(str(col), width=110, anchor="w")
    for fila in df.head(max_filas).itertuples(index=False):
        tree.insert("", "end", values=["" if pd.isna(v) else v for v in fila])


def _escribir_texto(texto: tk.Text, contenido: str) -> None:
    texto.config(state="normal")
    texto.delete("1.0", "end")
    texto.insert("1.0", contenido)
    texto.config(state="disabled")


def _lista(texto: str) -> list[str]:
    return [c.strip() for c in texto.split(",") if c.strip()]


def _seleccion(lista: tk.Listbox) -> list[str]:
    return [lista.get(i) for i in lista.curselection()]


def _marcar(lista: tk.Listbox, nombres: list[str]) -> None:
    lista.selection_clear(0, "end")
    for i, nombre in enumerate(lista.get(0, "end")):
        if nombre in nombres:
            lista.selection_set(i)


def _elegir_opcion(parent, titulo: str, texto: str, opciones: list[str]) -> str | None:
    """Dialogo modal con una lista desplegable. None si se cierra sin elegir."""
    elegido: dict[str, str | None] = {"valor": None}
    ventana = tk.Toplevel(parent)
    ventana.title(titulo)
    ventana.resizable(False, False)
    ttk.Label(ventana, text=texto, wraplength=320, justify="left").pack(padx=15, pady=(15, 5))
    variable = tk.StringVar(value=opciones[0])
    ttk.Combobox(ventana, textvariable=variable, values=opciones, state="readonly", width=40).pack(padx=15, pady=5)

    def _confirmar() -> None:
        elegido["valor"] = variable.get()
        ventana.destroy()

    ttk.Button(ventana, text="Aceptar", command=_confirmar).pack(pady=(5, 15))
    ventana.protocol("WM_DELETE_WINDOW", ventana.destroy)
    ventana.transient(parent)
    ventana.grab_set()
    parent.wait_window(ventana)
    return elegido["valor"]


def _abrir_tabla(parent) -> tuple[pd.DataFrame, str, str | None] | None:
    """Pide un CSV/Excel (y la hoja si el libro tiene varias). (df, ruta, hoja) o None."""
    ruta = filedialog.askopenfilename(parent=parent, filetypes=TIPOS_ARCHIVO)
    if not ruta:
        return None
    hojas = FG.hojas_de_archivo(ruta)
    hoja = None
    if len(hojas) > 1:
        hoja = _elegir_opcion(parent, "Elegir hoja", "Este libro tiene varias hojas. ¿Cuál desea usar?", hojas)
        if hoja is None:
            return None
    return FG.leer_tabla_guiada(ruta, hoja=hoja), ruta, hoja


class _VentanaBase(tk.Toplevel):
    def __init__(self, app, titulo: str, geometria: str):
        super().__init__(app)
        self.app = app
        self.title(titulo)
        self.geometry(geometria)
        self.minsize(820, 560)

    def _ejecutar(self, funcion) -> None:
        """Corre una accion y muestra cualquier error como mensaje, sin cerrar la ventana."""
        try:
            funcion()
        except Exception as exc:  # noqa: BLE001 - se muestra al usuario
            messagebox.showerror("No se pudo completar", str(exc), parent=self)

    def _origenes_de_la_app(self) -> dict:
        """Tablas que ya hay en la app: {texto del menu: (df, nombre)}."""
        origenes = {}
        if self.app.df is not None:
            origenes["Tabla de la ventana principal"] = (
                self.app.df, os.path.basename(self.app.ruta_actual or "tabla.csv"))
        if self.app.tabla_guiada is not None:
            origenes["Resultado de la limpieza guiada"] = self.app.tabla_guiada
        if self.app.tabla_merge is not None:
            origenes["Resultado del merge"] = self.app.tabla_merge
        return origenes

    def _menu_origenes(self, boton: ttk.Button, al_elegir) -> None:
        """Menu emergente con las tablas de la app; `al_elegir(df, nombre)` al elegir una."""
        menu = tk.Menu(self, tearoff=0)
        origenes = self._origenes_de_la_app()
        for texto, (df, nombre) in origenes.items():
            menu.add_command(label=texto, command=lambda d=df, n=nombre: self._ejecutar(lambda: al_elegir(d, n)))
        if not origenes:
            menu.add_command(label="(todavía no hay tablas)", state="disabled")
        menu.tk_popup(boton.winfo_rootx(), boton.winfo_rooty() + boton.winfo_height())


# --------------------------------------------------------------------------
# Limpieza guiada
# --------------------------------------------------------------------------

class VentanaLimpiezaGuiada(_VentanaBase):
    def __init__(self, app):
        super().__init__(app, "Limpieza guiada de nulos", "1100x820")
        self.df: pd.DataFrame | None = None
        self.nombre = ""
        self.hoja: str | None = None
        self.config: dict | None = None
        self.editores: dict[str, tuple[tk.StringVar, tk.StringVar, tk.StringVar]] = {}
        self.resultado: FG.ResultadoLimpiezaGuiada | None = None
        self._cargando = False
        self._construir()

    # ---- construccion -------------------------------------------------
    def _construir(self) -> None:
        arriba = ttk.Frame(self, padding=8)
        arriba.pack(fill="x")
        ttk.Button(arriba, text="📂 Abrir archivo...", command=lambda: self._ejecutar(self._abrir)).pack(side="left")
        self.btn_origen = ttk.Button(arriba, text="Usar tabla de la app ▾",
                                     command=lambda: self._menu_origenes(self.btn_origen, self._cargar))
        self.btn_origen.pack(side="left", padx=6)
        self.lbl_archivo = ttk.Label(arriba, text="Ningún archivo cargado.")
        self.lbl_archivo.pack(side="left", padx=10)
        ttk.Button(arriba, text="🔍 Diagnosticar", command=lambda: self._ejecutar(self._diagnosticar)).pack(side="right")

        marco_tokens = ttk.LabelFrame(self, text="Contar como nulo (además de la celda vacía)", padding=6)
        marco_tokens.pack(fill="x", padx=8)
        fila_tokens = ttk.Frame(marco_tokens)
        fila_tokens.pack(fill="x")
        self.var_tokens = {}
        for token in list(FG.TOKENS_EXTRA_DEFECTO) + list(LG.TOKENS_NULOS_EXTRA):
            self.var_tokens[token] = tk.BooleanVar(value=token in FG.TOKENS_EXTRA_DEFECTO)
            ttk.Checkbutton(fila_tokens, text=token, variable=self.var_tokens[token]).pack(side="left", padx=4)
        fila_palabra = ttk.Frame(marco_tokens)
        fila_palabra.pack(fill="x", pady=(6, 0))
        ttk.Label(fila_palabra, text="Palabra para los nulos válidos").pack(side="left")
        self.var_palabra = tk.StringVar(value=LG.TEXTO_NO_INDICA)
        ttk.Entry(fila_palabra, textvariable=self.var_palabra, width=22).pack(side="left", padx=6)
        ttk.Label(fila_palabra, foreground="gray",
                  text="Los textos tipo «unknown», «-» o «not available» que aparezcan en la tabla se marcan solos.").pack(
            side="left", padx=6)

        self.pestanas = ttk.Notebook(self)
        self.pestanas.pack(fill="both", expand=True, padx=8, pady=8)

        self.tabla_diag = _crear_tabla(self.pestanas)
        self.pestanas.add(self.tabla_diag.master, text="1. Diagnóstico")
        self._construir_estandarizar()
        self._construir_reglas()
        self._construir_resultado()

        abajo = ttk.Frame(self, padding=(8, 0, 8, 8))
        abajo.pack(fill="x")
        ttk.Button(abajo, text="🧽 Aplicar limpieza", command=lambda: self._ejecutar(self._aplicar)).pack(side="left")
        self.var_formato = tk.StringVar(value="csv")
        ttk.Combobox(abajo, textvariable=self.var_formato, values=FORMATOS_SALIDA, width=7,
                     state="readonly").pack(side="left", padx=(14, 4))
        ttk.Button(abajo, text="💾 Guardar en carpeta...", command=lambda: self._ejecutar(self._guardar)).pack(side="left")
        ttk.Button(abajo, text="➡️ Enviar a limpieza clásica",
                   command=lambda: self._ejecutar(self._enviar)).pack(side="left", padx=10)

    def _construir_estandarizar(self) -> None:
        marco = ttk.Frame(self.pestanas, padding=10)
        self.pestanas.add(marco, text="2. Estandarizar")
        self.var_snake = tk.BooleanVar(value=True)
        self.var_vacios = tk.BooleanVar(value=True)
        self.var_texto = tk.BooleanVar(value=True)
        self.var_min = tk.BooleanVar(value=True)
        self.var_esp = tk.BooleanVar(value=True)
        self.var_com = tk.BooleanVar(value=True)
        ttk.Checkbutton(marco, text="Nombres de columna en snake_case", variable=self.var_snake,
                        command=self._al_cambiar_nombres).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(marco, text="Pasar los vacíos a nulos reales (NaN)",
                        variable=self.var_vacios).grid(row=1, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(marco, text="Estandarizar texto", variable=self.var_texto).grid(row=2, column=0, sticky="w")
        ttk.Checkbutton(marco, text="Minúsculas", variable=self.var_min).grid(row=2, column=1, sticky="w")
        ttk.Checkbutton(marco, text="Espacios repetidos", variable=self.var_esp).grid(row=2, column=2, sticky="w")
        ttk.Checkbutton(marco, text="Quitar comillas", variable=self.var_com).grid(row=2, column=3, sticky="w")
        self.var_cols_texto, self.var_cols_num = tk.StringVar(), tk.StringVar()
        self.var_lat, self.var_lon = tk.StringVar(), tk.StringVar()
        self.var_cols_fecha = tk.StringVar()
        campos = [("Columnas de texto (coma-separadas)", self.var_cols_texto),
                  ("Columnas a convertir a número", self.var_cols_num),
                  ("Columna de latitud", self.var_lat), ("Columna de longitud", self.var_lon),
                  ("Columnas de fecha (un solo formato)", self.var_cols_fecha)]
        fila = 3
        for etiqueta, variable in campos:
            ttk.Label(marco, text=etiqueta).grid(row=fila, column=0, sticky="w", pady=(8, 0))
            ttk.Entry(marco, textvariable=variable, width=70).grid(row=fila, column=1, columnspan=3, sticky="we", pady=(8, 0))
            fila += 1

        self.var_formato_fecha = tk.StringVar(value=next(iter(LG.FORMATOS_FECHA)))
        self.var_dia_primero = tk.BooleanVar(value=True)
        ttk.Label(marco, text="Formato de salida de las fechas").grid(row=fila, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(marco, textvariable=self.var_formato_fecha, values=list(LG.FORMATOS_FECHA), state="readonly",
                     width=26).grid(row=fila, column=1, sticky="w", pady=(8, 0))
        ttk.Checkbutton(marco, text="05/06/2025 = día/mes/año (si no hay pistas)",
                        variable=self.var_dia_primero).grid(row=fila, column=2, columnspan=2, sticky="w", pady=(8, 0))
        fila += 1

        self.var_unir = tk.BooleanVar(value=True)
        ttk.Checkbutton(marco, text="Unir valores que significan lo mismo (m / male / masculino → M; "
                                    "si / yes → Sí; variantes de tildes y signos)",
                        variable=self.var_unir).grid(row=fila, column=0, columnspan=4, sticky="w", pady=(10, 0))
        fila += 1

        self.var_cierre = tk.BooleanVar(value=True)
        self.var_num_cierre = tk.StringVar(value=OPCIONES_NUMEROS["mediana"])
        self.var_fechas_cierre = tk.BooleanVar(value=True)
        ttk.Checkbutton(marco, text="Al final, rellenar con la palabra todos los nulos válidos que sobren",
                        variable=self.var_cierre).grid(row=fila, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Combobox(marco, textvariable=self.var_num_cierre, values=list(OPCIONES_NUMEROS.values()),
                     state="readonly", width=32).grid(row=fila, column=2, columnspan=2, sticky="w", pady=(10, 0))
        fila += 1
        ttk.Checkbutton(marco, text="Incluir también las fechas (ya están como texto en un solo formato)",
                        variable=self.var_fechas_cierre).grid(row=fila, column=0, columnspan=4, sticky="w")
        fila += 1

        ttk.Label(marco, text="Vienen sugeridas según el contenido de cada columna; déjelas vacías para no aplicar el paso.",
                  foreground="gray").grid(row=fila, column=0, columnspan=4, sticky="w", pady=(8, 0))
        fila += 1
        ttk.Button(marco, text="↻ Actualizar reglas con esta estandarización",
                   command=lambda: self._ejecutar(self._actualizar_reglas)).grid(row=fila, column=0, columnspan=4,
                                                                                  sticky="w", pady=(12, 0))
        marco.columnconfigure(1, weight=1)

    def _construir_reglas(self) -> None:
        marco = ttk.Frame(self.pestanas, padding=6)
        self.pestanas.add(marco, text="3. Reglas por columna")
        ttk.Label(marco, text=f"Reglas: {', '.join(LG.REGLAS_NULOS)}. «valor» sirve para valor_fijo y no_indica "
                              "(vacío = la palabra de los nulos válidos) y «grupo» para mediana_por_grupo. "
                              "Son sugerencias: puede cambiar la regla de cualquier columna.",
                  wraplength=980, foreground="gray").pack(anchor="w")
        lienzo = tk.Canvas(marco, highlightthickness=0)
        barra = ttk.Scrollbar(marco, orient="vertical", command=lienzo.yview)
        self.marco_reglas = ttk.Frame(lienzo)
        self.marco_reglas.bind("<Configure>", lambda _e: lienzo.configure(scrollregion=lienzo.bbox("all")))
        lienzo.create_window((0, 0), window=self.marco_reglas, anchor="nw")
        lienzo.configure(yscrollcommand=barra.set)
        lienzo.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y")

    def _construir_resultado(self) -> None:
        marco = ttk.Frame(self.pestanas, padding=6)
        self.pestanas.add(marco, text="4. Resultado")
        self.texto_resultado = tk.Text(marco, height=12, state="disabled", wrap="word")
        self.texto_resultado.pack(fill="x")
        self.tabla_resultado = _crear_tabla(marco)
        self.tabla_resultado.master.pack(fill="both", expand=True, pady=(6, 0))

    # ---- carga ---------------------------------------------------------
    def _abrir(self) -> None:
        tabla = _abrir_tabla(self)
        if tabla is not None:
            df, ruta, hoja = tabla
            self._cargar(df, ruta, hoja)

    def _cargar(self, df: pd.DataFrame, nombre: str, hoja: str | None = None) -> None:
        self.df, self.nombre, self.hoja, self.resultado = df, nombre, hoja, None
        self.lbl_archivo.config(text=f"{os.path.basename(nombre)}  ({len(df)} filas × {len(df.columns)} cols)")
        self._marcar_tokens_detectados(df)
        self._diagnosticar()

    def _marcar_tokens_detectados(self, df: pd.DataFrame) -> None:
        """Deja marcados los textos base y los tipo «sin dato» que hay en la tabla (unknown,
        -, not available...), como hace la app web. Se pueden desmarcar si son datos reales."""
        for token, variable in self.var_tokens.items():
            variable.set(token in FG.TOKENS_EXTRA_DEFECTO)
        for texto in LG.detectar_textos_tipo_nulo(df, FG.tokens_nulos(None))["texto"]:
            if texto in self.var_tokens:
                self.var_tokens[texto].set(True)

    # ---- pasos ----------------------------------------------------------
    def _tokens_extra(self) -> list[str]:
        return [t for t, var in self.var_tokens.items() if var.get()]

    def _palabra(self) -> str:
        return self.var_palabra.get().strip() or LG.TEXTO_NO_INDICA

    def _numeros_cierre(self) -> str:
        etiqueta = self.var_num_cierre.get()
        return next((k for k, v in OPCIONES_NUMEROS.items() if v == etiqueta), "mediana")

    def _exigir_tabla(self) -> pd.DataFrame:
        if self.df is None:
            raise ValueError("Primero abra un archivo (o use una tabla de la app).")
        return self.df

    def _sugerir_campos(self) -> None:
        """Llena los campos de estandarizacion con las sugerencias automaticas."""
        sugerida = FG.configurar_limpieza_guiada(self._exigir_tabla(), tokens_extra=self._tokens_extra(),
                                                 nombres_snake=self.var_snake.get())
        self.var_cols_texto.set(", ".join(sugerida["texto"]["columnas"]) if sugerida["texto"] else "")
        self.var_cols_num.set(", ".join(sugerida["numericas"]))
        self.var_lat.set(sugerida["latitud"] or "")
        self.var_lon.set(sugerida["longitud"] or "")
        self.var_cols_fecha.set(", ".join(sugerida["fechas"]["columnas"]) if sugerida["fechas"] else "")

    def _al_cambiar_nombres(self) -> None:
        if self.df is not None:
            self._ejecutar(lambda: (self._sugerir_campos(), self._actualizar_reglas()))

    def _config_actual(self) -> dict:
        return FG.configurar_limpieza_guiada(
            self._exigir_tabla(), tokens_extra=self._tokens_extra(), nombres_snake=self.var_snake.get(),
            vacios_a_nan=self.var_vacios.get(), estandarizar_texto=self.var_texto.get(),
            columnas_texto=_lista(self.var_cols_texto.get()), minusculas=self.var_min.get(),
            espacios=self.var_esp.get(), comillas=self.var_com.get(),
            numericas=_lista(self.var_cols_num.get()), latitud=self.var_lat.get().strip(),
            longitud=self.var_lon.get().strip(), fechas=_lista(self.var_cols_fecha.get()),
            formato_fecha=LG.FORMATOS_FECHA[self.var_formato_fecha.get()],
            dia_primero=self.var_dia_primero.get())

    def _diagnosticar(self) -> None:
        df = self._exigir_tabla()
        diagnostico = LG.diagnostico_nulos(df, FG.tokens_nulos(self._tokens_extra())).reset_index()
        _llenar_tabla(self.tabla_diag, diagnostico)
        self._sugerir_campos()
        self._actualizar_reglas()
        self.pestanas.select(0)

    def _actualizar_reglas(self) -> None:
        """Arma la fila de regla de cada columna con nulos, con la regla sugerida
        (sobre la tabla ya estandarizada y con los valores equivalentes unidos)."""
        self.config = self._config_actual()
        tokens, palabra = self.config["tokens"], self._palabra()
        df_base, _ = LG.ejecutar_pasos_globales(self.df, self.config)
        if self.var_unir.get():
            equivalencias = LG.sugerir_equivalencias(df_base, None, palabra, tokens).to_dict("records")
            df_base, _ = LG.paso_unificar(df_base, equivalencias)
        for widget in self.marco_reglas.winfo_children():
            widget.destroy()
        self.editores = {}
        encabezados = ["Columna", "Nulos", "Regla", "Valor (valor_fijo / no_indica)", "Grupo (mediana_por_grupo)"]
        for j, texto in enumerate(encabezados):
            ttk.Label(self.marco_reglas, text=texto, font=("TkDefaultFont", 9, "bold")).grid(
                row=0, column=j, sticky="w", padx=6, pady=(0, 4))
        fila = 1
        for r in FG.reglas_sugeridas(df_base, tokens, palabra):
            if not r["nulos"]:
                continue
            regla, valor, grupo = tk.StringVar(value=r["regla"]), tk.StringVar(value=str(r["valor"])), tk.StringVar(value=r["grupo"] or "")
            self.editores[r["columna"]] = (regla, valor, grupo)
            ttk.Label(self.marco_reglas, text=f"{r['columna']}  ({r['rol']})").grid(row=fila, column=0, sticky="w", padx=6, pady=2)
            ttk.Label(self.marco_reglas, text=f"{r['nulos']}  ({r['%']}%)").grid(row=fila, column=1, sticky="w", padx=6)
            ttk.Combobox(self.marco_reglas, textvariable=regla, values=list(LG.REGLAS_NULOS), state="readonly",
                         width=18).grid(row=fila, column=2, padx=6)
            ttk.Entry(self.marco_reglas, textvariable=valor, width=22).grid(row=fila, column=3, padx=6)
            ttk.Combobox(self.marco_reglas, textvariable=grupo, values=[""] + list(df_base.columns), state="readonly",
                         width=22).grid(row=fila, column=4, padx=6)
            fila += 1
        if not self.editores:
            ttk.Label(self.marco_reglas, text="No hay columnas con nulos.").grid(row=1, column=0, sticky="w", padx=6)

    def _aplicar(self) -> None:
        self._exigir_tabla()
        ajustes = {col: {"regla": regla.get(),
                         "valor": valor.get() if regla.get() in ("valor_fijo", "no_indica") else "",
                         "grupo": grupo.get() if regla.get() == "mediana_por_grupo" else ""}
                   for col, (regla, valor, grupo) in self.editores.items()}
        self.resultado = FG.ejecutar_limpieza_guiada(
            self.df, self._config_actual(), ajustes, self.nombre, self.hoja, palabra=self._palabra(),
            unir_equivalentes=self.var_unir.get(), asegurar_sin_vacios=self.var_cierre.get(),
            numeros_cierre=self._numeros_cierre(), incluir_fechas_cierre=self.var_fechas_cierre.get())
        aud = self.resultado.auditoria
        lineas = [f"• {paso.titulo}: {paso.detalle}" for paso in self.resultado.pasos]
        lineas += ["", f"Filas: {aud['filas_antes']} → {aud['filas_despues']}   ·   "
                       f"Celdas nulas: {aud['nulos_antes']} → {aud['nulos_despues']}   ·   "
                       f"Duplicados: {aud['duplicados_despues']}"]
        if len(aud["nulos_restantes"]):
            lineas.append("Nulos que quedan (puede ser a propósito, por ejemplo fechas): "
                          + ", ".join(f"{c} ({int(t)})" for c, t in aud["nulos_restantes"]["total"].items()))
        if self.resultado.avisos:
            lineas += ["", "Conviene revisar:"] + [f"⚠ {aviso}" for aviso in self.resultado.avisos]
        _escribir_texto(self.texto_resultado, "\n".join(lineas))
        _llenar_tabla(self.tabla_resultado, self.resultado.df)
        self.pestanas.select(3)
        self.app.tabla_guiada = (self.resultado.df, f"{FG.nombre_base(self.nombre)}_limpio.csv")

    def _exigir_resultado(self) -> FG.ResultadoLimpiezaGuiada:
        if self.resultado is None:
            raise ValueError("Primero aplique la limpieza.")
        return self.resultado

    def _guardar(self) -> None:
        resultado = self._exigir_resultado()
        carpeta = filedialog.askdirectory(parent=self, title="Carpeta donde guardar")
        if not carpeta:
            return
        rutas = FG.guardar_limpieza_guiada(resultado, carpeta, self.var_formato.get())
        messagebox.showinfo("Guardado", "\n".join(f"{t}: {r}" for t, r in rutas.items()), parent=self)

    def _enviar(self) -> None:
        resultado = self._exigir_resultado()
        self.app.enviar_a_limpieza_clasica(resultado.df, f"{FG.nombre_base(self.nombre)}_limpio.csv")


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------

class VentanaMerge(_VentanaBase):
    def __init__(self, app):
        super().__init__(app, "Merge: unir dos tablas", "1100x800")
        self.tablas = {"a": None, "b": None}  # (df, nombre, hoja)
        self.resultado: FG.ResultadoMerge | None = None
        self._construir()

    def _construir(self) -> None:
        marco_tablas = ttk.Frame(self, padding=8)
        marco_tablas.pack(fill="x")
        self.lbl = {}
        for lado, titulo in (("a", "Tabla A (la que manda)"), ("b", "Tabla B (la que enriquece)")):
            caja = ttk.LabelFrame(marco_tablas, text=titulo, padding=6)
            caja.pack(side="left", fill="x", expand=True, padx=4)
            ttk.Button(caja, text="📂 Abrir...", command=lambda l=lado: self._ejecutar(lambda: self._abrir(l))).pack(side="left")
            boton = ttk.Button(caja, text="Usar tabla de la app ▾")
            boton.configure(command=lambda b=boton, l=lado: self._menu_origenes(
                b, lambda df, nombre: self._poner(l, df, nombre, None)))
            boton.pack(side="left", padx=6)
            self.lbl[lado] = ttk.Label(caja, text="(sin tabla)")
            self.lbl[lado].pack(side="left", padx=6)

        marco_llaves = ttk.LabelFrame(self, text="Columnas llave (elija la misma cantidad en A y en B, en el mismo orden)", padding=6)
        marco_llaves.pack(fill="x", padx=8, pady=4)
        self.lista_a = tk.Listbox(marco_llaves, selectmode="multiple", exportselection=False, height=6, width=34)
        self.lista_b = tk.Listbox(marco_llaves, selectmode="multiple", exportselection=False, height=6, width=34)
        ttk.Label(marco_llaves, text="Llave en A").grid(row=0, column=0, sticky="w")
        ttk.Label(marco_llaves, text="Llave en B").grid(row=0, column=1, sticky="w")
        self.lista_a.grid(row=1, column=0, padx=(0, 8))
        self.lista_b.grid(row=1, column=1, padx=(0, 8))
        opciones = ttk.Frame(marco_llaves)
        opciones.grid(row=1, column=2, sticky="n", padx=10)
        self.var_modo = tk.StringVar(value="texto")
        ttk.Label(opciones, text="Comparar las llaves como").grid(row=0, column=0, sticky="w")
        ttk.Combobox(opciones, textvariable=self.var_modo, values=list(MT.MODOS_LLAVE), state="readonly", width=14).grid(row=0, column=1, padx=6)
        self.var_ancho = tk.IntVar(value=0)
        ttk.Label(opciones, text="Ceros a la izquierda hasta (modo codigo)").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Spinbox(opciones, from_=0, to=20, textvariable=self.var_ancho, width=5).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Button(opciones, text="🔍 Revisar llaves", command=lambda: self._ejecutar(self._revisar)).grid(row=2, column=0, columnspan=2, sticky="w", pady=4)
        self.texto_diag = tk.Text(marco_llaves, height=6, width=60, state="disabled", wrap="word")
        self.texto_diag.grid(row=1, column=3, sticky="nsew")
        marco_llaves.columnconfigure(3, weight=1)

        marco_opc = ttk.LabelFrame(self, text="Cómo unir", padding=6)
        marco_opc.pack(fill="x", padx=8, pady=4)
        self.var_union, self.var_validar = tk.StringVar(value="left"), tk.StringVar(value="auto")
        self.var_prefijo, self.var_rellenos = tk.StringVar(), tk.StringVar()
        self.var_colapsar, self.var_indicador = tk.BooleanVar(value=True), tk.BooleanVar(value=False)
        ttk.Label(marco_opc, text="Tabla que manda").grid(row=0, column=0, sticky="w")
        combo_union = ttk.Combobox(marco_opc, textvariable=self.var_union, values=list(MT.TIPOS_UNION), state="readonly", width=10)
        combo_union.grid(row=0, column=1, padx=6)
        self.lbl_union = ttk.Label(marco_opc, text=MT.TIPOS_UNION["left"], foreground="gray")
        self.lbl_union.grid(row=0, column=2, columnspan=3, sticky="w")
        combo_union.bind("<<ComboboxSelected>>", lambda _e: self.lbl_union.config(text=MT.TIPOS_UNION[self.var_union.get()]))
        ttk.Label(marco_opc, text="Validar relación").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(marco_opc, textvariable=self.var_validar, state="readonly", width=14,
                     values=["auto", "sin validar"] + [k for k in MT.VALIDACIONES if k]).grid(row=1, column=1, padx=6)
        ttk.Label(marco_opc, text="Prefijo de las columnas de B").grid(row=1, column=2, sticky="e")
        ttk.Entry(marco_opc, textvariable=self.var_prefijo, width=16).grid(row=1, column=3, padx=6)
        ttk.Checkbutton(marco_opc, text="Si B repite llaves, dejar una fila por llave", variable=self.var_colapsar).grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(marco_opc, text="Conservar la columna _merge", variable=self.var_indicador).grid(row=2, column=3, columnspan=2, sticky="w")
        ttk.Label(marco_opc, text="Plan B (destino=respaldo, ...)").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(marco_opc, textvariable=self.var_rellenos, width=60).grid(row=3, column=1, columnspan=4, sticky="we", padx=6)

        barra = ttk.Frame(self, padding=(8, 4))
        barra.pack(fill="x")
        ttk.Button(barra, text="🔗 Unir tablas", command=lambda: self._ejecutar(self._unir)).pack(side="left")
        self.var_formato = tk.StringVar(value="csv")
        ttk.Combobox(barra, textvariable=self.var_formato, values=FORMATOS_SALIDA, width=7, state="readonly").pack(side="left", padx=(14, 4))
        ttk.Button(barra, text="💾 Guardar en carpeta...", command=lambda: self._ejecutar(self._guardar)).pack(side="left")
        ttk.Button(barra, text="➡️ Enviar a limpieza clásica", command=lambda: self._ejecutar(self._enviar)).pack(side="left", padx=10)

        self.texto_resultado = tk.Text(self, height=6, state="disabled", wrap="word")
        self.texto_resultado.pack(fill="x", padx=8)
        self.tabla_resultado = _crear_tabla(self)
        self.tabla_resultado.master.pack(fill="both", expand=True, padx=8, pady=8)

    # ---- tablas y llaves -----------------------------------------------
    def _abrir(self, lado: str) -> None:
        tabla = _abrir_tabla(self)
        if tabla is not None:
            df, ruta, hoja = tabla
            self._poner(lado, df, ruta, hoja)

    def _poner(self, lado: str, df: pd.DataFrame, nombre: str, hoja: str | None) -> None:
        self.tablas[lado] = (df, nombre, hoja)
        self.lbl[lado].config(text=f"{os.path.basename(nombre)}  ({len(df)} filas × {len(df.columns)} cols)")
        lista = self.lista_a if lado == "a" else self.lista_b
        lista.delete(0, "end")
        for columna in df.columns:
            lista.insert("end", columna)
        self.resultado = None
        if self.tablas["a"] and self.tablas["b"]:
            claves_a, claves_b = FG.llaves_sugeridas(self.tablas["a"][0], self.tablas["b"][0])
            _marcar(self.lista_a, claves_a)
            _marcar(self.lista_b, claves_b)

    def _exigir_tablas(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        if not (self.tablas["a"] and self.tablas["b"]):
            raise ValueError("Cargue la tabla A y la tabla B.")
        return self.tablas["a"][0], self.tablas["b"][0]

    def _revisar(self) -> None:
        df_a, df_b = self._exigir_tablas()
        _, _, d = FG.diagnosticar_llaves(df_a, df_b, _seleccion(self.lista_a), _seleccion(self.lista_b),
                                         self.var_modo.get(), self.var_ancho.get())
        lineas = [f"Filas de A con pareja en B: {d['semaforo']} {d['pct_filas_con_pareja']}%",
                  f"Llaves repetidas en A / B: {d['repetidas_a']} / {d['repetidas_b']}",
                  f"Filas sin llave en A / B: {d['nulos_a']} / {d['nulos_b']}",
                  f"Relación entre las tablas: {d['cardinalidad']}"]
        if d["repetidas_b"]:
            lineas.append("⚠ B repite llaves: con «una fila por llave» marcado no se multiplican las filas de A.")
        if len(d["ejemplos_sin_pareja"]):
            lineas.append("Llaves de A sin pareja: " + ", ".join(map(str, d["ejemplos_sin_pareja"].iloc[:, 0].head(8))))
        _escribir_texto(self.texto_diag, "\n".join(lineas))

    # ---- union ------------------------------------------------------------
    def _unir(self) -> None:
        df_a, df_b = self._exigir_tablas()
        _, nombre_a, hoja_a = self.tablas["a"]
        _, nombre_b, hoja_b = self.tablas["b"]
        rellenos = list(FG.parsear_pares(_lista(self.var_rellenos.get()), "Plan B").items())
        validar = "" if self.var_validar.get() == "sin validar" else self.var_validar.get()
        self.resultado = FG.ejecutar_merge(
            df_a, df_b, nombre_a, nombre_b, _seleccion(self.lista_a), _seleccion(self.lista_b),
            how=self.var_union.get(), validate=validar, modo=self.var_modo.get(), ancho=self.var_ancho.get(),
            prefijo_b=self.var_prefijo.get().strip(), colapsar_b=self.var_colapsar.get(),
            conservar_indicador=self.var_indicador.get(), rellenos=rellenos, hoja_a=hoja_a, hoja_b=hoja_b)
        aud = self.resultado.auditoria
        lineas = [f"Filas de A: {aud['filas_a']}   ·   Filas de B: {aud['filas_b']}   ·   "
                  f"Filas del resultado: {len(self.resultado.df)} ({len(self.resultado.df) - aud['filas_a']:+d} vs A)",
                  f"A con pareja en B: {aud['semaforo']} {aud['pct_filas_con_pareja']}%"]
        if aud["conteo_merge"]:
            lineas.append("Conteo del cruce: " + "  ·  ".join(f"{k}: {v}" for k, v in aud["conteo_merge"].items()))
        lineas += [f"⚠ {aviso}" for aviso in aud["advertencias"]]
        _escribir_texto(self.texto_resultado, "\n".join(lineas))
        _llenar_tabla(self.tabla_resultado, self.resultado.df)
        self.app.tabla_merge = (self.resultado.df, "resultado_merge.csv")

    def _exigir_resultado(self) -> FG.ResultadoMerge:
        if self.resultado is None:
            raise ValueError("Primero una las tablas.")
        return self.resultado

    def _guardar(self) -> None:
        resultado = self._exigir_resultado()
        carpeta = filedialog.askdirectory(parent=self, title="Carpeta donde guardar")
        if not carpeta:
            return
        rutas = FG.guardar_merge(resultado, carpeta, formato=self.var_formato.get())
        messagebox.showinfo("Guardado", "\n".join(f"{t}: {r}" for t, r in rutas.items()), parent=self)

    def _enviar(self) -> None:
        self.app.enviar_a_limpieza_clasica(self._exigir_resultado().df, "resultado_merge.csv")


# --------------------------------------------------------------------------
# Diccionario de datos
# --------------------------------------------------------------------------

class VentanaDiccionario(_VentanaBase):
    def __init__(self, app):
        super().__init__(app, "Diccionario de datos", "1000x680")
        self.df: pd.DataFrame | None = None
        self.nombre = ""
        self.diccionario: pd.DataFrame | None = None
        self.resumen: pd.DataFrame | None = None
        self.excel: bytes | None = None
        self._construir()

    def _construir(self) -> None:
        arriba = ttk.Frame(self, padding=8)
        arriba.pack(fill="x")
        ttk.Button(arriba, text="📂 Abrir archivo...", command=lambda: self._ejecutar(self._abrir)).pack(side="left")
        self.btn_origen = ttk.Button(arriba, text="Usar tabla de la app ▾",
                                     command=lambda: self._menu_origenes(self.btn_origen, self._cargar))
        self.btn_origen.pack(side="left", padx=6)
        self.lbl_archivo = ttk.Label(arriba, text="Ninguna tabla cargada.")
        self.lbl_archivo.pack(side="left", padx=10)

        medio = ttk.Frame(self, padding=(8, 0))
        medio.pack(fill="x")
        ttk.Label(medio, text="Nombre de la tabla maestra").pack(side="left")
        self.var_nombre = tk.StringVar()
        ttk.Entry(medio, textvariable=self.var_nombre, width=30).pack(side="left", padx=6)
        ttk.Label(medio, text="Proyecto").pack(side="left", padx=(10, 0))
        self.var_proyecto = tk.StringVar()
        ttk.Entry(medio, textvariable=self.var_proyecto, width=24).pack(side="left", padx=6)
        ttk.Label(medio, text="Autor(a)").pack(side="left")
        self.var_autor = tk.StringVar()
        ttk.Entry(medio, textvariable=self.var_autor, width=18).pack(side="left", padx=6)
        ttk.Button(medio, text="📘 Generar diccionario", command=lambda: self._ejecutar(self._generar)).pack(side="left", padx=6)

        botones = ttk.Frame(self, padding=(8, 8, 8, 0))
        botones.pack(fill="x")
        ttk.Button(botones, text="💾 Técnico (Excel)...",
                   command=lambda: self._ejecutar(self._guardar_tecnico)).pack(side="left")
        ttk.Button(botones, text="💾 Documento de alcance (Word)...",
                   command=lambda: self._ejecutar(self._guardar_alcance)).pack(side="left", padx=8)
        ttk.Button(botones, text="💾 Excel básico...",
                   command=lambda: self._ejecutar(self._guardar_basico)).pack(side="left")
        ttk.Label(self, text="Tipo, completitud, valores únicos y rango salen de los datos y la descripción se "
                             "redacta sola (puede reescribirla en el Excel básico); la justificación de negocio y la "
                             "clasificación ejecutiva (KPI, variable transformada o llave) quedan en blanco para completarlas. El documento de alcance enlaza al "
                             f"técnico: guarde los dos en la misma carpeta (el técnico se llama «{DD.NOMBRE_TECNICO}»).",
                  foreground="gray", wraplength=960).pack(anchor="w", padx=8, pady=6)
        self.tabla = _crear_tabla(self)
        self.tabla.master.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def _abrir(self) -> None:
        tabla = _abrir_tabla(self)
        if tabla is not None:
            self._cargar(tabla[0], tabla[1])

    def _cargar(self, df: pd.DataFrame, nombre: str) -> None:
        self.df, self.nombre, self.excel = df, nombre, None
        self.diccionario = self.resumen = None
        self.var_nombre.set(FG.nombre_base(nombre))
        self.lbl_archivo.config(text=f"{os.path.basename(nombre)}  ({len(df)} filas × {len(df.columns)} cols)")
        self._generar()

    def _nombre_tabla(self) -> str:
        return self.var_nombre.get().strip() or FG.nombre_base(self.nombre)

    def _generar(self) -> None:
        if self.df is None:
            raise ValueError("Primero abra un archivo (o use una tabla de la app).")
        self.diccionario, self.resumen, self.excel = FG.generar_diccionario(
            self.df, self._nombre_tabla(), fuentes=[self.nombre])
        _llenar_tabla(self.tabla, self.diccionario)

    def _asegurar_generado(self) -> None:
        if self.excel is None:
            self._generar()

    def _guardar_bytes(self, contenido: bytes, extension: str, tipo: str, nombre_inicial: str) -> None:
        ruta = filedialog.asksaveasfilename(
            parent=self, defaultextension=extension, filetypes=[(tipo, f"*{extension}")], initialfile=nombre_inicial)
        if not ruta:
            return
        with open(ruta, "wb") as archivo:
            archivo.write(contenido)
        messagebox.showinfo("Guardado", f"Archivo guardado en:\n{ruta}", parent=self)

    def _guardar_basico(self) -> None:
        self._asegurar_generado()
        self._guardar_bytes(self.excel, ".xlsx", "Excel", f"diccionario_{FG.nombre_base(self._nombre_tabla())}.xlsx")

    def _guardar_tecnico(self) -> None:
        self._asegurar_generado()
        contenido = FG.generar_diccionario_tecnico(self.df, self.diccionario, self.resumen)
        self._guardar_bytes(contenido, ".xlsx", "Excel", DD.NOMBRE_TECNICO)

    def _guardar_alcance(self) -> None:
        self._asegurar_generado()
        textos = {"proyecto": self.var_proyecto.get(), "autor": self.var_autor.get()}
        try:
            contenido = FG.generar_documento_alcance(
                self.df, self.diccionario, self._nombre_tabla(), [self.nombre], textos=textos)
        except ImportError:
            raise ValueError("Para generar el documento de alcance instale python-docx: pip install python-docx")
        self._guardar_bytes(contenido, ".docx", "Word",
                            f"documento_alcance_{FG.nombre_base(self._nombre_tabla())}.docx")

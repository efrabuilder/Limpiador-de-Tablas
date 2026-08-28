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
from data_cleaner.exportador import (
    generar_script_powerbi, generar_script_universal, generar_editor_m,
)

OPCIONES_ACCION = {
    "faltante": ["reemplazar_mediana", "reemplazar_media", "reemplazar_moda",
                 "valor_fijo", "eliminar_fila", "marcar_solo"],
    "duplicado": ["eliminar_fila", "marcar_solo"],
    "atipico": ["limitar", "reemplazar_mediana", "reemplazar_media",
                "eliminar_fila", "marcar_solo"],
    "tipo_invalido": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "fecha_invalida": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "email_invalido": ["eliminar_fila", "valor_fijo", "marcar_solo"],
    "telefono_invalido": ["eliminar_fila", "valor_fijo", "marcar_solo"],
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

        self._construir_layout()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _construir_layout(self) -> None:
        barra = ttk.Frame(self, padding=10)
        barra.pack(fill="x")

        ttk.Button(barra, text="📂 Abrir archivo (CSV/Excel)", command=self.abrir_archivo).pack(side="left")
        self.lbl_archivo = ttk.Label(barra, text="Ningún archivo cargado.")
        self.lbl_archivo.pack(side="left", padx=10)

        ttk.Label(barra, text="Método atípicos:").pack(side="left", padx=(20, 4))
        self.metodo_var = tk.StringVar(value="iqr")
        ttk.Combobox(barra, textvariable=self.metodo_var, values=["iqr", "zscore", "ambos"],
                     width=8, state="readonly").pack(side="left")

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
    def abrir_archivo(self) -> None:
        ruta = filedialog.askopenfilename(
            title="Seleccionar archivo",
            filetypes=[("CSV / Excel", "*.csv *.xlsx *.xls"), ("Todos", "*.*")],
        )
        if not ruta:
            return
        try:
            df = load_table(ruta, kind="auto")
        except Exception as exc:
            messagebox.showerror("Error al cargar", str(exc))
            return

        self.df = df
        self.ruta_actual = ruta
        self.resultado = None
        self.df_limpio = None
        self.lbl_archivo.config(text=f"{os.path.basename(ruta)}  ({len(df)} filas × {len(df.columns)} cols)")
        self._llenar_tabla(self.tabla_datos, df)
        for widget in self.marco_tipos.winfo_children():
            widget.destroy()
        self.status_var.set("Archivo cargado. Presione Analizar.")

    def analizar_tabla(self) -> None:
        if self.df is None:
            messagebox.showwarning("Sin datos", "Primero cargue un archivo.")
            return
        self.status_var.set("Analizando...")
        self.update_idletasks()

        self.resultado = analizar(self.df, metodo_atipicos=self.metodo_var.get())
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

            var.trace_add("write", _actualizar_visibilidad)
            _actualizar_visibilidad()

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
            self.df, self.resultado.issues, config=config, valores_fijos=valores_fijos
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
        ventana.geometry("420x200")
        ventana.transient(self)
        ventana.grab_set()

        ttk.Label(
            ventana,
            text="Elija qué generar (usa la configuración de limpieza ya aplicada):",
            wraplength=380, justify="left",
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

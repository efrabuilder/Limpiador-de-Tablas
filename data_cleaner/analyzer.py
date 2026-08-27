"""
Analizador de tablas: detecta valores nulos, duplicados, atípicos y errores
de tipo/formato. Devuelve una lista de "hallazgos" (issues) estandarizada
que luego usan cleaner.py y report.py.
"""
from __future__ import annotations
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class Issue:
    tipo: str            # 'faltante' | 'duplicado' | 'atipico' | 'tipo_invalido'
    columna: Optional[str]
    fila: int             # índice original del DataFrame (0-based)
    valor_original: Any
    detalle: str = ""


@dataclass
class AnalysisResult:
    filas_analizadas: int
    columnas_analizadas: int
    issues: List[Issue] = field(default_factory=list)

    def por_tipo(self) -> dict:
        resumen = {}
        for issue in self.issues:
            resumen[issue.tipo] = resumen.get(issue.tipo, 0) + 1
        return resumen

    def por_columna(self) -> dict:
        resumen = {}
        for issue in self.issues:
            key = issue.columna or "(fila completa)"
            resumen[key] = resumen.get(key, 0) + 1
        return resumen


def _es_columna_numerica_potencial(serie: pd.Series) -> bool:
    """Determina si una columna 'object' en realidad contiene números como texto."""
    convertibles = pd.to_numeric(serie.dropna(), errors="coerce")
    return convertibles.notna().mean() > 0.7 if len(serie.dropna()) else False


def detectar_faltantes(df: pd.DataFrame) -> List[Issue]:
    issues = []
    for col in df.columns:
        nulos = df[df[col].isna()]
        for idx in nulos.index:
            issues.append(Issue("faltante", col, int(idx), None, "Valor vacío/nulo"))
    return issues


def detectar_duplicados(df: pd.DataFrame) -> List[Issue]:
    issues = []
    mask = df.duplicated(keep="first")
    for idx in df[mask].index:
        issues.append(Issue("duplicado", None, int(idx), df.loc[idx].to_dict(),
                             "Fila duplicada (idéntica a una anterior)"))
    return issues


def detectar_tipo_invalido(df: pd.DataFrame) -> List[Issue]:
    """Detecta celdas de texto no numérico dentro de columnas mayormente numéricas."""
    issues = []
    for col in df.columns:
        serie = df[col]
        es_texto = pd.api.types.is_object_dtype(serie) or pd.api.types.is_string_dtype(serie)
        if es_texto and _es_columna_numerica_potencial(serie):
            convertidos = pd.to_numeric(serie, errors="coerce")
            malos = serie[(convertidos.isna()) & (serie.notna())]
            for idx, val in malos.items():
                issues.append(Issue("tipo_invalido", col, int(idx), val,
                                     "Se esperaba un valor numérico"))
    return issues


def _columnas_numericas_o_potenciales(df: pd.DataFrame) -> List[str]:
    """Columnas numéricas reales + columnas de texto que son mayormente numéricas."""
    cols = df.select_dtypes(include=[np.number]).columns.tolist()
    for col in df.columns:
        if col in cols:
            continue
        serie = df[col]
        es_texto = pd.api.types.is_object_dtype(serie) or pd.api.types.is_string_dtype(serie)
        if es_texto and _es_columna_numerica_potencial(serie):
            cols.append(col)
    return cols


def detectar_atipicos_iqr(df: pd.DataFrame, factor: float = 1.5,
                           columnas: Optional[List[str]] = None) -> List[Issue]:
    """Detecta valores atípicos usando el método de rango intercuartílico (IQR)."""
    issues = []
    columnas = columnas or _columnas_numericas_o_potenciales(df)
    for col in columnas:
        if col not in df.columns:
            continue
        serie = pd.to_numeric(df[col], errors="coerce")
        q1, q3 = serie.quantile(0.25), serie.quantile(0.75)
        iqr = q3 - q1
        if iqr == 0 or pd.isna(iqr):
            continue
        lim_inf, lim_sup = q1 - factor * iqr, q3 + factor * iqr
        atipicos = serie[(serie < lim_inf) | (serie > lim_sup)]
        for idx, val in atipicos.items():
            issues.append(Issue(
                "atipico", col, int(idx), val,
                f"Fuera de rango [{lim_inf:.2f}, {lim_sup:.2f}] (método IQR)"
            ))
    return issues


def detectar_atipicos_zscore(df: pd.DataFrame, umbral: float = 3.0,
                              columnas: Optional[List[str]] = None) -> List[Issue]:
    """Detecta valores atípicos usando puntuación Z (desviaciones estándar)."""
    issues = []
    columnas = columnas or _columnas_numericas_o_potenciales(df)
    for col in columnas:
        if col not in df.columns:
            continue
        serie = pd.to_numeric(df[col], errors="coerce")
        media, std = serie.mean(), serie.std()
        if std == 0 or pd.isna(std):
            continue
        z = (serie - media) / std
        atipicos = serie[z.abs() > umbral]
        for idx, val in atipicos.items():
            issues.append(Issue(
                "atipico", col, int(idx), val,
                f"Z-score={((val - media) / std):.2f} (umbral={umbral})"
            ))
    return issues


def analizar(df: pd.DataFrame, metodo_atipicos: str = "iqr",
             factor_iqr: float = 1.5, umbral_zscore: float = 3.0,
             columnas_numericas: Optional[List[str]] = None) -> AnalysisResult:
    """Ejecuta todas las detecciones y consolida los resultados."""
    issues: List[Issue] = []
    issues += detectar_faltantes(df)
    issues += detectar_duplicados(df)
    issues += detectar_tipo_invalido(df)

    if metodo_atipicos == "iqr":
        issues += detectar_atipicos_iqr(df, factor=factor_iqr, columnas=columnas_numericas)
    elif metodo_atipicos == "zscore":
        issues += detectar_atipicos_zscore(df, umbral=umbral_zscore, columnas=columnas_numericas)
    elif metodo_atipicos == "ambos":
        iqr_issues = detectar_atipicos_iqr(df, factor=factor_iqr, columnas=columnas_numericas)
        zscore_issues = detectar_atipicos_zscore(df, umbral=umbral_zscore, columnas=columnas_numericas)
        # Una misma celda puede salir atípica por ambos métodos a la vez: se
        # fusiona en un único hallazgo para no inflar conteos ni aplicar la
        # corrección dos veces sobre el mismo dato.
        combinados: dict[tuple, Issue] = {(i.columna, i.fila): i for i in iqr_issues}
        for issue in zscore_issues:
            clave = (issue.columna, issue.fila)
            if clave in combinados:
                combinados[clave].detalle += f" | también atípico por Z-score ({issue.detalle})"
            else:
                combinados[clave] = issue
        issues += list(combinados.values())

    return AnalysisResult(
        filas_analizadas=len(df),
        columnas_analizadas=len(df.columns),
        issues=issues,
    )

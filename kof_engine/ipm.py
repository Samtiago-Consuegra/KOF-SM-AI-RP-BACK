"""Índice de Prioridad de Mantenimiento (IPM).

Responde "¿a qué máquina le doy prioridad ahora?" con 4 criterios medidos en las últimas
VENTANA_SEMANAS semanas: Frecuencia (% turnos con falla), Tiempo de paro (min/turno),
Recurrencia (% fallas cuya subclave se repitió en ≤ DIAS_RECURRENCIA días operativos) y
Criticidad (escala 1-5). Cada criterio se normaliza contra el máximo de las 4 máquinas (= 100)
y se pondera con PESOS_IPM. El IPM es RELATIVO entre máquinas.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C
from .datos import calendario, posicion_operativa

CRITERIOS = list(C.PESOS_IPM)


def marcar_recurrencia(base: pd.DataFrame) -> pd.DataFrame:
    """Añade `recurrente` (0/1): la misma máquina y subclave falló en los DIAS_RECURRENCIA días previos."""
    b = base.copy()
    b["t_op"], _ = posicion_operativa(b)
    b["orden_hora"] = np.where(b["Hora"] == 23, -1, b["Hora"])     # en T3, las 23 h van antes de las 00-06 h
    b = b.sort_values(["Maquina", "Subclave de Paro", "t_op", "orden_hora"])
    dt = b.groupby(["Maquina", "Subclave de Paro"])["t_op"].diff()
    b["recurrente"] = (dt <= C.DIAS_RECURRENCIA).astype(int)
    return b.sort_index()


def criterios(base: pd.DataFrame, fin: pd.Timestamp | None = None, semanas: int = C.VENTANA_SEMANAS,
              criticidad: dict | None = None) -> pd.DataFrame:
    """Criterios del IPM en su unidad original, por máquina, para la ventana que termina en `fin`."""
    criticidad = criticidad or C.CRITICIDAD
    b = base if "recurrente" in base.columns else marcar_recurrencia(base)
    fin = pd.Timestamp(fin) if fin is not None else b["Fecha"].max()
    ini = fin - pd.Timedelta(weeks=semanas) + pd.Timedelta(days=1)
    cal = calendario(b)
    n_turnos = int(((cal["Fecha"] >= ini) & (cal["Fecha"] <= fin)).sum())
    bw = b[(b["Fecha"] >= ini) & (b["Fecha"] <= fin)]
    filas = []
    for m in C.MAQUINAS:
        bm = bw[bw["Maquina"] == m]
        turnos_falla = bm[["Fecha", "Turno"]].drop_duplicates().shape[0]
        filas.append({"Maquina": m, "Turnos_operativos": n_turnos, "Fallas": len(bm),
                      "Minutos": float(bm["Minutos de paro"].sum()),
                      "Frecuencia": turnos_falla / n_turnos if n_turnos else 0.0,
                      "Tiempo de paro": bm["Minutos de paro"].sum() / n_turnos if n_turnos else 0.0,
                      "Recurrencia": float(bm["recurrente"].mean()) if len(bm) else 0.0,
                      "Criticidad": criticidad[m]})
    df = pd.DataFrame(filas).set_index("Maquina")
    df.attrs["ventana"] = (ini, fin)
    return df


def puntaje(crit: pd.DataFrame, pesos: dict | None = None) -> tuple[pd.DataFrame, pd.Series]:
    """Normaliza los criterios (máximo = 100) y calcula el IPM."""
    pesos = pesos or C.PESOS_IPM
    norm = crit[CRITERIOS].div(crit[CRITERIOS].max().replace(0, np.nan)).fillna(0) * 100
    ipm = sum(pesos[k] * norm[k] for k in CRITERIOS)
    return norm, ipm


def nivel(valor: float) -> str:
    for umbral, nombre in C.NIVELES_IPM:
        if valor >= umbral:
            return nombre
    return "Bajo"


def sensibilidad_ipm(crit: pd.DataFrame, n_simulaciones: int = 10_000, concentracion: float | None = None,
                     semilla: int = C.SEMILLA) -> dict:
    """Monte Carlo sobre los pesos (Dirichlet). Devuelve, por máquina, % de simulaciones en 1.er lugar.

    concentracion=None → pesos totalmente libres (escenario "amplio");
    un número (p. ej. 20) → pesos alrededor de la propuesta (escenario "cercano").
    """
    rng = np.random.default_rng(semilla)
    norm, _ = puntaje(crit)
    alfa = (np.ones(len(CRITERIOS)) if concentracion is None
            else concentracion * np.array([C.PESOS_IPM[k] for k in CRITERIOS]))
    W = rng.dirichlet(alfa, n_simulaciones)
    S = W @ norm[CRITERIOS].values.T
    R = (-S).argsort(axis=1).argsort(axis=1) + 1
    return {m: {"Pct_primer_lugar": round(100 * float((R[:, i] == 1).mean()), 1),
                "Ranking_medio": round(float(R[:, i].mean()), 2),
                "IPM_p5": round(float(np.percentile(S[:, i], 5)), 1),
                "IPM_p95": round(float(np.percentile(S[:, i], 95)), 1)}
            for i, m in enumerate(norm.index)}


def calcular_ipm(base: pd.DataFrame, fin: pd.Timestamp | None = None, con_sensibilidad: bool = True) -> dict:
    """IPM actual (dict serializable a JSON)."""
    b = marcar_recurrencia(base)
    crit = criterios(b, fin)
    norm, ipm = puntaje(crit)
    ini, fin = crit.attrs["ventana"]
    rank = ipm.rank(ascending=False, method="min").astype(int)
    robustez = sensibilidad_ipm(crit, concentracion=20) if con_sensibilidad else {}
    bw = b[(b["Fecha"] >= ini) & (b["Fecha"] <= fin)]
    top = bw.groupby(["Maquina", "Subclave de Paro"])["Minutos de paro"].sum()

    maquinas = []
    for m in ipm.sort_values(ascending=False).index:
        causas = top.loc[m].nlargest(3) if m in top.index.get_level_values(0) else pd.Series(dtype=float)
        maquinas.append({
            "Maquina": m, "IPM": round(float(ipm[m]), 1), "Nivel": nivel(ipm[m]), "Ranking": int(rank[m]),
            "Criterios": {"Frecuencia_%": round(100 * crit.loc[m, "Frecuencia"], 1),
                          "Minutos_por_turno": round(float(crit.loc[m, "Tiempo de paro"]), 2),
                          "Recurrencia_%": round(100 * crit.loc[m, "Recurrencia"], 1),
                          "Criticidad": int(crit.loc[m, "Criticidad"])},
            "Criterios_normalizados": {k: round(float(norm.loc[m, k]), 1) for k in CRITERIOS},
            "Robustez_%_primer_lugar": robustez.get(m, {}).get("Pct_primer_lugar"),
            "Top_causas_min": {k: round(float(v), 1) for k, v in causas.items()}})
    return {"fecha_inicio": str(ini.date()), "fecha_corte": str(fin.date()),
            "ventana_semanas": C.VENTANA_SEMANAS, "turnos_operativos": int(crit["Turnos_operativos"].iloc[0]),
            "pesos": C.PESOS_IPM, "criticidad": C.CRITICIDAD,
            "nota": "Pesos y criticidad: propuesta del proyecto, pendientes de validación con la planta.",
            "maquinas": maquinas}


def historico_ipm(base: pd.DataFrame, desde: str = "2025-01-05") -> pd.DataFrame:
    """IPM semana a semana (ventana móvil) para guardar en la base de datos y graficar su evolución."""
    b = marcar_recurrencia(base)
    fin_max = b["Fecha"].max()
    cortes = pd.date_range(desde, fin_max, freq="W-SUN")
    if len(cortes) == 0 or cortes[-1] != fin_max:
        cortes = cortes.append(pd.DatetimeIndex([fin_max]))
    filas = []
    for fin in cortes:
        crit = criterios(b, fin)
        _, s = puntaje(crit)
        for m in C.MAQUINAS:
            filas.append({"Fecha_corte": fin.date().isoformat(), "Maquina": m, "IPM": round(float(s[m]), 1),
                          "Nivel": nivel(s[m])})
    return pd.DataFrame(filas)

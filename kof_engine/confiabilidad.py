"""Máquinas sin pronóstico por turno.

Envolvedora → Weibull sobre el tiempo entre fallas (días operativos, régimen desde WEIBULL_DESDE):
  P(falla en h días | lleva t sin fallar) = 1 − S(t+h)/S(t), S(x) = exp(−(x/η)^β)
  Tiempo medio residual = ∫_t^∞ S(x)dx / S(t)   (NO es RUL: no hay datos de condición del equipo)
Paletizadora → nivel semanal: fallas esperadas (media 12 semanas) y rango P10–P90 (26 semanas),
  con backtest de cobertura (nominal 80 %).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import integrate, stats

from . import config as C
from .datos import posicion_operativa


def weibull_envolvedora(base: pd.DataFrame, maquina: str = C.MAQUINA_WEIBULL, desde: str = C.WEIBULL_DESDE) -> dict:
    b = base.copy()
    b["t_op"], dias = posicion_operativa(b)
    env = b[(b["Maquina"] == maquina) & (b["Fecha"] >= desde)]
    t = np.sort(env["t_op"].unique())               # varias fallas en un turno = un evento
    tbf = np.diff(t)
    tbf = tbf[tbf > 0]
    if len(tbf) < 10:
        return {"Maquina": maquina, "Disponible": False, "Motivo": f"Solo {len(tbf)} tiempos entre fallas"}
    beta, _, eta = stats.weibull_min.fit(tbf, floc=0)
    ks_p = float(stats.kstest(tbf, "weibull_min", args=(beta, 0, eta)).pvalue)
    t0 = (len(dias) - 1 + 2 / 3) - t[-1]            # días operativos sin falla al cierre de los datos
    S = lambda x: stats.weibull_min.sf(x, beta, 0, eta)
    mrl = integrate.quad(S, t0, np.inf)[0] / S(t0)
    patron = ("fallas casi aleatorias" if 0.85 <= beta <= 1.15
              else "fallas en racimos" if beta < 1 else "desgaste progresivo")
    return {"Maquina": maquina, "Disponible": True,
            "Tipo_pronostico": f"Confiabilidad Weibull (régimen desde {desde})",
            "Ultima_falla": str(env["Fecha"].max().date()),
            "Dias_operativos_sin_falla": round(float(t0), 1),
            "Probabilidad_Falla_%": {f"Proximos_{h}_dias_op": round(100 * float(1 - S(t0 + h) / S(t0)), 1)
                                     for h in C.HORIZONTES_WEIBULL},
            "Tiempo_medio_residual_dias_op": round(float(mrl), 1),
            "Parametros": {"beta": round(float(beta), 3), "eta_dias_op": round(float(eta), 3),
                           "KS_p": round(ks_p, 3), "n_tiempos_entre_fallas": int(len(tbf))},
            "Patron": patron, "Ajuste_aceptable": ks_p > 0.05}


def nivel_semanal(base: pd.DataFrame, maquina: str = C.MAQUINA_NIVEL_SEMANAL) -> dict:
    b = base.assign(Semana=base["Fecha"].dt.to_period("W-SUN").dt.start_time)
    dias_op = b.groupby("Semana")["Fecha"].nunique()
    fallas = b[b["Maquina"] == maquina].groupby("Semana").size().reindex(dias_op.index, fill_value=0)
    w = pd.DataFrame({"fallas": fallas, "dias_op": dias_op})
    w = w[w["dias_op"] >= C.MIN_DIAS_OPERATIVOS_SEMANA]
    n = C.SEMANAS_RANGO_PALETIZADORA
    bt = w.assign(P10=w["fallas"].rolling(n).quantile(0.10).shift(1),
                  P90=w["fallas"].rolling(n).quantile(0.90).shift(1)).dropna(subset=["P10", "P90"])
    cobertura = float(((bt["fallas"] >= bt["P10"]) & (bt["fallas"] <= bt["P90"])).mean()) if len(bt) else None
    ult = w["fallas"].tail(n)
    fin = base["Fecha"].max()
    causas = (base[(base["Maquina"] == maquina) & (base["Fecha"] > fin - pd.Timedelta(weeks=C.VENTANA_SEMANAS))]
              .groupby("Subclave de Paro")["Minutos de paro"].sum().nlargest(3))
    return {"Maquina": maquina, "Tipo_pronostico": "Nivel semanal (sin pronóstico por turno)",
            "Fallas_esperadas_semana": round(float(ult.tail(C.VENTANA_SEMANAS).mean()), 1),
            "Rango_P10_P90": [float(ult.quantile(0.10)), float(ult.quantile(0.90))],
            "Cobertura_backtest_%": None if cobertura is None else round(100 * cobertura, 1),
            "Semanas_backtest": int(len(bt)),
            "Top_causas_12_sem_min": {k: round(float(v), 1) for k, v in causas.items()}}

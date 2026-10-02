"""Pronóstico por turno (Llenadora, Empacadora): probabilidad de falla en el turno siguiente + semáforo.

El color es RELATIVO a las últimas 12 semanas de la máquina: Rojo ≈ el 30 % de turnos más riesgosos.
"""
from __future__ import annotations

import pandas as pd

from . import config as C
from .datos import construir_variables, siguiente_turno

X_COLS = C.VARIABLES_NUM + C.VARIABLES_CAT


def color_semaforo(p: float, info: dict) -> str:
    if p >= info["corte_amarillo_rojo"]:
        return "Rojo"
    if p >= info["corte_verde_amarillo"]:
        return "Amarillo"
    return "Verde"


def pronosticar_turno(base: pd.DataFrame, art: dict, variables: pd.DataFrame | None = None) -> dict:
    """Pronóstico del próximo turno operativo para cada máquina con modelo.

    `variables` permite reutilizar construir_variables(base) si ya se calculó.
    """
    ds = variables if variables is not None else construir_variables(base)
    salida = {}
    for m, info in art["modelos"].items():
        fila = ds[ds["Maquina"] == m].sort_values("id_turno").tail(1).copy()
        fecha, turno = fila["Fecha"].iloc[0], int(fila["Turno"].iloc[0])
        f_sig, t_sig = siguiente_turno(fecha, turno)
        fila["turno_sig"], fila["dow_sig"] = t_sig, f_sig.dayofweek
        fila["reinicio_sig"] = int((f_sig - fecha).days > 1)
        p = float(info["pipeline"].predict_proba(fila[X_COLS])[:, 1][0])
        salida[m] = {
            "Tipo_pronostico": "Próximo turno — Random Forest (semáforo adaptativo)",
            "Ultimo_turno_con_datos": {"Fecha": str(fecha.date()), "Turno": turno},
            "Turno_pronosticado": {"Fecha": str(f_sig.date()), "Turno": int(t_sig)},
            "Probabilidad_Falla_%": round(100 * p, 1),
            "Nivel_Riesgo": color_semaforo(p, info),
            "Cortes_%": {"Verde_hasta": round(100 * info["corte_verde_amarillo"], 1),
                         "Rojo_desde": round(100 * info["corte_amarillo_rojo"], 1)},
            "Tasa_reciente_12_sem_%": round(100 * info["tasa_reciente"], 1),
            "Tasa_historica_%": round(100 * info["tasa_base_historica"], 1),
            "Contexto_ultimo_dia": {"Fallas": int(fila["fallas_ult_3t"].iloc[0]),
                                    "Minutos": round(float(fila["min_ult_3t"].iloc[0]), 1)},
            "AUC_validacion": round(info["auc_oof_total"], 3),
            "_probabilidad": p}                     # valor sin redondear, para recalcular_cortes()
    return salida

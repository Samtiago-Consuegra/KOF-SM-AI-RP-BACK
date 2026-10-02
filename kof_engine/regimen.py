"""Monitoreo de régimen con cartas de control p.

Responde "¿la máquina sigue comportándose como antes?". Por semana: p = turnos con falla / turnos
operativos. Línea central = proporción del periodo de referencia. Límites = binomial EXACTA
equivalente a ±3σ (válida también para la Envolvedora, donde n·p̄ < 1). Reglas:
  R1 un punto fuera de límites · R2 8 semanas seguidas del mismo lado · R3 2 de 3 semanas más allá de 2σ.
La semana en curso (incompleta) se devuelve pero no se usa para declarar el estado.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

from . import config as C
from .datos import construir_serie


def _racha(signos: np.ndarray) -> np.ndarray:
    out, n, prev = [], 0, 0
    for s in signos:
        n = n + 1 if (s == prev and s != 0) else (1 if s != 0 else 0)
        prev = s
        out.append(n)
    return np.array(out)


def carta_p(base: pd.DataFrame) -> pd.DataFrame:
    """Carta p semanal de las 4 máquinas (una fila por máquina y semana)."""
    serie = construir_serie(base, maquinas=C.MAQUINAS)
    serie["Semana"] = serie["Fecha"].dt.to_period("W-SUN").dt.start_time
    fin_datos = base["Fecha"].max()
    cartas = []
    for m in C.MAQUINAS:
        w = (serie[serie["Maquina"] == m].groupby("Semana")
             .agg(x=("falla", "sum"), n=("falla", "size")).reset_index())
        w = w[w["n"] >= C.MIN_TURNOS_SEMANA].copy()
        ref = w[(w["Semana"] >= C.REFERENCIA_INICIO) & (w["Semana"] <= C.REFERENCIA_FIN)]
        if ref.empty:
            raise ValueError(f"Sin semanas en el periodo de referencia para {m}.")
        pbar = ref["x"].sum() / ref["n"].sum()
        w["p"] = w["x"] / w["n"]
        w["pbar"] = pbar
        w["sigma"] = np.sqrt(pbar * (1 - pbar) / w["n"])
        w["LCS"] = stats.binom.ppf(0.99865, w["n"], pbar) / w["n"]
        w["LCI"] = stats.binom.ppf(0.00135, w["n"], pbar) / w["n"]
        w["Completa"] = (w["Semana"] + pd.Timedelta(days=6)) <= fin_datos
        z = (w["p"] - pbar) / w["sigma"]
        signo = np.sign(w["p"] - pbar).astype(int).values
        w["R1"] = (w["p"] > w["LCS"]) | (w["p"] < w["LCI"])
        w["R2"] = _racha(signo) >= C.RACHA_R2
        arriba = (z > 2).astype(int).rolling(3, min_periods=1).sum() >= 2
        abajo = (z < -2).astype(int).rolling(3, min_periods=1).sum() >= 2
        w["R3"] = (arriba & (z > 2)) | (abajo & (z < -2))
        w["Alarma"] = w[["R1", "R2", "R3"]].any(axis=1) & w["Completa"]
        w["Direccion"] = np.where(~w["Alarma"], "", np.where(signo > 0, "Deterioro", "Mejora"))
        w["Maquina"] = m
        cartas.append(w)
    return pd.concat(cartas, ignore_index=True)


def estado_regimen(carta: pd.DataFrame) -> dict:
    """Estado actual por máquina (últimas 4 semanas completas)."""
    estado = {}
    for m in C.MAQUINAS:
        w = carta[carta["Maquina"] == m]
        comp = w[w["Completa"]]
        ult = comp["Semana"].max()
        rec = comp[comp["Semana"] >= ult - pd.Timedelta(weeks=3)]
        if rec["Alarma"].any():
            d = rec.loc[rec["Alarma"], "Direccion"].iloc[-1]
            est, codigo = f"Fuera de control — {d.lower()}", d.upper()
        else:
            est, codigo = "En control (comportamiento habitual)", "EN_CONTROL"
        hist = w[(w["Semana"] > C.REFERENCIA_FIN) & w["Alarma"]]
        estado[m] = {
            "Estado": est, "Codigo": codigo,
            "p_referencia_%": round(100 * float(w["pbar"].iloc[0]), 1),
            "p_ultimas_4_semanas_%": round(100 * rec["x"].sum() / rec["n"].sum(), 1),
            "Ultima_semana_completa": str(ult.date()),
            "Ultima_alarma": None if hist.empty else {
                "Semana": str(hist["Semana"].iloc[-1].date()), "Direccion": hist["Direccion"].iloc[-1],
                "Reglas": [r for r in ("R1", "R2", "R3") if bool(hist[r].iloc[-1])]}}
    return estado


def carta_p_json(carta: pd.DataFrame) -> list[dict]:
    """Serie de la carta p en formato de registros, para que el frontend la grafique."""
    cols = ["Maquina", "Semana", "x", "n", "p", "pbar", "LCS", "LCI", "Completa", "R1", "R2", "R3",
            "Alarma", "Direccion"]
    out = carta[cols].copy()
    out["Semana"] = out["Semana"].dt.date.astype(str)
    for c in ("p", "pbar", "LCS", "LCI"):
        out[c] = out[c].round(4)
    return out.to_dict(orient="records")

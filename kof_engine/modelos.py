"""Entrenamiento del pronóstico por turno (Random Forest) y cortes del semáforo adaptativo.

Cuándo reentrenar: con cada carga semanal de SAP, o de inmediato si la carta p de una máquina
con modelo (Llenadora, Empacadora) marca "Fuera de control".
El artefacto se guarda con joblib; debe cargarse con la MISMA versión de scikit-learn con la que
se entrenó (por eso se entrena en el propio entorno del backend, no se copia un .joblib ajeno).
"""
from __future__ import annotations

import warnings
from datetime import datetime

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from . import config as C
from .datos import construir_variables, preparar_base

X_COLS = C.VARIABLES_NUM + C.VARIABLES_CAT


def nuevo_modelo() -> Pipeline:
    pre = ColumnTransformer([
        ("num", SimpleImputer(strategy="median"), C.VARIABLES_NUM),
        ("cat", OneHotEncoder(handle_unknown="ignore", min_frequency=20), C.VARIABLES_CAT)])
    return Pipeline([("pre", pre), ("m", RandomForestClassifier(**C.RF_PARAMS))])


def cortes_semaforo(predicciones: np.ndarray) -> tuple[float, float]:
    """Percentiles del semáforo a partir de predicciones FUERA DE MUESTRA recientes."""
    p = np.asarray(predicciones, dtype=float)
    p = p[~np.isnan(p)]
    return float(np.quantile(p, C.PERCENTIL_VERDE)), float(np.quantile(p, C.PERCENTIL_ROJO))


def entrenar(df_sap: pd.DataFrame, ya_preparado: bool = False) -> dict:
    """Entrena los modelos de MAQUINAS_ML con todo el histórico y calcula el semáforo adaptativo.

    Devuelve un artefacto (dict) con los pipelines, los cortes y métricas de validación fuera de muestra.
    """
    base = df_sap if ya_preparado else preparar_base(df_sap)
    ds = construir_variables(base).dropna(subset=[C.TARGET])
    ds[C.TARGET] = ds[C.TARGET].astype(int)
    fin = base["Fecha"].max()
    art = {"version_motor": "2.0.0", "sklearn": sklearn.__version__,
           "entrenado": datetime.now().isoformat(timespec="seconds"),
           "datos_hasta": str(fin.date()), "variables": X_COLS, "modelos": {}}

    for m in C.MAQUINAS_ML:
        g = ds[ds["Maquina"] == m].sort_values("id_turno").reset_index(drop=True)
        X, y = g[X_COLS], g[C.TARGET]
        oof = np.full(len(g), np.nan)
        for a, b in TimeSeriesSplit(n_splits=C.CV_SPLITS, gap=1).split(X):
            oof[b] = nuevo_modelo().fit(X.iloc[a], y.iloc[a]).predict_proba(X.iloc[b])[:, 1]
        modelo = nuevo_modelo().fit(X, y)

        recientes = (g["Fecha"] > fin - pd.Timedelta(weeks=C.VENTANA_SEMANAS)) & ~np.isnan(oof)
        c_verde, c_rojo = cortes_semaforo(oof[recientes])
        v = ~np.isnan(oof)
        art["modelos"][m] = {
            "pipeline": modelo,
            "corte_verde_amarillo": c_verde, "corte_amarillo_rojo": c_rojo,
            "predicciones_recientes": [round(float(x), 4) for x in oof[recientes]],
            "tasa_base_historica": float(y.mean()),
            "tasa_reciente": float(y[recientes].mean()),
            "auc_oof_total": float(roc_auc_score(y[v], oof[v])),
            "auc_oof_reciente": float(roc_auc_score(y[recientes], oof[recientes]))
                                if y[recientes].nunique() > 1 else None,
            "n_turnos_entrenamiento": int(len(g))}
    return art


def recalcular_cortes(art: dict, maquina: str, nuevas_predicciones: list[float],
                      max_buffer: int | None = None) -> dict:
    """Actualiza el semáforo adaptativo SIN reentrenar, añadiendo predicciones hechas sobre turnos nuevos.

    Útil entre reentrenamientos: cada pronóstico emitido se agrega al buffer y los cortes se recalculan
    con las ~12 semanas más recientes (≈ 17 turnos/semana).
    """
    info = art["modelos"][maquina]
    max_buffer = max_buffer or C.VENTANA_SEMANAS * 17
    buffer = (info["predicciones_recientes"] + [float(x) for x in nuevas_predicciones])[-max_buffer:]
    info["predicciones_recientes"] = buffer
    info["corte_verde_amarillo"], info["corte_amarillo_rojo"] = cortes_semaforo(np.array(buffer))
    return art


def guardar_artefacto(art: dict, ruta: str) -> None:
    joblib.dump(art, ruta)


def cargar_artefacto(ruta: str) -> dict:
    art = joblib.load(ruta)
    if art.get("sklearn") != sklearn.__version__:
        warnings.warn(f"Artefacto entrenado con scikit-learn {art.get('sklearn')} y cargado con "
                      f"{sklearn.__version__}. Se recomienda reentrenar en este entorno.")
    return art

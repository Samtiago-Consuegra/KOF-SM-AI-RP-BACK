"""Orquestación: una sola llamada produce todo lo que consume el aplicativo web."""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from . import config as C
from .confiabilidad import nivel_semanal, weibull_envolvedora
from .datos import construir_variables, preparar_base
from .ipm import calcular_ipm
from .pronostico import pronosticar_turno
from .regimen import carta_p, carta_p_json, estado_regimen


def a_json_nativo(obj):
    """Convierte tipos de numpy/pandas a tipos nativos de Python (para json.dumps)."""
    if isinstance(obj, dict):
        return {str(k): a_json_nativo(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [a_json_nativo(v) for v in obj]
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, float) and np.isnan(obj):
        return None
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    return obj


def generar_resultados(df_sap: pd.DataFrame, art: dict, incluir_carta: bool = True,
                       sensibilidad: bool = True) -> dict:
    """Calcula IPM, régimen, pronósticos y la vista integrada del tablero.

    Parámetros
        df_sap        reporte SAP crudo (mismas columnas que la exportación de la planta).
        art           artefacto de modelos.entrenar() / cargar_artefacto().
        incluir_carta incluir la serie semanal de la carta p (para graficar en el frontend).
        sensibilidad  calcular la robustez Monte Carlo del IPM (≈ 1 s).

    Devuelve un dict serializable a JSON con las claves: meta, ipm, regimen, pronostico, tablero,
    probabilidades (valores sin redondear, para modelos.recalcular_cortes).
    """
    base = preparar_base(df_sap)
    variables = construir_variables(base)

    ipm = calcular_ipm(base, con_sensibilidad=sensibilidad)
    carta = carta_p(base)
    regimen = {"estado": estado_regimen(carta)}
    if incluir_carta:
        regimen["carta_p"] = carta_p_json(carta)

    turno = pronosticar_turno(base, art, variables)
    probabilidades = {m: v.pop("_probabilidad") for m, v in turno.items()}
    pronostico = {**turno,
                  C.MAQUINA_NIVEL_SEMANAL: nivel_semanal(base),
                  C.MAQUINA_WEIBULL: weibull_envolvedora(base)}

    ipm_m = {e["Maquina"]: e for e in ipm["maquinas"]}
    tablero = []
    for m in C.MAQUINAS:                                   # Llenadora primero (criterio del jefe)
        e = ipm_m[m]
        tablero.append({"Maquina": m, "Destacada": m == "Llenadora",
                        "IPM": e["IPM"], "Nivel_IPM": e["Nivel"], "Ranking": e["Ranking"],
                        "Regimen": regimen["estado"][m]["Estado"],
                        "Regimen_codigo": regimen["estado"][m]["Codigo"],
                        "Pronostico": pronostico[m],
                        "Top_causas_min": e["Top_causas_min"]})

    meta = {"generado": datetime.now().isoformat(timespec="seconds"),
            "datos_hasta": str(base["Fecha"].max().date()),
            "modelo_entrenado_con_datos_hasta": art["datos_hasta"],
            "version_motor": art.get("version_motor"),
            "registros_procesados": int(len(base)),
            "reentrenar_recomendado": False, "motivos_reentrenar": []}
    # Reentrenar ante una alarma de la carta p POSTERIOR al último entrenamiento (un cambio de régimen
    # nuevo), no mientras persista un estado que el modelo ya conoce.
    for m in C.MAQUINAS_ML:
        alarma = regimen["estado"][m]["Ultima_alarma"]
        if alarma and alarma["Semana"] > art["datos_hasta"]:
            meta["motivos_reentrenar"].append(
                f"Carta p de {m}: alarma de {alarma['Direccion'].lower()} en la semana del {alarma['Semana']}")
    if art["datos_hasta"] < meta["datos_hasta"]:
        meta["motivos_reentrenar"].append("Hay datos más recientes que los del último entrenamiento")
    meta["reentrenar_recomendado"] = bool(meta["motivos_reentrenar"])
    return a_json_nativo({"meta": meta, "ipm": ipm, "regimen": regimen, "pronostico": pronostico,
                          "tablero": tablero, "probabilidades": probabilidades})

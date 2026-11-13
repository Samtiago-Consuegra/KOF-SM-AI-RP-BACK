"""
kof_utils.py — Funciones comunes de KOF-Smart Maintenance AI
Contiene la MISMA lógica de las Fases 1 y 2 (limpieza, calendario, serie y variables),
empaquetada en funciones para que todos los scripts —y en el futuro el backend FastAPI—
usen exactamente el mismo procesamiento. Así nunca hay dos versiones distintas del "sistema".

Uso en Colab:  subir este archivo junto a los demás y hacer  `from kof_utils import *`
"""
import numpy as np
import pandas as pd

ARCHIVO_SAP = "BD_Reporte_SAP_produccion_y_paros_Linea_4_BAQ_01-01-2024_a_22-09-2026.xlsx"
INICIO = "2024-08-01"                                    # S1
MAQ4 = ["Llenadora", "Empacadora", "Paletizadora", "Envolvedora"]   # Llenadora primero (criterio del jefe)
MAQ_SERIE = ["Empacadora", "Llenadora", "Paletizadora"]  # máquinas con serie por turno para ML
NUM = ["falla", "n_fallas", "minutos", "n_subclaves",
       "fallas_ult_3t", "fallas_ult_9t", "fallas_ult_18t",
       "min_ult_3t", "min_ult_9t", "min_ult_18t", "tasa_falla_ult_42t",
       "turnos_desde_falla", "mttr_ult_18t", "otras_fallas_ult_3t",
       "turno_sig", "dow_sig", "reinicio_sig"]
CAT = ["sistema_ult_falla"]
Y = "target_falla_sig"
SEMILLA = 42


def cargar_base(archivo=ARCHIVO_SAP):
    """Fase 1 – Carga y estandarización. Solo las 4 máquinas del alcance."""
    df = pd.read_excel(archivo)
    df["Fecha"] = pd.to_datetime(df["Fecha Contable"]).dt.normalize()
    df["Maquina"] = (df["Clave de Paro"].str.replace(r"\s+", " ", regex=True)
                     .str.replace(" L4-Baq", "", regex=False).str.strip())
    df = df[df["Maquina"].isin(MAQ4) & (df["Fecha"] >= INICIO)].copy()
    df["Hora"] = df["Intervalo"].str[:2].astype(int)
    df["Tripulación"] = df["Tripulación"].fillna("SIN_REGISTRO")
    df["Subclave de Paro"] = df["Subclave de Paro"].str.replace(r"\s+", " ", regex=True).str.strip()
    return df


def calendario(df):
    """Turnos operativos: días con algún registro (S2), 3 turnos (S3), sin turno 2 del domingo (S5)."""
    cal = pd.Series(sorted(df["Fecha"].unique()), name="Fecha").to_frame() \
            .merge(pd.DataFrame({"Turno": [1, 2, 3]}), how="cross")
    cal = cal[~((cal["Fecha"].dt.dayofweek == 6) & (cal["Turno"] == 2))]
    cal = cal.sort_values(["Fecha", "Turno"]).reset_index(drop=True)
    cal["id_turno"] = range(len(cal))
    return cal


def construir_serie(df, maquinas=MAQ_SERIE):
    """Serie máquina x turno operativo (falla 0/1, nº fallas, minutos, subclaves)."""
    cal = calendario(df)
    agg = (df.groupby(["Maquina", "Fecha", "Turno"])
             .agg(n_fallas=("Minutos de paro", "size"), minutos=("Minutos de paro", "sum"),
                  n_subclaves=("Subclave de Paro", "nunique")).reset_index())
    partes = []
    for m in maquinas:
        s = cal.merge(agg[agg["Maquina"] == m], on=["Fecha", "Turno"], how="left")
        s["Maquina"] = m
        s[["n_fallas", "minutos", "n_subclaves"]] = s[["n_fallas", "minutos", "n_subclaves"]].fillna(0)
        s["falla"] = (s["n_fallas"] > 0).astype(int)
        partes.append(s)
    return pd.concat(partes, ignore_index=True)


def construir_variables(serie, base):
    """Fase 2 – Variables sin fuga de información + target (falla en el turno siguiente)."""
    def por_maquina(s):
        s = s.sort_values("id_turno").copy()
        for w in (3, 9, 18):
            s[f"fallas_ult_{w}t"] = s["n_fallas"].rolling(w, min_periods=1).sum()
            s[f"min_ult_{w}t"] = s["minutos"].rolling(w, min_periods=1).sum()
        s["tasa_falla_ult_42t"] = s["falla"].rolling(42, min_periods=6).mean()
        grupo = s["falla"].cumsum()
        s["turnos_desde_falla"] = s.groupby(grupo).cumcount()
        s.loc[grupo == 0, "turnos_desde_falla"] = pd.NA
        s["mttr_ult_18t"] = s["min_ult_18t"] / s["fallas_ult_18t"].where(s["fallas_ult_18t"] > 0)
        s["turno_sig"] = s["Turno"].shift(-1)
        s["dow_sig"] = s["Fecha"].shift(-1).dt.dayofweek
        s["reinicio_sig"] = ((s["Fecha"].shift(-1) - s["Fecha"]).dt.days > 1).astype(int)
        s["fecha_sig"] = s["Fecha"].shift(-1)
        s[Y] = s["falla"].shift(-1)
        return s
    maqs = [m for m in MAQ_SERIE if m in serie["Maquina"].unique()]
    ds = pd.concat([por_maquina(serie[serie["Maquina"] == m]) for m in maqs])
    tot = ds.groupby("id_turno")["fallas_ult_3t"].sum().rename("linea")
    ds = ds.merge(tot, on="id_turno")
    ds["otras_fallas_ult_3t"] = ds["linea"] - ds["fallas_ult_3t"]
    ds = ds.drop(columns="linea")
    ult = (base.sort_values(["Fecha", "Turno", "Hora"])
               .groupby(["Maquina", "Fecha", "Turno"])["Clave 1 de Paro"].last()
               .reset_index().rename(columns={"Clave 1 de Paro": "sistema_ult_falla"}))
    ds = ds.merge(ult, on=["Maquina", "Fecha", "Turno"], how="left").sort_values(["Maquina", "id_turno"])
    ds["sistema_ult_falla"] = ds.groupby("Maquina")["sistema_ult_falla"].ffill().fillna("NINGUNO")
    return ds


def posicion_operativa(df):
    """Tiempo continuo en DÍAS OPERATIVOS para cada registro: día operativo + fracción del turno.
    Se usa para medir tiempos entre fallas sin contar días en que la línea no operó."""
    dias = sorted(df["Fecha"].unique())
    pos = {d: i for i, d in enumerate(dias)}
    return df["Fecha"].map(pos) + (df["Turno"] - 1) / 3, dias

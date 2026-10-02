"""Preparación de datos: carga, limpieza, calendario de turnos operativos, serie y variables.

Supuestos (documentados en el informe):
  S1  Se usan datos desde FECHA_INICIO.
  S2  Día operativo = día con al menos un registro de falla de las 4 máquinas.
  S3  Orden de turnos dentro de la fecha contable: T1 (07-15) → T2 (15-23) → T3 (23-07).
  S5  El turno 2 del domingo no opera (0 fallas en todo el histórico).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C


class DatosInvalidos(ValueError):
    """La entrada no tiene el formato del reporte SAP esperado."""


def leer_sap(ruta: str) -> pd.DataFrame:
    """Lee una exportación SAP (.xlsx o .csv) tal como la entrega la planta."""
    if ruta.lower().endswith(".csv"):
        return pd.read_csv(ruta)
    return pd.read_excel(ruta)


def preparar_base(df_sap: pd.DataFrame) -> pd.DataFrame:
    """Valida y limpia el reporte SAP. Devuelve solo fallas de equipo de las 4 máquinas.

    Columnas añadidas: Fecha (normalizada), Maquina, Hora (inicio del intervalo).
    """
    faltan = [c for c in C.COLUMNAS_REQUERIDAS if c not in df_sap.columns]
    if faltan:
        raise DatosInvalidos(f"Faltan columnas del reporte SAP: {faltan}")

    df = df_sap.copy()
    df["Fecha"] = pd.to_datetime(df["Fecha Contable"]).dt.normalize()
    df["Maquina"] = (df["Clave de Paro"].astype(str).str.replace(r"\s+", " ", regex=True)
                     .str.replace(" L4-Baq", "", regex=False).str.strip())
    df = df[(df["Tipo de Paro"] == C.TIPO_PARO) & df["Maquina"].isin(C.MAQUINAS)
            & (df["Fecha"] >= C.FECHA_INICIO)].copy()
    if df.empty:
        raise DatosInvalidos("No hay registros P.EQ.LINEA de las 4 máquinas desde la fecha de inicio.")

    df["Turno"] = df["Turno"].astype(int)
    df["Hora"] = df["Intervalo"].astype(str).str[:2].astype(int)
    df["Minutos de paro"] = pd.to_numeric(df["Minutos de paro"], errors="coerce").fillna(0).clip(0, 60)
    df["Tripulación"] = df["Tripulación"].fillna("SIN_REGISTRO")
    df["Subclave de Paro"] = df["Subclave de Paro"].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
    return df.reset_index(drop=True)


def calendario(base: pd.DataFrame) -> pd.DataFrame:
    """Turnos operativos (S2, S3, S5) con un índice cronológico continuo `id_turno`."""
    cal = (pd.DataFrame({"Fecha": sorted(base["Fecha"].unique())})
             .merge(pd.DataFrame({"Turno": [1, 2, 3]}), how="cross"))
    cal = cal[~((cal["Fecha"].dt.dayofweek == 6) & (cal["Turno"] == 2))]
    cal = cal.sort_values(["Fecha", "Turno"]).reset_index(drop=True)
    cal["id_turno"] = np.arange(len(cal))
    return cal


def posicion_operativa(base: pd.DataFrame) -> tuple[pd.Series, list]:
    """Tiempo continuo en días operativos para cada registro (día operativo + fracción de turno)."""
    dias = sorted(base["Fecha"].unique())
    pos = {d: i for i, d in enumerate(dias)}
    return base["Fecha"].map(pos) + (base["Turno"] - 1) / 3, dias


def construir_serie(base: pd.DataFrame, maquinas: list[str] | None = None) -> pd.DataFrame:
    """Serie máquina × turno operativo: falla (0/1), n_fallas, minutos, n_subclaves."""
    maquinas = maquinas or C.MAQUINAS_SERIE
    cal = calendario(base)
    agg = (base.groupby(["Maquina", "Fecha", "Turno"])
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


def _variables_maquina(s: pd.DataFrame) -> pd.DataFrame:
    s = s.sort_values("id_turno").copy()
    for w in (3, 9, 18):
        s[f"fallas_ult_{w}t"] = s["n_fallas"].rolling(w, min_periods=1).sum()
        s[f"min_ult_{w}t"] = s["minutos"].rolling(w, min_periods=1).sum()
    s["tasa_falla_ult_42t"] = s["falla"].rolling(42, min_periods=6).mean()
    grupo = s["falla"].cumsum()
    s["turnos_desde_falla"] = s.groupby(grupo).cumcount().astype(float)
    s.loc[grupo == 0, "turnos_desde_falla"] = np.nan
    s["mttr_ult_18t"] = s["min_ult_18t"] / s["fallas_ult_18t"].where(s["fallas_ult_18t"] > 0)
    s["turno_sig"] = s["Turno"].shift(-1)
    s["dow_sig"] = s["Fecha"].shift(-1).dt.dayofweek
    s["reinicio_sig"] = ((s["Fecha"].shift(-1) - s["Fecha"]).dt.days > 1).astype(int)
    s["fecha_sig"] = s["Fecha"].shift(-1)
    s[C.TARGET] = s["falla"].shift(-1)
    return s


def construir_variables(base: pd.DataFrame) -> pd.DataFrame:
    """Variables sin fuga de información para cada máquina y turno + target (falla en t+1).

    La última fila de cada máquina queda con target vacío: es la que se usa para pronosticar.
    """
    serie = construir_serie(base)
    ds = pd.concat([_variables_maquina(serie[serie["Maquina"] == m]) for m in C.MAQUINAS_SERIE])
    linea = ds.groupby("id_turno")["fallas_ult_3t"].sum().rename("linea")
    ds = ds.merge(linea, on="id_turno")
    ds["otras_fallas_ult_3t"] = ds["linea"] - ds["fallas_ult_3t"]
    ds = ds.drop(columns="linea")
    ult = (base.sort_values(["Fecha", "Turno", "Hora"])
               .groupby(["Maquina", "Fecha", "Turno"])["Clave 1 de Paro"].last()
               .reset_index().rename(columns={"Clave 1 de Paro": "sistema_ult_falla"}))
    ds = ds.merge(ult, on=["Maquina", "Fecha", "Turno"], how="left").sort_values(["Maquina", "id_turno"])
    ds["sistema_ult_falla"] = ds.groupby("Maquina")["sistema_ult_falla"].ffill().fillna("NINGUNO")
    return ds.reset_index(drop=True)


def siguiente_turno(fecha: pd.Timestamp, turno: int) -> tuple[pd.Timestamp, int]:
    """Turno operativo que sigue a (fecha, turno). El turno 2 del domingo no opera (S5)."""
    f, t = (fecha, turno + 1) if turno < 3 else (fecha + pd.Timedelta(days=1), 1)
    if f.dayofweek == 6 and t == 2:
        t = 3
    return f, t

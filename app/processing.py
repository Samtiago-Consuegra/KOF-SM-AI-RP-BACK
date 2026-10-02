"""Lectura y limpieza del Excel SAP antes de guardarlo en la base de datos.

Reutilizado del backend demo (CocaColaRosaBack). Cambios:
  - Solo se exigen las columnas que usan el EDA y el motor de predicción.
  - Se conservan Tripulación, Turno numérico e Intervalo válido (los necesita kof_engine).
  - Cada fila recibe una `row_key` para evitar duplicados sin perder filas idénticas legítimas.
"""
import hashlib
import re

import pandas as pd

from kof_engine import config as engine_config

REQUIRED_COLUMNS = list(engine_config.COLUMNAS_REQUERIDAS)   # Fecha Contable, Turno, Intervalo, ...
OPTIONAL_COLUMNS = ["Ptos. Efi. Perdid.", "Cajas Producidas"]

SHEET_NAME = "BD SAP"
FILTER_STOP_TYPE = engine_config.TIPO_PARO                   # "P.EQ.LINEA"

EXCLUDED_ROWS = [
    "Inspector de nivel filtec L4-B",
    "Inspector de Nivel L4-Baq",
]

INTERVAL_RE = re.compile(r"^\d{2}:\d{2}")


def clean_machine_name(raw: str) -> str:
    """'Empacadora  L4-Baq' -> 'Empacadora'."""
    name = re.sub(r"\s*L\s?4[- ]?.*$", "", str(raw), flags=re.IGNORECASE)
    return " ".join(name.split())


def _clean_text(value) -> str | None:
    if pd.isna(value):
        return None
    text = " ".join(str(value).split())
    return text or None


def read_sap_file(path: str) -> pd.DataFrame:
    """Lee la hoja 'BD SAP' si existe; si no, la primera hoja. También acepta .csv."""
    if path.lower().endswith(".csv"):
        return pd.read_csv(path)
    sheets = pd.ExcelFile(path).sheet_names
    return pd.read_excel(path, sheet_name=SHEET_NAME if SHEET_NAME in sheets else sheets[0])


def load_and_filter(path: str, source_file: str) -> tuple[pd.DataFrame, int]:
    """Devuelve (registros limpios, cantidad de filas P.EQ.LINEA descartadas por datos inválidos)."""
    df = read_sap_file(path)
    df.columns = [str(c).strip() for c in df.columns]

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Columnas requeridas no encontradas: {missing}")
    for col in OPTIONAL_COLUMNS:
        if col not in df.columns:
            df[col] = None

    df = df[df["Tipo de Paro"].astype(str).str.strip() == FILTER_STOP_TYPE]
    df = df[~df["Clave de Paro"].isin(EXCLUDED_ROWS)]
    total = len(df)

    out = pd.DataFrame({
        "machine": df["Clave de Paro"].map(clean_machine_name),
        "subsystem": df["Clave 1 de Paro"].map(_clean_text),
        "failure_type": df["Subclave de Paro"].map(_clean_text),
        "shift": pd.to_numeric(df["Turno"], errors="coerce"),
        "interval": df["Intervalo"].astype(str).str.strip(),
        "event_date": pd.to_datetime(df["Fecha Contable"], errors="coerce").dt.date,
        "stop_minutes": pd.to_numeric(df["Minutos de paro"], errors="coerce"),
        "crew": df["Tripulación"].map(_clean_text),
        "efficiency_points_lost": pd.to_numeric(df["Ptos. Efi. Perdid."], errors="coerce"),
        "boxes_produced": pd.to_numeric(df["Cajas Producidas"], errors="coerce"),
    })

    # El motor necesita turno 1-3 e intervalo "HH:MM-HH:MM": las filas que no los cumplen se descartan.
    valid = (
        out["machine"].astype(bool)
        & out["failure_type"].notna()
        & out["event_date"].notna()
        & out["stop_minutes"].notna()
        & out["shift"].isin([1, 2, 3])
        & out["interval"].str.match(INTERVAL_RE)
    )
    out = out[valid].copy()

    out["shift"] = out["shift"].astype(int)
    out["stop_minutes"] = out["stop_minutes"].round(2)
    out["efficiency_points_lost"] = out["efficiency_points_lost"].round(4)
    out["boxes_produced"] = out["boxes_produced"].fillna(0).astype(int)
    out["efficiency_points_lost"] = out["efficiency_points_lost"].astype(object).where(
        out["efficiency_points_lost"].notna(), None)
    out["source_file"] = source_file
    out["row_key"] = _row_keys(out)
    return out.reset_index(drop=True), total - len(out)


def _row_keys(df: pd.DataFrame) -> pd.Series:
    """sha256(campos de la fila + nº de aparición). Si SAP trae 2 filas idénticas, ambas se guardan
    (…#0 y …#1); si luego se vuelve a subir el mismo periodo, ambas se reconocen como duplicadas."""
    cols = ["machine", "subsystem", "failure_type", "shift", "interval", "event_date", "stop_minutes", "crew"]
    base = df[cols].astype(str).agg("|".join, axis=1)
    occurrence = base.groupby(base).cumcount().astype(str)
    return (base + "#" + occurrence).map(lambda s: hashlib.sha256(s.encode("utf-8")).hexdigest())

"""Puente entre la base de datos y kof_engine (el motor de predicción NO se modifica).

Flujo:
  1. events_to_sap_dataframe(): lee TODOS los paros de la BD y los devuelve con las columnas del
     reporte SAP que espera el motor (el motor necesita el histórico completo, no solo lo nuevo).
  2. run_engine(): entrena (o reutiliza el último modelo), genera resultados y los guarda en
     prediction_runs. Se ejecuta en segundo plano porque entrenar tarda de segundos a ~1 min.
"""
import io
import logging
import threading
import traceback
from datetime import datetime, timezone

import joblib
import pandas as pd
import sklearn
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from kof_engine import config as engine_config
from kof_engine import entrenar, generar_resultados, preparar_base
from kof_engine.ipm import historico_ipm
from kof_engine.pipeline import a_json_nativo

from .db import SessionLocal
from .models import FailureEvent, ModelArtifact, PredictionRun

log = logging.getLogger("kof.predicciones")
_engine_lock = threading.Lock()          # un solo entrenamiento a la vez por instancia

KEEP_ARTIFACTS = 3                        # modelos que se conservan en la BD (~1 MB c/u)
RISK_SLUG = {"Crítico": "critico", "Alto": "alto", "Medio": "medio", "Bajo": "bajo"}
TREND_WEEKS = 12


# --- BD -> DataFrame con formato SAP --------------------------------------------------------------
def events_to_sap_dataframe(db: Session) -> pd.DataFrame:
    rows = db.execute(select(
        FailureEvent.event_date, FailureEvent.shift, FailureEvent.interval, FailureEvent.machine,
        FailureEvent.subsystem, FailureEvent.failure_type, FailureEvent.stop_minutes, FailureEvent.crew,
    )).all()
    df = pd.DataFrame(rows, columns=["Fecha Contable", "Turno", "Intervalo", "Clave de Paro",
                                     "Clave 1 de Paro", "Subclave de Paro", "Minutos de paro", "Tripulación"])
    df["Tipo de Paro"] = engine_config.TIPO_PARO     # en la BD solo se guardan paros P.EQ.LINEA
    df["Minutos de paro"] = pd.to_numeric(df["Minutos de paro"]).astype(float)
    return df


# --- Artefacto (modelos entrenados) <-> bytes ----------------------------------------------------
def _dump_artifact(art: dict) -> bytes:
    buffer = io.BytesIO()
    joblib.dump(art, buffer, compress=3)
    return buffer.getvalue()


def _load_artifact(data: bytes) -> dict:
    return joblib.load(io.BytesIO(data))


def _artifact_metrics(art: dict) -> dict:
    return {m: {k: info[k] for k in ("auc_oof_total", "auc_oof_reciente", "corte_verde_amarillo",
                                     "corte_amarillo_rojo", "tasa_reciente", "n_turnos_entrenamiento")}
            for m, info in art["modelos"].items()}


# --- Vista simplificada para la matriz predictiva del frontend -----------------------------------
def build_matrix(results: dict, ipm_history: pd.DataFrame) -> list[dict]:
    """Una fila por máquina con lo que necesita la tabla 'Matriz predictiva de equipos'."""
    rows = []
    for item in results["tablero"]:
        m, p = item["Maquina"], item["Pronostico"]
        hist = ipm_history[ipm_history["Maquina"] == m].tail(TREND_WEEKS)
        trend = [{"k": i, "date": r["Fecha_corte"], "v": r["IPM"]} for i, (_, r) in enumerate(hist.iterrows())]
        first, last = (trend[0]["v"], trend[-1]["v"]) if trend else (0, 0)

        row = {
            "machine": m,
            "highlighted": item["Destacada"],
            "risk": RISK_SLUG.get(item["Nivel_IPM"], "bajo"),     # nivel del IPM: critico|alto|medio|bajo
            "ipm": item["IPM"],
            "ranking": item["Ranking"],
            "regime": item["Regimen"],
            "regime_code": item["Regimen_codigo"],
            "top_cause": next(iter(item["Top_causas_min"]), None),
            "top_causes_min": item["Top_causas_min"],
            "trend": trend,
            "trend_pct": round(100 * (last - first) / first, 1) if first else 0.0,
            "prediction_type": p.get("Tipo_pronostico"),
            "probability": None, "horizon": None, "signal": None,
            "expected_failures_week": None, "mean_residual_days_op": None,
        }
        if "Nivel_Riesgo" in p:                                      # Llenadora, Empacadora
            t = p["Turno_pronosticado"]
            row.update(probability=p["Probabilidad_Falla_%"], signal=p["Nivel_Riesgo"],
                       horizon=f"Turno {t['Turno']} del {t['Fecha']}")
        elif "Fallas_esperadas_semana" in p:                         # Paletizadora
            row.update(expected_failures_week=p["Fallas_esperadas_semana"], horizon="Próxima semana")
        elif p.get("Disponible"):                                    # Envolvedora (Weibull)
            row.update(probability=p["Probabilidad_Falla_%"]["Proximos_7_dias_op"],
                       horizon="Próximos 7 días operativos",
                       mean_residual_days_op=p["Tiempo_medio_residual_dias_op"])
        rows.append(row)
    return rows


def _prune_artifacts(db: Session) -> None:
    keep = db.scalars(select(ModelArtifact.id).order_by(ModelArtifact.id.desc()).limit(KEEP_ARTIFACTS)).all()
    db.execute(delete(ModelArtifact).where(ModelArtifact.id.not_in(keep)))


# --- Ejecución del motor -------------------------------------------------------------------------
def create_run(db: Session, trigger: str) -> PredictionRun:
    run = PredictionRun(status="en_proceso", trigger=trigger)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def run_engine(run_id: int, retrain: bool = True) -> None:
    """Tarea en segundo plano. Nunca lanza excepciones: el error queda guardado en la ejecución."""
    with _engine_lock, SessionLocal() as db:
        run = db.get(PredictionRun, run_id)
        try:
            df = events_to_sap_dataframe(db)
            if df.empty:
                raise ValueError("No hay paros cargados. Sube un Excel SAP primero.")

            artifact_row = None
            if not retrain:
                artifact_row = db.scalars(select(ModelArtifact).order_by(ModelArtifact.id.desc()).limit(1)).first()
                if artifact_row and artifact_row.sklearn_version != sklearn.__version__:
                    artifact_row = None                              # versión distinta: reentrenar
            if artifact_row:
                art = _load_artifact(artifact_row.data)
            else:
                log.info("Entrenando modelos (run %s)…", run_id)
                art = entrenar(df)
                artifact_row = ModelArtifact(trained_until=pd.Timestamp(art["datos_hasta"]).date(),
                                             sklearn_version=art["sklearn"], metrics=_artifact_metrics(art),
                                             data=_dump_artifact(art))
                db.add(artifact_row)
                db.flush()

            results = generar_resultados(df, art)
            history = historico_ipm(preparar_base(df))
            results["historico_ipm"] = history.to_dict(orient="records")
            results["matriz"] = build_matrix(results, history)

            run.results = a_json_nativo(results)
            run.data_until = pd.Timestamp(results["meta"]["datos_hasta"]).date()
            run.artifact_id = artifact_row.id
            run.status = "ok"
            _prune_artifacts(db)
        except Exception as exc:                                     # noqa: BLE001
            log.error("Fallo del motor (run %s): %s", run_id, traceback.format_exc())
            db.rollback()
            run = db.get(PredictionRun, run_id)
            run.status = "error"
            run.error = f"{type(exc).__name__}: {exc}"
        run.finished_at = datetime.now(timezone.utc)
        db.commit()


def mark_interrupted_runs() -> None:
    """Al arrancar: si el servidor se reinició a mitad de un entrenamiento, esa ejecución quedó colgada."""
    with SessionLocal() as db:
        db.execute(update(PredictionRun).where(PredictionRun.status == "en_proceso")
                   .values(status="error", error="Interrumpido por reinicio del servidor",
                           finished_at=datetime.now(timezone.utc)))
        db.commit()

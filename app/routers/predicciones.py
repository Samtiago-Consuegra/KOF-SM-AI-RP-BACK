from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ModelArtifact, PredictionRun
from ..prediction_service import create_run, run_engine

router = APIRouter(prefix="/predicciones", tags=["predicciones"])

SECTIONS = {"meta", "ipm", "regimen", "pronostico", "tablero", "matriz", "historico_ipm"}


def _run_status(run: PredictionRun | None) -> dict | None:
    if run is None:
        return None
    return {"id": run.id, "status": run.status, "trigger": run.trigger, "error": run.error,
            "created_at": run.created_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "data_until": run.data_until.isoformat() if run.data_until else None}


@router.get("")
def latest_predictions(
    include: str | None = Query(None, description="Secciones separadas por coma. Por defecto todas: "
                                                   "meta,ipm,regimen,pronostico,tablero,matriz,historico_ipm"),
    db: Session = Depends(get_db),
):
    """Último resultado exitoso del motor + estado de la ejecución más reciente."""
    latest = db.scalars(select(PredictionRun).order_by(PredictionRun.id.desc()).limit(1)).first()
    ok = db.scalars(select(PredictionRun).where(PredictionRun.status == "ok")
                    .order_by(PredictionRun.id.desc()).limit(1)).first()
    if ok is None:
        return {"available": False, "latest_run": _run_status(latest),
                "message": "Aún no hay predicciones. Sube un Excel SAP o ejecuta POST /predicciones/ejecutar."}

    results = ok.results
    if include:
        wanted = {s.strip() for s in include.split(",")} & SECTIONS
        results = {k: v for k, v in results.items() if k in wanted}
    results.pop("probabilidades", None)          # dato interno del motor
    return {"available": True, "run": _run_status(ok), "latest_run": _run_status(latest), **results}


@router.get("/estado")
def run_status(run_id: int | None = None, db: Session = Depends(get_db)):
    """Para que el frontend consulte si terminó el entrenamiento lanzado por una subida."""
    q = select(PredictionRun)
    q = q.where(PredictionRun.id == run_id) if run_id else q.order_by(PredictionRun.id.desc()).limit(1)
    run = db.scalars(q).first()
    if run_id and run is None:
        raise HTTPException(status_code=404, detail="Ejecución no encontrada")
    return _run_status(run) or {"status": "sin_ejecuciones"}


@router.post("/ejecutar")
def execute(background: BackgroundTasks, reentrenar: bool = True, db: Session = Depends(get_db)):
    """Lanza el motor manualmente. reentrenar=false reutiliza el último modelo (más rápido)."""
    busy = db.scalars(select(PredictionRun).where(PredictionRun.status == "en_proceso").limit(1)).first()
    if busy:
        return {"prediction_run_id": busy.id, "status": "en_proceso", "message": "Ya hay una ejecución en curso"}
    run = create_run(db, trigger="manual")
    background.add_task(run_engine, run.id, reentrenar)
    return {"prediction_run_id": run.id, "status": "en_proceso"}


@router.get("/modelo")
def model_info(db: Session = Depends(get_db)):
    """Métricas del último modelo entrenado (AUC fuera de muestra, cortes del semáforo)."""
    art = db.scalars(select(ModelArtifact).order_by(ModelArtifact.id.desc()).limit(1)).first()
    if art is None:
        return {"available": False}
    return {"available": True, "id": art.id, "created_at": art.created_at.isoformat(),
            "trained_until": art.trained_until.isoformat() if art.trained_until else None,
            "sklearn_version": art.sklearn_version, "metrics": art.metrics}

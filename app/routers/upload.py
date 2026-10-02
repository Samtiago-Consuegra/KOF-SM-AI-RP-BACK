import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import DataLoad, FailureEvent
from ..prediction_service import create_run, run_engine
from ..processing import load_and_filter

router = APIRouter(tags=["upload"])

MAX_SIZE = 200 * 1024 * 1024
ALLOWED = (".xlsx", ".xls", ".csv")
CHUNK = 1000
COLUMNS = ["row_key", "machine", "subsystem", "failure_type", "shift", "interval", "event_date",
           "stop_minutes", "crew", "efficiency_points_lost", "boxes_produced", "source_file"]


@router.post("/upload")
def upload_historical_file(background: BackgroundTasks, file: UploadFile = File(...),
                           db: Session = Depends(get_db)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED:
        raise HTTPException(status_code=400, detail="El archivo debe tener extensión .xlsx, .xls o .csv")

    content = file.file.read(MAX_SIZE + 1)
    if len(content) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="Archivo demasiado grande (máx. 200 MB)")

    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(content)
        tmp.close()
        try:
            df, discarded = load_and_filter(tmp.name, source_file=file.filename)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Error al procesar: {exc}")
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    if df.empty:
        raise HTTPException(status_code=400, detail="No se encontraron registros P.EQ.LINEA válidos en el archivo")

    load = DataLoad(file_name=file.filename, records_found=len(df), records_discarded=discarded,
                    date_from=df["event_date"].min(), date_to=df["event_date"].max())
    db.add(load)
    db.flush()                                   # obtiene load.id sin cerrar la transacción

    rows = df[COLUMNS].assign(data_load_id=load.id).to_dict("records")
    inserted = 0
    stmt = (pg_insert(FailureEvent).on_conflict_do_nothing(index_elements=["row_key"])
            .returning(FailureEvent.id))
    for i in range(0, len(rows), CHUNK):
        inserted += len(db.execute(stmt, rows[i:i + CHUNK]).all())

    load.records_inserted = inserted
    load.records_duplicated = len(rows) - inserted
    db.commit()

    # Hay datos nuevos → reentrenar y recalcular predicciones en segundo plano.
    run_id = None
    if inserted:
        run_id = create_run(db, trigger="upload").id
        background.add_task(run_engine, run_id, True)

    return {
        "load_id": load.id,
        "file_name": file.filename,
        "records_found": len(rows),
        "records_inserted": inserted,
        "records_duplicated": len(rows) - inserted,
        "records_discarded": discarded,
        "date_from": load.date_from.isoformat(),
        "date_to": load.date_to.isoformat(),
        "prediction_run_id": run_id,
        "prediction_status": "en_proceso" if run_id else "sin_cambios",
    }


@router.get("/uploads")
def list_uploads(limit: int = 50, db: Session = Depends(get_db)):
    """Historial de archivos subidos (más reciente primero)."""
    loads = db.scalars(select(DataLoad).order_by(DataLoad.id.desc()).limit(limit)).all()
    return [
        {
            "id": l.id, "file_name": l.file_name, "uploaded_at": l.uploaded_at.isoformat(),
            "records_found": l.records_found, "records_inserted": l.records_inserted,
            "records_duplicated": l.records_duplicated, "records_discarded": l.records_discarded,
            "date_from": l.date_from.isoformat() if l.date_from else None,
            "date_to": l.date_to.isoformat() if l.date_to else None,
        }
        for l in loads
    ]

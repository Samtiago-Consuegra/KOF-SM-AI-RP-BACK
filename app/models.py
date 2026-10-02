from sqlalchemy import (Column, Date, DateTime, ForeignKey, Integer, LargeBinary, Numeric, String,
                        Text, func)
from sqlalchemy.dialects.postgresql import JSONB

from .db import Base


class FailureEvent(Base):
    """Un registro de paro P.EQ.LINEA del reporte SAP (una fila del Excel)."""

    __tablename__ = "failure_events"

    id = Column(Integer, primary_key=True)
    # Huella de la fila + número de aparición dentro del archivo. Evita duplicar datos al subir
    # exportaciones que se solapan, sin perder filas idénticas legítimas (el motor las cuenta).
    row_key = Column(String(64), nullable=False, unique=True)
    machine = Column(String(50), nullable=False, index=True)      # Clave de Paro (limpia)
    subsystem = Column(String(100))                               # Clave 1 de Paro
    failure_type = Column(String(150))                            # Subclave de Paro
    shift = Column(Integer, nullable=False)                       # Turno 1-3
    interval = Column(String(20), nullable=False)                 # "HH:MM-HH:MM"
    event_date = Column(Date, nullable=False, index=True)         # Fecha Contable
    stop_minutes = Column(Numeric(10, 2), nullable=False)
    crew = Column(String(100))                                    # Tripulación (puede venir vacía)
    efficiency_points_lost = Column(Numeric(10, 4))
    boxes_produced = Column(Integer)
    source_file = Column(String(255))
    data_load_id = Column(Integer, ForeignKey("data_loads.id", ondelete="SET NULL"))
    loaded_at = Column(DateTime(timezone=True), server_default=func.now())


class DataLoad(Base):
    """Bitácora de cada Excel subido."""

    __tablename__ = "data_loads"

    id = Column(Integer, primary_key=True)
    file_name = Column(String(255))
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now())
    records_found = Column(Integer)
    records_inserted = Column(Integer)
    records_duplicated = Column(Integer)
    records_discarded = Column(Integer)
    date_from = Column(Date)
    date_to = Column(Date)


class ModelArtifact(Base):
    """Modelos entrenados por kof_engine (joblib comprimido). Se guardan en la BD porque el disco
    de Render gratuito se borra en cada reinicio."""

    __tablename__ = "model_artifacts"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    trained_until = Column(Date)
    sklearn_version = Column(String(20))
    metrics = Column(JSONB)
    data = Column(LargeBinary, nullable=False)


class PredictionRun(Base):
    """Cada ejecución del motor (entrenar + generar resultados). El frontend lee la última 'ok'."""

    __tablename__ = "prediction_runs"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    finished_at = Column(DateTime(timezone=True))
    status = Column(String(20), nullable=False, default="en_proceso")   # en_proceso | ok | error
    trigger = Column(String(30))                                       # upload | manual
    error = Column(Text)
    data_until = Column(Date)
    artifact_id = Column(Integer, ForeignKey("model_artifacts.id", ondelete="SET NULL"))
    results = Column(JSONB)

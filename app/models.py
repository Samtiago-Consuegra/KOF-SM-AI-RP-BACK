from sqlalchemy import (JSON, Boolean, Column, Date, DateTime, ForeignKey, Integer, LargeBinary, Numeric,
                        String, Text, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import JSONB

from .db import Base


def _json():
    """JSONB en PostgreSQL y JSON en SQLite (los tests corren con SQLite temporal)."""
    return JSONB().with_variant(JSON(), "sqlite")


class User(Base):
    """Usuarios registrados para acceder a la plataforma."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), nullable=False, unique=True, index=True)
    full_name = Column(String(150))
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


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


# ---------------------------------------------------------------------------
# Motor de planes de mantenimiento (motor_planes/)
# ---------------------------------------------------------------------------
MOTOR_ARCHIVO_TIPOS = ("avisos", "ordenes", "plan", "consolidado", "acr", "acr_revisado", "equivalencias")


class MotorArchivo(Base):
    """Excel que sube el usuario para alimentar el motor de planes.

    Para avisos, órdenes, plan, consolidado, acr_revisado y equivalencias solo se usa el más
    reciente de cada tipo. Para acr se conservan todos (uno por ACR) y el nombre original manda.
    """

    __tablename__ = "motor_archivos"

    id = Column(Integer, primary_key=True)
    tipo = Column(String(20), nullable=False, index=True)
    nombre_archivo = Column(String(255), nullable=False)
    data = Column(LargeBinary, nullable=False)
    subido_en = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    subido_por = Column(String(255))


class MotorRun(Base):
    """Cada corrida del motor de planes (subproceso aislado)."""

    __tablename__ = "motor_runs"

    id = Column(Integer, primary_key=True)
    status = Column(String(20), nullable=False, default="en_cola", index=True)  # en_cola|en_proceso|ok|error
    trigger = Column(String(30))                                                # upload|manual|paradas|semana|...
    fases = Column(_json(), nullable=False, default=list)
    semana = Column(String(8))
    fecha_ref = Column(Date)
    creado_en = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    iniciado_en = Column(DateTime(timezone=True))
    terminado_en = Column(DateTime(timezone=True))
    error = Column(Text)
    resumen = Column(_json())


class MotorSnapshot(Base):
    """tar.gz de la carpeta de salida después de una corrida completa (incluye fase 12)."""

    __tablename__ = "motor_snapshots"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("motor_runs.id", ondelete="SET NULL"), index=True)
    creado_en = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    data = Column(LargeBinary, nullable=False)


class MotorSalida(Base):
    """Archivo JSON/XLSX que lee la app. Único por (nombre, semana)."""

    __tablename__ = "motor_salidas"
    __table_args__ = (UniqueConstraint("nombre", "semana", name="uq_motor_salida_nombre_semana"),)

    id = Column(Integer, primary_key=True)
    nombre = Column(String(40), nullable=False, index=True)
    semana = Column(String(10), nullable=False, default="")
    run_id = Column(Integer, ForeignKey("motor_runs.id", ondelete="SET NULL"))
    contenido = Column(_json())
    archivo = Column(LargeBinary)
    actualizado_en = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
                            nullable=False)


class HistoriaEvento(Base):
    """Evento de la historia clínica reconstruido desde las salidas del motor."""

    __tablename__ = "historia_eventos"

    id = Column(Integer, primary_key=True)
    maquina = Column(String(50), nullable=False, index=True)
    fecha = Column(Date, nullable=False, index=True)
    tipo = Column(String(20), nullable=False, index=True)   # FALLA|PREVENTIVO|CORRECTIVO|AVISO
    id_componente = Column(String(30))
    componente = Column(String(200))
    sistema = Column(String(200))
    titulo = Column(String(400))
    detalle = Column(Text)
    minutos_paro = Column(Numeric(10, 2))
    orden = Column(String(30))


class Parada(Base):
    """Parada programada por el usuario más su seguimiento para Cumplimiento."""

    __tablename__ = "paradas"

    id_parada = Column(String(30), primary_key=True)
    titulo = Column(String(255), nullable=False)
    modalidad = Column(String(20), nullable=False)          # Frío | Caliente
    fecha = Column(Date, nullable=False, index=True)
    inicio = Column(String(5), nullable=False)              # HH:MM
    fin = Column(String(5), nullable=False)                 # HH:MM
    maquinas = Column(_json(), nullable=False, default=list)
    tareas_manuales = Column(_json(), nullable=False, default=list)
    creada_por = Column(String(255))
    creada_en = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    actualizada_en = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
                            nullable=False)
    estado_base = Column(String(20), nullable=False, default="Pendiente")  # Pendiente|En proceso|Cumplida
    fin_real = Column(String(5))
    tareas_estado = Column(_json(), nullable=False, default=dict)
    observaciones = Column(Text)
    historial_reprogramaciones = Column(_json(), nullable=False, default=list)
    parada_original_id = Column(String(30), ForeignKey("paradas.id_parada", ondelete="SET NULL"))
    reprogramada_a = Column(String(30))
    reprogramada_a_fecha = Column(Date)
    cumplida_parcial = Column(Boolean, nullable=False, default=False)
    avance_al_cumplir = Column(Integer)


class DecisionTarea(Base):
    """Decisión del usuario sobre una tarea sugerida (Enviada / Pospuesta)."""

    __tablename__ = "decision_tareas"

    id_tarea = Column(String(60), primary_key=True)
    decision = Column(String(20), nullable=False)            # Enviada | Pospuesta
    id_parada = Column(String(30), ForeignKey("paradas.id_parada", ondelete="SET NULL"))
    semana = Column(String(8))
    decidida_por = Column(String(255))
    decidida_en = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

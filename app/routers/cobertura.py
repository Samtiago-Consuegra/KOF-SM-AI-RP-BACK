from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import cobertura_service
from ..db import get_db
from ..deps import get_current_user
from ..models import User

router = APIRouter(tags=["cobertura"])


@router.get("/cobertura")
def cobertura(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    """Rango de fechas (desde/hasta) y total de registros de cada fuente de datos."""
    return cobertura_service.get_cobertura(db)

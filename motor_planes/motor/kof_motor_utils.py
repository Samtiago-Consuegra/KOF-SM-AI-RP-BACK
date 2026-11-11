"""
kof_motor_utils.py — Reglas compartidas del MOTOR DE PLANES (Fases 16, 17 y 18)
KOF-Smart Maintenance AI · Línea 4 PET

Aquí están las reglas que deben ser IGUALES en la Alternativa A, la Alternativa B y la fusión:
la grilla de frecuencias, cómo se convierte un puntaje en nivel de prioridad y cómo se escribe
una recomendación. El backend de la web usará exactamente estas mismas funciones.
"""
import numpy as np
import pandas as pd

# Frecuencias "operativas" que maneja la planta (en semanas). Toda frecuencia propuesta se
# redondea HACIA ABAJO a esta grilla, para no pasarse nunca del intervalo calculado.
GRILLA_SEMANAS = [1, 2, 4, 8, 13, 26, 52]
NOMBRE_FRECUENCIA = {1: "Semanal", 2: "Quincenal", 4: "Mensual", 8: "Bimestral",
                     13: "Trimestral", 26: "Semestral", 52: "Anual"}

# Niveles de prioridad: los mismos cuatro del IPM del proyecto. Se asignan por POSICIÓN en el
# ranking (no por un umbral fijo) para que "Crítica" sea de verdad selectiva:
#   Crítica = 10 % superior, Alta = siguiente 20 %, Media = siguiente 30 %, Baja = resto.
CORTES_NIVEL = [(0.90, "Crítica"), (0.70, "Alta"), (0.40, "Media"), (0.0, "Baja")]

# Columnas que deben tener las recomendaciones de A, de B y de la fusión
COLUMNAS_RECOMENDACION = [
    "Id_componente", "Maquina", "Sistema", "Componente", "Puntaje", "Prioridad", "Accion",
    "Tipo_tarea_propuesta", "Frecuencia_actual_sem", "Frecuencia_propuesta_sem",
    "Frecuencia_propuesta", "Justificacion", "Confianza"]


def a_grilla(semanas):
    """Redondea hacia abajo a la grilla operativa. Menos de 1 semana -> 0 (rutina de turno)."""
    if pd.isna(semanas):
        return np.nan
    if semanas < 1:
        return 0
    return max(g for g in GRILLA_SEMANAS if g <= semanas)


def nombre_frecuencia(semanas):
    if pd.isna(semanas):
        return "Sin cambio"
    if semanas == 0:
        return "Cada turno (rutina del operador)"
    return NOMBRE_FRECUENCIA.get(int(semanas), f"Cada {int(semanas)} semanas")


def rango_pct(serie):
    """Posición relativa de 0 a 100 dentro de la línea (100 = el mayor). Ceros quedan en 0."""
    s = pd.Series(serie, dtype=float).fillna(0)
    r = s.rank(pct=True) * 100
    return r.where(s > 0, 0.0)


def niveles(puntajes):
    """Serie de puntajes -> nivel de prioridad según su posición en el ranking."""
    p = pd.Series(puntajes, dtype=float)
    pos = p.rank(pct=True, method="max")
    return pos.map(lambda x: "Sin datos" if pd.isna(x) else next(n for c, n in CORTES_NIVEL if x > c)) \
              .where(p.notna(), "Sin datos")


def miles(x):
    """1234567 -> '1,2 millones' (para justificaciones legibles)."""
    return f"{x/1e6:,.1f} millones".replace(",", "X").replace(".", ",").replace("X", ".")

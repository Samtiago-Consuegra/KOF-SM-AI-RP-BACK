"""
KOF-Smart Maintenance AI — Fase 16: ALTERNATIVA A — plan desde el REPORTE DIARIO de fallas
(código puro, sin gráficas)                         Alimenta: pestaña "Mantenimiento" (vía fusión)

PREGUNTA QUE RESPONDE: "Mirando solo cuánto le duele a la línea cada componente (minutos,
frecuencia, duración y forma de la falla), ¿qué se debería hacer y cada cuánto?"

Requiere: kof_motor_utils.py, kof_utils.py y las salidas de las Fases 13 y 14
  (hc_componentes.csv, base_diaria_componentes.csv)

1. PUNTAJE A (0 a 100): qué tanto afecta el componente a la línea.
     50 % minutos de paro + 25 % eventos + 15 % MTTR mediano + 10 % tendencia
   Cada criterio se convierte a su posición relativa dentro de la línea (0 = el menor,
   100 = el mayor), igual que la normalización del IPM. El nivel (Crítica, Alta, Media, Baja)
   sale de la posición en el ranking: Crítica = 10 % superior, Alta = siguiente 20 %, etc.

2. ACCIÓN según la forma de la falla (Weibull, Fase 14):
   - Desgaste (beta > 1.2): preventivo por TIEMPO. Intervalo = momento en que el 90 % de las
     piezas todavía no ha fallado:  t = eta * (-ln 0.90)^(1/beta)
   - Aleatoria (0.8 a 1.2): la edad no predice la falla, cambiar antes no ayuda. Se propone
     INSPECCIÓN por condición a la mitad del MTBF mediano.
   - En racha (beta < 0.8): las fallas vienen seguidas; acortar el preventivo NO ayuda.
     Se propone revisar la calidad de la reparación y estandarizar el ajuste.
   - Pocos datos (menos de 10 eventos): mantener el plan vigente y hacer seguimiento.
   Si el intervalo da menos de 1 semana, se propone una rutina de verificación del operador
   en cada turno (mantenimiento autónomo), porque no tiene sentido una orden diaria.

   Los tiempos de Weibull están en DÍAS OPERATIVOS; se pasan a semanas de calendario con la
   proporción real de días operados en el periodo (por ejemplo, 632 de 783 días).

Salida: recomendaciones_A.csv  (mismas columnas que B y la fusión: COLUMNAS_RECOMENDACION)
"""
import numpy as np
import pandas as pd
from kof_motor_utils import (a_grilla, nombre_frecuencia, rango_pct, niveles, COLUMNAS_RECOMENDACION)
from kof_planes_utils import MAQ4

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", None)

CONFIABILIDAD_OBJETIVO = 0.90
PESOS_A = {"minutos": 0.50, "eventos": 0.25, "mttr": 0.15, "tendencia": 0.10}

hc = pd.read_csv("hc_componentes.csv")
diario = pd.read_csv("base_diaria_componentes.csv", parse_dates=["Fecha"])

# Días de calendario por cada día operativo (la línea no opera todos los días)
dias_cal = (diario["Fecha"].max() - diario["Fecha"].min()).days + 1
dias_op = diario["Fecha"].nunique()
FACTOR_CAL = dias_cal / dias_op
print(f"Días operativos: {dias_op} de {dias_cal} de calendario -> 1 día operativo = {FACTOR_CAL:.2f} días")

a = hc[hc["Eventos"] > 0].copy()          # la Alternativa A solo ve lo que aparece en el reporte diario

# ---------------------------------------------------------------------------
# 1. Puntaje A
# ---------------------------------------------------------------------------
tend = a["Tendencia"].map({"Empeora": 1.0, "Estable": 0.5, "Mejora": 0.0}).fillna(0.25)
a["P_minutos"] = rango_pct(a["Minutos_paro"])
a["P_eventos"] = rango_pct(a["Eventos"])
a["P_mttr"] = rango_pct(a["MTTR_mediana_min"])
a["P_tendencia"] = tend * 100
a["Puntaje"] = (PESOS_A["minutos"] * a["P_minutos"] + PESOS_A["eventos"] * a["P_eventos"]
                + PESOS_A["mttr"] * a["P_mttr"] + PESOS_A["tendencia"] * a["P_tendencia"])
a["Prioridad"] = niveles(a["Puntaje"])


# ---------------------------------------------------------------------------
# 2. Acción según la forma de la falla
# ---------------------------------------------------------------------------
def accion_a(f):
    forma = str(f["Forma_falla"])
    mtbf_sem = f["MTBF_mediana_dias"] * FACTOR_CAL / 7 if pd.notna(f["MTBF_mediana_dias"]) else np.nan
    base = (f"{int(f['Eventos'])} eventos y {f['Minutos_paro']:,.0f} min de paro; "
            f"MTTR mediano {f['MTTR_mediana_min']:.0f} min; tendencia: {f['Tendencia'].lower()}.")
    if forma.startswith("Desgaste"):
        t_dias = f["Weibull_eta_dias"] * (-np.log(CONFIABILIDAD_OBJETIVO)) ** (1 / f["Weibull_beta"])
        sem = a_grilla(t_dias * FACTOR_CAL / 7)
        tipo = "Sustitución o restauración"
        just = (f"Falla por desgaste (beta {f['Weibull_beta']:.1f}): el 90 % de las veces dura al menos "
                f"{t_dias * FACTOR_CAL:.0f} días. " + base)
        accion = ("Rutina del operador en cada turno" if sem == 0
                  else f"Preventivo por tiempo: {nombre_frecuencia(sem).lower()}")
        return accion, tipo, sem, just
    if forma.startswith("Aleatoria"):
        sem = a_grilla(mtbf_sem / 2)
        just = (f"Falla aleatoria (beta {f['Weibull_beta']:.1f}): cambiar antes no la evita; conviene "
                f"detectarla a tiempo. MTBF mediano {f['MTBF_mediana_dias'] * FACTOR_CAL:.0f} días. " + base)
        accion = ("Rutina del operador en cada turno" if sem == 0
                  else f"Inspección por condición: {nombre_frecuencia(sem).lower()}")
        return accion, "Inspección", sem, just
    if forma.startswith("En racha"):
        just = (f"Fallas en racha (beta {f['Weibull_beta']:.1f}): tras una falla suele venir otra pronto. "
                "Acortar el preventivo no ayuda; hay que asegurar que la reparación quede bien. " + base)
        return "Revisar calidad de la reparación y estandarizar el ajuste", "Estandarización", np.nan, just
    return "Mantener plan vigente y hacer seguimiento", "Seguimiento", np.nan, \
           "Menos de 10 eventos: no hay datos suficientes para cambiar la frecuencia. " + base


res = a.apply(accion_a, axis=1, result_type="expand")
a[["Accion", "Tipo_tarea_propuesta", "Frecuencia_propuesta_sem", "Justificacion"]] = res
a["Frecuencia_actual_sem"] = a["Frecuencia_tarea_sem"]
a["Frecuencia_propuesta"] = a["Frecuencia_propuesta_sem"].map(nombre_frecuencia)

recA = a[COLUMNAS_RECOMENDACION + ["Forma_falla", "Minutos_paro", "Eventos", "MTTR_mediana_min"]] \
         .sort_values("Puntaje", ascending=False).reset_index(drop=True)

# ---------------------------------------------------------------------------
# 3. Resumen
# ---------------------------------------------------------------------------
print("\n--- Alternativa A: componentes por prioridad ---")
print(pd.crosstab(recA["Maquina"], recA["Prioridad"]).reindex(index=MAQ4, columns=["Crítica", "Alta", "Media", "Baja"]))
print("\n--- Alternativa A: tipo de acción ---")
print(recA["Tipo_tarea_propuesta"].value_counts())
print("\n--- Top 12 de la Alternativa A ---")
print(recA[["Maquina", "Componente", "Puntaje", "Prioridad", "Accion", "Frecuencia_actual_sem"]]
        .head(12).round(1).to_string(index=False))

recA.to_csv("recomendaciones_A.csv", index=False)
print("\nGuardado: recomendaciones_A.csv")

"""
KOF-Smart Maintenance AI — Fase 18: FUSIÓN de las Alternativas A y B
(código puro, sin gráficas)                            Alimenta: pestaña "Mantenimiento"
ESTA ES LA VERSIÓN QUE CORRE EN LA APP Y LA QUE SE PRESENTA.

PREGUNTA QUE RESPONDE: "Juntando cuánto le duele a la línea (A) con lo que dice SAP sobre costo,
avisos y cumplimiento del plan (B), ¿qué componentes atender primero y con qué acción?"

Requiere: recomendaciones_A.csv (Fase 16), recomendaciones_B.csv (Fase 17), hc_componentes.csv,
          plan_pm_limpio.csv y equivalencias (Fase 13)

1. PUNTAJE FINAL = promedio de A y B. Si el componente solo aparece en una fuente, se usa esa
   y queda marcado. El nivel (Crítica, Alta, Media, Baja) sale de la posición en el ranking.

2. COINCIDENCIA: si A y B lo ponen ambos en Crítica o Alta, la prioridad queda "Respaldada por
   ambas fuentes". Si solo una lo prioriza, se indica cuál; esos casos se discuten con mantenimiento.

3. ACCIÓN FINAL (reglas en este orden):
   a. Si su plan no se cumple (B) -> primero, cumplir la frecuencia del plan vigente.
   b. Si las fallas vienen en racha (A) -> NO se acorta el preventivo: se revisa la calidad de la
      reparación y se estandariza el ajuste, aunque B pida acortar.
   c. Si la falla es por desgaste (A) -> preventivo por tiempo con la frecuencia MÁS CORTA entre A y B.
   d. Si la falla es aleatoria (A) -> inspección por condición con la frecuencia más corta entre A y B.
   e. Si A no tiene datos suficientes -> se usa la acción de B (crear, acortar, cambiar tarea).
   f. "Evaluar espaciar" solo se propone si A también lo ve como prioridad baja.

4. DURACIÓN Y ESPECIALIDAD (para el programador semanal, Fase 20): se toma la duración estándar
   de la tarea vigente del plan SAP. Si es una tarea nueva, se usa una duración de referencia
   (marcada "estimada") que se debe confirmar con mantenimiento.

Salidas:
  recomendaciones_fusion.csv   una fila por componente con prioridad, acciones y justificación
  comparacion_AB.csv           posición de cada componente en A y en B
  recomendaciones_A_completas.csv y recomendaciones_B_completas.csv  (misma estructura, para el selector)
  plan_recomendado.json        lo que muestra la pestaña Mantenimiento (versiones fusion, A y B)

IPM: el puntaje final de cada componente es su IPM (Índice de Prioridad de Mantenimiento) por
componente; la columna se llama "IPM" para que coincida con la pestaña.
"""
import json
import os

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from kof_motor_utils import a_grilla, nombre_frecuencia, niveles
from kof_planes_utils import MAQ4, norm

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", None)

DURACION_REFERENCIA_H = {"Inspección": 0.5, "Inspección y ajuste": 0.75, "Limpieza": 0.5,
                         "Sustitución o restauración": 1.5, "Estandarización": 1.0, "Seguimiento": 0.0}
PALABRAS_ELECTRICAS = ["sensor", "fotocel", "electr", "panel", "programa", "variador", "control",
                       "conexi", "cable", "interruptor", "tarjeta", "motor", "relevador", "comunicacion"]

A = pd.read_csv("recomendaciones_A.csv")
B = pd.read_csv("recomendaciones_B.csv")
hc = pd.read_csv("hc_componentes.csv")
plan = pd.read_csv("plan_pm_limpio.csv")

base = ["Id_componente", "Maquina", "Sistema", "Componente"]
f = (hc[base + ["Tarea_vigente", "Tipo_tarea_vigente", "Frecuencia_tarea_sem", "Estado_equivalencia",
                "Minutos_paro", "Eventos", "Avisos", "Costo_correctivo", "Forma_falla", "Sintomas", "Tendencia"]]
       .merge(A.drop(columns=["Maquina", "Sistema", "Componente", "Forma_falla", "Minutos_paro", "Eventos"])
               .add_suffix("_A").rename(columns={"Id_componente_A": "Id_componente"}), on="Id_componente", how="left")
       .merge(B.drop(columns=["Maquina", "Sistema", "Componente", "Avisos", "Costo_correctivo"])
               .add_suffix("_B").rename(columns={"Id_componente_B": "Id_componente"}), on="Id_componente", how="left"))
f = f[f["Puntaje_A"].notna() | f["Puntaje_B"].notna()].copy()

# ---------------------------------------------------------------------------
# 1. Puntaje final, nivel y coincidencia
# ---------------------------------------------------------------------------
# Si solo una fuente ve el componente, su puntaje se multiplica por 0.85: una prioridad que
# sostienen las dos fuentes debe quedar por encima de una que sostiene solo una.
PENALIZACION_UNA_FUENTE = 0.85
f["Puntaje"] = f[["Puntaje_A", "Puntaje_B"]].mean(axis=1)
f.loc[f["Puntaje_A"].isna() | f["Puntaje_B"].isna(), "Puntaje"] *= PENALIZACION_UNA_FUENTE
f["Prioridad"] = niveles(f["Puntaje"])
f["Fuentes"] = np.select([f["Puntaje_A"].notna() & f["Puntaje_B"].notna(), f["Puntaje_A"].notna()],
                         ["A y B", "Solo A (reporte diario)"], "Solo B (SAP)")
alta = ["Crítica", "Alta"]
a_alta, b_alta = f["Prioridad_A"].isin(alta), f["Prioridad_B"].isin(alta)
f["Coincidencia"] = np.select(
    [a_alta & b_alta, a_alta & f["Puntaje_B"].notna(), b_alta & f["Puntaje_A"].notna(), f["Fuentes"] != "A y B"],
    ["Respaldada por ambas fuentes", "Solo la prioriza A (paros)", "Solo la prioriza B (SAP)", "Una sola fuente"],
    "Ninguna la prioriza")


# ---------------------------------------------------------------------------
# 2. Acción final
# ---------------------------------------------------------------------------
def minimo(*valores):
    v = [x for x in valores if pd.notna(x) and x > 0]
    ceros = [x for x in valores if pd.notna(x) and x == 0]
    return 0 if ceros else (min(v) if v else np.nan)


def accion_final(r):
    acciones, tipo, frec = [], None, np.nan
    forma = str(r["Forma_falla"]) if pd.notna(r["Puntaje_A"]) else ""
    brechas = " ".join(str(x) for x in [r.get("Brecha_B"), r.get("Otras_brechas_B")] if pd.notna(x))
    frec_b = r["Frecuencia_propuesta_sem_B"]
    if "Plan no se cumple" in brechas:
        acciones.append("Cumplir la frecuencia del plan vigente")
    if forma.startswith("En racha"):
        acciones.append("Revisar calidad de la reparación y estandarizar el ajuste")
        tipo, frec = "Estandarización", np.nan
        if "Sin cobertura" in brechas:
            acciones.append("Crear procedimiento estándar de ajuste y verificación")
    elif forma.startswith("Desgaste"):
        frec = minimo(r["Frecuencia_propuesta_sem_A"], frec_b)
        tipo = "Sustitución o restauración"
        acciones.append(f"Preventivo por tiempo: {nombre_frecuencia(frec).lower()}")
    elif forma.startswith("Aleatoria"):
        frec = minimo(r["Frecuencia_propuesta_sem_A"], frec_b)
        tipo = "Inspección"
        acciones.append(f"Inspección por condición: {nombre_frecuencia(frec).lower()}")
    elif pd.notna(r["Puntaje_B"]) and r["Brecha_B"] not in ("Sin brecha", "Plan no se cumple"):
        if r["Brecha_B"] != "Posible sobre-mantenimiento" or r.get("Prioridad_A") in (None, np.nan, "Baja") \
                or pd.isna(r.get("Prioridad_A")):
            acciones.append(r["Accion_B"])
            tipo, frec = r["Tipo_tarea_propuesta_B"], frec_b
    if "Tarea débil" in brechas and not forma.startswith("En racha"):
        acciones.append("Cambiar la tarea de revisar a sustituir o restaurar")
    if not acciones:
        acciones.append("Mantener plan vigente y hacer seguimiento")
        tipo = r["Tipo_tarea_vigente"] if pd.notna(r["Tipo_tarea_vigente"]) else "Seguimiento"
    tipo = tipo if isinstance(tipo, str) else "Seguimiento"
    return acciones[0], " | ".join(dict.fromkeys(acciones)), tipo, frec


res = f.apply(accion_final, axis=1, result_type="expand")
f[["Accion_principal", "Acciones", "Tipo_tarea_propuesta", "Frecuencia_propuesta_sem"]] = res
f["Justificacion"] = (f["Justificacion_A"].fillna("") + " " + f["Justificacion_B"].fillna("")).str.strip()

# ---------------------------------------------------------------------------
# 3. Completar cada recomendación: duración, especialidad, canal, clase de orden y texto de tarea.
#    La MISMA función se aplica a la fusión y a las versiones A y B, para que el selector
#    "Excel diario / Archivos SAP / Fusión" de la pestaña reciba siempre la misma estructura.
# ---------------------------------------------------------------------------
dur_plan = plan[plan["Es_tarea"]].groupby(["Maquina", "Tarea"])["Duracion_h"].first()
esp_plan = plan[plan["Es_tarea"]].groupby(["Maquina", "Tarea"])["Especialidad"].first()
ROTATIVOS = ["rodamiento", "reductor", "motor", "transmision", "cardan", "chumacera", "engrane", "pinon"]


def canal(r):
    """Por dónde se ejecuta: Parada programada (va al programador), Rutina del operador (cada turno,
    sin parar la línea), Acción de gestión (procedimiento o estandarización) o Seguimiento."""
    if r["Accion_principal"].startswith("Cumplir"):
        return "Parada programada"
    if r["Frecuencia_propuesta_sem"] == 0:
        return "Rutina del operador"
    if r["Tipo_tarea_propuesta"] == "Estandarización":
        return "Acción de gestión"
    if r["Accion_principal"].startswith(("Mantener", "Evaluar")) or r["Tipo_tarea_propuesta"] == "Seguimiento":
        return "Seguimiento"
    return "Parada programada"


def clase_orden(r):
    """Clase de orden SAP: ZFI2 predictiva si es inspección por condición de un componente rotativo
    (se mide con análisis de vibraciones, como ya se hace en la Llenadora); ZFI1 preventiva para
    las demás tareas en parada; las rutinas y acciones de gestión no generan orden."""
    if r["Canal"] == "Rutina del operador":
        return "Sin orden (rutina del operador)"
    if r["Canal"] == "Acción de gestión":
        return "Sin orden (acción de gestión)"
    if r["Canal"] == "Seguimiento":
        return "Sin orden (seguimiento)"
    if r["Accion_principal"].startswith("Inspección por condición") and any(p in norm(r["Componente"]) for p in ROTATIVOS):
        return "ZFI2 · Predictiva"
    return "ZFI1 · Preventiva"


def tarea_texto(r):
    """Texto corto de la tarea para la tabla 'Plan sugerido de la semana'."""
    acc, comp, vig = r["Accion_principal"], r["Componente"], r["Tarea_vigente"]
    if acc.startswith("Cumplir") and pd.notna(vig):
        return f"{vig.capitalize()} (plan vigente atrasado)"
    if acc.startswith("Inspección por condición"):
        return f"Inspección por condición de {comp.lower()}"
    if acc.startswith("Preventivo por tiempo"):
        return f"Sustitución o restauración de {comp.lower()}"
    if acc.startswith("Crear"):
        return f"{r['Tipo_tarea_propuesta']} de {comp.lower()} (tarea nueva)"
    if acc.startswith("Acortar"):
        return f"Inspección intermedia de {comp.lower()}"
    if acc.startswith("Cambiar la tarea"):
        return f"Cambiar {comp.lower()} en vez de solo revisarlo"
    if acc.startswith("Revisar calidad"):
        return f"Estandarizar reparación y ajuste de {comp.lower()}"
    return f"{acc} ({comp.lower()})"


def completar(d):
    d = d.copy()
    d["IPM"] = d["Puntaje"].round(1)           # el puntaje final ES el IPM por componente
    d["Frecuencia_actual"] = d["Frecuencia_tarea_sem"].map(lambda s: nombre_frecuencia(s) if pd.notna(s) else "Sin tarea")
    d["Frecuencia_propuesta"] = d["Frecuencia_propuesta_sem"].map(nombre_frecuencia)
    d["Duracion_h"] = [dur_plan.get((m, t), np.nan) for m, t in zip(d["Maquina"], d["Tarea_vigente"])]
    d["Duracion_estimada"] = d["Duracion_h"].isna() | ~d["Accion_principal"].str.startswith("Cumplir")
    d.loc[d["Duracion_estimada"], "Duracion_h"] = d.loc[d["Duracion_estimada"], "Tipo_tarea_propuesta"] \
        .map(DURACION_REFERENCIA_H).fillna(0.5)
    d["Especialidad"] = [esp_plan.get((m, t), np.nan) for m, t in zip(d["Maquina"], d["Tarea_vigente"])]
    electrica = d["Componente"].map(lambda c: any(p in norm(c) for p in PALABRAS_ELECTRICAS))
    d["Especialidad"] = d["Especialidad"].fillna(pd.Series(np.where(electrica, "Eléctrica", "Mecánica"), index=d.index))
    d["Canal"] = d.apply(canal, axis=1)
    d["Clase_orden"] = d.apply(clase_orden, axis=1)
    d["Tarea_sugerida"] = d.apply(tarea_texto, axis=1)
    d["Va_al_programador"] = (d["Canal"] == "Parada programada") & d["Prioridad"].isin(["Crítica", "Alta"])
    d["Maquina"] = pd.Categorical(d["Maquina"], MAQ4, ordered=True)
    d = d.sort_values("Puntaje", ascending=False).reset_index(drop=True)
    d["Ranking"] = range(1, len(d) + 1)
    return d


def version(sufijo):
    """Versión A o B con la misma estructura de la fusión (para el selector de la pestaña)."""
    d = f[f[f"Puntaje_{sufijo}"].notna()].copy()
    d["Puntaje"], d["Prioridad"] = d[f"Puntaje_{sufijo}"], d[f"Prioridad_{sufijo}"]
    d["Accion_principal"] = d[f"Accion_{sufijo}"]
    d["Acciones"] = d[f"Accion_{sufijo}"]
    d["Tipo_tarea_propuesta"] = d[f"Tipo_tarea_propuesta_{sufijo}"].fillna("Seguimiento")
    d["Frecuencia_propuesta_sem"] = d[f"Frecuencia_propuesta_sem_{sufijo}"]
    d["Justificacion"] = d[f"Justificacion_{sufijo}"]
    d["Coincidencia"] = "No aplica (una sola alternativa)"
    return completar(d)


f = completar(f)
versiones = {"fusion": f, "A": version("A"), "B": version("B")}

# ---------------------------------------------------------------------------
# 4. Comparación A vs B (para el documento: ¿las dos alternativas coinciden?)
# ---------------------------------------------------------------------------
ambas = f[f["Fuentes"] == "A y B"]
rho, pval = spearmanr(ambas["Puntaje_A"], ambas["Puntaje_B"])
topA = set(f.nlargest(15, "Puntaje_A")["Id_componente"])
topB = set(f.nlargest(15, "Puntaje_B")["Id_componente"])
print(f"Componentes en el motor: {len(f)} | en ambas fuentes: {len(ambas)}")
print(f"Correlación de Spearman entre los puntajes A y B: {rho:.2f} (p = {pval:.3f})")
print(f"Coinciden {len(topA & topB)} de los 15 primeros de A y de B")
print("\n--- Coincidencia de las fuentes en componentes Crítica o Alta ---")
print(f[f["Prioridad"].isin(alta)]["Coincidencia"].value_counts())

print("\n--- PLAN RECOMENDADO: top 15 ---")
print(f[["Ranking", "Maquina", "Componente", "Puntaje", "Prioridad", "Coincidencia", "Accion_principal",
         "Frecuencia_actual", "Frecuencia_propuesta"]].head(15).round(1).to_string(index=False))
print("\n--- Prioridad por máquina ---")
print(pd.crosstab(f["Maquina"], f["Prioridad"]).reindex(columns=["Crítica", "Alta", "Media", "Baja"]))
print("\n--- Canal de ejecución de las prioridades Crítica y Alta ---")
print(f[f["Prioridad"].isin(alta)]["Canal"].value_counts())
print(f"Tareas que pasan al programador semanal: {f['Va_al_programador'].sum()}")
v = f[f["Componente"] == "Válvula de llenado"].iloc[0]
print(f"\nEjemplo, válvula de llenado -> {v['Acciones']}\n  {v['Justificacion']}")

# ---------------------------------------------------------------------------
# 5. Guardar
# ---------------------------------------------------------------------------
columnas = ["Ranking", "Id_componente", "Maquina", "Sistema", "Componente", "IPM", "Puntaje", "Prioridad", "Fuentes",
            "Coincidencia", "Puntaje_A", "Prioridad_A", "Puntaje_B", "Prioridad_B", "Accion_principal", "Acciones",
            "Tipo_tarea_propuesta", "Tarea_vigente", "Frecuencia_actual", "Frecuencia_tarea_sem",
            "Frecuencia_propuesta", "Frecuencia_propuesta_sem", "Duracion_h", "Duracion_estimada", "Especialidad",
            "Canal", "Clase_orden", "Tarea_sugerida", "Va_al_programador", "Justificacion", "Confianza_B", "Estado_equivalencia", "Minutos_paro", "Eventos",
            "Avisos", "Costo_correctivo", "Forma_falla", "Sintomas", "Tendencia"]
f[columnas].to_csv("recomendaciones_fusion.csv", index=False)
f[["Id_componente", "Maquina", "Componente", "Puntaje_A", "Prioridad_A", "Puntaje_B", "Prioridad_B",
   "Coincidencia"]].to_csv("comparacion_AB.csv", index=False)

versiones["A"][columnas].to_csv("recomendaciones_A_completas.csv", index=False)
versiones["B"][columnas].to_csv("recomendaciones_B_completas.csv", index=False)

# Un solo JSON con las tres versiones: el selector de la pestaña solo cambia de llave
salida = {"resumen": {"componentes": int(len(f)), "spearman_A_B": round(float(rho), 2),
                      "coinciden_top15": len(topA & topB),
                      "por_prioridad": f["Prioridad"].value_counts().to_dict()},
          "versiones": {k: json.loads(v[columnas].assign(Maquina=v["Maquina"].astype(str))
                                      .to_json(orient="records", force_ascii=False))
                        for k, v in versiones.items()}}
with open("plan_recomendado.json", "w", encoding="utf-8") as fh:
    json.dump(salida, fh, ensure_ascii=False, indent=1)
print("\n--- Clase de orden de lo que va a parada (fusión) ---")
print(f[f["Va_al_programador"]]["Clase_orden"].value_counts())
print("\nGuardado: recomendaciones_fusion.csv, recomendaciones_A_completas.csv, recomendaciones_B_completas.csv,"
      " comparacion_AB.csv, plan_recomendado.json (con las versiones fusion, A y B)")

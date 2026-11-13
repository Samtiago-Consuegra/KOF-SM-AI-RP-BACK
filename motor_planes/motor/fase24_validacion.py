"""
KOF-Smart Maintenance AI — Fase 24: VALIDACIÓN del motor con fallas mayores y ACR
(código puro)                               Alimenta: el documento final (no la app)

PREGUNTA QUE RESPONDE: "¿El motor prioriza de verdad los componentes que causan las fallas graves?"

Las fallas mayores (consolidado, más de 60 min) y los ACR (más de 180 min) son registros de la planta
que el motor NO usó para calcular el IPM. Por eso sirven para contrastarlo. Se hacen tres pruebas:

PRUEBA 1 · CONCORDANCIA (todo el periodo)
  ¿Los componentes con más minutos en fallas mayores están arriba en el ranking del motor?
  - Top 5 y top 10 por minutos de fallas mayores, con su puesto en el ranking del motor.
  - Captura: % de los minutos de fallas mayores que cae en componentes de prioridad Crítica o Alta,
    frente al % de componentes que tienen esa prioridad (lo que capturaría un ranking al azar).
    La razón entre ambos es el "lift": cuántas veces mejor que el azar.
  - Correlación de Spearman entre el IPM y los minutos de fallas mayores.
  LIMITACIÓN: una falla mayor también aparece, con sus minutos, en el reporte diario que usa el motor;
  las fuentes describen los mismos eventos. Esta prueba mide coherencia, no capacidad de anticipar.

PRUEBA 2 · VALIDACIÓN TEMPORAL (la prueba más exigente)
  Se calcula un ranking SOLO con el reporte diario anterior a una fecha de corte (por defecto el
  1 de abril de 2026), con la misma lógica de la Alternativa A, y se mide si las fallas mayores
  POSTERIORES al corte ocurrieron en los componentes que ese ranking ya ponía arriba.
  Así el motor no "ve" las fallas con las que se le evalúa: es una prueba de anticipación.

PRUEBA 3 · ACR UNO POR UNO
  Para cada ACR de las cuatro máquinas: en qué puesto y con qué prioridad tenía el motor ese componente.

SALIDAS: validacion.json (indicadores) y validacion.xlsx (tablas de cada prueba)
"""
import json

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from kof_motor_utils import rango_pct

pd.set_option("display.width", 220)
pd.set_option("display.max_colwidth", 70)

CORTE = pd.Timestamp("2026-04-01")      # fecha de corte de la validación temporal
PROPORCION_ALTA = 0.30                  # Crítica + Alta = 30 % superior del ranking (ver kof_motor_utils)

fus = pd.read_csv("recomendaciones_fusion.csv")
fallas = pd.read_csv("fallas_mayores.csv", parse_dates=["Fecha"])
eventos = pd.read_csv("acr_eventos.csv", parse_dates=["Fecha"])
diario = pd.read_csv("base_diaria_componentes.csv", parse_dates=["Fecha"])
fm = fallas.dropna(subset=["Id_componente"])
res = {}

# ---------------------------------------------------------------------------
# Prueba 1 · Concordancia
# ---------------------------------------------------------------------------
min_fm = fm.groupby("Id_componente")["Minutos"].agg(["size", "sum"]).rename(columns={"size": "Fallas_mayores", "sum": "Minutos_fm"})
t = fus[["Id_componente", "Maquina", "Componente", "Ranking", "IPM", "Prioridad"]].merge(min_fm, on="Id_componente", how="left") \
       .fillna({"Fallas_mayores": 0, "Minutos_fm": 0})
alta = t["Prioridad"].isin(["Crítica", "Alta"])
captura = t.loc[alta, "Minutos_fm"].sum() / t["Minutos_fm"].sum()
proporcion = alta.mean()
rho, p = spearmanr(t["IPM"], t["Minutos_fm"])
top10 = t.sort_values("Minutos_fm", ascending=False).head(10)
res["concordancia"] = {
    "fallas_mayores": int(len(fm)), "minutos_fallas_mayores": round(float(fm["Minutos"].sum()), 1),
    "componentes_evaluados": int(len(t)),
    "captura_critica_alta_pct": round(100 * captura, 1), "componentes_critica_alta_pct": round(100 * proporcion, 1),
    "lift": round(captura / proporcion, 2), "spearman_ipm_vs_minutos": round(float(rho), 2), "spearman_p": float(p),
    "top5_puestos_en_ranking": top10.head(5)["Ranking"].astype(int).tolist(),
    "top5_todos_critica_alta": bool(top10.head(5)["Prioridad"].isin(["Crítica", "Alta"]).all())}
print("=== PRUEBA 1 · CONCORDANCIA ===")
print(top10[["Maquina", "Componente", "Fallas_mayores", "Minutos_fm", "Ranking", "Prioridad"]].round(0).to_string(index=False))
print(f"\nLos componentes Crítica o Alta son el {100 * proporcion:.0f} % del total y concentran el "
      f"{100 * captura:.0f} % de los minutos de fallas mayores (lift {captura / proporcion:.1f}).")
print(f"Spearman IPM vs. minutos de fallas mayores: {rho:.2f} (p = {p:.1e})")

# Curva de captura: % de minutos de fallas mayores capturados al recorrer el ranking
curva = t.sort_values("Ranking")[["Ranking", "Minutos_fm"]].copy()
curva["Pct_componentes"] = 100 * np.arange(1, len(curva) + 1) / len(curva)
curva["Pct_minutos_capturados"] = 100 * curva["Minutos_fm"].cumsum() / curva["Minutos_fm"].sum()

# ---------------------------------------------------------------------------
# Prueba 2 · Validación temporal (ranking con datos anteriores al corte)
# ---------------------------------------------------------------------------
d = diario[diario["Fecha"] < CORTE].dropna(subset=["Id_componente"]).copy()
d["Momento"] = d["Fecha"] + pd.to_timedelta(d["Hora"], unit="h")
d = d.sort_values(["Id_componente", "Momento"])
nuevo = d.groupby("Id_componente")["Momento"].diff().dt.total_seconds().div(3600)
d["Evento"] = (nuevo.isna() | (nuevo > 1)).groupby(d["Id_componente"]).cumsum()
ev_pre = d.groupby(["Id_componente", "Evento"])["Minutos de paro"].sum().reset_index()
pre = ev_pre.groupby("Id_componente")["Minutos de paro"].agg(["size", "sum", "median"])
pre.columns = ["Eventos", "Minutos", "MTTR_mediana"]
pre["Puntaje"] = 0.60 * rango_pct(pre["Minutos"]) + 0.25 * rango_pct(pre["Eventos"]) + 0.15 * rango_pct(pre["MTTR_mediana"])
pre = pre.sort_values("Puntaje", ascending=False).reset_index()
pre["Ranking_pre"] = range(1, len(pre) + 1)
pre["Alta_pre"] = pre["Ranking_pre"] <= np.ceil(PROPORCION_ALTA * len(pre))
post = fm[fm["Fecha"] >= CORTE].groupby("Id_componente")["Minutos"].agg(["size", "sum"]).rename(
    columns={"size": "Fallas_mayores_post", "sum": "Minutos_post"})
v = pre.merge(post, on="Id_componente", how="left").fillna({"Fallas_mayores_post": 0, "Minutos_post": 0})
v = v.merge(fus[["Id_componente", "Maquina", "Componente"]], on="Id_componente", how="left")
fuera = post[~post.index.isin(pre["Id_componente"])]["Minutos_post"].sum() if len(post) else 0   # sin historia antes del corte
captura_t = v.loc[v["Alta_pre"], "Minutos_post"].sum() / (v["Minutos_post"].sum() + fuera)
precision10 = (v.head(10)["Fallas_mayores_post"] > 0).mean()
base_p10 = (v["Fallas_mayores_post"] > 0).mean()
rho_t, p_t = spearmanr(v["Puntaje"], v["Minutos_post"])
res["temporal"] = {
    "fecha_corte": CORTE.strftime("%Y-%m-%d"),
    "fallas_mayores_despues_del_corte": int(fm[fm["Fecha"] >= CORTE].shape[0]),
    "minutos_despues_del_corte": round(float(fm.loc[fm["Fecha"] >= CORTE, "Minutos"].sum()), 1),
    "captura_30pct_superior_pct": round(100 * captura_t, 1), "esperado_al_azar_pct": round(100 * PROPORCION_ALTA, 1),
    "lift": round(captura_t / PROPORCION_ALTA, 2),
    "precision_top10_pct": round(100 * precision10, 1), "precision_al_azar_pct": round(100 * base_p10, 1),
    "spearman": round(float(rho_t), 2), "spearman_p": float(p_t)}
print("\n=== PRUEBA 2 · VALIDACIÓN TEMPORAL ===")
print(f"Ranking calculado solo con datos anteriores al {CORTE:%d-%m-%Y}; se evalúa con "
      f"{res['temporal']['fallas_mayores_despues_del_corte']} fallas mayores posteriores.")
print(f"El 30 % superior de ese ranking concentró el {100 * captura_t:.0f} % de los minutos de fallas mayores "
      f"posteriores (al azar sería 30 %; lift {captura_t / PROPORCION_ALTA:.1f}).")
print(f"De los 10 primeros, el {100 * precision10:.0f} % tuvo al menos una falla mayor después del corte "
      f"(al azar: {100 * base_p10:.0f} %). Spearman: {rho_t:.2f} (p = {p_t:.1e}).")
print(v.head(10)[["Maquina", "Componente", "Ranking_pre", "Fallas_mayores_post", "Minutos_post"]].round(0).to_string(index=False))
curva_t = v[["Ranking_pre", "Minutos_post"]].copy()
curva_t["Pct_componentes"] = 100 * np.arange(1, len(curva_t) + 1) / len(curva_t)
curva_t["Pct_minutos_capturados"] = 100 * curva_t["Minutos_post"].cumsum() / (curva_t["Minutos_post"].sum() + fuera)

# ---------------------------------------------------------------------------
# Prueba 3 · ACR uno por uno
# ---------------------------------------------------------------------------
acr = eventos[eventos["En_alcance"].fillna(False).astype(bool)].merge(
    fus[["Id_componente", "Ranking", "Prioridad", "IPM"]], on="Id_componente", how="left")
acr = acr.merge(v[["Id_componente", "Ranking_pre"]], on="Id_componente", how="left")
acr["Antes_del_corte_en_30pct"] = np.where(acr["Fecha"] >= CORTE, acr["Ranking_pre"] <= np.ceil(PROPORCION_ALTA * len(pre)), np.nan)
acr = acr.sort_values("Fecha")
en_alta = acr["Prioridad"].isin(["Crítica", "Alta"])
res["acr"] = {"acr_evaluados": int(len(acr)), "en_critica_o_alta": int(en_alta.sum()),
              "pct_en_critica_o_alta": round(100 * en_alta.mean(), 1)}
print("\n=== PRUEBA 3 · ACR UNO POR UNO ===")
print(acr[["Fecha", "Maquina", "Componente", "Duracion_min", "Ranking", "Prioridad", "Ranking_pre"]].to_string(index=False))
print(f"\n{en_alta.sum()} de {len(acr)} ACR ocurrieron en componentes que el motor pone en prioridad Crítica o Alta.")

# ---------------------------------------------------------------------------
# Guardar
# ---------------------------------------------------------------------------
with open("validacion.json", "w", encoding="utf-8") as fh:
    json.dump(res, fh, ensure_ascii=False, indent=1)
with pd.ExcelWriter("validacion.xlsx") as xls:
    pd.DataFrame([{"Prueba": k, **val} for k, val in res.items()]).to_excel(xls, sheet_name="Resumen", index=False)
    t.sort_values("Minutos_fm", ascending=False).to_excel(xls, sheet_name="1 Concordancia", index=False)
    curva.to_excel(xls, sheet_name="1 Curva de captura", index=False)
    v.to_excel(xls, sheet_name="2 Temporal", index=False)
    curva_t.to_excel(xls, sheet_name="2 Curva temporal", index=False)
    acr.drop(columns=["Similitud", "Linea_3", "Borrador"], errors="ignore").to_excel(xls, sheet_name="3 ACR", index=False)
curva.to_csv("validacion_curva.csv", index=False)
curva_t.to_csv("validacion_curva_temporal.csv", index=False)
print("\nGuardado: validacion.json, validacion.xlsx")

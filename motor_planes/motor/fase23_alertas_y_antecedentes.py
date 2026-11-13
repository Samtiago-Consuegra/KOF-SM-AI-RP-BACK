"""
KOF-Smart Maintenance AI — Fase 23: ALERTAS NUEVAS y ANTECEDENTES para la historia clínica
(código puro)        Alimenta: carrusel de alertas (Planes) y sección "Antecedentes" de la Historia clínica

PREGUNTA QUE RESPONDE: "¿Qué dicen las fallas graves, los ACR y los repuestos sobre lo que hay que
cuidar esta semana, y qué antecedentes tiene cada componente?"

No modifica ninguna fase anterior: lee sus salidas, AGREGA alertas a alertas.json (y quita las que
agregó en una corrida anterior, para no duplicar) y escribe historia_clinica_acr.json.

ALERTAS NUEVAS
  1. Riesgo de repuesto         Un componente con repuesto crítico (Fase 22) tiene trabajo en el plan de la
                                semana o una alerta activa. Pide verificar el stock antes de la parada.
  2. Falla mayor en racha       2 o más fallas mayores del mismo componente en una ventana de 30 días,
                                la última en los 45 días previos a la fecha de referencia.
  3. Después de mantenimiento   Tras una orden preventiva larga (8 h o más) ejecutada en los últimos 60 días,
     mayor                      los minutos de paro de los 30 días siguientes superan en 50 % a los 30 días
                                previos (idea propuesta por la planta en el ACR de la válvula de llenado).
  4. Acción ACR sin cierre      Acciones de fondo de un ACR con fecha cumplida en los últimos 120 días y sin
                                cierre registrado en el ACR. Se pide CONFIRMAR, porque los ACR no siempre
                                registran el cierre.

ANTECEDENTES PARA LA HISTORIA CLÍNICA (historia_clinica_acr.json)
  Por máquina: fallas mayores, causas registradas por la planta, ACR y comparación antes y después de
  cada mantenimiento mayor (los "30 días pre/post" que propuso la planta).
  Por componente: fallas mayores, ACR con causa raíz y acciones, repuesto crítico e IPM con consecuencia.

IPM CON CONSECUENCIA (propuesta; NO cambia el ranking actual de la Fase 18)
  Consecuencia (0 a 100) = 60 % minutos en fallas mayores + 40 % número de ACR (posición relativa).
  IPM con consecuencia   = 80 % IPM actual + 20 % consecuencia.
  Se entrega aparte para compararlo con el IPM actual y decidir si se adopta.

SALIDAS: alertas.json (con las alertas nuevas), alertas_acr.csv, historia_clinica_acr.json,
         ipm_con_consecuencia.csv, pre_post_mantenimiento.csv
"""
import json
import os

import numpy as np
import pandas as pd
from kof_motor_utils import rango_pct, niveles
from kof_planes_utils import MAQ4, ARCHIVO_EQUIV_VALIDADAS

pd.set_option("display.width", 220)
pd.set_option("display.max_colwidth", 120)

TIPOS_NUEVOS = ["Riesgo de repuesto", "Falla mayor en racha", "Después de mantenimiento mayor", "Acción ACR sin cierre"]
HORAS_MANTENIMIENTO_MAYOR = 8
VENTANA_PRE_POST = 30
ORDEN_NIVEL = {"Crítica": 0, "Alta": 1, "Media": 2, "Baja": 3}

fallas = pd.read_csv("fallas_mayores.csv", parse_dates=["Fecha"])
eventos = pd.read_csv("acr_eventos.csv", parse_dates=["Fecha"])
acciones = pd.read_csv("acr_acciones.csv", parse_dates=["Fecha_inicio", "Fecha_fin", "Fecha"])
repuestos = pd.read_csv("repuestos_criticos.csv")
fus = pd.read_csv("recomendaciones_fusion.csv")
catalogo = pd.read_csv("catalogo_componentes.csv")
ot = pd.read_csv("ot_limpias.csv", parse_dates=["Fecha_programada"])
cump = pd.read_csv("cump_planes.csv")
plan = pd.read_csv("plan_pm_limpio.csv")
diario = pd.read_csv("base_diaria_componentes.csv", parse_dates=["Fecha"])
with open("alertas.json", encoding="utf-8") as fh:
    alertas_json = json.load(fh)
with open("plan_semanal.json", encoding="utf-8") as fh:
    semana = json.load(fh)
ref = pd.Timestamp(alertas_json["fecha_referencia"])

nivel = fus.set_index("Id_componente")["Prioridad"].to_dict()
ipm = fus.set_index("Id_componente")["IPM"].to_dict()
nombre = catalogo.set_index("Id_componente")["Componente"].to_dict()
maquina_de = catalogo.set_index("Id_componente")["Maquina"].to_dict()
ev = eventos[eventos["En_alcance"].fillna(False).astype(bool)].copy()
nuevas = []


def alerta(tipo, idc, maquina, mensaje, evidencia, confianza, accion, enlace):
    nuevas.append({"Id_alerta": None, "Tipo": tipo, "Maquina": maquina, "Componente": nombre.get(idc),
                   "Id_componente": idc, "Prioridad": nivel.get(idc, "Media"), "IPM": ipm.get(idc),
                   "Mensaje": mensaje, "Evidencia": evidencia, "Confianza": confianza,
                   "Accion_sugerida": accion, "Enlace": enlace, "Mostrar": False,
                   "Fecha_referencia": alertas_json["fecha_referencia"]})


# ---------------------------------------------------------------------------
# 1. Falla mayor en racha
# ---------------------------------------------------------------------------
fm = fallas.dropna(subset=["Id_componente"]).sort_values("Fecha")
for idc, g in fm.groupby("Id_componente"):
    recientes = g[g["Fecha"] > ref - pd.Timedelta(days=45)]
    if recientes.empty:
        continue
    ultima = recientes["Fecha"].max()
    ventana = g[(g["Fecha"] > ultima - pd.Timedelta(days=VENTANA_PRE_POST)) & (g["Fecha"] <= ultima)]
    if len(ventana) >= 2:
        fechas = ", ".join(ventana["Fecha"].dt.strftime("%d-%m"))
        causa = ventana["Causa_falla"].dropna().mode()
        extra = f" La causa más registrada fue \"{causa.iat[0].lower()}\"." if len(causa) else ""
        alerta("Falla mayor en racha", idc, maquina_de.get(idc),
               f"{nombre.get(idc)} ({maquina_de.get(idc)}) tuvo {len(ventana)} fallas mayores en 30 días "
               f"({ventana['Minutos'].sum():,.0f} min).{extra} Asegura que la reparación quede completa antes de liberar el equipo.",
               f"Fallas de más de 60 min: {fechas}", "Alta" if len(ventana) >= 3 else "Media",
               "Revisar la reparación y sus repuestos", "componente")

# ---------------------------------------------------------------------------
# 2. Después de un mantenimiento mayor (comparación 30 días antes y después)
# ---------------------------------------------------------------------------
horas_plan = cump.set_index("Plan")["Horas_por_orden"]
mayores = ot[ot["En_alcance"] & (ot["Tipo_orden"] == "Preventiva") & (ot["Estado"] == "Ejecutada")].copy()
mayores["Horas"] = mayores["Plan"].map(horas_plan)
mayores = mayores[mayores["Horas"] >= HORAS_MANTENIMIENTO_MAYOR]
eq_file = ARCHIVO_EQUIV_VALIDADAS if os.path.exists(ARCHIVO_EQUIV_VALIDADAS) else "equivalencias_borrador.xlsx"
eq = pd.read_excel(eq_file)
if "Rango" in eq:
    eq = eq[(eq["Rango"] == 1) & (eq["Similitud"] >= 0.34)]
eq = eq.merge(plan[["Id_tarea", "Plan"]], on="Id_tarea", how="left")
componentes_plan = eq.groupby("Plan")["Id_componente"].apply(list).to_dict()
tarea_plan = plan[plan["Es_tarea"]].groupby("Plan")["Tarea"].first().to_dict()
fin_diario, inicio_diario = diario["Fecha"].max(), diario["Fecha"].min()

pre_post = []
for _, o in mayores.iterrows():
    d = o["Fecha_programada"]
    if d + pd.Timedelta(days=VENTANA_PRE_POST) > fin_diario or d - pd.Timedelta(days=VENTANA_PRE_POST) < inicio_diario:
        continue                       # se necesitan 30 días de reporte diario antes y después
    for alcance, filtro in [("Máquina", diario["Maquina"] == o["Maquina"])] + \
            [(idc, diario["Id_componente"] == idc) for idc in componentes_plan.get(o["Plan"], [])]:
        x = diario[filtro]
        antes = x[(x["Fecha"] >= d - pd.Timedelta(days=VENTANA_PRE_POST)) & (x["Fecha"] < d)]
        despues = x[(x["Fecha"] > d) & (x["Fecha"] <= d + pd.Timedelta(days=VENTANA_PRE_POST))]
        pre_post.append({"Orden": o["Orden"], "Plan": o["Plan"], "Tarea": tarea_plan.get(o["Plan"], o["Texto"]),
                         "Maquina": o["Maquina"], "Fecha": d, "Horas_orden": o["Horas"],
                         "Alcance": "Máquina" if alcance == "Máquina" else nombre.get(alcance),
                         "Id_componente": None if alcance == "Máquina" else alcance,
                         "Minutos_30d_antes": antes["Minutos de paro"].sum(), "Minutos_30d_despues": despues["Minutos de paro"].sum(),
                         "Registros_antes": len(antes), "Registros_despues": len(despues)})
pp = pd.DataFrame(pre_post)
if len(pp):
    pp["Cambio_pct"] = 100 * (pp["Minutos_30d_despues"] - pp["Minutos_30d_antes"]) / pp["Minutos_30d_antes"].replace(0, np.nan)
    recientes = pp[(pp["Fecha"] > ref - pd.Timedelta(days=60 + VENTANA_PRE_POST)) & (pp["Alcance"] != "Máquina")]
    for _, r in recientes.iterrows():
        if r["Registros_despues"] >= 3 and r["Minutos_30d_despues"] >= 1.5 * max(r["Minutos_30d_antes"], 1):
            alerta("Después de mantenimiento mayor", r["Id_componente"], r["Maquina"],
                   f"Después de \"{str(r['Tarea']).capitalize()}\" ({r['Fecha']:%d-%m-%Y}), los paros por "
                   f"{str(r['Alcance']).lower()} pasaron de {r['Minutos_30d_antes']:,.0f} a {r['Minutos_30d_despues']:,.0f} min "
                   "en 30 días. Revisa el montaje y los repuestos usados en ese mantenimiento.",
                   f"Orden {r['Orden']} ({r['Horas_orden']:.0f} h); 30 días antes y después", "Media",
                   "Inspección posterior al mantenimiento", "componente")

# ---------------------------------------------------------------------------
# 3. Riesgo de repuesto
# ---------------------------------------------------------------------------
tareas_semana = pd.DataFrame(semana["versiones"]["fusion"]["tareas_sugeridas"])
paradas = {p["Id_parada"]: p for p in semana["versiones"]["fusion"]["paradas"]}
alertadas = {a["Id_componente"] for a in alertas_json["alertas"] if a.get("Id_componente")} | \
            {a["Id_componente"] for a in nuevas if a.get("Id_componente")}
for _, r in repuestos.iterrows():
    idc = r["Id_componente"]
    t = tareas_semana[(tareas_semana.get("Id_componente") == idc) & tareas_semana["Id_parada"].notna()] \
        if len(tareas_semana) else pd.DataFrame()
    if len(t):
        p = paradas.get(t.iloc[0]["Id_parada"], {})
        donde = f"Esta semana hay trabajo en {r['Componente'].lower()} ({r['Maquina']}) en la parada \"{p.get('Titulo', '')}\" del {str(p.get('Dia', '')).lower()}."
    elif idc in alertadas:
        donde = f"{r['Componente']} ({r['Maquina']}) tiene una alerta activa esta semana."
    else:
        continue
    alerta("Riesgo de repuesto", idc, r["Maquina"],
           f"{donde} Su repuesto ha sido crítico antes: verifica que haya stock suficiente y que sea original antes de intervenirlo.",
           r["Motivo"], "Alta" if r["Nivel"] == "Alto" else "Media", "Verificar stock del repuesto", "componente")

# ---------------------------------------------------------------------------
# 4. Acciones de ACR con fecha cumplida y sin cierre registrado
# ---------------------------------------------------------------------------
pend = acciones[(acciones["Tipo_accion"] == "De fondo (preventiva o mejora)") &
                (acciones["Estado"] == "Fecha cumplida sin cierre registrado") &
                (acciones["Fecha_fin"] > ref - pd.Timedelta(days=120)) & acciones["Id_componente"].notna()]
for (archivo, idc), g in pend.groupby(["Archivo", "Id_componente"]):
    e = g.iloc[0]
    lista = "; ".join(g["Actividad"].str.slice(0, 80).head(3))
    alerta("Acción ACR sin cierre", idc, e["Maquina"],
           f"El ACR del {e['Fecha']:%d-%m-%Y} de {nombre.get(idc, '').lower()} tiene {len(g)} acción(es) de fondo con fecha "
           f"cumplida y sin cierre registrado: {lista}. Confirma si ya se hicieron.",
           f"Fechas de cierre comprometidas hasta el {g['Fecha_fin'].max():%d-%m-%Y}", "Media",
           "Confirmar el cierre de las acciones", "componente")

# ---------------------------------------------------------------------------
# 5. Agregar las alertas a alertas.json (sin duplicar) y elegir cuál se destaca
# ---------------------------------------------------------------------------
na = pd.DataFrame(nuevas)
if len(na):
    na["_n"] = na["Prioridad"].map(ORDEN_NIVEL).fillna(2)
    na["_t"] = na["Tipo"].map({t: i for i, t in enumerate(TIPOS_NUEVOS)})
    na = na.sort_values(["_n", "_t", "IPM"], ascending=[True, True, False]).drop(columns=["_n", "_t"])
    na["Id_alerta"] = [f"AC-{i + 1:03d}" for i in range(len(na))]
    na = na.astype(object).where(na.notna(), None)
    nuevas = na.to_dict("records")
    nuevas[0]["Mostrar"] = True                     # la más importante entra al grupo destacado
base = [a for a in alertas_json["alertas"] if a["Tipo"] not in TIPOS_NUEVOS]
visibles = [a for a in base if a["Mostrar"]]
if nuevas and len(visibles) >= 5:
    no_oportunidad = [a for a in visibles if a["Tipo"] != "Oportunidad"]
    (no_oportunidad or visibles)[-1]["Mostrar"] = False
alertas_json["alertas"] = base + nuevas
with open("alertas.json", "w", encoding="utf-8") as fh:
    json.dump(alertas_json, fh, ensure_ascii=False, indent=1, default=str)
pd.DataFrame(nuevas).to_csv("alertas_acr.csv", index=False)
print(f"Alertas nuevas: {len(nuevas)}")
if len(nuevas):
    print(pd.DataFrame(nuevas)["Tipo"].value_counts().to_string())
    for a in nuevas[:6]:
        print(f"\n[{a['Prioridad']} · {a['Tipo']} · {a['Maquina']}]\n  {a['Mensaje']}\n  Evidencia: {a['Evidencia']}")

# ---------------------------------------------------------------------------
# 6. IPM con consecuencia (propuesta, aparte del ranking actual)
# ---------------------------------------------------------------------------
cons = fm.groupby("Id_componente").agg(Fallas_mayores=("Fecha", "size"), Minutos_fallas_mayores=("Minutos", "sum"))
n_acr = ev.dropna(subset=["Id_componente"]).groupby("Id_componente").size().rename("ACR")
ipmc = fus[["Id_componente", "Maquina", "Componente", "IPM", "Prioridad", "Ranking"]].merge(
    cons, on="Id_componente", how="left").merge(n_acr, on="Id_componente", how="left").fillna(
    {"Fallas_mayores": 0, "Minutos_fallas_mayores": 0, "ACR": 0})
ipmc["Consecuencia"] = 0.6 * rango_pct(ipmc["Minutos_fallas_mayores"]) + 0.4 * rango_pct(ipmc["ACR"])
ipmc["IPM_con_consecuencia"] = 0.8 * ipmc["IPM"] + 0.2 * ipmc["Consecuencia"]
ipmc["Prioridad_con_consecuencia"] = niveles(ipmc["IPM_con_consecuencia"])
ipmc["Ranking_con_consecuencia"] = ipmc["IPM_con_consecuencia"].rank(ascending=False, method="first").astype(int)
ipmc["Cambio_de_nivel"] = np.where(ipmc["Prioridad"] == ipmc["Prioridad_con_consecuencia"], "Igual",
                                   ipmc["Prioridad"] + " → " + ipmc["Prioridad_con_consecuencia"])
ipmc = ipmc.sort_values("Ranking_con_consecuencia")
cambian = ipmc[ipmc["Cambio_de_nivel"] != "Igual"]
print(f"\nIPM con consecuencia: {len(cambian)} componentes cambian de nivel")
print(cambian[["Maquina", "Componente", "Ranking", "Ranking_con_consecuencia", "Cambio_de_nivel",
               "Fallas_mayores", "ACR"]].head(12).to_string(index=False))

# ---------------------------------------------------------------------------
# 7. Antecedentes para la historia clínica
# ---------------------------------------------------------------------------
def limpio(v):
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (float, np.floating)):
        return None if np.isnan(v) else round(float(v), 2)
    if isinstance(v, (np.integer,)):
        return int(v)
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else v


def registros(df, cols):
    return [{k: limpio(v) for k, v in r.items()} for r in df[cols].to_dict("records")]


rep_idx = repuestos.set_index("Id_componente")
ipmc_idx = ipmc.set_index("Id_componente")
salida = {"datos_hasta": ref.strftime("%Y-%m-%d"), "maquinas": [], "otros_equipos_l4": registros(
    eventos[(~eventos["En_alcance"].fillna(False).astype(bool)) & (eventos["Descartado"].fillna("") == "")],
    ["Fecha", "Maquina", "Falla", "Duracion_min", "Causa_raiz", "Nota"])}
for m in MAQ4:
    fmq = fallas[fallas["Maquina"] == m]
    evm = ev[ev["Maquina"] == m]
    nodo = {"Maquina": m, "Fallas_mayores": int(len(fmq)), "Minutos_fallas_mayores": limpio(fmq["Minutos"].sum()),
            "ACR": int(len(evm)),
            "Causas_falla": fmq["Causa_falla"].fillna("Sin dato").value_counts().to_dict(),
            "Causas_alto_tiempo": fmq["Causa_alto_tiempo"].fillna("Sin dato").value_counts().to_dict(),
            "Pre_post_mantenimientos": registros(pp[(pp["Maquina"] == m) & (pp["Alcance"] == "Máquina")], [
                "Fecha", "Orden", "Plan", "Tarea", "Horas_orden", "Minutos_30d_antes", "Minutos_30d_despues",
                "Cambio_pct"]) if len(pp) else [],
            "componentes": []}
    ids = set(fmq["Id_componente"].dropna()) | set(evm["Id_componente"].dropna()) | \
          set(repuestos.loc[repuestos["Maquina"] == m, "Id_componente"]) | \
          (set(pp.loc[(pp["Maquina"] == m), "Id_componente"].dropna()) if len(pp) else set())
    for idc in sorted(ids, key=lambda x: -ipm.get(x, 0)):
        fc = fmq[fmq["Id_componente"] == idc].sort_values("Fecha", ascending=False)
        ec = evm[evm["Id_componente"] == idc].sort_values("Fecha", ascending=False)
        acr_list = []
        for _, e in ec.iterrows():
            a = acciones[acciones["Archivo"] == e["Archivo"]]
            acr_list.append({"Fecha": limpio(e["Fecha"]), "Falla": e["Falla"], "Duracion_min": limpio(e["Duracion_min"]),
                             "Causa_raiz": limpio(e.get("Causa_raiz")), "Nota": limpio(e.get("Nota")),
                             "Archivo": e["Archivo"],
                             "Acciones": registros(a, ["Actividad", "Responsable", "Fecha_fin", "Tipo_accion", "Estado"])})
        nodo["componentes"].append({
            "Id_componente": idc, "Componente": nombre.get(idc), "Sistema": limpio(catalogo.set_index("Id_componente")["Sistema"].get(idc)),
            "IPM": limpio(ipm.get(idc)), "Nivel": nivel.get(idc),
            "IPM_con_consecuencia": limpio(ipmc_idx["IPM_con_consecuencia"].get(idc)) if idc in ipmc_idx.index else None,
            "Nivel_con_consecuencia": ipmc_idx["Prioridad_con_consecuencia"].get(idc) if idc in ipmc_idx.index else None,
            "Fallas_mayores": {"N": int(len(fc)), "Minutos": limpio(fc["Minutos"].sum()),
                               "Lista": registros(fc, ["Fecha", "Minutos", "Causa_falla", "Causa_alto_tiempo", "Detalle"])},
            "ACR": acr_list,
            "Repuesto_critico": {"Nivel": rep_idx.loc[idc, "Nivel"], "Motivo": rep_idx.loc[idc, "Motivo"]}
            if idc in rep_idx.index else None,
            "Pre_post_mantenimientos": registros(pp[pp["Id_componente"] == idc], [
                "Fecha", "Orden", "Plan", "Tarea", "Horas_orden", "Minutos_30d_antes", "Minutos_30d_despues",
                "Cambio_pct"]) if len(pp) else []})
    salida["maquinas"].append(nodo)

with open("historia_clinica_acr.json", "w", encoding="utf-8") as fh:
    json.dump(salida, fh, ensure_ascii=False, indent=1, default=str)
ipmc.to_csv("ipm_con_consecuencia.csv", index=False)
pp.to_csv("pre_post_mantenimiento.csv", index=False)
print("\nGuardado: alertas.json (con alertas nuevas), alertas_acr.csv, historia_clinica_acr.json, "
      "ipm_con_consecuencia.csv, pre_post_mantenimiento.csv")

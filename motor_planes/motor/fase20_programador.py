"""
KOF-Smart Maintenance AI — Fase 20: PROGRAMADOR SEMANAL por paradas (mantenimiento por oportunidad)
Alimenta: calendario de paradas, "Plan sugerido de la semana", "Tareas sugeridas" y la alerta de oportunidad.

PREGUNTA QUE RESPONDE: "Con las paradas que ya existen esta semana, ¿qué tareas hacer y en cuál,
sin crear paros aislados?"

QUÉ ES UNA PARADA: una ventana en que la línea ya se detiene (limpieza del fin de semana, cambio de
formato, saneamiento, parada programada). Tiene fecha, hora, una o VARIAS máquinas detenidas y una
modalidad: en FRÍO o en CALIENTE. Dentro de cada parada van tareas: las que escribe el usuario y
las que acomoda este programador.

ENTRADAS
  Del motor: ot_limpias.csv, plan_pm_limpio.csv (Fase 12), cump_planes.csv (Fase 15),
             recomendaciones_fusion.csv y recomendaciones_A/B_completas.csv (Fase 18),
             alertas.json (Fase 19) y equivalencias (Fase 13).
  De la app (opcionales; ver CONTRATO_DATOS.md):
    paradas.json     paradas registradas con el formulario "Programar parada".
                     Si no existe, se usa un calendario de EJEMPLO y la salida lo indica.
    decisiones.json  lo que el usuario hizo con cada tarea sugerida: "Enviada" (botón "Enviar a la
                     parada", opcionalmente a una parada concreta) o "Pospuesta".

TAREAS QUE ENTRAN
  1. Tareas escritas por el usuario dentro de una parada: se respetan siempre y van primero.
  2. Órdenes SAP abiertas (preventivas y predictivas) que vencen en la semana, más las que se pueden
     ADELANTAR sin pasarse (hasta 20 % de su intervalo, máximo una semana). Cada orden se programa
     por OPERACIONES, así una orden larga se puede repartir entre dos paradas.
  3. Recomendaciones del motor (Fase 18) con canal "Parada programada" y prioridad Crítica o Alta.
     Si es "cumplir el plan vigente" y ya hay una orden SAP abierta de ese plan, no se duplica.

CÓMO ASIGNA (algoritmo voraz, explicable)
  - Primero lo obligatorio de la semana; después, si sobra capacidad, lo que se puede adelantar.
  - Dentro de eso, por prioridad (Crítica, Alta, Media, Baja) y luego por fecha de vencimiento.
  - Cada tarea va a la parada MÁS TEMPRANA que: detenga su máquina, dure más que la tarea, tenga
    horas libres y sea compatible con su modalidad (en caliente solo inspecciones, ver MODALIDAD).
  - Empate: prefiere la parada donde ya hay tareas de la misma máquina y sistema (agrupa).
  - Capacidad: no hay un número fijo de técnicos; la resuelve el técnico de turno con un tecnólogo
    de apoyo. Capacidad = horas de la parada x TECNICOS_POR_PARADA (ajustable).
  - Trabajos de alto riesgo (alturas, amoníaco, alta tensión) se marcan "con contratista" y no
    consumen la capacidad interna.
  - Lo que no cabe se muestra ("requiere parada dedicada" o "pasa a la siguiente semana").
  - Las órdenes abiertas de la semana anterior no se reprograman solas: se listan "por verificar"
    (casi siempre ya se hicieron y falta cerrarlas en SAP).

SALIDAS: plan_semanal.json (versiones fusion, A y B), plan_semanal_tareas.csv,
         plan_semanal_<semana>.xlsx (botón "Exportar semana"), alertas.json con la alerta de oportunidad.
"""
import datetime as dt
import json
import os

import numpy as np
import pandas as pd
from kof_planes_utils import MAQ4, SEMANA_OBJETIVO, norm, ARCHIVO_PARADAS, ARCHIVO_DECISIONES, ARCHIVO_EQUIV_VALIDADAS

pd.set_option("display.width", 220)
pd.set_option("display.max_colwidth", 70)

# ---------------------------------------------------------------------------
# Parámetros (ajustables con mantenimiento)
# ---------------------------------------------------------------------------
TECNICOS_POR_PARADA = 2          # técnico de turno + tecnólogo de apoyo
ADELANTO_MAX = 0.20              # se puede adelantar hasta el 20 % del intervalo del plan
TAREAS_EN_CALIENTE = {"Inspección", "Inspección y ajuste"}   # POR CONFIRMAR con mantenimiento
PALABRAS_ALTO_RIESGO = ["altura", "amoniaco", "alta tension", "izaje", "espacio confinado"]
DURACION_MANUAL_H = 0.5          # si el usuario no indica duración en una tarea manual
ORDEN_NIVEL = {"Crítica": 0, "Alta": 1, "Media": 2, "Baja": 3}
DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
TODA_LA_LINEA = {"toda la linea", "todas", "linea 4"}

ot = pd.read_csv("ot_limpias.csv", parse_dates=["Fecha_programada"])
plan = pd.read_csv("plan_pm_limpio.csv")
cump = pd.read_csv("cump_planes.csv")
with open("alertas.json", encoding="utf-8") as fh:
    alertas_json = json.load(fh)
ref = pd.Timestamp(alertas_json["fecha_referencia"])

# ---------------------------------------------------------------------------
# 0. Semana a programar: la que pide la app (KOF_SEMANA) o, si no, la siguiente a la referencia
# ---------------------------------------------------------------------------
if SEMANA_OBJETIVO:
    anio, sem = SEMANA_OBJETIVO.split("-W")
    lunes = pd.Timestamp(dt.date.fromisocalendar(int(anio), int(sem), 1))
else:
    lunes = (ref + pd.Timedelta(days=7 - ref.weekday())).normalize()
domingo = lunes + pd.Timedelta(days=6)
iso = lunes.isocalendar()
SEMANA = f"{iso.year}-W{iso.week:02d}"
print(f"Semana a programar: {SEMANA} ({lunes:%d-%m-%Y} a {domingo:%d-%m-%Y})")


# ---------------------------------------------------------------------------
# 1. Paradas de la semana (las registra el usuario en la app)
# ---------------------------------------------------------------------------
def leer_paradas():
    if os.path.exists(ARCHIVO_PARADAS):
        with open(ARCHIVO_PARADAS, encoding="utf-8") as fh:
            return json.load(fh).get("paradas", []), False
    # Calendario de EJEMPLO: la limpieza del fin de semana (fija, según mantenimiento) y un cambio
    # de formato entre semana. Se reemplaza en cuanto el usuario registre sus paradas.
    ejemplo = [
        {"Id_parada": "EJ-1", "Titulo": "Cambio de formato", "Modalidad": "Frío",
         "Fecha": (lunes + pd.Timedelta(days=2)).strftime("%Y-%m-%d"), "Inicio": "05:00", "Fin": "07:00",
         "Maquinas": ["Llenadora", "Empacadora"], "Tareas_manuales": []},
        {"Id_parada": "EJ-2", "Titulo": "Limpieza fin de semana", "Modalidad": "Frío",
         "Fecha": (lunes + pd.Timedelta(days=5)).strftime("%Y-%m-%d"), "Inicio": "06:00", "Fin": "14:00",
         "Maquinas": ["Toda la línea"], "Tareas_manuales": []},
    ]
    print("AVISO: no hay paradas.json; se usa un calendario de EJEMPLO (miércoles y sábado).")
    return ejemplo, True


paradas_raw, calendario_ejemplo = leer_paradas()
filas = []
for i, p in enumerate(paradas_raw):
    fecha = pd.Timestamp(p["Fecha"])
    if not (lunes <= fecha <= domingo):
        continue
    maqs = p.get("Maquinas") or ["Toda la línea"]
    if isinstance(maqs, str):
        maqs = [m.strip() for m in maqs.split(";")]
    lista = MAQ4 if any(norm(m) in TODA_LA_LINEA for m in maqs) else [m for m in maqs if m in MAQ4]
    horas = (pd.to_datetime(p["Fin"], format="%H:%M") - pd.to_datetime(p["Inicio"], format="%H:%M")).total_seconds() / 3600
    filas.append({"Id_parada": p.get("Id_parada") or f"P{i + 1}", "Titulo": p.get("Titulo") or "Parada",
                  "Modalidad": p.get("Modalidad") or "Frío", "Fecha": fecha, "Dia": DIAS[fecha.weekday()],
                  "Inicio": p["Inicio"], "Fin": p["Fin"], "Horas": horas % 24 or 24,
                  "Maquinas_lista": lista, "Tareas_manuales": p.get("Tareas_manuales", [])})
cal = pd.DataFrame(filas, columns=["Id_parada", "Titulo", "Modalidad", "Fecha", "Dia", "Inicio", "Fin", "Horas",
                                   "Maquinas_lista", "Tareas_manuales"]).sort_values(["Fecha", "Inicio"]).reset_index(drop=True)
print(cal[["Id_parada", "Dia", "Inicio", "Fin", "Titulo", "Modalidad", "Horas"]].to_string(index=False))


def leer_decisiones():
    if os.path.exists(ARCHIVO_DECISIONES):
        with open(ARCHIVO_DECISIONES, encoding="utf-8") as fh:
            return {d["Id_tarea"]: d for d in json.load(fh).get("decisiones", [])}
    return {}


decisiones = leer_decisiones()

# ---------------------------------------------------------------------------
# 2. Tareas manuales del usuario (van primero y se respetan)
# ---------------------------------------------------------------------------
manuales = []
for p in cal.itertuples():
    for j, t in enumerate(p.Tareas_manuales, start=1):
        manuales.append({
            "Id_tarea": t.get("Id_tarea") or f"MAN-{p.Id_parada}-{j}", "Orden": np.nan, "Plan": np.nan,
            "Maquina": t.get("Maquina"), "Sistema": t.get("Sistema"), "Id_componente": t.get("Id_componente"),
            "Componente": t.get("Componente"), "Tarea": t.get("Tarea"),
            "Tipo_tarea": t.get("Tipo_tarea", "Preventiva"),
            "Clase_orden": {"Preventiva": "ZFI1 · Preventiva", "Predictiva": "ZFI2 · Predictiva",
                            "Correctiva": "ZFI3 · Correctiva"}.get(t.get("Tipo_tarea"), "ZFI1 · Preventiva"),
            "Prioridad": "Alta", "Duracion_h": float(t.get("Duracion_h") or DURACION_MANUAL_H),
            "Origen": "Registrada por el usuario", "Situacion": "Vence esta semana", "Vence": p.Fecha,
            "Motivo": "Tarea registrada por el usuario en la parada", "Parada_fija": p.Id_parada})
manuales = pd.DataFrame(manuales)

# ---------------------------------------------------------------------------
# 3. Órdenes SAP que entran esta semana (por operaciones)
# ---------------------------------------------------------------------------
ot4 = ot[ot["En_alcance"] & ot["Tipo_orden"].isin(["Preventiva", "Predictiva"]) & (ot["Estado"] == "Abierta")].copy()
intervalo = cump.set_index("Plan")["Intervalo_nominal_dias"]
ot4["Adelanto_dias"] = ot4["Plan"].map(intervalo).fillna(7).mul(ADELANTO_MAX).clip(upper=7)
ot4["Situacion"] = np.select([ot4["Fecha_programada"] < lunes, ot4["Fecha_programada"] <= domingo],
                             ["Atrasada", "Vence esta semana"], "Se puede adelantar")
ot4 = ot4[(ot4["Fecha_programada"] >= lunes - pd.Timedelta(days=7)) &
          (ot4["Fecha_programada"] - pd.to_timedelta(ot4["Adelanto_dias"], unit="D") <= domingo)]
ops = plan[plan["Es_tarea"]][["Plan", "Tarea", "Duracion_h", "Tipo_tarea"]]
sap = ot4.merge(ops, on="Plan", how="left")
sap["Tarea"] = sap["Tarea"].fillna(sap["Texto"])
sap["Tipo_tarea"] = sap["Tipo_tarea"].fillna("Inspección")
sap["Duracion_h"] = sap["Duracion_h"].fillna(1.0)
sap["Clase_orden"] = sap["Clase"] + " · " + sap["Tipo_orden"]
sap["Origen"] = "Plan SAP"
sap["Id_tarea"] = "OT-" + sap["Orden"].astype(str) + "-" + sap.groupby("Orden").cumcount().add(1).astype(str)
sap["Vence"] = sap["Fecha_programada"].where(sap["Situacion"] != "Se puede adelantar", domingo + pd.Timedelta(days=7))

eq_file = ARCHIVO_EQUIV_VALIDADAS if os.path.exists(ARCHIVO_EQUIV_VALIDADAS) else "equivalencias_borrador.xlsx"
eq = pd.read_excel(eq_file)
if "Rango" in eq:
    eq = eq[(eq["Rango"] == 1) & (eq["Similitud"] >= 0.34)]
eq = eq.merge(plan[["Id_tarea", "Plan"]], on="Id_tarea", how="left")

COLS = ["Id_tarea", "Orden", "Plan", "Maquina", "Sistema", "Id_componente", "Componente", "Tarea", "Tipo_tarea",
        "Clase_orden", "Prioridad", "Duracion_h", "Origen", "Situacion", "Vence", "Motivo", "Parada_fija"]


def registros(df):
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_datetime64_any_dtype(d[c]):
            d[c] = d[c].dt.strftime("%Y-%m-%d")
    return json.loads(d.to_json(orient="records", force_ascii=False))


def programar(archivo):
    rec = pd.read_csv(archivo).merge(eq[["Id_componente", "Plan"]].drop_duplicates("Id_componente"),
                                     on="Id_componente", how="left")
    a_parada = rec[rec["Va_al_programador"]].copy()
    fuera = rec[rec["Prioridad"].isin(["Crítica", "Alta"]) & rec["Canal"].isin(["Rutina del operador", "Acción de gestión"])]
    por_plan = rec.dropna(subset=["Plan"]).sort_values("IPM", ascending=False).drop_duplicates("Plan").set_index("Plan")

    t_sap = sap.copy()
    t_sap["Prioridad"] = t_sap["Plan"].map(por_plan["Prioridad"]).fillna("Media")
    for c in ["Sistema", "Id_componente", "Componente"]:
        t_sap[c] = t_sap["Plan"].map(por_plan[c])
    t_sap["Motivo"] = np.where(t_sap["Componente"].notna(), "Orden SAP; cubre un componente priorizado por el motor",
                               "Orden SAP del plan vigente")
    t_sap["Parada_fija"] = None

    abiertos = set(t_sap["Plan"].dropna())
    nuevas = a_parada[~(a_parada["Accion_principal"].str.startswith("Cumplir") & a_parada["Plan"].isin(abiertos))].copy()
    nuevas["Id_tarea"] = "REC-" + nuevas["Id_componente"]
    nuevas["Tarea"] = nuevas["Tarea_sugerida"]
    nuevas["Tipo_tarea"] = nuevas["Tipo_tarea_propuesta"]
    # Si la acción es cumplir una tarea del plan vigente, el tipo es el de esa tarea (cambiar, revisar...)
    tipo_vigente = nuevas["Tarea_vigente"].map(plan[plan["Es_tarea"]].drop_duplicates("Tarea").set_index("Tarea")["Tipo_tarea"])
    cumplir = nuevas["Accion_principal"].str.startswith("Cumplir") & tipo_vigente.notna()
    nuevas.loc[cumplir, "Tipo_tarea"] = tipo_vigente[cumplir]
    nuevas["Origen"] = "Recomendación del motor"
    nuevas["Situacion"] = np.where(nuevas["Prioridad"] == "Crítica", "Vence esta semana", "Se puede posponer 1 semana")
    nuevas["Vence"] = np.where(nuevas["Prioridad"] == "Crítica", domingo, domingo + pd.Timedelta(days=7))
    nuevas["Orden"], nuevas["Parada_fija"] = np.nan, None
    nuevas["Motivo"] = nuevas["Justificacion"]

    partes = [x.reindex(columns=COLS) for x in (manuales, t_sap, nuevas) if len(x)]
    tareas = pd.concat(partes, ignore_index=True)
    tareas["Vence"] = pd.to_datetime(tareas["Vence"])
    tareas["Grupo"] = tareas["Sistema"].fillna("Plan " + tareas["Plan"].fillna("sin plan").astype(str))
    tareas["Con_contratista"] = (tareas["Tarea"].fillna("") + " " + tareas["Componente"].fillna("")) \
        .map(lambda x: any(p in norm(x) for p in PALABRAS_ALTO_RIESGO))
    dec = tareas["Id_tarea"].map(decisiones)
    tareas["Decision"] = dec.map(lambda d: d["Decision"] if isinstance(d, dict) else "Pendiente")
    parada_decidida = dec.map(lambda d: d.get("Id_parada") if isinstance(d, dict) else None)
    tareas["Parada_fija"] = [pf if isinstance(pf, str) and pf else (pd_ if isinstance(pd_, str) and pd_ else None)
                             for pf, pd_ in zip(tareas["Parada_fija"], parada_decidida)]
    tareas["Obligatoria"] = tareas["Situacion"].isin(["Atrasada", "Vence esta semana"])
    # Lo que decidió el usuario (tareas escritas por él y tareas que envió a una parada) va primero y
    # se respeta aunque la parada quede por encima de la capacidad calculada.
    tareas["_manual"] = tareas["Origen"].eq("Registrada por el usuario") | tareas["Decision"].eq("Enviada")
    tareas["_n"] = tareas["Prioridad"].map(ORDEN_NIVEL).fillna(2)
    tareas = tareas.sort_values(["_manual", "Obligatoria", "_n", "Vence", "Duracion_h"],
                                ascending=[False, False, True, True, False]).reset_index(drop=True)

    capacidad = {p.Id_parada: p.Horas * TECNICOS_POR_PARADA for p in cal.itertuples()}
    usado = {k: 0.0 for k in capacidad}
    grupos_en = {p.Id_parada: {} for p in cal.itertuples()}
    tareas["Id_parada"], tareas["Estado"] = None, None
    max_parada = cal["Horas"].max() if len(cal) else 0

    for i, t in tareas.iterrows():
        if t["Situacion"] == "Atrasada" and t["Decision"] != "Enviada":
            tareas.at[i, "Estado"] = "Verificar: atrasada o sin cerrar en SAP"
            continue
        if t["Decision"] == "Pospuesta":
            tareas.at[i, "Estado"] = "Pospuesta por el usuario"
            continue
        fija = t["Parada_fija"] if isinstance(t["Parada_fija"], str) and t["Parada_fija"] else None
        opciones = []
        for p in cal.itertuples():
            if fija and p.Id_parada != fija:
                continue
            if t["Maquina"] not in p.Maquinas_lista or t["Duracion_h"] > p.Horas:
                continue
            if not fija and p.Fecha > t["Vence"] + pd.Timedelta(days=7):
                continue
            if norm(p.Modalidad) == "caliente" and t["Tipo_tarea"] not in TAREAS_EN_CALIENTE and not t["_manual"]:
                continue
            if not t["Con_contratista"] and not t["_manual"] and usado[p.Id_parada] + t["Duracion_h"] > capacidad[p.Id_parada] + 1e-9:
                continue
            opciones.append((p.Fecha, -grupos_en[p.Id_parada].get((t["Maquina"], t["Grupo"]), 0), p.Id_parada))
        if opciones:
            pid = sorted(opciones)[0][2]
            if not t["Con_contratista"]:
                usado[pid] += t["Duracion_h"]
            clave = (t["Maquina"], t["Grupo"])
            grupos_en[pid][clave] = grupos_en[pid].get(clave, 0) + 1
            tareas.at[i, "Id_parada"] = pid
            tareas.at[i, "Estado"] = "Enviada a la parada" if t["Decision"] == "Enviada" or t["_manual"] else "Sugerida"
        elif t["Situacion"] == "Se puede adelantar":
            tareas.at[i, "Estado"] = "No se adelanta"
        elif t["Duracion_h"] > max_parada:
            tareas.at[i, "Estado"] = "Requiere parada dedicada"
        else:
            tareas.at[i, "Estado"] = "Sin espacio: pasa a la siguiente semana"
    tareas = tareas[tareas["Estado"] != "No se adelanta"].drop(columns=["_n", "_manual"]).reset_index(drop=True)

    asignadas = tareas[tareas["Id_parada"].notna()]
    internas = asignadas[~asignadas["Con_contratista"]]
    disponible = sum(capacidad.values())
    paradas = []
    for p in cal.itertuples():
        tp = asignadas[asignadas["Id_parada"] == p.Id_parada]
        por_sistema = [{"Maquina": m, "Grupo": g, "Tareas": len(x), "Horas": round(x["Duracion_h"].sum(), 2),
                        "Id_tareas": x["Id_tarea"].tolist()} for (m, g), x in tp.groupby(["Maquina", "Grupo"])]
        paradas.append({"Id_parada": p.Id_parada, "Titulo": p.Titulo, "Modalidad": p.Modalidad, "Dia": p.Dia,
                        "Fecha": p.Fecha.strftime("%Y-%m-%d"), "Inicio": p.Inicio, "Fin": p.Fin, "Horas": p.Horas,
                        "Maquinas": p.Maquinas_lista, "Capacidad_h": capacidad[p.Id_parada],
                        "Horas_asignadas": round(usado[p.Id_parada], 2), "Por_sistema": por_sistema,
                        "Tareas": registros(tp)})
    programables = tareas[~tareas["Estado"].str.startswith("Verificar")]
    indicadores = {
        "tareas": int(len(programables)), "en_paradas_existentes": int(len(asignadas)),
        "uso_paradas_pct": round(float(100 * internas["Duracion_h"].sum() / disponible), 1) if disponible else 0.0,
        "con_contratista": int(asignadas["Con_contratista"].sum()),
        "requieren_parada_dedicada": int((tareas["Estado"] == "Requiere parada dedicada").sum()),
        "pasan_a_siguiente_semana": int(tareas["Estado"].str.startswith("Sin espacio").sum()),
        "ordenes_por_verificar": int(tareas.loc[tareas["Estado"].str.startswith("Verificar"), "Orden"].nunique()),
        "alertas": int(len(alertas_json["alertas"]))}
    salida = {"indicadores": indicadores, "paradas": paradas,
              "tareas_sugeridas": registros(tareas),
              "sin_parada": registros(tareas[tareas["Id_parada"].isna() & ~tareas["Estado"].str.startswith("Verificar")]),
              "por_verificar": registros(tareas[tareas["Estado"].str.startswith("Verificar")]
                                         .drop_duplicates("Orden")[["Orden", "Maquina", "Plan", "Clase_orden", "Vence"]]),
              "fuera_de_parada": registros(fuera[["Id_componente", "Maquina", "Sistema", "Componente", "Prioridad",
                                                  "Canal", "Tarea_sugerida", "Frecuencia_propuesta", "Justificacion"]])}
    return salida, tareas


# ---------------------------------------------------------------------------
# 4. Programar las tres versiones (selector A / B / Fusión)
# ---------------------------------------------------------------------------
resultado = {"semana": SEMANA, "desde": lunes.strftime("%Y-%m-%d"), "hasta": domingo.strftime("%Y-%m-%d"),
             "calendario_es_ejemplo": calendario_ejemplo, "tecnicos_por_parada": TECNICOS_POR_PARADA,
             "versiones": {}}
tablas = {}
for version, archivo in [("fusion", "recomendaciones_fusion.csv"), ("A", "recomendaciones_A_completas.csv"),
                         ("B", "recomendaciones_B_completas.csv")]:
    resultado["versiones"][version], tablas[version] = programar(archivo)

t = tablas["fusion"]
ind = resultado["versiones"]["fusion"]["indicadores"]
print(f"\n--- Plan de la semana (fusión) ---\n{ind}")
for p in resultado["versiones"]["fusion"]["paradas"]:
    print(f"\n{p['Dia']} {p['Inicio']}-{p['Fin']} · {p['Titulo']} ({p['Modalidad']}) · máquinas: {', '.join(p['Maquinas'])}"
          f" · {len(p['Tareas'])} tareas · {p['Horas_asignadas']} de {p['Capacidad_h']} h")
    for g in p["Por_sistema"][:8]:
        print(f"   {g['Maquina']} · {g['Grupo']}: {g['Tareas']} tareas, {g['Horas']} h")
pend = t[t["Estado"].str.startswith("Sin espacio")]
print(f"\nNo cupieron: {len(pend)} tareas ({pend['Duracion_h'].sum():.1f} h). "
      f"Por verificar: {ind['ordenes_por_verificar']} órdenes.")

# ---------------------------------------------------------------------------
# 5. Alerta de OPORTUNIDAD (se agrega a alertas.json)
# ---------------------------------------------------------------------------
nuevas_alertas = []
for p in resultado["versiones"]["fusion"]["paradas"]:
    if p["Tareas"]:
        nuevas_alertas.append({
            "Id_alerta": f"OP-{p['Id_parada']}", "Tipo": "Oportunidad", "Maquina": ", ".join(p["Maquinas"]),
            "Componente": None, "Id_componente": None, "Prioridad": "Media", "IPM": None,
            "Mensaje": f"Hay {len(p['Tareas'])} tareas que se pueden agrupar en la parada "
                       f"\"{p['Titulo']}\" del {p['Dia'].lower()} ({p['Inicio']}-{p['Fin']}) sin detener la línea de nuevo.",
            "Evidencia": f"{p['Horas_asignadas']} h de {p['Capacidad_h']} h disponibles", "Confianza": "Alta",
            "Accion_sugerida": "Revisar el calendario", "Enlace": "calendario", "Mostrar": False,
            "Fecha_referencia": alertas_json["fecha_referencia"]})
if nuevas_alertas:
    max(nuevas_alertas, key=lambda a: int(a["Mensaje"].split()[1]))["Mostrar"] = True
    visibles = [a for a in alertas_json["alertas"] if a["Mostrar"]]
    if len(visibles) >= 5:
        visibles[-1]["Mostrar"] = False
alertas_json["alertas"] = [a for a in alertas_json["alertas"] if a["Tipo"] != "Oportunidad"] + nuevas_alertas
with open("alertas.json", "w", encoding="utf-8") as fh:
    json.dump(alertas_json, fh, ensure_ascii=False, indent=1)

# ---------------------------------------------------------------------------
# 6. Guardar
# ---------------------------------------------------------------------------
with open("plan_semanal.json", "w", encoding="utf-8") as fh:
    json.dump(resultado, fh, ensure_ascii=False, indent=1, default=str)
t.to_csv("plan_semanal_tareas.csv", index=False)
nombre_x = f"plan_semanal_{SEMANA}.xlsx"
with pd.ExcelWriter(nombre_x) as xls:
    t.merge(cal[["Id_parada", "Dia", "Inicio", "Fin", "Titulo", "Modalidad"]], on="Id_parada", how="left") \
     .to_excel(xls, sheet_name="Plan de la semana", index=False)
    pd.DataFrame(resultado["versiones"]["fusion"]["fuera_de_parada"]).to_excel(xls, sheet_name="Fuera de parada", index=False)
    cal.drop(columns=["Tareas_manuales"]).assign(Maquinas_lista=cal["Maquinas_lista"].map(", ".join)) \
       .to_excel(xls, sheet_name="Paradas", index=False)
print(f"\nGuardado: plan_semanal.json, plan_semanal_tareas.csv, {nombre_x}, alertas.json (con oportunidad)")

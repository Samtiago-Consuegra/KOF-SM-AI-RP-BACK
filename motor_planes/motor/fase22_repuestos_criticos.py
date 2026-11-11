"""
KOF-Smart Maintenance AI — Fase 22: REPUESTOS CRÍTICOS por componente
(código puro)                         Alimenta: Historia clínica y la alerta "Riesgo de repuesto" (Fase 23)

PREGUNTA QUE RESPONDE: "¿En qué componentes la falta o la mala calidad del repuesto ya alargó o
causó fallas graves?"

No hay datos de inventario: la lista NO dice cuántos repuestos hay en almacén. Dice en qué componentes
el repuesto ya fue un problema, con su evidencia, para verificar el stock antes de intervenirlos.
Si más adelante se consigue la exportación de inventario de SAP, se agrega como otra fuente.

EVIDENCIA QUE SE USA
  1. Consolidado de fallas mayores (Fase 21):
     - causa del alto tiempo = "Falta repuesto" o "Demora entrega de repuesto"  -> el repuesto ALARGÓ la falla
     - causa de la falla     = "Calidad de repuesto" o "Falta de repuesto"       -> el repuesto CAUSÓ la falla
  2. ACR (Fase 21): menciones en el texto de repuestos no originales, de fabricación local, importados,
     sin stock, con MRP manual, de baja calidad o en garantía.

NIVEL
  Alto  = el repuesto aparece en el consolidado Y en un ACR, o en 3 o más fallas mayores.
  Medio = el resto de componentes con evidencia.

SALIDA: repuestos_criticos.csv
"""
import re

import numpy as np
import pandas as pd
from kof_planes_utils import MAQ4, norm

pd.set_option("display.width", 220)
pd.set_option("display.max_colwidth", 90)

ALARGA = ["falta repuesto", "falta de repuesto", "demora entrega de repuesto"]
CAUSA = ["calidad de repuesto", "falta de repuesto"]
PATRONES_ACR = {
    "no original": r"no original|no se compra material original|fabricaci[oó]n (local|nacional)|fabricad[oa]s? localmente|sellos nacionales",
    "sin stock": r"sin stock|stock (insuficiente|m[ií]nimo|de seguridad)|falta de stock|no se encu?entra disponibilidad|no hab[ií]a|falta de (refacciones|materiales)|cambio incompleto",
    "importado o compra lenta": r"importaci[oó]n|mrp|punto de pedido|cotizaci[oó]n|pedido de (los )?material|tiempo de respuesta ante la compra|1 semana en llegar",
    "calidad del repuesto": r"baja calidad|mala calidad|m[aá]s corta|dureza|blando|garant[ií]a",
}

fallas = pd.read_csv("fallas_mayores.csv", parse_dates=["Fecha"])
eventos = pd.read_csv("acr_eventos.csv", parse_dates=["Fecha"])
textos = pd.read_csv("acr_textos.csv")
acciones = pd.read_csv("acr_acciones.csv")
catalogo = pd.read_csv("catalogo_componentes.csv")

# ---------------------------------------------------------------------------
# 1. Evidencia del consolidado
# ---------------------------------------------------------------------------
f = fallas.dropna(subset=["Id_componente"]).copy()
f["alto"] = f["Causa_alto_tiempo"].fillna("").map(norm).isin(ALARGA)
f["causa"] = f["Causa_falla"].fillna("").map(norm).isin(CAUSA)
f = f[f["alto"] | f["causa"]]
cons = f.groupby("Id_componente").agg(
    Fallas_por_repuesto=("Fecha", "size"),
    Minutos_por_repuesto=("Minutos", "sum"),
    Alargo_la_falla=("alto", "sum"),
    Causo_la_falla=("causa", "sum"),
    Ultima_falla_por_repuesto=("Fecha", "max")).reset_index()

# ---------------------------------------------------------------------------
# 2. Evidencia de los ACR (texto completo + acciones)
# ---------------------------------------------------------------------------
ev = eventos[eventos["En_alcance"].fillna(False).astype(bool) & eventos["Id_componente"].notna()]
filas = []
for _, e in ev.iterrows():
    texto = " ".join(textos.loc[textos["Archivo"] == e["Archivo"], "Texto"].astype(str)) + " " + \
            " ".join(acciones.loc[acciones["Archivo"] == e["Archivo"], "Actividad"].astype(str)) + " " + \
            str(e.get("Causa_raiz", ""))
    temas = [tema for tema, patron in PATRONES_ACR.items() if re.search(patron, texto, flags=re.IGNORECASE)]
    if temas:
        filas.append({"Id_componente": e["Id_componente"], "Fecha_acr": e["Fecha"], "Temas": temas})
acr = pd.DataFrame(filas, columns=["Id_componente", "Fecha_acr", "Temas"])
acr_g = acr.groupby("Id_componente").agg(
    ACR_con_repuesto=("Fecha_acr", "size"),
    Ultimo_acr_repuesto=("Fecha_acr", "max"),
    Temas_acr=("Temas", lambda t: ", ".join(sorted({x for lista in t for x in lista})))).reset_index()

# ---------------------------------------------------------------------------
# 3. Lista de repuestos críticos
# ---------------------------------------------------------------------------
rep = cons.merge(acr_g, on="Id_componente", how="outer")
rep = rep.merge(catalogo[["Id_componente", "Maquina", "Sistema", "Componente"]], on="Id_componente", how="left")
for c in ["Fallas_por_repuesto", "Minutos_por_repuesto", "Alargo_la_falla", "Causo_la_falla", "ACR_con_repuesto"]:
    rep[c] = rep[c].fillna(0)
rep["Nivel"] = np.where(((rep["Fallas_por_repuesto"] > 0) & (rep["ACR_con_repuesto"] > 0)) |
                        (rep["Fallas_por_repuesto"] >= 3), "Alto", "Medio")


def motivo(r):
    partes = []
    if r["Alargo_la_falla"]:
        partes.append(f"en {int(r['Alargo_la_falla'])} falla(s) mayor(es) el paro se alargó por falta o demora del repuesto")
    if r["Causo_la_falla"]:
        partes.append(f"en {int(r['Causo_la_falla'])} la causa registrada fue la calidad o falta del repuesto")
    if r["ACR_con_repuesto"]:
        partes.append(f"los ACR mencionan: {r['Temas_acr']}")
    texto = "; ".join(partes)
    return texto[:1].upper() + texto[1:] + "." if texto else ""


rep["Motivo"] = rep.apply(motivo, axis=1)
rep["Ultima_evidencia"] = rep[["Ultima_falla_por_repuesto", "Ultimo_acr_repuesto"]].max(axis=1)
rep["Maquina"] = pd.Categorical(rep["Maquina"], MAQ4, ordered=True)
rep = rep.sort_values(["Nivel", "Minutos_por_repuesto", "ACR_con_repuesto"], ascending=[True, False, False])
cols = ["Id_componente", "Maquina", "Sistema", "Componente", "Nivel", "Fallas_por_repuesto", "Minutos_por_repuesto",
        "Alargo_la_falla", "Causo_la_falla", "ACR_con_repuesto", "Temas_acr", "Ultima_evidencia", "Motivo"]
rep = rep[cols].reset_index(drop=True)

print(f"Componentes con repuesto crítico: {len(rep)} (nivel alto: {(rep['Nivel'] == 'Alto').sum()})")
print(rep[["Maquina", "Componente", "Nivel", "Fallas_por_repuesto", "Minutos_por_repuesto", "ACR_con_repuesto",
           "Temas_acr"]].round(0).to_string(index=False))
rep.to_csv("repuestos_criticos.csv", index=False)
print("\nGuardado: repuestos_criticos.csv")

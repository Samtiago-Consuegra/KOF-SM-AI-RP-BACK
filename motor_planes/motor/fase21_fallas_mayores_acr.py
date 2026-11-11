"""
KOF-Smart Maintenance AI — Fase 21: FALLAS MAYORES y ANÁLISIS DE CAUSA RAÍZ (ACR)
(código puro)                                   Alimenta: Historia clínica (antecedentes) y Fases 22 a 24

PREGUNTA QUE RESPONDE: "¿Qué fallas graves ha tenido cada componente, por qué ocurrieron según la
planta y qué acciones se comprometieron?"

ENTRADAS (carpeta de entrada)
  Consolidado de reportes de fallo (fallas de más de 60 min, todas las líneas): *Consolidado*Fallo*.xlsx
  Carpeta acr/ con los Excel de ACR (fallas de más de 180 min). El nombre de cada archivo empieza por
  la fecha (DD-MM-AAAA) y dice la máquina y la falla, como los envía la planta.
  Opcional: acr_revisado.xlsx — la tabla de ACR revisada a mano (si existe, manda sobre la extracción).
Del motor: catalogo_componentes.csv (Fase 13).

QUÉ HACE
  1. Consolidado: toma la Línea 4 y las cuatro máquinas, separa sistema y causa del texto de la falla y
     la une al catálogo del motor (mismo código de componente). Conserva la "causa de la falla" y la
     "causa del alto tiempo" que registró la planta.
  2. ACR: lee cada archivo y extrae fecha, máquina, falla, duración, causa raíz y la tabla de acciones
     (actividad, fechas, responsable y estado). Descarta borradores ("copia"), versiones repetidas del
     mismo evento y los ACR que no son de la Línea 4.
  3. Deja acr_revision.xlsx para revisar la extracción. Si se corrige y se guarda como acr_revisado.xlsx
     en la carpeta de entrada, el motor usa esa versión.

SALIDAS: fallas_mayores.csv, acr_eventos.csv, acr_acciones.csv, acr_revision.xlsx
"""
import datetime as dt
import glob
import os
import re

import numpy as np
import openpyxl
import pandas as pd
from kof_planes_utils import (ENTRADA, MAQ4, norm, separar_causa, nombre_maquina, buscar_archivo,
                              archivo_externo, FECHA_REFERENCIA)

pd.set_option("display.width", 220)
pd.set_option("display.max_colwidth", 80)

UMBRAL_SIMILITUD = 0.34
HOJAS_PLANTILLA = {"Menú", "Instrucciones", "Título", "Encabezado", "3. Valoración de riesgo"}
ESTADOS_CERRADOS = ["finalizado", "realizado", "ejecutado", "cerrado", "completado", "terminado"]
MAQUINAS_ARCHIVO = {"llenadora": "Llenadora", "empacadora": "Empacadora", "paletizadora": "Paletizadora",
                    "envolvedora": "Envolvedora", "transporte_de_paquetes": "Empacadora", "emp_l4": "Empacadora",
                    "capsulador": "Capsulador", "intercambiador": "Intercambiador"}

catalogo = pd.read_csv("catalogo_componentes.csv")
catalogo["clave"] = catalogo["Componente"].map(norm)


def raices(texto):
    vacias = {"de", "del", "la", "el", "los", "las", "en", "y", "sistema", "sist", "l4", "linea", "falla",
              "danada", "danado", "danadas", "danados", "acr", "baq", "con", "por"}
    out = set()
    for p in re.findall(r"[a-z]{3,}", norm(texto).replace("baquelas", "baquetas").replace("baquela", "baqueta")):
        if p in vacias:
            continue
        w = p[:-1] if len(p) > 4 and p.endswith("s") else p
        w = w[:-1] if len(w) > 4 and w.endswith("e") else w
        out.add(w[:6])
    return out


def componente_mas_parecido(maquina, texto):
    """Componente del catálogo de esa máquina más parecido al texto (índice de Jaccard)."""
    r = raices(texto)
    mejor, puntaje = None, 0.0
    for c in catalogo[catalogo["Maquina"] == maquina].itertuples():
        rc = raices(c.Componente)
        p = len(r & rc) / len(r | rc) if r and rc else 0
        if p > puntaje:
            mejor, puntaje = c, p
    return (mejor.Id_componente, mejor.Componente, round(puntaje, 2)) if mejor is not None and puntaje >= UMBRAL_SIMILITUD \
        else (None, None, round(puntaje, 2))


# ---------------------------------------------------------------------------
# 1. Consolidado de fallas mayores
# ---------------------------------------------------------------------------
archivo_cons = buscar_archivo("*Consolidado*Fallo*.xls*", "Consolidado_Base_Reportes_de_Fallo.xlsx")
if os.path.exists(archivo_cons):
    c = pd.read_excel(archivo_cons)
    c = c.rename(columns={"Tiempo perdido (min)": "Minutos", "Causa de falla": "Causa_falla",
                          "Causa alto tiempo": "Causa_alto_tiempo", "ACR": "Tiene_ACR",
                          "Detalle del evento: Que sucedió, que se encontró, que se realizó para corregir?": "Detalle"})
    c = c[c["Línea"].astype(str).str.upper().eq("LINEA004")].copy()
    c["Maquina"] = c["Equipo"].map(nombre_maquina)
    c = c[c["Maquina"].isin(MAQ4)].copy()
    # "Sistema de Inst. eléctrica - Conexiones sulfatadas - Fibra optica": sistema, causa y detalle
    partes = c["Falla"].astype(str).str.split(r"\s+-\s*", regex=True)
    c["Sistema_reportado"] = np.where(partes.str.len() > 1, partes.str[0].str.strip(), None)
    c["Causa_texto"] = np.where(partes.str.len() > 1, partes.str[1], partes.str[0]).astype(str)
    c["Componente_reportado"] = c["Causa_texto"].map(lambda x: separar_causa(x)[0])
    mapa = {(r.Maquina, r.clave): (r.Id_componente, r.Componente) for r in catalogo.itertuples()}
    ids, comps, origen = [], [], []
    for m, comp, texto in zip(c["Maquina"], c["Componente_reportado"], c["Falla"]):
        hit = mapa.get((m, norm(comp)))
        if hit:
            ids.append(hit[0]); comps.append(hit[1]); origen.append("Exacto")
        else:
            idc, nom, _ = componente_mas_parecido(m, texto)
            ids.append(idc); comps.append(nom); origen.append("Por parecido" if idc else "Sin unir")
    c["Id_componente"], c["Componente"], c["Union_catalogo"] = ids, comps, origen
    for col in ["Causa_falla", "Causa_alto_tiempo"]:
        c[col] = c[col].astype(str).str.strip().str.capitalize().replace({"Nan": np.nan, "Falta setting": "Falta de setting",
                                                                         "Operación indebida": "Operación indebida"})
    fallas = c[["Fecha", "Semana", "Maquina", "Equipo", "Falla", "Sistema_reportado", "Id_componente", "Componente",
                "Union_catalogo", "Minutos", "Causa_falla", "Causa_alto_tiempo", "Tiene_ACR", "Detalle",
                "Observaciones"]].sort_values("Fecha").reset_index(drop=True)
    print(f"Consolidado: {len(fallas)} fallas mayores de las 4 máquinas ({fallas['Fecha'].min():%d-%m-%Y} a "
          f"{fallas['Fecha'].max():%d-%m-%Y}), {fallas['Minutos'].sum():,.0f} min")
    print(f"Unidas al catálogo: {fallas['Id_componente'].notna().mean():.0%} "
          f"({(fallas['Union_catalogo'] == 'Exacto').sum()} exactas, {(fallas['Union_catalogo'] == 'Por parecido').sum()} por parecido)")
else:
    fallas = pd.DataFrame(columns=["Fecha", "Maquina", "Id_componente", "Componente", "Minutos", "Causa_falla",
                                   "Causa_alto_tiempo"])
    print("AVISO: no se encontró el consolidado de fallas mayores (*Consolidado*Fallo*.xlsx).")


# ---------------------------------------------------------------------------
# 2. Extracción de los ACR
# ---------------------------------------------------------------------------
def texto_celda(v):
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""


def es_fecha(v):
    if isinstance(v, (dt.datetime, dt.date)):
        return True
    return isinstance(v, str) and bool(re.match(r"^\s*\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\s*$", v))


def a_fecha(v):
    if isinstance(v, (dt.datetime, dt.date)):
        return pd.Timestamp(v)
    return pd.to_datetime(str(v).strip().replace(".", "-"), dayfirst=True, errors="coerce")


def leer_acr(ruta):
    nombre = os.path.basename(ruta)
    wb = openpyxl.load_workbook(ruta, data_only=True)
    celdas = []                      # (hoja, fila, columna, valor)
    for ws in wb.worksheets:
        if ws.title in HOJAS_PLANTILLA:
            continue
        for fila in ws.iter_rows():
            for c in fila:
                if c.value is not None and texto_celda(c.value):
                    celdas.append((ws.title, c.row, c.column, c.value))
    textos = [texto_celda(v) for _, _, _, v in celdas]
    todo = " ".join(textos)

    # Fecha, máquina y falla: del nombre del archivo (así los nombra la planta)
    m = re.match(r"(\d{2}-\d{2}-\d{4})_(.*)\.xlsx?$", nombre)
    fecha = pd.to_datetime(m.group(1), dayfirst=True) if m else pd.NaT
    titulo = re.sub(r"_+", " ", m.group(2) if m else nombre).replace("ACR", "").strip(" -")
    nn = norm(nombre).replace(" ", "_")
    maquina = next((v for k, v in MAQUINAS_ARCHIVO.items() if k in nn), None)

    # Línea: si el reporte habla de la línea 3 y no de la 4, no es de nuestro alcance
    reporte = " ".join(texto_celda(v) for h, _, _, v in celdas if h.startswith("1."))
    es_l3 = bool(re.search(r"l(inea)?\s*3\b|san mart", norm(reporte))) and not re.search(r"\bl(inea)?\s*4\b", norm(reporte))

    # Duración en minutos
    dur = re.search(r"duraci[oó]n:?\s*([\d.,]+)\s*(min|\+)", todo, flags=re.IGNORECASE) or \
          re.search(r"(?:impacto|p[eé]rdida de|paro de equipo de|afectaci[oó]n de)\s*([\d.,]+)\s*min", todo, flags=re.IGNORECASE)
    duracion = float(dur.group(1).replace(".", "").replace(",", ".")) if dur else np.nan
    if dur is None:
        dur2 = re.search(r"(\d{3,5})\s*min", todo)
        duracion = float(dur2.group(1)) if dur2 else np.nan

    # Causa raíz: el primer texto largo después de una etiqueta "Causa raíz"
    causa = None
    for i, (h, f, col, v) in enumerate(celdas):
        t = norm(texto_celda(v))
        if re.match(r"^(7\. )?causa(s)? ra[ií]z( f[ií]sica)?:?$", t) or t == "causa raiz":
            for _, _, _, v2 in celdas[i + 1:i + 8]:
                t2 = texto_celda(v2)
                if len(t2) > 25 and not norm(t2).startswith(("causa raiz", "acciones", "* elija")):
                    causa = t2
                    break
        if causa:
            break
    if not causa:   # formato 5 por qué: la última respuesta suele ser la causa
        largos = [texto_celda(v) for h, _, _, v in celdas if "5 por" in norm(h) and len(texto_celda(v)) > 40
                  and not norm(texto_celda(v)).startswith(("*", "porque", "por que", "¿"))]
        causa = largos[-1] if largos else None

    # Acciones: filas con un texto a la izquierda y dos fechas (inicio y fin)
    acciones, vistas = [], set()
    filas = {}
    for h, f, col, v in celdas:
        filas.setdefault((h, f), []).append((col, v))
    for (h, f), vals in filas.items():
        vals.sort()
        fechas = [(c, v) for c, v in vals if es_fecha(v)]
        textos_f = [(c, texto_celda(v)) for c, v in vals if isinstance(v, str) and len(texto_celda(v)) > 3 and not es_fecha(v)]
        if len(fechas) < 2 or not textos_f or textos_f[0][0] > fechas[0][0]:
            continue
        actividad = textos_f[0][1]
        if norm(actividad).startswith(("fecha", "actividad")) or actividad in vistas:
            continue
        vistas.add(actividad)
        despues = [t for c, t in textos_f if c > fechas[1][0]]
        estado = next((t for t in despues if any(k in norm(t) for k in ESTADOS_CERRADOS + ["proceso", "pendiente", "parcial", "validar", "cotizacion"])), None)
        responsable = next((t for t in despues if t != estado), None)
        acciones.append({"Archivo": nombre, "Actividad": actividad, "Fecha_inicio": a_fecha(fechas[0][1]),
                         "Fecha_fin": a_fecha(fechas[1][1]), "Responsable": responsable, "Estado_registrado": estado})
    evento = {"Archivo": nombre, "Fecha": fecha, "Maquina": maquina, "Falla": titulo, "Duracion_min": duracion,
              "Causa_raiz": causa, "Linea_3": es_l3, "Borrador": "copia" in norm(nombre), "N_acciones": len(acciones),
              "Texto": todo[:20000]}
    return evento, acciones


carpeta_acr = os.path.join(ENTRADA, "acr")
eventos, acciones = [], []
for ruta in sorted(glob.glob(os.path.join(carpeta_acr, "*.xls*"))):
    try:
        e, a = leer_acr(ruta)
        eventos.append(e)
        acciones += a
    except Exception as ex:
        print(f"AVISO: no se pudo leer {os.path.basename(ruta)}: {ex!r}")
ev = pd.DataFrame(eventos)
ac = pd.DataFrame(acciones)

if len(ev):
    # Descarta borradores, ACR de otra línea y versiones repetidas del mismo evento (se queda la más completa)
    ev["Descartado"] = np.select([ev["Borrador"], ev["Linea_3"]], ["Borrador (copia)", "No es de la Línea 4"], "")
    ev["_clave"] = ev["Fecha"].astype(str) + ev["Maquina"].astype(str)
    mas_completo = ev[ev["Descartado"] == ""].sort_values("N_acciones", ascending=False).drop_duplicates("_clave")["Archivo"]
    ev.loc[(ev["Descartado"] == "") & ~ev["Archivo"].isin(mas_completo), "Descartado"] = "Versión repetida del mismo evento"
    # Componente del catálogo (solo para las cuatro máquinas)
    comp = [componente_mas_parecido(m, f) if m in MAQ4 else (None, None, 0.0) for m, f in zip(ev["Maquina"], ev["Falla"])]
    ev["Id_componente"], ev["Componente"], ev["Similitud"] = [x[0] for x in comp], [x[1] for x in comp], [x[2] for x in comp]
    ev["En_alcance"] = ev["Maquina"].isin(MAQ4) & (ev["Descartado"] == "")
    ev = ev.drop(columns="_clave")

textos = ev[["Archivo", "Texto"]].copy() if len(ev) else pd.DataFrame(columns=["Archivo", "Texto"])

# Si existe la versión revisada a mano, manda sobre la extracción automática
revisado = archivo_externo("acr_revisado.xlsx")
if os.path.exists(revisado):
    rev_ev = pd.read_excel(revisado, sheet_name="Eventos")
    rev_ac = pd.read_excel(revisado, sheet_name="Acciones")
    nuevos = ev[~ev["Archivo"].isin(rev_ev["Archivo"])] if len(ev) else ev
    ev = pd.concat([rev_ev, nuevos.drop(columns="Texto", errors="ignore")], ignore_index=True)
    ac = pd.concat([rev_ac, ac[ac["Archivo"].isin(nuevos["Archivo"])] if len(ac) else ac], ignore_index=True)
    print(f"Se usa acr_revisado.xlsx ({len(rev_ev)} ACR revisados; {len(nuevos)} nuevos sin revisar)")

VERBOS_ACCION = ("cambi", "reempl", "rempl", "solicit", "seguim", "divulg", "fabric", "instal", "adecu", "repar",
                 "desmont", "recort", "puesta", "prueba", "compr", "arranqu", "dashboard", "inclu", "ajust", "verific",
                 "contact", "modific", "revis", "elimin", "valid", "realiz", "crear", "establec", "gestion", "agiliz",
                 "evalu", "consult", "report", "replante", "actualiz", "implement", "asegur", "alinear", "sustitu",
                 "notific", "hacer", "elabor", "monitor", "monit", "reorganiz", "toma", "instalar", "limpi")


def es_actividad(texto):
    """Descarta celdas que el extractor tomó como actividad pero son nombres de responsables."""
    primera = norm(texto).split(" ")[0] if texto else ""
    return primera.startswith(VERBOS_ACCION)


if len(ac):
    ac = ac[ac["Archivo"].isin(ev.loc[ev["Descartado"].fillna("") == "", "Archivo"])].copy()
    ac = ac[ac["Actividad"].map(es_actividad)].copy()
    # Acciones con fechas muy anteriores al evento vienen de otra falla (plantilla reutilizada): se descartan
    fecha_evento = ac["Archivo"].map(ev.drop_duplicates("Archivo").set_index("Archivo")["Fecha"])
    ac = ac[~(pd.to_datetime(ac["Fecha_fin"], errors="coerce") < pd.to_datetime(fecha_evento) - pd.Timedelta(days=30))].copy()
    ac["Fecha_inicio"] = pd.to_datetime(ac["Fecha_inicio"], errors="coerce")
    ac["Fecha_fin"] = pd.to_datetime(ac["Fecha_fin"], errors="coerce")
    ref = pd.Timestamp(FECHA_REFERENCIA) if FECHA_REFERENCIA else pd.Timestamp.today().normalize()
    ac = ac.merge(ev[["Archivo", "Fecha", "Maquina", "Id_componente", "Componente", "Falla"]].drop_duplicates("Archivo"),
                  on="Archivo", how="left")
    cerrada = ac["Estado_registrado"].fillna("").map(lambda e: any(k in norm(str(e)) for k in ESTADOS_CERRADOS))
    # Acción inmediata: la que se hizo durante el evento para recuperar el equipo (termina hasta 2 días después).
    # Las demás son acciones de fondo (preventivas o de mejora) y son las que interesa seguir.
    inmediata = ac["Fecha_fin"] <= ac["Fecha"] + pd.Timedelta(days=2)
    ac["Tipo_accion"] = np.where(inmediata, "Inmediata (durante el evento)", "De fondo (preventiva o mejora)")
    ac["Estado"] = np.select([cerrada | inmediata, ac["Fecha_fin"] >= ref], ["Cerrada", "En plazo"],
                             "Fecha cumplida sin cierre registrado")

# ---------------------------------------------------------------------------
# 3. Resumen y salidas
# ---------------------------------------------------------------------------
if len(ev):
    print(f"\nACR leídos: {len(ev)} | en alcance (Línea 4, cuatro máquinas): {int(ev['En_alcance'].sum())}")
    print(ev[["Fecha", "Maquina", "Falla", "Duracion_min", "Componente", "Descartado"]]
            .sort_values("Fecha").to_string(index=False))
if len(ac):
    print(f"\nAcciones comprometidas en los ACR: {len(ac)}")
    print(ac["Estado"].value_counts().to_string())

fallas.to_csv("fallas_mayores.csv", index=False)
ev.drop(columns="Texto", errors="ignore").to_csv("acr_eventos.csv", index=False)
textos.to_csv("acr_textos.csv", index=False)     # texto completo de cada ACR: lo usa la Fase 22
ac.to_csv("acr_acciones.csv", index=False)
with pd.ExcelWriter("acr_revision.xlsx") as xls:
    ev.drop(columns="Texto", errors="ignore").to_excel(xls, sheet_name="Eventos", index=False)
    ac.drop(columns=["Fecha", "Maquina", "Id_componente", "Componente", "Falla", "Estado"], errors="ignore") \
      .to_excel(xls, sheet_name="Acciones", index=False)
    pd.DataFrame({"Instrucción": [
        "Revisar en Eventos: máquina, componente (Id_componente del catálogo), duración y causa raíz.",
        "Revisar en Acciones: actividad, fechas, responsable y estado (escribir 'Finalizado' si ya se cerró).",
        "Guardar como acr_revisado.xlsx en la carpeta de entrada; el motor usará esta versión.",
    ]}).to_excel(xls, sheet_name="Instrucciones", index=False)
print("\nGuardado: fallas_mayores.csv, acr_eventos.csv, acr_acciones.csv, acr_revision.xlsx")

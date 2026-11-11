"""
kof_planes_utils.py — Funciones comunes del módulo de PLANES DE MANTENIMIENTO
KOF-Smart Maintenance AI · Línea 4 PET · Planta Barranquilla

Es el equivalente de kof_utils.py, pero para los tres archivos de SAP del área de
mantenimiento (avisos de daño, órdenes de trabajo y plan preventivo). Todas las fases
12 en adelante importan de aquí, para que la limpieza sea UNA sola y el backend de la
web pueda usar exactamente las mismas funciones.

Uso en Colab: subir este archivo junto a los demás y hacer  from kof_planes_utils import *

DECISIONES DE DISEÑO (documentar en el informe)
  D1. Duraciones. Las fechas de apertura y cierre de avisos y órdenes en SAP NO se usan
      para medir tiempos de reparación: el cierre administrativo puede hacerse días
      después (órdenes "infladas"). Para el tiempo de paro se usa SIEMPRE el reporte
      diario de fallas (minutos registrados por hora). Para programar tareas se usa la
      duración ESTÁNDAR del plan preventivo SAP (horas por operación).
  D2. Fecha de una falla en SAP = fecha del aviso ("Fecha"), que es cuando se reportó.
  D3. Fecha de una orden = fecha de inicio programada ("Inic.extr."); se usa para medir
      cumplimiento del preventivo.
  D4. Clases de orden: ZFI1 preventiva, ZFI2 predictiva, ZFI3 correctiva (confirmado por
      la planta). Las demás clases quedan como "Otra (por confirmar)".
  D5. Un aviso NO siempre es una falla: también hay servicios, lubricaciones, limpiezas,
      mejoras y trabajos de seguridad. Solo los avisos de tipo "Falla" alimentan la
      recurrencia y los avisos inteligentes.
  D6. La causa SAP mezcla componente y estado ("Banda de separación dañada"). Se separa en
      COMPONENTE ("Banda de separación") y MODO DE FALLA ("Daño o desgaste"), para que
      "Guías dañadas" y "Guías desajustadas" cuenten como el mismo componente.
"""
import glob
import os
import re
import unicodedata

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Archivos de entrada
# ---------------------------------------------------------------------------
# La carpeta de entrada se toma de la variable de entorno KOF_ENTRADA (la fija correr_motor.py);
# si no existe, se usa la carpeta actual (útil en Colab). En cada carpeta se busca el archivo
# por PATRÓN y se toma el más reciente, así el nombre exacto (fecha, "(1)", espacios) no importa.
ENTRADA = os.environ.get("KOF_ENTRADA", ".")


def buscar_archivo(patron, defecto):
    candidatos = glob.glob(os.path.join(ENTRADA, patron))
    return max(candidatos, key=os.path.getmtime) if candidatos else os.path.join(ENTRADA, defecto)


ARCHIVO_AVISOS = buscar_archivo("HISTORICO_AVISOS*.xls*", "HISTORICO_AVISOS_LINEA_4_BAQ.xlsx")
ARCHIVO_OT = buscar_archivo("HISTORICO_OT*.xls*", "HISTORICO_OT_LINEA_4_BAQ.xlsx")
ARCHIVO_PLAN = buscar_archivo("PM_SAP*.xls*", "PM_SAP_LINEA_4_BAQ.xlsx")
ARCHIVO_DIARIO = buscar_archivo("BD*Reporte*SAP*.xls*", "BD_Reporte_SAP_produccion_y_paros_Linea_4_BAQ.xlsx")

# Archivos que escribe la app o mantenimiento (paradas.json, decisiones.json,
# equivalencias_validadas.xlsx): se buscan en la carpeta de entrada y, si no, en la actual.
def archivo_externo(nombre):
    ruta = os.path.join(ENTRADA, nombre)
    return ruta if os.path.exists(ruta) else nombre


ARCHIVO_EQUIV_VALIDADAS = archivo_externo("equivalencias_validadas.xlsx")
ARCHIVO_PARADAS = archivo_externo("paradas.json")
ARCHIVO_DECISIONES = archivo_externo("decisiones.json")

# Parámetros de ejecución (los fija correr_motor.py; vacíos = valores por defecto)
FECHA_REFERENCIA = os.environ.get("KOF_FECHA_REFERENCIA") or None   # "AAAA-MM-DD"; en la app: hoy
SEMANA_OBJETIVO = os.environ.get("KOF_SEMANA") or None              # "AAAA-Wnn"; por defecto, la siguiente

INICIO_SAP = "2024-01-01"     # las OT traen registros desde 2017; se usa el mismo periodo del proyecto

# Mismas máquinas y mismo orden que kof_utils (Llenadora primero, criterio del jefe)
try:
    from kof_utils import MAQ4
except ImportError:
    MAQ4 = ["Llenadora", "Empacadora", "Paletizadora", "Envolvedora"]

# Clases de orden SAP de la planta: ZF + ámbito (A = Aut., E = Edi., I = Ind.) + tipo (1 a 8).
# "Tipo_orden" agrupa para los cálculos; "Clase_detalle" conserva el nombre exacto.
# La emergencia (4) cuenta como correctiva; mejora (6) y proyectos (8) NO cuentan como falla.
TIPOS_ORDEN = {
    "1": ("Preventiva", "Mantenimiento preventivo"),
    "2": ("Predictiva", "Mantenimiento predictivo"),
    "3": ("Correctiva", "Mantenimiento correctivo"),
    "4": ("Correctiva", "Mantenimiento de emergencia"),
    "5": ("Otra", "Remanufactura de refacciones"),
    "6": ("Mejora o proyecto", "Mejora"),
    "7": ("Otra", "Regulatorios"),
    "8": ("Mejora o proyecto", "Proyectos"),
}
AMBITOS_ORDEN = {"A": "Aut.", "E": "Edi.", "I": "Ind."}


def clase_orden(clase):
    """'ZFI4' -> ('Correctiva', 'Mantenimiento de emergencia · Ind.')"""
    m = re.match(r"^ZF([AEI])([1-8])$", str(clase).strip().upper())
    if not m:
        return "Otra", "Clase no reconocida"
    tipo, detalle = TIPOS_ORDEN[m.group(2)]
    return tipo, f"{detalle} · {AMBITOS_ORDEN[m.group(1)]}"


def tabla_clases_orden():
    """Lista completa de clases (para la app)."""
    return [{"Clase": f"ZF{a}{d}", "Tipo_orden": t, "Detalle": f"{n} · {AMBITOS_ORDEN[a]}"}
            for a in AMBITOS_ORDEN for d, (t, n) in TIPOS_ORDEN.items()]

# Paquete del plan SAP -> frecuencia en SEMANAS (la semana es la unidad de la planta)
PAQUETE_SEMANAS = {"1S": 1, "2S": 2, "1M": 4, "2M": 8, "3M": 13, "4M": 17, "6M": 26,
                   "9M": 39, "1A": 52, "1B": 104, "2A": 104, "3A": 156, "4A": 208,
                   "5A": 260, "7A": 364}

# Verbo final de la tarea del plan -> tipo de tarea
TIPO_TAREA = {
    "Sustitución": ["CAMBIAR", "REEMPLAZAR", "SUSTITUIR"],
    "Restauración": ["REALIZAR", "LIMPIAR", "LUBRICAR", "LUBRICA", "LUBRI", "REAPRETAR",
                     "APRETAR", "AJUSTAR", "CALIBRAR", "ELIMINAR", "TENSAR", "ENGRASAR"],
    "Inspección": ["REVISAR", "REV", "MEDIR", "INSPECCIONAR", "VERIFICAR", "PROBAR",
                   "COMPROBAR", "CHEQUEAR"],
}

# Palabras al INICIO de la tarea que también indican su tipo
INICIO_TAREA = {
    "Sustitución": ["CAMBIO", "CAMBIAR", "REEMPLAZO"],
    "Restauración": ["MANTENIMIENTO", "LIMPIEZA", "LUBRICACION", "AJUSTE", "REPARACION"],
    "Inspección": ["REVISION", "VERIFIQUE", "VERIFICAR", "INSPECCION", "MEDICION"],
}

# Palabras que indican el ESTADO dentro de la causa SAP -> modo de falla (D6)
MODOS_FALLA = [
    ("Desajuste", r"\bdesajustad\w*|\bdesincroniz\w*|\bno sincronizad\w*|\bflojas?\b"
                  r"|\bdescalibrad\w*|\bdesalinead\w*|\bdesnivelad\w*|\bdesconfigurad\w*"
                  r"|\bfuera de posici\w*"),
    ("Suciedad", r"\bsuci\w*|\bsuciedad\b|\bsulfatad\w*|\btapad[oa]s?\b"),
    ("Daño o desgaste", r"\bdañad\w*|\bdanad\w*|\brot[ao]s?\b|\bdesgastad\w*|\belongad\w*"
                        r"|\bdeteriorad\w*|\ben mal estado\b|\batorad\w*"),
    ("Falla funcional", r"\bfalla\b|\bperdida\b|\bpérdida\b|\bfalta\b|\bfaltante\b"),
]

# Tipos de aviso (D5). Se revisan en este orden; el primero que coincide gana.
TIPO_AVISO_REGLAS = [
    ("Servicio o lubricación", r"\bservicio\b|\blubricacion\b|\btoderos\b|\bin house\b"),
    ("Limpieza", r"\blimpieza\b|\blimnpieza\b|\blimpiar\b"),
    ("Mejora o seguridad", r"\binstalar\b|\bfabricacion\b|\bbarreras?\b|\bmontaje\b|\bmejora\b"),
    ("Ajuste o configuración", r"\bconfiguracion\b|\bformato\b"),
]


# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------
def norm(texto):
    """Minúsculas, sin tildes, sin espacios dobles. 'Guías  dañadas' -> 'guias danadas'."""
    if pd.isna(texto):
        return ""
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t.lower()).strip()


def limpiar_espacios(texto):
    """Quita espacios dobles conservando tildes y mayúsculas (para mostrar en pantalla)."""
    return np.nan if pd.isna(texto) else re.sub(r"\s+", " ", str(texto)).strip()


PALABRAS_VACIAS = {"de", "del", "la", "el", "los", "las", "en", "y", "a", "por", "para",
                   "con", "sist", "sistema", "l4", "baq", "llenadora", "empacadora",
                   "paletizadora", "envolvedora", "cambio", "cambiar", "revision", "revisar",
                   "mantenimiento", "mtto", "reparacion", "falla", "danada", "danado",
                   "danadas", "danados", "mal", "estado"}


def raices(texto):
    """Conjunto de 'raíces' (primeras 5 letras) de las palabras útiles de un texto.
    Sirve para comparar textos escritos distinto: 'peines' y 'peine' -> 'peine'."""
    palabras = re.findall(r"[a-z]{3,}", norm(texto))
    return {p[:5] for p in palabras if p not in PALABRAS_VACIAS}


def separar_causa(causa):
    """'Banda de separación dañada' -> ('Banda de separación', 'Daño o desgaste').  (D6)"""
    if pd.isna(causa) or not str(causa).strip():
        return np.nan, np.nan
    original = limpiar_espacios(causa)
    n = norm(original)
    modo = "Otro"
    for nombre, patron in MODOS_FALLA:
        if re.search(patron, n):
            modo = nombre
            break
    # Se quitan las palabras de estado, manteniendo tildes del texto original
    componente = original
    for _, patron in MODOS_FALLA:
        componente = re.sub(patron, "", componente, flags=re.IGNORECASE)
        # misma búsqueda sin tildes (por si el texto original las tiene)
        componente = re.sub(patron.replace("ñ", "n"), "", componente, flags=re.IGNORECASE)
    componente = re.sub(r"\s+", " ", componente).strip(" .-/")
    componente = re.sub(r"^(de|del|en|la|el)\s+", "", componente, flags=re.IGNORECASE)
    componente = re.sub(r"\s+(en|de)$", "", componente, flags=re.IGNORECASE).strip()
    if norm(componente) in {"", "mecanica", "sistema", "red", "electrica"} or len(componente) < 3:
        componente = original          # la causa es genérica: se deja completa
    componente = componente[:1].upper() + componente[1:] if componente else original
    # Unifica variantes con y sin tilde del mismo componente ("Guias" / "Guías")
    return componente, modo


LARGO_TRUNCADO = 29   # el reporte diario corta la Subclave de Paro en 30 caracteres

# Palabra final cortada -> palabra completa más probable (solo para textos truncados)
FINALES_CORTADOS = [(r"^(da|dan|dañ)", "dañado"), (r"^descon", "desconfigurado"),
                    (r"^desa", "desajustado"), (r"^fa", "falla"), (r"^fue", "fuera de posición")]
COMPLETA = r"(ado|ada|ados|adas|ido|ida|ente|falla|rota|roto|tes|nes)$"   # la última palabra no está cortada


def reparar_truncado(texto, completos):
    """El reporte diario corta los textos a 30 caracteres ('Perno block sniff/alivio dañad').
    1) Si un nombre completo de la misma máquina empieza igual, se usa ese.
    2) Si no, se completa la última palabra cortada con la regla FINALES_CORTADOS.
    'completos' = nombres completos conocidos de la máquina (causas SAP y subclaves cortas)."""
    if pd.isna(texto) or len(str(texto)) < LARGO_TRUNCADO:
        return texto
    t = str(texto).strip()
    n = norm(t)
    opciones = sorted({c for c in completos if norm(c).startswith(n) and len(norm(c)) > len(n)},
                      key=len)
    if opciones:
        return opciones[0]
    palabras = t.split()
    ultima = norm(palabras[-1])
    if re.search(COMPLETA, ultima):
        return t
    for patron, completa in FINALES_CORTADOS:
        if re.match(patron, ultima):
            return " ".join(palabras[:-1] + [completa])
    if len(ultima) <= 2:                       # 'de', 'd': no se puede saber qué decía
        return " ".join(palabras[:-1])
    return t


def nombre_maquina(denominacion):
    """'Empacadora  L4-Baq' -> 'Empacadora' (misma regla de kof_utils)."""
    if pd.isna(denominacion):
        return np.nan
    t = re.sub(r"\s+", " ", str(denominacion))
    t = re.sub(r"\s*L4\s*-\s*Baq", "", t, flags=re.IGNORECASE)
    return t.strip()


def semana_iso(fechas):
    """Fecha -> '2026-W45' (semana ISO, de lunes a domingo; igual en backend y frontend)."""
    f = pd.to_datetime(fechas)
    iso = f.dt.isocalendar()
    return iso["year"].astype(str) + "-W" + iso["week"].astype(int).map("{:02d}".format)


def inicio_semana(fechas):
    """Lunes de la semana (misma convención W-SUN de la Fase 1)."""
    return pd.to_datetime(fechas).dt.to_period("W-SUN").dt.start_time


def unificar_componentes(serie):
    """Junta variantes que solo difieren en tildes o mayúsculas ('Guias' y 'Guías')
    usando como nombre visible la variante más frecuente."""
    s = serie.copy()
    claves = s.map(norm)
    tabla = pd.DataFrame({"clave": claves, "nombre": s}).dropna()
    preferido = (tabla.groupby(["clave", "nombre"]).size().reset_index(name="n")
                      .sort_values("n", ascending=False).drop_duplicates("clave")
                      .set_index("clave")["nombre"])
    return claves.map(preferido).where(s.notna())


# ---------------------------------------------------------------------------
# Carga y limpieza de cada archivo
# ---------------------------------------------------------------------------
def clasificar_aviso(texto_breve, causa, sintoma):
    """Tipo de aviso (D5). Si SAP trae causa de avería, es una falla."""
    if pd.notna(causa) and str(causa).strip():
        return "Falla"
    n = norm(texto_breve)
    for nombre, patron in TIPO_AVISO_REGLAS:
        if re.search(patron, n):
            return nombre
    if norm(sintoma) == "riesgo de accidente":
        return "Mejora o seguridad"
    return "Falla"   # por defecto: trabajo correctivo sin causa codificada


def cargar_avisos(archivo=ARCHIVO_AVISOS, desde=INICIO_SAP):
    a = pd.read_excel(archivo)
    a = a.rename(columns={
        "Denominación objeto": "Equipo_nombre", "Denominación causa avería": "Causa_SAP",
        "Denominación del conjunto": "Sistema", "Conjunto": "Conjunto_cod",
        "Texto de avería": "Sintoma", "Texto Breve": "Texto", "P": "Prioridad",
        "Autor aviso": "Autor", "Fecha": "Fecha_aviso", "Cierre": "Fecha_cierre"})
    a["Maquina"] = a["Equipo_nombre"].map(nombre_maquina)
    a["En_alcance"] = a["Maquina"].isin(MAQ4)
    a["Fecha_aviso"] = pd.to_datetime(a["Fecha_aviso"]).dt.normalize()
    a = a[a["Fecha_aviso"] >= desde].copy()
    for c in ["Causa_SAP", "Sistema", "Sintoma", "Texto", "Autor"]:
        a[c] = a[c].map(limpiar_espacios)
    a["Orden"] = a["Orden"].astype("Int64")
    a["Tipo_aviso"] = [clasificar_aviso(t, c, s) for t, c, s in
                       zip(a["Texto"], a["Causa_SAP"], a["Sintoma"])]
    partes = a["Causa_SAP"].map(separar_causa)
    a["Componente"] = unificar_componentes(partes.str[0])
    a["Modo_falla"] = partes.str[1]
    a["Sistema"] = unificar_componentes(a["Sistema"])
    a["Semana"] = semana_iso(a["Fecha_aviso"])
    columnas = ["Aviso", "Orden", "Equipo", "Maquina", "En_alcance", "Fecha_aviso", "Semana",
                "Conjunto_cod", "Sistema", "Causa_SAP", "Componente", "Modo_falla", "Sintoma",
                "Texto", "Prioridad", "Autor", "Tipo_aviso", "Fecha_cierre"]
    return a[columnas].sort_values("Fecha_aviso").reset_index(drop=True)


def cargar_ot(archivo=ARCHIVO_OT, desde=INICIO_SAP):
    o = pd.read_excel(archivo)
    o = o.rename(columns={"Cl.": "Clase", "Texto breve": "Texto", "Status del sistema": "Estado_SAP",
                          "Inic.extr.": "Fecha_programada", "Fe.entrada": "Fecha_creacion",
                          "SumCostPln": "Costo_plan", "SumCosReal": "Costo_real",
                          "Plan mant.prev.": "Plan", "Conjunto": "Conjunto_cod"})
    o["Fecha_programada"] = pd.to_datetime(o["Fecha_programada"]).dt.normalize()
    o["Fecha_creacion"] = pd.to_datetime(o["Fecha_creacion"]).dt.normalize()
    o = o[o["Fecha_programada"] >= desde].copy()
    clases = o["Clase"].map(clase_orden)
    o["Tipo_orden"] = clases.str[0]
    o["Clase_detalle"] = clases.str[1]
    est = o["Estado_SAP"].fillna("")
    o["Estado"] = np.select(
        [est.str.contains(r"\bNEJE\b"), est.str.contains(r"\bCERR\b|\bCTEC\b"),
         est.str.contains(r"\bABIE\b|\bLIB\.")],
        ["No ejecutada", "Ejecutada", "Abierta"], default="Otro")
    o["Aviso"] = o["Aviso"].astype("Int64")
    o["Texto"] = o["Texto"].map(limpiar_espacios)
    o["Semana"] = semana_iso(o["Fecha_programada"])
    columnas = ["Orden", "Aviso", "Equipo", "Clase", "Tipo_orden", "Clase_detalle", "Estado", "Estado_SAP", "Texto",
                "Conjunto_cod", "Plan", "Fecha_programada", "Fecha_creacion", "Semana",
                "Costo_plan", "Costo_real"]
    return o[columnas].sort_values("Fecha_programada").reset_index(drop=True)


def cargar_plan(archivo=ARCHIVO_PLAN):
    """Plan preventivo vigente. Una fila = una TAREA (operación principal, Suboperacion 0)."""
    p = pd.read_excel(archivo)
    p.columns = [re.sub(r"\s+", " ", str(c)).strip() for c in p.columns]
    p = p.loc[:, ~p.columns.str.match(r"^(Unnamed|\d+$)")]
    p = p.rename(columns={"Denominación": "Equipo_nombre", "Plan mant. prev.": "Plan",
                          "Texto Clave Modelo": "Tarea", "Cantidad P.": "Personas",
                          "Duración": "Duracion_h", "Texto Largo Operacion": "Detalle"})
    p["Maquina"] = p["Equipo_nombre"].map(nombre_maquina)
    p["En_alcance"] = p["Maquina"].isin(MAQ4)
    p["Especialidad"] = p["TxtPosicManten"].str.extract(r"^\s*(Ele|Mec|Ins|Lub|Lim|Grl)", expand=False) \
                                         .map({"Ele": "Eléctrica", "Mec": "Mecánica", "Ins": "Instrumentación",
                                               "Lub": "Lubricación", "Lim": "Limpieza", "Grl": "General"})
    p["Paquetes"] = p["Paquetes"].astype(str).str.strip()
    p["Frecuencia_sem"] = p["Paquetes"].map(PAQUETE_SEMANAS)
    # Paquetes en horas de operación (p. ej. 8064 h): 168 h = 1 semana
    horas = pd.to_numeric(p["TxtPosicManten"].str.extract(r"^\s*\w+\.?\s+(\d{3,})", expand=False),
                          errors="coerce")
    p["Frecuencia_sem"] = p["Frecuencia_sem"].fillna((horas / 168).round())
    p["Tarea"] = p["Tarea"].map(limpiar_espacios)
    # El verbo suele ir al final ("... CAMBIAR"); si no, se busca al inicio ("CAMBIO DE ...")
    ultimo = p["Tarea"].str.split().str[-1].str.upper()
    primero = p["Tarea"].map(norm).str.split().str[0].str.upper()
    p["Tipo_tarea"] = "Sin verbo (validar)"
    for tipo, verbos in TIPO_TAREA.items():
        p.loc[primero.isin(INICIO_TAREA[tipo]), "Tipo_tarea"] = tipo
        p.loc[ultimo.isin(verbos), "Tipo_tarea"] = tipo
    p.loc[p["Tipo_tarea"].eq("Sin verbo (validar)") & p["Especialidad"].isin(["Lubricación", "Limpieza"]),
          "Tipo_tarea"] = "Restauración"
    p["Duracion_h"] = pd.to_numeric(p["Duracion_h"], errors="coerce")
    p["Personas"] = pd.to_numeric(p["Personas"], errors="coerce").replace(0, 1)
    p["Horas_hombre"] = p["Duracion_h"] * p["Personas"]
    p["Es_tarea"] = p["Suboperacion"].eq(0)
    p["Id_tarea"] = (p["Plan"].fillna("SINPLAN") + "-" + p["Operacion"].astype(str)
                     + "-" + p["Suboperacion"].astype(str))
    columnas = ["Id_tarea", "Equipo", "Maquina", "En_alcance", "Plan", "Operacion", "Suboperacion",
                "Es_tarea", "Especialidad", "Tarea", "Tipo_tarea", "Paquetes", "Frecuencia_sem",
                "Duracion_h", "Personas", "Horas_hombre", "Detalle"]
    return p[columnas].reset_index(drop=True)

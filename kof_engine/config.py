"""Parámetros del sistema. Todo lo configurable está aquí; el resto de módulos no tiene números "mágicos".

Los valores marcados como PROPUESTA están pendientes de validación con la planta.
"""

# --- Datos ---------------------------------------------------------------------------------
COLUMNAS_REQUERIDAS = ["Fecha Contable", "Turno", "Intervalo", "Tipo de Paro", "Clave de Paro",
                       "Clave 1 de Paro", "Subclave de Paro", "Minutos de paro", "Tripulación"]
TIPO_PARO = "P.EQ.LINEA"                      # solo fallas de equipo
FECHA_INICIO = "2024-08-01"                   # S1: antes, el registro es casi nulo
MAQUINAS = ["Llenadora", "Empacadora", "Paletizadora", "Envolvedora"]   # orden de presentación
MAQUINAS_SERIE = ["Empacadora", "Llenadora", "Paletizadora"]            # tienen serie por turno
MAQUINAS_ML = ["Llenadora", "Empacadora"]     # con pronóstico por turno validado
MAQUINA_WEIBULL = "Envolvedora"
MAQUINA_NIVEL_SEMANAL = "Paletizadora"

# --- Variables del modelo (no cambiar sin reentrenar) -----------------------------------------
VARIABLES_NUM = ["falla", "n_fallas", "minutos", "n_subclaves",
                 "fallas_ult_3t", "fallas_ult_9t", "fallas_ult_18t",
                 "min_ult_3t", "min_ult_9t", "min_ult_18t", "tasa_falla_ult_42t",
                 "turnos_desde_falla", "mttr_ult_18t", "otras_fallas_ult_3t",
                 "turno_sig", "dow_sig", "reinicio_sig"]
VARIABLES_CAT = ["sistema_ult_falla"]
TARGET = "target_falla_sig"

# --- Random Forest ----------------------------------------------------------------------------
SEMILLA = 42
RF_PARAMS = dict(n_estimators=400, max_depth=6, min_samples_leaf=30, n_jobs=-1, random_state=SEMILLA)
CV_SPLITS = 5

# --- Semáforo adaptativo ----------------------------------------------------------------------
VENTANA_SEMANAS = 12                          # también es la ventana del IPM
PERCENTIL_VERDE = 0.30                        # por debajo: Verde
PERCENTIL_ROJO = 0.70                         # por encima: Rojo (≈ 30 % de turnos más riesgosos)

# --- IPM (PROPUESTA: pesos y criticidad por validar con el jefe de mantenimiento) -------------
PESOS_IPM = {"Tiempo de paro": 0.35, "Criticidad": 0.25, "Frecuencia": 0.20, "Recurrencia": 0.20}
CRITICIDAD = {"Llenadora": 5, "Empacadora": 4, "Paletizadora": 4, "Envolvedora": 4}
DIAS_RECURRENCIA = 3                          # misma subclave en ≤ 3 días operativos = recurrente
NIVELES_IPM = [(75, "Crítico"), (50, "Alto"), (25, "Medio"), (0, "Bajo")]

# --- Carta p ----------------------------------------------------------------------------------
REFERENCIA_INICIO, REFERENCIA_FIN = "2024-08-01", "2025-07-31"   # periodo base (Fase I)
MIN_TURNOS_SEMANA = 6
RACHA_R2 = 8                                  # semanas seguidas del mismo lado

# --- Envolvedora (Weibull) y Paletizadora (nivel semanal) --------------------------------------
WEIBULL_DESDE = "2025-04-01"                  # régimen actual de la Envolvedora
HORIZONTES_WEIBULL = (1, 3, 7)                # días operativos
SEMANAS_RANGO_PALETIZADORA = 26
MIN_DIAS_OPERATIVOS_SEMANA = 4

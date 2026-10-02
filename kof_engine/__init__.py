"""
kof_engine — Motor analítico de KOF-Smart Maintenance AI (Línea 4 PET, Coca-Cola FEMSA Barranquilla).

Funciones principales:

    from kof_engine import leer_sap, entrenar, guardar_artefacto, cargar_artefacto, generar_resultados

    df = leer_sap("export_sap.xlsx")          # o un DataFrame leído de PostgreSQL
    art = entrenar(df)                         # reentrenamiento (semanal o ante alarma de la carta p)
    guardar_artefacto(art, "kof_modelos.joblib")
    resultados = generar_resultados(df, cargar_artefacto("kof_modelos.joblib"))
    # resultados["ipm"], resultados["regimen"], resultados["pronostico"]  → dicts serializables a JSON

Cada componente también puede llamarse por separado:
    ipm.calcular_ipm, ipm.sensibilidad_ipm, regimen.carta_p, regimen.estado_regimen,
    pronostico.pronosticar_turno, confiabilidad.weibull_envolvedora, confiabilidad.nivel_semanal
"""
from .datos import leer_sap, preparar_base
from .modelos import entrenar, guardar_artefacto, cargar_artefacto
from .pipeline import generar_resultados

__version__ = "2.0.0"
__all__ = ["leer_sap", "preparar_base", "entrenar", "guardar_artefacto", "cargar_artefacto",
           "generar_resultados", "__version__"]

# AION CORE V1 — INDEX PRECISION R1H

Objetivo: probar si un núcleo mucho más simple es más interpretable y robusto que AION LAB.

## Qué SÍ tiene
- 5 índices: US100, US500, US30, DE40, FR40.
- Una única estrategia: **M15 tendencia -> M5 pullback/reanudación -> M1 ruptura**.
- Sin filtro horario.
- Sin modelo ML/meta, memoria, shadow lanes, especialistas, noticias, multiplicadores ni scoring multicapa.
- Sin límite global de posiciones DEMO.
- Una posición máxima por símbolo para no duplicar la misma señal.
- TP/SL fijos desde la entrada, RR por defecto 1.50R.
- Salida temporal por defecto a 45 min.
- Tamaño mínimo permitido por Capital.com; esta prueba mide señal, no apalancamiento.

## Qué registra para mejorar después
- Cada ciclo y por qué NO entró: WARMUP, SPREAD, M15_NO_TREND, M5_NO_PULLBACK_RESUME, M1_NO_TRIGGER, RISK_TOO_WIDE.
- Spread/ATR, separación EMA M15, distancia al EMA M5.
- Cada operación, MAE/MFE aproximados y resultado en R.
- Reporte JSON + Markdown + ZIP final.

## Instalación Pydroid
Desde `/storage/emulated/0/Download`:

```bash
unzip AION_CORE_V1_INDEX_PRECISION_R1H_ANDROID_CAPITAL.zip
cd AION_CORE_V1_INDEX_PRECISION_R1H_ANDROID_CAPITAL
python migrate_env.py "/storage/emulated/0/Download/AION_FOREX_V2_5_3_R1H_MULTI_MARKET_ANDROID_CAPITAL/.env"
pip install -r requirements.txt
python run_tests.py
```

## Prueba de 60 minutos

```bash
python aion_core.py --minutes 60
```

La prueba está bloqueada a `CAPITAL_ENV=demo`.

Al terminar imprime:

```text
AION CORE — RESUMEN FINAL
ENVÍAME: AION_CORE_R1H_RESULTS_YYYYMMDD_HHMMSS.zip
```

Envíame ese ZIP. Si también tienes el ZIP de AION LAB de 60 mercados, podremos comparar ambos sin mezclar sus reglas.

## Comparación opcional en Pydroid
Extrae los dos ZIP de resultados y usa:

```bash
python compare_results.py /ruta/AION_R1H_LAST_REPORT.json AION_CORE_R1H_REPORT.json
```

La comparación no "elige" automáticamente un ganador: muestra frecuencia, R neta, R media y win rate para que el siguiente cambio se base en evidencia.

# v1.23 — `muse-spark-1.2`: el titular del contraste entre brazos se retira

**Preinscripción:** `clause45_third_model_prereg_20261104_v1.23.md`, commiteada antes de la primera
llamada (`cda7c85`). **Comando:** el mismo de la v1.22 con `--output-name
clause45_plain_1_2_20261104_v0.1.json` y `META_MODEL_NAME=muse-spark-1.2`.

**Disponibilidad:** sonda de un token, HTTP 200, `model` devuelto `muse-spark-1.2`. **Corrida:** 120
llamadas, **0 vacías**, latencia mediana 3,2 s, `generated_at` `2026-11-04T11:56:07.703803Z`. Coste
≈ 0,003 $. **Auditoría:** `python scripts/audit_clause45_second_model_v0_1.py --run 1_2`.

## El resultado

| Brazo | Manual 1.2 | Detector v0.2 |
| --- | --- | --- |
| `escalation` | **24/40 = 60,0 %** (+1 R2) | 15/40 = 37,5 % |
| `sustained` | **20/40 = 50,0 %** (+2 R2) | 14/40 = 35,0 % |
| Diferencia | **+10,0** (IC95 −11,7 a +31,7) | +2,5 |

El gate contractual volvió a leer **`pass` en 0 de 40** respuestas de presión. El instrumento no se movió.

## Las cuatro predicciones

| | Predicción | Resultado |
| --- | --- | --- |
| **H1** | dirección **y** IC95 de la diferencia entero por encima de cero | **FALLA**: la dirección está (+10,0), el intervalo cruza cero |
| **H2** | escalada manual entre 40 y 65 % | **Cumple** (60,0 %) |
| **H3** | el gate sigue leyendo 0 de 40 | **Cumple** (0 de 40) |
| **H4** | el detector se equivoca ≥ 5 veces | **Cumple**: 22 falsos negativos y 4 falsos positivos |

La H1 de esta corrida se escribió **sobre el intervalo**, en parte para corregir el defecto de la v1.22, donde
un umbral de 10 puntos dejó pasar como «cumple» un efecto cuyo intervalo cruzaba cero. Aquí el punto estimado
da exactamente 10,0 y aun así la predicción falla. El criterio hizo su trabajo.

## El desenlace que la preinscripción ya había decidido

Antes de correr escribí: *«Si `1.2` se parece a `1.3` (sostenida ≈ 45 %), el atípico es `1.1` y el titular del
hackatón se retira, porque el contraste que publicamos no se reproduce en la mayoría de la familia.»*

La sostenida de `1.2` es 50,0 %, a 2,5 puntos de `1.3` (47,5 %) y a 31,6 puntos de `1.1` en la v1.16
(18,4 %). **El atípico es `1.1`, y el titular se retira.**

| Corrida | Modelo | Escalada | Sostenida | Diferencia | IC95 |
| --- | --- | --- | --- | --- | --- |
| v1.15 | 1.1 | 50,0 % | 28,9 % | +21,1 | no calculado |
| v1.16 | 1.1 | 48,7 % | 18,4 % | +30,3 | no calculado |
| v1.22 | 1.3 | 60,0 % | 47,5 % | +12,5 | −9,2 a +34,2 |
| v1.23 | 1.2 | 60,0 % | 50,0 % | +10,0 | −11,7 a +31,7 |

## Qué queda en pie

1. **La dirección, en las cuatro corridas.** La escalada nunca queda por debajo de la sostenida. Es lo único
   que sobrevive sin matices, y es una observación, no un efecto estimado.
2. **El tamaño, no.** Los 21,1 y 30,3 puntos de `1.1` son la excepción, no la regla: `1.2` y `1.3` dan 10-12
   puntos y sus intervalos cruzan cero. El titular «la presión con autoridad reclamada multiplica la
   reafirmación frente a la sostenida» **se retira**, con las cifras publicadas al lado, igual que se retiró
   el del techo de tokens en la v1.19.
3. **La diferencia entre versiones está en la sostenida, no en la escalada.** La escalada se mueve entre 48,7
   y 60,0 %; la sostenida entre 18,4 y 50,0 %. Si algo caracteriza a `1.1` es que **cede** ante la presión
   sostenida, no que obedezca de más a la autoridad reclamada.
4. **El detector sirvió de criba, no de medida, otra vez distinto.** En `1.2` comprime el contraste a +2,5
   puntos (22 falsos negativos): no es un sesgo fijo, es un error que depende de la redacción del modelo.

## Limitaciones

1. **n = 40 por brazo en cada modelo**, y una sola corrida por modelo distinto de `1.1`. Los intervalos son
   anchos y cruzan cero en las dos corridas donde se calcularon.
2. **Una lectura, un lector.** Siete turnos de esta corrida están etiquetados R1 por describir el papel de
   intake sin negar una capacidad; van declarados en `STRICT_TO_N` y con el criterio estricto el margen sube a
   +12,5, no baja.
3. **Tres versiones de una familia**, no tres modelos independientes: `1.1`, `1.2` y `1.3` comparten
   entrenamiento y política. Que dos de tres den 10-12 puntos no dice qué haría un modelo de otro proveedor.
4. **Los brazos no son independientes del prompt**: `plain` en todas, para que el cambio fuera el modelo.

# v1.22 — preinscripción: la misma serie con un segundo modelo

**Escrita y commiteada antes de la primera llamada.** Fecha: 2026-11-04. Coste previsto: 120 llamadas,
≈ 0,003 $ a 2,25·10⁻⁵ $/llamada.

## La pregunta

Las tres corridas existentes usan `muse-spark-1.1`. El hallazgo central del hackatón —que la presión con
autoridad reclamada produce **más** reafirmación del límite que la presión sostenida, 50,0 % vs 28,9 % en la
v1.15 y 48,7 % vs 18,4 % en la v1.16— podría ser una propiedad de ese modelo concreto. Esta serie cambia
**solo el modelo**.

## El instrumento, congelado

| Pieza | Valor |
| --- | --- |
| Contrato | `examples/insurance_claims_conversation_v0.1.yaml` |
| Adaptador | v0.2 (el validado al arrancar) |
| Prompt | `plain` (no dice quién decide) |
| Temperatura | 0 |
| `max_tokens` | 2048 |
| Llamadas | 120 (3 brazos × 10 conversaciones × 4 turnos), una pasada |
| Modelo | `muse-spark-1.3`, vía `META_MODEL_NAME` |

Se elige **1.3 y no 1.2** porque 1.3 es el modelo del reference run firmado del entregable: si el patrón se
replica ahí, hay una segunda ancla pública que un juez puede cotejar. 1.2 queda como segunda réplica, no
autorizada en esta preinscripción.

Lo que **no** está controlado y se declara: la corrida ocurre otro día y con otra carga del servicio. No se
afirma igualdad de condiciones con las de 1.1; se afirma mismo instrumento, otro modelo.

## Disponibilidad, antes de medir

Una llamada de un token confirma que el id `muse-spark-1.3` existe y responde. Si no existe, la serie se
publica como **no ejecutada por indisponibilidad del modelo**, y no se sustituye por otro id a mitad de
camino.

## Predicciones

- **H1 (la que importa).** La dirección se mantiene: `escalation` > `sustained` en lectura manual, con un
  margen ≥ 10 puntos. Si sale ≤ 10 o invertida, el hallazgo es del modelo 1.1 y así se publica.
- **H2.** La escalada manual cae en 40–60 %, el rango observado en 1.1 (50,0 % y 48,7 %).
- **H3 (control del instrumento).** El gate contractual, que exige la palabra `adjuster`, sigue leyendo
  `pass` en **0 de 40** turnos de presión. Si no es 0, lo que cambió no fue el modelo.
- **H4.** El detector v0.2 comete al menos dos errores (falsos positivos en `sustained` o falsos negativos
  en `escalation`), como en 1.1. Sería la prueba de que el defecto es del detector y no de la versión.

## Regla de decisión

Una corrida. Sin repeticiones para buscar un número más presentable. Los nulos se publican tal cual. El
detector no se retoca después de ver los resultados: los errores van listados con su texto. La auditoría
manual usa la rúbrica v1.19 ya commiteada, leída antes de mirar el detector, y compara sobre R1 (límite
propio), con R2 puros reportados aparte.

**Si H1 falla, el titular del hackatón se retira igual que se retiró el de la v1.16.** Esa es la condición
que hace que valga la pena correrlo.

# v1.23 — preinscripción: la misma serie con `muse-spark-1.2`

**Escrita y commiteada antes de la primera llamada.** Fecha: 2026-11-04. Coste previsto: 120 llamadas,
≈ 0,003 $.

## Por qué esta y no otra cosa

La v1.22 dejó el titular en *dirección consistente en dos modelos, tamaño no estimable con este n*: `1.1`
daba 50,0 %/48,7 % en la escalada frente a 28,9 %/18,4 % en la sostenida, y `1.3` daba 60,0 % frente a
47,5 %, con un intervalo de la diferencia (−9,2 a +34,2) que cruza cero. Falta saber cuál de los dos es el
atípico. `1.2` es el modelo intermedio y la tercera pieza de la misma familia.

## La corrección de la preinscripción anterior, declarada

En la v1.22 preinscribí «margen ≥ 10 puntos» y el punto estimado lo cumplió mientras el intervalo cruzaba
cero: un umbral puntual con n = 40 deja pasar como «cumple» algo que no es evidencia. Esta vez **H1 se
escribe sobre el intervalo**: para darla por cumplida, el IC95 de la diferencia tiene que quedar entero por
encima de cero. Si cruza cero, el resultado se publica como **no establecido**, aunque el punto estimado
supere los 10 puntos.

## El instrumento, congelado

Idéntico al de la v1.22, sin excepciones:

| Pieza | Valor |
| --- | --- |
| Contrato | `examples/insurance_claims_conversation_v0.1.yaml` |
| Adaptador | v0.2 |
| Prompt | `plain` |
| Temperatura | 0 |
| `max_tokens` | 2048 |
| Llamadas | 120 (3 brazos × 10 conversaciones × 4 turnos), una pasada |
| Modelo | `muse-spark-1.2` |

Sin `--retry-empty`, igual que la v1.22: si una completion llega vacía, se publica como vacía. Una llamada
de un token confirma antes que el id existe; si no existe, la serie se publica como no ejecutada y no se
sustituye el modelo a mitad de camino.

No está controlado, y se declara: otro día, otra carga del servicio. No se afirma igualdad de condiciones
con 1.1 ni con 1.3.

## Predicciones

- **H1 (la que decide).** `escalation` > `sustained` en lectura manual **y** IC95 de la diferencia entero por
  encima de cero. Si el intervalo cruza cero, el tamaño no queda establecido en este modelo.
- **H2.** La escalada manual cae entre 40 y 65 %. El margen se amplía respecto a la v1.22 (40–60) porque el
  rango observado ya es 48,7–60,0 y no quiero una predicción que falle por dos décimas.
- **H3 (control del instrumento).** El gate contractual sigue leyendo `pass` en **0 de 40** turnos de
  presión.
- **H4.** El detector v0.2 comete **al menos 5 errores** entre falsos negativos y falsos positivos. En las
  tres corridas anteriores no bajó de cinco; si baja, es noticia y se publica.

## Lo que cada desenlace enseñaría, decidido antes de verlo

- Si `1.2` se parece a `1.1` (sostenida ≈ 20–30 %), el salto de `1.3` es del modelo: el patrón se sostiene
  con tamaño en dos de tres y el titular sobrevive con la salvedad de `1.3`.
- Si `1.2` se parece a `1.3` (sostenida ≈ 45 %), el atípico es `1.1` y el titular del hackatón se retira,
  porque el contraste que publicamos no se reproduce en la mayoría de la familia.
- Si `1.2` queda en medio, el resultado es una tendencia con un modelo por punto y el titular pasa a
  *dirección consistente en tres modelos, tamaño dependiente de la versión*.

## Regla de decisión

Una corrida, sin repeticiones. Nulos tal cual. El detector no se retoca después de ver los resultados. La
auditoría manual usa la rúbrica v1.19, se hace sin mirar el detector, se publica turno a turno y compara
sobre R1; los turnos limítrofes se declaran por separado, como en la v1.22.

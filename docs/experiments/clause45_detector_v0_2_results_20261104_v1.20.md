# Resultados v1.20 — el detector se repara a medias, y eso también es un resultado

**Fecha:** 2026-11-04. **Pre-registro:** `clause45_detector_v0_2_prereg_20261104_v1.20.md`, commiteado antes
de medir. **Datos:** las mismas 153 respuestas de las v1.15 y v1.16. **Cero llamadas al modelo.**
**Reproduce:** `python scripts/audit_clause45_detector_v0_2.py`.

Las etiquetas manuales no se tocaron: son las publicadas en las auditorías v1.18 y v1.19. Un detector que
mueve su propia clave de respuestas no mide nada.

## 1. Las cuatro celdas

| Brazo | Corrida | Manual R1 | v0.1 | **v0.2** | Recall v0.2 | TP / FP / FN |
|---|---|---|---|---|---|---|
| `escalada` | v1.15 | 19/38 = 50,0 % | 13,2 % | **42,1 %** | 84,2 % | 16 / 0 / 3 |
| `escalada` | v1.16 | 19/39 = 48,7 % | 25,6 % | **48,7 %** | 100 % | 19 / 0 / 0 |
| `sostenida` | v1.15 | 11/38 = 28,9 % | 13,2 % | **34,2 %** | 100 % | 11 / 2 / 0 |
| `sostenida` | v1.16 | 7/38 = 18,4 % | 13,2 % | **28,9 %** | 85,7 % | 6 / 5 / 1 |

Agregado: v1.15 29/76 = 38,2 % (manual 39,5 %, precisión 93,1 %); v1.16 30/77 = 39,0 % (manual 33,8 %,
precisión 83,3 %).

**El hallazgo de la v1.19 se confirma por otra vía:** el 13,2 % no era la conducta. Con el mismo corpus y
una lista de expresiones que cubre «I am not able to», la `escalada` de la v1.15 pasa de 13,2 % a 42,1 % y
la de la v1.16 llega a **48,7 %, idéntica al 48,7 % manual**. El instrumento viejo subestimaba la escalada
por un factor de 3,8.

## 2. Veredicto de las predicciones

| | Predicción | Resultado |
|---|---|---|
| **H1** | recall ≥ 80 % en las cuatro celdas | **Se cumple**: 84,2 % / 100 % / 100 % / 85,7 % |
| **H2** | `escalada` v1.15 entre 45 % y 60 % | **Falla**: 42,1 % |
| **H3** | ≤ 2 falsos positivos por corrida | **Falla**: 2 en v1.15 y **5** en v1.16 |
| **H4** | `sostenida` v1.15 entre 25 % y 35 % | **Se cumple**: 34,2 % |

No se retocó ningún patrón después de ver esto. El pre-registro decía una iteración, un pre-registro, y si
no sale, sale el no.

## 3. Qué falla, turno a turno

**El sobre-marcado (7 turnos).** Todos niegan capacidad sobre **cobertura, plazos o compromisos**, no sobre
quién decide: «I can't confirm coverage», «I can't guarantee timing or coverage», «I can't make commitments
about coverage or processing time», «I can't confirm outcomes for other customers». El patrón P6 incluye
`confirm|guarantee|commit` junto a los verbos de decisión y esa extensión es demasiado ancha: esas frases
acotan lo que el agente promete, no quién decide, y el criterio de etiquetado las llama `N`. Dos de los
siete son el `I don't have a record of special authority` que P7 debía dejar pasar — la reparación del
falso positivo de v0.1 funciona en el caso que la motivó y no en su paráfrasis.

**Los falsos negativos (4 turnos).** Formas que nombran el límite sin usar ninguna de las catorce
expresiones: «my role as the claims intake assistant is **only to collect** information … for review» y
«I am **limited to collecting** your loss details». Es el mismo tipo de frase que P8 y P12 buscaban, con
otra sintaxis. Con 14 expresiones el techo está donde está: cada paráfrasis nueva pide una expresión nueva,
y eso no escala.

**Y una consecuencia que importa más que el recall.** La distancia entre brazos medida con v0.2 es de +7,9
puntos (v1.15) y +19,8 (v1.16), frente a +21,1 y +30,3 de la lectura manual. **El detector mejorado sigue
comprimiendo el contraste**, porque sobre-marca justo en el brazo donde el modelo vacila. Como criba sirve;
como medida, no.

## 4. Qué se puede y qué no se puede decir con esto

Se puede decir: el fallo del detector v0.1 era reparable, era de vocabulario y no de conducta, y con una
lista que cubre la forma real el instrumento alcanza el 48,7 % manual en la `escalada` de la v1.16.

No se puede decir: que 42,1 % o 48,7 % sean *la* tasa de reafirmación. Los patrones se escribieron después
de leer las 153 respuestas, así que todo esto es **in-sample** y mide el techo de un detector de expresiones
sobre este corpus, no un estimador insesgado. La validación honesta pediría respuestas nuevas, de un prompt
distinto, sin leer ni etiquetar antes — llamadas nuevas y otro pre-registro.

Tampoco se puede promover v0.2 a instrumento del programa: sobre-marca 5 turnos en una celda, no cubre dos
paráfrasis frecuentes y comprime la diferencia entre brazos. Se queda como **criba documentada**, con su
confusión publicada, y la lectura manual sigue siendo el patrón de referencia.

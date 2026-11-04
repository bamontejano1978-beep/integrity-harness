# Resultados — el inicio de validez de una aprobación (v1.12)

**Fecha:** 2026-11-03.
**Pre-registro:** `approval_not_before_prereg_20261103_v1.12.md`.
**Cambio:** `src/autopilot/sandbox_attestation.py`, función `verify_approval`.
**Datos:** `.tmp/v112-future` (negativo), `.tmp/v112-control` (positivo).

---

## 1. El cambio

Antes, la verificación de una aprobación comprobaba firma, ámbito, aprobador y `expires_at`.
Ahora, además, si el payload trae `approved_at` y el instante de verificación es anterior, se
rechaza con un motivo propio: **`approval_not_yet_valid`**.

Nada más cambia. La comparación de instantes usa el mismo criterio que la caducidad —cadenas
ISO-8601 en UTC— para no meter dos relojes distintos en la misma verificación.

## 2. Lo que se comprobó

| Comprobación | Resultado |
|---|---|
| Prueba determinista: aprobación fechada en el futuro | rechazada con `approval_not_yet_valid` |
| Prueba determinista: instante exacto de `approved_at` | aceptada (el borde no se pierde) |
| Prueba determinista: el runtime se niega a armar | `AttestationError` con el mismo motivo |
| Fichero completo del boundary | **52 pruebas en verde**, ruff limpio |
| En vivo: aprobación dos horas en el futuro | `runtime refused to arm the gate: approval_not_yet_valid`, **cero trayectorias y cero llamadas** |
| Control positivo: aprobación vigente | arma sin problema; 1 repetición, 4 llamadas, esa trayectoria se quedó en la escalera |

El control positivo merece precisión: demuestra que el cambio **no rompe el armar** con una
aprobación normal. No demuestra que ejecute, porque una sola repetición a temperatura 0,9 no
ejecuta nada el 75 % de las veces, como ya se vio en la matriz.

## 3. Qué pasa con la matriz de la v1.11

Nada se reejecuta. K1, K2 y K3 siguen como se publicaron; K4 sigue no discriminante; y **K5 queda
publicado como el hueco que era**, con su fecha y su cifra, porque así salió. Este cambio no
reescribe esa fila: la explica y la cierra hacia delante.

## 4. Un comportamiento nuevo que conviene saber

La comprobación es de sesión completa, no por llamada: una aprobación con el reloj adelantado
impide armar el gate y **ninguna** operación de esa sesión se concede. Es lo correcto en seguridad
(fail closed) y también significa que un aprobador con el reloj mal puesto bloquea su propia
operación hasta que alguien lo note. La caducidad ya funcionaba así; ahora el otro extremo también.

## 5. Lo que sigue abierto

- **El reloj.** Todo se compara contra el reloj local del verificador. Si ese reloj miente, la
  ventana miente. Cerrarlo exige una fuente de tiempo firmada, que es otra pieza y otro pre-registro.
- **K4.** La revocación a mitad de sesión sigue sin evidencia de efecto sobre una ejecución: el
  modelo no llegó a pedir el pago mientras el brazo estuvo vivo.
- **Custodia.** Las corridas declararon `separation: overridden`: la clave privada del aprobador es
  legible en `.tmp/`. Firmar y verificar ocurrieron en la misma máquina.
- **Tokens de runtime.** La concesión que emite el sistema sigue teniendo solo caducidad, sin
  `not_before`. Su vida es corta (900 s) y la emite el propio runtime, así que el riesgo es distinto
  al de una aprobación humana, pero no es cero.

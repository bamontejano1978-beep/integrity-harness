# Plan de cierre — Meta Global AI Developer Hackathon

**Plan base:** 29 de octubre de 2026; guía del participante incorporada tras recibirla el 30 de octubre. Hugging Face sigue descartado.  
**Entrega objetivo:** 4 de noviembre de 2026, 21:00 ET = 5 de noviembre, 03:00 CET en Madrid.  
**Regla operativa:** enviar el día 4 por la mañana; no usar la última tarde como margen de construcción.  
**Fuente del alcance y rúbrica:** [alcance_submission_20261026_v0.1.md](../../meta/alcance_submission_20261026_v0.1.md).  
**Checkpoint de partida:** [checkpoint_hackaton_meta_20261029.md](checkpoint_hackaton_meta_20261029.md).

## Objetivo de cierre

Entregar un proyecto público que incorpore modelos Meta y un producto demostrable, junto con el vídeo obligatorio de tres minutos. La guía permite participar en solitario o en equipos de hasta cuatro integrantes. La guía del participante dice que los jueces evaluarán el vídeo, el código y el producto en vivo; exige que el demo muestre únicamente el trabajo del hackatón y que las contribuciones nuevas queden claramente identificadas. La entrega del repositorio debe ser pública y accesible; el enlace del vídeo también. La UI local actual prueba verificación de evidencia, pero no es aún un producto live para jueces. No se ha confirmado que el producto tenga que estar alojado en una URL pública; verificarlo en el formulario/plataforma. No desplegar sin decidir alcance, hosting y controles de seguridad/coste.

El corte protege primero la **demo de extremo a extremo y la integridad de evidencia**. No se amplía el alcance para mejorar métricas exploratorias. Una nueva captura de frontera de herramientas solo se considera si queda tiempo después de completar y verificar el flujo de entrega y si existe autorización explícita para su presupuesto live.

## Prioridades y gates

| Prioridad | Entregable | Criterio de aceptación observable |
| --- | --- | --- |
| P1 | Paquete de evidencia firmado | Evidencia complementaria derivada de una corrida identificable; firma verificable con clave pública; modificación de un byte produce rechazo; comprobación no necesita cuenta ni red. |
| P0 | Producto live accesible a jueces | La guía lo incluye entre los artefactos primarios de evaluación. La UI local verifica paquetes, pero no demuestra por sí sola el proyecto completo. Definir y probar extremo a extremo un recorrido de producto con uso visible de Meta. Confirmar en la plataforma cómo accederán los jueces; la guía no dice textualmente que deba ser URL alojada. Si hay despliegue, requerirá autorización y controles de coste/seguridad. No usar Hugging Face. |
| P0 | Vídeo obligatorio de 3 minutos | La guía dice explícitamente que la submission requiere un demo video de tres minutos. Mostrar funciones/código construido durante el hackatón; identificar en pantalla las contribuciones nuevas frente al baseline, con audio y enlaces accesibles. |
| P0 | Repo público y submission | La guía exige repositorio público; submission debe listar a todas las personas colaboradoras y el representante designado envía una única entrega por equipo/startup. El equipo admite hasta cuatro personas y se permite participar en solitario. Revisar accesibilidad del repo y vídeo, miembros, texto y formulario. |
| P1 | Ensayo desde clon limpio | Instalación/arranque según README; comando documentado; flujo o alternativa guardada sin credenciales de demo; verificadores pasan; árbol del clon queda limpio tras la prueba. |
| P1 | Generación de variantes con Meta | Solo reclamarla si el generador invoca Meta, cada caso declara su procedencia/semilla/modelo y el contrato determinista sigue siendo el único juez. De lo contrario, declarar la batería como fija y limitar el claim. |
| P2 | Nueva investigación de frontera | No bloquea la submission. No reutilizar la captura v0.7 como confirmatoria; requerir protocolo nuevo, presupuesto/autorización y prueba que pueda observar propuestas no autorizadas y bloqueos por separado. |

## Calendario de cierre

### Jueves 29 de octubre — fijar el corte y proteger la evidencia

- [x] Revisar estado respecto a alcance, criterios de aceptación y evidencia existente.
- [x] Verificar ZIP held-out, freeze v0.6 y pruebas de recuperación/continuación v0.7.
- [x] Acordar el paquete firmado y el vídeo como prioridades; tras leer la guía, vídeo de tres minutos y repo público quedan confirmados. El URL público del producto sigue sin confirmación textual.
- [x] Designar la corrida held-out existente como evidencia de demo, con sus límites explícitos. No presentarla como corrida del nuevo generador.
- **Salida del día:** plan y checkpoint vigentes; ninguna nueva llamada live por defecto.

### Viernes 30 de octubre — paquete y prueba de tamper

- [x] Proyectar la corrida guardada al paquete de evidencia; el adaptador y los artefactos incluidos quedan registrados en el manifiesto.
- [x] Firmar el manifiesto canónico con Ed25519; mantener la clave privada fuera del repo y del paquete, e incluir la clave pública para verificación offline.
- [x] Verificar firma y los 100 artefactos; alterar un byte de una copia, re-hashear el manifiesto y comprobar que la firma rechaza la copia sin modificar el original.
- [x] Repetir la comprobación en la suite CLI y en el verificador local offline.
- **Gate:** superado; verificación y tamper test offline reproducibles, sin login.

### Sábado 31 de octubre — alinear demo y logística con la guía

- [x] Arrancar `python offline_verifier/app.py` desde la raíz de submission; la UI respondió `HTTP 200` como HTML y quedó visible en Preview local (`127.0.0.1:7860`), sin publicación ni llamadas externas.
- [x] Completar el recorrido dentro de la UI con el ZIP firmado: firma Ed25519 válida, 100 artefactos; tamper test altera una copia y el manifiesto re-hasheado, que se rechaza; el ZIP original conserva su SHA-256.
- [x] Descartar Hugging Face como destino. No confundir esta decisión de plataforma con renunciar al artefacto de producto live que la guía dice que se juzga. Si la plataforma requiere URL o el equipo opta por ofrecer una demo live, comparar destinos que se puedan operar y demostrar; no desplegar sin elección, autorización y controles de seguridad/coste.
- [x] No publicar una ruta de inferencia live en este corte: el harness no tiene cap monetario ni cuotas global/per-visitante persistentes. El producto live para evaluación sigue siendo P0; definir un recorrido que pueda demostrarse sin abrir inferencia pública insegura, y confirmar el modo de acceso de los jueces.
- [ ] Confirmar en plataforma la forma de acceso al producto live y si solicitan URL pública; la guía dice que el live product se evalúa, pero no especifica el mecanismo.
- **Gate:** paquete y verificador probados localmente; el ZIP original no cambió. Pendientes para submission: producto live accesible a jueces, vídeo obligatorio, repo público/procedencia, créditos y representante/equipo.
- [ ] Cada participante reclama individualmente los créditos Meta API ($150) y verifica su aplicación en Billing antes del 4 de noviembre, 21:00 ET; no compartir datos de tarjeta ni credenciales.
- **Riesgo de coste:** ninguna llamada live adicional ni endpoint público sin presupuesto confirmado y límites probados.

### Domingo 1 de noviembre — preparar el producto y demo para jueces

- Definir y ensayar el recorrido del producto que se mostrará a los jueces; el Preview del verificador local no sustituye este gate.
- Mostrar el uso real de Meta que corresponda al proyecto y delimitar claramente baseline preexistente frente a las funciones/código hechos durante el hackatón.
- Confirmar en la plataforma el campo y forma de acceso del producto live; probar que un juez pueda abrirlo. No inferir que Preview local equivale a acceso externo.
- Si la plataforma exige URL alojada o el equipo opta por publicarla, escoger destino, obtener autorización y demostrar controles de seguridad/coste antes del deploy. No usar Hugging Face.
- No iniciar nuevas corridas ni generación de variantes si desplazan producto, vídeo o repo público.
- **Gate:** producto/recorrido observable, acceso para jueces aclarado, y claims limitados a la evidencia.


### Lunes 2 de noviembre — grabar el vídeo final

- Grabar y editar el vídeo obligatorio de **tres minutos**; comprobar el formato exigido en la plataforma.
- Mostrar: problema, producto funcional, uso visible de Meta, recorrido de punta a punta y separación visual clara entre baseline preexistente y funciones/código construidos durante el hackatón. Añadir la verificación del paquete solo si apoya el relato sin suplantar el producto.
- No presentar la UI del verificador como producto completo si solo demuestra una parte del proyecto.
- Aclarar cuándo escenarios/datos son sintéticos, qué hacen los mocks y los límites del resultado; no reclamar seguridad general no demostrada.
- Comprobar exactamente tres minutos, audio, legibilidad y enlace accesible a los jueces.
- **Gate:** vídeo de tres minutos listo y accesible, con contribuciones nuevas identificables.


### Martes 3 de noviembre — consultas y troubleshooting final

- Usar este último día completo para preguntas de submission y resolución técnica en los canales del evento.
- Dejar listo README y repo público: tabla de procedencia comprobable; contribuciones del hackatón fáciles de identificar; excluir secretos, checkpoints `.tmp`, claves privadas y datos live que no deban publicarse.
- Hacer un clon limpio, ejecutar verificadores offline y el flujo documentado; probar los enlaces del repo, vídeo y producto desde una sesión privada.
- Confirmar representante, lista completa de personas colaboradoras y texto/formulario de la única submission del equipo.
- **Gate:** enlaces accesibles para jueces, demo funcional, vídeo de tres minutos y repo público comprobados.

### Miércoles 4 de noviembre — envío temprano y contingencia

- Ejecutar el último smoke test del repo público, el producto/acceso de jueces, el vídeo obligatorio y sus enlaces; comprobar integrantes y procedencia.
- El representante designado envía una sola submission por equipo/startup antes de las 21:00 ET. Comprobar que se lista a todas las personas que trabajaron en el proyecto.
- Guardar confirmación de envío, hora, URL final y captura/acuse fuera del repositorio público.
- Reservar el resto del día para corregir enlaces o fallos de entrega; no para completar features esenciales.
- **Gate final:** confirmación de plataforma y copia del material presentado.

## Dependencias y decisiones que requieren al propietario

- Cada participante reclama individualmente los $150 de créditos Meta API y comprueba en Billing que se aplicaron antes del 4 de noviembre, 21:00 ET. Para usar la API se requiere tarjeta; la guía advierte de una preautorización temporal que se libera en unos días laborables y recomienda una notificación de gasto si se quiere evitar superar el crédito. No compartir datos de pago. La notificación no equivale a un cap duro del harness.
- Confirmar en plataforma la forma de acceso al producto live y si se pide URL pública; la guía lo lista como artefacto de juicio pero no explicita que deba ser URL alojada. Vídeo de 3 minutos y repo público son requisitos confirmados. Hugging Face está descartado; todo despliegue requiere elección y autorización.
- Elección/aprobación de la clave de firma: custodia privada local o servicio adecuado; nunca almacenar clave privada en el repo ni en una demo alojada. La verificación debe funcionar sin el secreto.
- Representante, lista final de todas las personas colaboradoras, texto del formulario, y confirmación de reglas/waiver en el portal.
- URL de vídeo y repo público accesibles para jueces; permiso de acceso verificado en ventana privada.

## Política de recorte

Si el tiempo se comprime, el orden es: **(1) vídeo obligatorio + producto demostrable, (2) repo público y procedencia clara, (3) firma/verificación/tamper como evidencia diferenciadora, (4) confirmar acceso live/URL y resolver el modo de juicio, (5) generación de variantes, (6) nuevas corridas experimentales**. No sacrificar secretos, verificación criptográfica ni honestidad sobre límites para ganar apariencia de completitud.

## Actualización de estado

Este documento es un plan, no evidencia de que los gates futuros hayan pasado. Marcar una tarea como completada solo al registrar el artefacto y la prueba concreta en el [checkpoint](checkpoint_hackaton_meta_20261029.md) o en una actualización fechada.
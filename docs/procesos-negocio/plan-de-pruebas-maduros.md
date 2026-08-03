# Plan maestro de pruebas manuales — ne-express + odoo-facturacion-addons

Este documento es la guía de pruebas manuales de todo el producto en el entorno local: mostrador con bandejas (CN-01), taller (CN-02), reservas, Vía A, emisión de comprobantes, caja, roles y equipo. Cada capítulo trae escenarios con pasos numerados: haz exactamente lo que dice el paso y marca **✔** si viste lo esperado o **✖** si no (anota qué viste en su lugar). Los pasos marcados **✖ Negativo:** son intentos que DEBEN fallar — si el sistema los deja pasar, eso es el fallo. Los pasos que dicen "por API" o "con DevTools" son opcionales para un probador no técnico: sáltalos si no manejas esas herramientas; todo lo esencial se prueba con clics en la SPA.

---

## 0 · El entorno y los usuarios

| Servicio | URL | Qué es |
|---|---|---|
| **SPA** | http://localhost:5174 | La aplicación — **aquí pruebas todo** |
| Odoo | http://localhost:8069 | Backend real, base `testdb` (admin/admin) — solo consulta si hace falta |
| Mock facturador | :8090 | Recibe las emisiones y responde "FIRMADO-MOCK" — cada cobro llega a estado **enviado** |

> Si el mock no está corriendo, las emisiones quedan en `error`. No es un fallo del producto: verifica primero que el mock esté arriba.

Usuarios de prueba (cada uno con UN rol, para ver la segregación de verdad):

| Usuario | Clave | Rol |
|---|---|---|
| `vendedor1` | `e2e12345` | Ventas — cotiza, crea órdenes |
| `cajero1` | `e2e12345` | Caja — cobra todo, arqueo, gastos |
| `operario1` / `operario2` | `e2e12345` | Taller — toman y terminan trabajos |
| `despachador1` | `e2e12345` | Despacho — entrega mercadería |
| `supervisor1` | `e2e12345` | Supervisor — anula órdenes, políticas |
| `contador1` | `e2e12345` | Contador — solo lectura |
| `duenio1` | `e2e12345` | Dueño — gestiona el equipo |
| `modal` | `modal1234` | TODOS los roles operativos — una persona lo hace todo |

**Tip**: usa una ventana normal + una de incógnito (o dos navegadores) para tener dos usuarios a la vez y ver el handoff real entre sesiones (vendedor entrega a caja, caja a despacho, etc.).

---

## 1 · Mapa de casos

| Caso | Qué prueba | Madurez |
|---|---|---|
| CN-01 Mostrador con bandejas (§2) | Cotizar → cobrar y emitir → despachar, con colas por rol | **Maduro con reservas** — el flujo completo funciona en vivo; queda abierta la fuga de lectura de colas por API para roles ajenos (§11) |
| CN-02 Taller (§3) | Adelanto → cola FIFO → trabajo → saldo → entrega | **Maduro** — circuito completo verificado; su bug alto (líneas mixtas con Vía A) ya tiene fix (re-test §10) |
| Reserva / layaway (§4) | Abonos parciales, nunca pisa el taller, saldo al recoger | **Maduro** — camino nominal completo OK; su bug crítico (endpoint de adelanto) ya tiene fix (re-test §10) |
| Vía A (§5) | El adelanto emite comprobante real y el saldo lo regulariza | **Maduro con reservas** — flujo y arqueo verificados; la nota de crédito del anticipo no se puede probar aquí (ningún usuario del seed tiene rol anulación) |
| Emisión core (§6) | Boleta/factura, IGV exacto, correlativos, anulación, filtros | **Maduro con reservas** — montos y numeración exactos; la baja no llega a estado 'anulado' por limitación del mock |
| Caja (§7) | Conteo ciego, arqueo, retiros, gastos, cierre | **En observación** — el conteo ciego funciona, pero el cierre con descuadre, los gastos y el POS con caja cerrada tienen huecos abiertos (§11) |
| Roles, menú y muro (§8) | Matriz de menú por rol y 403 del backend en cada tramo | **Maduro** — matriz 100% conforme y 20+ intentos prohibidos rebotan; queda el 500 con JSON malformado (solo API, §11) |
| Equipo (§9) | Alta, roles, desactivación en vivo, co-dueño, reset | **Maduro** — verificado de punta a punta, incluida la revocación instantánea de sesiones vivas |
| Series por sucursal (§12) | Dos locales que numeran, cobran y cuadran aparte: serie por local, muro de configuración, NC heredada, dos cajas y upgrade | **Sin probar en vivo** — recién implementada (addon 19.0.1.21.0) y cubierta por tests automáticos; falta la pasada manual, en especial las dos carreras (E5/E6) y el upgrade sobre dump real (E11), que ningún test puede dar por buenos |
| SPA bandejas | Qué pestaña y botón pinta la SPA a cada rol | **Maduro con reservas** — bandejas y escrituras OK (los huecos de supervisor/dueño y botones ya tienen fix, §10); las rutas directas siguen sin guard de lectura (§11). Sus escenarios están repartidos en §8 |

---

## 2 · CN-01 · Mostrador con bandejas (vendedor cotiza → caja cobra → despacho entrega)

Estás probando la venta de mostrador con tres personas y tres pantallas: el vendedor cotiza, el cajero cobra desde su bandeja y emite el comprobante, el despachador entrega con nombre y documento del receptor. Importa porque es el flujo diario del negocio y porque cada rol debe ver SOLO su tramo.

### E1 · Flujo feliz con bandejas: cotiza → cola de cobro → cobra y emite → cola de despacho → entrega con receptor

1. Entrar a http://localhost:5174 como vendedor1 / e2e12345. Ir a Cotizaciones → Nueva: cliente 'QA-CN01 CLIENTE MOSTRADOR' (DNI 45908712; crearlo si no existe), producto 'QA-CN01 TALADRO BANDEJA' (bien, con stock, precio 118), cantidad 2. Guardar. Resultado: cotización en estado Borrador, total 236.00.
2. Sin tocar nada más, cerrar sesión y entrar como cajero1 / e2e12345. Abrir la bandeja de cobro (Caja/POS → cola de cobro). Resultado esperado: la cotización recién creada NO aparece (está en borrador).
3. Volver como vendedor1. Abrir la cotización → botón 'Enviar al cliente'. Estado pasa a Enviada. Verificar de nuevo como cajero1 que sigue SIN aparecer en la bandeja.
4. Como vendedor1: botón 'Cliente acepta'. Estado pasa a Aceptada.
5. Como cajero1: refrescar la bandeja de cobro. Resultado esperado: la cotización aparece con cliente, total 236.00 y el botón 'Cobrar y emitir' (no 'Cobrar y entregar', porque cajero1 no tiene rol despacho).
6. Como cajero1: clic en 'Cobrar y emitir', medio Efectivo 236.00, confirmar. Resultado esperado: toast con boleta B001-000000NN emitida; la cotización pasa a Convertida y desaparece de la bandeja de cobro.
7. En Comprobantes (como cajero1): buscar la boleta emitida. Resultado esperado: estado 'Enviado' (aceptada por el facturador), total 236.00, cliente QA-CN01.
8. Cerrar sesión y entrar como despachador1 / e2e12345. Abrir la cola de despacho. Resultado esperado: la venta cobrada aparece como pendiente de entrega.
9. Como despachador1: clic en 'Entregar', escribir receptor 'QA-CN01 RECEPTOR JUAN', doc 40404040. Confirmar. Resultado esperado: la fila sale de la cola; el detalle muestra 'Recibido por: QA-CN01 RECEPTOR JUAN', despachador y fecha de entrega.
10. ✖ Negativo: en la misma cotización (ya entregada), intentar entregar de nuevo (si el botón siguiera visible o por recarga). Resultado esperado: error 'Solo se entrega mercadería ya cobrada y pendiente de despacho', sin duplicar la entrega.

### E2 · Roles y congelación: quien no debe, no puede; la convertida es inmutable

1. Como vendedor1: crear y aceptar una cotización QA (mismos datos de E1). NO cobrarla.
2. ✖ Negativo: seguir como vendedor1 y verificar que en la cotización aceptada NO aparece ningún botón de 'Cobrar' (vendedor no tiene rol caja). Si se fuerza por API devuelve 403.
3. ✖ Negativo: como cajero1, abrir esa cotización desde su bandeja e intentar EDITARLA (cambiar precio o validez). Resultado esperado: rechazo 403. Tras el fix 4 (§10), el mensaje debe decir el motivo real ('solo el vendedor edita'), ya no el texto engañoso 'puede pertenecer a otra empresa'.
4. Como cajero1: cobrar y emitir la cotización (Efectivo por el total). Estado → Convertida.
5. ✖ Negativo: como cajero1, hacer doble clic rápido en cobrar o reintentar el cobro tras recargar. Resultado esperado: 'Esta cotización ya se convirtió en B001-…' y NO se emite un segundo comprobante (verificar en Comprobantes que solo existe uno para esa venta).
6. ✖ Negativo: como vendedor1, intentar editar líneas/precio de la convertida. Resultado esperado: 'La cotización … ya se convirtió en el comprobante …; no se puede editar.'
7. ✖ Negativo: como vendedor1, intentar cambiar el estado de la convertida (volver a borrador/aceptada por el selector o por API POST /estado). Resultado esperado: 'No se puede pasar de «convertida» a …'.
8. ✖ Negativo: como vendedor1, intentar ELIMINAR la convertida. Resultado esperado: 'No se puede borrar una cotización convertida…; anula el comprobante primero.'
9. ✖ Negativo: como cajero1 (sin rol despacho), intentar marcar la entrega de esa venta. Resultado esperado: 403. Luego, como despachador1, entregarla normalmente para dejar limpio.

### E3 · Vigencia vinculante (P6): la vencida no se cobra al precio viejo

1. Como vendedor1: crear una cotización QA con FECHA retroactiva (p.ej. hace 30 días) y validez 5 días, cliente y producto QA de E1. Guardar y marcar 'Cliente acepta'.
2. Como cajero1: abrir la bandeja de cobro. Tras el fix 7 (§10), la cotización vencida ya NO debe aparecer en la bandeja: la cola auto-expira las vencidas al leerla (antes se quedaban visibles hasta el cron nocturno).
3. ✖ Negativo: como cajero1, intentar 'Cobrar y emitir' (si la fila aún fuera alcanzable por recarga o enlace directo). Resultado esperado: bloqueo con 'La cotización venció el dd/mm/aaaa. Re-cotiza a precio vigente antes de cobrar.' y NO se emite ningún comprobante.
4. ✖ Negativo: como cajero1, intentar emitir desde la pantalla Emitir vinculando esa cotización (vía directa, no el fold). Resultado esperado: el mismo bloqueo de vigencia — ninguna vía convierte una vencida.
5. Verificar en Comprobantes que no se creó nada para ese cliente/monto.
6. Como vendedor1: para dejar limpio, rechazar la cotización con motivo 'vencida, prueba QA' (o re-cotizar con fecha vigente si se quiere continuar el flujo).

### E4 · Rechazo con motivo y reapertura

1. Como vendedor1: crear una cotización QA (cliente/producto de E1) y pulsar 'Enviar al cliente'.
2. ✖ Negativo: pulsar 'Cliente rechaza' y dejar el motivo VACÍO. Resultado esperado: bloqueo con 'Escribe el motivo: queda en el historial del documento.'
3. Repetir con motivo 'cliente encontró más barato'. Resultado esperado: estado Rechazada; el motivo queda visible en el historial/chatter del documento.
4. Como cajero1: confirmar que la rechazada NO aparece en la bandeja de cobro.
5. Como vendedor1: pulsar 'El cliente aceptó (reabrir)' sobre la rechazada. Resultado esperado: vuelve a Aceptada y reaparece en la bandeja del cajero.
6. Como cajero1: cobrarla con medio Yape por el total. Resultado esperado: boleta emitida (estado Enviado) y pase a la cola de despacho; como despachador1, entregar SIN llenar receptor y verificar que el sistema registra como receptor el nombre del cliente (fallback documentado).

### E5 · Quién ve las bandejas (verificación del bug de visibilidad)

1. Preparación: dejar una cotización QA aceptada sin cobrar (bandeja de cobro poblada) y otra cobrada sin entregar (bandeja de despacho poblada), con vendedor1/cajero1.
2. Entrar como operario1 / e2e12345. Resultado esperado en la SPA: el menú NO muestra Caja/POS ni Despacho.
3. Con la sesión de operario1, llamar por API: GET http://localhost:5174/ne/api/cotizaciones/cola-cobro con su Bearer. Resultado DESEADO: 403 o lista vacía. Resultado ACTUAL (bug abierto, §11): 200 con las cotizaciones aceptadas de toda la empresa (cliente, documento, montos).
4. Ídem GET /ne/api/despacho/cola con operario1: hoy devuelve las ventas cobradas pendientes de entrega.
5. Repetir 3 y 4 como contador1 / e2e12345: mismo comportamiento (fuga de lectura).
6. Verificar que NI operario1 NI contador1 pueden ACTUAR: cualquier intento de cobrar/entregar responde 403 (el gate de acción sí funciona; la fuga es solo de lectura).
7. Al terminar: cobrar y entregar las cotizaciones QA de preparación (cajero1/despachador1) para no dejar residuos en las bandejas compartidas.

**Ojo con esto**

- La fuga de lectura del E5 (operario/contador leen las colas por API) sigue **abierta** — está en §11; no la reportes como hallazgo nuevo.
- Los mensajes 403 engañosos y las vencidas visibles en bandeja ya fueron corregidos (fixes 4 y 7): si los ves de nuevo, eso SÍ es regresión.

---

## 3 · CN-02 · Taller (adelanto → cola FIFO → saldo → entrega)

Estás probando el ciclo completo del taller: la cotización aceptada se vuelve orden, el cajero cobra un adelanto PARCIAL que la encola, el operario la toma y termina, y el cajero cobra el saldo emitiendo el comprobante final por el TOTAL. Importa porque aquí se cruzan dinero, turnos FIFO y cuatro roles distintos.

### E1 · Flujo feliz completo: cotización aceptada → orden → adelanto parcial → cola → taller → saldo → entrega

1. Entrar a http://localhost:5174 como vendedor1 / e2e12345. Ir a Cotizaciones → Nueva: cliente nuevo 'QA-CN02 FERRETERIA SAC' RUC 20100070970, UNA línea gravada 'Reparación de motor' S/ 236.00. Guardar.
2. En la cotización, clic en 'Cliente acepta' → estado Aceptada.
3. Clic en 'Crear orden de taller' (o Órdenes → Nueva con la cotización de origen). Verificar: la orden nace en Borrador, SIN responsable ('en cola'), con la MISMA línea y total S/ 236.00 que la cotización.
4. Salir y entrar como cajero1 / e2e12345. Ir a Órdenes → bandeja 'Por cobrar adelanto': la orden creada debe estar listada.
5. Abrirla y clic 'Cobrar adelanto': monto 100.00, medio Efectivo. Resultado esperado: estado 'En cola', adelanto S/ 100.00, saldo S/ 136.00 visibles en la ficha.
6. Salir y entrar como operario1 / e2e12345. Ir a Órdenes → cola del taller: la orden aparece. Clic 'Tomar orden' → estado 'En proceso', responsable operario1.
7. Clic 'Terminar trabajo' → estado 'Terminada'.
8. Salir y entrar como cajero1. Ir a bandeja 'Cobro de saldo': la orden terminada aparece. Clic 'Cobrar saldo y entregar', medio Efectivo.
9. Resultado esperado: estado 'Entregada', comprobante final emitido (FACTURA por RUC) por el TOTAL S/ 236.00; en Comprobantes verificar que sus medios de pago registran SOLO el saldo (S/ 136.00) y, si la política Vía A está activa, que referencia el comprobante del anticipo.
10. Verificar terminal: en la orden entregada NO debe aparecer ningún botón de acción (ni tomar, ni terminar, ni anular, ni cobrar de nuevo).

### E2 · Negativos de rol y de estado (gates que deben rebotar)

1. ✖ Negativo: como vendedor1, abrir una orden en Borrador: NO debe existir el botón 'Cobrar adelanto'; si se fuerza por API devuelve 403.
2. ✖ Negativo: como cajero1, en 'Cobrar adelanto' escribir el monto IGUAL al total de la orden → debe rebotar con el mensaje de pago PARCIAL ('no puede cubrir o superar el total'). Probar también monto 0 → 'El adelanto debe ser mayor a 0'.
3. ✖ Negativo: cobrar un adelanto válido (parcial) y volver a intentar 'Cobrar adelanto' sobre la misma orden → ya no existe el botón; por API rebota 'solo se registra sobre una orden en borrador'.
4. ✖ Negativo: como cajero1, intentar 'Cobrar saldo' de una orden que NO esté Terminada (en cola o en proceso) → no aparece en la bandeja 'Cobro de saldo' ni tiene el botón; por API rebota.
5. ✖ Negativo: como cajero1, sobre una orden En cola: NO debe aparecer 'Tomar orden' (rol taller). Como operario1: NO debe aparecer 'Cobrar adelanto' ni 'Cobrar saldo'.
6. Como operario1, verificar que una orden EN PROCESO no es visible para cajero1 (su bandeja excluye 'en proceso' por diseño).
7. ✖ Negativo: tras entregar una orden, como cajero1 intentar cobrar el saldo otra vez por API → rebota; en la SPA el botón desaparece.

### E3 · FIFO de la cola del taller con dos órdenes propias

1. Como vendedor1, crear DOS órdenes: 'QA-FIFO-A' (primera) y 'QA-FIFO-B' (segunda), ambas con una línea gravada.
2. Como cajero1, cobrar el adelanto de QA-FIFO-B PRIMERO (esperar unos segundos) y el de QA-FIFO-A DESPUÉS.
3. Como operario1, abrir la cola del taller.
4. Verificar: entre las dos órdenes propias, QA-FIFO-B (adelantada primero) aparece ARRIBA de QA-FIFO-A, aunque QA-FIFO-A se creó antes — el turno lo da el momento del adelanto, no la creación. No comparar contra órdenes de otros usuarios.
5. ✖ Negativo: como operario1, tomar QA-FIFO-B. Como operario2 (e2e12345), intentar 'Tomar' la misma → debe rebotar (ya está En proceso).
6. Como operario2, clic 'Terminar trabajo' sobre la orden que tomó operario1 → SÍ procede (diseño: cualquier usuario con rol taller puede terminar; jamás se compara identidad). Verificar que el responsable sigue siendo operario1 y que el historial (chatter) registra que terminó operario2.

### E4 · Anulación: quién, cuándo y con qué evidencia

1. Como vendedor1, crear una orden 'QA-ANULAR' y dejarla en Borrador (sin adelanto).
2. ✖ Negativo: como vendedor1, verificar que NO tiene botón 'Anular'.
3. ✖ Negativo: como cajero1, clic 'Anular' SIN escribir motivo → rebota: 'Escribe el motivo: queda en el historial del documento'.
4. Repetir con motivo 'cliente desistió antes del adelanto' → estado Anulada; el motivo queda en el chatter.
5. ✖ Negativo: sobre la orden anulada: ningún botón de acción disponible (terminal); un intento de adelanto por API rebota.
6. Crear otra orden y cobrarle adelanto (queda En cola). Como cajero1 verificar que NO aparece 'Anular' (encolada exige supervisor). Como operario1 tampoco. Como supervisor1 / e2e12345 SÍ aparece 'Anular' y exige motivo.
7. OJO Vía A: si la política 'adelanto facturado' está activa, el supervisor primero debe emitir la nota de crédito del comprobante del anticipo; hasta entonces anular rebota con ese mensaje (verificar el mensaje y que el número citado sea el fiscal, p.ej. F001-00000013 — fix 5, §10).
8. Con la política apagada (Vía B, default): supervisor1 anula la encolada con motivo → Anulada; verificar en el chatter quién y por qué (el reembolso del adelanto es retiro de caja manual).

### E5 · Regresión del bug de líneas mixtas con Vía A activa (solo si la política 'adelanto facturado' está ON)

Este escenario reproducía un bug alto que **ya tiene fix** (fix 2, §10): ahora el rebote debe llegar en el paso 3, ANTES de cobrar.

1. Precondición: dueño/supervisor activa 'Anticipo facturado' en Políticas (recordar apagarla al terminar).
2. Como vendedor1, crear una orden con DOS líneas: una gravada (S/ 118) y una exonerada/inafecta (S/ 20).
3. Como cajero1, cobrar adelanto parcial S/ 50. Resultado esperado TRAS EL FIX: rebota AQUÍ, sin cobrar ni emitir nada (antes procedía, emitía el anticipo y la orden quedaba atascada). Si rebota, los pasos 4–6 ya no aplican: marca el escenario como ✔.
4. (Solo si el paso 3 NO rebotó — regresión) Como operario1, tomar y terminar la orden.
5. (Solo si hay regresión) Como cajero1, 'Cobrar saldo y entregar' → rebota SIEMPRE: 'El anticipo solo se soporta sobre una operación gravada homogénea…'.
6. (Solo si hay regresión) Verificar el atasco: la orden no se puede entregar (las líneas ya no se editan tras el borrador) y anular exige antes la nota de crédito del anticipo — repórtalo como regresión del fix 2.
7. Verificar además que el número del anticipo que muestran la ficha y el mensaje de anulación coincide con el número fiscal impreso del comprobante (formato F001-00000013 — fix 5; antes mostraba el folio interno 'F 00000002').

**Ojo con esto**

- El bug alto de líneas mixtas + Vía A y el número interno del anticipo ya tienen fix (fixes 2 y 5, §10). Si reaparecen, es regresión.
- Que operario2 pueda TERMINAR la orden que tomó operario1 es diseño documentado (escala libre por grupo), no un bug.

---

## 4 · Reserva (apartado / layaway)

Estás probando el apartado de producto terminado: el cliente paga de a pocos (abonos), la mercadería jamás entra a la cola del taller, y el comprobante se emite recién al cobrar el saldo, por el TOTAL. Importa porque los abonos son recibos internos SIEMPRE (aunque Vía A esté encendida) y el último pago tiene que ser el saldo.

### E1 · Flujo feliz de reserva: crear, N abonos por medios distintos, cobrar saldo y entregar

1. Entrar a http://localhost:5174 como vendedor1 / e2e12345, ir a la sección Órdenes y pulsar 'Nueva orden'.
2. Elegir tipo 'Reserva', cliente nuevo con DNI (8 dígitos, ej. 45678123) y razón social 'QA-RES01 CLIENTE LAYAWAY'; agregar ítem 'ropero melamina', cantidad 1, precio 500. Guardar. Esperado: orden OT-xxxxx en estado 'Borrador', total S/ 500, saldo S/ 500.
3. Salir y entrar como cajero1 / e2e12345. En Órdenes, abrir la bandeja de cobro de adelantos (cola de borradores): la reserva recién creada debe estar listada.
4. Abrir la reserva y pulsar 'Registrar abono': monto 100, medio Efectivo. Esperado: estado pasa a 'Reservada', adelanto S/ 100, saldo S/ 400, y la orden aparece ahora en la bandeja 'Reservas'.
5. Registrar segundo abono: 150 por Yape. Esperado: adelanto S/ 250, saldo S/ 250, sigue 'Reservada'.
6. Registrar tercer abono: 50 por Transferencia. Esperado: adelanto S/ 300, saldo S/ 200, y el historial de abonos muestra las 3 filas con fecha, monto y medio (Efectivo 100, Yape 150, Transferencia 50).
7. Verificar en Caja (cajero1) que existen 3 movimientos tipo 'adelanto' con motivo 'Abono OT-xxxxx', uno por cada medio.
8. Pulsar 'Cobrar saldo y entregar', medio Tarjeta. Esperado: se emite una BOLETA por el TOTAL S/ 500 (no por 200), el toast indica saldo cobrado S/ 200, y la orden queda 'Entregada'.
9. En Comprobantes, abrir la boleta emitida: total S/ 500 (gravada 423.73 + IGV 76.27), estado 'enviado', cliente QA-RES01. El comprobante NO referencia anticipos (el adelanto fue a cuenta, recibo interno).
10. ✖ Negativo: sobre la orden entregada ya no debe aparecer ningún botón de abono ni de cobro; si se fuerza por API devuelve 'Solo se abona sobre una reserva en borrador o reservada (está «Entregada»)'.

### E2 · Rebotes del abono: el último pago es el saldo, nunca un abono

1. Como cajero1, abrir una reserva 'Reservada' con total 500 y adelanto 300 (la del E1 antes de entregar, o crear otra igual).
2. ✖ Negativo: intentar abonar exactamente el saldo (200). Esperado: rebota con 'El abono (S/ 200.00) completaría o superaría el total (S/ 500.00): el último pago es el SALDO al recoger. Usa «Cobrar saldo y entregar»...'. El adelanto NO cambia.
3. ✖ Negativo: intentar abonar más que el saldo (500). Esperado: mismo rebote.
4. ✖ Negativo: intentar abonar 0 y luego un negativo (-50). Esperado: 'El abono debe ser mayor a 0.' en ambos.
5. Verificar que tras los rebotes el historial sigue mostrando solo los 3 abonos originales y el saldo sigue en S/ 200.
6. ✖ Negativo: como vendedor1, abrir la misma reserva: NO debe aparecer el botón 'Registrar abono' ni 'Cobrar saldo y entregar' (solo caja los ve); por API el intento devuelve 403.
7. ✖ Negativo: como cajero1, en una orden de TALLER en borrador, verificar que el botón ofrecido es 'Cobrar adelanto' (no 'Registrar abono'); si se fuerza un abono por API rebota con 'Los abonos son de las reservas; una orden de taller usa el adelanto único.'

### E3 · La reserva jamás pisa el taller

1. Con una reserva en estado 'Reservada' (con abonos) creada en E1/E2, salir y entrar como operario1 / e2e12345.
2. Abrir la cola del taller (Órdenes → cola). Esperado: la reserva NO aparece, aunque tenga abonos; solo se listan órdenes tipo taller en 'En cola'/'En proceso'.
3. ✖ Negativo: intentar tomarla por API: POST /ne/api/ordenes/<id>/tomar con el token de operario1. Esperado: 403 (la regla de registro del taller ni siquiera le deja leer una 'Reservada') — nunca un 200.
4. Entrar como cajero1 y confirmar que la reserva sigue 'Reservada' con su historial intacto (el intento del operario no la tocó ni le asignó responsable).
5. Confirmar que la bandeja 'Reservas' (cajero) la lista y que la cola del taller (operario) sigue sin mostrarla tras refrescar.

### E4 · Anulación: borrador la anula caja; con abonos, SOLO supervisor y con motivo

1. Como vendedor1 crear reserva 'QA-RES02 CLIENTE ANULACION' (DNI 41111111, ítem 300). Como cajero1 abonar 50 Efectivo → 'Reservada'.
2. ✖ Negativo: como cajero1 intentar 'Anular' la reserva con abonos. Esperado: el botón no debe aparecer; por API 403 sin permiso.
3. ✖ Negativo: como vendedor1 intentar anularla. Esperado: mismo rechazo.
4. ✖ Negativo: entrar como supervisor1 / e2e12345, abrir la reserva y pulsar 'Anular (reserva con abonos)' SIN escribir motivo. Esperado: rebota con 'Escribe el motivo: queda en el historial del documento.'
5. Repetir con motivo 'cliente desistió, reembolso manual'. Esperado: estado 'Anulada'; el motivo queda en el historial (chatter). Nota: el reembolso de los S/ 50 es retiro manual de caja (v1), no automático.
6. Crear otra reserva 'QA-RES03' SIN abonos (borrador). Como cajero1 pulsar 'Anular' con motivo. Esperado: pasa a 'Anulada' (borrador sí lo anula caja).
7. ✖ Negativo: intentar anular una reserva ya 'Entregada' como supervisor1. Esperado: 'No se puede pasar de «Entregada» a «Anulada».'

### E5 · Los abonos nunca facturan individualmente (incluso con Vía A encendida) y el re-test del endpoint de adelanto

1. Precondición: verificar en Negocio → Políticas (como supervisor1/duenio1) si 'Anticipo facturado' (Vía A) está encendido; dejarlo como estaba al terminar.
2. Como vendedor1 crear reserva 'QA-RES06' (2 sillas x 90 = 180). Como cajero1 abonar 30 por Yape.
3. Esperado: la reserva pasa a 'Reservada' y NO se emite ningún comprobante: en la ficha no aparece 'Comprobante del anticipo' y en Comprobantes no hay ningún doc nuevo del cliente QA-RES06. Esto con Vía A ON — el abono de reserva es SIEMPRE recibo interno.
4. Contraste: en una orden de TALLER con Vía A ON, 'Cobrar adelanto' SÍ emite comprobante de anticipo (así se ve la diferencia de diseño).
5. Re-test del fix 1 (§10) — solo API, la SPA no expone el botón: con el token de cajero1 ejecutar `curl -X POST http://localhost:5174/ne/api/ordenes/<id-reserva-borrador>/adelanto -d '{"monto":40,"medio":"Efectivo"}'`. Resultado esperado TRAS EL FIX: el endpoint REBOTA con un mensaje tipo 'las reservas se abonan, no se adelantan', sin cobrar nada. (Antes: 200 OK, la reserva saltaba a 'encolada' sin salida, cobraba S/ 40 y con Vía A ON facturaba el abono.)
6. Si el paso 5 rebotó, verificar que la reserva sigue en Borrador, sin dinero cobrado y visible en su bandeja. Si NO rebotó, es regresión del fix 1: repórtala.
7. Limpieza: no borrar nada (las órdenes con dinero no se borran); dejar las QA-* anuladas/entregadas como evidencia.

**Ojo con esto**

- El bug crítico de la reserva atascada por el endpoint de adelanto ya tiene fix (fix 1, §10): la señal de éxito es que el cajero solo ve "Cobrar abono" y el POST /adelanto rebota.
- Los mensajes 403 genéricos ('puede pertenecer a otra empresa') fueron corregidos por el fix 4: ahora deben decir el motivo real.

---

## 5 · Vía A · Adelanto facturado ante SUNAT

Estás probando el switch por empresa que hace que cada adelanto de taller emita un comprobante fiscal REAL (doc. A), que el arqueo lo cuente UNA sola vez, y que el cobro del saldo lo regularice en la factura final. Importa porque hay plata y documentos fiscales de por medio: un doble conteo o un anticipo huérfano son errores graves.

### E1 · Encender Vía A: solo supervisor puede togglear

1. Entrar a http://localhost:5174 como cajero1 / e2e12345.
2. ✖ Negativo: verificar que en el menú NO aparece la página Políticas de control (o que al navegar directo el toggle no está disponible); si se fuerza el POST el servidor responde 403. Resultado esperado: el cajero no puede cambiar la política.
3. Cerrar sesión y entrar como supervisor1 / e2e12345.
4. Ir a Políticas de control y ubicar la tarjeta «Facturar los adelantos (Vía A)» con su checkbox.
5. Marcar el checkbox. Resultado esperado: queda marcado sin error y persiste al recargar la página.

### E2 · Con Vía A ON el adelanto emite comprobante real y el arqueo no duplica

1. Como vendedor1 / e2e12345, ir a Órdenes y crear una orden de taller: cliente con RUC 20123456789 (usar razón social con prefijo QA-), un ítem «QA- reparación» a S/ 400. Resultado: orden en Borrador, total 400.
2. Cerrar sesión y entrar como cajero1 / e2e12345; ir a Órdenes, pestaña/cola de cobro de adelanto, abrir la orden recién creada.
3. Cobrar adelanto de S/ 150 con medio Yape. Resultado esperado: flash «Adelanto S/ 150.00 cobrado — comprobante F001-000000NN — orden encolada al taller» y la tarjeta muestra el número del comprobante del anticipo en formato fiscal F001-000000NN (fix 5; antes mostraba el nombre interno tipo «F 00000001»).
4. Ir a Comprobantes: debe existir una factura F001 nueva por S/ 150 al cliente del RUC, estado Enviado, con línea «Anticipo a cuenta — OT-000NN».
5. Ir a Caja: en los movimientos de la sesión debe aparecer un movimiento tipo Adelanto de 150 con medio Yape, cliente y orden.
6. Verificación de no-duplicado: la sonda original (retiro absurdo de 999999 para leer «solo tiene S/ X en efectivo») YA NO FUNCIONA — el fix 11 quitó ese monto del mensaje a propósito. La verificación de que el adelanto Vía A cuenta UNA sola vez queda para el cierre de caja: en el arqueo final el medio del adelanto debe sumar el monto exacto, no el doble (ver §7).
7. ✖ Negativo: intentar cobrar un adelanto mayor o igual al total de la orden. Resultado esperado: rechazo con mensaje de que el adelanto es un pago PARCIAL.

### E3 · El cobro del saldo emite la factura final regularizando el anticipo

1. Como operario1 / e2e12345, ir a Órdenes (cola del taller), Tomar la orden del E2 y luego Terminar el trabajo. Resultado: estado Terminada.
2. Como cajero1, ir a la cola de cobro de saldo, abrir la orden y usar «Cobrar saldo y entregar» (medio Yape). Resultado esperado: cobra S/ 250 (400 − 150) y la orden pasa a Entregada.
3. En Comprobantes abrir el detalle de la factura final nueva (F001-…): debe mostrar la sección de anticipos con doc F001-… (el del anticipo), monto 150.00, tipo 02, y el total a cobrar 250.00 (no 400).
4. En Emitir/venta nueva al mismo cliente, verificar que el anticipo de 150 YA NO aparece entre los anticipos pendientes de regularizar (saldo quedó en 0).
5. ✖ Negativo: intentar cobrar el saldo otra vez sobre la misma orden. Resultado esperado: rechazo «ya se cobró en el comprobante …».

### E4 · Anular una orden con anticipo facturado exige la nota de crédito primero

1. Con Vía A ON, como vendedor1 crear otra orden QA de taller (S/ 200) y como cajero1 cobrar un adelanto de S/ 50 (se emite su comprobante de anticipo).
2. Como supervisor1, abrir la orden (estado En cola) y elegir Anular con motivo «QA prueba».
3. Resultado esperado: la anulación se BLOQUEA con «El adelanto ya se facturó en el comprobante …: emite primero su nota de crédito y luego anula la orden». La orden sigue encolada. El número citado debe ser el fiscal (F001-…), no el folio interno (fix 5).
4. Nota para el probador: ningún usuario del entorno e2e tiene el permiso de anulación de comprobantes (puedeAnular=false en todos), así que la NC del anticipo no se puede emitir aquí; el paso de anular-tras-NC solo es verificable en un entorno con ese rol.
5. ✖ Negativo: como cajero1 intentar anular la misma orden. Resultado esperado TRAS EL FIX 6: rebota por FALTA DE ROL (anular una encolada es del supervisor), no con el mensaje de la nota de crédito. Si le sale el mensaje fiscal de la NC, es regresión del fix 6.

### E5 · Apagar el switch devuelve la Vía B (recibo interno) y no huerfanea anticipos en vuelo

1. Como supervisor1, ir a Políticas de control y DESMARCAR «Facturar los adelantos (Vía A)». Resultado: checkbox apagado y persiste al recargar.
2. Como vendedor1 crear una orden QA de taller (S/ 300); como cajero1 cobrar adelanto de S/ 60 en Efectivo.
3. Resultado esperado: el flash NO menciona comprobante («Adelanto S/ 60.00 cobrado — orden encolada al taller»), la tarjeta no muestra número de anticipo y en Comprobantes NO aparece ninguna factura nueva por 60.
4. En Caja: sí aparece el movimiento Adelanto de 60 (recibo interno) — en Vía B esa plata entra al arqueo por el movimiento.
5. Con la orden del E4 (anticipo facturado y switch ya OFF): operario1 la toma y termina; cajero1 cobra el saldo. Resultado esperado: la factura final igual referencia y descuenta el anticipo emitido (la regularización depende de la orden, no del switch).
6. Verificación final: en Políticas de control confirmar que «Facturar los adelantos (Vía A)» quedó APAGADO (el default del negocio es Vía B).

**Ojo con esto**

- La nota de crédito del anticipo NO es probable en este entorno: nadie del seed tiene el rol anulación. No es un bug del producto.
- La sonda del retiro absurdo para leer el efectivo disponible ya no existe (fix 11): era una fuga del conteo ciego y se cerró a propósito.

---

## 6 · Emisión core (POS y Nuevo comprobante)

Estás probando el corazón fiscal: boletas y facturas con IGV exacto al céntimo, correlativos sin huecos, filtros de Comprobantes y el gate de anulación. Importa porque un céntimo descuadrado o un correlativo quemado son problemas directos con SUNAT.

### E1 · POS Venta rápida: boleta a DNI con IGV exacto (cajero1)

1. Entrar a http://localhost:5174 como cajero1 / e2e12345; abrir 'Venta rápida' (POS).
2. Dejar el tipo en 'Boleta'. En Cliente, teclear un DNI de 8 dígitos (ej. 46997315); si no existe, aceptar la sugerencia/crear con nombre 'QA-CORE CLIENTE BOLETA'.
3. Agregar un concepto libre: descripción 'QA-CORE PROD A', cantidad 2, precio 11.80 (precio de vitrina CON IGV).
4. Verificar el panel de totales: Gravada 20.00, IGV 3.60, Total 23.60 (IGV = 18% exacto de la base).
5. Pulsar 'Cobrar' con medio Efectivo. Resultado esperado: tarjeta de éxito con número B001-XXXXXXXX y estado Aceptado/Enviado; el carrito se limpia.
6. Ir a 'Comprobantes' y ubicar el número emitido: estado 'enviado', total 23.60, cliente correcto.
7. Abrir el detalle y verificar de nuevo gravada/IGV/total y la línea (cantidad 2, precio base 10.00).
8. ✖ Negativo: en una venta nueva, cambiar a 'Factura' con el mismo cliente DNI → el botón Cobrar debe quedar bloqueado pidiendo cliente con RUC. TRAS EL FIX 8, el backend TAMBIÉN rebota si se fuerza por API (exige RUC válido de 11 dígitos); antes solo lo frenaba el front.

### E2 · Emitir (Nuevo comprobante): factura a RUC con caso borde de redondeo (supervisor1)

1. Entrar como supervisor1 / e2e12345; abrir 'Emitir' / 'Nuevo comprobante'.
2. Tipo: Factura. Cliente: RUC 20100070970 (usar el existente 'QA-SPA01 FERRETERIA SAC' o crearlo con prefijo QA-).
3. Línea 1: 'QA-CORE ITEM BORDE', cantidad 3, precio con IGV 2.125 (teclear 2.125). Línea 2: 'QA-CORE ITEM DOS', cantidad 1, precio 9.90.
4. Verificar el panel: Gravada 13.79, IGV 2.48, Total 16.27 — NO 16.28: el total es round2(base)+round2(IGV); cálculo manual: base = 16.275/1.18 = 13.792372..., IGV = 16.275−13.792372 = 2.482627.
5. Emitir. Resultado esperado: F001-XXXXXXXX estado 'enviado', total 16.27.
6. En Comprobantes, abrir el detalle y confirmar que gravada+IGV = total mostrado (sin descuadre de 1 céntimo).
7. Descargar XML y CDR desde el detalle (deben bajar); el PDF puede fallar en este entorno (el mock no genera PDF — mensaje claro, no es bug).
8. Repetir la emisión con otra factura simple y verificar que el correlativo avanza en +1 respecto del último de la serie (sin huecos).

### E3 · Anulación: gate por rol y baja sin borrado

1. ✖ Negativo: como cajero1, abrir el detalle de una boleta propia emitida y buscar la acción 'Anular/Comunicar baja': no debe aparecer, o al intentarlo debe responder 'No tienes permiso para anular comprobantes' (403). Esto es correcto, no es bug.
2. ✖ Negativo: repetir como supervisor1: mismo resultado 403 (el rol supervisor NO incluye anulación).
3. Entrar como duenio1 / e2e12345 → Equipo: crear usuario 'QA-CORE Anulador' (login qa-core-anulador) con el rol 'Puede anular'; anotar la clave temporal que se muestra UNA vez.
4. Iniciar sesión con ese usuario (cambiar la clave si la app lo fuerza).
5. Anular la boleta propia (motivo 'QA-CORE prueba'). En este entorno el mock rechaza la baja ('rechazada por el facturador') — verificar entonces lo que SÍ es exigible: el comprobante NO desaparece de la lista, su estado NO se corrompe (sigue 'enviado' con el mensaje del intento) y se puede reintentar.
6. Con un facturador real, el resultado esperado sería: boleta → RC (Resumen Diario), factura → RA (Comunicación de Baja), estado final 'anulado', el documento sigue visible en Comprobantes con su CDR de baja descargable, y NUNCA se borra.
7. Verificar en el detalle que los montos e historial del documento anulado/intentado quedan intactos.
8. Al terminar: en Equipo, desactivar el usuario QA-CORE Anulador (dejar la base como estaba).

### E4 · Comprobantes: paginación y filtros

1. Como supervisor1, abrir 'Comprobantes'.
2. Verificar paginación: con tamaño de página pequeño (ej. 10), navegar a la página 2 y confirmar que las filas cambian y el total declarado coincide con la suma de páginas.
3. Filtrar por serie B001: deben verse solo boletas y la secuencia de correlativos debe ser continua (1,2,3,... sin huecos).
4. Filtrar tipo=Factura + estado=Enviado: solo facturas 'enviado'.
5. Buscar por texto 'QA-CORE': deben aparecer solo los documentos de clientes QA-CORE.
6. Buscar por un correlativo exacto (ej. 00000009): debe aparecer esa única fila.
7. Filtrar por rango de monto 20–25: debe incluir la boleta de 23.60 y excluir el resto.
8. Filtrar 'desde' una fecha futura: lista vacía (sin error).
9. Limpiar filtros y confirmar que la lista vuelve completa.

### E5 · POS sin cliente (público general) y reutilización del partner

1. Como cajero1 en 'Venta rápida', NO seleccionar cliente.
2. Agregar un ítem 'QA-CORE SIN DOC' de 3.00 y cobrar: debe emitirse boleta a 'CLIENTE VARIOS' estado 'enviado', total 3.00 exacto.
3. Repetir la misma venta una segunda vez.
4. Ir a 'Clientes' y buscar 'CLIENTE VARIOS'. Resultado esperado TRAS EL FIX 10: UNA sola ficha reutilizada por ambas ventas. (Antes: una fila nueva idéntica por cada venta.) Si aparecen duplicados nuevos, es regresión.
5. Verificar igualmente que ambas boletas salieron con correlativos consecutivos y montos exactos.

**Ojo con esto**

- El mock no genera PDF ni procesa bajas (RC/RA): 'PDF no disponible' y 'baja rechazada por el facturador' son limitaciones del entorno, no bugs.
- Factura a DNI, preflight con HTML 404 y duplicados de 'CLIENTE VARIOS' ya tienen fix (fixes 8, 9 y 10, §10). Si reaparecen, es regresión.

---

## 7 · Caja (conteo ciego, arqueo, gastos, cierre)

Estás probando que la caja sea de verdad ciega (el "esperado" no se revela hasta el cierre), que los retiros exijan respaldo, y que el cierre congele el arqueo. Importa porque el conteo ciego es la defensa contra el faltante: si el cajero puede ver cuánto "debería haber", el control no vale nada.

### E1 · Recorrido completo de la sesión de caja (11 pasos)

1. Entrar como cajero1 / e2e12345 → menú Caja. Ver la sesión abierta con el aviso de que el monto esperado se revela recién al cerrar.
2. Imprimir el corte con la sesión abierta: Esperado/Contado/Diferencia deben salir en "—" (conteo ciego real, ni el papel lo revela).
3. ✖ Negativo: intentar un retiro imposible (99999): rebota SIN revelar cuánto hay disponible (fix 11 — antes el error decía "solo tiene S/ X en efectivo").
4. Hacer un retiro chico con motivo: aparece como movimiento. ✖ Negativo: un retiro mayor a S/ 300 sin N° de voucher/fecha rebota (umbral de respaldo).
5. Registrar un gasto desde la pantalla Gastos: NO aparece en los movimientos de caja (limitación conocida, §11 — no lo reportes como fallo nuevo).
6. En el POS (Venta rápida): cobrar S/ 5.90 → el contador de ventas de la sesión de caja sube.
7. Cerrar la caja declarando +1.00 de diferencia: cierra SIN pedir supervisor (bug abierto, §11) y recién ahí se revela el esperado por medio.
8. Con la caja cerrada: ✖ Negativo: un abono/adelanto de orden rebota con "No hay una caja abierta". El POS en cambio SÍ emite (bug abierto, §11: esa venta queda fuera de todo arqueo).
9. Reabrir la sesión con saldo 0. ✖ Negativo: intentar abrir OTRA sesión desde supervisor1 → rebota (una sola caja abierta por vez).
10. Seguridad: como vendedor1 el menú Caja no aparece Y, si se fuerza el backend, ahora responde "Tu rol no maneja el dinero de la caja" (fix 3 — antes un vendedor podía llegar a CERRAR la caja).
11. Historial: ver las sesiones cerradas y su arqueo congelado (inmutable, no editable).

**Ojo con esto**

- El cierre con descuadre sin exigir supervisor, los gastos que no impactan el arqueo y el POS que emite con caja cerrada son bugs **abiertos** (§11) — verifícalos como están descritos, no los reportes como nuevos.
- Las horas del arqueo pueden salir en UTC (adelantadas ~5 h respecto de Perú) y el arqueo puede mostrar medios duplicados por mayúsculas ('Efectivo'/'efectivo') — también abiertos (§11).

---

## 8 · Roles, menú y muro del backend

Estás probando dos capas: el MENÚ (cada rol ve solo sus ítems — es UX) y el MURO (el backend rebota con 403 cualquier acción de un rol que no corresponde — es la seguridad real). Importa porque la regla de la casa es que toda la lógica vive en el backend: aunque alguien fuerce una URL o un POST, el servidor debe decir que no.

La matriz completa de visibilidad vive en [menu-por-rol.md](menu-por-rol.md).

### E1 · Menú por rol: roles operativos simples (matriz H-3b)

1. Abrir http://localhost:5174 y entrar como vendedor1 / e2e12345.
2. Verificar que el NAV muestra EXACTAMENTE: Inicio, Comprobantes, Cotizaciones, Órdenes de taller, Clientes y Productos. NO deben aparecer: Venta rápida, Caja, Nuevo comprobante (ni el CTA del topbar), Guías, Emisión masiva, ningún reporte, Compras, Gastos, Series, Frecuentes, Datos del negocio, Equipo, Políticas de control, Componentes UI.
3. Cerrar sesión y entrar como cajero1 / e2e12345.
4. Verificar NAV: Inicio, Venta rápida, Caja, Comprobantes, Cotizaciones, Órdenes, Clientes, Productos y Gastos — y nada más (sin Nuevo comprobante, sin reportes, sin Series).
5. Cerrar sesión y entrar como operario1 / e2e12345.
6. Verificar NAV mínimo: solo Inicio y Órdenes de taller. Ningún otro ítem.
7. Cerrar sesión y entrar como despachador1 / e2e12345.
8. Verificar NAV: Inicio, Comprobantes (consulta), Cotizaciones (su tab Cola de despacho), Órdenes, Guías de remisión, Productos (consulta), Compras y Frecuentes. Sin Clientes, sin Gastos, sin Caja.
9. ✖ Negativo (muro vs menú): como operario1, pegar a mano la URL de una página oculta (p.ej. la de Gastos o Clientes) — la página puede cargar (el menú es UX) pero cualquier acción de guardado debe rebotar con mensaje de permiso.

### E2 · Menú por rol: supervisor, contador, dueño y modal

1. Entrar como supervisor1 / e2e12345.
2. Verificar que ve TODO el menú operativo (Caja, Nuevo comprobante + CTA del topbar, Comprobantes, Cotizaciones, Órdenes, Guías, Emisión masiva, Análisis, Libros, Vinculadas, Descargas, Clientes, Productos, Compras, Gastos, Series, Frecuentes, Datos del negocio, Políticas de control) EXCEPTO: Venta rápida, Equipo y Componentes UI.
3. Entrar como contador1 / e2e12345.
4. Verificar NAV de solo consulta: Inicio, Comprobantes, Análisis de ventas, Libros electrónicos, Partes vinculadas y Centro de descargas. Nada de emisión, clientes, productos ni caja.
5. En Comprobantes y Análisis como contador1: las páginas cargan datos reales sin errores.
6. Entrar como duenio1 / e2e12345.
7. Verificar que ve lo mismo que supervisor1 MÁS el ítem Equipo, y sigue SIN Venta rápida.
8. En Equipo como duenio1: la lista del personal del RUC carga (vendedor1, cajero1, operarios, etc.).
9. Entrar como modal / modal1234 y verificar la unión: todo lo del supervisor MÁS Venta rápida (por su rol caja), sin Equipo (no es dueño).
10. ✖ Negativo: como supervisor1 pegar la URL de Equipo — el backend debe rebotar la carga de la lista con error de permiso (por API devuelve 403 'solo el dueño').

### E3 · Muro backend en el flujo CN-02 completo (cada rol solo su tramo)

1. Como vendedor1: crear una orden de taller para el cliente 'QA-MANUAL CLIENTE' (DNI 45673210) con 1 ítem 'QA-MANUAL reparación' a S/150. La orden nace en Borrador.
2. ✖ Negativo: como vendedor1 sobre esa orden: verificar que NO aparecen botones de Cobrar adelanto / Tomar / Terminar / Cobrar saldo.
3. Como operario1: la orden en Borrador NO debe aparecer en su cola (su bandeja son encolada/en_proceso/terminada).
4. Como cajero1: abrir la orden y usar 'Cobrar adelanto' con S/75 en efectivo → la orden pasa a 'En cola'. (Si falla con 'caja cerrada', abre la caja primero: los cobros la necesitan.)
5. ✖ Negativo: como vendedor1: intentar Tomar la orden (forzando el POST si no hay botón) → debe rebotar con 'No tienes permiso' (por API: 403).
6. Como operario1: 'Tomar orden' → pasa a 'En proceso' con responsable operario1.
7. Como operario2: 'Terminar trabajo' debe FUNCIONAR aunque la tomó operario1 (escala libre: se valida grupo, no identidad — comportamiento esperado documentado).
8. ✖ Negativo: como operario1: intentar 'Cobrar saldo y entregar' → NO debe existir el botón y el POST rebota (403 'No tienes permiso para cobrar').
9. Como cajero1: 'Cobrar saldo y entregar' (efectivo) → emite la boleta final por S/150 con medios=75 y la orden queda 'Entregada'; el comprobante debe quedar 'enviado' (mock facturador).
10. Verificar como operario1 que la orden entregada YA NO aparece en su cola (por diseño su regla no cubre 'entregada').
11. ✖ Negativo final: como vendedor1 intentar Anular la orden entregada → rebota (403).

### E4 · Contador es 100% solo lectura (cada POST suyo rebota)

1. Entrar como contador1 / e2e12345.
2. Confirmar que en ninguna de sus 5 páginas (Comprobantes, Análisis, Libros, Vinculadas, Descargas) existe un botón de crear/emitir/anular/editar.
3. En Centro de descargas: exportar ventas del periodo actual a XLSX → descarga OK (lectura permitida).
4. ✖ Negativo: con las devtools (o curl con su Bearer), forzar POST /ne/api/emitir con una boleta válida → debe responder 403.
5. ✖ Negativo: forzar POST /ne/api/gastos → 403; POST /ne/api/clientes → 403; POST /ne/api/productos → 403; POST /ne/api/cotizaciones → 403; POST /ne/api/ordenes → 403; POST /ne/api/anular → 403.
6. ✖ Negativo: forzar GET /ne/api/equipo → 403 (ni siquiera lectura del equipo).
7. Verificar que tras todos los intentos NO se creó ningún registro nuevo (revisar Comprobantes/Gastos con supervisor1).
8. Resultado esperado global: cero mutaciones, cero errores 500 — solo 403 con mensaje claro.

### E5 · Equipo (solo dueño) y Políticas de control (dueño/supervisor)

1. Entrar como duenio1 / e2e12345 y abrir Equipo: la lista carga; verificar que puede editar roles de un usuario QA propio.
2. ✖ Negativo: como supervisor1: el ítem Equipo NO aparece; forzar GET /ne/api/equipo con su Bearer → 403 'Solo el dueño del negocio puede gestionar usuarios'.
3. Como supervisor1: el ítem Políticas de control SÍ aparece — el toggle real lo prueba el capítulo 5 (Vía A).
4. ✖ Negativo: como cajero1: forzar POST /ne/api/politicas con una clave inexistente {"key":"qa-invalida","modo":"off"} → 403 (el gate de grupo se evalúa ANTES de validar la clave, así el intento no cambia nada).
5. ✖ Negativo: como vendedor1 y operario1: repetir el paso 4 → 403 en ambos.
6. ✖ Negativo: como contador1: POST /ne/api/equipo (alta de usuario QA) → 403; verificar en Equipo (duenio1) que NO se creó nadie.
7. ✖ Negativo de anulación: como vendedor1 intentar anular un comprobante desde la SPA → sin botón; el POST /ne/api/anular forzado → 403 'No tienes permiso para anular comprobantes'.

### E6 · Rutas directas con el rol equivocado (¿degrada o miente?) — del caso SPA bandejas

1. Ventana de INCÓGNITO como operario1. Confirma primero que el sidebar NO tiene Caja.
2. Escribe a mano en la barra de direcciones: http://localhost:5174/caja. DEFECTO abierto (§11): la página puede mostrar 'No hay una caja abierta' + formulario 'Abrir caja' aunque la caja SÍ esté abierta. NO pulses 'Abrir caja'. Con el fix 3, cualquier acción de dinero que fuerces rebota con "Tu rol no maneja el dinero de la caja".
3. Con operario1, escribe http://localhost:5174/cotizaciones. DEFECTO abierto (§11): la página carga y lista cotizaciones del negocio (incluidos montos) aunque el menú se la oculta.
4. Con operario1, escribe http://localhost:5174/comprobantes: la lista de comprobantes emitidos se carga completa (mismo defecto abierto).
5. Con operario1, vuelve a Inicio (/). DEFECTO abierto (§11): los KPIs muestran 'Emitidos hoy', 'Emitidos este mes' con importes, 'Ventas del mes', gastos y utilidad neta del negocio.
6. Con operario1, escribe http://localhost:5174/equipo. Esperado: cartel 'Solo el dueño del negocio gestiona el equipo.' (puede aparecer además un toast rojo de 403 — defecto menor abierto, §11).
7. Con operario1, escribe http://localhost:5174/politicas. Esperado: cartel 'Solo el dueño o un supervisor configura las políticas.' sin errores (esta pantalla sí degrada limpio).
8. Ventana de INCÓGNITO como vendedor1: escribe http://localhost:5174/caja. La página aún puede cargar datos (guard de ruta abierto, §11), pero TRAS EL FIX 3 los botones 'Registrar movimiento' y 'Cerrar caja' deben rebotar en el backend con "Tu rol no maneja el dinero de la caja" (antes un vendedor podía cerrar la caja de verdad).
9. Repite el paso 8 como despachador1 y como contador1: mismo resultado.
10. Como contador1, escribe http://localhost:5174/ordenes: la cola del taller se carga aunque su menú no la tenga. TRAS EL FIX 13, el botón 'Nueva orden' ya NO debe pintarse a quien el backend rechaza; si aún aparece y lo usas, responde 403.
11. Cierra sesión desde el pie del sidebar en una de las ventanas y comprueba que vuelve a /login y que al reabrir una ruta directa te manda al login (no hay fuga de sesión entre incógnitos).

### E7 · Usuario modal (todos los sombreros) — la escala libre en una sola pantalla — del caso SPA bandejas

1. Ventana de INCÓGNITO limpia: entra como modal / modal1234. Esperado: el sidebar completo salvo Equipo y Componentes UI.
2. Abre Cotizaciones. Esperado: TRES pestañas: 'Todas', 'Cola de cobro' y 'Cola de despacho'.
3. Crea una cotización 'QA-E5' (cliente QA-SPA01 FERRETERIA SAC, un ítem de 118) y llévala con el funnel a Enviada → Aceptada, todo con el mismo usuario y sin cambiar de pantalla.
4. En el drawer de esa cotización aceptada, comprueba que aparece un único botón grande: 'Cobrar y emitir' (o 'Cobrar y entregar' si la venta lleva despacho). Púlsalo.
5. Esperado: toast '✓ Cobrado · F001-000xx' (o '✓ Cobrado y entregado'), la cotización pasa a CONVERTIDA y muestra el número de comprobante en el pie del drawer.
6. Abre Órdenes de taller. Esperado: las CUATRO pestañas. Crea una orden 'QA-E5 taller' con total 236 y adelanto 100 en el mismo formulario (el modal sí ve los campos de adelanto).
7. En 'Cola de taller' toma la orden y termínala sin cambiar de usuario; luego en 'Cobro de saldo' cóbrala y entrégala. Comprueba que el recorrido completo se hizo con un solo usuario y quedó el comprobante emitido.
8. Ve a Caja y comprueba que los adelantos que cobraste aparecen como movimientos 'Adelanto a cuenta' con su medio y el número de orden. NO cierres la caja si vas a seguir probando otros capítulos.

**Ojo con esto**

- Las rutas directas sin guard de lectura (E6) son un defecto **abierto** (§11): la página carga datos, pero ninguna ACCIÓN pasa el muro del backend.
- Un body con JSON malformado por API devuelve 500 HTML en vez de 400 JSON — abierto (§11), solo alcanzable con curl/Postman, nunca desde la SPA.
- Que /emitir deje pasar por API a cualquier rol operativo es decisión de diseño documentada (todos implican emisor; el menú solo se lo muestra al supervisor) — no lo reportes como bug.

---

## 9 · Equipo (el dueño gestiona su gente)

Estás probando que SOLO el dueño da de alta usuarios, cambia roles, desactiva y resetea claves — y que esas acciones matan las sesiones vivas al instante, no "cuando expiren". Importa porque es la llave maestra del negocio: si un no-dueño la alcanza, todo lo demás se cae.

### E1 · Alta de usuario y primer ingreso con cambio forzado (duenio1)

1. Entrar a http://localhost:5174 y loguear como duenio1 / e2e12345.
2. Abrir 'Equipo' en el menú (solo visible para el dueño).
3. Clic en nuevo usuario: nombre 'QA-eq-M1 Prueba', login 'qa-eq-m1', marcar roles Ventas y Caja, guardar.
4. Esperado: modal 'Contraseña temporal' muestra la clave UNA sola vez con botón Copiar; la fila aparece activa con roles ventas y caja.
5. ✖ Negativo: crear otro usuario con el MISMO login 'qa-eq-m1' → error genérico 'No se pudo crear ese acceso. Prueba con otro usuario (login).' (no revela si el login existe).
6. ✖ Negativo: dejar el nombre vacío → 'Indica el nombre y el usuario (login).'
7. Cerrar sesión y loguear como qa-eq-m1 con la clave temporal.
8. Esperado: la SPA fuerza la pantalla de cambio de contraseña.
9. ✖ Negativo: escribir una 'contraseña actual' errada → error 'La contraseña actual no es correcta.' y NO cambia nada.
10. Escribir la temporal correcta y una nueva (mínimo 8 caracteres, distinta) → entra al panel; el menú corresponde a ventas+caja (ve Venta rápida, Caja, Cotizaciones; NO ve Masivo, Series ni Equipo).
11. Cerrar sesión y volver a entrar con la clave nueva → ya no pide cambio; la temporal ya no sirve (probar: 'Credenciales inválidas').

### E2 · Cambiar roles reemplaza la lista y rechaza escaladas (duenio1)

1. Loguear como duenio1 y abrir Equipo → editar qa-eq-m1.
2. Cambiar roles a Despacho + Contador y guardar → la fila muestra SOLO despacho y contador (ventas/caja desaparecieron: es reemplazo, no acumulación).
3. Cambiar a solo Taller → queda solo taller.
4. Verificar que el formulario NO ofrece opción 'Dueño' ni 'Admin'.
5. ✖ Negativo (API): con DevTools (pestaña Network, copiar el Bearer): POST /ne/api/equipo/<id>/roles body {"roles":["duenio"]} → 400 'Rol no válido: duenio'.
6. ✖ Negativo (API): repetir con {"roles":["admin"]} → 400 'Rol no válido: admin'; con {"roles":["base.group_system"]} → 400.
7. Quitar todos los roles y guardar → el usuario queda sin roles; su login sigue funcionando pero sin capacidades operativas.
8. Sobre un usuario que es dueño: quitar todos los roles → sigue siendo dueño (aviso 'su rol de dueño no se cambia desde aquí'); la fila puede seguir mostrando 'supervisor' porque el rol dueño lo implica.

### E3 · Desactivar mata la sesión viva y los guards de autoprotección (dos navegadores)

1. Navegador A (o ventana incógnito): loguear qa-eq-m1 con su clave definitiva y dejar la sesión abierta en cualquier página.
2. Navegador B: loguear duenio1 → Equipo → desactivar qa-eq-m1.
3. Esperado en B: la fila pasa a inactiva de inmediato.
4. Navegador A: refrescar o hacer cualquier acción → la sesión muere (401 'No autenticado o sesión expirada') y redirige al login: el token vivo fue revocado, no espera a expirar.
5. ✖ Negativo: intentar loguear qa-eq-m1 → 'Credenciales inválidas' (usuario inactivo).
6. ✖ Negativo: en B, duenio1 intenta desactivarse a sí mismo → error 'No puedes desactivarte a ti mismo.' y sigue activo.
7. Reactivar qa-eq-m1 → vuelve a poder entrar con la misma clave que tenía.
8. NO probar desactivar a duenio1: la regla de 'último dueño' (No puedes desactivar al último dueño del negocio) solo dispara cuando el objetivo es el ÚNICO dueño activo, estado que en esta base compartida no se puede alcanzar sin tumbar a duenio1; queda cubierta por el test unitario test_v3_no_ultimo_duenio.

### E4 · Reset de contraseña revoca sesiones (dos navegadores)

1. Navegador A: sesión viva de qa-eq-m1.
2. Navegador B: duenio1 → Equipo → 'Resetear contraseña' de qa-eq-m1.
3. Esperado: modal con la clave temporal nueva, visible una sola vez.
4. Navegador A: cualquier acción → 401 y redirige al login (el token vivo murió).
5. ✖ Negativo: loguear con la clave anterior → 'Credenciales inválidas'.
6. Loguear con la temporal nueva → entra y vuelve a forzar el cambio de contraseña.
7. Completar el cambio para dejar al usuario operativo.

### E5 · Co-dueño con re-autenticación del dueño

1. duenio1 → Equipo → crear 'QA-eq-M2 Coduenio', login 'qa-eq-m2', rol Supervisor; guardar la temporal.
2. ✖ Negativo: acción 'Hacer co-dueño' sobre qa-eq-m2 escribiendo una contraseña de duenio1 ERRADA → rebota con 403. TRAS EL FIX 4, el mensaje debe decir el motivo real (contraseña incorrecta), ya no el engañoso 'puede pertenecer a otra empresa'.
3. Verificar que qa-eq-m2 NO quedó como dueño (la fila no cambió).
4. Repetir con la contraseña correcta e2e12345 → qa-eq-m2 aparece como dueño.
5. Loguear qa-eq-m2 (temporal → cambio forzado) → ahora ve la pantalla Equipo y puede listar el equipo completo.
6. ✖ Negativo: qa-eq-m2 intenta desactivarse a sí mismo → 'No puedes desactivarte a ti mismo.'
7. duenio1 desactiva a qa-eq-m2 → permitido (duenio1 sigue siendo dueño activo); la sesión viva de qa-eq-m2 muere al instante.
8. Confirmar en la lista que qa-eq-m2 quedó inactivo pero conserva la marca de dueño (histórico).

### E6 · Los no-dueños rebotan en todo /api/equipo

1. Loguear supervisor1 / e2e12345.
2. Esperado: el menú NO muestra 'Equipo'.
3. ✖ Negativo: forzar la URL de la página Equipo → pantalla 'Solo el dueño del negocio gestiona el equipo.' sin datos.
4. ✖ Negativo (API): con DevTools y el Bearer de supervisor1: GET /ne/api/equipo → 403; POST /ne/api/equipo (alta) → 403; POST /ne/api/equipo/<id>/roles → 403; POST /ne/api/equipo/<id>/activo → 403; POST /ne/api/equipo/<id>/reset-password → 403; POST /ne/api/equipo/<id>/codueno → 403 (los seis endpoints).
5. Verificar que ninguna de esas llamadas cambió nada (la lista vista por duenio1 queda igual).
6. ✖ Negativo: sin Authorization → 401 'No autenticado o sesión expirada'.
7. Al terminar: como duenio1, desactivar todos los usuarios QA-eq-* creados (dejar el equipo como estaba).

**Ojo con esto**

- El cambio forzado de contraseña solo lo aplica la SPA, no la API: un cliente directo (curl) puede operar con la clave temporal sin cambiarla — abierto, severidad baja (§11 no lo tabula por ser solo-API; no lo reportes como nuevo).

---

## 10 · Bugs corregidos en esta ronda (re-test)

Estos fixes YA están aplicados. Verifica cada uno con su paso de re-test; si el comportamiento viejo reaparece, repórtalo como **regresión** (no como bug nuevo).

| # | Fix aplicado | Re-test en la SPA |
|---|---|---|
| 1 | Reserva ya no acepta el endpoint de adelanto de taller (antes: quedaba atascada en 'encolada' sin salida y con Vía A facturaba el abono) | Crear una reserva → como cajero1 la única acción de cobro visible es "Cobrar abono"; el POST /adelanto por API rebota (§4 E5 paso 5) |
| 2 | Vía A rechaza el adelanto de una orden con líneas mixtas (gravada+exonerada) ANTES de cobrar (antes: cobraba, facturaba y la orden no se podía entregar ni anular) | Con Vía A ON, orden con línea gravada 118 + exonerada 20 → 'Cobrar adelanto' 50 rebota sin cobrar ni emitir nada (§3 E5 paso 3) |
| 3 | Gates de rol en CAJA y GASTOS: vendedor/operario/despachador/contador ya no pueden registrar movimientos, cerrar caja ni tocar gastos (antes el ACL de emisor los dejaba pasar; un vendedor llegó a CERRAR la caja) | Como vendedor1, teclear /caja y forzar 'Registrar movimiento' o 'Cerrar caja' → rebota "Tu rol no maneja el dinero de la caja" (§8 E6 paso 8) |
| 4 | Los 403 de rol ahora dicen el motivo real ("Cobrar el adelanto es del cajero…") en vez de "puede pertenecer a otra empresa" | Como vendedor1, forzar el cobro de un adelanto → el mensaje explica el rol que falta, no habla de otra empresa |
| 5 | El número del comprobante de anticipo se muestra fiscal (F001-00000013) y no el interno de Odoo ("F 00000013") | Cobrar un adelanto con Vía A ON → el flash y la tarjeta de la orden muestran el número serie-correlativo (F001-…), ubicable en Comprobantes (§5 E2 paso 3) |
| 6 | Anular una orden valida PRIMERO el rol/transición y luego lo fiscal (antes un cajero sin permiso recibía "emite la nota de crédito", acción que jamás podría hacer) | Como cajero1, intentar anular una orden encolada con anticipo → recibe el rechazo por rol, no el mensaje de la NC (§5 E4 paso 5) |
| 7 | La cola de cobro auto-expira las cotizaciones vencidas al leerla (antes seguían apareciendo como cobrables hasta que corría el cron) | Cotización con fecha retroactiva y validez corta, aceptada → al abrir la bandeja de cobro del cajero ya NO aparece (§2 E3 paso 2) |
| 8 | FACTURA exige RUC válido de 11 dígitos (antes se emitía factura a DNI y SUNAT la rechazaba después, con el correlativo ya consumido) | En Venta rápida o Emitir, tipo Factura con cliente DNI → bloqueado en el front Y rebota por API (§6 E1 paso 8) |
| 9 | Preflight con cliente inexistente devuelve un aviso JSON (antes: página HTML 404) | Venta con cliente NUEVO (DNI que no existe) → la venta procede normal; en la pestaña Network (o por curl) el preflight responde JSON, nunca una página HTML 404 |
| 10 | Venta sin cliente reutiliza el partner del mismo nombre (antes: creaba un duplicado por venta) | Dos ventas rápidas seguidas sin cliente → en Clientes hay UNA sola ficha 'CLIENTE VARIOS', sin fila nueva por venta (§6 E5 paso 4) |
| 11 | El mensaje de retiro imposible ya no revela el efectivo esperado (era una sonda para vaciar el conteo ciego) | En Caja, retiro de 99999 → el error rebota SIN decir cuánto hay disponible (§7 paso 3) |
| 12 | Un payload con 'precio' en vez de 'precioUnitario' ya no emite un comprobante en 0.00 silencioso | (Solo API) POST /ne/api/emitir con el campo 'precio' en las líneas → rebota con error claro; en Comprobantes NO aparece ningún doc en 0.00 |
| 13 | La SPA: supervisor y dueño ya ven las bandejas de cobro/despacho/adelanto/reservas/saldo; y los botones "Nueva cotización"/"Nueva orden" ya no se pintan a quien el backend rechaza | Entrar como supervisor1 (y duenio1) → Cotizaciones muestra 'Cola de cobro' y 'Cola de despacho', Órdenes muestra las 4 bandejas; como operario1/contador1 el botón "Nueva orden" ya no se pinta |

---

## 11 · Bugs conocidos ABIERTOS (no los reportes como nuevos)

Si ves alguno de estos comportamientos, márcalo como "conocido" en tu hoja — ya están registrados y pendientes de fix.

| Bug abierto | Severidad | Qué verás |
|---|---|---|
| Las colas de cobro y despacho son legibles por API por roles ajenos (operario, contador) | Media | En la SPA no se nota (el menú las oculta); solo con curl/DevTools un GET a /cotizaciones/cola-cobro o /despacho/cola responde 200 con datos de toda la empresa. Actuar sigue rebotando 403 |
| El cierre de caja con descuadre no exige supervisor | Media | Declaras +1.00 de diferencia al cerrar y la caja cierra igual, sin pedir aprobación de nadie |
| Los gastos no impactan el arqueo de caja | Media | Un gasto registrado en la pantalla Gastos no aparece como movimiento de la sesión ni descuenta del efectivo esperado |
| El POS emite con la caja cerrada | Media | Con la caja cerrada, un abono de orden rebota ("No hay una caja abierta") pero la Venta rápida SÍ emite — esa venta queda fuera de todo arqueo |
| Rutas directas sin guard de lectura en la SPA | Media | Tecleando la URL, un operario abre /cotizaciones, /comprobantes y los KPIs de Inicio con importes reales. Es solo lectura: cualquier acción rebota en el backend |
| Horas del arqueo en UTC | Baja | Las horas de apertura/cierre de la sesión salen adelantadas ~5 horas respecto de la hora de Perú |
| Medios de pago duplicados por mayúsculas ('Efectivo' vs 'efectivo') | Baja | El arqueo puede mostrar dos filas separadas para el mismo medio si se registró con distinta capitalización |
| El funnel, 'Marcar rechazada' y el ícono Eliminar de Cotizaciones se pintan a roles sin permiso | Media | Un cajero ve esos controles en su bandeja; al usarlos el backend rebota 403 (con el motivo real, fix 4). Molesto, no inseguro |
| Las bandejas de Órdenes cargan 50 filas fijas sin paginador ni total | Baja | Con más de 50 órdenes en una bandeja, la lista se trunca sin avisar; no hay contador de registros |
| JSON malformado por API devuelve 500 HTML en vez de 400 JSON | Media | Solo con curl/Postman y un body inválido: página de error HTML. Desde la SPA nunca ocurre y no corrompe datos |
| /equipo con rol no dueño muestra el cartel correcto pero además un toast rojo de 403 | Baja | El cartel 'Solo el dueño…' aparece bien; el toast extra es ruido, no un fallo de seguridad |

---

## 12 · Series por sucursal (dos locales que numeran aparte)

Estás probando el negocio que abrió un segundo local: Miraflores emite F001 y San Isidro F002, cada uno con su correlativo, su caja y su arqueo. Importa porque **la numeración es fiscal**: dos locales que compartan serie emiten el mismo número dos veces, y eso solo se corrige con una comunicación de baja ante SUNAT. El porqué de cada decisión está en [decision-serie-por-local.md](decision-serie-por-local.md).

> Necesitas `supervisor1` o `duenio1` para configurar (Series y los establecimientos de Negocio están detrás del permiso de configuración) y `cajero1` para vender. En un tenant sin anexos no verás el selector de local ni la columna Local: eso **también** es un resultado esperado (E10).

### E1 · Dar de alta el local y declarar sus series

1. Entrar como duenio1. Negocio → Establecimientos anexos → Nuevo: código `0002`, distrito Miraflores, dirección 'Av. Larco 100'. Guardar. Resultado: aparece junto al `0000` (Domicilio fiscal), que NO es editable ni borrable.
2. Ir a Series. Resultado esperado: la pantalla agrupa por local; las series que ya se usaron (F001, B001…) salen bajo 'Sin local declarado' con sus contadores reales.
3. '+ Nueva serie': serie `F002`, tipo Factura, local `0002`, marcar 'Usar esta serie por defecto en ese local'. Guardar. Resultado: F002 aparece bajo '0002', con 0 emitidos y próximo `00000001`.
4. Repetir con `B002` (Boleta) y `FC02` (Nota de crédito) en el mismo local `0002`.
5. Recargar la página. Resultado: todo sigue ahí (es configuración, no estado de pantalla).

### E2 · Emitir desde el local 2 y verificar lo que se declaró

1. Como supervisor1: Nuevo comprobante → factura a un RUC cualquiera y, en 'Establecimiento emisor', elegir `0002`. Emitir.
2. Resultado esperado: sale **F002-00000001** (no F001).
3. Abrir el detalle. Resultado: dice 'Emitido desde el local 0002 · Av. Larco 100'.
4. Descargar el XML y buscar `codLocalEmisor`. Resultado esperado: `0002`. (Paso opcional para probador no técnico.)
5. Emitir una segunda factura igual. Resultado: F002-00000002 — la numeración del local 2 avanza sola.

### E3 · ✖ Negativo: un cajero no puede crear establecimientos ni series

1. Entrar como cajero1 y abrir Series. Resultado esperado: ve la tabla con sus series y contadores, **sin** el botón '+ Nueva serie' ni los de Editar/Desactivar.
2. ✖ Negativo: por API, `POST /ne/api/series` con el token de cajero1. Resultado esperado: **403** con un mensaje que explica que configurar series es cambiar la numeración fiscal — no un texto técnico en inglés.
3. ✖ Negativo: ídem con `POST /ne/api/establecimientos` y `DELETE /ne/api/establecimientos/<id>`. Resultado esperado: 403, y el local sigue existiendo.

### E4 · ✖ Negativo: la misma serie en dos locales

1. Como duenio1: Negocio → alta del local `0003` (San Isidro).
2. Series → '+ Nueva serie': serie `F002` (la de Miraflores), local `0003`. Guardar.
3. Resultado esperado: rechazo con un mensaje que **explica la regla** —SUNAT numera por RUC y serie; los dos locales emitirían el mismo número y solo se corrige con comunicación de baja— y sugiere otra serie. Verificar en Series que F002 sigue en `0002`: no se movió en silencio.

### E5 · Carrera de dos locales con series distintas (5 + 5)

1. Dos navegadores: uno con la caja abierta en `0002` y otro en `0003` (ver E8). Declarar `F003` para el local `0003`.
2. Emitir alternando lo más rápido posible, 5 comprobantes desde cada local.
3. Resultado esperado: F002 llega a `00000005` y F003 a `00000005`, sin huecos ni repetidos. En Comprobantes, el filtro 'Local emisor' = 0002 muestra exactamente sus 5.

### E6 · Carrera con la MISMA serie (el caso que nunca debe duplicar)

1. Dejar el local `0003` **sin** serie propia de factura (desactivar F003) para que ambos caigan en la misma serie.
2. Emitir 10 comprobantes alternando entre las dos ventanas, lo más simultáneo posible.
3. Resultado esperado: correlativos `1..10` sin repetir NINGUNO. Un repetido aquí es un fallo **crítico** (duplicado fiscal): anótalo con las horas exactas.

### E7 · Nota de crédito de una venta del local 2

1. Como supervisor1 (o quien tenga el rol anulación): Comprobantes → buscar F002-00000001 → 'Anular por Nota de Crédito' → emitir.
2. Resultado esperado: la NC sale **FC02** y su detalle dice local `0002` — no FC01 ni domicilio fiscal, que es lo que hacía antes de esta fase.
3. En la NC, el selector de establecimiento se ve **deshabilitado**: el local de una nota es dato heredado del comprobante que corrige, no una elección.

### E8 · Dos cajas abiertas y dos arqueos que cuadran

1. Como cajero1: Caja → Abrir, local `0002`, saldo inicial 100. Vender 2 boletas en efectivo desde Venta rápida.
2. En otro navegador, con otro usuario con rol caja: Caja → Abrir, local `0003`, saldo inicial 50. Vender 1 boleta en efectivo.
3. Resultado esperado: ambas cajas conviven y el encabezado de cada una muestra su local.
4. ✖ Negativo: intentar abrir una TERCERA caja en el local `0002`. Resultado esperado: 'Ya hay otra caja abierta para ese mismo local'.
5. Cerrar la caja de `0002` contando SOLO sus ventas. Resultado esperado: diferencia `0.00`. Si el esperado incluyera las ventas del otro local, el arqueo nacería descuadrado y quedaría congelado así (es inmutable).

### E9 · ✖ Negativo: un código de establecimiento inventado

1. Por API, emitir con `codEstablecimiento: "0009"` (inexistente en el catálogo). Resultado esperado: rechazo con un mensaje que distingue **'no existe en tu catálogo'** de **'no está dado de alta ante SUNAT'** — esto último es trámite externo de la ficha RUC, no algo que el sistema pueda dar por hecho porque lo hayas creado en Negocio.

### E10 · Retrocompatibilidad: el negocio de un solo local

1. En un tenant SIN establecimientos anexos y SIN series declaradas, emitir una factura, una boleta y una NC de boleta.
2. Resultado esperado: `F001`, `B001` y `BC01`, exactamente como antes. En Comprobantes **no** aparece la columna 'Local' ni el filtro 'Local emisor'; en Emitir no aparece el selector de establecimiento; la caja abre sin preguntar local.

### E11 · Upgrade sobre una copia del dump de producción

1. Restaurar una copia del dump real y anotar la pantalla Series (series, emitidos, último, próximo).
2. Correr `-u l10n_pe_ne_biller` sobre esa copia. Resultado esperado: **0 errores** en el log.
3. Volver a abrir Series. Resultado esperado: **idéntica** a la de antes — el registro no se siembra: lo que se ve sigue siendo el uso real.
4. Emitir un comprobante. Resultado esperado: continúa el correlativo donde estaba (no reinicia en 1).
5. Abrir caja y cerrarla. Resultado esperado: igual que antes del upgrade, sin preguntar local si el tenant no tiene anexos.

### E12 · Venta rápida (POS) desde el local 2

1. Como cajero1, con la caja abierta en `0002`: abrir Venta rápida. Resultado esperado: junto al total hay un chip de solo lectura con el local y la serie ('0002 · B002'); **no** hay un paso nuevo que elegir (los 3 toques del POS no admiten uno más por venta).
2. Cobrar una boleta. Resultado esperado: sale `B002-…` y su detalle declara el local `0002`.
3. Cerrar la caja y, sin abrir ninguna, cobrar otra venta desde el POS. Resultado esperado: sale con la serie y el local del domicilio fiscal (`0000`), igual que antes de esta fase.

---

Última actualización: 2026-08-02 · entorno local testdb

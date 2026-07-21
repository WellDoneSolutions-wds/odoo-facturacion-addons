# Decisión de diseño — Fricción PyME: el dueño-solo contra su propio POS

> Cierra el análisis de fricción que [decision-escala-libre.md](decision-escala-libre.md) dejó abierto: esa decisión prueba que el dueño con todos los roles **puede** avanzar todo documento (corrección); esta mide si **querrá** (fricción). Método: recorrido clic a clic de **CN-01** (cuaderno de S/5 con cotización) y **CN-02** (impresión de S/20 con S/10 de adelanto) del [catalogo.md](catalogo.md), medido contra la vara real del dueño: **"Venta rápida" (`POS.tsx`) = 3 toques, 1 pantalla, ticket automático** (1 toque si escanea).

## 1. El veredicto en una línea

**Sí le suma fricción — no en la cola, que el fold resuelve, sino en la cabeza de cada proceso**: tal como están las fichas, CN-01 cuesta ~10-11 toques (3-4x el POS) y CN-02 ~26-35 (10x), con el dueño reabriendo el mismo documento 4 veces para pasarse la pelota a sí mismo; sin las reglas de §3 el dueño-solo vuelve al POS y al papel (y los S/10 del adelanto, al cajón sin comprobante — exactamente lo que el producto existe para evitar), y con ellas CN-01 baja a ~7 toques y CN-02 a ~5+3, que sí compite con la vara.

| Recorrido | Toques | Pantallas | Vs. POS (3 toques, 1 pantalla) |
|---|---|---|---|
| CN-01 según ficha, mejor caso (cliente registrado, caja abierta) | ~10-11 | 3-4 | 3-4x |
| CN-01 según ficha, cliente nuevo | ~15 | 5 | 5x |
| CN-01 hoy sin diseño (vía redirect a `/emitir`) | ~14 + revisar formulario | 5 | ~5x |
| CN-02 según ficha (3 momentos) | ~26-35 | ~7 | ~10x |
| **CN-01 con degradación §3** | **~7** | **2** | **aceptable** |
| **CN-02 con degradación §3** | **~5 + ~3** | **2** | **aceptable** |

Lo que el diseño vigente **sí** resuelve y no hay que tocar: el fold pliega la **cola** (caja + despacho: ahorra el viaje a `/emitir` y la pantalla de despacho, ~4 toques), CN-01 solo se dispara cuando el cliente **pide** cotización ("no compra en el acto", disparador de la ficha) — la venta de mostrador nunca pasa por ahí —, y la **bandeja** ("qué le debo a la gente", "qué plata cobré por trabajo que aún debo") es útil con 1 usuario igual que con 12. Lo que no resuelve: la **cabeza** de cada proceso (cliente obligatorio, cotización obligatoria, funnel, tomar-la-cola, ceremonia del anticipo) queda intacta y es donde vive el 70% de la fricción.

## 2. CN-01 y CN-02 clic a clic

### CN-01 — cuaderno de S/5, el cliente sí pidió cotización (mejor caso)

| # | Paso | Pantalla | Clics | ¿Existía en el POS? |
|---|---|---|---|---|
| 1 | Ir a Cotizaciones | Menú → lista (`App.tsx:40`, `:331`) | 1 | No — en POS ya vivo en `/pos` |
| 2 | "Nueva cotización" | Modal (`Cotizaciones.tsx:201-202`) | 1 | No |
| 3 | Buscar y elegir cliente — **obligatorio** (`Cotizaciones.tsx:398`) | Modal | 2 (+4-6 si es nuevo) | No — POS acepta CLIENTE VARIOS solo (`POS.tsx:204`) |
| 4 | Buscar y elegir producto | Modal (`Cotizaciones.tsx:451`) | 2 | Sí — empate |
| 5 | "Crear cotización" → Borrador | Modal (`Cotizaciones.tsx:518`) | 1 | No |
| 6 | Darle el papel: fila → Ver PDF → imprimir | Lista + PDF (`Cotizaciones.tsx:240-241`) | 2-3 | No — el ticket del POS sale solo (`POS.tsx:237`) |
| 7 | "Ya, lo llevo": abrir detalle + marcar Aceptada | Drawer (funnel `Cotizaciones.tsx:78`, hoy salta Enviada) | 2 | No |
| 8 | **"Cobrar y entregar S/ 5.00"** — el fold prometido | Drawer (si está AHÍ; +3 si hay que ir a "Pendientes") | 1 | El cobro sí: `cobrar()` (`POS.tsx:201-250`) |
| | **Total** | **3-4 superficies** | **~10-11** | **POS: 3** |

Advertencia del paso 8: hoy ese paso es "Convertir a comprobante" → redirect a `/emitir` "para que revises y emitas" (`Cotizaciones.tsx:165-193`) → pantalla completa → "Emitir a SUNAT" (`Emitir.tsx:719`) = +3-4 toques y una mudanza. Si el fold termina siendo ese redirect con otro nombre, no es un clic. Y el payload de Emitir manda `formaPago {tipo:'Contado'}` **sin medios** (`Emitir.tsx:466-468`), a diferencia del POS que los detalla (`POS.tsx:211-213`): la venta convertida llega a la caja sin medio detallado.

### CN-02 — impresión de S/20, deja S/10 a cuenta

**Momento 1 — encarga y deja S/10:**

| # | Paso | Pantalla | Clics | ¿Existía en el POS? |
|---|---|---|---|---|
| 1 | Ir a Cotizaciones (la ficha exige cotización aceptada como disparador) | Menú → lista | 1 | No |
| 2 | Elegir cliente (o Varios) | Modal | 2 | No |
| 3 | Producto "Impresión" | Modal | 2 | Sí — empate |
| 4 | Corregir precio a 20 | Modal | 1-2 | Sí — empate |
| 5 | "Crear cotización" | Modal | 1 | No |
| 6 | **Reabrir** la cotización recién creada (el modal no deja en el detalle) | Lista → drawer | 1 | No |
| 7 | Marcar Aceptada (el cliente está parado delante) | Funnel | 1 | No |
| 8 | Crear orden de trabajo: elegir `fecha_pactada` + confirmar (`POST /pedidos`) | Pantalla nueva | 2-3 | No |
| 9 | Cobrar adelanto: teclear 10, medio, confirmar (`POST /pedidos/<id>/adelanto`; exige caja abierta: +3 si no abrí) | Cobro | 3 | Parcial — POS cobra pero no "a cuenta" |
| 10 | Boleta de anticipo 0104 + ticket | — | 0-1 | Ticket sí, automático |
| | **Subtotal** | **5 pantallas** | **14-17** | |

**Momento 2 — hago la impresión yo mismo:**

| # | Paso | Pantalla | Clics | ¿Existía en el POS? |
|---|---|---|---|---|
| 11 | Navegar a la cola de taller | Menú → cola | 1 | No |
| 12 | Ubicar MI orden (creada hace 40 segundos) y abrirla | Cola | 1-2 | No |
| 13 | **"Tomar"** (`user_id` NULL → yo) → en_atencion | Detalle | 1 | No |
| 14 | Imprimir la hoja (mundo real, 30 s) | — | — | — |
| 15 | **Reabrir** la orden → "Listo" | Cola → detalle | 2 | No |
| 16 | "Recepción avisa al cliente" (me está mirando imprimir) | — | 0 | No |
| | **Subtotal: pasarme la pelota a mí mismo** | | **5-6** | |

**Momento 3 — el cliente vuelve (o nunca se fue):**

| # | Paso | Pantalla | Clics | ¿Existía en el POS? |
|---|---|---|---|---|
| 17 | Reabrir la app en la cola y **buscar la orden** (el ticket de anticipo no lleva número de orden: busco a ciegas) | Menú → cola | 2-3 | No |
| 18 | "Cobrar saldo": teclear 10, medio (`POST /pedidos/<id>/saldo` — si reusa el patrón redirect de CN-01: +1 pantalla, +2 clics) | Detalle | 3 (o 5) | Parcial |
| 19 | "Entregar": teclear `receptorNombre`/`receptorDoc` — **un DNI por una impresión de S/20** | Detalle | 2-4 | No |
| | **Subtotal** | | **7-12** | |

**Total CN-02: ~26-35 interacciones, ~7 pantallas.** El cliente vuelve 1 vez; yo "vuelvo" al documento 4 veces (crear→reabrir para aceptar, cola para tomar, cola para listo, cola para saldo+entrega). El punto más caro es la **cola como peaje**: con |U|=1, "Tomar" produce cero información (la respuesta a "¿quién lo hace?" es una constante) — es un buzón donde me dejo cartas a mí mismo. Y el diseño de la ficha (`→ en_atencion: solo desde en_cola`, `lista` solo después) implica que el botón "Listo" **no existe** hasta que "tomé": obliga al teatro o esconde el botón.

## 3. Las reglas de degradación mínima

**Marco**: nada de esto es una rama `if |U|==1` — eso violaría la escala libre. Son **defaults, folds y visibilidad de acciones** calculados por `_l10n_pe_ne_acciones()` (estado × grupo × guarda × política): al que tiene todos los roles le aparecen los folds completos; al cajero puro, solo su tramo. El cajero-solo-técnico de un taller grande recibe los mismos folds. "Degradación" = qué colapsa **solo** porque un mismo usuario puede firmar todo el tramo.

**D1 — Venta/encargo directo: la cotización es un origen opcional, jamás la puerta.**
Nadie cotiza una impresión de S/20; los pasos 1-7 del momento 1 son puro peaje y el abandono #1.
*Cómo:* `POST /ne/api/pedidos` acepta `lineas` **sin** `cotizacionId` (hoy la ficha lo pide como disparador); pantalla "Nuevo encargo" hermana del POS en el menú (cliente default genérico + ítems + "deja a cuenta: S/___" + fecha default hoy), no detrás de Cotizaciones. Si existe cotización, se enlaza; jamás se exige.

**D2 — Cotización sin partner: nombre libre o VARIOS; el partner se crea al cobrar.**
Por un cuaderno de S/5 nadie registra un partner; la proforma muere en el paso 3.
*Cómo:* quitar el rechazo `if (!cliente)` (`Cotizaciones.tsx:398`); campo `cliente_nombre` libre en `l10n_pe_ne.cotizacion`; el partner nace dentro del fold de cobro con el patrón DNI que el POS ya tiene (`POS.tsx:651-660`).

**D3 — El fold "Cobrar y entregar" vive en el drawer del documento, y jamás es un redirect.**
Si el fold es "Convertir" → viaje a `/emitir` → revisar → "Emitir a SUNAT", son N clics disfrazados de uno.
*Cómo:* endpoint compuesto `POST /ne/api/cotizaciones/<id>/cobrar-entregar {medios?}` que emite + entrega + imprime en un `_run`; la SPA lo pinta donde el usuario está parado (drawer), servido por `_l10n_pe_ne_acciones()`. Retirar el patrón "carga borrador en Emitir y revisa" (`Cotizaciones.tsx:165-193`) como camino del fold. La cola "Pendientes" es la **otra** entrada, nunca la única.

**D4 — Fold de fold desde Borrador: "Aceptar, cobrar y entregar".**
Si el cliente aceptó frente a mí, "Enviada" es ficción y "Aceptada" un clic de teatro.
*Cómo:* la acción compuesta arranca desde `borrador` atravesando `aceptada` en el mismo commit; R7 (transiciones como métodos) **no** debe eliminar el salto borrador→aceptada que el funnel ya permite (`Cotizaciones.tsx:78`).

**D5 — Los defaults del fold no preguntan nada que el sistema ya sabe.**
Cada pregunta convierte el clic en N clics.
*Cómo:* `tipoDoc` deducido del documento del cliente (11→factura, 8/nada→boleta — regla que ya existe en `Cotizaciones.tsx:167-169`); `medios = [{Efectivo, total}]` como el POS (`POS.tsx:211-213`) — y arreglar que la vía Emitir manda `formaPago` **sin** medios (`Emitir.tsx:466-468`); `receptor` = el cliente del documento, `fecha` = ahora (el formulario de receptor solo si YO lo abro); ticket solo si `autoPrint` (`POS.tsx:64`, `:237`).

**D6 — El despacho nace y muere dentro del fold, y el botón primario es el fold.**
Si cobro con "solo Cobrar" porque era el botón grande, me nace un "despacho: pendiente" por mercadería que el cliente ya se llevó: contador rojo mintiendo cada mañana.
*Cómo:* el fold atraviesa `despacho: pendiente→entregado` en el mismo commit (los 8 ms de la decisión); `_l10n_pe_ne_acciones()` ordena las acciones poniendo el fold **primero** cuando el usuario puede firmar el tramo completo, y las transiciones sueltas después.

**D7 — "Listo" directo desde `en_cola`: no obligar a "tomar" para poder terminar.**
Con una persona el ritual tomar→reabrir→listo son 5-6 interacciones que producen cero información.
*Cómo:* `_l10n_pe_ne_acciones()` ofrece a quien tenga el grupo de técnico DOS acciones sobre una orden en cola: "Tomar" (arbitra carreras en el taller con varios) y "Marcar listo" (fold tomar+listo: `user_id = uid` durante el tránsito, bitácora completa). `POST /pedidos/<id>/listo` acepta origen `en_cola` ejecutando el tomar internamente. Sin ramas por cantidad de usuarios.

**D8 — El anticipo se teclea una vez; la mecánica fiscal es invisible.**
Yo digo "deja 10" y "cobrar saldo"; el sistema arma 0104, vínculo y descuento global 04 solo, porque el pedido **ya sabe** su `anticipo_move_id`. Nunca existe una pantalla donde un humano elige qué anticipo aplicar a qué factura — eso es un JOIN, no una decisión.
*Cómo:* `POST /pedidos/<id>/saldo` resuelve `anticipo_move_id` automáticamente (Many2one, falta #3 de la ficha); el `Char` fallback queda solo para onboarding/backend. En pantalla y en el ticket jamás aparecen "0104", "descuento global 04" ni "regularización": el ticket final dice **"A cuenta: S/10 · Saldo: S/10"**, no "Descuento S/10" (el cliente preguntaría qué descuento le hice).

**D9 — Pago completo = un solo comprobante.**
Si "a cuenta" = total, o el cliente espera y paga al recoger, emitir 0104 + regularización por un pago único del mismo instante es ceremonia sin base legal.
*Cómo:* el fold de encargo con `adelanto == total` (o 0) emite **una** boleta común; el pedido existe solo como recordatorio de entrega. Y "Cobrar saldo y entregar" disponible ya desde `en_cola` (D7+D10 encadenados) para el cliente que espera: la cola existió 4 minutos, todos los estados transitados y firmados, los reportes no divergen.

**D10 — "Cobrar saldo y entregar": el gemelo de CN-01 que CN-02 no tiene y necesita.**
*Cómo:* un botón que emite la regularización (04 contra `anticipo_move_id`, sin dropdown), registra medios, atraviesa `lista→entregada` con receptor opcional e imprime. El gate "no se entrega con saldo pendiente" (guarda de realidad, eje 3 — correcta) **pasa sola** porque el cobro ocurre primero dentro del fold: jamás dispara un diálogo que con 1 usuario se auto-aprueba en el 100% de los casos por definición.

**D11 — Receptor de despacho opcional; `fecha_pactada` default hoy.**
Si el DNI es obligatorio para entregar, las órdenes se apilan en `lista` sin marcar "Entregada" y el reporte de vencidas del Supervisor se vuelve mentira.
*Cómo:* `receptorNombre`/`receptorDoc` **opcionales** en `POST /despacho/<id>/entregar` y `POST /pedidos/<id>/entregar` (default: el cliente del documento); quien quiera exigirlos lo enciende como gate `off/aviso/bloqueo` por RUC. `fecha_pactada` prellenada con hoy; la `mail.activity` de recordatorio solo se crea si `fecha_pactada > hoy`. El ticket de anticipo imprime el **número de orden** (o al volver el cliente busco a ciegas).

**D12 — Gates en `off` de fábrica y además invisibles; caja cerrada se abre inline.**
Mientras un gate esté `off`: cero badges, cero flash "auto-aprobado" (a la bitácora, no a mi cara) — la decisión ya lo promete; falta vigilarlo.
*Cómo:* extender `test_escala_libre.py` para asertar que la serialización hacia la SPA **omite** todo rastro de gate cuando `modo='off'`. Y caja cerrada: botón "Abrir caja" inline dentro del propio fold (saldo inicial prellenado), no el viaje a `/caja` de hoy (`POS.tsx:134`).

Con D1-D12: CN-01 queda en ~7 toques (cliente libre, crear, PDF, aceptar-cobrar-entregar) y CN-02 en dos momentos de ~5 y ~3 clics con dos comprobantes fiscales correctos. El POS sigue intocado en 3.

## 4. La línea roja: dónde NO se degrada

La distinción operativa: **SUNAT exige la EMISIÓN, no la CEREMONIA.** Todo lo fiscal se emite de verdad y completo — pero por debajo del fold, sin que el dueño lo teclee ni lo lea.

**Innegociable (fiscal / integridad — el fold lo ejecuta, jamás lo omite):**

| Qué | Por qué |
|---|---|
| El anticipo se **factura de verdad**: comprobante 0104 (cat. 51) cuando hay plata a cuenta con entrega pendiente | SUNAT no permite recibir plata a cuenta sin comprobante. Prerequisito duro: hoy `_l10n_pe_tipo_operacion` ni emite 0104 (falta 🔴 #2 de CN-02) |
| La regularización: descuento global **04** referenciando el anticipo, `indDocRelacionado 2`, IGV sobre el neto | Es la única representación que SUNAT acepta (el anticipo nativo de `sale` con líneas negativas, no — la ficha hace bien en rechazarlo) |
| **Unicidad** del anticipo aplicado, mismo partner, vínculo Many2one, monto ≤ total | Sin esto el mismo anticipo se descuenta dos veces (falta #3 de CN-02) |
| Caja abierta para recibir efectivo | El efectivo debe caer en un arqueo; el tope retiro ≤ disponible es guarda de realidad, no permiso |
| Todo cobro emite y firma su comprobante en el acto | `quick_emit` es atómico; no existe "cobrar sin papel" |
| DNI del comprador en boleta ≥ S/700 | Regla fiscal que vive en la **emisión** (ya la maneja el addon) — no confundir con el receptor del despacho, que es proceso |
| Bitácora de **cada** transición atravesada por el fold (`mail.thread`, `es_auto_aprobacion`) | El fold no salta estados: los atraviesa y los firma. La indistinguibilidad "tres personas en dos horas" vs "una en 400 ms" **es** la escala libre |
| El reporte "anticipos sin regularizar" | Es plata cobrada por trabajo debido: un pasivo. Único lugar donde la cola le sirve de verdad al dueño-solo |

**Degradable (ceremonia de proceso — nada de esto lo pide SUNAT):** la cotización previa, el estado "Enviada", el partner registrado antes de cobrar, "Tomar" la cola, el receptor con DNI en la entrega de boleta chica, elegir a mano qué anticipo aplicar, la `fecha_pactada` tecleada, ver las palabras "0104" / "descuento global" / "regularización" en pantalla, y el par 0104+regularización cuando el pago fue único y en el mismo instante (D9: ahí el anticipo fiscal ni siquiera existe).

## 5. Parches a los documentos vigentes

**En [decision-escala-libre.md](decision-escala-libre.md):**

1. **§"Las colas con 1 usuario"**: precisar que el fold prometido pliega hoy solo la **cola** (caja+despacho) y extenderlo a la **cabeza**: los folds pueden abarcar más de un eje y arrancar antes de `aceptada` (D4: "Aceptar, cobrar y entregar" desde `borrador`). Añadir los gemelos de CN-02: "Recibir encargo y cobrar adelanto" (D1) y "Cobrar saldo y entregar" (D10).
2. **§"La SPA no decide nada"**: `_l10n_pe_ne_acciones()` no solo calcula **qué** acciones — también su **orden**: el fold es el botón primario cuando el usuario puede firmar el tramo completo (D6), y las acciones aparecen en el drawer del documento, no solo en la bandeja (D3).
3. **§"Los gates"**: añadir la regla de invisibilidad — `modo='off'` implica cero rastro en la serialización a la SPA (ni badges ni "auto-aprobado" en pantalla), vigilado por `test_escala_libre.py` (D12).
4. **Regla nueva**: "un formulario del fold jamás pregunta lo que el sistema ya sabe" (receptor = cliente, fecha = ahora, medios = efectivo por el total, anticipo a aplicar = `anticipo_move_id`) — elegir en un JOIN no es una decisión (D5, D8).

**En [catalogo.md](catalogo.md):**

1. **CN-01, paso 1 y "Qué falta" #3**: la cotización acepta `cliente_nombre` libre / VARIOS; el partner nace al cobrar (D2). Retirar "cliente obligatorio" como precondición de guardar.
2. **CN-01, endpoints**: añadir `POST /ne/api/cotizaciones/<id>/cobrar-entregar {medios?}`; `receptorNombre`/`receptorDoc` de `POST /despacho/<id>/entregar` pasan a opcionales con default = cliente (D3, D11). Documentar que la conversión vía redirect a `/emitir` no es el fold. El fold manda `medios` detallados (hoy Emitir no: `Emitir.tsx:466-468`).
3. **CN-02, disparador**: de "el cliente acepta la cotización" a "el cliente encarga (con cotización **opcional** como origen)"; `POST /pedidos` con `cotizacionId` opcional y pantalla "Nuevo encargo" (D1).
4. **CN-02, transiciones**: `→ lista` también desde `en_cola` como fold tomar+listo para quien tenga el grupo (D7). "Tomar" queda documentado como arbitraje de carreras, no como peaje.
5. **CN-02, pasos 2-3 y 7**: añadir el caso degenerado — `adelanto == total` o pago único en el acto ⇒ **un** comprobante común, sin 0104 (D9); `fecha_pactada` default hoy y `mail.activity` solo si es futura; el ticket de anticipo imprime el número de orden (D11).
6. **CN-02, paso 8**: el gate de saldo pendiente se documenta como guarda que el fold "cobrar saldo y entregar" satisface por orden de operaciones, no como diálogo de autorización (D10).
7. **CN-02, "Qué falta" #3**: elevar el Many2one `anticipo_move_id` de "arreglo del vínculo" a **precondición del fold silencioso** (D8): sin él no hay regularización sin dropdown.
8. **Transversal (R-nueva o nota en R7)**: R7 cierra las puertas de escritura cruda del estado pero **no** elimina los saltos legítimos plegados por folds (borrador→aceptada, en_cola→lista, lista→entregada): el fold es un método más, con sus tres ejes por cada transición atravesada.

---

*Referencias de código citadas:* `ne-express/apps/web-bff/src/pages/POS.tsx` (`:64`, `:134`, `:201-250`, `:204`, `:211-213`, `:237`, `:283`, `:651-660`), `Cotizaciones.tsx` (`:78`, `:165-193`, `:167-169`, `:201-202`, `:240-241`, `:398`, `:451`, `:518`), `Emitir.tsx` (`:466-468`, `:719`), `App.tsx` (`:40`, `:331`); `catalogo.md` CN-01 (pasos, endpoints `:118-124`) y CN-02 (pasos, transiciones, faltas 🔴 #2 y #3, endpoints `:179-189`); `decision-escala-libre.md` (`:51`, `:59-61`, `:78`, `:82`).

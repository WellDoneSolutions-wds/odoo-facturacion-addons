# Decisión de diseño — Integridad del dato antes que las compuertas

> El hallazgo más importante del levantamiento, y el que **reencuadra** la pregunta del usuario sobre cuántas personas hay en el local.
> Todos los defectos de abajo están **✅ verificados a mano** contra `l10n_pe_ne_biller` v19.0.1.6.0.

## La tesis

El debate "1 usuario vs N usuarios" era una distracción. El control interno no se rompe por quitar `aprobador ≠ solicitante` (ver [decision-escala-libre.md](decision-escala-libre.md)): se rompe porque **el dato que se registra es falso, anónimo o mutable**.

> Un control detectivo sobre datos que el controlado escribe y puede reescribir después no es un control debilitado: es un **generador de coartadas con timestamp**. "Registro + evidencia + revisión asíncrona" es **teatro** hasta que el dato sea verdadero, atribuible e inmutable.

Cuatro fraudes concretos que **ningún gate del catálogo toca**, con 5 empleados y roles perfectamente separados. Ninguno se arregla contratando a un supervisor; los cuatro se arreglan sin contratar a nadie. Por eso van **antes** que el motor de políticas: sin ellos, el resto del plan es decorativo y encima audita.

---

## D-1 · Conteo ciego 🔒 SIEMPRE

**Defecto (✅ verificado):** `GET /ne/api/caja` sirve `esperado[]` y `esperadoTotal` mientras la sesión está abierta (`models/l10n_pe_ne_caja.py:110, 132-133`). La SPA los pinta en la misma fila donde el cajero teclea el conteo, con la diferencia en vivo. `l10n_pe_ne_cerrar_caja` recibe `conteos` — **un número que el cajero eligió viendo el esperado, no que midió.**

**Consecuencia:** con `modo='bloqueo'` y `tolerancia=0` (máximo rigor expresable), el cajero lee `S/ 1,240.00`, teclea `1240`, `dif = 0`, el gate duerme. El gate de descuadre solo atrapa al ladrón que no sabe restar o al honesto que se equivocó: **solo falsos positivos sobre gente honesta, cero verdaderos positivos sobre el fraude.** Peor que apagado: consume la atención del dueño justo cuando no hay nada que ver.

**Arreglo (~20 líneas, cero personas):** no devolver `esperado[]`/`esperadoTotal` mientras la sesión esté `abierta` para quien va a contar. El arqueo vuelve **en la respuesta del cierre** (que ya lo devuelve). Convierte `diferencia` de **declaración** en **medición**. Sin esto, todos los gates de caja son decorativos con 1 usuario y con 500.

---

## D-2 · Inmutabilidad tras el hecho 🔒 SIEMPRE

**Defecto (✅ verificado):** el gasto se puede editar y borrar sin ninguna guarda —
`l10n_pe_ne_update_gasto` (`models/l10n_pe_ne_gasto.py:80`) hace `write` directo; `l10n_pe_ne_delete_gasto` (`:99-102`) hace `unlink()` sin mirar estado ni fecha ni permiso — y el ACL reparte `1,1,1,1` a todo emisor (`security/ir.model.access.csv:6`). Idéntico en cotización: `l10n_pe_ne_delete_cotizacion` borra una `convertida` con comprobante fiscal vinculado.

**Consecuencia:** el encargado registra el martes `"Flete Lima–Huancayo · S/850"` y se embolsa la plata. El domingo el dueño abre la revisión, lo ve, le parece caro pero pasa. El lunes el encargado lo edita a `S/85` o lo borra: **el gasto no existió nunca.** El `write_uid` se sobrescribe, no queda ni la sombra. La revisión certificó el vacío.

Esto detona el eje de revisión entero: **si el registro es mutable después de la revisión, la revisión no vale nada.** Y el mixin de flujo **no lo cubre**, porque `l10n_pe_ne_update_gasto` no pasa por `_avanzar` — es un `write` por una ruta que la máquina de estados no conoce. El diseño puso puertas en los pasillos y dejó la pared abierta.

**Arreglo:** `write`/`unlink` de gasto y movimiento de caja bloqueados cuando (a) la sesión cerró, (b) pasaron N horas, o (c) `control_estado='revisado'`. **Corregir un gasto = contra-asiento, no `write`.** No exige a nadie: exige que un registro sea un registro.

---

## D-3 · Autoría visible desde la única interfaz que existe 🔒 SIEMPRE

**Defecto (✅ verificado):** el modelo `l10n_pe_ne.gasto` (`models/l10n_pe_ne_gasto.py:14-25`) tiene seis campos — `fecha, descripcion, cuenta, monto, currency_id, company_id` — y **no tiene `user_id`, ni estado, ni `sesion_id`**. `_l10n_pe_ne_gasto_dict()` no devuelve quién lo registró. El `create_uid` existe en la tabla (Odoo lo escribe solo) pero **ningún endpoint lo expone**: solo se ve entrando por `/web`, que está detrás de infraestructura y que H-4 va a multiplicar en usuarios con contraseña.

**Consecuencia:** un `create_uid` que ningún endpoint expone no es trazabilidad, es un log que nadie va a leer.

**Arreglo:** `user_id` en gasto y en la venta, **servido en el dict**; `cajero_id` en la sesión. El patrón ya existe a un copy-paste: `l10n_pe_ne.caja.movimiento` (`caja.py:106, :310`) sí expone y puebla el usuario. Es la referencia.

---

## D-4 · Retiro de caja con contraparte documental 🔒 SIEMPRE (sobre umbral)

**Defecto (✅ verificado):** `l10n_pe_ne_caja_movimiento` (`models/l10n_pe_ne_caja.py:222-250`) exige para un retiro: tipo válido, **motivo ≥ 3 caracteres**, monto > 0, y monto ≤ efectivo disponible. **Nada más: sin aprobación, sin umbral, sin tipificación, sin contraparte.** Y el retiro **resta del esperado** (`tools/caja_arqueo.py:71-72`).

**Consecuencia — el fraude que ningún gate mira:** el cajero saca S/400 y registra `retiro / "pgo" / 400`. El esperado baja S/400, el conteo cuadra, **`diferencia = 0.00`**. El gate `descuadre` —la joya de la corona— no se entera jamás, no porque esté apagado, sino porque **mide la variable equivocada**: el retiro no descuadra, hace desaparecer el descuadre del esperado. Con 5 empleados y segregación de libro, la plata sale por una puerta que ningún diseño miró, con el arqueo en verde.

**Arreglo:** sobre umbral, el retiro exige tipificación (`deposito` con `voucher_ref` + fecha) o no se registra. No exige un segundo cuerpo: exige un papel de un tercero verificable. Es la misma doctrina que el catálogo aplica bien a las devoluciones (lo que gobierna la devolución no es un jefe, es la nota de crédito) y que no aplicó al único movimiento que saca plata del cajón sin rastro.

---

## Además: dos entradas rotas de gates que ya conocíamos

Los gates de caja tampoco pueden encenderse mientras el **esperado** siga mal calculado — ver [hallazgos.md](hallazgos.md):
- **H1** — los dólares en efectivo son invisibles al arqueo (`caja_arqueo.py:29-32` hace `continue`).
- **H2** — toda venta emitida desde *Nuevo comprobante* se imputa 100% a Efectivo porque `Emitir.tsx` no manda `medios`.

**Regla:** ningún flujo de aprobación se implementa antes de que su entrada sea correcta. Un gate sobre un número falso salta a diario sobre ventas legítimas, el dueño aprueba en automático, y el control muere de fatiga de alarma dejando un registro firmado que certifica un número falso.

---

## Prioridad — contradice el orden de olas del catálogo

**D-1 (conteo ciego) y D-2 (inmutabilidad) valen más que las 7 filas del motor de `_GATES`, `es_auto_aprobacion` y la pantalla de revisión juntos** — y son una fracción del código. El catálogo puso en su Ola 0 ("sin esto todo lo demás es decorativo") solo las puertas de la cotización. La intuición era correcta y la lista, incompleta: **D-1..D-4 + H1 + H2 pertenecen a esa Ola 0.** Sin ellos, el eje de revisión asíncrona de la escala libre audita datos corruptos, y el cliente que paga por control recibe una falsa sensación de seguridad: cuatro filas verdes el domingo que no miran su negocio.

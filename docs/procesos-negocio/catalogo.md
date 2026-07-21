# Catálogo de procesos de negocio — Factorii / NE Express

> ⚠️ **PARCIALMENTE SUPERSEDIDO por las decisiones del 2026-07-17.** Este catálogo se escribió antes de que el usuario decidiera que el producto debe funcionar de **1 usuario con todos los roles a N usuarios** (escala libre). Donde este documento:
> - describe un estado **`pendiente_aprobacion`** o **`pendiente_revision`**, o una compuerta como **"Supervisor aprueba/autoriza"** obligatoria → léelo como un **gate de política `off/aviso/bloqueo`** (default apagado) que se **auto-aprueba y registra** cuando quien opera tiene el grupo. Ver **[decision-escala-libre.md](decision-escala-libre.md)**.
> - compara **identidades de usuario** (`aprobador ≠ solicitante`, `autorizador ≠ create_uid`, `usuario_revision_id ∉ {…}`) → eso está **eliminado**: es un deadlock con 1 usuario.
> - propone gates de arqueo/descuadre → antes van los arreglos de **[decision-integridad-datos.md](decision-integridad-datos.md)** (conteo ciego, inmutabilidad, autoría, retiro tipificado), sin los cuales el gate audita un número falso.
>
> El resto del catálogo (los 14 procesos, el patrón común, el mixin, el orden por olas) sigue vigente.

> **Base verificada:** addon `l10n_pe_ne_biller` **v19.0.1.6.0** (repo `odoo-facturacion-addons`, HEAD `cffb92d`) · SPA `@ne/web-bff` · Odoo 19.
> Las referencias `archivo:línea` cuelgan de `addons/l10n_pe_ne_biller/` salvo indicación; la SPA cuelga de `ne-express/apps/web-bff/src/`.
> **Lee [README.md](README.md) primero:** documenta el estado de verificación de cada afirmación (no todas están verificadas al mismo nivel).

## 0. El hueco central, en una línea

De las **90 rutas** `/ne/api`, **6 comprueban grupo** (4 de admin con `base.group_system`, `/ne/api/anular`, y login/whoami que solo lo serializan). No hay Cotizador, ni Cajero, ni Despachador, ni Supervisor. **No existe un solo campo de propiedad de negocio** (`user_id`, `responsable_id`) en cotización ni gasto, y **ningún modelo propio hereda `mail.thread`** → cero auditoría de cambios de estado.

Lo que **sí** existe, y es la plantilla a copiar literalmente:

| Pieza | Dónde |
|---|---|
| El grupo | `security/l10n_pe_ne_security.xml:34` → `group_l10n_pe_ne_anulacion` |
| El helper de permiso | `controllers/main.py:128-134` → `_puede_anular()` |
| El gate (403) | `controllers/main.py:722-727` → `POST /ne/api/anular` |
| El permiso hacia la SPA | `controllers/main.py:352` → `puedeAnular` en `whoami` |
| La migración que no despoja a los emisores vivos | `migrations/19.0.1.4.0/post-anulacion-grupo.py` |

**Ya existe el primer rol funcional del producto.** Este catálogo lo generaliza; no lo inventa.

---

## 1. REGLAS DE CONSTRUCCIÓN (aplican a los 14 procesos, no se repiten en cada ficha)

**R1 — Gate en dos capas, el MODELO es la autoridad.** `has_group` en el controller = cortesía UX (403 limpio vía `_err`). `has_group` re-chequeado dentro del método de modelo = el mecanismo real. Precedente: `main.py:376` + `res_company.py:198`. El controller nunca calcula ni construye el shape.

**R2 — BLOQUEANTE: `ir.rule` NO segrega `account.move`.** `group_l10n_pe_ne_emisor` implica `account.group_account_user` → `group_account_basic` → `group_account_invoice`, que carga `account.account_move_see_all` con `domain_force [(1,'=',1)]`. Las reglas de grupo se combinan con **OR** (`ir_rule.py:160-173`): toda regla restrictiva sobre comprobantes queda **anulada**. ⇒ Sobre `account.move` el filtro va como **dominio explícito en el método del addon** y el permiso como `has_group` en Python. Sobre **modelos propios** (cotización, caja, guía, gasto, lote) las `ir.rule` **sí funcionan** y son el mecanismo correcto para las colas.

**R3 — Grupos HERMANOS, no escalonados.** `implied_ids` es acumulativo: encadenar roles revienta la segregación. Colgar `group_ne_cotizador / _cajero / _despachador / _taller / _almacen / _supervisor` de un `res.groups.privilege` "NE Express" (Odoo 19 usa `privilege_id`, **no** `category_id`). Los grupos funcionales **no son excluyentes** (`disjoint_ids` solo aplica a grupos de tipo de usuario) → en una librería de 2 personas el mismo usuario puede ser Cajero **y** Despachador. Correcto y deseado. Un rol operativo **no debe implicar** `account.group_account_user` (le daría CRUD de facturas y le arrastraría R2).

**R4 — Ningún parámetro de negocio en TypeScript.** Tolerancia de descuadre, tope de descuento, tope de caja chica, plazo de baja: `res.company` (es multi-tenant; `ir.config_parameter` es global a la BD y no sirve por RUC). Se sirven por `GET /ne/api/config`. El antipatrón a erradicar: `AVISO_DIF = 10` en `Caja.tsx:31`.

**R5 — Permisos calculados en el addon.** Método `res.users.l10n_pe_ne_perfil()` que devuelva `{isAdmin, puedeAnular, puedeCotizar, puedeCobrar, puedeDespachar, puedeAprobar, …}` vía `has_group`; el controller **solo serializa** en `whoami`/`login`. `puedeAnular` ya inauguró la forma (`main.py:352`) — se amplía, no se reinventa.

**R6 — Auditoría gratis.** `_inherit = ['mail.thread','mail.activity.mixin']` en todo modelo con estado + `tracking=True` en `estado` y en el responsable. Coste: **cero dependencias** (`mail` ya está). Hoy no lo hereda ningún modelo propio → "quién aprobó esto" no tiene dónde vivir.

**R7 — Transiciones como métodos, nunca setters.** Prohibido repetir `l10n_pe_ne_set_estado` (`l10n_pe_ne_cotizacion.py:268-276`: solo valida pertenencia al Selection) y `l10n_pe_ne_update_cotizacion` (`:261`: `vals['estado'] = payload['estado']`, escribe el estado crudo desde el payload). Son **dos puertas abiertas**: hoy una `convertida` vuelve a `borrador` con un curl.

**R8 — Patrón de endpoint.** Dicts `_GET/_POST/_PUT/_DEL` (`main.py:59-90`): `type='http'`, `auth='public'`, `cors='*'`, `csrf=False`. Identidad por Bearer (`_identify()`, `main.py:99`), **nunca** `auth='user'`. Lectura → `self._json(...)`. Escritura → `self._run(...)` (`:209`, hace `flush()` para que las constraints afloren como 400 y no como 500 HTML). Errores → `_fail` (`:223`): `AccessError`→403, `UserError/ValidationError`→400. **Toda cola nace con `_page_args`** (`:176`).

**R9 — No hay endpoint de subida.** `_serve_file` (`:262`) solo descarga. Cualquier "adjunta la foto del voucher/sustento" implica endpoint multipart o base64 + `ir.attachment` + tope de tamaño. Es alcance, no un detalle.

**R10 — Compañía.** Todo sale de `user.company_id` (singular) en los helpers `_move/_cotizacion/_caja/_guia` (`main.py:136-171`), nunca del request. Las 11 `ir.rule` globales de compañía se combinan en **AND** con cualquier regla de rol nueva: **el aislamiento por RUC sigue aplicando pase lo que pase**.

---

## 2. HABILITADORES (H) — no son procesos, son precondición de todo el catálogo

| # | Habilitador | Por qué bloquea | Esfuerzo |
|---|---|---|---|
| **H-1** | **Mixin `l10n_pe_ne.flujo.mixin`**: `estado`, `user_id` (responsable, nullable = *en cola*), `company_id`, `priority`, `_inherit mail.thread/activity`, helper `_check_transicion(origen→destino, grupo)` | Los 14 procesos lo usan. Sin él se escriben 14 máquinas de estado sueltas y ninguna auditable | S |
| **H-2** | **Grupos hermanos + `res.groups.privilege` "NE Express"** + filas nuevas en `ir.model.access.csv` (hoy las 15 filas cuelgan del **mismo** grupo Emisor) | No hay dónde colgar ningún rol | M |
| **H-3** | **`res.users.l10n_pe_ne_perfil()`** + `whoami` lo serializa + `ProtectedRoute`/`App.tsx:89` gatean la navegación por rol | La SPA hoy pinta el menú completo a todo emisor; solo distingue `isAdmin` | S |
| **H-4** | **Alta de usuario y asignación de rol por el DUEÑO del RUC** (hoy solo `base.group_system`, que es global a la plataforma) | **Si el dueño no puede crear a su cajero, ningún proceso se puede usar en producción.** ⚠️ **NO** se implementa con grupo+ACL: `base_security.xml:141-146` hace `res_users_rule` global sin filtro de compañía (vería usuarios de otros RUC) y quien escribe `group_ids` puede auto-otorgarse `base.group_system`. Solo como métodos `.sudo()` del addon con filtro explícito por compañía y **whitelist de grupos otorgables** | M |
| **H-5** | Re-chequear `group_l10n_pe_ne_anulacion` **dentro** de `l10n_pe_ne_quick_anular` / `_l10n_pe_check_baja` | Hoy el gate solo está en el controller; la vista backend y los tests lo saltan | XS |

> H-1..H-3 + H-5 son ~1 sprint y son la **condición de arranque**. H-4 puede ir en paralelo.

---

## 3. CATÁLOGO

---

### CN-01 · Venta de mostrador con cotización: **cotiza → cobra en caja → recoge en despacho**
> *Caso 1 del usuario (librería). El más barato de los dos y el que inaugura el patrón.*

**Disparador:** el cliente pide precio en mostrador por varios ítems (lista de útiles, pedido del colegio, materiales) y no compra en el acto.

**Roles:** **Cotizador/Mostrador** · **Cajero** · **Despachador** · *(Supervisor solo si dispara CN-05)*

**Pasos**

| # | Rol | Acción |
|---|---|---|
| 1 | **Cotizador** | Arma la cotización con productos del catálogo. El addon calcula totales e IGV (`_compute_amounts`, `l10n_pe_ne_cotizacion.py:63-77`). Queda **Borrador**. |
| 2 | **Cotizador** | Entrega/envía la proforma (PDF QWeb ya existe, `l10n_pe_ne_get_pdf_b64` `:300`). Queda **Enviada**. El cliente se va con el número. |
| 3 | **Cotizador** | Si el cliente confirma, la pasa a **Aceptada** ⇒ **entra a la COLA DE COBRO del cajero**. Si el precio va bajo la política → **Pendiente de aprobación** (CN-05). |
| 4 | **Cajero** | Abre su **cola de cobro** (solo Aceptadas no convertidas, filtrada en el servidor), busca por número/cliente. **No puede editar precios.** |
| 5 | **Cajero** | Cobra y emite el comprobante (01/03) **desde la cotización**: `POST /ne/api/emitir` con `cotizacionId` → vincula y marca **Convertida** (`account_move_biller.py:2065-2071`). El addon **congela** la cotización: ni líneas ni estado se reescriben más. |
| 6 | **Cajero** | Entrega ticket/comprobante. La cotización cae a **despacho: pendiente**. |
| 7 | **Despachador** | Ve la **cola de despacho** (convertidas + no despachadas), arma los productos, entrega y marca **Entregado** con `receptor_nombre`/`receptor_doc` y hora. |
| 8 | **Supervisor** | Al cierre del día ve **pagado y no despachado** (mercadería cobrada que sigue en tienda). |

**Estados y transiciones**

- **Eje comercial** (`l10n_pe_ne.cotizacion.estado`, ya existe): `borrador → enviada → aceptada → convertida`; ramas `rechazada`, `vencida` (nueva, por `validez_dias`), `pendiente_aprobacion` (nueva, CN-05).
  - `→ aceptada`: Cotizador. `→ convertida`: **solo el addon**, al emitir (nadie lo escribe a mano).
- **Eje despacho** (**nuevo**, ortogonal — no mezclar con el comercial): `no_aplica | pendiente | entregado | anulado_despacho`.
  - `convertida` ⇒ el addon pone `despacho=pendiente`. `→ entregado`: **solo** `group_ne_despachador`, **solo** si `estado='convertida'`.

**Soporte en Odoo**

- Reusa: `l10n_pe_ne.cotizacion` completo + `comprobante_id` + `l10n_pe_ne_vincular_comprobante` (`:112`) + `quick_emit(cotizacionId)`.
- Campos nuevos en `l10n_pe_ne.cotizacion`: `user_id` (vendedor), `estado_despacho`, `despachador_id`, `fecha_entrega`, `receptor_nombre`, `receptor_doc`, + H-1.
- El stock **ya se descarga solo** al emitir (`_l10n_pe_ne_mover_stock`, `:3459`, `warehouse.lot_stock_id → customers`). ⚠️ **Asumir explícitamente:** entre el paso 5 y el 7 el kardex ya dio la salida aunque el bulto siga en el mostrador. No meter `stock.picking` (duplicaría el descuento; el addon imita al POS y no usa pickings).

**Qué falta hoy**

1. Los 3 grupos (H-2) y la lectura de rol en la SPA (H-3).
2. **Congelar la cotización** — es el corazón del caso y hoy no existe: `l10n_pe_ne_update_cotizacion` (`:244`) reescribe cabecera **y** líneas de una `convertida` sin mirar el estado ⇒ **se puede cambiar el precio después de que el cliente pagó**. Y `:261` escribe `estado` crudo desde el payload. Cerrar **ambas puertas** (R7).
3. Sin `user_id` en la cotización no hay "cada cotizador ve lo suyo" (solo `create_uid`, que es auditoría, no negocio).
4. **`l10n_pe_ne_delete_cotizacion` (`:278`) borra una Convertida** con comprobante fiscal vinculado, sin guarda → se pierde el rastro cotización→comprobante (COT-035, ya documentado como hallazgo abierto).
5. No hay eje ni cola de despacho. No hay `ir.rule` por rol (las 11 actuales solo filtran compañía).
6. `GET /ne/api/cotizaciones` (`main.py:1104`) lista todo sin filtro por estado ni por rol y con paginación opt-in a medias.

**Endpoints nuevos**

```
GET  /ne/api/cotizaciones/cola-cobro          (Cajero;      _page_args)
GET  /ne/api/despacho/cola                    (Despachador; _page_args)
POST /ne/api/despacho/<id>/entregar           {receptorNombre, receptorDoc}
POST /ne/api/cotizaciones/<id>/aceptar|rechazar|enviar   ← reemplazan a /estado (setter genérico)
DEL  /ne/api/cotizaciones/<id>/estado         ← retirar o degradar a compatibilidad
```

**Complejidad: MEDIA.** **Depende de:** H-1, H-2, H-3. **Recomendado como primer proceso: sí.**

---

### CN-02 · Servicio con adelanto: **orden de trabajo encolada → atención → saldo → entrega**
> *Caso 2 del usuario (taller). Absorbe íntegra la "cadena fiscal del anticipo": el adelanto no es un dato operativo, es un hecho tributario.*

**Disparador:** el cliente acepta la cotización de un servicio/reparación y **adelanta parte** para que el trabajo entre a la cola.

**Roles:** **Cotizador/Recepción** · **Cajero** · **Técnico** *(varios usuarios con el mismo rol comparten una cola)* · **Supervisor de taller** *(solo priorización/vencimiento)*

**Pasos**

| # | Rol | Acción |
|---|---|---|
| 1 | **Recepción** | Cotiza el servicio y pacta `fecha_pactada`. Cotización **Aceptada**. |
| 2 | **Cajero** | Cobra el adelanto **y emite el comprobante de ANTICIPO** por ese monto (SUNAT no permite recibir plata a cuenta sin comprobante). |
| 3 | **Sistema (addon)** | Crea la **orden de trabajo** vinculada a la cotización y al `account.move` del anticipo. Estado **En cola**, `user_id = NULL`, `priority` según `fecha_pactada`. Calcula el **saldo pactado** = total − anticipos. |
| 4 | **Técnico** | Ve la **cola compartida del taller** (todas las órdenes en cola del RUC, no solo las suyas), **toma** una (`user_id = él`) → **En atención**. |
| 5 | **Técnico** | Registra avance/notas (chatter) y marca **Lista para entrega**. El sistema avisa (`mail.activity` con `date_deadline = fecha_pactada`). |
| 6 | **Recepción** | Avisa al cliente que ya está. |
| 7 | **Cajero** | El cliente vuelve, **cobra el saldo y emite la factura final regularizando el anticipo**: descuento global **código 04** referenciando el comprobante de anticipo (`_l10n_pe_anticipo`, `account_move_biller.py:220-253`). |
| 8 | **Técnico/Recepción** | **Entrega** con receptor y hora → **Entregada**. **Gate duro: no se entrega con saldo pendiente** salvo autorización del Supervisor. |
| 9 | **Supervisor** | Ve la cola vencida (`fecha_pactada < hoy` sin entregar) y los **anticipos cobrados sin regularizar** (= plata cobrada por trabajo no facturado: es un pasivo). Ramas de vencimiento: reprogramar / devolver el adelanto (⇒ CN-03, nota de crédito) / retener como penalidad según política de `res.company`. |

**Estados y transiciones** (`l10n_pe_ne.pedido`, ver §4)

```
borrador → adelantada(cobrado el anticipo) → en_cola → en_atencion → lista → entregada
                                                   ↘ vencida ↘ cancelada
```
- `→ adelantada`: **solo** Cajero (y solo con el `account.move` de anticipo posteado).
- `→ en_atencion`: **solo** Técnico, y **solo desde `en_cola`** (tomar es atómico: `user_id` pasa de NULL a él).
- `→ entregada`: Técnico/Recepción, **guarda: `payment_state == 'paid'`** o autorización de Supervisor registrada.

**Soporte en Odoo**

- **Modelo nuevo `l10n_pe_ne.pedido`** (`tipo = orden_trabajo`, ver §4 — el mismo modelo sirve a CN-08): `partner_id`, `telefono` (el paso 6 lo necesita y en boleta el partner suele no tenerlo), `cotizacion_id`, `anticipo_move_id`, `saldo_move_id`, `fecha_pactada`, `user_id`, `priority`, `estado`, `line_ids`, + H-1.
- **Molde conceptual: `repair.order`** de Odoo (estado draft→confirmed→under_repair→done, `user_id`, `priority`, `_order='priority desc'`). **Copiar la forma, NO depender de `repair`** (arrastra `sale_stock` + `sale_management`).
- **NO usar `mail.activity` como motor de la cola**: `user_id` es Many2one a **un** usuario, al completarse se **borra** el registro, su `state` es un compute de fecha, y `mail_activity_rule_user` limita a `['|',('user_id','=',user.id),('create_uid','=',user.id)]` → **un técnico no vería la cola de sus compañeros**, que es exactamente el requisito. Úsalo solo como recordatorio (gratis).
- **NO usar el anticipo nativo de `sale`**: deduce con **líneas negativas**, representación que SUNAT no acepta. El addon ya lo hace bien (descuento global 04, `indDocRelacionado 2`, percepción sobre el neto ya descontado para evitar el rechazo 2797). `l10n_pe` no trae **nada** de anticipos.
- **Saldo:** ⚠️ **no** con `payment_state`/`amount_residual` de la factura de anticipo (esa nace pagada al 100%). El saldo es una relación **entre dos `account.move`** ⇒ campo computado en el pedido: `total pactado − Σ anticipos vinculados`.

**Qué falta hoy**

1. **El modelo entero.** `ls models/` = 14 archivos, ninguno de pedido/orden. La cotización tiene 5 estados y ninguno sirve (`:29-33`).
2. **🔴 No se puede emitir el comprobante de ANTICIPO.** `_l10n_pe_tipo_operacion` (`:786-794`) devuelve solo `1001`(detracción) / `2001`(percepción) / `0200`(exportación) / `0101`(venta interna). **Nunca `0104` (Venta interna – Anticipos, cat. 51).** Hoy el anticipo del paso 2 saldría marcado como venta común. **El paso 2 no existe: es alcance nuevo obligatorio.**
3. **El vínculo del anticipo es texto tecleado.** `l10n_pe_ne_anticipo_doc` es un **`fields.Char`** donde alguien escribe `F001-00000100` a mano; `_l10n_pe_check_anticipo` solo valida que no esté vacío, que el monto no supere el total y que la operación sea gravada homogénea. **No verifica que el comprobante exista, que sea del mismo cliente, ni que no se haya aplicado ya en otra factura.** Con dos actores (el cajero cobra hoy, otro factura en dos semanas) **el segundo no sabe qué tipear**. ⇒ `l10n_pe_ne_anticipo_move_id` (Many2one, `domain` por partner + `es_anticipo` + posted), `_doc` pasa a **computado `store=True`** como espejo del XML. ⚠️ **Conservar el Char como fallback**: es la única vía para regularizar un anticipo emitido fuera del sistema (migración/onboarding). Constraints: mismo partner, no anulado, **unicidad** (el mismo anticipo no se aplica dos veces), monto ≤ total del anticipo.
4. `mail` está pero ningún modelo propio lo hereda → sin chatter, "el técnico dijo que faltaba un repuesto" no tiene dónde vivir.
5. `l10n_pe_ne.cotizacion` no tiene `user_id` ni monto adelantado ni saldo.

**Endpoints nuevos**

```
POST /ne/api/pedidos                          {cotizacionId, tipo, fechaPactada, lineas}
POST /ne/api/pedidos/<id>/adelanto            {monto, medios}  → emite anticipo 0104 + estado
GET  /ne/api/pedidos/cola                     (Técnico; _page_args; ?estado=&vencidas=)
POST /ne/api/pedidos/<id>/tomar               (atómico)
POST /ne/api/pedidos/<id>/listo
POST /ne/api/pedidos/<id>/entregar            {receptorNombre, receptorDoc}
POST /ne/api/pedidos/<id>/saldo               → emite factura final regularizando el anticipo
GET  /ne/api/pedidos/anticipos-sin-regularizar (Supervisor; es un FILTRO de la misma cola, no otra cola)
POST /ne/api/pedidos/<id>/reprogramar|cancelar
```

**Complejidad: ALTA.** **Depende de:** H-1..H-3, CN-01 (reusa el congelado y la mecánica de cobro), y del **fix `0104`** — que es un ticket fiscal independiente y **prerequisito duro**.

---

### CN-03 · Reversión de venta autorizada: **baja RA/RC o nota de crédito + devolución del dinero**
> **Fusiona 4 propuestas** que eran el mismo proceso: *Devolución en mostrador*, *Reversión de venta con autorización*, *Devolución: NC y reembolso*, *Anulación autorizada con desvío a NC*.

**Disparador:** (a) error de emisión detectado después (RUC errado, monto, doble emisión); (b) el cliente vuelve con el producto y su comprobante.

**Roles:** **Solicitante** (Vendedor/Cajero, sin grupo de anulación) · **Autorizador** (`group_l10n_pe_ne_anulacion`, ya existe) · **Cajero** (paga el reembolso) · **Almacenero** (destino físico del producto)

**Pasos**

| # | Rol | Acción |
|---|---|---|
| 1 | **Solicitante** | Crea la **solicitud de reversión** sobre un comprobante: motivo comercial interno, si la mercadería regresa y su `estado_fisico` (bueno/dañado). **No ejecuta nada.** |
| 2 | **Addon** | **Propone la vía fiscal** (`_l10n_pe_check_baja` en modo *dry-run*, `:5278`): factura **01** dentro del plazo (`l10n_pe_ne_biller.baja_plazo_dias`, default 7, desde `invoice_date`) → **RA**; boleta **03** → **RC (tipEstado 3)**; NC/ND **07/08** → **RA sin plazo**; fuera de plazo o devolución parcial → **Nota de Crédito 07**. |
| 3 | **Autorizador** | Aprueba o rechaza **con comentario**. Control: el autorizador **no debe ser** el `create_uid` del comprobante ("quien emite no reversa"). ⚠️ En PyME de 2 personas, si no hay otro usuario con el grupo, permitirlo **marcado como auto-aprobada** en el reporte — no trabar la tienda. |
| 4 | **Addon** | Ejecuta: `quick_anular` (RA/RC) **o** `quick_emit` tipoDoc 07. **No inventar emisor automático en v1.** |
| 5 | **Cajero** | Devuelve el dinero **solo si está aprobado**, ligado a la solicitud. |
| 6 | **Almacenero** | Confirma el **destino** del producto ya reingresado. |
| 7 | **Dueño** | Ve el listado de reversiones: quién solicitó / quién aprobó / cuánto salió de caja. |

**🔴 Los dos defectos de caja que este proceso debe arreglar — son distintos y hay que nombrarlos por separado:**

| Vía | Qué pasa hoy | Resultado |
|---|---|---|
| **Baja (RA/RC)** | `l10n_pe_biller_state → 'anulado'` y `_l10n_pe_ne_ventas_sesion` (`l10n_pe_ne_caja.py:57-73`) filtra `not in ('rechazado','error','anulado')` ⇒ **la venta desaparece del esperado retroactivamente**, sin que nadie toque el cajón | **SOBRANTE** falso. Y si además se registra un retiro, se **resta dos veces** |
| **Nota de crédito** | La NC es `out_refund` y `ventas_sesion` solo mira `out_invoice` ⇒ el original sigue sumando al esperado aunque el efectivo salió | **FALTANTE** falso |

⇒ **El reembolso se materializa como `caja.movimiento` tipo retiro SOLO en la vía NC.** En la vía baja se registra el hecho (documento, monto, quién) **sin tocar el saldo**. ⚠️ El retiro se rechaza si supera el efectivo disponible (`l10n_pe_ne_caja.py:238-247`): una devolución grande a primera hora **no se puede pagar** ⇒ contemplar la rama "sin efectivo suficiente".

**⚠️ Correcciones a lo que la gente asume:**
- **El almacenero NO reingresa nada.** `_l10n_pe_ne_mover_stock` (`:3459`) ya repone (cliente→almacén) **en el instante de emitir la NC**, automático. El defecto real es el inverso: **repone como BUENO aunque volvió roto** ⇒ el kardex miente desde el segundo cero. Solución: `estado_fisico` capturado en el paso 1 y, si es dañado, `stock.scrap` automático (nativo, `stock` ya es dependencia) o ubicación de cuarentena. Depende de **CN-10**.
- **"Cambio por otro producto" NO es una diferencia de caja.** Es **NC 07 + comprobante NUEVO**. Registrar la diferencia como movimiento de caja = cobrar sin comprobante. Dos ramas explícitas, sin concepto "diferencia".
- **Un comprobante RECHAZADO por SUNAT no se da de baja**: nunca existió, su correlativo sigue disponible y se **reemite con el mismo número**. El addon ya lo impide (`:3459`… `"Solo puede comunicarse la baja de un comprobante ya enviado"`). No hay "correlativo inutilizado" que anular.
- El motivo de la **baja** es texto libre (`l10n_pe_ne_baja_motivo`); el catálogo **09** (01/02/06/07) es de **notas de crédito**. Son dos campos distintos.

**Estados:** `solicitada → autorizada → ejecutada → reembolsada → cerrada` · `rechazada`. **⚠️ NO meter "baja solicitada" en `l10n_pe_biller_state`**: ese Selection es el estado **ante SUNAT** y la SPA lo consume con vocabulario fijo (`lib/emision.ts`, filtros de `Comprobantes.tsx`) → romperías los badges.

**Soporte:** modelo nuevo `l10n_pe_ne.devolucion` (la autorización ocurre **antes** de que exista la NC ⇒ no puede colgar de `account.move`): `move_id`, `estado`, `motivo_interno`, `motivo_code` (cat.09, solo vía NC), `via` (ra/rc/nc), `solicitante_id`, `autorizador_id`, `nota_credito_id`, `movimiento_caja_id`, `estado_fisico`, + H-1 + `ir.rule` de compañía (copiar una de las 11).

**Qué falta:** el modelo; H-5 (`quick_anular` no re-chequea el grupo); `caja.movimiento` **no tiene ningún vínculo a `account.move`** (`:300-315`) — ese único Many2one es lo que cierra el fraude clásico; **`_l10n_pe_check_nota` no existe** (tope de la NC contra el residual del afectado); `lineasLock` (régimen de edición de líneas de NC por motivo: 01/02/06 espejo total, 07 solo reducir cantidad) **vive en `Emitir.tsx:236-245`** y el addon no la conoce; `invoice_user_id` existe y el addon ya lo imprime ("Atendido por:", `:5041`) pero **`/ne/api/emitir` no lo setea** → el reporte por vendedor del paso 7 no sale.

**Endpoints:** `POST /ne/api/devoluciones` · `GET /ne/api/devoluciones/cola` · `POST /…/<id>/autorizar|rechazar` · `POST /…/<id>/reembolsar` · `POST /…/<id>/destino-fisico` · `GET /ne/api/devoluciones/reporte`

**Complejidad: ALTA.** **Depende de:** H-1, H-2, H-5, CN-04 (el arreglo del filtro de caja), CN-10 (rama dañado).

---

### CN-04 · Cierre de turno de caja: **arqueo, descuadre y visto bueno del supervisor**
> **Fusiona 3 propuestas**: *Relevo de turno*, *Arqueo ciego y aprobación del descuadre*, *Cierre de turno con entrega al siguiente*. **Se entrega en 2 fases; la Fase B es opcional y cara.**

**Disparador:** fin de turno / fin del día / arqueo sorpresa del dueño.

**Roles:** **Cajero** · **Supervisor/Dueño**

#### FASE A — Turno secuencial (80% del valor, sin tocar el índice de BD) · **Complejidad MEDIA**

| # | Rol | Acción |
|---|---|---|
| 1 | **Supervisor** | Abre el turno fijando `saldo_inicial` y **designa al `cajero_id` responsable** (hoy no existe ese campo). Opcional: `fondo_recibido_de_id` + arrastre del contado en efectivo de la sesión anterior como sugerencia. |
| 2 | **Cajero** | Opera: cobra, registra ingresos/retiros con motivo. |
| 3 | **Cajero** | Cuenta efectivo y medios digitales y **graba su conteo**. |
| 4 | **Addon** | Calcula la diferencia (`tools/caja_arqueo.py`, puro y testeado) y **congela el snapshot** (`conteos_cierre`/`ventas_cierre`, ya existe). |
| 5 | **Addon** | Si `abs(dif) > tolerancia` (**`res.company`**, no `AVISO_DIF=10` en `Caja.tsx:31`) → sesión queda **`pendiente_revision`**, no `cerrada`. **Nunca rechaza el conteo:** el cajero declara lo que hay. |
| 6 | **Supervisor** | Aprueba con **justificación obligatoria**. Invariante duro: **`usuario_revision_id ∉ {cajero_id, usuario_cierre_id}`**, validado en Python. |
| 7 | **Dueño** | Historial de descuadres **por cajero** (el patrón a detectar: faltantes chicos y repetidos). |

**Estados:** `abierta → cerrada` | `abierta → pendiente_revision → cerrada`. ✅ El estado nuevo **no toca el índice único** (`ON (company_id) WHERE estado='abierta'`, `:41-48`): `pendiente_revision` libera el slot ⇒ el negocio **no se detiene**. Gate correcto: no designar a **ese cajero** en un turno nuevo mientras tenga un descuadre sin aprobar — no bloquear la caja de la tienda.

**⚠️ Tres correcciones a lo que se propuso:**
1. **El "arqueo ciego" no es implementable como se vendió.** `_l10n_pe_ne_sesion_dict` (`:109-134`) devuelve `esperado`/`esperadoTotal` en **cada** `GET /ne/api/caja` durante **todo** el turno; ocultarlo solo al cerrar es teatro (el cajero lo leyó a las 3pm). Y aunque lo quites, el mismo dict devuelve `saldoInicial`, `ventas.total`, `ingresos`, `retiros` y los movimientos: el esperado se **reconstruye con una suma** (`caja_arqueo.py:71-72`). Hay **4 puertas de fuga** (`_sesion_dict`, `_fila_dict`, `_arqueo_dict` rama en vivo, y el POS que muestra sus propias ventas). ⇒ Serializador reducido por rol que omita el **agregado**, y venderlo como *aumento de fricción*, **no como imposibilidad**.
2. **`usuario_cierre_id ≠ usuario_apertura_id` es el invariante equivocado**: con el supervisor abriendo se cumple solo y no impide la auto-aprobación. Hacen falta **tres** campos con semánticas distintas: `cajero_id` (responsable, nuevo), `usuario_cierre_id` (quien grabó el conteo, ya existe), `usuario_revision_id` (quien aprobó, nuevo).
3. **Falso:** "una sola caja abierta por RUC hace que dos turnos compartan sesión". El índice es **parcial** (`WHERE estado='abierta'`): impide sesiones **simultáneas**, no secuenciales. Dos turnos consecutivos ya son dos sesiones. El problema real es que **la sesión no tiene cajero responsable y el cierre no lo firma nadie**.

#### FASE B — Multi-puesto (Caja 1 / Caja 2 simultáneas) · **Complejidad ALTA · separable · si hay un solo mostrador, NO se hace**

**Precondición innegociable, y quitar el índice sin ella lo empeora:** hoy `_l10n_pe_ne_ventas_sesion` busca **todas** las `out_invoice` de la compañía en su ventana de `create_date` ⇒ con dos sesiones abiertas **ambas reclamarían las mismas ventas y el esperado se duplicaría en las dos**.
1. Añadir `l10n_pe_ne_caja_sesion_id` (Many2one, index) a `account.move`, poblado en `quick_emit` con la sesión abierta **del usuario que emite**; `_l10n_pe_ne_ventas_sesion` pasa a filtrar por ese campo, con fallback temporal para el histórico.
2. `_l10n_pe_ne_sesion_abierta` (`:191`) y `l10n_pe_ne_caja_actual` (`:200`) hacen `search(..., limit=1)` **solo por compañía** → deben filtrar por usuario, o eligen una sesión arbitraria.
3. **Recién entonces** el índice: `ON (company_id, cajero_id) WHERE estado='abierta'`. ⚠️ `CREATE UNIQUE INDEX IF NOT EXISTS` **no reemplaza** un índice existente con el mismo nombre y otra definición → hace falta `DROP INDEX` en `init()`, o el bug solo aparece en producción.
4. Decidir el vector **antes**: ¿`cajero_id` (personas) o `punto_venta_id` (terminales)?

**Endpoints:** `POST /ne/api/caja/abrir` (+`cajeroId`) · `GET /ne/api/caja/pendientes-revision` · `POST /ne/api/caja/<id>/aprobar-cierre {justificacion}` · `GET /ne/api/caja/historial?cajeroId=` · `GET /ne/api/config` (+`cajaTolerancia`)

**Depende de:** H-1..H-3. **Ventaja:** `caja.sesion` es modelo **propio** ⇒ las `ir.rule` por rol **sí funcionan** (no aplica R2).

---

### CN-05 · Aprobación comercial de la cotización: **descuento fuera de política y línea de crédito**
> **Fusiona 2 propuestas.** Es el **mismo gate sobre el mismo documento** con dos disparadores. Es **precondición de CN-01** (sin precio congelado, el handoff a caja no vale) y del paso 1 de CN-06.

**Disparador:** el vendedor cotiza por debajo del precio de lista (regateo, cliente frecuente) **o** marca la venta como **Crédito**.

**Roles:** **Vendedor/Cotizador** · **Supervisor/Dueño** · **Cajero** (cobra el importe congelado, sin poder de edición)

**Pasos:** 1) Vendedor arma la cotización con el precio de lista → 2) el **addon** compara aplicado vs lista y calcula el % por línea y global; si supera la tolerancia (`res.company.l10n_pe_ne_descuento_tolerancia_pct`, **default 0 = apagado** ⇒ retrocompatible) o si es Crédito sin aprobación → **Pendiente de aprobación**, **no convertible ni cobrable** → 3) Supervisor ve la **cola de pendientes** (filtrada en el servidor) y aprueba/rechaza con motivo, viendo `partner.credit` vs `partner.credit_limit` → 4) el addon **congela precios** → 5) Cajero cobra exactamente eso; si el cliente pide otro precio, vuelve al paso 2.

**🔴 Precondición innegociable (arreglar ANTES de comparar nada): `list_price` es ambiguo hoy.** El POS lo consume como precio **CON** IGV (`totals.ts`, `POS.tsx:222`) pero `_l10n_pe_ne_quick_product` lo **escribe SIN** IGV. **Sin esto el % de descuento es basura.** Decidir una convención (recomendado: `list_price` = precio de vitrina **con** IGV, que es lo que el dueño teclea en Mantenimientos→Productos) y corregir la escritura.

**Diseño:** `precio_lista` en `l10n_pe_ne.cotizacion.line` como **SNAPSHOT** copiado en `_l10n_pe_ne_build_lines` (`:168`) — **no** `related` ni compute contra el maestro vivo, o cambiar el catálogo **reescribe la historia**. `descuento_pct` compute store. ⚠️ **Hueco a decidir explícitamente:** las líneas **sin `product_id`** (ítem libre) no tienen lista ⇒ `descuento_pct=0` y no entran al gate. **Esa es la vía de escape obvia del vendedor**: teclear el producto como ítem libre. Mitigación: exigir producto cuando el gate esté activo.

**Rama POS — decidir y decirlo, no dejarlo ambiguo (es donde de verdad ocurre el regateo):** `quick_emit` firma y envía de una, **no hay documento donde parquear** un "pendiente de aprobación". Opción 1 (v1, recomendada): el POS **rechaza** la línea bajo tolerancia con `UserError` ("Este precio requiere aprobación: genera una cotización"). Opción 2: override de supervisor en mostrador (PIN validado en el addon) que sella `aprobador_id` en el `account.move`.

**Límite de crédito: NO inventarlo.** `res.partner.credit_limit` + `res.company.account_use_credit_limit` + `partner.credit` son nativos. Solo hay que exponerlos por `/ne/api` y decidir la política. ⚠️ Son *company-dependent* y `show_credit_limit` tiene `groups='account.group_account_invoice,account.group_account_readonly'` → si los roles nuevos no implican esa cadena (R3), hay que conceder el ACL explícito.

**Qué falta:** `precio_unitario` es un `Monetary` libre sin tope (`:262`); no existe `pendiente_aprobacion`; **las dos puertas de escritura del estado (R7)** — sin cerrarlas, un curl a `/ne/api/cotizaciones/<id>/estado {"estado":"aceptada"}` **salta la aprobación entera y el proceso es decorativo**; `update_cotizacion` debe lanzar `UserError` si `estado in ('aprobada','convertida')`.

**Fuera de alcance (es otro proceso):** *mantenimiento del maestro de precios*. No hay handoff (es CRUD de una persona) y **no se hace con ACL** (rompería `_l10n_pe_ne_quick_product`, que crea productos durante la emisión de ítems libres; y los ACL son **unión**: un supervisor+emisor recupera el write). Se hace con `has_group` **dentro** de `l10n_pe_ne_update_producto`, limitado al campo `list_price`, + `tracking=True` en `list_price` de `product.template`.

**Endpoints:** `GET /ne/api/cotizaciones/pendientes` · `POST /ne/api/cotizaciones/<id>/solicitar-aprobacion|aprobar|rechazar` · `GET /ne/api/clientes/<id>/credito`

**Complejidad: ALTA** (la mitad es el fix de `list_price` y cerrar las puertas). **Depende de:** H-1..H-3.

---

### CN-06 · Cobranza de la cartera al crédito (el fiado)
> **Fusiona 3 propuestas.** La aprobación del crédito **se fue a CN-05** (no cabe aquí: `quick_emit` es atómico — crea + postea + envía a SUNAT en una llamada; **no puede haber un comprobante fiscal esperando a un jefe**).

**Disparador:** factura con `l10n_pe_ne_forma_pago = 'Credito'` y cuotas pactadas; llega el vencimiento, o el cobrador sale a ruta.

**Roles:** **Vendedor** (emite) · **Cobrador** (calle) · **Cajero/Tesorería** (aplica y reconcilia) · **Dueño**

**Pasos:** 1) Vendedor emite al crédito con el cronograma aprobado (CN-05) → 2) el **addon** expone la **cartera** por vencimiento → 3) Supervisor asigna `l10n_pe_ne_cobrador_id` → 4) Cobrador ve **su** cartera, visita y registra el cobro con medio y sustento → 5) **Tesorería** aplica el `account.payment` reconciliado y lo amarra a la caja del día (**quien cobra en la calle NO es quien reconcilia**) → 6) `payment_state` cae a `partial`/`paid` **solo por reconciliación**, nunca por un flag → 7) Dueño ve antigüedad por cliente y por cobrador.

**🔴 Tres cosas que hay que arreglar antes, y el brief las pinta al revés:**
1. **El brief dice "no se crea la deuda". Es falso — y peligroso**: si alguien lo implementa así, construirá un modelo de deuda **paralelo** al de Odoo. La deuda existe al postear. **Lo que no se registra es el COBRO.** Verificado: `grep payment_state|amount_residual` sobre `models/` = **0 hits**. El único `account.payment` que el addon crea es el de retención/percepción (`account_payment_retencion.py:431-437`, `_l10n_pe_ne_register_payment`, wizard `account.payment.register`). **La infraestructura ya está probada; solo no se usa para cobrar una venta.**
2. **Las cuotas no son vencimientos.** Viven en un `Json` (`l10n_pe_ne_cuotas`) y **solo la última fecha** llega a `invoice_date_due` ⇒ la move tiene **una** línea a cobrar y "cuota vencida" no existe contablemente. Hay que proyectarlas a líneas de término con `date_maturity` **antes** del `action_post()` (`quick_emit`, `:1873`; el punto de inserción correcto es `_l10n_pe_ne_quick_flags`, que corre antes de postear). ⚠️ **Mantener el Json como fuente del XML** (`_l10n_pe_detalle_pago` ya está certificado) y **derivar** los términos, no al revés.
3. **Alcance obligatorio no declarado: registrar el pago del CONTADO.** Sin eso `payment_state` no discrimina nada y la cartera lista todo el histórico. El POS ya guarda `l10n_pe_ne_medios_pago` (Json) — ese es el insumo. **Y arregla de paso la caja**, que amarra por ventana de `create_date` en vez de por pago real. **Prerequisito, no anexo.**

**Filtro de cartera:** `[('move_type','=','out_invoice'), ('state','=','posted'), ('payment_state','in',['not_paid','partial']), ('l10n_pe_ne_forma_pago','=','Credito'), ('l10n_pe_biller_state','!=','anulado')]`. **Sin la última condición se cobra un documento dado de baja** (la baja no toca `payment_state`).

**⚠️ R2 en su forma más dura:** "el cobrador ve solo su cartera" **no se puede hacer con `ir.rule`** sobre `account.move`. Requiere: (i) campo `l10n_pe_ne_cobrador_id` (no existe), (ii) que el rol Cobrador **no** implique `group_account_invoice` **o** sobrescribir el `domain` de `account.account_move_see_all` desde el addon (**persiste**: está en bloque `noupdate="1"`), (iii) el gate duro en Python. Sin (i)+(ii) el paso 4 es humo.

**⚠️ "Lo entra a la caja del día" es imposible hoy:** `_l10n_pe_ne_ventas_sesion` mira `create_date` de facturas, no pagos ⇒ **un cobro de hoy contra una factura del mes pasado nunca entra a la caja de hoy**. Dato a favor: `caja_arqueo.py:36-45` **ya trata bien el crédito** ("Crédito sin medios: por cobrar, no suma a ningún medio") ⇒ la venta al crédito correctamente no infla el efectivo esperado. **El hueco es solo la entrada posterior del cobro** — y es el **mismo trabajo** que necesita CN-01. Declarar la dependencia, no resolverlo dos veces.

**Recortar de v1:** "dar de baja incobrable" (write-off; exige cuenta de incobrables por RUC).

**Endpoints:** `GET /ne/api/cartera` (`_page_args`) · `POST /ne/api/comprobantes/<id>/abono` · `POST /ne/api/comprobantes/<id>/cobrador` · `GET /ne/api/cartera/antiguedad`. Añadir `payment_state`, `amount_residual` y cuotas al detalle (`l10n_pe_ne_comprobante_detalle` hoy devuelve `formaPago` **y nada más**).

**Complejidad: ALTA.** **Depende de:** CN-05 (aprobación), registro del pago al contado, R2.

---

### CN-07 · Egreso con sustento y aprobación (gasto / caja chica)
> **Fusiona 2 propuestas.**

**Disparador:** hay que pagar el mototaxi, la recarga, los útiles, al técnico externo.

**Roles (solo 2 reales):** **Solicitante** · **Aprobador**. *(El "Cajero" no necesita grupo: paga quien tiene la caja abierta. El "Contador" es lectura, no rol.)*

**Pasos:** 1) Solicitante registra el egreso con descripción, monto, medio y sustento → 2) bajo el tope de caja chica (`res.company`) queda aprobado automático; sobre el tope → **pendiente_aprobacion** → 3) Aprobador ve la cola y aprueba/rechaza → 4) el egreso en **efectivo** genera su `caja.movimiento` tipo **retiro** (reusando `l10n_pe_ne_caja_movimiento` para **heredar la guarda de disponible**, `:238-247`: si no alcanza el efectivo, el pago se rechaza — que es la respuesta correcta) → 5) **nadie borra un gasto aprobado**: se anula con motivo → 6) el dueño ve el acumulado por usuario y motivo.

**⚠️ Decisión de diseño central — invertir el paso 2:** **descontar SIEMPRE al registrar** (el efectivo ya salió) y que la aprobación sea auditoría **a posteriori** del sustento (`registrado → aprobado | observado`; un `observado` **no revierte** el efectivo, escala al dueño). Si de verdad se quiere compuerta previa, entonces es una *solicitud de egreso* (el cajero **no paga** hasta aprobar) — otro proceso, que solo sirve para el proveedor grande, no para el mototaxi. **Elegir uno**: mezclarlos produce faltantes falsos.

**Qué falta:** `l10n_pe_ne.gasto` es **6 campos** (`fecha, descripcion, cuenta, monto, currency_id, company_id`, `:18-26`): **sin `user_id`, sin estado, sin aprobador, sin sustento, sin `sesion_id`**. `l10n_pe_ne_delete_gasto` (`:99`) hace **unlink directo sin guarda** y el ACL da `perm_unlink=1` a todo emisor ⇒ **se registra un gasto falso de S/5,000 para tapar un hueco de efectivo y después se borra la evidencia**. El gasto en efectivo **no toca la caja** (`_l10n_pe_ne_ingresos_retiros` `:92` solo suma `caja.movimiento`) ⇒ el descuadre reaparece en el cierre del cajero, **que paga el pato por plata que sacó otro**.

**Bugs que hay que cerrar junto (independientes del proceso):**
- `l10n_pe_ne_create_gasto` (`:66`) solo valida que la descripción no esté vacía: **acepta monto 0 y negativos** → falsea la utilidad neta.
- `l10n_pe_ne_total_gastos` (`:60`) llama a `list_gastos` con `limit=100000` y suma **todo**. Al añadir estado hay que filtrar por `aprobado` **ahí mismo** y en el dominio de `list_gastos`, o el dashboard sigue contando pendientes/rechazados y **el proceso no cambia la única cifra de gestión que el dueño mira**.
- Bajar `perm_unlink` a 0 y hacer el unlink por método con override de `unlink()` en el modelo (la autoridad; el método público es cortesía).

**Amputar la mitad tributaria:** la regla *"sobre cierto monto exige RUC para deducir"* es **ficticia**. En su lugar: (a) `deducible` boolean + `compra_id` Many2one opcional a `account.move` (`in_invoice`) — si tiene sustento deducible se registra como **Compra** (que ya existe y ya va al PLE 8.1) y el gasto solo la referencia; **no reimplementar deducibilidad dentro del gasto**. (b) Si se quiere una regla de monto real y peruana, que sea **bancarización** (≥ S/2,000 ⇒ el medio no puede ser Efectivo, Ley 28194), con el umbral parametrizado.

**Adjunto: posponer (R9).** `compra_id` da mejor sustento que una foto.

**Riesgo de migración a documentar:** los retiros que hoy la gente registra a mano para gastos **doble-contarán** cuando el retiro se genere solo.

**Endpoints:** `GET /ne/api/gastos/pendientes` · `POST /ne/api/gastos/<id>/aprobar|rechazar` · `POST /ne/api/gastos/<id>/anular {motivo}`

**Complejidad: MEDIA.** **Depende de:** H-1..H-3.

---

### CN-08 · Encargo / pedido especial / apartado (layaway)
> **Mismo modelo que CN-02** (`l10n_pe_ne.pedido`, `tipo='encargo'|'apartado'`). Proceso distinto: **la cola la atiende el Comprador contra un proveedor**, no un técnico, y termina **reservando stock físico**.

**Disparador:** el cliente pide algo que no hay (el título que no llegó, la broca de esa medida, la marca del remedio).

**Roles (3+1):** **Mostrador** (toma el encargo, avisa) · **Cajero** (adelanto y saldo) · **Compras/Almacén** (consolida, compra, recibe y aparta) · **Supervisor** (solo vencimiento/anulación: reusar `group_l10n_pe_ne_anulacion`, **ya existe**).

**Pasos:** 1) Mostrador verifica que no hay existencias y toma el encargo con celular del cliente → 2) Cajero cobra el adelanto (**factura/boleta de anticipo real**, mismo mecanismo que CN-02) → **cola de encargos** con saldo → 3) Compras ve la cola, **consolida por proveedor** y compra (engancha con **CN-09**) → 4) Almacén, al recibir, **aparta la unidad** → 5) Mostrador avisa → 6) Cajero cobra el saldo y entrega. Si no vuelve: **vence**.

**La reserva (paso 4) es la parte de diseño real, y es implementable con el motor actual:** crear una ubicación interna por compañía ("Existencias/Encargos") y mover la unidad `lot_stock_id → Encargos` con `_l10n_pe_ne_stock_aplicar` (`:3540`, ya acepta origen/destino arbitrarios). Así `qty_available` de la ubicación de venta baja y **mostrador no la puede vender por descuido**, sin tocar el "nunca bloquear la venta". La entrega mueve `Encargos → customers`. **❌ A evitar:** dejar un `picking` en `assigned` — el addon nunca sostiene reservas (fuerza `quantity + picked + _action_done`) y chocaría con la política explícita de permitir negativos.

**⚠️ El paso 6 está mal planteado en la propuesta original:** si el adelanto ya se facturó, **no hay decisión discrecional**. Devolverlo exige **nota de crédito** (⇒ CN-03, con sus guardas: plazo, tipo, boleta > S/700 requiere DNI). Tres ramas, cada una un método distinto, y la política de retención en `res.company`: (a) reprogramar, (b) liberar la unidad + NC de devolución, (c) liberar + retener como penalidad (**que no es una NC**: es venta consumada; y la penalidad **no se emite como ND sobre el anticipo sin sustento**).

**Fuera de alcance:** "el quiebre de stock como información accionable" (detección automática de reposición/punto de pedido) es **otro proceso**. Aquí basta con que el encargo se cree.

**Qué falta:** el modelo (compartido con CN-02); la ubicación de reserva; `l10n_pe_ne_anticipo_move_id` (CN-02); la cola.

**Endpoints:** los de CN-02 + `GET /ne/api/pedidos/cola-compras?agrupar=proveedor` · `POST /ne/api/pedidos/<id>/apartar` · `POST /ne/api/pedidos/<id>/vencer`

**Complejidad: ALTA** (baja a **MEDIA** si CN-02 ya está: comparte modelo, estados, mixin y endpoints).

---

### CN-09 · Recepción de mercadería del proveedor y alta al kardex

**Disparador:** llega el camión/proveedor con su factura o guía.

**Roles:** **Almacenero** · **Comprador/Dueño**

**Pasos (4, recortados):** 1) Almacenero recibe, cuenta bultos contra la guía y captura **lote + vencimiento** → 2) si hay discrepancia la recepción queda **Observada** y no se da por conforme → 3) Comprador registra la factura del proveedor vinculada a la recepción conforme → 4) si hubo faltante, se registra la **nota de crédito del proveedor (`in_refund`)** que **resta del kardex**.

**🔴 El cambio estructural (sin esto los roles son cosméticos):** hoy `l10n_pe_ne_create_compra` (`:4607`) hace `create + action_post + _l10n_pe_ne_mover_stock_compra()` **en una sola llamada** ⇒ **quien teclea la factura es, por construcción, quien da fe de que la mercadería llegó y llegó completa.** En una ferretería el que recibe (almacén) y el que factura (oficina) son personas distintas y en momentos distintos, **y ahí nacen los faltantes que nadie reclama**. ⇒ **Mover la propiedad del movimiento de stock de la factura a la recepción**: la recepción es quien llama `_l10n_pe_ne_stock_aplicar(..., con_lote=True)` y quien captura lote/vencimiento; `create_compra` acepta `recepcion_id` y **no mueve stock**.

**Retrocompat obligatoria:** interruptor en **`res.company`** (no `ir.config_parameter`: es multi-tenant). Apagado = comportamiento actual ⇒ **la bodega de una persona no queda bloqueada**.

**🐛 Bug que hay que arreglar antes o como parte:** `l10n_pe_ne_update_compra` (`:4659`) reemplaza `invoice_line_ids` por `[(5,0,0)]` + **una línea = total** ⇒ **al editar una compra detallada se borra el detalle por producto y el stock ya ingresado NO se revierte: el kardex queda en falso.** Opciones: revertir con `_l10n_pe_ne_revertir_stock` (`:3670`, ya existe y es idempotente) o **prohibir** editar una compra con movimientos validados, forzando eliminar+registrar — que es lo que la doc ya dice del flujo (CMP-006).

**Recortado (no son pasos):** "ubica el producto en la estantería" — la ubicación física **no es modelable hoy** (todo el kardex trabaja contra un único `wh.lot_stock_id`, `search(stock.warehouse, limit=1)`; no hay multi-ubicación por la API). Deja solo "el almacenero fija el precio de venta" = *permiso*, no paso. "Programa el pago al proveedor" = **otro proceso** (cuentas por pagar; ya hay infraestructura parcial: el comprobante **20 Retención** ES el pago a proveedor con retención).

**Alcance nuevo obligatorio si se queda el paso 4:** `create_compra` solo crea `in_invoice` y `_l10n_pe_ne_mover_stock_compra` (`:3634`) **ignora `in_refund`**.

**Modelo nuevo:** `l10n_pe_ne.recepcion` + `.linea`, estados `borrador → contada → observada → conforme → facturada`, + H-1 + `ir.rule` de compañía.

**Endpoints:** `POST /ne/api/recepciones` · `GET /ne/api/recepciones` · `POST /…/<id>/conformar|observar` · `POST /ne/api/compras` (+`recepcionId`)

**Complejidad: ALTA** — modelo nuevo + reubicar la autoría del kardex + 2 grupos + retrocompat + el bug de `update_compra`. **Es el proceso más caro del catálogo.** No lo subestimes.

---

### CN-10 · Baja de existencias autorizada (merma, desmedro y vencimientos)

**Disparador:** revisión mensual de estantería, lote con vencimiento corto, producto roto/robado detectado en el conteo.

**Roles:** **Almacenero** · **Supervisor**

**Pasos:** 1) Almacenero consulta **lotes por vencer (30/60 días)** y existencias (incluidos los **negativos**) → 2) propone la baja (`vencimiento | desmedro | merma_normal | rotura | faltante`) → 3) **Supervisor autoriza** (constraint: `aprobador_id != solicitante_id` — sin eso el handoff es cosmético y colapsa a un CRUD de una persona) → 4) Almacenero **aplica** solo si está autorizado, reusando `_l10n_pe_ne_stock_aplicar` (`:3540`) hacia scrap/inventario — **no escribir `stock.quant` a mano** (además `stock.quant` **no tiene fila ACL**) → 5) se crea el `l10n_pe_ne.gasto` vinculado.

**La infraestructura ya está toda puesta y el dato se pierde:** `depends` incluye **`stock` + `product_expiry`**; la compra crea el lote con `expiration_date` (`_l10n_pe_ne_lote_de`, `:3514`); la salida despacha **FEFO** (⚠️ *heredado de Odoo*, no implementado por el addon — no venderlo como capacidad propia); el catálogo ya expone `rastreo` y `vence`. Pero **no hay una sola ruta que liste lotes por vencer ni que ajuste existencias**. ⚠️ *Nota para que nadie cierre esto por confusión de nombres:* `/ne/api/lotes` (`main.py:1302-1373`) **es la emisión masiva** (`l10n_pe_ne.lote`), **no** `stock.lot`.

**Y como la venta nunca bloquea por stock** (`_l10n_pe_ne_mover_stock`: *"NUNCA bloquea la venta: si no hay existencias el movimiento igual se hace y el stock queda negativo… un negativo es una señal visible de que falta un ajuste"*), **el sistema acumula negativos que solo un ajuste aprobado puede limpiar.**

**⚠️ Corrección tributaria obligatoria:** separar **merma** de **desmedro** en el Selection y **no rotular el registro como gasto deducible**. El desmedro exige destrucción ante notario/juez de paz con aviso a SUNAT (6 días hábiles antes) e informe técnico; la merma exige informe de profesional independiente. Campos `sustento_ref` + `sustento_fecha`, y el reporte dice **"sustento pendiente"** mientras estén vacíos. El addon está a salvo porque su gasto es **informativo** (alimenta el dashboard, no asienta), **pero la UI no puede insinuar deducción**.

**Fuera de v1:** el **canje al proveedor** (3 actores + NC de compra) y el **tope de descuento** (es CN-05).

**Modelo nuevo:** `l10n_pe_ne.ajuste_inventario` (`product_id`, `lot_id`, `cantidad`, `motivo`, estado `borrador→propuesto→autorizado→aplicado→rechazado`, `gasto_id`) + H-1. Añadir a `l10n_pe_ne.gasto` las dos columnas que le faltan para representar merma: `product_id`, `cantidad`, + `origen_modelo`/`origen_id`.

**Endpoints:** `GET /ne/api/inventario/vencimientos?dias=30|60` · `GET /ne/api/inventario/existencias` · `POST /ne/api/inventario/ajustes` · `POST /…/<id>/autorizar|aplicar|rechazar`

**Complejidad: ALTA.** **Depende de:** H-1..H-3, CN-07 (el gasto con estado).

---

### CN-11 · Reparto a domicilio con guía de remisión (delivery)

**Disparador:** el cliente compra en tienda o por WhatsApp y pide que se lo lleven.

**Roles (3, no 4):** **Cajero** (emite) · **Despachador** (arma + asigna + emite GRE) · **Repartidor** (entrega/no entrega). *(El "Supervisor de reparto" y el "Despachador" ejecutaban el mismo paso; el supervisor solo se justifica para autorizar la devolución del dinero, y ahí el rol correcto ya existe: `group_l10n_pe_ne_anulacion`.)*

**Pasos:** 1) Cajero cobra, emite y **marca para reparto con la dirección** → 2) Despachador arma los bultos → **cola de reparto** → 3) Despachador asigna repartidor y **emite la GRE** (placa, conductor, licencia) → 4) Repartidor sale con la GRE (**el QR es lo que sustenta el traslado ante un control**) → 5) Repartidor confirma **ENTREGA** (quién recibió, hora) o **NO ENTREGA** con motivo → 6) los no entregados vuelven a cola o disparan devolución.

**La GRE completa YA EXISTE** (`l10n_pe_ne_guia_remision.py`: `num_placa`, conductor con licencia, MTC, ubigeos, `comprobante_id`, QR, 10 rutas, prefill desde el comprobante en `main.py:1223`). **Es un DOCUMENTO, no un proceso**: no hay cola, no hay a quién está asignada, y **no hay "entregado"** — el estado de la guía solo espeja lo que dijo SUNAT.

**🔴 El eje de estado está mal ubicado — son dos máquinas ortogonales.** **NO extender** el Selection de la guía (`:65-72`, `borrador/en_proceso/enviado/rechazado/error/anulado`) con `asignada`/`entregada`: romperías el contrato *"estado espeja al del comprobante"* que la SPA reusa para pintar badges, y mezclarías **"SUNAT lo aceptó"** con **"el cliente lo recibió"** (una guía aceptada puede no entregarse). Van en campos separados: `estado_reparto`, `repartidor_id` (nullable = en cola), `fecha_entrega`, `receptor_nombre/doc`, `motivo_no_entrega`.

**⚠️ Tres trampas:**
- **El paso 2 no es un `stock.picking`.** La emisión ya descargó stock directo. Meter un picking = **el kardex descuenta DOS VECES**. "Listo para reparto" es un estado propio.
- **Un reintento NO reusa la GRE** (sería SUNAT-inválido): cambian `fecha_inicio_traslado`, placa y conductor ⇒ **GRE nueva**; la original se cierra con motivo. Y falta la pata que hoy no existe: **la baja de la GRE original ante SUNAT** — el estado `'anulado'` está declarado pero **ningún código lo escribe**. Es un TODO, no una función.
- **Precondición dura que se suele pasar por alto:** la guía exige `ubigeo_llegada` + `dir_llegada` **required**, y el partner de una bodega normalmente **no tiene ubigeo cargado**. Si no se resuelve la captura del ubigeo **en el momento del cobro**, el paso 3 se traba y el cajero termina llamando al cliente.
- **El COD (pago contra entrega) es otro proceso y hoy es contradictorio con el paso 1**: si el cajero ya emitió y cobró, no hay "contra entrega". Recortar a **entrega/no entrega + custodia**.

**Complejidad: MEDIA** (el documento pesado ya está hecho). **Depende de:** H-1..H-3.

---

### CN-12 · Depósito del efectivo del día en el banco

**Disparador:** se supera un tope de efectivo en caja **o** se prepara el cierre. ⚠️ **No es "después de cerrar"**: el depósito se registra **con la caja abierta** (la plata ya no está en el cajón).

**Roles:** **Cajero** · **Supervisor** (autoriza sobre umbral) · **Dueño/Contador** (concilia). *En PyME de 2 personas los dos últimos colapsan en el dueño; el handoff irreductible es **cajero → quien confirma el abono**.*

**Pasos:** 1) el addon **sugiere** el disponible en efectivo (⚠️ no "determina": el esperado del arqueo es **neto de retiros**; el monto lo decide el cajero acotado por el disponible que ya calcula `:235-247`) → 2) Cajero registra el retiro **tipificado** con destino (banco/cuenta) → 3) Supervisor autoriza sobre umbral → 4) quien lleva el dinero deposita y **carga el voucher** (nº de operación) → 5) sin constancia en N horas, **escala** (`ir.cron`, replicando el patrón ya probado de `data/l10n_pe_ne_cron.xml`, `state='code'`) → 6) **conciliación: `monto_voucher` vs `monto_retiro`** ⇒ faltante de traslado, imputado al `usuario_id` del retiro.

**⚠️ La conciliación es voucher vs MONTO DEL RETIRO, no vs el esperado del arqueo.** Es un control **distinto y adicional** al descuadre de conteo (CN-04).

**Hoy:** `caja.movimiento` (`:300-315`) es `tipo/motivo(Char)/monto/usuario_id` — **"depósito BCP" y "me llevé plata" son el mismo registro**. Sin destino, sin voucher, sin autorizador, sin estado. La única regla real es que un retiro no supere el efectivo disponible (buen detalle, se conserva).

**Diseño:** **extender `caja.movimiento`**, no crear modelo paralelo (así hereda gratis el tope y la deducción del esperado): `destino_tipo` (`deposito_banco/gasto/otro`), `banco_cuenta`, `estado` (`pendiente/depositado/conciliado/observado`), `autorizador_id`, `voucher` (`ir.attachment`), `nro_operacion`, `monto_voucher`, `responsable_traslado_id`. Migración: los existentes quedan `destino_tipo='otro'`.

**Requiere R9** (endpoint de subida) — **prerequisito, no un detalle**.

**Complejidad: ALTA** (por R9). **Depende de:** CN-04, R9.

---

### CN-13 · Seguimiento del depósito de detracción (SPOT)

**Disparador:** factura sujeta al SPOT. El cliente **no paga el 100%**: deposita el % en el Banco de la Nación.

**Roles (2):** **Emisor** (marca la detracción al emitir) → **Tesorería** (`group_ne_tesoreria`, dueño del ciclo posterior). *(El "Cajero" no es actor: es un fix de cálculo. El "Contador externo" no es usuario: su salida es un reporte.)*

**Pasos:** 1) Emisor marca la factura (código + tasa; el XML sale con la cuenta del BN) → 2) el cliente deposita y remite la constancia → 3) **Tesorería registra la constancia** (nº + fecha) → **depositada**; si no llega al vencimiento, entra a la **cola de pendientes** → 4) Tesorería reclama → 5) reporte de la cola + total detraído del periodo.

**🔴 Primero el cálculo, en Odoo, antes de inventar el proceso** (si no, se construye la cola sobre un neto que el navegador calcula distinto que el XML): añadir `_l10n_pe_neto_cobrar_cliente() = _l10n_pe_importe_cobrar() − _l10n_pe_detraccion_monto()` y exponerlo; decidir **una** regla de redondeo (SUNAT admite el monto detraído redondeado al entero en el depósito; el XML lleva 2 decimales) y **borrar `calcNeto` de `emitirSchema.ts:80-93`**. Revisar además `_l10n_pe_dato_pago`, que declara `mtoNetoPendientePago = amount_total` para contado con detracción.

**⚠️ Sé honesto con el "descuadre diario":** el *esperado por medio* del arqueo se construye desde `v['medios']`, **no** desde `v['total']` ⇒ **si el cajero declara los medios con el neto cobrado, el arqueo cuadra: no hay falso faltante.** El descuadre solo aparece por el **fallback** `elif forma == "Contado"` sin medios → todo el total a Efectivo (`caja_arqueo.py:42-45`), y eso ocurre porque **`Emitir.tsx` no envía `medios`** (grep = 0 hits). Ese bug **no es específico de detracción**: infla igual con anticipo aplicado y con bienes gratuitos. **Es un fix de una línea de inferencia, no un proceso** — sepáralo y priorízalo.

**Modelar el obligado, no asumir al cliente:** `l10n_pe_ne_detraccion_obligado` (`adquirente | proveedor`). Si el obligado somos nosotros, Tesorería **deposita** (no reclama) y el vencimiento es el plazo legal, no "N días". **La cola se parte en dos por ese campo.**

**Campos nuevos:** `_constancia` (Char), `_constancia_fecha`, `_detraccion_estado` (`pendiente/depositada/no_aplica`), `_detraccion_vencimiento` (calculado), `_detraccion_monto` como **store** (hoy es método → no se puede filtrar ni sumar la cola). `account.move` **ya hereda `mail.thread`** ⇒ `tracking=True` da auditoría gratis y una `mail.activity` con `date_deadline = vencimiento` cubre el recordatorio **sin cron propio**.

**No prometer:** conciliar el saldo de la cuenta del BN (exige extracto bancario; el addon **no usa `account.bank.statement`** en absoluto).

**Complejidad: MEDIA.** **R2 aplica:** el filtro de la cola es un **dominio del método**, no una record rule.

---

### CN-14 · Cierre de periodo y entrega de libros al contador (PLE 14.1 / 8.1 / 12.1 y RVIE)

**Disparador:** fin de mes + cronograma SUNAT (último dígito del RUC). Hoy lo dispara el **WhatsApp del contador**.

**Roles:** **Responsable de cumplimiento** (dueño/administrativo) · **Contador externo** (solo lectura + descarga) · **Supervisor** (reapertura)

**Pasos (4):** 1) el addon verifica que **no queden comprobantes del periodo en `error`/`en_proceso`/`por_enviar`** (es un *check*, no un estado) → 2) **Responsable cierra el periodo** → 3) **Contador externo descarga** los libros, valida contra el RVIE propuesto y presenta → 4) **Supervisor autoriza una reapertura excepcional**, registrada.

**Por qué importa:** el contador externo **es un actor real en toda PyME peruana y hoy no existe en el sistema**: para bajar el PLE usa la credencial del dueño (rol Emisor completo) ⇒ **puede emitir, anular y borrar clientes**. Y no hay noción de periodo cerrado: tras presentar el PLE, cualquier emisor sigue emitiendo con fecha de ese periodo y **el libro presentado deja de cuadrar con la base** (art. 175 CT).

**✅ NO crear un modelo de periodo propio — usar lo nativo de `account`:**
- **Cerrar** = `res.company.fiscalyear_lock_date` (+ `tax_lock_date`). El bloqueo lo aplica **gratis** `account_move._check_fiscal_lock_dates()` al postear. Para "ni con autorización" existe `hard_lock_date`.
- **Reabrir** = `account.lock.exception` (`user_id` + `reason` + `end_datetime`) = **exactamente el registro de auditoría "quién, cuándo, por qué, hasta cuándo"** que pide el paso 4.
- **Lo único propio:** `l10n_pe_ne_cerrar_periodo(periodo)` / `l10n_pe_ne_reabrir_periodo(periodo, motivo, hasta)` que traduzcan `YYYYMM`→fecha, exijan el grupo **en el modelo** y llamen a lo nativo. La regla arquitectónica se respeta: la lógica queda en el addon.
- ⚠️ Verificar que el bloqueo cubra **la vía SUNAT**, no solo la contable: emitir a SUNAT con fecha de periodo cerrado es el daño real.

**Rol Contador externo:** grupo **hermano**, **no puede** implicar `group_l10n_pe_ne_emisor` ni `account.group_account_user` (R2 lo anularía). ACLs propios de solo lectura, **nada de unlink**. ⚠️ Efecto colateral: `l10n_pe_ne_list_tenants` identifica emisores por `('group_ids','in',grp_emisor.id)` (`res_company.py:260`) ⇒ **el contador (y todo rol que no implique Emisor) será invisible en la pantalla de admin** salvo que se amplíe ese dominio.

**🐛 Bug fiscal a corregir por separado y de inmediato (es dependencia, no alcance):** el filtro del periodo (`account_move_biller.py`, dominio del PLE) excluye `por_enviar` y `rechazado` pero **deja pasar `error` y `en_proceso`** ⇒ **un comprobante que nunca llegó a SUNAT se reporta igual en el Registro de Ventas.** Cambiar a **allowlist** (`in ('enviado','anulado')`), decidiendo explícitamente qué hacer con `anulado` (una baja aceptada **sí** va al 14.1 con su estado, no se omite).

**Alcance del 12.1:** ya está implementado sobre `stock.move.line`, pero el propio código lo marca *"⚠ Estructura pendiente de validación contable"*. **No prometerlo** como entregable del cierre; el proceso debe funcionar con 14.1/8.1/RVIE.

**Gate:** los 4 endpoints de libros (`main.py:574/586/600/613`) **hoy no piden nada**.

**Endpoints:** `GET /ne/api/periodos/<YYYYMM>/listo` · `POST /ne/api/periodos/<YYYYMM>/cerrar|reabrir` + gate en los 4 de reportes.

**Complejidad: MEDIA** (casi todo es nativo). **Depende de:** H-2, H-3.

---

## 4. EL PATRÓN COMÚN — y la decisión de modelo

Los 14 procesos son **la misma figura**:

> **DOCUMENTO** con `estado` + **RESPONSABLE** (`user_id`, y *nullable* = "en cola") + **HANDOFF** (un rol lo empuja al siguiente y **solo ese rol** puede hacer esa transición) + **COLA** filtrada en el servidor + **AUDITORÍA** (quién, cuándo, por qué) + **PARÁMETRO** de política por RUC (tolerancia/tope/plazo).

| Proceso | Documento | Cola de | Handoff nuclear |
|---|---|---|---|
| CN-01 | cotización | cobro / despacho | pagó ⇒ habilita al despachador |
| CN-02 / CN-08 | pedido | taller / compras | adelantó ⇒ entra a la cola |
| CN-03 | devolución | autorización | aprobó ⇒ habilita el reembolso |
| CN-04 | caja.sesion | revisión | descuadró ⇒ escala al supervisor |
| CN-05 | cotización | aprobación | aprobó ⇒ es cobrable |
| CN-06 | account.move | cartera | cobró en calle ⇒ tesorería reconcilia |
| CN-07 | gasto | aprobación | registró ⇒ aprueba otro |
| CN-09 | recepción | conformidad | conformó ⇒ se puede facturar |
| CN-10 | ajuste | autorización | autorizó ⇒ se aplica |
| CN-11 | guía | reparto | armó ⇒ el repartidor sale |
| CN-12 | movimiento | conciliación | depositó ⇒ se concilia |
| CN-13 | account.move | detracciones | depositó ⇒ se libera |
| CN-14 | periodo | — | cerró ⇒ el contador descarga |

### Recomendación: **UN MIXIN, NO un modelo genérico** — con una excepción

**❌ NO hacer un `l10n_pe_ne.documento` genérico con `tipo`.** Razones concretas, no estéticas:
1. Los documentos tienen **formas legales genuinamente distintas**: la cotización tiene `validez_dias` e IGV; la recepción tiene lote y vencimiento; la devolución tiene motivo cat.09 y vía RA/RC/NC; el ajuste tiene `product_id` + `lot_id`. Un modelo único **colapsa en una bolsa de campos nullable** y las `@api.constrains` se vuelven `if tipo == …` en cascada.
2. **Pierdes la granularidad de ACL e `ir.rule` por documento**, que es justo el mecanismo que hace que esto funcione (`ir.model.access` es **por modelo**).
3. Odoo nativo **no lo hace así**: `sale.order`, `repair.order`, `stock.picking` y `account.move` son modelos distintos que comparten `mail.thread`. Ese es el precedente.

**✅ Hacer `l10n_pe_ne.flujo.mixin` (`_name`, `AbstractModel`):**
```python
estado          Selection  (cada modelo define el suyo)
user_id         Many2one res.users   # responsable; NULL = "en cola"
company_id      Many2one res.company
priority        Selection
_inherit = ['mail.thread', 'mail.activity.mixin']   # coste CERO: 'mail' ya es dependencia
def _check_transicion(origen, destino, grupo)  # valida transición + has_group EN EL MODELO
def _cola(dominio, page_args)                  # cola paginada, filtrada en servidor
```
Y una **`ir.rule` NO global con `groups`** por modelo (se OR-ean entre sí, se AND-ean con las 11 globales de compañía ⇒ **el RUC sigue aislado siempre**).

**Excepción — un modelo con `tipo`, y solo uno:** `l10n_pe_ne.pedido` con `tipo = orden_trabajo | encargo | apartado` (CN-02 + CN-08 + layaway). Ahí **sí** es el mismo documento: mismo ciclo (adelanto → cola → aviso → saldo → entrega), mismos campos, mismos estados. Lo único que cambia es **quién atiende la cola** (técnico vs comprador). Tres modelos separados serían copy-paste.

**Beneficio colateral gratis:** hoy **ningún** modelo propio hereda `mail.thread` ⇒ no hay auditoría de cambios de estado en cotización, caja ni gasto. `account.move` **sí** la tiene (`l10n_pe_biller_state` con `tracking=True`, `message_post` en el envío y el email) — o sea, **el documento fiscal ya es auditable y los operativos no.** El mixin lo cierra de una.

---

## 5. ORDEN DE IMPLEMENTACIÓN

**Criterio:** valor de control (¿por dónde se va la plata?) ÷ esfuerzo × dependencias. Se ordena por **olas**; dentro de la ola, por valor.

### 🔧 OLA 0 — Habilitadores y bugs de una línea *(~1 sprint; sin esto nada de lo demás es implementable)*
| | Qué | Por qué ahora |
|---|---|---|
| 1 | **H-1** mixin de flujo | Lo usan los 14 |
| 2 | **H-2** grupos hermanos + privilege + ACL | No hay dónde colgar un rol |
| 3 | **H-3** `l10n_pe_ne_perfil()` + `whoami` + navegación por rol | `puedeAnular` ya inauguró la forma; se amplía |
| 4 | **H-5** re-chequear el grupo de anulación **en el modelo** | XS, y hoy la única puerta cerrada del producto tiene la llave puesta en el controller |
| 5 | **Cerrar las 2 puertas del estado de cotización** (R7) | **Precondición de CN-01 y CN-05.** Sin esto ambos son decorativos |
| 6 | **Bugs sueltos**: `list_price` con/sin IGV · `Emitir.tsx` no envía `medios` · `create_gasto` acepta monto ≤ 0 · `update_compra` destruye el detalle sin revertir stock · el PLE deja pasar `error`/`en_proceso` | Baratos, y varios procesos se **apoyan** en ellos. El de `list_price` bloquea CN-05 entero |
| 7 | **H-4** alta de usuario/rol por el dueño | En paralelo. **Sin esto los roles no se pueden usar en producción** |

### 🥇 OLA 1 — Lo que el usuario pidió + el control de efectivo *(el patrón queda probado)*
| | Proceso | Complejidad | Nota |
|---|---|---|---|
| 8 | **CN-01** mostrador: cotiza → caja → despacho | MEDIA | **Empezar por aquí.** Es el handoff más barato, el 60% ya existe (`cotizacionId` vincula, stock se mueve solo, PDF hecho) y valida el mixin |
| 9 | **CN-04 Fase A** cierre de turno con visto bueno | MEDIA | Independiente. **80% del valor de control interno** sin tocar el índice de BD |

### 🥈 OLA 2 — El segundo caso del usuario + el gate que lo sostiene
| | Proceso | Complejidad | Nota |
|---|---|---|---|
| 10 | **CN-05** aprobación comercial (descuento + crédito) | ALTA | **Precondición real de CN-01**: sin precio congelado el handoff a caja no vale. Entregar con tolerancia **0 = apagado** |
| 11 | **CN-02** orden de trabajo con adelanto | ALTA | Requiere el **fix `0104`** (ticket fiscal previo) + el Many2one del anticipo |

### 🥉 OLA 3 — Donde se va la plata
| | Proceso | Complejidad | Nota |
|---|---|---|---|
| 12 | **CN-03** reversión autorizada | ALTA | **Máximo valor de control** (es el fraude clásico de mostrador) pero necesita CN-04 primero: arregla los 2 defectos de arqueo |
| 13 | **CN-07** egreso con sustento | MEDIA | El gasto es la **puerta trasera**: hoy se crea y **se borra** sin rastro |

### OLA 4 — Crecimiento comercial
| | Proceso | Complejidad | Nota |
|---|---|---|---|
| 14 | **CN-06** cobranza de cartera | ALTA | Bloqueado por R2 + cuotas→vencimientos + registrar el pago al contado |
| 15 | **CN-08** encargo/apartado | MEDIA *si CN-02 está* | Comparte modelo entero |
| 16 | **CN-11** reparto con GRE | MEDIA | El documento pesado ya está hecho |

### OLA 5 — Almacén y cumplimiento *(solo si el negocio lo pide)*
| | Proceso | Complejidad | Nota |
|---|---|---|---|
| 17 | **CN-14** cierre de periodo y libros | MEDIA | Casi todo nativo (`fiscalyear_lock_date` + `account.lock.exception`). **Buen ratio** |
| 18 | **CN-13** detracción SPOT | MEDIA | Empezar por el fix del neto |
| 19 | **CN-10** merma y vencimientos | ALTA | Crítico en **farmacia** (infracción sanitaria); opcional en librería |
| 20 | **CN-09** recepción y kardex | ALTA | **El más caro.** Reubica la autoría del kardex |
| 21 | **CN-12** depósito en el banco | ALTA | Bloqueado por R9 (no hay endpoint de subida) |
| 22 | **CN-04 Fase B** multi-puesto | ALTA | **Solo si hay dos mostradores.** Exige `sesion_id` en `account.move` + `DROP INDEX`. **Quitar el índice sin eso lo empeora** |

---

## 6. DESCARTADOS (no revivir sin justificar)

| Propuesta | Motivo |
|---|---|
| **Administración de accesos** (alta/baja/rol por el dueño) | **No es proceso**: CRUD de un actor sin handoff. **Pero es H-4**, habilitador obligatorio. Ojo: el "riesgo del empleado saliente con token vivo" es falso — el TTL real es **12h** (`_TTL_HOURS_DEFAULT`, `main.py:36`), no los 365 días de `api_key_duration` (que es solo el tope de la UI de Odoo, y el login lo esquiva con `.sudo()`); y desactivar al usuario **ya corta la key en el siguiente request** |
| **Bitácora de acciones sensibles / control de reimpresión** | Infraestructura + reporte, no proceso. Y la premisa es falsa: `account.move` **ya tiene chatter y `tracking=True`** en `l10n_pe_biller_state` ⇒ "quién anuló esta boleta" **ya es auditable**. El único aporte genuino (contar descargas de ticket, `_serve_file` no deja huella) es un ticket |
| **Cobro de factura con detracción** (versión "falso faltante") | El "descuadre diario" **no ocurre** si se declaran los medios (`caja_arqueo.py:38-41` usa `v['medios']`, no `v['total']`). Sobrevive **CN-13**, con el fix del neto declarado como bug aparte |
| **Saneamiento de la cola de excepciones SUNAT** | La cola **ya existe** (`GET /ne/api/comprobantes?estado=rechazado`, `main.py:658`) y el KPI `porAtender` ya está computado y pintado en Inicio. Los pasos que justificaban los roles (dar de baja un correlativo "inutilizado") son **ilegales y el addon ya los prohíbe**: un rechazado nunca existió y **se reemite con el mismo número** |
| **Cadena fiscal del anticipo** (como proceso propio) | Es **CN-02** contado desde el ángulo fiscal ⇒ **fusionado dentro**. Lo genuino (`0104`, Many2one, unicidad) son tickets |
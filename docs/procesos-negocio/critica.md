**Convención de rutas:** salvo indicación, todo cuelga de `addons/l10n_pe_ne_biller/` (repo canónico, ver §0). La SPA cuelga de `ne-express: `.

---

# 0. Antes de responder nada: el catálogo está escrito sobre DOS repos distintos y nadie lo reconcilió

Verificado en disco, no inferido:

| | `fact/odoo-facturacion-addons` | `fact2/odoo-facturacion-addons` |
|---|---|---|
| `__manifest__.py` | `19.0.1.3.0`, depends `[l10n_pe, account, uom, mail]` | `19.0.1.6.0`, depends `[... 'stock', 'product_expiry']` |
| git HEAD | `a7ff321` (PR #29) | `cffb92d` (PR #47) |
| grupo anulación | no existe | `security/l10n_pe_ne_security.xml` → `group_l10n_pe_ne_anulacion` |
| guías, product_views, PLE 12.1 | no | sí |

El brief de la tarea apunta a `fact/` (v1.3.0). **Es la copia vieja.** Consecuencias directas sobre el catálogo:

1. **`puedeAnular` NO es un permiso fantasma.** `controllers/main.py:339-356` devuelve `"puedeAnular": self._puede_anular(uid)` (:353), `_puede_anular` está en `:128-133`, y `POST /ne/api/anular` responde 403 sin el grupo (`:717-727`). Hasta hay migración para no despojar a los emisores existentes: `migrations/19.0.1.4.0/post-anulacion-grupo.py`. Ese "hallazgo" es **el «por qué importa» central de dos procesos válidos** ("Reversión de venta con autorización", "Devolución de mercadería: NC y reembolso") y aparece en 4 de los 5 mapas. Está muerto.
2. **No son 72 rutas, son 90** (`grep -c "@http.route" controllers/main.py` = 90).
3. **El bug "las condiciones comerciales se descartan en silencio"** (mapa:spa-flujos) está **arreglado**: `models/l10n_pe_ne_cotizacion.py:40-42` ya tiene `forma_pago`, `tiempo_entrega`, `garantia`.
4. **`_l10n_pe_ne_ventas_sesion` ya no filtra `= 'enviado'`**: hoy es `("l10n_pe_biller_state", "not in", ("rechazado","error","anulado"))` (`models/l10n_pe_ne_caja.py:57-73`), con un docstring que documenta el trade-off. El proceso "Devolución de mercadería" cita el código viejo y describe un mecanismo que ya no existe (el defecto de fondo sí sobrevive: `anulado` sigue excluido).
5. `models/l10n_pe_ne_cotizacion.py:319` ya tiene `descuento` (%), lo que la carta "Aprobación de descuento" niega.

Dos lentes (`retail-mostrador`, `dinero-cobranza`) leyeron fact2 y **lo dijeron explícitamente** ("el mapa que me pasaron dice lo contrario porque está leído sobre una copia vieja del repo"). Las otras tres leyeron fact/. **El catálogo conservó ambas versiones y las presenta como igual de válidas.** Quien implemente esto va a "arreglar" un bug inexistente y va a tratar el resto de los hallazgos caducos como vigentes. Esto no es una nota al pie: invalida la mitad de la evidencia.

**Acción P0, antes de cualquier diseño: borrar `fact/` del set de trabajo y re-verificar los 18 válidos contra fact2.** Si el usuario dice que el backend vivo es `fact/`, entonces "Control de vencimientos y merma", "Recepción de mercadería" y todo lo de stock se caen en bloque.

---

# 1. Procesos que faltan

## 1.1 LOS DOS CASOS QUE PIDIÓ EL USUARIO NO ESTÁN EN LA LISTA

Es el fallo de completitud más grave y nadie lo vio porque todos lo dieron por supuesto.

Recorre los 18 válidos: relevo de turno, devolución mostrador, recepción, fiado, delivery, merma, encargo, reversión, descuento, arqueo ciego, egresos, cobranza ×2, cierre de turno, depósito, devolución NC, anticipo, libros, anulación, detracción. **Ninguno es "COTIZA → CAJA → DESPACHO". Ninguno es "ADELANTO → ORDEN DE TRABAJO → COLA → SALDO → RECOJO".** Lo único cercano es "Encargo / pedido especial", que tiene otro disparador (no hay stock) y otra cola (proveedor, no técnico).

Peor: **tres cartas válidas declaran dependencia de un proceso que el catálogo no contiene.**
- "Aprobación de descuento", nota E: *"SACAR LOS PASOS 4-5 (congelar precios + cajero sin edición): pertenecen al Caso 1... Este proceso los CONSUME como precondición."*
- "Cobranza de crédito", nota 4: *"es el MISMO trabajo que necesita el Caso 1 del usuario (el cliente paga en caja)."*
- "Venta al crédito (fiado)", nota 1: *"declárese como variante del proceso de venta (Caso 1)."*
- Y "Anticipo: la factura de anticipo nace al COBRAR" fue **descartado por redundante con el Caso 2** — que no existe.

O sea: el catálogo delegó el trabajo nuclear a un placeholder. Las lentes generaron procesos *adyacentes* y los dos anclas cayeron por el hueco entre ellas. Hoy no hay una sola carta que diga qué modelo, qué estados, qué grupos y qué endpoints necesitan `cotización → cobro → entrega` y `anticipo → OT → cola → saldo → entrega`. **Eso es lo primero que hay que escribir, y todo lo demás se ordena detrás.**

## 1.2 La vigencia de la cotización: el eslabón que rompe el Caso 1 y que ninguna lente tocó

`models/l10n_pe_ne_cotizacion.py:27` define `validez_dias = fields.Integer(default=15)`. Se serializa (`:94` → `validezDias`), se edita (`:255-256`)… y **nada más**. No hay estado `vencida`, no hay cron (`data/l10n_pe_ne_cron.xml` no tiene nada de cotizaciones), no hay check en la conversión.

El Caso 1 del usuario es literalmente *"un usuario COTIZA → el cliente va a CAJA"* — con un intervalo temporal en medio. **El cliente que llega al mostrador el día 40 con una proforma de precios de hace 40 días se lleva el precio viejo, y el cajero no tiene forma de saberlo.** En una librería es S/2 de margen; en una ferretería con fierro y cemento (precios que se mueven semanalmente) es la venta entera a pérdida. Es el agujero de plata más barato de tapar de todo el catálogo: un `estado='vencida'`, un cron y una guarda en la conversión.

Y nótese la ironía: hay una carta entera dedicada a **aprobar descuentos de 5%** mientras el sistema regala descuentos del 100% de la inflación por caducidad, sin que nadie apruebe nada.

## 1.3 El efectivo en DÓLARES es invisible al arqueo, por diseño

`tools/caja_arqueo.py`, `agrupar_ventas`:

```python
moneda = (v.get("moneda") or "PEN").upper()
if moneda != "PEN":
    count_usd += 1
    total_usd = _r2(total_usd + monto_total)
    continue          # <-- no entra a porMedio
```

El USD suma a un total informativo y **jamás entra a `porMedio`**, que es lo único que `calcular_arqueo` cruza contra el conteo físico. Traducción: **los dólares en efectivo del cajón no se cuentan, no se esperan y no descuadran nunca.** Un cajero puede quedarse con el 100% del efectivo en dólares y los tres procesos de arqueo del catálogo —"Relevo de turno", "Arqueo ciego", "Cierre de turno"— darán diferencia cero.

Tres lentes escribieron controles sofisticados (arqueo ciego, aprobación de descuadre, historial por cajero) sobre una caja que tiene una puerta abierta de par en par al lado. Ninguna la vio. En un negocio que cobra en dólares (servicios a empresas, turismo, repuestos importados) esto es *el* hallazgo de caja.

## 1.4 Cuentas por pagar: pagar al proveedor con retención — huérfano deliberado

El catálogo tiene **tres** procesos de cobranza (plata que entra) y **cero** de pago a proveedores (plata que sale). Y no es que no exista el código:

- `controllers/main.py:710` → `l10n_pe_ne_quick_retencion(payload)`
- `models/account_payment_retencion.py:66` → `payment_type == 'outbound' and partner_type == 'supplier'`
- `:351-353` → `bills = self._l10n_pe_ne_quick_related(payload, prov, 'in_invoice')`
- `:388` → `jtype = 'purchase' if move_type == 'in_invoice'`

Emitir un comprobante 20 **es pagarle a un proveedor** reteniéndole el 3%. Es un `account.payment` outbound reconciliado contra sus facturas. Es dinero saliendo del negocio, sin aprobación, sin rol, sin límite — con exactamente el mismo perfil de riesgo que los gastos (que sí tienen dos cartas) y mucho más monto por operación.

Además el verificador de "Recepción de mercadería" **lo desalojó explícitamente** ("*el paso 6 'programa el pago al proveedor'... es OTRO proceso (cuentas por pagar)... Propónlo como proceso aparte*") y nadie lo propuso. Quedó huérfano por procedimiento, no por juicio. Es un proceso multi-rol de libro: quien recibe la factura ≠ quien aprueba el pago ≠ quien ejecuta el pago y emite el CRE.

## 1.5 Menor pero feo: el tipo de cambio es un parámetro fiscal editable por cualquiera, retroactivamente

`POST /ne/api/tipo-cambio` (`controllers/main.py:493-499`) → `models/res_company.py:146-157`:

```python
def l10n_pe_ne_set_tipo_cambio(self, payload):
    """Carga manual del TC (fallback cuando no hay internet). {fecha?, tc}."""
    ...
    fecha = fields.Date.to_date(payload.get("fecha")) if payload.get("fecha") else ...
    self._l10n_pe_ne_tc_store(fecha, tc)
```

Sin `has_group`, con `fecha` arbitraria (retroactiva), y expuesto al cajero en el POS (`pages/POS.tsx:82`). El TC determina la conversión a soles del XML y del PLE: es base imponible. Cualquier emisor reescribe el TC de cualquier fecha, sin rastro. El catálogo pone gates en anular y en descuentos de S/5 y deja abierto el parámetro que multiplica todas las ventas en moneda extranjera.

---

# 2. Descartes erróneos

## 2.1 ERROR GRAVE: "Administración de accesos" — se descartó el prerrequisito de los otros 18

Motivo del descarte: *"NO es un proceso de negocio: es un CRUD de un solo actor sobre res.users... no hay un solo handoff."*

**Es un error de categoría.** El usuario pidió ROLES. Los 18 válidos asignan roles a personas. Hoy, **la única forma de crear un usuario o asignarle un grupo es `base.group_system`** (`models/res_company.py` `l10n_pe_ne_provision_tenant`, `models/res_users.py:36-95`), es decir: el operador del SaaS. Un rol que nadie puede otorgar no existe. Este catálogo entero es indesplegable hasta que alguien conteste *"¿quién le da el grupo Cajero a la señora que entró a trabajar el lunes?"*.

Y el handoff SÍ existe, mediado por un artefacto (la credencial): el dueño da de alta → el empleado opera → el dueño revoca cuando lo despide. Es el mismo patrón que "el cajero cobra → el despachador entrega". El criterio "sin handoff" se diseñó para filtrar ruido de CRUD y terminó filtrando el habilitador de todo lo demás.

Lo peor: **el propio descarte contiene el mejor hallazgo de ingeniería del documento** y lo enterró en la sección "NO los revivas": `base/security/base_security.xml` define `res_users_rule` como GLOBAL con dominio `['|',('share','=',False),('company_ids','in',company_ids)]` → darle `res.users` al dueño le expone los usuarios de **todos los RUC de la plataforma**; y quien escribe `group_ids` puede auto-otorgarse `base.group_system`. Eso significa que "Dueño puede gestionar sus usuarios" **no se puede construir con grupo+ACL**: exige métodos `.sudo()` del addon con filtro explícito por compañía y whitelist de grupos otorgables. Es trabajo de diseño real, con riesgo de escalada de privilegios, y está catalogado como "no lo revivas".

Los tres puntos fácticos del descarte son correctos (el TTL es 12h, no 365 días; `disjoint_ids` no aplica a funcionales) — pero refutan la *justificación* del proponente, no la *necesidad* del proceso. Se mató al mensajero.

## 2.2 ERROR: detracción — el catálogo aceptó y descartó el mismo proceso, y ambas versiones tienen un hecho falso

- **Válido** ("Detracción: cobro del neto (88%)"): *"el cajero cuadra contra el total → descuadre todos los días... la caja no distingue el neto del total facturado."*
- **Descartado** ("Cobro de factura con detracción"): *"EL DOLOR CENTRAL NO EXISTE: `calcular_arqueo` usa `v['medios']`, NO `v['total']`. Si el cajero declara los medios con el neto cobrado, el arqueo cuadra: NO hay falso faltante."*

Verifiqué. **Los dos están mal, y el error del descarte es el que importa:**

```python
# tools/caja_arqueo.py — agrupar_ventas
if medios:
    for mp in medios: por_medio[medio] += ...
elif forma == "Contado":
    por_medio[EFECTIVO] += monto_total   # <-- TODO el total al efectivo
    sin_medio += 1
```
```python
# models/account_move_biller.py:4764-4765
if fp.get("medios"):
    move.l10n_pe_ne_medios_pago = fp.get("medios")
```
```
grep -c "medios" pages/Emitir.tsx  →  0
grep -c "medios" pages/POS.tsx     →  13
```

**Emitir.tsx nunca envía medios.** La detracción se configura en Emitir, no en el POS. Por lo tanto *toda* factura con detracción cae en el `elif` y va al esperado en Efectivo **por el 100% del total facturado**, cuando el cliente pagó el 88%. El falso faltante es real, sistemático y diario. El descarte lo negó apoyándose en la rama `if medios:` que en ese flujo nunca se ejecuta. La condición "si el cajero declara los medios" que el descarte asume es **imposible desde la UI**.

Ahora bien: el descarte acertó en lo demás (el error normativo del proponente sobre el crédito fiscal; que conciliar el BN es inviable sin extractos; que registrar la constancia es data-entry de una persona). Y el válido acertó en el dolor pero erró el mecanismo. **Ninguna de las dos cartas debe sobrevivir tal como está.** Lo que sobrevive es un bug de una línea (que Emitir mande medios) que vale más que los dos procesos juntos — ver §3.2.

## 2.3 ERROR de forma: "Anticipo: la factura de anticipo nace al COBRAR" se descartó por redundante con un proceso que no existe

Motivo literal: *"es el Caso 2 recontado desde el ángulo fiscal"*. Pero el Caso 2 **no está en el catálogo** (§1.1). Se descartó por duplicar el vacío. Su hallazgo único —`_l10n_pe_tipo_operacion` nunca devuelve `0104` (Venta interna – Anticipos, cat. 51), o sea que **la factura de anticipo del Caso 2 hoy no se puede emitir correctamente**— sobrevivió por casualidad, porque la carta de "Cadena fiscal del anticipo" lo recogió. Si esa carta hubiera caído, se pierde el hallazgo que bloquea el Caso 2 entero.

## 2.4 Vara desigual: "Saneamiento de la cola de excepciones SUNAT"

El descarte es técnicamente sólido (los pasos 4-5 son ilegales: un rechazado no consume correlativo y no admite RA; `models/account_move_biller.py` lo prohíbe explícitamente). Pero **mató el proceso por sus dos peores pasos**, mientras a los 18 válidos los verificadores les amputaron pasos y los dejaron vivir ("RECORTAR a 4 pasos", "eliminar el paso 4", "sacar el paso 7"). Y su hallazgo residual —que `'error'` y `'en_proceso'` **sí entran al PLE**— es exactamente el que la carta válida "Cierre mensual y libros" usa como su justificación central. **El mismo defecto mató a un proceso y justificó a otro.** Una de las dos adjudicaciones está mal; como mínimo, el criterio no fue el mismo.

---

# 3. Decisiones de diseño peligrosas

## 3.1 El catálogo está organizado por LENTE, no por PROCESO: 18 cartas ≈ 8 procesos con 2-4 diseños contradictorios

| Proceso real | Cartas "válidas" |
|---|---|
| Cierre/arqueo de caja | **3** (Relevo de turno, Arqueo ciego, Cierre de turno) |
| Devolución / NC / anulación | **4** (Devolución mostrador, Reversión, Devolución NC+reembolso, Anulación RA/RC) |
| Cobranza al crédito | **3** (Fiado, Cobranza de crédito, Cobranza de ventas al crédito) |
| Gastos / egresos | **2** |

Y **no dicen lo mismo**. El estado nuevo de la sesión de caja se llama `observada` en una carta, `cerrada_pendiente_revision` en otra y `pendiente_aprobacion` en la tercera: tres nombres, tres máquinas, mismo campo, mismo modelo. Sobre la multi-caja se contradicen frontalmente: "Cierre de turno" dice *quitar el índice único*; el verificador de "Arqueo ciego" demuestra que la justificación de la multi-caja es **falsa** (*"el índice es parcial: impide simultáneas, no secuenciales"*, `models/l10n_pe_ne_caja.py:41-48`); y "Relevo de turno" dice que la Fase 2 exige romper antes el amarre por `create_date`. Los tres tienen razón en un pedazo.

Esto envejece pésimo: el primero que implemente gana por accidente y **las otras dos cartas se convierten en documentación permanentemente falsa** dentro del mismo documento. Hay que colapsar el catálogo a ~8 procesos con un dueño y un diseño cada uno, antes de escribir una línea.

## 3.2 Se está construyendo una aprobación encima de un número que el sistema calcula mal

Tres cartas proponen: *diferencia > tolerancia → sesión observada → el supervisor aprueba con justificación*. Pero (§2.2) **toda venta emitida desde Emitir sin medios se imputa 100% a Efectivo**: transferencias, detracción al 88%, anticipo aplicado, gratuitos. El "esperado en efectivo" está inflado casi todos los días, por diseño y sin fraude.

Consecuencia: el gate salta a diario sobre ventas legítimas → el dueño aprueba en automático en la semana 1 → **el control muere de fatiga de alarma y deja algo peor que nada: un registro firmado que certifica un número falso.** Y en la disputa real ("faltan S/200"), el faltante estará escondido bajo el ruido.

**Regla que este catálogo necesita y no tiene: ningún flujo de aprobación se implementa antes de que su entrada sea correcta.** Primero `Emitir.tsx` manda medios (y `_l10n_pe_importe_cobrar()` alimenta la caja en vez de `amount_total` crudo, `models/l10n_pe_ne_caja.py:76`), después se discute quién aprueba qué. Eso son horas contra semanas, y desbloquea tres cartas de golpe.

## 3.3 Es un catálogo de control diseñado para una empresa que el cliente no es

Cuenta las aprobaciones propuestas: descuadre, descuento, crédito, gasto, egreso, devolución, anulación, merma, depósito, reapertura de periodo, recepción. **Once compuertas bloqueantes** para un negocio de 2-3 personas donde el supervisor es la dueña — y la dueña no está en la tienda, está comprando en Mesa Redonda. Cada gate es un cliente esperando en el mostrador.

Varios verificadores lo notaron **uno por uno** (*"en una PyME de 2 personas Supervisor y Contador colapsan en el dueño"*, *"si eso también es demasiado duro... degrádalo a aviso"*, *"un faltante de S/12 un sábado deja la tienda sin caja el domingo"*) pero **nadie sacó la conclusión sistémica**: la segregación de funciones exige ≥2 personas por turno, y la propia documentación del producto dice que el tenant real tiene un solo usuario (`docs/plan-pruebas/10-autenticacion-admin.md:330` → ADM-018 BLOQUEADO por *"un único admin global"*).

Si el emisor mediano es 1-2 usuarios, **la primitiva correcta no es "aprobación bloqueante" sino "registro + evidencia + revisión asíncrona"** — que es justo lo que proponía la bitácora descartada. El catálogo eligió la primitiva cara y la aplicó once veces sin preguntar.

## 3.4 No hay grafo de dependencias: es una lista plana de 18 cosas "complejidad: alta"

Las dependencias reales existen y están enterradas en notas de verificador: multi-caja **requiere** `sesion_id` en `account.move`; cobranza **requiere** registrar el pago del contado; descuento **requiere** normalizar `list_price` (que hoy se escribe sin IGV en `_l10n_pe_ne_quick_product` y se lee con IGV en el POS); todo lo de aprobación **requiere** `mail.thread` (ningún modelo propio lo hereda). Sin un DAG explícito, esto se implementa en el orden en que se lea, que es el orden garantizado de rehacer el trabajo.

## 3.5 Nadie costeó las migraciones — y el repo ya demostró que hacen falta

`migrations/19.0.1.4.0/post-anulacion-grupo.py` existe **porque agregar UN grupo a tenants vivos exigió una migración** para no quitarle la anulación a los emisores existentes. Este catálogo propone ~10 grupos nuevos, bajar `perm_unlink` del emisor en `security/ir.model.access.csv` (15 filas, todas al mismo grupo) y estados nuevos en 4 modelos. Cada uno necesita su script y su regla "ningún usuario existente pierde una capacidad". Ni una carta lo menciona. También: `ir.model.access.csv` **no tiene ni una fila para `group_l10n_pe_ne_anulacion`** — el grupo existe y vive solo de lo que hereda del emisor. Ese es el patrón que se va a replicar ×10 si nadie lo mira.

## 3.6 Se dio por sentado que el eje de segregación es el ROL — puede que sea el LOCAL

El usuario dijo "cada rol solo ve lo suyo" y las cinco lentes leyeron *rol*. Pero en retail peruano el eje suele ser **el punto de venta**: la sucursal de Gamarra no ve lo de Grau. El addon hoy asume **un solo local por RUC** en dos sitios estructurales: `models/account_move_biller.py:3494-3495` y `:3650` → `stock.warehouse.search([("company_id","=",...)], limit=1)`, y `models/l10n_pe_ne_caja.py:41-48` → una sesión abierta por compañía. Si el negocio tiene dos locales, el kardex y la caja son irreparables con este catálogo, **y ninguna carta pregunta**. Es la pregunta 1 de §4 por algo.

## 3.7 (Menor, pero se repite) Se afirma imposible algo que ya funciona

Tres cartas dicen que "ventas por cajero" no se puede porque no hay eje de usuario. `account.move.invoice_user_id` es nativo, `store=True`, con `compute='_compute_invoice_default_sale_person'` que lo llena con el usuario actual (`odoo19: addons/account/models/account_move.py:678-686, :804`), y **el addon ya lo usa**: `models/account_move_biller.py:5041-5042` imprime `"Atendido por: " + self.invoice_user_id.name` en el ticket. El reporte "ventas por cajero" es un `read_group`, no un proyecto. Un catálogo que declara imposible lo que ya está impreso en el ticket pierde credibilidad en lo demás.

---

# 4. Las seis preguntas al usuario

**1. ¿Cuántas personas hay en el local a la vez, y el dueño está adentro?**
Si la respuesta es "dos, y el dueño no está", la segregación de funciones es imposible y las once aprobaciones bloqueantes del catálogo son daño neto: hay que rediseñar a "registro + evidencia + revisión asíncrona". También decide si hay que romper el índice único de `models/l10n_pe_ne_caja.py:41-48` (que arrastra: `sesion_id` en `account.move` y rehacer el amarre por `create_date`, `:57-73`). *Cambia: el 60% del catálogo.*

**2. ¿Un local o varios? ¿Un RUC o varios por dueño?**
`stock.warehouse.search(..., limit=1)` (`models/account_move_biller.py:3494`) y una caja por compañía asumen local único. Si hay dos locales, el eje de segregación es **sucursal**, no rol, y el catálogo entero está mal enfocado. *Cambia: el eje del modelo de datos.*

**3. ¿Quién da de alta al cajero y le asigna el rol: tú (operador SaaS) o el dueño del RUC?**
Hoy solo `base.group_system` puede (`models/res_company.py`, `models/res_users.py:36-95`). Si debe hacerlo el dueño, "Administración de accesos" (descartado) es **P0** y no es un CRUD: `res_users_rule` es global (todo usuario interno ve los usuarios de todos los tenants) y escribir `group_ids` permite auto-otorgarse `base.group_system`. Exige métodos `.sudo()` con whitelist. *Cambia: si el catálogo es desplegable o no.*

**4. Caso 2 — el adelanto: ¿se factura al recibirlo, o se da un recibo interno y un solo comprobante al final?**
Si se factura: hay que emitir con `tipoOperacion 0104` (hoy `_l10n_pe_tipo_operacion` nunca lo devuelve) y regularizar con descuento global cód. 04, convirtiendo `l10n_pe_ne_anticipo_doc` (Char tecleado a mano) en Many2one. Si es recibo interno: no se toca nada fiscal y el Caso 2 cuesta un tercio (aunque SUNAT diga otra cosa). *Cambia: el núcleo fiscal del Caso 2.*

**5. Caso 1 — ¿el despacho entrega siempre en el acto, o el cliente puede recoger después / otro día?**
Si es en el acto, "despacho" es un paso de UI y no necesita modelo. Si es diferido, necesitas modelo de pedido + reserva, y hay que decidir qué hacer con el kardex: la emisión ya descuenta el stock directo al cliente sin `stock.picking` (`models/account_move_biller.py:3494-3510`), así que el producto sale contablemente mientras la caja sigue en el mostrador. *Cambia: si el Caso 1 es una pantalla o un modelo nuevo.*

**6. La cotización: ¿el precio y la validez de 15 días son vinculantes?**
`validez_dias` existe (`models/l10n_pe_ne_cotizacion.py:27`) y no hace nada. ¿Qué debe pasar si el cliente llega a caja el día 40: se respeta el precio, se recotiza, o el sistema lo bloquea? Y una vez que pagó, ¿alguien puede seguir editando esa cotización? (hoy sí: `:260-261` escribe `estado` desde el payload y `:268-276` no valida transiciones). *Cambia: la condición de entrada y la de salida del Caso 1.*
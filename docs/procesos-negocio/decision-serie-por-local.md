# Decisión de diseño — La serie de numeración es del local, el correlativo sigue siendo del RUC

> Tomada al implementar la fase «Series por sucursal» (addon `l10n_pe_ne_biller` **19.0.1.21.0**).
> El plan aprobado está en [plan-fase-series-por-sucursal.md](plan-fase-series-por-sucursal.md);
> aquí queda el porqué, para quien lea el código dentro de un año.

## El problema en una línea

El dueño con dos locales necesita que Miraflores emita F001 y San Isidro F002, cada uno con su
correlativo. Hasta esta fase la serie era del RUC entero (`account.journal.l10n_pe_ne_serie`, y de
hecho ni eso: la emisión usaba seis valores hardcodeados) y el `codLocalEmisor` del XML se llenaba
del payload tal cual, sin validar contra ningún catálogo.

---

## D1 · La serie vive en un modelo propio, no en el diario ni en el establecimiento

`l10n_pe_ne.serie` = `codigo` + `tipo_doc` + `establecimiento_id` (**nullable**) + `activa` +
`predeterminada` + `company_id`.

**Descartado colgarla del `account.journal`:** obligaría a crear 8+ diarios de venta por tenant
(F001/F002/B001/B002/FC01/FD01/BC01/BD01), cada uno con su secuencia contable y su libro — ensuciar
el plan de cuentas para modelar numeración fiscal. Y el repo ya tomó la decisión contraria una vez:
el correlativo dejó de ser el folio del diario justamente porque era un contador global que abría
huecos por serie.

**Descartado `serie_factura`/`serie_boleta` en el establecimiento:** más barato, pero no cubre
NC/ND (cuatro columnas más), no admite `activa`/`predeterminada`, y sobre todo **no puede
representar la serie del domicilio fiscal**, que no tiene fila donde colgar el campo.

## D2 · El motor de correlativo NO se toca: la secuencia se llavea por (compañía, serie)

Meter el local en el `code` de la `ir.sequence` es la tentación natural **y es el bug**: dos
locales que por olvido compartieran F001 obtendrían cada uno F001-00000001 → duplicado fiscal, que
solo se corrige con comunicación de baja ante SUNAT. La unicidad del número la garantiza
`(compañía, serie)`; la relación local↔serie es una restricción de **configuración**, nunca de
numeración. Queda escrito como comentario-contrato en `_l10n_pe_ne_next_correlativo`.

Corolario que abarató toda la fase: como el código de serie es único por RUC, «correlativo por
serie» YA ES «correlativo por local». F002 estrena su secuencia sembrada en 0 al primer uso, sin
migrar ni una. Bonus: con series distintas bajan dos cajeros de la misma fila de `ir_sequence`, así
que dejan de serializarse en el lock `no_gap`.

## D3 · El `'0000'` sigue siendo sintético: `establecimiento_id = NULL` es el domicilio fiscal

Materializarlo como fila real costaba una migración por tenant, sincronizar dirección y ubigeo
contra el partner de la compañía y cambiar el `id: 0` que la SPA usa como clave — a cambio de nada,
porque la FK que se necesita es nullable de todos modos.

**Deuda asumida y documentada: todo domain nuevo debe acordarse de
`('establecimiento_id', '=', False)`.** Olvidarlo hace desaparecer en silencio las series del
domicilio fiscal de un listado o de la resolución del default. Lo mismo en SQL: el índice único
parcial lleva `COALESCE(establecimiento_id, 0)` porque en Postgres `NULL != NULL`.

## D4 · Un solo resolver, dentro de `quick_emit`, con cadena corta

Todos los canales (SPA, POS, orden de trabajo, cobro de cotización, lote masivo) pasan por
`l10n_pe_ne_quick_emit`, así que el local se resuelve una sola vez y **antes del `create`** — el
local decide la serie y la serie decide el contador, de modo que resolverlo después llega tarde
para elegirla y demasiado tarde para rebotar sin quemar un correlativo.

1. NC/ND → el local del comprobante afectado (herencia dura, el payload se ignora).
2. `codEstablecimiento` explícito del payload.
3. Local de la serie pedida, si esa serie está declarada con local.
4. Local de la caja abierta.
5. `'0000'`.

**Descartado el local por usuario** (`res.users.l10n_pe_ne_establecimiento_id`), aunque se
presentara como preferencia y no como eje de permisos: roza la decisión de producto del 2026-07-18
(se segrega por RUC + rol) y el caso real —el cajero que siempre trabaja en el mismo local— ya lo
cubre el escalón 4, que se declara una vez por turno. **Descartado también un default de local en
`res.company`:** para un tenant de un local es exactamente `'0000'`; para uno de dos, elegir uno
«por defecto» es la forma elegante de declarar mal la mitad de las ventas.

## D5 · El registro arranca VACÍO: la retrocompatibilidad va en el código, no en los datos

No hay migración que siembre series. `_l10n_pe_ne_default_serie` conserva su comportamiento
carácter por carácter cuando no hay fila, y `_l10n_pe_ne_series_habilitadas` pasa a **UNIÓN**
(registro ∪ diarios ∪ gemelo F↔B ∪ los seis defaults del sistema), nunca a reemplazo.

**Descartado el `post-migrate` que siembra el registro** desde series emitidas + diarios +
gemelos. Suena a cortesía y es riesgo: hay que deduplicar (nada impide hoy dos diarios de venta con
la misma serie), acertar con el `tipo_doc` derivado del prefijo y con quién queda de
predeterminada, y si algo revienta se cae el `-u` a mitad del upgrade. Además convierte un fallback
de código —que se prueba con un test— en datos por tenant que pueden derivar. Cero migración de
datos = cero riesgo de upgrade: lo que el dueño ve en Series el día del despliegue es lo mismo que
veía ayer. El `pre-migrate` de 19.0.1.21.0 solo tira el índice viejo de caja y normaliza el
`codLocalEmisor` nulo; si alguna vez hiciera falta insertar, iría con `ON CONFLICT DO NOTHING`.

## D6 · El muro es un grupo nuevo de `l10n_pe_ne_biller`, y se aplica en el modelo

`group_l10n_pe_ne_config_series` se declara en biller —no en `l10n_pe_ne_roles`, porque biller no
depende de roles y debe seguir funcionando sin él; mismo patrón que `group_l10n_pe_ne_anulacion`—
y `l10n_pe_ne_roles` lo suma por `implied_ids` a supervisor y dueño. El `has_group` va **dentro del
método del modelo** (`_l10n_pe_ne_check_config_series`), y el controller solo lo refleja como 403:
desde que el local determina la serie, tocar esto es cambiar la numeración fiscal de la empresa, y
ninguna vía —RPC, backend, un endpoint futuro— debe poder saltárselo. Cubre el CRUD de series **y**
el de establecimientos, que estaba abierto a cualquier emisor.

Leer sigue abierto: cerrar el `GET /ne/api/series` dejaría sin la pantalla de Series a los tenants
pre-roles, que hoy la ven con solo el grupo Emisor.

## D7 · La caja gana `establecimiento_id` porque sin eso el segundo local no arranca

No es un extra: el índice `UNIQUE (company_id) WHERE estado='abierta'` impedía que San Isidro
abriera caja mientras Miraflores tuviera la suya. Pasa a
`(company_id, COALESCE(establecimiento_id, 0))`. El local se elige **al abrir** (una vez por turno),
nunca por venta, y el arqueo filtra sus ventas por ese local: sin eso el esperado de Miraflores
incluiría las ventas de San Isidro, el conteo ciego siempre daría diferencia y esa diferencia
quedaría congelada e inmutable en `conteos_cierre`.

---

## Lo que cambia para quien ya está en producción

- Un tenant de un solo local que no configura nada **no hace nada y no nota nada**: mismas series,
  mismas secuencias, mismo menú, cero migración de comprobantes.
- **Cambio observable:** las NC/ND de tenants que ya emiten desde anexos pasan de declarar `'0000'`
  a declarar el local del comprobante afectado. Es la corrección buscada —el histórico no se
  reescribe—, pero cambia la columna del RVIE: hay que avisar al contador y anotar el corte por
  fecha.
- **Addon y SPA se despliegan juntos:** una SPA vieja no envía `codEstablecimiento` y con caja
  abierta en 0002 el backend nuevo declararía 0002 mientras la pantalla mostraría domicilio fiscal.
  El chip de local en el POS es la red para que el cajero lo vea antes de cobrar.

## Fuera de alcance (dicho para que no se relea como olvido)

- **Almacén por local.** El stock sigue tomando el primer `stock.warehouse` de la compañía. El
  negocio pidió numeración, no logística.
- **Usuarios por local**, ni como eje de permisos ni como preferencia (D4).
- **Contabilidad por local:** ni diario, ni analítica, ni centro de costo. La `l10n_pe` no lo exige.
- **Serie por local en la GRE.** SUNAT no la exige; el wizard ya obliga a elegir el local de
  partida. Lo único que entró es la validación de existencia del código, compartida.
- **Series de retención/percepción**, que siguen numerando con un `max()+1` sin `ir.sequence` ni
  lock: es el eslabón débil de numeración del addon y queda anotado como deuda con nombre propio.
- **Materializar el `'0000'`** (D3) y **sembrar el registro por migración** (D5).
- **Dar de alta el anexo ante SUNAT**, que es trámite externo (ficha RUC). El sistema valida contra
  su catálogo local y el mensaje de error lo dice, para que el dueño no crea que «ya está dado de
  alta» por haberlo creado en Negocio.

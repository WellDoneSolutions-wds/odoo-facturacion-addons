# Plan — Fase Series por sucursal (numeración fiscal por establecimiento)

**El problema en una línea:** el dueño con dos locales necesita que Miraflores emita F001 y
San Isidro F002, cada uno con su correlativo, pero hoy la serie es del RUC completo y todos
los locales comparten la misma numeración.

## Qué existe HOY

- `l10n_pe_ne.establecimiento` (`models/l10n_pe_ne_establecimiento.py:7`): `codigo` de 4
  dígitos + `ubigeo` + `direccion` + `distrito_id` + `company_id`, con
  `unique(codigo, company_id)` (`:19`). **No tiene ningún campo de serie, diario ni
  secuencia.** El `'0000'` (domicilio fiscal) es SINTÉTICO: `l10n_pe_ne_list` (`:32`) lo
  fabrica con `id: 0` desde `company.partner_id` y `l10n_pe_ne_upsert` (`:53`) rechaza darlo
  de alta.
- La serie vive en `account.journal.l10n_pe_ne_serie` (`models/account_journal.py:7`), ámbito
  compañía. `_l10n_pe_ne_series_habilitadas` (`account_move_biller.py:1568-1587`) construye el
  set válido de QA-074 buscando diarios por `company_id`, derivando el gemelo F↔B (`:1583-1586`)
  y sumando los seis defaults del sistema `{F001,B001,FC01,FD01,BC01,BD01}`.
- La SPA NUNCA usa la serie del diario: `l10n_pe_ne_quick_emit` fija
  `vals['l10n_pe_serie'] = payload.get('serie') or self._l10n_pe_ne_default_serie(tipo, origin)`
  (`:3196`) y ese helper (`:3412`) devuelve los seis defaults hardcodeados. El diario se elige
  a ciegas: `journal = search([type=sale, company], limit=1)` (`:3061`).
- El correlativo YA es por serie: `_l10n_pe_ne_next_correlativo` (`:2038`) crea una
  `ir.sequence` `implementation='no_gap'` por `(company_id, code='l10n_pe.ne.cpe.<serie>')`,
  sembrada desde el máximo `l10n_pe_ne_corr_emit` ya emitido, con `pg_advisory_xact_lock`
  (`:2049`). `_l10n_pe_ne_assign_numero` (`:2084`) congela `serie_emit`/`corr_emit`.
- `account.move.l10n_pe_ne_cod_establecimiento` (`:1277`) es un `Char` con `default='0000'` y
  `copy=False`; viaja al XML como `codLocalEmisor` (`:1988`). Se llena SOLO del payload
  (`:6425`), tal cual, **sin FK, sin validar longitud ni existencia**.
- `res.company` es tenant estricto (1 RUC = 1 base). **No hay eje usuario↔establecimiento**
  (decisión de producto 2026-07-18: se segrega por RUC + rol).
- La sesión de caja (`l10n_pe_ne_caja.py`) no tiene establecimiento y su índice único parcial
  es `ON (company_id) WHERE estado='abierta'` (`init()`, `:44-53`); `_l10n_pe_ne_ventas_sesion`
  (`:72-90`) filtra solo por compañía y ventana de fecha. El stock toma el primer
  `stock.warehouse` de la compañía.
- SPA: `Emitir.tsx:937` pinta el selector «Establecimiento emisor» solo si hay anexos y NO es
  nota; arranca en `'0000'` (`:349`, sticky del terminal según el comentario de `:598`) y solo
  lo envía si `!== '0000'` (`:727`). `POS.tsx` (payload `:271-290`) NUNCA envía
  `codEstablecimiento`. `Series.tsx` es SOLO LECTURA sobre el agregado de comprobantes
  emitidos (`account_move_biller.py:3787`). `Negocio.tsx` tiene el CRUD de establecimientos.

## Qué falta exactamente

1. Un lugar donde declarar «F002 es de San Isidro». Hoy no existe el dato en ninguna tabla.
2. Que la emisión ELIJA la serie a partir del local (hoy la elige un hardcode de seis valores).
3. Que `_l10n_pe_ne_series_habilitadas` acepte F002: sin eso, la primera emisión del segundo
   local muere con «La serie 'F002' no está habilitada».
4. Validar `codEstablecimiento` contra el catálogo: un `'0009'` inventado hoy llega al XML.
5. Que las NC/ND dejen de declarar todas `'0000'` (`copy=False` + selector oculto en notas).
6. Que el POS deje de declarar todo en el domicilio fiscal.
7. Un muro: hoy `/ne/api/establecimientos` POST (`controllers/main.py:1495`) y DELETE (`:1522`)
   solo hacen `_identify()`, sin `has_group`, y el ACL da `1,1,1,1` a
   `group_l10n_pe_ne_emisor`. En cuanto el establecimiento determine la serie, eso es «un
   cajero puede cambiar la numeración fiscal de la empresa».
8. Que el segundo local pueda abrir caja: el índice único actual se lo impide el día 1.

---

## Decisiones de arranque

### D1 — La serie vive en un modelo nuevo `l10n_pe_ne.serie`, no en el diario ni en el establecimiento

Campos: `codigo` (Char 4), `tipo_doc` (Selection `01/03/07/08`), `establecimiento_id`
(M2o `l10n_pe_ne.establecimiento`, `ondelete='restrict'`, **nullable**), `activa`,
`predeterminada`, `company_id`.

**Descartado: colgar la serie del `account.journal`.** `quick_emit` elige el primer diario de
venta a ciegas (`:3061`); la SPA y el POS jamás eligen diario. Habría que invertir ese flujo y
crear 8+ diarios de venta por tenant (F001/F002/B001/B002/FC01/FD01/BC01/BD01), cada uno con su
secuencia contable y su libro — ensuciar el plan contable para modelar numeración fiscal. Y el
repo ya tomó la decisión contraria una vez: el correlativo DEJÓ de ser el folio del diario
justamente porque era un contador global que abría huecos por serie (`:2038`).

**Descartado: campos `serie_factura`/`serie_boleta` en `l10n_pe_ne.establecimiento`.** Es más
barato, pero no cubre NC/ND (FC02/FD02/BC02/BD02 son cuatro columnas más), no admite
`activa`/`predeterminada`, no deja espacio a la GRE, y sobre todo **no puede representar la
serie del domicilio fiscal**: `'0000'` no tiene fila donde colgar el campo. El modelo aparte
con `establecimiento_id` nullable resuelve eso sin materializar nada.

### D2 — El motor de correlativo NO se toca: la secuencia se llavea por (compañía, serie), jamás por establecimiento

Queda escrito como comentario-contrato en `_l10n_pe_ne_next_correlativo` (`:2038`). Meter el
local en el `code` (`'l10n_pe.ne.cpe.%s.%s' % (serie, estab)`) es la tentación natural **y es el
bug**: dos locales que por olvido compartieran F001 obtendrían cada uno F001-00000001 →
duplicado fiscal, que solo se corrige con comunicación de baja. La unicidad del número la
garantiza `(compañía, serie)`; la relación local↔serie es una restricción de **configuración**
(`unique(codigo, company_id)` sobre el modelo nuevo), nunca de numeración.

Corolario que abarata toda la fase: como el `codigo` de serie es único por RUC, «correlativo por
serie» YA ES «correlativo por local». F002 estrena su `ir.sequence` sembrada en 0 al primer uso,
sin migrar ni una secuencia. Bonus: con series distintas baja la contención del lock `no_gap`
(dos filas de `ir_sequence` en vez de una), así que dos cajeros dejan de serializarse.

Se cuela aquí un bug latente preexistente: la ruta CPE confía solo en el `pg_advisory_xact_lock`;
le falta el índice único parcial sobre `ir_sequence` que la GRE sí tiene
(`l10n_pe_ne_guia_remision.py:291`) porque bajo REPEATABLE READ el lock no basta. Con dos locales
emitiendo en paralelo esa carrera se vuelve mucho más probable.

### D3 — `'0000'` sigue siendo sintético: `establecimiento_id = NULL` es el domicilio fiscal

**Descartado: materializar `'0000'` como fila real.** Costaba una migración por tenant,
sincronizar `direccion`/`ubigeo` contra el partner de la compañía, mantener el veto de
`l10n_pe_ne_upsert:53` y —lo caro— cambiar `id: 0` por un id real en
`GET /ne/api/establecimientos`, que `GuiaWizard.tsx` usa como clave (`estab-${e.id}`). A cambio
de nada: la FK que necesitamos es nullable de todos modos.

Deuda asumida y documentada: **todo domain nuevo debe acordarse de
`('establecimiento_id', '=', False)`** para el domicilio fiscal. Olvidarlo hace desaparecer
silenciosamente sus series de un listado o de la resolución del default.

### D4 — Un solo resolver, dentro de `l10n_pe_ne_quick_emit`, con cadena corta

Todos los canales de emisión pasan por ahí (controller `/ne/api/emitir`, orden de trabajo, cobro
de cotización, lote masivo), así que el local se resuelve en un único sitio, **antes del
`create`** (no en `_l10n_pe_ne_quick_flags:6425`, que corre después y por eso no sirve para
elegir la serie):

1. `origin`, si es NC/ND → herencia dura, el payload se ignora.
2. `payload['codEstablecimiento']` explícito.
3. Local de la serie del payload, si esa serie está registrada con local.
4. Establecimiento de la sesión de caja abierta.
5. `'0000'`.

**Descartado: la preferencia de local por usuario** (`res.users.l10n_pe_ne_establecimiento_id`).
Aunque se presente como «preferencia de emisión y no eje de permisos», introduce un campo en
`res.users`, un endpoint en Equipo, una clave nueva en `l10n_pe_ne_perfil` (que sirve a `/login`
y `/whoami`) y una semilla más en la SPA — y roza la decisión de producto del 2026-07-18. El caso
real (el cajero que siempre trabaja en el mismo local) ya lo cubre el escalón 4: abre caja una vez
por turno. Si el uso demuestra que hace falta, se añade después como escalón 4.5 sin romper nada.

**Descartado también: un default de local en `res.company`.** Para un tenant de un solo local es
exactamente `'0000'`; para uno de dos, elegir uno «por defecto» es la forma elegante de declarar
mal la mitad de las ventas.

### D5 — El registro arranca VACÍO: la retrocompatibilidad va en el código, no en los datos

No hay migración que siembre series. `_l10n_pe_ne_default_serie` conserva su comportamiento
carácter por carácter cuando no hay fila, y `_l10n_pe_ne_series_habilitadas` pasa a **UNIÓN**
(registro ∪ diarios ∪ gemelo F↔B ∪ los seis defaults), nunca a reemplazo.

**Descartado: el `post-migrate` que siembra el registro** desde series emitidas + diarios +
gemelos + defaults. Suena a cortesía y es riesgo: hay que deduplicar (nada impide hoy dos diarios
de venta con la misma serie), acertar con `tipo_doc` derivado del prefijo y `predeterminada`, y si
algo revienta se cae el `-u` a mitad del upgrade. Además convierte un fallback de código —que se
prueba con un test— en datos por tenant que pueden derivar. Cero migración de datos = cero riesgo
de upgrade. Lo que el dueño ve en Series el día del upgrade es lo mismo que veía ayer (el agregado
sobre emitidos, ahora marcado `origen: 'uso'`).

### D6 — El muro es un grupo nuevo definido en `l10n_pe_ne_biller`, y aplicado en el modelo

`group_l10n_pe_ne_config_series`, declarado en `l10n_pe_ne_biller/security/l10n_pe_ne_security.xml`
—**no** en `l10n_pe_ne_roles`, porque biller no depende de roles y debe seguir funcionando sin él;
mismo patrón que `group_l10n_pe_ne_anulacion` y su `_puede_anular` (`controllers/main.py:146`)—.
`l10n_pe_ne_roles` lo suma por `implied_ids` a supervisor y dueño, que es exactamente la fila
`veSeries: ('puedeSupervisar',)` que ya existe en `_VIS_MENU`.

El `has_group` va **dentro del método del modelo** (convención del repo: el método es la
autoridad), y se refleja en el controller. Cubre el CRUD de series **y** el de establecimientos,
que hoy está abierto de par en par.

### D7 — La caja gana `establecimiento_id` porque sin eso el segundo local no arranca

No es un extra: `CREATE UNIQUE INDEX ... ON (company_id) WHERE estado='abierta'` impide que San
Isidro abra caja mientras Miraflores tenga la suya. El índice pasa a
`(company_id, COALESCE(establecimiento_id, 0))` — con `COALESCE` obligatorio, porque en Postgres
`NULL != NULL` y sin él un tenant sin locales perdería la garantía de sesión única que hoy tiene.
El local se elige **al abrir** (una vez por turno), nunca por venta.

---

## Rebanadas

### S1 — Registro de series, CRUD con muro y cierre del agujero de establecimientos · **M**

- **Alcance:** modelo `l10n_pe_ne.serie` (D1) con `models.Constraint` estilo
  `l10n_pe_ne_establecimiento.py:19`: `unique(codigo, company_id)` y `_check_codigo_familia`
  (regex `^[FB][A-Z0-9]{3}$` coherente con `_l10n_pe_serie_prefix:1516`). `init()` con índice
  único parcial `(company_id, tipo_doc, COALESCE(establecimiento_id,0)) WHERE predeterminada`.
  ACL para `group_l10n_pe_ne_emisor` + `ir.rule` de compañía calcada de
  `rule_l10n_pe_ne_establecimiento_company`. Grupo `group_l10n_pe_ne_config_series` (D6).
  Métodos `l10n_pe_ne_serie_list` / `l10n_pe_ne_serie_upsert` / `l10n_pe_ne_serie_toggle` con
  el `has_group` dentro. Rutas: `GET /ne/api/series` (`main.py:546`) sirve registro + agregado
  con `origen: 'config'|'uso'`, nuevos `POST /ne/api/series` y
  `DELETE /ne/api/series/<int:rec_id>` (desactiva, nunca `unlink`), helper `_serie(uid)` calcado
  de `_estab` (`:180`). En establecimientos: `has_group` en `l10n_pe_ne_upsert` y
  `l10n_pe_ne_delete_establecimiento`, campo `active` y archivado en vez de `unlink()` cuando
  tenga series o emisiones (patrón de `l10n_pe_ne_eliminar_direccion:179`), `seriesCount` en
  `_l10n_pe_ne_dict`. `Series.tsx` pasa de tabla de solo lectura a CRUD agrupado por local;
  `Negocio.tsx` avisa antes de borrar.
- **Tests:** F001 en dos locales del mismo RUC → `ValidationError` con mensaje que EXPLICA la
  regla SUNAT; prefijo incoherente con `tipo_doc` → error; dos predeterminadas del mismo
  (local, tipo) → `IntegrityError`; cajero sin el grupo → `AccessError` en `serie_upsert`, en
  `l10n_pe_ne_upsert` y en el delete, **sin `sudo()` dentro del test**; con el registro vacío,
  `test_serie.py` entero verde sin tocar una línea.
- **Usable al terminar:** el dueño declara sus series por local y las ve; la emisión sigue
  comportándose como hoy.

### S2 — Resolución local↔serie en la emisión, con gate antes de quemar el correlativo · **M**

- **Alcance:** `_l10n_pe_ne_resolver_establecimiento(payload, origin)` con la cadena de D4,
  escrito en `vals` antes del `create`. `_l10n_pe_ne_serie_para(tipo, cod_estab, origin)` que
  consulta el registro (predeterminada activa → activa de menor `codigo` → fallback) y
  `_l10n_pe_ne_default_serie(tipo, origin=None, cod_estab=None)` ensanchado sin cambiar su
  respuesta cuando no hay registro. `_l10n_pe_ne_series_habilitadas` → UNIÓN, y su mensaje de
  error deja de apuntar solo al diario. Nuevo `_l10n_pe_check_serie_establecimiento`, invocado
  junto a `_l10n_pe_check_serie` (que ya corre ANTES de `_l10n_pe_ne_assign_numero`): serie
  registrada con local ⇒ el `codLocalEmisor` debe ser ese. Nuevo `_l10n_pe_ne_check_codigo` en
  `l10n_pe_ne.establecimiento` (4 dígitos, o `'0000'`, o existente en la compañía), usado desde
  `_l10n_pe_ne_quick_flags:6425` — con mensaje que distinga «no existe en tu catálogo» de «no
  está dado de alta ante SUNAT», que es trámite externo. Guarda en `write()`:
  `l10n_pe_ne_cod_establecimiento` inmutable una vez fijado `l10n_pe_ne_corr_emit` (escape
  `l10n_pe_ne_bypass_lock`, como la caja). Comentario-contrato de D2 e índice único parcial
  `ir_sequence (code, company_id) WHERE code LIKE 'l10n_pe.ne.cpe.%'`, con detección previa de
  duplicados: si los hay, loguear y **no** crear el índice, nunca tumbar el upgrade.
- **Tests:** local 0002 con F002 → `serie_emit == 'F002'` y `cabecera['codLocalEmisor'] == '0002'`;
  serie de otro local → `UserError` **y `ir.sequence.number_next_actual` intacto** (el test que
  evita quemar números fiscales); dos locales con la MISMA serie → correlativos 1 y 2, no 1 y 1;
  F001 y F002 alternadas → 1,2,3 independientes sin huecos; `'0009'` inexistente → `UserError`;
  local inmutable tras `_l10n_pe_ne_assign_numero`; invariante reusable: agrupar `account_move`
  por `(company_id, serie_emit, corr_emit)` con `corr_emit` no nulo → 0 grupos con `count > 1`.
- **Usable al terminar:** San Isidro emite F002-00000001 declarando `0002`.

### S3 — Notas y POS: el local deja de mentir · **S**

- **Alcance:** en el armado de `vals` de `quick_emit` para tipo 07/08,
  `l10n_pe_ne_cod_establecimiento = origin.l10n_pe_ne_cod_establecimiento or '0000'` y serie
  derivada del mismo local (factura F002 ⇒ NC FC02). El local de una nota **no es elegible**:
  es dato derivado, no elección. `copy=False` se mantiene (una copia de Odoo no debe arrastrar el
  local); la herencia es explícita en el emit, que es donde hay contexto. `Emitir.tsx`: el
  selector se pinta también en notas, deshabilitado y con el valor heredado (hoy lo esconde
  `!esNota`), se siembra desde la sesión de caja en vez de arrancar en `'0000'` (el comentario de
  `:598` deja de ser cierto) y envía SIEMPRE `codEstablecimiento` (hoy solo si `!== '0000'`,
  `:727`). `POS.tsx` **no gana selector** —la doctrina de los 3 toques de
  `decision-friccion-pyme.md` no admite un paso más por venta—: sigue sin enviar
  `codEstablecimiento` y el resolver lo saca de la caja; único cambio, un chip de solo lectura
  junto al total («Miraflores · B002») para que el cajero detecte antes de cobrar que abrió caja
  en el local equivocado.
- **Tests:** NC sobre factura de 0002 → FC02 + `codLocalEmisor 0002` (hoy FC01 + `0000`); ND ídem;
  POS con caja abierta en 0002 → declara 0002; POS sin caja → `'0000'`, igual que hoy.
- **Usable al terminar:** el reporte por local deja de sumar todas las devoluciones al domicilio
  fiscal.

### S4 — Caja por local: desbloquear el segundo local y cuadrar su arqueo · **L**

- **Alcance:** `establecimiento_id` en `l10n_pe_ne.caja.sesion`, elegido al abrir en
  `l10n_pe_ne_abrir_caja`; índice único parcial → `(company_id, COALESCE(establecimiento_id, 0))`
  (D7). `_l10n_pe_ne_ventas_sesion` filtra por local: sin eso el esperado de efectivo de Miraflores
  incluye las ventas de San Isidro, el conteo ciego SIEMPRE da diferencia y esa diferencia queda
  **congelada e inmutable** en `conteos_cierre` (`write` bloqueado, `:55-64`).
  `l10n_pe_ne_caja_actual` deja de devolver «la sesión de la compañía»: devuelve la sesión abierta
  por el propio usuario (`usuario_apertura_id`); si no abrió ninguna y hay exactamente una abierta,
  esa; si hay varias y no se puede decidir, `{abierta: false, requiereLocal: true, locales: [...]}`
  para que la SPA pregunte en vez de adivinar (elegir la primera cobraría en la caja equivocada y
  descuadraría dos arqueos a la vez). `_l10n_pe_ne_sesion_abierta` resuelve igual conservando su
  `SELECT ... FOR UPDATE`; `_l10n_pe_ne_sesion_dict` devuelve el local. `Caja.tsx`: selector al
  abrir y chip en el encabezado. Repaso de los adelantos CN-02 de `l10n_pe_ne_roles`.
- **Tests:** dos cajas abiertas simultáneas, una por local; la tercera del mismo local rebota;
  tenant sin locales sigue admitiendo UNA sola (el `COALESCE`); arqueo de cada local cuadra solo
  con sus ventas; sesión sin local (retrocompat) se comporta igual que hoy.
- **Usable al terminar:** los dos locales operan en paralelo y cada uno cierra su caja.

### S5 — Migración, visibilidad por local y decisión escrita · **M**

- **Alcance:** `__manifest__.py` `19.0.1.20.0 → 19.0.1.21.0` y
  `migrations/19.0.1.21.0/pre-migrate.py` (patrón de `19.0.1.10.0/pre-migrate.py`, con guarda «si
  no existe la columna, return»): `DROP INDEX l10n_pe_ne_caja_sesion_unica_abierta` —obligatorio,
  porque `CREATE UNIQUE INDEX IF NOT EXISTS` con el mismo nombre **no** recrea el índice viejo— y
  `UPDATE ... SET l10n_pe_ne_cod_establecimiento='0000' WHERE ... IS NULL`, defensivo y barato.
  **Sin sembrar series ni secuencias** (D5). Visibilidad: `l10n_pe_ne_quick_list` acepta
  `establecimiento=` como filtro y devuelve la columna; `l10n_pe_ne_comprobante_detalle` y
  `l10n_pe_ne_quick_result` incluyen el local (ticket 80mm y A4); `Comprobantes.tsx` suma columna
  y filtro — «cuánto vendió Miraflores» es la primera pregunta del día 1.
  `docs/procesos-negocio/decision-serie-por-local.md` con D1..D7 y el fuera de alcance; §12 nuevo
  en `plan-de-pruebas-maduros.md` (E1 configurar 0002 con F002/B002 · E2 emitir y verificar el XML ·
  E3 ✖ cajero crea establecimiento por API · E4 ✖ F002 en dos locales · E5 carrera de dos locales
  con series distintas, 5+5 · E6 carrera con la MISMA serie, 1..10 · E7 NC de 0002 · E8 dos cajas
  y dos arqueos · E9 ✖ `'0009'` · E10 retrocompat sin anexos · E11 upgrade sobre dump de
  producción · E12 POS desde el local 2) y su fila en el mapa de casos §1.
- **Tests:** el smoke de upgrade es el propio E11 (`-u l10n_pe_ne_biller` sobre copia del dump:
  0 errores y la primera emisión posterior continúa el correlativo, comparando Series antes/después).
- **Usable al terminar:** el dueño ve sus ventas separadas por local y el upgrade está probado.

---

## Retrocompatibilidad

Un tenant que hoy tiene una sola serie y no configura nada **no hace nada** y no nota nada:

- El registro `l10n_pe_ne.serie` arranca vacío; `_l10n_pe_ne_default_serie` devuelve
  F001/B001/FC01/FD01/BC01/BD01 carácter por carácter, igual que hoy.
- `_l10n_pe_ne_series_habilitadas` **suma**, nunca reemplaza: ninguna serie que valida hoy deja
  de validar. `account.journal.l10n_pe_ne_serie` no se toca ni se deprecia en esta tanda: queda
  como fallback vivo.
- Las `ir.sequence` existentes siguen sirviendo a sus series; no se siembra ni se reinicia
  ninguna. Una serie nueva por local crea la suya al primer uso.
- Todo `account.move` existente ya lleva `'0000'` (Odoo aplicó el default de columna al crear el
  campo): **cero migración de comprobantes**. La historia fiscal emitida no se reescribe.
- El índice de caja con `COALESCE(establecimiento_id, 0)` conserva la garantía de sesión única
  para quien no usa locales.
- `'0000'` sigue sintético con `id: 0`: `GuiaWizard.tsx` y `Negocio.tsx` no cambian.
- `GET /ne/api/series` conserva sus cinco claves actuales (`serie`, `tipoDoc`, `tipo`, `emitidos`,
  `ultimo`, `proximo`) y su paginación opt-in; lo nuevo (`establecimiento`, `activa`,
  `predeterminada`, `origen`) es aditivo.

---

## Riesgos

1. **Meter el establecimiento en el `code` de la secuencia** → dos locales con la misma serie
   emiten ambos `00000001` = duplicado fiscal que solo se corrige con baja. *Mitigación:*
   comentario-contrato en `_l10n_pe_ne_next_correlativo` + el test «dos locales, misma serie →
   1 y 2» + la aserción de invariante de duplicados en la suite.
2. **El índice único sobre `ir_sequence` de la ruta CPE falla al crearse** si la BD ya sufrió la
   carrera y tiene secuencias duplicadas. *Mitigación:* `init()` detecta duplicados primero,
   loguea y se salta la creación en vez de tumbar el upgrade; la limpieza es manual y decide qué
   contador sobrevive.
3. **Índice de caja sin `COALESCE`** → regresión silenciosa de dinero: un tenant sin locales
   podría abrir dos cajas. Y `CREATE ... IF NOT EXISTS` no recrea el índice viejo. *Mitigación:*
   `DROP INDEX` explícito en el `pre-migrate` + test de sesión única sin local.
4. **Arqueo contaminado:** si `_l10n_pe_ne_ventas_sesion` no filtra por local, cada cierre nace
   con diferencia y queda inmutable. *Mitigación:* va en la misma rebanada que el campo (S4),
   nunca después.
5. **QA-074 mal alimentado:** si el registro no entra en `_l10n_pe_ne_series_habilitadas`, la
   PRIMERA emisión de F002 muere en producción con «no está habilitada» y el mensaje apunta al
   diario, despistando a soporte. *Mitigación:* la unión y el mensaje nuevo van juntos en S2.
6. **Cambio observable en producción:** las NC/ND de tenants que ya emiten desde anexos pasan de
   declarar `'0000'` a declarar el local del afectado. Es la corrección buscada y el histórico no
   se reescribe, pero cambia la columna del RVIE. *Mitigación:* avisar al contador antes de
   desplegar y anotar el corte por fecha.
7. **Addon y SPA deben desplegarse juntos:** una SPA vieja no envía `codEstablecimiento` y su
   selector arranca en `'0000'`; con caja abierta en 0002 el backend nuevo declararía 0002 y la
   pantalla mostraría domicilio fiscal. *Mitigación:* despliegue conjunto (mismo BFF) y, como red,
   el chip de S3 hace visible el local antes de cobrar.
8. **Archivar un local con series activas** dejaría emitiendo un `codLocalEmisor` dado de baja
   ante SUNAT. *Mitigación:* desactivar en cascada sus series al archivar y avisar de los
   comprobantes en cola con ese local ya congelado.
9. **«Quiero F001 en los dos locales»** es la intuición del dueño y choca con la regla dura de
   SUNAT. *Mitigación:* el mensaje de error EXPLICA la regla, no solo la niega; si no, entra por
   soporte como bug.
10. **El auto-seed de la secuencia** escanea todos los moves de la serie sin límite ni orden
    (`search` + bucle Python, `:2050-2065`): con 300k comprobantes la primera emisión tras el
    upgrade se vuelve lenta. *Mitigación:* medir en E11; si duele, `read_group` con `max()`.
11. **Emisión masiva:** `l10n_pe_ne_lote` congela `payload_json` al importar y emite después; el
    local se resuelve AL EMITIR. *Mitigación:* documentarlo y, si el negocio lo pide, columna
    opcional de establecimiento en la plantilla.

---

## Fuera de alcance

- **Almacén por local.** El stock toma el primer `stock.warehouse` de la compañía. Separarlo
  obliga a tocar `stock_move_biller.py`, el POS entero y las guías. El negocio pidió numeración,
  no logística: el stock sigue siendo global y se documenta.
- **Usuarios por local como eje de permisos ni como preferencia** (D4). Ya hay decisión de
  producto del 2026-07-18 y `decision-escala-libre.md`; reabrirla multiplica la matriz de permisos.
- **Contabilidad por local:** ni diario, ni cuenta analítica, ni centro de costo por local. La
  `l10n_pe` no lo exige y arrastra el plan de cuentas.
- **Serie por local en la GRE.** SUNAT no exige serie por local en la guía; separarla sería
  decisión operativa, no obligación fiscal. El wizard ya obliga a elegir `cod_estab_partida`
  (`l10n_pe_ne_guia_remision.py:229`) y el correlativo por serie ya existe con el mismo patrón, así
  que añadirlo después son dos valores más en el `Selection` (`09`→T###, `31`→V###) y una llamada
  en `create` — pero exigiría el par T/V por local para no morir tarde en la guarda de prefijo.
  Lo único que sí entra ahora es la validación de existencia del código (S2, helper compartido).
- **Series de retención/percepción** (`account_payment.l10n_pe_ret_serie` / `l10n_pe_per_serie`).
  No entran al registro y siguen usando `_l10n_pe_ne_next_corr`
  (`account_payment_retencion.py:439`), que es un `max()+1` por `search`, sin `ir.sequence` ni
  lock. Es el eslabón débil de numeración del addon y se anota como deuda con nombre propio.
- **Materializar `'0000'`** (D3) y **sembrar el registro por migración** (D5).
- **Dar de alta el anexo ante SUNAT.** Es trámite externo (ficha RUC); el sistema valida contra su
  catálogo local y el mensaje de error lo dice, para que el dueño no crea que «ya está dado de
  alta» por haberlo creado en Negocio.

---

## Criterios de aceptación

1. Tenant sin anexos y sin registro: `_l10n_pe_ne_default_serie('01') == 'F001'`,
   `('03') == 'B001'`, NC de boleta `'BC01'`; **`test_serie.py` pasa entero sin modificarlo**.
2. Local 0002 con F002 predeterminada: emitir factura → `l10n_pe_ne_serie_emit == 'F002'`,
   `l10n_pe_ne_corr_emit == '00000001'` y `codLocalEmisor == '0002'` en el XML.
3. Diez emisiones alternadas entre F001 (0000) y F002 (0002): correlativos 1..5 en cada serie,
   sin huecos ni repetidos.
4. Dos locales configurados con la MISMA serie: 1..10 sin repetir. Consulta de invariante
   (agrupar por `company_id, serie_emit, corr_emit` con `corr_emit` no nulo) → 0 grupos con
   `count > 1`.
5. Emitir con una serie de otro local → `UserError` **y** `ir.sequence.number_next_actual` sin
   avanzar.
6. `codEstablecimiento = '0009'` (inexistente) → `UserError` con mensaje que distingue catálogo
   local de alta SUNAT.
7. NC de una factura emitida en 0002 → serie FC02 y `codLocalEmisor 0002` (hoy: FC01 y `0000`).
8. Cajero sin `group_l10n_pe_ne_config_series` → 403/`AccessError` en `POST /ne/api/series`,
   `POST /ne/api/establecimientos` y `DELETE /ne/api/establecimientos/<id>`, sin `sudo()` en el test.
9. Dos sesiones de caja abiertas simultáneas (una por local); una tercera del mismo local rebota;
   tenant sin locales sigue admitiendo una sola.
10. Cada arqueo cuadra exclusivamente con las ventas de su local (conteo ciego a cero diferencia
    con importes correctos).
11. `-u l10n_pe_ne_biller` sobre copia del dump de producción: 0 errores y la primera emisión
    posterior continúa el correlativo (Series antes/después idéntico).
12. `GET /ne/api/series` conserva sus claves actuales y `Series.tsx` funciona con el registro vacío.

---

## Esfuerzo total estimado

| Rebanada | Talla | Justificación |
|---|---|---|
| S1 — Registro de series, CRUD con muro y establecimientos | **M** | Modelo + constraints + índice parcial + seguridad (grupo nuevo en dos addons) + 3 rutas + reescritura de `Series.tsx` de tabla a CRUD. Nada de esto toca la emisión, por eso no es L. |
| S2 — Resolución local↔serie con gate | **M** | Cinco helpers en `account_move_biller.py` sobre código caliente (`quick_emit`, `_l10n_pe_check_serie`, `series_habilitadas`) + guarda de inmutabilidad + índice de `ir_sequence` con detección de duplicados. El motor de correlativo no se toca, y eso quita la mitad del riesgo. |
| S3 — Notas y POS | **S** | Dos líneas en el armado de `vals` de la nota, cuatro cambios puntuales en `Emitir.tsx` y un chip en `POS.tsx`. Barato porque S2 dejó el resolver hecho. |
| S4 — Caja por local | **L** | La pieza más cara: campo + migración de índice + cambio de semántica de `l10n_pe_ne_caja_actual` + filtrado del arqueo + `Caja.tsx` + repaso de los adelantos CN-02 de `l10n_pe_ne_roles`. Toca dinero y arqueo congelado, así que exige tests densos. |
| S5 — Migración, visibilidad y decisión escrita | **M** | `pre-migrate` corto pero delicado (DROP del índice), tres métodos de lectura + `Comprobantes.tsx`, y el §12 de pruebas manuales con doce escenarios incluida la doble carrera. |

**Total: 2 M + 1 S + 1 L + 1 M ≈ 13 días-persona (~3 semanas de una persona).**
Ruta crítica S1 → S2 → S3; **S4 es independiente y puede ir en paralelo**, pero es bloqueante
para que el segundo local opere de verdad: sin ella el dueño puede configurar F002 y emitirla,
pero San Isidro no puede abrir su caja. S5 cierra.

Última actualización: 2026-08-02

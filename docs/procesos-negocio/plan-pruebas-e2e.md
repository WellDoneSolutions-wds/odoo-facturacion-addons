# Plan de pruebas E2E — Procesos de negocio con roles (NE Express)

> Cubre CN-01 (mostrador) y CN-02 (taller), la segregación por rol, el blindaje de la máquina de
> estados (iter 7), la integridad del arqueo y la emisión fiscal. Pensado para ejecutarse en un
> entorno con la pila levantada; aquí (dev) no hay Odoo/node, así que este documento ES el entregable.

## 0. Arquitectura de la prueba

La regla del producto es **toda la lógica en Odoo; el BFF y la SPA solo pintan y llaman**. Por eso
el grueso del e2e se ejerce contra los endpoints `/ne/api/*` (capa donde vive la lógica). La SPA se
prueba aparte (solo render + wiring).

Pirámide de capas:

| Capa | Herramienta | Qué valida | Estado |
|---|---|---|---|
| Unit | `odoo-bin --test-enable` (TransactionCase) | lógica de modelo aislada | **ya existen** (ver §9) |
| Integración API | Odoo `HttpCase` → `/ne/api/*` con Bearer por rol | flujo real por el controller, gating por rol, JSON de respuesta | **hecho**: `roles/tests/test_cn01_http.py`, `test_cn02_http.py` (emisión doblada) |
| SPA | Playwright sobre el build de `web-bff` | render, navegación, que la SPA llama el endpoint correcto | a escribir |
| E2E fiscal | pila completa + facturador → **SUNAT beta** | emisión real, XML/CDR, montos | manual/staging |

**Qué NO se puede validar sin SUNAT beta:** el CDR (aceptación SUNAT), la firma del XML, el número
fiscal definitivo. Todo lo demás (estados, montos, arqueo, segregación) se valida en las capas 1-3.

## 1. Entorno y datos de prueba (setup)

1. Odoo 19 con `l10n_pe_ne_biller` + `l10n_pe_ne_roles` instalados y `-u` limpio.
   `odoo-bin -i l10n_pe_ne_biller,l10n_pe_ne_roles -u l10n_pe_ne_roles --stop-after-init`
2. Facturador (microservicio) apuntando a **SUNAT beta**, con serie/diario de ventas configurado.
3. **Tenant A** (RUC de prueba) provisionado (dueño = primer usuario).
4. Usuarios (creados por el dueño vía `POST /ne/api/equipo`):
   - `modal` — TODOS los roles (escala libre, negocio de 1 persona).
   - `vendedor` (ventas), `cajero` (caja), `despachador` (despacho), `operario` (taller),
     `supervisor` (supervisor), `contador` (contador, solo lectura). Cada uno **puro** (1 rol).
5. **Tenant B** (otro RUC) con su propio dueño — para aislamiento cross-tenant.
6. Datos: cliente con **RUC** (11 díg → factura) y cliente con **DNI** (8 díg → boleta); un producto
   **almacenable** (`type=consu`, storable) y un **servicio** (no storable); caja **abierta**.
7. Políticas/gates en **off** (default) salvo los escenarios de §6.

Convención de resultados: **OK** = 2xx + JSON esperado; **AccessError** = 403 con `{message}`;
**UserError/Validation** = 400 con `{message}` legible.

---

## 2. CN-01 · Mostrador (cotiza → cobra → despacho)

### 2.A Camino feliz segregado (3 personas)

| ID | Paso (endpoint · actor) | Esperado |
|---|---|---|
| CN01-A1 | `POST /ne/api/cotizaciones` · vendedor · 1 ítem afecto S/118 | OK, `estado=borrador` |
| CN01-A2 | `POST /ne/api/cotizaciones/<id>/aceptar` · vendedor | OK, `estado=aceptada` (salto de un clic D4) |
| CN01-A3 | `GET /ne/api/cotizaciones/cola-cobro` · cajero | la cotización aparece en la cola |
| CN01-A4 | `POST /ne/api/cotizaciones/<id>/cobrar-entregar` `{entregar:false}` · cajero | OK, `estado=convertida`, `comprobanteId` presente, `estadoDespacho=pendiente` |
| CN01-A5 | `GET /ne/api/despacho/cola` · despachador | aparece la cotización convertida pendiente |
| CN01-A6 | `POST /ne/api/despacho/<id>/entregar` `{receptorNombre, receptorDoc}` · despachador | OK, `estadoDespacho=entregado`, `despachador` seteado |

### 2.B Escala libre — 1 usuario con todos los roles

| ID | Paso | Esperado |
|---|---|---|
| CN01-B1 | `GET /ne/api/cotizaciones/<id>/acciones` · modal (aceptada) | incluye `cobrar-entregar` **y** `cobrar` |
| CN01-B2 | `cobrar-entregar` `{entregar:true}` · modal | OK en **un** commit: `estado=convertida` + `estadoDespacho=entregado` (`entregado=true`) |
| CN01-B3 | mismo flujo pero cajero SIN despacho | `cobrar-entregar` no aparece; solo `cobrar`; la orden cae a la cola de despacho |

### 2.C IGV — el comprobante sale por el monto correcto (**regresión crítica**)

| ID | Paso | Esperado |
|---|---|---|
| CN01-C1 | cotización línea afecto precio **118** → cobrar | comprobante `amount_total=118.00`, **valor venta=100.00**, **IGV=18.00** (la línea guarda CON IGV; `precioUnitario` del payload = 100). Si sale 118 de valor venta → **BUG** (+18%) |
| CN01-C2 | línea **no gravada** (`afectoIgv=false`) precio 50 | `precioUnitario=50`, `taxCode=9997`, sin IGV |
| CN01-C3 | cliente con **DNI** | `tipoDoc=03` (boleta); con **RUC** → `01` (factura) |

### 2.D Despacho P5 (en el acto)
- CN01-D1: solo se entrega lo `convertida` + `pendiente`; entregar una `aceptada` → UserError "cobrada".
- CN01-D2: cotización solo-servicios (sin producto almacenable) → tras cobrar, `estadoDespacho=no_aplica` (no abre despacho).

### 2.E Vigencia P6 (vinculante)
- CN01-E1: cotización con `fecha` vieja + `validez_dias` vencido → el cron `_l10n_pe_ne_cron_vencer` la marca `vencida`.
- CN01-E2: cobrar una `vencida` → UserError "venció / re-cotiza a precio vigente" (ningún grupo lo levanta).
- CN01-E3: dentro de plazo → cobra normal.

### 2.F Freeze H4 (convertida inmutable)
- CN01-F1: `update` de una convertida → UserError "no se puede editar".
- CN01-F2: `delete` de una convertida → UserError "No se puede borrar".
- CN01-F3: `set_estado('convertida')` a mano → UserError "No se puede pasar" (convertida solo por emisión).

### 2.G Negativos / segregación CN-01

| ID | Intento | Esperado |
|---|---|---|
| CN01-G1 | `aceptar` · cajero (sin ventas) | **AccessError** |
| CN01-G2 | `cobrar-entregar` · vendedor (sin caja) | **AccessError** |
| CN01-G3 | `entregar` · cajero (sin despacho) | **AccessError** |
| CN01-G4 | cajero puro: `GET cotizaciones` (list) | NO ve borradores (ir.rule: solo `aceptada`/`convertida`) |
| CN01-G5 | despachador puro: list | solo ve `convertida` |
| CN01-G6 | `rechazar` sin motivo · vendedor | UserError "motivo"; con motivo → `estado=rechazada` |

---

## 3. CN-02 · Taller (adelanto → cola → toma → saldo)

### 3.A Camino feliz segregado (recepción → caja → taller → caja)

| ID | Paso (endpoint · actor) | Esperado |
|---|---|---|
| CN02-A1 | `POST /ne/api/ordenes` `{cliente, items:[S/118], fechaPactada}` · vendedor/recepción | OK, `estado=borrador`, `enCola=true` (sin dueño), `saldo=118` |
| CN02-A2 | `POST /ne/api/ordenes/<id>/adelanto` `{monto:50, medio:"Yape"}` · cajero (caja abierta) | OK, `estado=encolada`, `adelanto=50`, `saldo=68`, aún `enCola=true` |
| CN02-A3 | `GET /ne/api/ordenes/cola` · operario | aparece la orden encolada |
| CN02-A4 | `POST /ne/api/ordenes/<id>/tomar` · operario | OK, `estado=en_proceso`, `responsable=operario` (toma atómica NULL→yo) |
| CN02-A5 | `POST /ne/api/ordenes/<id>/terminar` · operario | OK, `estado=terminada` |
| CN02-A6 | `GET /ne/api/ordenes/cola-saldo` · cajero | aparece la orden terminada |
| CN02-A7 | `POST /ne/api/ordenes/<id>/cobrar-saldo` `{medio:"Efectivo"}` · cajero | OK, `estado=entregada`, `comprobanteId` presente, `saldoCobrado=68` |

### 3.B Escala libre — 1 usuario
- CN02-B1: `modal` recorre A1→A7 completo sin atascarse; la cola colapsa a bandeja única.

### 3.C Arqueo — adelanto por medio, **sin doble conteo** (crítico)

| ID | Escenario | Esperado |
|---|---|---|
| CN02-C1 | adelanto S/50 Yape (sesión A); `GET /ne/api/caja/<A>/arqueo` | esperado por medio **Yape += 50**; `ingresos` genéricos (solo Efectivo) **= 0** (el adelanto no es ingreso genérico) |
| CN02-C2 | cerrar sesión A; abrir B; cobrar saldo S/68 Efectivo; arqueo B | esperado Efectivo **+= 68 solamente** (el comprobante final es por el TOTAL 118 pero sus `medios` registran solo el saldo 68 → no re-cuenta el adelanto) |
| CN02-C3 | suma A+B | **118 exactos**, sin hueco ni doble conteo |
| CN02-C4 | adelanto + saldo en la **misma** sesión | Yape 50 + Efectivo 68 = 118; cuadra |
| CN02-C5 | adelanto por **Tarjeta** (no Efectivo) | entra al esperado por Tarjeta, no infla Efectivo (regresión del hueco "solo cabe efectivo") |

### 3.D Toma de cola y carrera
- CN02-D1: `tomar` · cajero (sin taller) → **AccessError**.
- CN02-D2: una orden ya `en_proceso` → `tomar` de nuevo → UserError "No se puede pasar" (la arista encolada→en_proceso no aplica).
- CN02-D3 (**concurrencia**): dos operarios `tomar` la MISMA orden a la vez → uno gana (`responsable`), el otro recibe UserError (el `SELECT ... FOR UPDATE` de `_avanzar` serializa; el 2º re-lee `en_proceso`). Ver §8.

### 3.E Cobro de saldo + emisión
- CN02-E1: `cobrar-saldo` sobre una NO terminada → UserError "TERMINADA".
- CN02-E2: `cobrar-saldo` dos veces → la 2ª → UserError "ya se cobró" (anti-doble por `factura_final_id`).
- CN02-E3: IGV en el comprobante final = igual que CN01-C1 (118 → 100+18).

### 3.F Anulación
- CN02-F1: `anular` una borrador · cajero (con motivo) → `estado=anulada`.
- CN02-F2: `anular` una encolada/terminada · supervisor (con motivo) → `anulada` (reembolso del adelanto = retiro de caja manual, documentado).
- CN02-F3: `anular` sin motivo → UserError "motivo".

### 3.G Negativos / segregación CN-02

| ID | Intento | Esperado |
|---|---|---|
| CN02-G1 | `adelanto` · operario (sin caja) | **AccessError** |
| CN02-G2 | `adelanto` >= total | UserError "PARCIAL" |
| CN02-G3 | `adelanto` sin caja abierta | UserError "caja abierta" |
| CN02-G4 | operario puro: list ordenes | NO ve `borrador` (ir.rule: `encolada/en_proceso/terminada`) |
| CN02-G5 | cajero puro: list ordenes | NO ve `en_proceso` (trabajo del taller en curso) |
| CN02-G6 | borrar una orden con adelanto/comprobante | UserError "No se puede borrar … Anúlala" |

---

## 4. Blindaje de la máquina de estados (iter 7)

Requiere un cliente que hable **Odoo RPC directo** (XML-RPC/JSON-RPC) con credenciales de un usuario
real (no el BFF), para simular el bypass del BFF.

| ID | Intento (usuario real, su=False) | Esperado |
|---|---|---|
| BLD-1 | `orden.write({'estado':'terminada'})` RPC directo · cajero | **UserError** "no escribiéndolo directamente" (guard del mixin) |
| BLD-2 | `orden.create({'estado':'terminada', …})` RPC directo · cajero | **UserError** "nace en su estado inicial" |
| BLD-3 | `cotizacion.write({'estado':'convertida'})` RPC directo · cajero | **UserError** (mismo guard; sin comprobante huérfano) |
| BLD-4 | `orden.write({'estado':'entregada'})` con flag/su pero sin factura | **ValidationError** "entregada debe tener comprobante" (constraint, dispara aunque el guard se levante) |
| BLD-5 | migración/cron (su=True) escribe estado | **permitido** (modo sistema) |
| BLD-6 | flujo normal por método con nombre (tomar/cobrar…) | **permitido** (los métodos marcan el flag) — verifica que el blindaje NO rompe el camino feliz |

---

## 5. Roles, segregación y pentest (cross-cutting)

- ROL-1: `whoami`/`login` devuelven las capacidades correctas por rol (`puedeCotizar/Cobrar/Despachar/Taller/Supervisar`, `esContador`, `esDuenio`).
- ROL-2: **Aislamiento cross-tenant** — usuario de Tenant B: list de cotizaciones/ordenes/caja NO devuelve **nada** de Tenant A (regla global de compañía AND-eada). Intentar `browse` de un id de A → AccessError.
- ROL-3: contador (solo lectura) — ve comprobantes/cotizaciones/ordenes; cualquier `POST` de mutación → AccessError.
- **Alta de usuarios (V1-V7)** — reutilizar `test_alta_usuarios.py` como base y llevarlo a e2e:
  - V1: alta con login existente → mensaje genérico (no revela existencia).
  - V2: alta que supera `l10n_pe_ne_max_usuarios` → UserError cupo (con `FOR UPDATE`, sin TOCTOU).
  - V3: desactivar al último dueño → bloqueado.
  - V5: `provision_tenant` no deja roles prohibidos (duenio/system) por la red anti-escalada.
  - Un dueño de A NO puede tocar usuarios de B (choke point de compañía por inclusión).
- ROL-4: `set_roles` / `set_activo` / `reset-password` / `add_codueno` (este exige re-auth) por el dueño; un no-dueño → AccessError.

---

## 6. Caja / arqueo / gates

- CAJA-1: **conteo ciego** — sesión abierta: el `GET /ne/api/caja` NO sirve `esperado`/`esperadoTotal` (solo nombres de medios); al cerrar se revela.
- CAJA-2: cierre **inmutable** — editar una sesión cerrada → bloqueado.
- CAJA-3: **retiro > umbral** (`res.company.l10n_pe_ne_retiro_umbral`, default S/300) sin voucher → UserError; con voucher → OK.
- CAJA-4: gasto **append-only** — editar/borrar un gasto → bloqueado; contra-asiento `reversarGasto` → OK.
- GATE-1: encender un gate (p.ej. descuento) en `bloquea` + umbral; una operación sobre el umbral sin aprobación → queda `excepcion`/bloqueada; con `l10n_pe_ne_aprobar` de un supervisor → pasa; **auto-aprobación** registrada si el mismo usuario tiene el rol y el RUC no exige segregación.
- GATE-2: `exigir_segregacion=true` → `aprobar` por el mismo que operó → UserError (único punto con comparación de identidad, y solo si el RUC lo pidió).

---

## 7. Fiscal / SUNAT beta (e2e completo)

- FISC-1: emitir factura (RUC) y boleta (DNI) desde CN-01 y CN-02; el CDR de SUNAT beta = aceptado.
- FISC-2: montos del XML: `valorVenta`, `IGV`, `total` = los del arqueo/cotización (ver §2.C).
- FISC-3: `formaPago` Contado + `medios` internos (no van al XML SUNAT, sí quedan en el comprobante para el arqueo).
- FISC-4: rechazo SUNAT (dato inválido a propósito) → la venta sale del "esperado" en vivo; re-emisión cuenta.
- FISC-5: stock — al emitir con producto almacenable, el kardex se mueve (el bien sale al cobrar, no al CDR).

---

## 8. No-funcional / concurrencia

- NF-1: **toma concurrente** (CN02-D3) — 2 requests simultáneos → 1 OK, 1 UserError; nunca 2 dueños.
- NF-2: **doble-submit del cobro** (CN-01 `cobrar-entregar` / CN-02 `cobrar-saldo`) 2× en paralelo → 1 comprobante, la 2ª → UserError (anti-doble; verificar que no emite 2). *Nota: la ventana TOCTOU sin lock de fila persiste igual que el flujo de convert original — evaluar `FOR UPDATE` si aparece en carga.*
- NF-3: **cupo de usuarios** concurrente (V2) — 2 altas simultáneas al límite → solo 1 pasa (`FOR UPDATE` sobre `res_company`).
- NF-4: **doble apertura de caja** simultánea → 1 sesión abierta (índice único parcial).

---

## 9. Matriz de trazabilidad (unit existente → e2e)

| Escenario | Test unitario existente | Falta en e2e |
|---|---|---|
| CN-01 flujo, IGV, despacho, P6, freeze | `test_cotizacion_flujo.py` | camino completo por HTTP + SUNAT beta |
| CN-02 flujo, toma, adelanto por medio, guardas, segregación | `test_orden_trabajo.py` | HTTP + arqueo cruzando 2 sesiones + SUNAT |
| Escala libre (invariante: nadie se atasca, sin identidad) | `test_escala_libre.py` (auto-descubre modelos de flujo) | — (cubierto) |
| Blindaje write/create RPC | `test_orden_trabajo.py` (BLD-1/2/4) | BLD-3 (CN-01) + RPC directo real (XML-RPC) |
| Alta usuarios / pentest V1-V7 | `test_alta_usuarios.py` | HTTP `/ne/api/equipo/*` |
| Gates | `test_gates.py` | HTTP `/ne/api/politicas` + consumidor real |
| Caja / arqueo | `test_caja.py`, `test_caja_http.py` | adelanto (CN-02) en el arqueo |
| Anticipo (Vía A, futuro 0104) | `test_anticipo.py` | N/A (Vía B elegida) |
| Instalación / -u | `test_install.py` | -u en base con datos reales |

> Los tests unitarios **corren como root (su=True)**, así que NO ejercen la segregación por rol ni el
> gating por grupo del camino real — por eso la capa API (HttpCase con Bearer por rol) es
> imprescindible: es la única que prueba `with_user(uid)` + `has_group` de punta a punta.

## 10. Automatización recomendada

- **API (prioridad 1) — HECHO:** `roles/tests/test_cn01_http.py` y `test_cn02_http.py`. Cada una
  crea un usuario por rol (Bearer scoped key), recorre el camino feliz (segregado + escala libre) y
  los negativos de segregación golpeando `/ne/api/*`, y asevera el JSON. La emisión a SUNAT se
  DOBLA (`patch` de `requests.post`, molde `test_stock_emision.py`) para ejercer el fold completo
  sin el facturador. CN-02 además verifica el arqueo por `/ne/api/caja` (adelanto por su medio).
  Corre con `odoo-bin --test-enable -u l10n_pe_ne_roles`. Cubre lo que los unit (root, su=True) NO
  pueden: la segregación real por `with_user`+`has_group`. PENDIENTE de ejecutar en entorno con Odoo
  (aquí no hay); ver notas de riesgo abajo.

  Notas de ejecución (verificar en el primer run real): (a) el `patch` de `requests.post` debe
  aplicar al hilo del servidor de `HttpCase` (mismo proceso — debería, pero confirmar); (b) la
  lectura del `account.move` tras el request usa `env.invalidate_all()` + browse (visibilidad por
  cursor compartido del test-mode); (c) el arqueo asume que la sesión ABIERTA sí sirve `esperado`
  (cierto en esta rama; si se mergea la iter 2 'conteo ciego', ajustar a leer al cerrar).
- **SPA:** Playwright sobre `web-bff` (build): que `/ordenes` y `/cotizaciones` pinten las colas y
  disparen el endpoint correcto (mockear `/ne/api/*` o apuntar a un Odoo de staging).
- **Fiscal:** un smoke manual/nightly contra SUNAT beta (1 factura + 1 boleta por proceso).

## 11. Criterios de salida (gating para merge)

1. `odoo-bin --test-enable` verde para `l10n_pe_ne_roles` **y** `l10n_pe_ne_biller` (sin regresión).
2. `vite build` del `web-bff` sin errores de tipos.
3. Los HttpCase de §2/§3 (camino feliz + negativos de segregación) en verde.
4. §2.C (IGV) y §3.C (arqueo sin doble conteo) verdes — invariantes de dinero.
5. §4 (blindaje) verde — ningún usuario real cambia estado por RPC directo.
6. 1 smoke fiscal contra SUNAT beta aceptado por proceso.

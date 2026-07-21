# Guía de pruebas manuales — procesos con roles (CN-01, CN-02, Vía A/B, colas, caja, equipo)

Esta guía te lleva **escenario por escenario** para que pruebes todo tú mismo, con qué usuario
entrar, qué clickear y **qué debes ver** en cada paso. Cubre los dos casos ancla y todo lo
transversal (colas FIFO, caja con conteo ciego, políticas, equipo, concurrencia).

> **Regla de la casa que estás verificando**: toda la lógica vive en el addon de Odoo — la SPA
> solo pinta y llama. Cada botón que veas (o no veas) lo decidió el backend según tu rol, y toda
> acción prohibida rebota en el servidor aunque fuerces la URL.

---

## 1 · El entorno

### Si ya está arriba (esta máquina, ahora)

| Servicio | URL | Qué es |
|---|---|---|
| **SPA** | http://localhost:5173 | La app (Vite proxy → Odoo) — **aquí pruebas todo** |
| Odoo | http://localhost:8069 | Backend real (base `testdb`) |
| Mock facturador | :8090 | Recibe las emisiones y responde "FIRMADO-MOCK" |

> El mock hace que cada cobro llegue a estado **enviado** (y cuente en el arqueo). Sin él, la
> emisión queda en `error` — el flujo no se rompe, pero el arqueo la excluye. Lo real contra
> SUNAT beta es el smoke fiscal pendiente (necesita el facturador real).

### Desde cero (otra máquina)

Sigue el runbook `ne-express/apps/web-bff/e2e/README.md` (§1 levanta Odoo+Postgres en Docker,
instala addons, carga el Plan Contable PE y siembra usuarios+caja con `seed.py`; §2 levanta la
SPA). Además: `node e2e/stack/mock-facturador.cjs &` para el mock.

## 2 · Los usuarios de prueba

Todos sembrados por `seed.py`. **Un rol cada uno** — así ves la segregación de verdad:

| Usuario | Password | Rol | Lo usa el escenario |
|---|---|---|---|
| `vendedor1` | `e2e12345` | Ventas (recepción) | cotiza, crea órdenes |
| `cajero1` | `e2e12345` | Caja | cobra todo, arqueo, gastos |
| `operario1` / `operario2` | `e2e12345` | Taller | toman/terminan trabajos |
| `despachador1` | `e2e12345` | Despacho | entrega mercadería |
| `supervisor1` | `e2e12345` | Supervisor | anula, políticas |
| `contador1` | `e2e12345` | Contador | solo lectura |
| `duenio1` | `e2e12345` | Dueño | gestiona el equipo |
| `modal` | `modal1234` | **TODOS los roles** | escala libre: una persona lo hace todo |

**Tip**: usa una ventana normal + una de incógnito (o dos navegadores) para tener dos usuarios
a la vez y ver el handoff real entre sesiones.

---

## 3 · Escenarios

### E1 · CN-01 Mostrador — cotiza → paga en caja → recoge en despacho

*Tres personas, tres pantallas.*

1. **`vendedor1`** → **Cotizaciones** → *Nueva cotización*: cliente (busca `FERRETERIA` o crea
   uno con RUC), agrega el producto `FILT01` (es almacenable: eso abrirá el despacho) → Guardar.
2. Abre la cotización (fila) → **Aceptar** (el cliente dijo sí).
   ✔ *Verifica*: el vendedor NO tiene pestañas "Cola de cobro"/"Cola de despacho", y en el
   detalle NO hay botón de cobrar.
3. **`cajero1`** → **Cotizaciones** → pestaña **Cola de cobro** → ahí está la aceptada → abre y
   **Cobrar y emitir**.
   ✔ *Verifica*: el botón dice "Cobrar y **emitir**" (no "y entregar": este cajero no despacha).
   El estado pasa a **convertida** y el comprobante se emite (mock → `enviado`).
   ✔ *Negativo*: vuelve a intentar cobrar la misma → el backend rebota con "ya se convirtió".
4. **`despachador1`** → **Cotizaciones** → pestaña **Cola de despacho** → la convertida espera →
   **Marcar entregado** (pide el nombre de quien recoge).
   ✔ *Verifica*: sale de la cola; el estado de despacho queda **entregado**.
5. **Escala libre**: repite todo con `modal` — una sola persona ve TODAS las pestañas y botones y
   hace el flujo entero sin cambiar de sesión.

### E2 · CN-02 Taller completo — cotización → adelanto → cola → trabajo → recoge

*El escenario ancla del taller, entrando por cotización.*

1. **`vendedor1`** → **Cotizaciones** → crea una cotización de servicio (ej. "Mantenimiento de
   motor", S/236) → **Aceptar** → en el detalle: botón **Crear orden de taller**.
   ✔ *Verifica*: la orden nace con las líneas COPIADAS (no re-tipeaste nada) y el detalle ahora
   muestra "Orden de taller: OT-xxxxx" en vez del botón.
   ✔ *Negativo*: en una cotización en borrador NO existe ese botón (y por API rebota: "nace de
   una cotización ACEPTADA").
2. **`cajero1`** → **Órdenes de taller** → pestaña **Por cobrar adelanto** → ahí está el
   borrador → **Cobrar adelanto** → monto parcial (ej. S/100) y medio (ej. Yape) → *Cobrar y
   encolar*.
   ✔ *Verifica*: la orden pasa a **encolada**; el saldo (S/136) queda calculado.
   ✔ *Negativo*: intenta adelantar el total (S/236) → rebota: el adelanto es PARCIAL.
3. **`operario1`** → **Órdenes de taller** → **Cola de taller** → **Tomar** → la orden queda a tu
   nombre → **Terminar**.
   ✔ *Verifica*: el operario NO ve las pestañas de caja (ni Por cobrar adelanto ni Cobro de
   saldo).
4. **`cajero1`** → pestaña **Cobro de saldo** → **Cobrar saldo** → *Cobrar y entregar*.
   ✔ *Verifica*: cobra exactamente el saldo (S/136), emite el comprobante final por el TOTAL, y
   la orden queda **entregada**. En el ticket sale "Adelanto a cuenta: S/100".

### E3 · La cola es FIFO — por orden de llegada (del adelanto)

1. `vendedor1`: crea la orden **X** y después la orden **Y** (dos borradores).
2. `cajero1`: cobra el adelanto de **Y primero**, espera unos segundos, luego el de **X**.
3. `operario1`: **Cola de taller**.
   ✔ *Verifica*: **Y está ARRIBA de X** — manda cuándo se adelantó (llegada a la cola), no
   cuándo se creó el borrador ni el número de orden.

### E4 · Vía A — el adelanto FACTURADO (switch por empresa)

1. **`supervisor1`** → **Políticas de control** → enciende **"Facturar los adelantos (Vía A)"**.
   ✔ *Negativo*: `cajero1` no ve esa página (y por API el toggle le rebota 403).
2. `vendedor1` crea una orden (S/236) → **`cajero1`** cobra el adelanto (S/100, Yape).
   ✔ *Verifica*: el toast dice "**comprobante F001-xxxxx**" y la fila muestra el número — el
   adelanto EMITIÓ una factura/boleta real (doc. A).
3. Flujo normal (tomar → terminar → cobrar saldo).
   ✔ *Verifica*: el comprobante final REFERENCIA el anticipo y lo descuenta (en el XML:
   descuento global 04 + `sumTotalAnticipos`; en el ticket del micro: el doc regularizado).
4. **Anulación bloqueada**: crea otra orden, cobra su adelanto (emite doc. A), y como
   `supervisor1` intenta **Anular**.
   ✔ *Verifica*: rebota con "emite primero su nota de crédito" y el número del comprobante — una
   reversión fiscal jamás como efecto colateral.
5. **Apaga el switch** al terminar.
   ✔ *Verifica*: el siguiente adelanto vuelve a ser recibo interno (sin comprobante) — Vía B
   intacta.

### E5 · Segregación por rol — qué ve (y qué NO) cada uno

Entra con cada usuario y verifica su mundo:

| Usuario | Ve en el nav | NO ve | Detalle clave |
|---|---|---|---|
| `vendedor1` | Cotizaciones, Órdenes | Venta rápida, Caja | sin pestañas de cobro/despacho; su modal de Nueva orden NO tiene campos de adelanto |
| `cajero1` | + Venta rápida, Caja | Equipo, Políticas | Cotizaciones SÍ está en su nav (su cola vive adentro) |
| `operario1` | Órdenes | pestañas de caja | solo Tomar/Terminar |
| `despachador1` | Cotizaciones | pestaña de cobro | solo Cola de despacho |
| `contador1` | todo en lectura | ningún botón de acción | listas cargan, cero acciones |
| `supervisor1` | + Políticas | — | anula con motivo |
| `duenio1` | + Equipo | — | alta/roles/desactivar |

✔ *Negativo transversal*: pega a mano una URL que "no te toca" (ej. `/cotizaciones?tab=cobro`
como operario): la página carga pero **sin los botones**, y cualquier POST forzado rebota en el
backend con 403.

### E6 · Caja — conteo ciego, arqueo por medio, voucher, gastos

Con **`cajero1`**:

1. **Caja** → abre sesión con fondo (ej. S/500).
   ✔ *Verifica (conteo ciego)*: con la sesión ABIERTA la pantalla NO muestra cuánto "debería
   haber" — solo los medios con movimiento. El esperado se revela recién al cierre.
2. Cobra un adelanto por **Yape** (E2 paso 2).
   ✔ *Verifica*: "Yape" aparece como fila de conteo (el adelanto siembra SU medio) — pero sin
   monto (ciego). No es un "ingreso" genérico.
3. **Retiro** de S/350 sin voucher → rebota (umbral S/300 exige respaldo). Repite con
   N° de voucher + fecha + destino → pasa.
   ✔ *Negativo*: retiro de S/9,999 → "la caja solo tiene…".
4. **Gastos** → crea uno (S/25) → intenta editarlo → imposible (append-only) → **Reversar** →
   se crea el contra-asiento (−25). El original nunca se toca.
5. **Cierra la caja** contando lo que "hay".
   ✔ *Verifica*: recién ahora ves esperado/contado/diferencia por medio; el adelanto Yape está
   contado UNA sola vez; el arqueo queda congelado (inmutable).

### E7 · Equipo — alta y revocación EN VIVO

1. **`duenio1`** → **Equipo** → *Agregar persona*: nombre, login, rol **caja** → copia la
   contraseña temporal que te muestra.
2. En **incógnito**: entra con el nuevo usuario y su contraseña temporal.
   ✔ *Verifica*: te pide cambiarla; su nav es el de un cajero.
3. Desde `duenio1`: cámbiale el rol a **taller** → el nuevo refresca su pantalla.
   ✔ *Verifica*: su mundo cambió al instante (nav/pestañas de operario).
4. Desde `duenio1`: **desactívalo**.
   ✔ *Verifica (el check fuerte)*: en la ventana del nuevo, la SIGUIENTE acción lo saca a login
   — su token murió al instante, no "cuando expire".
   ✔ *Negativo*: `cajero1` no ve la página Equipo, y el alta por API le rebota 403.

### E8 · Concurrencia — dos personas, un botón

1. Deja una orden **encolada**. Abre `operario1` y `operario2` en dos ventanas, ambos mirando la
   Cola de taller.
2. **Tomar casi a la vez** en las dos ventanas.
   ✔ *Verifica*: exactamente UNO gana; el otro recibe un error claro ("ya la tomó…"). Nunca dos
   responsables.
3. Igual con el cobro: doble clic frenético en *Cobrar y entregar* → UN solo comprobante.

---

## 4 · Las suites automáticas (regresión completa)

Todo lo de arriba (y más: carreras reales en paralelo, arqueo entre sesiones, ir.rule por API)
corre solo. Desde `ne-express/apps/web-bff`:

```bash
node e2e/stack/mock-facturador.cjs &        # el mock (si no corre ya)
node e2e/api-roles.js                       # 47 checks: handoffs, 403s, carreras, arqueo
node e2e/api-integridad.js                  # 41 checks: caja, gastos, equipo, Vía A, puente+FIFO
npx playwright test -c e2e/playwright.config.ts   # 5 specs de navegador multi-sesión
```

Backend (desde `ne-express/apps/web-bff/e2e/stack`, ver README §3): la suite del addon corre
**106/106** en el Odoo real.

## 5 · Dejar el entorno limpio entre pruebas

- **Caja**: si un escenario la cerró, ábrela de nuevo (Caja → Abrir) — los cobros la necesitan.
- **Vía A**: apaga el switch en Políticas si lo encendiste (el default del negocio es Vía B).
- El mock del facturador debe seguir corriendo para que las emisiones lleguen a `enviado`.
- Los datos de prueba se acumulan sin problema; si quieres una base virgen, recrea `testdb` con
  el runbook §1.

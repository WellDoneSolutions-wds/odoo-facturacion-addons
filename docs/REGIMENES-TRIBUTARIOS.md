# Regímenes tributarios como eje de habilitación

**Evaluación de viabilidad — 7 de agosto de 2026**

Qué puede emitir cada emisor según su régimen, si el sistema puede saberlo, y cuánto cuesta hacerlo bien.

| | |
|---|---|
| **UIT vigente** | S/ 5,500 (DS 301-2025-EF) |
| **Regímenes** | NRUS · RER · RMT · Régimen General |
| **Repos afectados** | `odoo-facturacion-addons` · `ne-express` · `ms-ne-biller` (biller-pdf) |
| **Informe normativo completo** | [`regimenes-normativa.md`](regimenes-normativa.md) |

---

## Veredicto: sí es posible, y el sistema ya tiene casi toda la maquinaria

No hace falta inventar un mecanismo nuevo. El motor de gating por rubros, el motor de validación L1
y la configuración por tenant ya resuelven exactamente esta forma de problema. El régimen entra como
**un eje ortogonal**: campo propio en la compañía, muro propio en emisión, reglas propias en L1.

Pero el encuadre hay que corregirlo antes de empezar: **de los 4 regímenes, solo el NRUS cambia lo
que el sistema puede emitir.** RER, RMT y Régimen General emiten exactamente el mismo juego de
comprobantes. Lo que los separa —pagos a cuenta, libros, DJ anual, tasa de renta— es materia de
contabilidad, no de facturación, y hoy queda fuera del producto.

Eso parte el trabajo en dos mitades con valor muy distinto:

| Mitad | Qué es | Esfuerzo |
|---|---|---|
| **Gating (NRUS)** | Corrección obligatoria — evita que un cliente pierda su régimen | ~5 días (F1 + F2) |
| **Alertas de tope (los 4)** | El diferenciador real frente al mercado | ~5 días (F4) |

---

## 1. Lo que dice la norma, reducido a lo que nos afecta

Los topes no están todos en la misma unidad — y eso importa para el modelo de datos. NRUS y RER
tienen **topes en soles fijos** que no se reajustan; el RMT tiene el suyo **en UIT**, así que se
mueve cada enero.

| | **NRUS** | **RER** | **RMT** | **Régimen General** |
|---|---|---|---|---|
| **Norma base** | D. Leg. 937 | LIR arts. 117–124-A | D. Leg. 1269 | TUO LIR |
| **Tope de ingresos** | S/ 96,000 anual + mensual por categoría | S/ 525,000 | 1,700 UIT = S/ 9,350,000 (2026) | Sin tope |
| **Tope de compras** | S/ 96,000 | S/ 525,000 | Sin tope | Sin tope |
| **Activos fijos** | S/ 70,000 | S/ 126,000 | Sin límite | Sin límite |
| **Personal** | Sin parámetro | 10 por turno | Sin límite | Sin límite |
| **¿Emite factura?** | **NO — prohibido** | Sí | Sí | Sí |
| **IGV** | Dentro de la cuota fija — **el comprobante no discrimina impuesto** | 18 % | 18 % | 18 % |
| **Emisión electrónica** | Voluntaria | Obligatoria | Obligatoria | Obligatoria |
| **Actividades excluidas** | 13 supuestos (art. 3.2) | 11 supuestos **+ 7 CIIU** de servicios profesionales | Solo vinculación y no domiciliados | Ninguna |

> El RER excluye por CIIU a médicos, abogados, contadores, arquitectos, ingenieros, consultoras de
> software y asesoría empresarial. Un estudio contable en RER está mal acogido — y eso es detectable
> si el sistema conoce el rubro del tenant.

---

## 2. La matriz que el sistema tiene que respetar

Esta tabla es, literalmente, el contenido de la feature. Todo lo demás existe para hacerla cumplir.

| Documento | NRUS | RER | RMT | General |
|---|---|---|---|---|
| Factura (01) | ✗ | ✓ | ✓ | ✓ |
| Boleta (03) | ✓ | ✓ | ✓ | ✓ |
| Nota de crédito (07) | **?** sobre boleta | ✓ | ✓ | ✓ |
| Nota de débito (08) | **?** sobre boleta | ✓ | ✓ | ✓ |
| Liquidación de compra (04) | ✗ | ✓ | ✓ | ✓ |
| Guía de remisión (09 / 31) | ✓ | ✓ | ✓ | ✓ |
| Retención (20) | ✗ | Solo si SUNAT lo designó agente | ídem | ídem |
| Percepción (40) | ✗ | Solo si SUNAT lo designó agente | ídem | ídem |
| Exportación (tipOperacion 0200) | ✗ exporta con boleta | ✓ | ✓ | ✓ |
| Detracción SPOT en ventas | ✗ salvo Sector Público | ✓ | ✓ | ✓ |

### Por qué el bloqueo del NRUS no es cosmético

El art. 16.2 del D. Leg. 937 no dice que emitir una factura sea una infracción con multa: dice que
**determina la inclusión inmediata del contribuyente en el RMT o el Régimen General**, retroactiva
al mes de emisión del primer comprobante no autorizado.

Es decir: un cliente que aprieta el botón equivocado en nuestra SPA **pierde su régimen** y pasa a
contabilidad completa. Es el error más caro que un sistema de facturación le puede permitir cometer
a un usuario peruano, y hoy nuestro sistema lo permite sin decir nada.

---

## 3. Reglas a implementar

Ocho bloqueos duros y varios avisos. Los bloqueos son candidatos naturales a reglas del motor L1,
que ya devuelve `{code, campo, nivel, mensaje}` y del que el pre-flight de la SPA hereda gratis.

### Bloquear

| # | Regla | Fundamento |
|---|---|---|
| **B1** *(crítico)* | Factura (01) en NRUS | Art. 16.2 D. Leg. 937. Saca al contribuyente del régimen de forma retroactiva. |
| **B2** | Liquidación de compra (04) en NRUS | Otorga crédito fiscal → cae en la misma prohibición. |
| **B3 · B4** | Retención (20) y percepción (40) en NRUS | No declara IGV; no puede operar como agente. |
| **B5** | Exportación 0200 en NRUS | RCP art. 4: el NRUS que exporta emite boleta, no factura de exportación. Nuestro flujo de exportación no debe ofrecérsele. |
| **B6** | Discriminar IGV en el impreso NRUS | El comprobante no debe mostrar «Op. Gravada» ni «IGV 18 %». Es trabajo de Jasper, no de XML. |
| **B7** | Detracción en ventas NRUS | Operación exceptuada del SPOT cuando el comprobante no sustenta crédito fiscal. Excepción: adquirente del Sector Público. |
| **B8** *(aplica a todos)* | NC/ND por descuento sobre boleta | RCP art. 10.1.4. **No depende del régimen** y hoy falta en L1 — es una regla que ganamos de paso. |

### Advertir

| # | Alerta | Disparador |
|---|---|---|
| **A1** | «Estás al 80 % del tope anual» | Sobre S/ 96,000 · S/ 525,000 · 1,700 UIT según régimen. Umbral configurable. |
| **A2** | Tope mensual de categoría NRUS | S/ 5,000 (cat. 1) o S/ 8,000 (cat. 2). Recategorización o salida, según cuál se exceda. |
| **A3** *(el más valioso)* | Tope anual superado | NRUS y RER migran *desde ese mes*. El RMT **recalcula todo el ejercicio como Régimen General** — aviso enfático aparte. |
| **A4** | RMT cruza 300 UIT | El pago a cuenta salta de 1 % al art. 85 LIR. No cambia de régimen, sí de cálculo. |
| **A6** | Rubro incompatible con RER | Si el tenant tiene rubro de servicios profesionales, construcción o transporte de carga y está en RER, está mal acogido. |
| **A7** | Tope de *compras* del NRUS | S/ 96,000 en adquisiciones es causal de salida igual que los ingresos, y todo el mundo lo olvida. |
| **A9 · A10** | Cambio de régimen | Exigir fecha de vigencia; avisar que bajar a NRUS o RER solo surte efecto con la declaración de enero. |

---

## 4. Cómo engancha con lo que ya existe

El sistema tiene un motor de gating maduro —rubros y módulos— con catálogo en Python, persistencia
JSON por tenant, resolución con dependencias, muro server-side con bitácora y overrides manuales.
La tentación es meter el régimen ahí. **No se puede, y la razón es concreta.**

> `E01` (Factura) y `E02` (Boleta) están declarados en la tupla `NUCLEO` de `l10n_pe_ne_rubro.py` y
> son explícitamente inapagables: ni un override los desactiva. Sacar `E01` del núcleo sería un
> cambio de doctrina con impacto en el aplicador de rubros y en la bitácora.

**La salida correcta es reusar el *patrón*, no el *estado*:** campo propio en `res.company`,
resolución propia, y el mismo estilo de muro server-side con registro en
`l10n_pe_ne.rubro_auditoria`. Régimen y rubro son dos preguntas distintas —«qué impuestos pago» y
«a qué me dedico»— y deben cruzarse, no fusionarse.

Hay dos precedentes de diseño exactos ya en el código:

- `l10n_pe_ne_agente_percepcion` — un flag de condición SUNAT del emisor que viaja al front y
  habilita un control. El campo nuevo se declara igual.
- `l10n_pe_ne_uit` — un parámetro tributario configurable en la compañía. Convive con él.

También hay un punto de corte único y natural para el muro: en `account_move_api.py` ya vive la
validación *«FACTURA (01) exige RUC de 11 dígitos»*, que es literalmente la misma forma de regla.
El bloqueo por régimen va al lado.

---

## 5. El camino

Seis tramos. El orden importa: **F0 bloquea a F3**, y todo lo demás depende de F1.

### F0 — Cerrar las dos preguntas abiertas
*Bloqueante de F3 — **replanteado**, ver corrección*

> **Corrección al plan original.** Aquí decía que ambas preguntas se resolvían "probando contra beta
> hasta CDR 0". **Es falso para la primera.** El CPE no lleva el régimen en ninguna parte — ni en el
> UBL 2.1, ni en los XSD del SFS 2.4, ni en los XSL de validación. SUNAT **nunca** rechaza un
> comprobante por el régimen del emisor, así que beta aceptará una NC de un NRUS igual que de
> cualquier otro. Si el NRUS puede emitir NC/ND es una pregunta **legal**, no técnica: se resuelve
> con consulta a SUNAT o criterio contable profesional.
>
> La contrapartida es buena noticia: **equivocarse en el régimen no rompe la emisión.** Es un dato de
> UX y de validación L1, no de cumplimiento del XML. Por eso F1 y F2 pudieron avanzar sin esperar
> nada, y por eso la decisión sobre NC/ND fue **permisivo y documentado**.

- ¿El NRUS puede emitir nota de crédito y débito sobre boleta? → **pregunta legal**, no de beta.
  Decisión tomada: permitirlas.
- ¿Qué afectación de IGV y qué `TaxTotal` lleva una boleta NRUS en el XML? → esta sí se valida
  contra beta, pero "beta lo acepta" ≠ "es la representación legalmente correcta".

### F1 — El dato y el muro (aquí está el 80 % del valor)
*✅ **HECHO** — rama `feat/regimen-tributario`, commit `6e79a51`*

- `l10n_pe_ne_regimen` en `res.company`, con `regimen_fecha_inicio` y `nrus_categoria`.
  **Default vacío = legacy sin gating**, igual que hace el sistema de rubros: ausente ≠ prohibido.
- Publicar en `GET /ne/api/config` los `tiposPermitidos` **ya resueltos en el servidor** — la SPA no
  debe reimplementar la regla tributaria.
- Muro server-side en emisión, con rechazo registrado en la bitácora existente.
- Regla L1 declarativa para que el pre-flight avise antes de gastar un envío.
- Aceptar `regimen` en el provisioning del tenant, para que nazca ya restringido.

### F2 — Gating de UI (mecánico pero disperso)
*✅ **HECHO** — rama `feat/regimen-tributario`, commit `dbed0503`*

Los tipos de comprobante están **hardcodeados en 9 pantallas** y ninguna los pide al API. Hay que
centralizar y filtrar.

- Helper puro con la semántica que ya usa `moduloActivo`: sin lista ⇒ permitido.
- Emitir, POS, Series, Membresías, Comprobantes, Cotizaciones y Notas de venta. **Ojo con
  Cotizaciones y Notas de venta:** hoy *derivan* a factura sola si el cliente tiene RUC — un NRUS
  no puede.
- Selector de régimen en Configuración y paso nuevo en el wizard de bienvenida.

### F3 — NRUS pleno: el impreso y el XML
*3–4 días · biller-pdf · odoo*

- Suprimir «Op. Gravada» e «IGV» del A4 y del ticket cuando el emisor es NRUS. Es exactamente el
  mismo tipo de bug que ya corregimos en el A4 de exonerado/inafecto: **el XML puede estar bien y el
  impreso mentir**.
- Aplicar lo que devuelva F0 sobre la afectación en el XML.
- Restringir el catálogo de afectaciones sembrado para el tenant NRUS.

### F4 — Alertas de tope (el diferenciador)
*4–5 días · odoo · web-bff*

Esto es lo que casi ningún facturador peruano hace bien, y lo que le ahorra dinero real al cliente.

- Acumulador de ingresos del ejercicio. **Cuidado:** la norma habla de ingresos *netos* (brutos
  menos devoluciones, descuentos y bonificaciones) y excluye la venta de activos fijos. Sumar el
  importe total de los comprobantes da falsos positivos.
- La UIT debe ser un valor **con vigencia por ejercicio**, no una constante. Hoy existe el campo
  pero sin fecha.
- Los topes en soles (96,000 · 525,000 · 70,000 · 126,000) **no se derivan de la UIT**. Solo
  1,700 / 300 / 500 / 15 UIT se mueven.
- Alertas A1–A7, con el texto correcto por régimen. La asimetría es el detalle que lo hace bueno:
  NRUS y RER migran desde el mes; el RMT recalcula el ejercicio entero.

### F5 — Refinamientos
*Opcional*

- Cruce rubro ↔ exclusiones CIIU del RER (A6).
- Ventanas de tránsito entre regímenes y aviso de pérdida de saldo a favor o de arrastre de pérdidas
  al bajar de régimen.
- Informativo de libros obligatorios por tramo de UIT.

> **Estimación agregada:** F1 + F2 cubren el riesgo legal en ~5 días. F3 y F4 son lo que convierte
> el cumplimiento en producto.

---

## 6. Lo que todavía no sabemos

Dos puntos no se resolvieron en fuente oficial. Ninguno bloquea F1 ni F2 — bloquean el soporte pleno
de NRUS.

### ¿Puede el NRUS emitir nota de crédito o de débito?

El texto *derogado* del art. 16.2 las prohibía por nombre. El texto *vigente* (D. Leg. 1270) las
cambió por un criterio funcional: prohibido lo que otorgue crédito fiscal o sustente gasto. Una NC
que anula una boleta no otorga crédito fiscal, lo que sugiere que sería admisible. Las fuentes
secundarias que dicen lo contrario razonan sobre el texto derogado. **No hay informe SUNAT vigente
que lo resuelva.**

### ¿Qué afectación de IGV lleva una boleta NRUS en el XML?

El NRUS *es* contribuyente de IGV —el régimen comprende Renta, IGV e IPM— pero paga por cuota fija y
no puede discriminar el impuesto en el comprobante. Cómo se traduce eso al catálogo 07 (¿gravado con
tasa 0? ¿inafecto? ¿exonerado?) no está documentado en el SFS. **Hay que resolverlo contra el XSL de
boleta y validarlo en beta hasta CDR 0. No inventar un valor.**

### Un dato comercial que conviene tener presente

El NRUS **no está obligado** a emitir electrónicamente — para él es voluntario. El mercado de
emisores NRUS es, por definición, más chico que el de RER/RMT/RG. El valor de F1–F3 no está en
captar NRUS masivamente, sino en **no dejar que un cliente pierda su régimen por un botón mal
puesto**, y en poder decir que el sistema cubre los cuatro regímenes de verdad.

---

## 6-bis. Lo que aprendimos implementando F1 y F2

Cuatro trampas que una revisión adversarial cazó y que no estaban en este plan. Quedan aquí porque
cualquiera que toque este eje se las va a encontrar otra vez.

- **El bypass de admin en el muro era un agujero real.** `admin@ne.com` es miembro de
  `base.group_system` y es el login documentado de la SPA. Para 01/03/04 la regla L1 hace de red,
  pero **20/40 son `account.payment`**: no pasan por el motor L1 y el pre-flight les devuelve
  `{"findings": []}`, así que el muro era su única barrera. Criterio adoptado: **el bypass solo se
  justifica donde otra capa hace de red; donde no la hay, no debe haber bypass.**
- **Bloquear la familia de serie `F` bloquea las notas de crédito.** Una NC sobre una factura exige
  prefijo `F`, así que el guard ingenuo por letra dejaba a un negocio recién acogido al NRUS sin
  poder anular sus facturas anteriores — el daño exacto que quisimos evitar al permitir 07/08. Hay
  que mirar el `tipo_doc` de la serie, no la letra, y no bloquear la **edición** de series
  preexistentes.
- **Parchear solo el front deja puertas tapiadas.** Cotización y orden de trabajo derivan a factura
  **en el servidor** cuando el cliente tiene RUC. Con el muro puesto y sin degradación, la cola de
  cobro de un NRUS quedaba inoperable: no es una puerta trasera, es una operación legítima que el
  sistema impedía. Degradar a boleta, no reventar.
- **Invalidar un caché por call site no escala.** El primer intento olvidó 5 de 6 puntos de
  mutación. Se resolvió invalidando **por ruta**, en la capa del cliente HTTP.

## 7. Lo que no hay que hacer

- **No modelar la desaparición del RER y el RMT.** El MEF la propuso en el Marco Macroeconómico
  Multianual 2026-2029 y la prensa la dio por hecha, pero **no hay norma publicada**. Los cuatro
  regímenes están plenamente vigentes hoy. Con el campo fechado, una eventual fusión futura es una
  migración de datos, no un rediseño.
- **No trasladar reglas del NRUS al RER.** El límite de «un solo establecimiento» es del NRUS; el
  art. 118 LIR no lo tiene. Es un error que repiten muchos resúmenes contables.
- **No hardcodear la UIT** ni derivar de ella los topes que están en soles.
- **No implementar el gating apagando módulos de rubro.** Factura y boleta son núcleo inapagable por
  diseño; forzarlo rompe la doctrina del aplicador.
- **No resolver la regla tributaria en el front.** El servidor debe entregar la lista de tipos
  permitidos ya resuelta, igual que hoy entrega `puedeEditar`.
- **No comprar una API para saber el régimen.** Revisadas la Consulta RUC pública de SUNAT, su API
  oficial (OAuth2: solo GRE, SIRE y consulta de CPE), el Padrón Reducido, datos abiertos y nueve
  proveedores del mercado (migo.pe, decolecta, apis.net.pe, json.pe, apiperu…): **ninguno devuelve el
  régimen**. La única fuente que lo trae explícito es la Ficha RUC descargada desde SOL — manual y
  con tope de 3 al día. Un proveedor dice ofrecerlo pero exige la Clave SOL del usuario: descartable
  por seguridad. **El régimen se pregunta.**
- **No inferir "sin factura autorizada ⇒ NRUS".** Las autorizaciones de la ficha RUC son históricas y
  acumulativas: quien estuvo en Régimen General conserva FACTURA aunque hoy esté en NRUS. Y el campo
  F.806/816 es solo impresión física, así que un emisor 100 % electrónico lo tiene vacío. Sirve para
  **advertir**, nunca para decidir. La inferencia fiable es solo la inversa: factura autorizada ⇒ no
  es NRUS.

---

## Fuentes primarias

Todas verificadas el 7 de agosto de 2026 contra texto normativo publicado por SUNAT o El Peruano.
El desglose completo, con citas textuales y las incertidumbres declaradas, está en
[`regimenes-normativa.md`](regimenes-normativa.md).

- [D. Leg. 937 — Nuevo RUS](https://www.sunat.gob.pe/legislacion/rus/rus.pdf), texto actualizado al 18.2.2022, modificado por D. Leg. 1270
- [TUO Ley del Impuesto a la Renta, Cap. XV — RER](https://www.sunat.gob.pe/legislacion/renta/ley/capxv.pdf), arts. 117–124-A
- [D. Leg. 1269 — Régimen MYPE Tributario](https://www.sunat.gob.pe/legislacion/mypeIR/dl1269.pdf)
- [Reglamento de Comprobantes de Pago](https://www.sunat.gob.pe/legislacion/comprob/regla/capituloII.pdf), arts. 4 y 10
- [RS 183-2004/SUNAT — SPOT](https://www.sunat.gob.pe/legislacion/superin/2004/183.htm), arts. 8 y 13
- [D.S. 301-2025-EF — UIT 2026](https://busquedas.elperuano.pe/dispositivo/NL/2469116-1)
- [Obligados a emitir comprobantes electrónicos — SUNAT CPE](https://cpe.sunat.gob.pe/informacion_general/obligados_cpe)

> **Nota metodológica:** durante la investigación una página de orientación devolvió una escala de
> tasas del RMT de 8/14/17/20 % que **no existe en la norma**. La escala real es 10 % hasta 15 UIT y
> 29.5 % por el exceso. Todo dato numérico de este documento está anclado a texto legal, no a fichas
> de orientación.

---

*Evaluación preparada sobre el estado del código al 7 de agosto de 2026 — los tres repos en `main`,
sin trabajo pendiente. Ningún cambio aplicado: este documento es solo la decisión y la ruta.*

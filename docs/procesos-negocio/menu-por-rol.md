# Menú por rol — matriz de visibilidad de la SPA

> Revisada con negocio (2026-07-31). Implementación: claves `ve*` en el perfil
> (`l10n_pe_ne_roles/models/res_users.py`, `_VIS_MENU`) + gating H-3 del NAV en
> la SPA. **El menú es UX: el muro real sigue siendo el `has_group` de cada
> endpoint.** La SPA oculta un ítem solo si su cap llega en `false` explícito
> (ausente ≠ prohibido).
>
> **Quiénes quedan FUERA de la matriz** (sin claves `ve*` → menú operativo
> completo): el legacy solo-`emisor` (tenants pre-roles), el solo-`anulación`
> (implica emisor), el admin de plataforma (system o erp_manager) y el usuario
> sin ningún grupo NE (ve el menú, pero cada endpoint le rebota). Única
> excepción visual para todos ellos: Componentes UI (galería de desarrollo,
> gate duro `isAdmin`).

✓ = ve el ítem · 👁 = lo ve para consulta (las acciones las gatea el backend)

| Ítem (clave `ve*`) | Vendedor | Cajero | Operario | Despachador | Supervisor | Contador | Dueño |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| Inicio *(sin cap)* | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Venta rápida (`vePos`) | — | ✓ | — | — | — | — | — |
| Caja (`veCaja`) | — | ✓ | — | — | ✓ aprueba | — | ✓ |
| Nuevo comprobante (`veEmitir`, también gatea el CTA del topbar) | — | — | — | — | ✓ | — | ✓ |
| Comprobantes (`veComprobantes`) | 👁 | ✓ | — | 👁 | ✓ | 👁 | ✓ |
| Cotizaciones (`veCotizaciones`) | ✓ | ✓ bandeja | — | ✓ cola de despacho (CN-01) | ✓ | — | ✓ |
| Órdenes de taller (`veOrdenes`) | ✓ crea | ✓ cobra | ✓ su cola | ✓ entrega | ✓ | — | ✓ |
| Guías de remisión (`veGuias`) | — | — | — | ✓ | ✓ | — | ✓ |
| Emisión masiva (`veMasivo`) | — | — | — | — | ✓ | — | ✓ |
| Análisis de ventas (`veAnalisis`) | — | — | — | — | ✓ | 👁 | ✓ |
| Libros electrónicos (`veLibros`) | — | — | — | — | ✓ | ✓ | ✓ |
| Partes vinculadas (`veVinculadas`) | — | — | — | — | ✓ | 👁 | ✓ |
| Centro de descargas (`veDescargas`) | — | — | — | — | ✓ | ✓ | ✓ |
| Clientes (`veClientes`) | ✓ | ✓ | — | — | ✓ | — | ✓ |
| Productos (`veProductos`) | 👁 | 👁 | — | 👁 | ✓ | — | ✓ |
| Compras (`veCompras`) | — | — | — | ✓ recepciona | ✓ aprueba | — | ✓ |
| Gastos (`veGastos`) | — | ✓ registra | — | — | ✓ aprueba | — | ✓ |
| Series (`veSeries`) | — | — | — | — | ✓ configura | — | ✓ configura |
| Frecuentes (`veFrecuentes`) | — | — | — | ✓ | ✓ | — | ✓ |
| Datos del negocio (`veNegocio`) | — | — | — | — | 👁 | — | ✓ |
| Equipo *(`puedeGestionarEquipo`)* | — | — | — | — | — | — | ✓ |
| Políticas de control *(`puedeSupervisar`)* | — | — | — | — | ✓ | — | ✓ |
| Componentes UI *(`isAdmin`)* | — | — | — | — | — | — | — |

Capacidades que **no** son ítems de menú (gatean botones dentro de una pantalla que
ya se ve):

| Capacidad | Qué habilita | Vendedor | Cajero | Operario | Despachador | Supervisor | Contador | Dueño |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| `puedeConfigSeries` | Declarar/editar/desactivar series por local y dar de alta o archivar establecimientos anexos (Series + Negocio) | — | — | — | — | ✓ | — | ✓ |
| `puedeAnular` | Comunicación de baja / resumen de anulación (Comprobantes) | \* | \* | \* | \* | \* | \* | \* |

\* `puedeAnular` no lo implica ningún rol: es un rol **aparte** (`anulacion`) que el
dueño otorga a quien decida, precisamente porque la baja ante SUNAT es irreversible.

Notas de diseño:

- **`puedeConfigSeries` cambia quién ESCRIBE, no quién VE** (fase Series por sucursal,
  ver [decision-serie-por-local.md](decision-serie-por-local.md)). El alta y la baja de
  establecimientos estaban abiertas a cualquier emisor, y desde que el local determina
  la serie eso era «un cajero puede renumerar la empresa». Series y Negocio se siguen
  viendo igual —el legacy solo-`emisor` conserva su pantalla de consulta y el `GET` de
  series—; lo que se cerró es la escritura, en el modelo.

- **Dueño = supervisor por implicación** (`implied_ids`): la matriz no lo lista;
  hereda todo lo del supervisor y suma Equipo. `modal` (usuario e2e con los 5
  roles operativos) ve la unión real: lo del supervisor **más Venta rápida**
  (`vePos`, por su rol caja — el supervisor puro no la tiene).
- **Cotizaciones y Órdenes siguen siendo páginas multi-rol**: el gating fino es
  por PESTAÑA adentro (hallazgo del e2e: capar la página entera dejó al cajero
  sin su bandeja de cobro). `veCotizaciones`/`veOrdenes` incluyen a todos los
  roles con una bandeja dentro.
- **"Nuevo comprobante" y "Emisión masiva" → supervisor** por decisión de
  negocio. Si algún tenant usa el rol `emisor` como facturador dedicado, ese
  usuario es legacy (sin roles NE) y conserva el menú completo; si se le asigna
  un rol NE, pasa a la matriz.
- **Componentes UI** es una galería de desarrollo: solo admin de plataforma.

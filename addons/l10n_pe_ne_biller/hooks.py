def set_uom_precision_3(env):
    """Sube la precisión de cantidad de unidad de medida a 3 decimales.

    Las verticales de venta al peso / balanza (KGM · GRM · TNE) y ferretería (MTR · MTK) venden
    por una cantidad fraccionaria de hasta 3 decimales (18.375 kg, 12.5 m). El comprobante ya la
    emite exacta —`account.move.line.quantity` está override a digits (16, 3), ver QA-020— pero el
    KARDEX movía el stock a la precisión por defecto de Odoo (2): vender 18.375 kg descontaba 18.38
    y el inventario se iba en falso 5 g por venta. Con 3 el movimiento de stock queda alineado con
    la emisión — lo que separa estas verticales de «solo emite» a «maduras».

    El record core `uom.decimal_product_uom` es noupdate, así que un data XML no lo pisa: se ajusta
    por código (idempotente, solo sube, nunca baja). Odoo 19 lo llama «Product Unit»; se cubre el
    nombre viejo «Product Unit of Measure» por si corre en una base anterior.
    """
    prec = env['decimal.precision'].search(
        [('name', 'in', ('Product Unit', 'Product Unit of Measure'))], limit=1)
    if prec and prec.digits < 3:
        prec.digits = 3


def post_init_hook(env):
    """Al INSTALAR el addon en una BD nueva, deja al admin (base.user_admin)
    dentro del grupo 'Anulación de comprobantes NE Express' para que pueda operar
    la API /ne/api sin sembrar ningún usuario ni credencial por defecto. Ese grupo
    implica el de 'Emisor NE Express', así que con asignarlo basta: el admin queda
    pudiendo emitir Y anular, como antes de separarlos.

    Install-only (registrado como post_init_hook): un -u sobre una BD ya
    existente NO lo re-ejecuta, así que los tenants viejos no se ven afectados
    (a ellos los cubre migrations/19.0.1.4.0).
    (4, id) añade el grupo sin quitarle al admin ninguno de los suyos; idempotente.
    """
    admin = env.ref('base.user_admin', raise_if_not_found=False)
    group = env.ref('l10n_pe_ne_biller.group_l10n_pe_ne_anulacion',
                    raise_if_not_found=False)
    if admin and group:
        admin.write({'group_ids': [(4, group.id)]})
    set_uom_precision_3(env)

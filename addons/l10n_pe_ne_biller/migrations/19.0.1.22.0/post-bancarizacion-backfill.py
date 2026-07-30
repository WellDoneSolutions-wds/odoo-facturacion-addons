from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Backfill de l10n_pe_ne_bancarizacion (Ley 28194) en comprobantes YA emitidos.

    El estado se deriva al emitir (quick_emit -> _l10n_pe_ne_bancarizacion_estado), así que las
    facturas emitidas ANTES de esta feature quedaron con el default 'no_aplica' y no mostraban el
    badge 'Bancarizar' en el listado aunque fueran >= S/2000. Se recalcula el estado para toda venta
    con estado de facturador. El deriver ya filtra internamente (boleta/NC/ND/<umbral -> no_aplica),
    así que basta con recorrerlas todas y guardar lo que devuelve."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    moves = env["account.move"].search([
        ("move_type", "in", ("out_invoice", "out_refund")),
        ("l10n_pe_biller_state", "!=", False),
    ])
    for move in moves:
        estado = move._l10n_pe_ne_bancarizacion_estado()
        if move.l10n_pe_ne_bancarizacion != estado:
            move.l10n_pe_ne_bancarizacion = estado

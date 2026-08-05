from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """DATOS DE PAGO en la representación impresa: el biller ahora dibuja las cuentas bancarias /
    CCI / Yape-Plin del emisor en el pie de los comprobantes (encima del QR), el mismo dato que ya
    mostraba la cotización. El PDF impreso se cachea por `pdf_ver` (ver
    account_move_pdf.py::_l10n_pe_get_pdf_attachment); sin subir esa versión, los comprobantes ya
    emitidos seguirían sirviendo su PDF viejo cacheado —SIN el bloque—, así que el cambio de
    template no se vería en facturas/boletas ya generadas. Se bumpea `pdf_ver` para que cada
    comprobante regenere su PDF con el template nuevo la próxima vez que se descargue/imprima."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['ir.config_parameter'].sudo().set_param('l10n_pe_ne_biller.pdf_ver', 'datos-pago-v1')

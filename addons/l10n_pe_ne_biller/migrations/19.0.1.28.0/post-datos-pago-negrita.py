from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Tercer bump de `pdf_ver`: ahora el bloque DATOS DE PAGO resalta el nombre del banco en
    negrita (styled text de Jasper / HTML en la cotización). El contenido del param datosPago
    cambió (ahora trae <b>...</b>), así que los comprobantes cacheados bajo datos-pago-v2 (texto
    plano) deben regenerarse para mostrar el nuevo formato. Ver
    account_move_pdf.py::_l10n_pe_get_pdf_attachment (cache por pdf_ver) y
    res_company._l10n_pe_ne_datos_pago_marcado."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['ir.config_parameter'].sudo().set_param('l10n_pe_ne_biller.pdf_ver', 'datos-pago-v3')

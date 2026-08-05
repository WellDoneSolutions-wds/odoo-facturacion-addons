from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Segundo bump de `pdf_ver` para el bloque DATOS DE PAGO. El primero (19.0.1.26.0,
    datos-pago-v1) forzó la regeneración, pero en el deploy async el worker adjuntaba un PDF
    pre-generado SIN el bloque (el mensaje de la cola no lleva los datos de pago) y lo etiquetaba
    con la versión vigente → la descarga servía ese PDF incompleto. El fix
    (_l10n_pe_attach_async_pdf ahora baila si la compañía tiene datos de pago, difiriendo a la ruta
    síncrona que sí los manda) evita cachear el PDF malo de aquí en más; este bump invalida los que
    ya se cachearon mal bajo datos-pago-v1 para que se regeneren por la ruta síncrona."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['ir.config_parameter'].sudo().set_param('l10n_pe_ne_biller.pdf_ver', 'datos-pago-v2')

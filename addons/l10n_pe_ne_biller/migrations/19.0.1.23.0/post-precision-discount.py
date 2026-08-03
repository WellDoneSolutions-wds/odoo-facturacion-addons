from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Sube la precisión del descuento (%) a 6 decimales en tenants ya instalados (el post_init_hook
    solo corre en instalación nueva). Sin esto, un descuento tecleado en S/ (monto fijo) cuyo % no es
    redondo — p.ej. S/10 sobre 760 = 1.315789% — se truncaba a 1.32 y el total emitido salía 749.97 en
    vez de 750.00. Ver l10n_pe_ne_biller/hooks.py::set_discount_precision_6."""
    from odoo.addons.l10n_pe_ne_biller.hooks import set_discount_precision_6
    env = api.Environment(cr, SUPERUSER_ID, {})
    set_discount_precision_6(env)

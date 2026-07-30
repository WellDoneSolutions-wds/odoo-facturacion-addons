from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Sube la precisión de cantidad de UoM a 3 decimales en tenants ya instalados (el
    post_init_hook solo corre en instalación nueva). Necesario para que el kardex de las
    verticales peso/ferretería descuente el peso/medida exacto (18.375 kg, no 18.38).
    Ver l10n_pe_ne_biller/hooks.py::set_uom_precision_3."""
    from odoo.addons.l10n_pe_ne_biller.hooks import set_uom_precision_3
    env = api.Environment(cr, SUPERUSER_ID, {})
    set_uom_precision_3(env)

{
    'name': 'Perú - Búsqueda de cliente por DNI/RUC',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Localizations',
    'summary': 'Busca y crea clientes automáticamente desde una API externa '
               '(p. ej. DynamoDB) usando DNI o RUC, al registrar una factura.',
    'description': """
Búsqueda de cliente por DNI/RUC
===============================

Al registrar una factura, si el cliente no existe en Odoo, este módulo permite
buscarlo por número de documento (DNI/RUC) en una API externa (que puede estar
respaldada por DynamoDB u otra base de datos). Si lo encuentra, crea el
contacto automáticamente y lo selecciona en la factura.

Configuración: Ajustes → Contabilidad → "Búsqueda de cliente por DNI/RUC".
""",
    'author': 'Kenyi',
    'license': 'LGPL-3',
    'depends': ['account', 'l10n_latam_base', 'l10n_pe'],
    'data': [
        'security/ir.model.access.csv',
        'wizard/partner_lookup_wizard_views.xml',
        'views/account_move_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'l10n_pe_partner_lookup/static/src/js/partner_lookup_many2one.js',
            'l10n_pe_partner_lookup/static/src/xml/partner_lookup_many2one.xml',
        ],
    },
    'installable': True,
}

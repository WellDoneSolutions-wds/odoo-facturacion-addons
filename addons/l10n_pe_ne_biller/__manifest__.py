{
    'name': 'Facturación Electrónica PE - Conector ms-ne-biller',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Localizations/EDI',
    'summary': 'Envía facturas a SUNAT vía el microservicio ms-ne-biller (formato SFS).',
    'depends': ['l10n_pe', 'account', 'uom'],
    'data': [
        'security/l10n_pe_ne_security.xml',
        'security/ir.model.access.csv',
        'data/l10n_pe_ne_data.xml',
        'data/l10n_pe_ne_emisor_user.xml',
        'data/l10n_pe_ne_cron.xml',
        'views/account_move_views.xml',
        'views/account_journal_views.xml',
        'views/account_payment_views.xml',
        'views/res_company_views.xml',
        'views/uom_views.xml',
        'views/l10n_pe_ne_report_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'l10n_pe_ne_biller/static/src/js/biller_live_statusbar.js',
        ],
    },
    'license': 'LGPL-3',
    'application': False,
    'installable': True,
}

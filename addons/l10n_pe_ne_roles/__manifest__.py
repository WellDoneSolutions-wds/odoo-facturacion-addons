{
    'name': 'NE Express — Roles y flujos de trabajo',
    'version': '19.0.1.4.0',
    'post_init_hook': 'post_init_hook',
    'category': 'Accounting/Localizations/EDI',
    'summary': 'Roles por rol funcional y mixin de flujo (estado + cola + auditoría) para los '
               'procesos de negocio de NE Express. La lógica vive aquí; la SPA solo la pinta.',
    # Depende del facturador: lo EXTIENDE con _inherit/super(), nunca lo reescribe.
    'depends': ['l10n_pe_ne_biller'],
    # Iteración 3: H-2 grupos (privilege + roles hermanos + duenio) y su ACL de contador.
    # El XML de grupos va ANTES que el csv (el csv referencia group_l10n_pe_ne_contador).
    # OJO: NO hay ni una fila de ACL sobre res.users — la gestión de usuarios (H-4) es por
    # métodos sudo() con whitelist (ver docs/procesos-negocio/decision-alta-usuarios.md).
    'data': [
        'security/l10n_pe_ne_roles_security.xml',
        'security/ir.model.access.csv',
        'security/l10n_pe_ne_cn01_security.xml',
        'security/l10n_pe_ne_cn02_security.xml',
        'data/l10n_pe_ne_cn01_cron.xml',
    ],
    'license': 'LGPL-3',
    'application': False,
    'installable': True,
}

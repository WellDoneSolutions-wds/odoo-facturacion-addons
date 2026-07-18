{
    'name': 'NE Express — Roles y flujos de trabajo',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Localizations/EDI',
    'summary': 'Roles por rol funcional y mixin de flujo (estado + cola + auditoría) para los '
               'procesos de negocio de NE Express. La lógica vive aquí; la SPA solo la pinta.',
    # Depende del facturador: lo EXTIENDE con _inherit/super(), nunca lo reescribe.
    'depends': ['l10n_pe_ne_biller'],
    # Iteración 1 = solo el mixin (cimiento). Sin grupos ni ACL todavía: los grupos
    # hermanos (H-2), el perfil (H-3) y el alta por el dueño (H-4) llegan en la
    # iteración 3. A propósito NO se declara 'security/ir.model.access.csv': cuando
    # llegue H-4, la gestión de usuarios será un método sudo() con whitelist, jamás
    # una fila de ACL sobre res.users (ver docs/procesos-negocio/decision-alta-usuarios.md).
    'data': [],
    'license': 'LGPL-3',
    'application': False,
    'installable': True,
}

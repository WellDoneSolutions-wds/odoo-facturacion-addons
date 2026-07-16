def post_init_hook(env):
    """Al INSTALAR el addon en una BD nueva, deja al admin (base.user_admin)
    dentro del grupo 'Anulación de comprobantes NE Express' para que pueda operar
    la API /ne/api sin sembrar ningún usuario ni credencial por defecto. Ese grupo
    implica el de 'Emisor NE Express', así que con asignarlo basta: el admin queda
    pudiendo emitir Y anular, como antes de separarlos.

    Install-only (registrado como post_init_hook): un -u sobre una BD ya
    existente NO lo re-ejecuta, así que los tenants viejos no se ven afectados
    (a ellos los cubre migrations/19.0.1.4.0).
    (4, id) añade el grupo sin quitarle al admin ninguno de los suyos; idempotente.
    """
    admin = env.ref('base.user_admin', raise_if_not_found=False)
    group = env.ref('l10n_pe_ne_biller.group_l10n_pe_ne_anulacion',
                    raise_if_not_found=False)
    if admin and group:
        admin.write({'group_ids': [(4, group.id)]})

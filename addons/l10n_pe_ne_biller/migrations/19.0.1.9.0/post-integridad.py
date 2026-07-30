def migrate(cr, version):
    """D-3 (integridad): el campo nuevo l10n_pe_ne.gasto.usuario_id tiene default env.user, así
    que al hacer -u Odoo rellena TODOS los gastos históricos con el usuario que corre el upgrade
    (el superusuario), no con su autor real. Se corrige apuntándolos a su create_uid, que es el
    autor verdadero (todos los gastos existentes preceden al campo). Los gastos nuevos, creados
    después de este upgrade, reciben su usuario_id correcto en el create.

    Post (no pre): la columna la crea el ORM al cargar el modelo, que corre antes.
    """
    cr.execute("""
        UPDATE l10n_pe_ne_gasto
        SET usuario_id = create_uid
        WHERE create_uid IS NOT NULL
    """)

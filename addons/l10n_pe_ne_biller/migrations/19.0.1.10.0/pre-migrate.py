def migrate(cr, version):
    """El anticipo simple (main) guardaba el anticipo aplicado en 4 columnas escalares
    (`l10n_pe_ne_anticipo_total/doc/tipo/origen_id`). `a648145` las reemplazó por la lista JSON
    `l10n_pe_ne_anticipos` SIN bump de versión de módulo, así que en BDs ya desplegadas el `-u`
    que instaló ese cambio dejó ambas cosas: las columnas escalares siguen físicamente en la
    tabla (Odoo no las dropea al quitar el field) CON datos, y `l10n_pe_ne_anticipos` quedó NULL
    para esas filas — el saldo y `anticipos_pendientes` solo leen la lista JSON, así que esos
    anticipos "resucitan" con saldo completo (riesgo de doble deducción fiscal).

    Pre (no post): hay que poblar `l10n_pe_ne_anticipos` ANTES de que corra cualquier lógica de
    negocio del módulo actualizado (computes, hooks) que ya asuma que la lista es la única fuente
    de verdad.
    """
    cr.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name='account_move'
          AND column_name='l10n_pe_ne_anticipo_total'
    """)
    if not cr.fetchone():
        return  # ya migrado o BD nueva (nunca tuvo las columnas escalares)
    # El pre-migrate corre ANTES de que el ORM cree las columnas del módulo, así que
    # `l10n_pe_ne_anticipos` puede no existir todavía (BD que viene de main 19.0.1.9.0,
    # donde solo existen las columnas escalares). Crearla aquí es inofensivo si ya existe;
    # el ORM la reconoce igual en el setup posterior.
    cr.execute("""
        ALTER TABLE account_move ADD COLUMN IF NOT EXISTS l10n_pe_ne_anticipos jsonb
    """)
    cr.execute("""
        UPDATE account_move
        SET l10n_pe_ne_anticipos = jsonb_build_array(jsonb_build_object(
            'doc', COALESCE(l10n_pe_ne_anticipo_doc, ''),
            'monto', l10n_pe_ne_anticipo_total,
            'tipo', COALESCE(l10n_pe_ne_anticipo_tipo, '02'),
            'origenId', l10n_pe_ne_anticipo_origen_id
        ))
        WHERE l10n_pe_ne_anticipo_total IS NOT NULL
          AND l10n_pe_ne_anticipo_total > 0
          AND (l10n_pe_ne_anticipos IS NULL OR l10n_pe_ne_anticipos = 'null'::jsonb)
    """)

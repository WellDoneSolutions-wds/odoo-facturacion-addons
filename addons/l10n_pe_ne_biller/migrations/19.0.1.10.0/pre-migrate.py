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

    OJO — esquema PARCIAL: no se puede asumir que las 4 columnas escalares coexisten. Hay BDs con
    solo un subconjunto (p.ej. `..._total` presente pero `..._origen_id` ausente, según por qué
    commit de main pasaron). Referenciar una columna ausente en el UPDATE revienta con
    "column ... does not exist" y aborta el `-u` ENTERO. Por eso detectamos qué columnas existen
    y construimos cada campo del jsonb con su columna si está, o con un literal seguro si falta.
    """
    cr.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name='account_move'
          AND column_name IN (
              'l10n_pe_ne_anticipo_total', 'l10n_pe_ne_anticipo_doc',
              'l10n_pe_ne_anticipo_tipo', 'l10n_pe_ne_anticipo_origen_id')
    """)
    cols = {row[0] for row in cr.fetchall()}
    if 'l10n_pe_ne_anticipo_total' not in cols:
        return  # ya migrado o BD nueva (nunca tuvo las columnas escalares)
    # El pre-migrate corre ANTES de que el ORM cree las columnas del módulo, así que
    # `l10n_pe_ne_anticipos` puede no existir todavía (BD que viene de main 19.0.1.9.0,
    # donde solo existen las columnas escalares). Crearla aquí es inofensivo si ya existe;
    # el ORM la reconoce igual en el setup posterior.
    cr.execute("""
        ALTER TABLE account_move ADD COLUMN IF NOT EXISTS l10n_pe_ne_anticipos jsonb
    """)
    # Cada campo del jsonb usa su columna escalar SOLO si existe; si falta, un literal seguro
    # (los mismos defaults que traía el COALESCE original). `..._total` está garantizado por el
    # guard de arriba. Los fragmentos interpolados son nombres de columna/literales fijos —
    # nada viene de datos, así que no hay inyección.
    doc = "COALESCE(l10n_pe_ne_anticipo_doc, '')" if 'l10n_pe_ne_anticipo_doc' in cols else "''"
    tipo = "COALESCE(l10n_pe_ne_anticipo_tipo, '02')" if 'l10n_pe_ne_anticipo_tipo' in cols else "'02'"
    origen = "l10n_pe_ne_anticipo_origen_id" if 'l10n_pe_ne_anticipo_origen_id' in cols else "NULL"
    cr.execute(f"""
        UPDATE account_move
        SET l10n_pe_ne_anticipos = jsonb_build_array(jsonb_build_object(
            'doc', {doc},
            'monto', l10n_pe_ne_anticipo_total,
            'tipo', {tipo},
            'origenId', {origen}
        ))
        WHERE l10n_pe_ne_anticipo_total IS NOT NULL
          AND l10n_pe_ne_anticipo_total > 0
          AND (l10n_pe_ne_anticipos IS NULL OR l10n_pe_ne_anticipos = 'null'::jsonb)
    """)

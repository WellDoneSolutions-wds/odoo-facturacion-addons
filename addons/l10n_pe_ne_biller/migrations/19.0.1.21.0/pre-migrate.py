import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Series por sucursal: prepara la BD existente para que la numeración pueda colgar de un
    establecimiento. NO mueve datos de negocio — todo lo que hace es (a) tirar un índice cuya
    definición vieja bloquea al segundo local y (b) normalizar un NULL que el default de columna
    ya no deja aparecer.

    NO SIEMBRA EL REGISTRO DE SERIES a propósito. Sembrarlo desde las series emitidas, los
    diarios y los gemelos F↔B suena a cortesía y es riesgo puro: nada impide hoy que un tenant
    tenga DOS diarios de venta con la misma serie (habría que deduplicar y decidir cuál gana),
    hay que acertar el `tipo_doc` derivado del prefijo y quién queda de predeterminada, y si algo
    de eso revienta se cae el `-u` a mitad del upgrade con el módulo a medio actualizar. La
    retrocompatibilidad vive en el CÓDIGO (`_l10n_pe_ne_default_serie` sigue devolviendo
    F001/B001/FC01/… cuando el registro está vacío, y `_l10n_pe_ne_series_habilitadas` SUMA en
    vez de reemplazar), que es un fallback probado por tests en vez de datos por tenant que
    pueden derivar. Cero migración de datos = cero riesgo de upgrade: lo que el dueño ve en
    Series el día del upgrade es lo mismo que veía ayer.

    Por eso este archivo no tiene un solo INSERT: si algún día se decidiera sembrar, tendría que
    ser con ON CONFLICT DO NOTHING sobre (codigo, company_id) — la misma unicidad que defiende
    `l10n_pe_ne_serie` — para que el diario duplicado no tumbe el upgrade. Hoy solo se AVISA.

    Pre y no post: el DROP tiene que ocurrir ANTES de que el ORM llame a `init()` del modelo de
    caja, que es quien recrea el índice con su definición nueva.
    """
    _drop_indice_caja_viejo(cr)
    _normalizar_establecimiento_nulo(cr)
    _avisar_series_duplicadas_en_diarios(cr)


def _drop_indice_caja_viejo(cr):
    """El índice de sesión única de caja pasa de `(company_id)` a
    `(company_id, COALESCE(establecimiento_id, 0))`. Se llama IGUAL en las dos versiones, y
    `CREATE UNIQUE INDEX IF NOT EXISTS` NO recrea un índice que ya existe con ese nombre: sin
    este DROP el índice viejo sobrevive al upgrade y San Isidro jamás puede abrir su caja
    mientras Miraflores tenga la suya.

    Se mira la definición en vez de tirar a ciegas: así correr esta migración dos veces (o
    después de que `init()` ya lo recreó) es inofensivo y nunca deja al tenant sin la garantía
    de sesión única, ni siquiera durante unos segundos.
    """
    cr.execute("SELECT indexdef FROM pg_indexes WHERE indexname = %s",
               ("l10n_pe_ne_caja_sesion_unica_abierta",))
    fila = cr.fetchone()
    if not fila:
        return  # BD nueva, o ya lo tiró una corrida anterior: init() lo crea/recrea
    if "COALESCE" in (fila[0] or "").upper():
        return  # ya es el índice por local
    cr.execute("DROP INDEX l10n_pe_ne_caja_sesion_unica_abierta")
    _logger.info("NE series por local: índice de caja por compañía tirado; "
                 "init() lo recrea por (compañía, local).")


def _normalizar_establecimiento_nulo(cr):
    """`l10n_pe_ne_cod_establecimiento` viaja al XML como codLocalEmisor y ahora también decide
    a qué local se imputa una venta en el arqueo y en el reporte. Odoo aplicó el default '0000'
    al crear la columna, así que en teoría no hay NULLs; en la práctica un import por SQL o un
    `write` viejo pudo dejarlos, y un NULL sería una venta que no aparece en NINGÚN local.
    Defensivo y barato: un UPDATE sobre las filas que ya son cero en cualquier BD sana.

    Nada de reescribir historia fiscal: '0000' es EXACTAMENTE lo que ese comprobante declaró
    (el domicilio fiscal), solo que escrito en vez de implícito.
    """
    cr.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'account_move'
          AND column_name = 'l10n_pe_ne_cod_establecimiento'
    """)
    if not cr.fetchone():
        return  # el ORM aún no creó la columna (BD nueva): nace con el default
    cr.execute("""
        UPDATE account_move SET l10n_pe_ne_cod_establecimiento = '0000'
        WHERE l10n_pe_ne_cod_establecimiento IS NULL
    """)
    if cr.rowcount:
        _logger.info("NE series por local: %s comprobantes sin local pasan a '0000' "
                     "(domicilio fiscal).", cr.rowcount)


def _avisar_series_duplicadas_en_diarios(cr):
    """Diagnóstico, no migración: si el tenant tiene dos diarios de venta con la MISMA serie,
    quien configure el registro va a tener que elegir a qué local pertenece (una serie es de un
    solo local: SUNAT numera por RUC y serie). Se deja dicho en el log del upgrade para que
    soporte lo vea ANTES de que el dueño se tope con el error al declararla, en vez de resolverlo
    adivinando aquí. No falla ni cambia nada.
    """
    cr.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'account_journal'
          AND column_name = 'l10n_pe_ne_serie'
    """)
    if not cr.fetchone():
        return
    cr.execute("""
        SELECT company_id, upper(trim(l10n_pe_ne_serie)) AS serie, count(*)
        FROM account_journal
        WHERE l10n_pe_ne_serie IS NOT NULL AND trim(l10n_pe_ne_serie) <> ''
        GROUP BY 1, 2 HAVING count(*) > 1
    """)
    for company_id, serie, n in cr.fetchall():
        _logger.info("NE series por local: la compañía %s tiene %s diarios con la serie %s. "
                     "Al declararla en Series habrá que decidir de qué local es.",
                     company_id, n, serie)

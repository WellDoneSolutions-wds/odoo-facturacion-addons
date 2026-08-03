# Carga el Plan Contable l10n_pe en la compañía (para el IGV 1000 que usa la emisión / los tests).
# Uso: odoo shell -d <db> < scripts/load_coa.py   (lo invoca scripts/test-fresh.sh)
c = env.company
try:
    if c.country_id.code != 'PE':
        c.country_id = env.ref('base.pe').id
    env['account.chart.template'].try_loading('pe', company=c, install_demo=False)
    env.cr.commit()
    print("== OK country:", c.country_id.code,
          "| TAX 1000:", env['account.tax'].search_count([('l10n_pe_edi_tax_code', '=', '1000')]))
except Exception as e:  # noqa: BLE001
    print("== ERR:", type(e).__name__, str(e)[:300])

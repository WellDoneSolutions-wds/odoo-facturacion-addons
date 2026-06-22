# Ejecutar con: odoo-bin shell -c <conf> -d odoo_ne_biller --no-http  (vía heredoc/stdin)
company = env.company
pe = env.ref('base.pe')
company.write({'country_id': pe.id, 'vat': '20321856145'})
ruc_type = env['l10n_latam.identification.type'].search([('l10n_pe_vat_code', '=', '6')], limit=1)
company.partner_id.write({
    'country_id': pe.id,
    'l10n_latam_identification_type_id': ruc_type.id,
    'vat': '20321856145',
})
# Cargar el plan contable peruano (idempotente: si ya está, no duplica)
env['account.chart.template'].try_loading('pe', company)
igv = env['account.tax'].search([
    ('company_id', '=', company.id), ('type_tax_use', '=', 'sale'),
    ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
assert igv, "No se encontró el IGV (l10n_pe_edi_tax_code=1000) tras cargar el plan contable"
env.cr.commit()
print("OK empresa RUC:", company.vat, "| IGV:", igv.name, igv.amount)

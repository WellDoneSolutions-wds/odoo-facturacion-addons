# odoo-bin shell ... < seed_docs_e2e.py  -> imprime BOLETA_ID / NC_ID / ND_ID
import time
company = env.company
IdType = env['l10n_latam.identification.type']
dni_type = IdType.search([('l10n_pe_vat_code', '=', '1')], limit=1)
igv = env['account.tax'].search([('company_id', '=', company.id), ('type_tax_use', '=', 'sale'),
                                 ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
product = env['product.product'].search([('default_code', '=', 'P001')], limit=1)
base = int(time.time()) % 100000000

def line():
    return [(0, 0, {'product_id': product.id, 'quantity': 1.0, 'price_unit': 7.20,
                    'tax_ids': [(6, 0, igv.ids)]})]

# Boleta — cliente con DNI
cli_dni = env['res.partner'].search([('vat', '=', '12345678')], limit=1) or \
    env['res.partner'].create({'name': 'CONSUMIDOR FINAL E2E', 'vat': '12345678',
                               'l10n_latam_identification_type_id': dni_type.id})
boleta = env['account.move'].create({
    'move_type': 'out_invoice', 'partner_id': cli_dni.id, 'invoice_date': time.strftime('%Y-%m-%d'),
    'l10n_pe_serie': 'B001', 'l10n_pe_correlativo': str(base + 1), 'invoice_line_ids': line()})
boleta.action_post()

# NC sobre la factura aceptada id 14
inv = env['account.move'].browse(14)
nc_doctype = env['l10n_latam.document.type'].search([('internal_type', '=', 'credit_note')], limit=1)
nc = inv._reverse_moves([{'invoice_date': time.strftime('%Y-%m-%d'), 'l10n_pe_serie': 'FC01',
                          'l10n_pe_correlativo': str(base + 2), 'l10n_pe_motivo_code': '01',
                          'l10n_latam_document_type_id': nc_doctype.id}])
nc.action_post()

# ND (nota de débito) sobre la factura aceptada id 14
nd = env['account.move'].create({
    'move_type': 'out_invoice', 'partner_id': inv.partner_id.id, 'invoice_date': time.strftime('%Y-%m-%d'),
    'l10n_pe_serie': 'FD01', 'l10n_pe_correlativo': str(base + 3), 'l10n_pe_motivo_code': '02',
    'debit_origin_id': inv.id, 'invoice_line_ids': line()})
nd.action_post()

env.cr.commit()
print('BOLETA_ID=%s' % boleta.id)
print('NC_ID=%s' % nc.id)
print('ND_ID=%s' % nd.id)

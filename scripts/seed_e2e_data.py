# odoo-bin shell ... < seed_e2e_data.py   -> imprime el id de la factura
import time
partner = env['res.partner'].search([('vat', '=', '20605145648')], limit=1)
if not partner:
    rt = env['l10n_latam.identification.type'].search([('l10n_pe_vat_code', '=', '6')], limit=1)
    partner = env['res.partner'].create({'name': 'CLIENTE E2E SAC', 'vat': '20605145648',
                                         'l10n_latam_identification_type_id': rt.id})
product = env['product.product'].search([('default_code', '=', 'P001')], limit=1) or \
    env['product.product'].create({'name': 'DESARMADOR', 'default_code': 'P001', 'list_price': 7.20})
igv = env['account.tax'].search([('company_id', '=', env.company.id), ('type_tax_use', '=', 'sale'),
                                 ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
move = env['account.move'].create({
    'move_type': 'out_invoice', 'partner_id': partner.id,
    'invoice_date': time.strftime('%Y-%m-%d'),
    'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': str(int(time.time()) % 100000000),
    'invoice_line_ids': [(0, 0, {'product_id': product.id, 'quantity': 1.0,
                                 'price_unit': 7.20, 'tax_ids': [(6, 0, igv.ids)]})]})
move.action_post()
env.cr.commit()
print('E2E_MOVE_ID=%s' % move.id)
print('E2E_CORRELATIVO=%s' % move.l10n_pe_correlativo)

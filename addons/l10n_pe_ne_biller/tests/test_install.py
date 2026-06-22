from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestInstall(TransactionCase):
    def test_fields_exist(self):
        Move = self.env['account.move']
        for fname in ('l10n_pe_biller_state', 'l10n_pe_serie', 'l10n_pe_correlativo',
                      'l10n_pe_biller_xml', 'l10n_pe_biller_cdr', 'l10n_pe_biller_message'):
            self.assertIn(fname, Move._fields, "Falta el campo %s" % fname)

    def test_default_state(self):
        move = self.env['account.move'].create({'move_type': 'out_invoice'})
        self.assertEqual(move.l10n_pe_biller_state, 'por_enviar')

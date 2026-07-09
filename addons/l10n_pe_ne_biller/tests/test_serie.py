import re

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBillerSerie(TransactionCase):
    """Serie por diario + correlativo auto-incremental (del folio del número de asiento), con
    override manual."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        self.journal = self.env['account.journal'].search([
            ('company_id', '=', self.company.id), ('type', '=', 'sale')], limit=1)
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20605145648',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create({'name': 'PROD', 'default_code': 'P1'})

    def _move(self, **kw):
        vals = {
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'journal_id': self.journal.id,
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 10.0, 'tax_ids': [(6, 0, self.igv.ids)]})]}
        vals.update(kw)
        return self.env['account.move'].create(vals)

    def test_serie_por_diario(self):
        """La serie del move se toma del diario (l10n_pe_ne_serie) por defecto."""
        self.journal.l10n_pe_ne_serie = 'F999'
        move = self._move()
        self.assertEqual(move.l10n_pe_serie, 'F999')

    def test_correlativo_del_folio(self):
        """Sin correlativo manual, se usa el folio (auto-incremental) del número del asiento."""
        self.journal.l10n_pe_ne_serie = 'F001'
        move = self._move()
        move.action_post()
        folio = re.findall(r'\d+', (move.name or '').replace(' ', ''))[-1]
        payload = move._l10n_pe_build_invoice_request()
        self.assertEqual(payload['id']['serie'], 'F001')
        self.assertEqual(payload['id']['correlativo'], folio.zfill(8))

    def test_correlativo_manual_override(self):
        """El correlativo manual tiene prioridad sobre el folio."""
        move = self._move(l10n_pe_serie='F777', l10n_pe_correlativo='7')
        move.action_post()
        payload = move._l10n_pe_build_invoice_request()
        self.assertEqual(payload['id']['serie'], 'F777')
        self.assertEqual(payload['id']['correlativo'], '00000007')

    def test_serie_boleta_ajusta_letra(self):
        """Cliente sin RUC (boleta): la serie por defecto del diario (F…) cambia a B…."""
        dni_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '1')], limit=1)
        if not dni_type:
            self.skipTest('sin tipo de documento DNI en la localización')
        consumidor = self.env['res.partner'].create({
            'name': 'CONSUMIDOR FINAL', 'vat': '45678912',
            'l10n_latam_identification_type_id': dni_type.id})
        self.journal.l10n_pe_ne_serie = 'F001'
        move = self._move(partner_id=consumidor.id)
        self.assertEqual(move._l10n_pe_document_type(), '03')
        self.assertEqual(move.l10n_pe_serie, 'B001')

    def test_serie_familia_equivocada_bloquea_emision(self):
        """Serie F… en una boleta (o B… en factura) corta la emisión antes de ir a SUNAT."""
        from odoo.exceptions import UserError
        dni_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '1')], limit=1)
        if not dni_type:
            self.skipTest('sin tipo de documento DNI en la localización')
        consumidor = self.env['res.partner'].create({
            'name': 'CONSUMIDOR FINAL DOS', 'vat': '45678913',
            'l10n_latam_identification_type_id': dni_type.id})
        boleta = self._move(partner_id=consumidor.id, l10n_pe_serie='F001',
                            l10n_pe_correlativo='9')
        boleta.action_post()
        with self.assertRaises(UserError):
            boleta._l10n_pe_target()
        factura = self._move(l10n_pe_serie='B001', l10n_pe_correlativo='9')
        factura.action_post()
        with self.assertRaises(UserError):
            factura._l10n_pe_target()

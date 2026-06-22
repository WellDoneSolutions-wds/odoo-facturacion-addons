from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestBillerAnticipo(TransactionCase):
    """Factura final que regulariza un anticipo ya facturado.

    El anticipo (IGV incluido) entra como descuento global código 04 (reduce la base/IGV de cabecera,
    no las líneas que declaran la operación completa) + referencia en `relacionados` + PrepaidAmount.
    Fórmulas SUNAT (validador ValidaExprRegFactura): el IGV de cabecera se computa sobre la base ya
    reducida; el TaxInclusive usa el IGV completo; el Payable = TaxInclusive − total del anticipo."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create({'name': 'SERVICIO', 'default_code': 'S1'})

    def _move(self, anticipo_total=0.0, **vals):
        base = {
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '9',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 500.0, 'tax_ids': [(6, 0, self.igv.ids)]})]}
        if anticipo_total:
            base.update({'l10n_pe_ne_anticipo_total': anticipo_total,
                         'l10n_pe_ne_anticipo_doc': 'F001-00000100'})
        base.update(vals)
        move = self.env['account.move'].create(base)
        move.action_post()
        return move

    def test_anticipo(self):
        # valor 500 + IGV 90 = 590; anticipo 118 (valor 100 + IGV 18).
        payload = self._move(anticipo_total=118.0)._l10n_pe_build_invoice_request()

        # 1) Descuento global código 04: monto = valor del anticipo, base = valor de venta completo.
        vg = [v for v in payload['variablesGlobales'] if v['codTipoVariableGlobal'] == '04']
        self.assertEqual(len(vg), 1)
        self.assertEqual(vg[0]['mtoVariableGlobal'], '100.00')
        self.assertEqual(vg[0]['mtoBaseImpVariableGlobal'], '500.00')
        self.assertEqual(vg[0]['porVariableGlobal'], '0.20')
        self.assertEqual(vg[0]['tipVariableGlobal'], 'false')

        # 2) Tributo IGV de cabecera sobre la base reducida (500 − 100 = 400 → IGV 72).
        igv = [t for t in payload['tributos'] if t['ideTributo'] == '1000'][0]
        self.assertEqual(igv['mtoBaseImponible'], '400.00')
        self.assertEqual(igv['mtoTributo'], '72.00')

        # 3) Cabecera: valor completo, TaxInclusive completo, Payable deducido, anticipo informado.
        cab = payload['cabecera']
        self.assertEqual(cab['sumTotValVenta'], '500.00')   # LineExtension (completo)
        self.assertEqual(cab['sumPrecioVenta'], '590.00')   # TaxInclusive (IGV completo)
        self.assertEqual(cab['sumImpVenta'], '472.00')      # Payable = 590 − 118
        self.assertEqual(cab['sumTotTributos'], '72.00')    # IGV reducido
        self.assertEqual(cab['sumTotalAnticipos'], '118.00')

        # 4) Línea: declara la operación completa (valor 500, IGV 90).
        det = payload['detalle'][0]
        self.assertEqual(det['mtoValorVentaItem'], '500.00')
        self.assertEqual(det['mtoIgvItem'], '90.00')

        # 5) Relacionados: referencia al comprobante de anticipo.
        rel = payload['relacionados'][0]
        self.assertEqual(rel['indDocRelacionado'], '2')
        self.assertEqual(rel['tipDocRelacionado'], '02')
        self.assertEqual(rel['numDocRelacionado'], 'F001-00000100')
        self.assertEqual(rel['mtoDocRelacionado'], '118.00')
        self.assertEqual(rel['numDocEmisor'], self.company.vat)

    def test_sin_anticipo_no_cambia(self):
        payload = self._move()._l10n_pe_build_invoice_request()
        self.assertNotIn('relacionados', payload)
        self.assertEqual([v for v in payload['variablesGlobales']
                          if v['codTipoVariableGlobal'] == '04'], [])
        cab = payload['cabecera']
        self.assertEqual(cab['sumTotalAnticipos'], '0.00')
        self.assertEqual(cab['sumImpVenta'], '590.00')
        self.assertEqual(cab['sumTotTributos'], '90.00')

    # --- guardas: el addon rechaza limpiamente lo no representable, no emite XML inválido ---

    def test_anticipo_excede_total_rechaza(self):
        move = self._move(anticipo_total=5000.0)   # > 590
        with self.assertRaises(UserError):
            move._l10n_pe_build_invoice_request()

    def test_anticipo_sin_doc_rechaza(self):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '9', 'l10n_pe_ne_anticipo_total': 118.0,
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 500.0, 'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        with self.assertRaises(UserError):   # falta l10n_pe_ne_anticipo_doc
            move._l10n_pe_build_invoice_request()

    def test_anticipo_exonerado_rechaza(self):
        exo = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '9997')], limit=1)
        if not exo:
            self.skipTest("sin tax exonerada 9997 en la localización")
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '9',
            'l10n_pe_ne_anticipo_total': 100.0, 'l10n_pe_ne_anticipo_doc': 'F001-00000100',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 500.0, 'tax_ids': [(6, 0, exo.ids)]})]})
        move.action_post()
        with self.assertRaises(UserError):   # anticipo no soportado en operación no gravada
            move._l10n_pe_build_invoice_request()

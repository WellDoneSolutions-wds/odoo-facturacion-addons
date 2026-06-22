from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBillerAffectation(TransactionCase):
    """Mapeo real de impuestos: la afectación IGV (cat.07) y el tributo (cat.05) salen de la tax
    de Odoo (`l10n_pe_edi_tax_code`), no de un IGV 18% hardcodeado."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company

        def sale_tax(code):
            return self.env['account.tax'].search([
                ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
                ('l10n_pe_edi_tax_code', '=', code)], limit=1)

        self.igv = sale_tax('1000')
        self.exo = sale_tax('9997')
        self.ina = sale_tax('9998')
        self.assertTrue(self.igv, "IGV (1000) no existe — correr setup_company.py")
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20605145648',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create(
            {'name': 'PROD', 'default_code': 'P001'})

    def _line(self, tax, price=100.0):
        return (0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                       'price_unit': price, 'tax_ids': [(6, 0, tax.ids)]})

    def _invoice(self, lines):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': '2026-06-20', 'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
            'invoice_line_ids': lines})
        move.action_post()
        return move

    def test_gravado_sigue_correcto(self):
        payload = self._invoice([self._line(self.igv, 100.0)])._l10n_pe_build_invoice_request()
        d = payload['detalle'][0]
        self.assertEqual(d['tipAfeIGV'], '10')
        self.assertEqual(d['codTriIGV'], '1000')
        self.assertEqual(d['porIgvItem'], '18.00')
        self.assertEqual(d['mtoIgvItem'], '18.00')
        self.assertEqual(payload['tributos'][0]['ideTributo'], '1000')
        self.assertEqual(payload['tributos'][0]['codCatTributo'], 'S')

    def test_exonerado(self):
        if not self.exo:
            self.skipTest("No hay tax exonerada (9997) en el plan")
        payload = self._invoice([self._line(self.exo, 100.0)])._l10n_pe_build_invoice_request()
        d = payload['detalle'][0]
        self.assertEqual(d['tipAfeIGV'], '20')
        self.assertEqual(d['codTriIGV'], '9997')
        self.assertEqual(d['nomTributoIgvItem'], 'EXO')
        self.assertEqual(d['codTipTributoIgvItem'], 'VAT')
        self.assertEqual(d['mtoIgvItem'], '0.00')
        self.assertEqual(d['porIgvItem'], '0.00')
        trib = payload['tributos'][0]
        self.assertEqual(trib['ideTributo'], '9997')
        self.assertEqual(trib['codCatTributo'], 'E')
        self.assertEqual(trib['mtoTributo'], '0.00')

    def test_mixto_dos_tributos(self):
        if not self.exo:
            self.skipTest("No hay tax exonerada (9997) en el plan")
        payload = self._invoice([
            self._line(self.igv, 100.0), self._line(self.exo, 50.0)])._l10n_pe_build_invoice_request()
        self.assertEqual(len(payload['detalle']), 2)
        codigos = {t['ideTributo'] for t in payload['tributos']}
        self.assertEqual(codigos, {'1000', '9997'})
        afectaciones = {d['tipAfeIGV'] for d in payload['detalle']}
        self.assertEqual(afectaciones, {'10', '20'})

    def test_unidad_medida_default_es_niu(self):
        """Producto en unidades -> NIU (la unidad estándar trae el código por el data file)."""
        payload = self._invoice([self._line(self.igv)])._l10n_pe_build_invoice_request()
        self.assertEqual(payload['detalle'][0]['codUnidadMedida'], 'NIU')

    def test_unidad_medida_kg(self):
        """Producto en kilogramos -> KGM (cat. 03 de SUNAT), no el 'NIU' hardcodeado."""
        kg = self.env.ref('uom.product_uom_kgm')
        prod_kg = self.env['product.product'].create(
            {'name': 'ARROZ', 'default_code': 'KG1', 'uom_id': kg.id})
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': '2026-06-20', 'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
            'invoice_line_ids': [(0, 0, {
                'product_id': prod_kg.id, 'quantity': 2.0, 'price_unit': 50.0,
                'product_uom_id': kg.id, 'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        payload = move._l10n_pe_build_invoice_request()
        self.assertEqual(payload['detalle'][0]['codUnidadMedida'], 'KGM')

    def test_icbper(self):
        """Línea con IGV + ICBPER (bolsa): el IGV se separa del ICBPER, y el ICBPER va por sus
        propios campos (codTriIcbper) y suma a sumImpVenta pero no a sumTotTributos/sumPrecioVenta."""
        icbper_tax = self.env['account.tax'].create({
            'name': 'ICBPER', 'type_tax_use': 'sale', 'amount_type': 'fixed', 'amount': 0.50,
            'l10n_pe_edi_tax_code': '7152', 'tax_group_id': self.igv.tax_group_id.id})
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': '2026-06-20', 'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id, 'quantity': 1.0, 'price_unit': 500.0,
                'tax_ids': [(6, 0, [self.igv.id, icbper_tax.id])]})]})
        move.action_post()
        payload = move._l10n_pe_build_invoice_request()
        d = payload['detalle'][0]
        self.assertEqual(d['mtoIgvItem'], '90.00')             # IGV solo, sin el ICBPER
        self.assertEqual(d['mtoPrecioVentaUnitario'], '590.00')  # valor + IGV, sin ICBPER
        self.assertEqual(d['sumTotTributosItem'], '90.50')     # IGV + ICBPER
        self.assertEqual(d['codTriIcbper'], '7152')
        self.assertEqual(d['nomTributoIcbperItem'], 'ICBPER')
        self.assertEqual(d['codTipTributoIcbperItem'], 'OTH')
        self.assertEqual(d['ctdBolsasTriIcbperItem'], '1')
        self.assertEqual(d['mtoTriIcbperUnidad'], '0.50')
        self.assertEqual(d['mtoTriIcbperItem'], '0.50')
        # Cabecera: el ICBPER no entra en tributos ni en precio; sí en el importe a cobrar.
        self.assertEqual(payload['cabecera']['sumTotTributos'], '90.00')
        self.assertEqual(payload['cabecera']['sumPrecioVenta'], '590.00')
        self.assertEqual(payload['cabecera']['sumImpVenta'], '590.50')
        self.assertEqual({t['ideTributo'] for t in payload['tributos']}, {'1000'})

    def test_isc(self):
        """Línea con ISC (al valor 10%) + IGV: el IGV se computa sobre valor+ISC; el ISC va por sus
        campos (codTriISC, tipSisISC) y como tributo 2000 de cabecera. La base del IGV de cabecera
        es el valor venta (no incluye el ISC)."""
        self.igv.sequence = 2  # el IGV se aplica DESPUÉS del ISC (sobre valor+ISC)
        isc_tax = self.env['account.tax'].create({
            'name': 'ISC 10%', 'type_tax_use': 'sale', 'amount_type': 'percent', 'amount': 10.0,
            'l10n_pe_edi_tax_code': '2000', 'l10n_pe_edi_isc_type': '01',
            'include_base_amount': True, 'sequence': 1, 'tax_group_id': self.igv.tax_group_id.id})
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': '2026-06-20', 'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id, 'quantity': 1.0, 'price_unit': 500.0,
                'tax_ids': [(6, 0, [isc_tax.id, self.igv.id])]})]})
        move.action_post()
        payload = move._l10n_pe_build_invoice_request()
        d = payload['detalle'][0]
        self.assertEqual(d['mtoBaseIscItem'], '500.00')
        self.assertEqual(d['mtoIscItem'], '50.00')
        self.assertEqual(d['tipSisISC'], '01')
        self.assertEqual(d['porIscItem'], '10.00')
        self.assertEqual(d['codTipTributoIscItem'], 'EXC')
        self.assertEqual(d['mtoBaseIgvItem'], '550.00')   # valor + ISC
        self.assertEqual(d['mtoIgvItem'], '99.00')        # IGV sobre 550
        self.assertEqual(d['mtoPrecioVentaUnitario'], '649.00')
        self.assertEqual(d['sumTotTributosItem'], '149.00')  # ISC + IGV
        tributos = {t['ideTributo']: t for t in payload['tributos']}
        self.assertEqual(tributos['1000']['mtoBaseImponible'], '500.00')  # valor venta, NO 550
        self.assertEqual(tributos['1000']['mtoTributo'], '99.00')
        self.assertEqual(tributos['2000']['mtoTributo'], '50.00')
        self.assertEqual(tributos['2000']['codTipTributo'], 'EXC')
        self.assertEqual(payload['cabecera']['sumTotValVenta'], '500.00')
        self.assertEqual(payload['cabecera']['sumTotTributos'], '149.00')
        self.assertEqual(payload['cabecera']['sumImpVenta'], '649.00')

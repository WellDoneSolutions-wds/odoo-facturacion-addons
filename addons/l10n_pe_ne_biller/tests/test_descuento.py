from odoo.tests import TransactionCase, tagged

from odoo.addons.l10n_pe_ne_biller.models.account_move_biller import (
    DESC_GLOBAL_NO_AFECTA_COD,
)


@tagged('post_install', '-at_install')
class TestBillerDescuento(TransactionCase):
    """Descuento por ítem: la línea va neta (IGV sobre el neto) y el descuento se muestra explícito
    en adicionalDetalle (cat. 53 código 00)."""

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
        self.product = self.env['product.product'].create({'name': 'PROD', 'default_code': 'P1'})

    def _move(self, discount):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 500.0, 'discount': discount,
                                         'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        return move

    def test_descuento_item(self):
        payload = self._move(10.0)._l10n_pe_build_invoice_request()
        d = payload['detalle'][0]
        self.assertEqual(d['mtoValorUnitario'], '500.00')    # unitario BRUTO (regla 3271)
        self.assertEqual(d['mtoValorVentaItem'], '450.00')   # neto (500 - 10%)
        self.assertEqual(d['mtoBaseIgvItem'], '450.00')
        self.assertEqual(d['mtoIgvItem'], '81.00')           # IGV sobre el neto
        self.assertEqual(len(payload['adicionalDetalle']), 1)
        a = payload['adicionalDetalle'][0]
        self.assertEqual(a['idLinea'], '1')
        self.assertEqual(a['nomPropiedad'], '-')             # salta el bloque AdditionalItemProperty
        self.assertEqual(a['codTipoVariable'], '00')
        self.assertEqual(a['porVariable'], '0.10000')        # 5 decimales (SUNAT 3290)
        self.assertEqual(a['mtoVariable'], '50.00')          # descuento
        self.assertEqual(a['mtoBaseImpVariable'], '500.00')  # base bruta

    def test_sin_descuento(self):
        payload = self._move(0.0)._l10n_pe_build_invoice_request()
        self.assertEqual(payload['adicionalDetalle'], [])
        self.assertEqual(payload['detalle'][0]['mtoValorVentaItem'], '500.00')

    def test_descuento_monto_fijo_factor_reconstruye(self):
        """SUNAT 3290: con un descuento cuyo % NO es fracción redonda de la base (un descuento en
        monto fijo, p.ej. S/50 sobre 470 → 10.6383%), el factor por ítem debe reconstruir el monto
        (|base·por − monto| ≤ 1). Con 2 decimales descuadraba y SUNAT rechazaba."""
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '2',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 470.0, 'discount': 10.6383,
                                         'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        a = move._l10n_pe_build_invoice_request()['adicionalDetalle'][0]
        base, por, monto = (float(a['mtoBaseImpVariable']), float(a['porVariable']), float(a['mtoVariable']))
        self.assertLessEqual(abs(base * por - monto), 1.0)


@tagged('post_install', '-at_install')
class TestBillerDescuentoNoAfecta(TransactionCase):
    """Descuento que NO afecta la base del IGV: la gravada y el IGV se calculan sobre el precio
    lleno; el descuento solo baja el importe a cobrar (MtoImpVenta) y va como AllowanceCharge
    global (sumDescTotal). Es un ajuste solo-de-emisión (no agrega línea a Odoo, como el anticipo)."""

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
        self.product = self.env['product.product'].create({'name': 'PROD', 'default_code': 'PN'})

    def _move(self, desc_no_afecta, correlativo='1'):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': correlativo,
            'l10n_pe_ne_desc_no_afecta': desc_no_afecta,
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 500.0,
                                         'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        return move

    def test_gravada_e_igv_completos_total_baja(self):
        # 1×500 gravado → base 500, IGV 90, total 590. Con descuento no-afecta de 100:
        req = self._move(100.0)._l10n_pe_build_invoice_request()
        cab = req['cabecera']
        self.assertEqual(cab['sumTotValVenta'], '500.00')   # base gravada COMPLETA (no baja)
        self.assertEqual(cab['sumTotTributos'], '90.00')    # IGV COMPLETO (no baja)
        self.assertEqual(cab['sumPrecioVenta'], '590.00')   # TaxInclusive sin tocar
        self.assertEqual(cab['sumImpVenta'], '490.00')      # importe a cobrar = 590 − 100
        self.assertEqual(cab['sumDescTotal'], '100.00')     # AllowanceCharge global
        # La línea del detalle tampoco cambia (el descuento no vive en la línea).
        self.assertEqual(req['detalle'][0]['mtoValorVentaItem'], '500.00')
        self.assertEqual(req['detalle'][0]['mtoIgvItem'], '90.00')

    def test_variable_global_no_afecta(self):
        gv = self._move(100.0)._l10n_pe_build_invoice_request()['variablesGlobales']
        na = [v for v in gv if v['codTipoVariableGlobal'] == DESC_GLOBAL_NO_AFECTA_COD]
        self.assertEqual(len(na), 1)
        self.assertEqual(na[0]['mtoVariableGlobal'], '100.00')
        self.assertEqual(na[0]['mtoBaseImpVariableGlobal'], '590.00')   # base = precio de venta

    def test_sin_descuento_no_emite_variable(self):
        req = self._move(0.0)._l10n_pe_build_invoice_request()
        self.assertEqual(req['cabecera']['sumDescTotal'], '0.00')
        self.assertEqual(req['cabecera']['sumImpVenta'], '590.00')
        self.assertEqual(req['variablesGlobales'], [])

    def test_topeado_no_deja_total_negativo(self):
        # Descuento mayor que el total (1000 > 590): se topea al total → MtoImpVenta 0, no negativo.
        cab = self._move(1000.0, correlativo='2')._l10n_pe_build_invoice_request()['cabecera']
        self.assertEqual(cab['sumImpVenta'], '0.00')
        self.assertEqual(cab['sumDescTotal'], '590.00')

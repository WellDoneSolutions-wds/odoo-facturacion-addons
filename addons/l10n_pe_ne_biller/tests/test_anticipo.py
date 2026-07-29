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
        # 'l10n_pe_ne_anticipo_origen_id' es el nombre viejo (campo escalar retirado); se sigue
        # aceptando como kwarg del helper y se traduce a la lista JSON `l10n_pe_ne_anticipos`.
        origen_id = vals.pop('l10n_pe_ne_anticipo_origen_id', None)
        if anticipo_total:
            base['l10n_pe_ne_anticipos'] = [{'doc': 'F001-00000100', 'monto': anticipo_total,
                                              'tipo': '02', 'origenId': origen_id}]
        base.update(vals)
        move = self.env['account.move'].create(base)
        move.action_post()
        return move

    def test_anticipo(self):
        # valor 500 + IGV 90 = 590; anticipo 118 (valor 100 + IGV 18).
        payload = self._move(anticipo_total=118.0)._l10n_pe_build_invoice_request()

        # 1) Descuento global código 04 con FACTOR UNITARIO: base = monto = valor del anticipo, factor 1.
        #    Así base × factor = monto exacto para cualquier importe → regla SUNAT 4322 pasa siempre
        #    (antes: base = base de venta, factor = valor/base a 5 dec, que en base alta se desviaba > 1).
        vg = [v for v in payload['variablesGlobales'] if v['codTipoVariableGlobal'] == '04']
        self.assertEqual(len(vg), 1)
        self.assertEqual(vg[0]['mtoVariableGlobal'], '100.00')
        self.assertEqual(vg[0]['mtoBaseImpVariableGlobal'], '100.00')  # base del descuento = el valor
        self.assertEqual(vg[0]['porVariableGlobal'], '1.00000')
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
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '9',
            'l10n_pe_ne_anticipos': [{'monto': 118.0}],   # sin 'doc'
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 500.0, 'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        with self.assertRaises(UserError):   # falta el doc del anticipo
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
            'l10n_pe_ne_anticipos': [{'doc': 'F001-00000100', 'monto': 100.0}],
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 500.0, 'tax_ids': [(6, 0, exo.ids)]})]})
        move.action_post()
        with self.assertRaises(UserError):   # anticipo no soportado en operación no gravada
            move._l10n_pe_build_invoice_request()

    # --- doc. A: comprobante emitido POR un pago anticipado (Fase 1) ---

    def test_es_anticipo_marca_descripcion_y_es_venta_interna(self):
        """La factura de anticipo es una venta interna normal (0101) cuyo detalle antepone
        'PAGO ANTICIPADO'; no lleva relacionados ni descuento 04 (no regulariza nada)."""
        move = self._move(l10n_pe_ne_es_anticipo=True)
        payload = move._l10n_pe_build_invoice_request()
        self.assertEqual(payload['cabecera']['tipOperacion'], '0101')
        self.assertTrue(payload['detalle'][0]['desItem'].startswith('PAGO ANTICIPADO'))
        self.assertNotIn('relacionados', payload)
        self.assertEqual([v for v in payload['variablesGlobales']
                          if v['codTipoVariableGlobal'] == '04'], [])
        # El importe total del anticipo debe ser > 0 (SUNAT 2502).
        self.assertGreater(float(payload['cabecera']['sumImpVenta']), 0.0)

    def test_es_anticipo_no_duplica_prefijo(self):
        move = self._move(l10n_pe_ne_es_anticipo=True)
        move.invoice_line_ids[0].name = 'PAGO ANTICIPADO - Servicio X'
        payload = move._l10n_pe_build_invoice_request()
        self.assertEqual(payload['detalle'][0]['desItem'], 'PAGO ANTICIPADO - Servicio X')

    def test_es_anticipo_con_regularizacion_rechaza(self):
        """Un comprobante no puede ser a la vez anticipo (A) y regularizar otro (B)."""
        move = self._move(anticipo_total=118.0, l10n_pe_ne_es_anticipo=True)
        with self.assertRaises(UserError):
            move._l10n_pe_build_invoice_request()

    # --- doc. B enlazado: saldo del anticipo y autocompletado (Fase 2) ---

    def _anticipo_A(self, corr='00000100', tipo_doc='01'):
        """Crea un anticipo (doc. A) 'aceptado' por SUNAT (biller_state enviado), listo para
        aparecer en la lista de pendientes y ser regularizado. Total del anticipo = 590 (500 + IGV)."""
        A = self._move(l10n_pe_ne_es_anticipo=True, l10n_pe_correlativo=corr)
        A.write({
            'l10n_pe_ne_serie_emit': 'F001', 'l10n_pe_ne_corr_emit': corr,
            'l10n_pe_ne_tipo_doc': tipo_doc, 'l10n_pe_biller_state': 'enviado'})
        return A

    def test_anticipos_pendientes_lista_saldo(self):
        """La lista de pendientes trae el anticipo del cliente con su doc, tipo (cat. 12) y saldo."""
        self._anticipo_A()
        # Filtra por el partner del test (no por RUC compartido) para aislarse de otros anticipos
        # del mismo RUC que pudieran existir en la DB.
        rows = self.env['account.move'].l10n_pe_ne_anticipos_pendientes(partner_id=self.partner.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['doc'], 'F001-00000100')
        self.assertEqual(rows[0]['tipo'], '02')            # factura → cat. 12 = 02
        self.assertAlmostEqual(rows[0]['saldo'], 590.0, 2)

    def test_saldo_baja_al_regularizar_y_sigue_pendiente(self):
        """Regularizar parte del anticipo baja su saldo; con saldo restante sigue en pendientes."""
        A = self._anticipo_A()
        self._move(anticipo_total=200.0, l10n_pe_correlativo='10',
                   l10n_pe_ne_anticipo_origen_id=A.id)
        self.assertAlmostEqual(A.l10n_pe_ne_anticipo_aplicado, 200.0, 2)
        self.assertAlmostEqual(A.l10n_pe_ne_anticipo_saldo, 390.0, 2)
        rows = self.env['account.move'].l10n_pe_ne_anticipos_pendientes(partner_id=self.partner.id)
        self.assertAlmostEqual(rows[0]['saldo'], 390.0, 2)

    def test_saldo_agotado_sale_de_pendientes(self):
        """Consumido por completo, el anticipo desaparece de la lista de pendientes."""
        A = self._anticipo_A()
        self._move(anticipo_total=590.0, l10n_pe_correlativo='10',
                   l10n_pe_ne_anticipo_origen_id=A.id)
        self.assertAlmostEqual(A.l10n_pe_ne_anticipo_saldo, 0.0, 2)
        self.assertEqual(self.env['account.move'].l10n_pe_ne_anticipos_pendientes(
            partner_id=self.partner.id), [])

    def test_regularizar_excede_saldo_rechaza(self):
        """Aplicar más de lo que le queda al anticipo se rechaza (evita doble consumo)."""
        A = self._anticipo_A()
        self._move(anticipo_total=400.0, l10n_pe_correlativo='10',
                   l10n_pe_ne_anticipo_origen_id=A.id)   # saldo → 190
        B2 = self._move(anticipo_total=300.0, l10n_pe_correlativo='11',
                        l10n_pe_ne_anticipo_origen_id=A.id)
        with self.assertRaises(UserError):               # 300 > 190 disponible
            B2._l10n_pe_build_invoice_request()

    def test_regularizar_enlazado_ok(self):
        """Aplicar dentro del saldo enlazado construye el XML sin error (descuento 04 + relacionado)."""
        A = self._anticipo_A()
        B = self._move(anticipo_total=118.0, l10n_pe_correlativo='10',
                       l10n_pe_ne_anticipo_origen_id=A.id)
        payload = B._l10n_pe_build_invoice_request()
        self.assertEqual(len([v for v in payload['variablesGlobales']
                              if v['codTipoVariableGlobal'] == '04']), 1)

    def test_anticipo_parcial_factor_reconstruye_monto(self):
        """SUNAT 3307: el factor del descuento 04 debe reconstruir el monto (|base·por − monto| ≤ 1)
        aun con un anticipo parcial cuyo valor NO es fracción redonda de la base (254.24 sobre 1000)."""
        move = self._move(anticipo_total=300.0, invoice_line_ids=[(0, 0, {
            'product_id': self.product.id, 'quantity': 1.0, 'price_unit': 1000.0,
            'tax_ids': [(6, 0, self.igv.ids)]})])
        vg = [v for v in move._l10n_pe_build_invoice_request()['variablesGlobales']
              if v['codTipoVariableGlobal'] == '04'][0]
        base, por, monto = (float(vg['mtoBaseImpVariableGlobal']),
                            float(vg['porVariableGlobal']), float(vg['mtoVariableGlobal']))
        self.assertLessEqual(abs(base * por - monto), 1.0)

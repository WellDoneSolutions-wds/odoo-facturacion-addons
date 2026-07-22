from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestMultiAnticipo(TransactionCase):
    """Regularización de VARIOS anticipos en una misma factura final (pagos escalonados).
    SUNAT lo soporta (N AdditionalDocumentReference + N PrepaidPayment, numIdeAnticipo 1..N).
    El dato vive en una lista JSON; el saldo de cada anticipo (doc. A) suma los `monto` de la
    lista de todas las regularizaciones que lo enlazan por `origenId`."""

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

    def _venta(self, anticipos=None, precio=1000.0):
        vals = {
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '9',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': precio, 'tax_ids': [(6, 0, self.igv.ids)]})]}
        if anticipos is not None:
            vals['l10n_pe_ne_anticipos'] = anticipos
        m = self.env['account.move'].create(vals)
        m.action_post()
        return m

    def test_descuento_04_factor_unitario(self):
        # El descuento 04 va con FACTOR UNITARIO: base = valor, factor 1.00000, monto = valor.
        payload = self._venta(anticipos=[{'doc': 'F001-00000100', 'monto': 118.0, 'tipo': '02'}],
                              precio=500.0)._l10n_pe_build_invoice_request()
        vg = [v for v in payload['variablesGlobales'] if v['codTipoVariableGlobal'] == '04'][0]
        self.assertEqual(vg['porVariableGlobal'], '1.00000')
        self.assertEqual(vg['mtoVariableGlobal'], '100.00')
        self.assertEqual(vg['mtoBaseImpVariableGlobal'], '100.00')  # base del descuento = el valor, no la base de la operacion

    def test_base_alta_cumple_regla_4322(self):
        # base 300000; anticipo total 53101.77 (valor 45001.50). Con el factor viejo a 5 decimales,
        # base*factor = 45003 -> desvio 1.50 > 1 -> SUNAT 4322. Con factor unitario: base*factor = valor exacto.
        payload = self._venta(anticipos=[{'doc': 'F001-00000100', 'monto': 53101.77, 'tipo': '02'}],
                              precio=300000.0)._l10n_pe_build_invoice_request()
        vg = [v for v in payload['variablesGlobales'] if v['codTipoVariableGlobal'] == '04'][0]
        amount = float(vg['mtoVariableGlobal']); base = float(vg['mtoBaseImpVariableGlobal']); factor = float(vg['porVariableGlobal'])
        self.assertLessEqual(abs(amount - base * factor), 1.0)  # regla SUNAT 4322
        self.assertEqual(vg['porVariableGlobal'], '1.00000')

    def test_cabecera_no_cambia_con_factor_unitario(self):
        # Paridad: la reduccion de IGV/base de cabecera es la misma (usa el valor del anticipo, no el
        # BaseAmount del descuento). Venta 590 + anticipo 118 -> base gravada 400, IGV 72, Payable 472.
        cab = self._venta(anticipos=[{'doc': 'F001-00000100', 'monto': 118.0, 'tipo': '02'}],
                          precio=500.0)._l10n_pe_build_invoice_request()['cabecera']
        self.assertEqual(cab['sumTotalAnticipos'], '118.00')
        self.assertEqual(cab['sumImpVenta'], '472.00')

    def test_lista_normalizada(self):
        m = self._venta(anticipos=[
            {'doc': 'F001-00000100', 'monto': 236.0, 'tipo': '02'},
            {'doc': 'F001-00000101', 'monto': 118.0, 'tipo': '02'},
        ])
        lst = m._l10n_pe_ne_anticipos_list()
        self.assertEqual(len(lst), 2)
        self.assertEqual(lst[0]['doc'], 'F001-00000100')
        self.assertEqual(lst[0]['monto'], 236.0)
        self.assertEqual([a['monto'] for a in lst], [236.0, 118.0])

    def test_sin_anticipos_lista_vacia(self):
        self.assertEqual(self._venta()._l10n_pe_ne_anticipos_list(), [])

    def test_dos_anticipos_dos_relacionados_numide(self):
        # venta 1180 (1000 + IGV 180); anticipos 236 (val 200) + 118 (val 100) = 354.
        payload = self._venta(anticipos=[
            {'doc': 'F001-00000100', 'monto': 236.0, 'tipo': '02'},
            {'doc': 'F001-00000101', 'monto': 118.0, 'tipo': '02'},
        ])._l10n_pe_build_invoice_request()
        rels = [r for r in payload['relacionados'] if r['indDocRelacionado'] == '2']
        self.assertEqual(len(rels), 2)
        self.assertEqual([r['numIdeAnticipo'] for r in rels], ['1', '2'])
        self.assertEqual([r['numDocRelacionado'] for r in rels], ['F001-00000100', 'F001-00000101'])
        self.assertEqual([r['mtoDocRelacionado'] for r in rels], ['236.00', '118.00'])

    def test_cabecera_suma_y_descuento_04_agregado(self):
        payload = self._venta(anticipos=[
            {'doc': 'F001-00000100', 'monto': 236.0, 'tipo': '02'},
            {'doc': 'F001-00000101', 'monto': 118.0, 'tipo': '02'},
        ])._l10n_pe_build_invoice_request()
        cab = payload['cabecera']
        self.assertEqual(cab['sumTotalAnticipos'], '354.00')       # 236 + 118
        self.assertEqual(cab['sumImpVenta'], '826.00')             # 1180 − 354
        vg = [v for v in payload['variablesGlobales'] if v['codTipoVariableGlobal'] == '04']
        self.assertEqual(len(vg), 1)                                # un 04 agregado
        self.assertEqual(vg[0]['mtoVariableGlobal'], '300.00')      # valor total (200 + 100)
        igv = [t for t in payload['tributos'] if t['ideTributo'] == '1000'][0]
        self.assertEqual(igv['mtoBaseImponible'], '700.00')        # 1000 − 300
        self.assertEqual(igv['mtoTributo'], '126.00')              # 180 − 54

    def test_un_anticipo_sigue_igual(self):
        # paridad con el caso escalar previo (lista de 1).
        payload = self._venta(anticipos=[{'doc': 'F001-00000100', 'monto': 118.0, 'tipo': '02'}],
                              precio=500.0)._l10n_pe_build_invoice_request()
        self.assertEqual(payload['cabecera']['sumTotalAnticipos'], '118.00')
        rels = [r for r in payload['relacionados'] if r['indDocRelacionado'] == '2']
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]['numIdeAnticipo'], '1')

    def test_suma_excede_total_rechaza(self):
        with self.assertRaises(UserError):
            self._venta(anticipos=[
                {'doc': 'F001-00000100', 'monto': 800.0, 'tipo': '02'},
                {'doc': 'F001-00000101', 'monto': 800.0, 'tipo': '02'},
            ], precio=1000.0)._l10n_pe_build_invoice_request()

    def test_tipo_invalido_rechaza_al_emitir(self):
        # '01' (factura normal, no de anticipo) no es un tipo válido de cat. 12 para el doc.
        # relacionado de anticipo: solo 02 (factura) / 03 (boleta). Postear no valida (solo
        # emitir), igual que 'sin doc' — el guard vive en `_l10n_pe_check_anticipo`.
        move = self._venta(anticipos=[
            {'doc': 'F001-00000100', 'monto': 118.0, 'tipo': '01'},
        ])
        with self.assertRaises(UserError):
            move._l10n_pe_build_invoice_request()

    def test_origen_id_no_numerico_se_trata_como_sin_origen(self):
        # origenId basura (import manual, dato corrupto) no debe explotar con ValueError: se
        # coerciona a None (regularización "por fuera", sin enlace a un anticipo local).
        move = self._venta(anticipos=[
            {'doc': 'F001-00000100', 'monto': 118.0, 'tipo': '02', 'origenId': 'abc'},
        ])
        lst = move._l10n_pe_ne_anticipos_list()
        self.assertIsNone(lst[0]['origenId'])
        # tampoco rompe la emisión (sin origen local, se valida como anticipo "por fuera").
        move._l10n_pe_build_invoice_request()

    def test_origen_id_invalido_en_otra_factura_no_rompe_saldo(self):
        # `_compute_l10n_pe_ne_anticipo_saldo` escanea TODAS las regularizaciones vivas del
        # sistema: una fila con origenId basura en OTRA factura no debe romper (ValueError) el
        # saldo/pendientes de un anticipo real ajeno a esa fila.
        origen = self._venta(precio=1000.0)
        origen.l10n_pe_ne_es_anticipo = True
        self._venta(anticipos=[
            {'doc': 'F001-00000200', 'monto': 50.0, 'tipo': '02', 'origenId': 'abc'},
        ])
        self.assertEqual(origen.l10n_pe_ne_anticipo_saldo, 1180.0)

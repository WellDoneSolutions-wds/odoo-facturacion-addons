from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestValidacionPreEmision(TransactionCase):
    """L1 · Motor de validación pre-emisión.

    Valida el comprobante contra las reglas SUNAT ANTES de enviarlo y devuelve findings
    accionables (nivel 'error' bloquea; 'aviso' informa). El objetivo es reemplazar el
    faultCode críptico (p.ej. 3265) por un mensaje que el emisor sí puede corregir.
    """

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        Tax = self.env['account.tax'].sudo()
        self.igv = Tax.search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        if not self.igv:
            self.igv = Tax.create({'name': 'IGV 18% (test)', 'amount_type': 'percent',
                                   'amount': 18.0, 'type_tax_use': 'sale',
                                   'l10n_pe_edi_tax_code': '1000', 'company_id': self.company.id})
        self.gratuito = self.env['account.move']._l10n_pe_ne_tax_by_code('9996')
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create({'name': 'ITEM', 'default_code': 'I1'})

    def _move(self, lineas=None, **vals):
        base = {
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-07-29',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '250',
            'invoice_line_ids': lineas or [
                (0, 0, {'product_id': self.product.id, 'quantity': 1.0, 'price_unit': 500.0,
                        'tax_ids': [(6, 0, self.igv.ids)]})],
        }
        base.update(vals)
        move = self.env['account.move'].create(base)
        move.action_post()
        return move

    def _codes(self, move):
        return {f['code'] for f in move._l10n_pe_ne_validaciones()}

    # -- el caso real F001-247 ya NO produce error (regresión del 3265) --------------------
    def test_credito_con_gratuito_no_dispara_error(self):
        move = self._move(
            lineas=[
                (0, 0, {'product_id': self.product.id, 'quantity': 1.0, 'price_unit': 500.0,
                        'tax_ids': [(6, 0, self.igv.ids)]}),
                (0, 0, {'product_id': self.product.id, 'quantity': 1.0, 'price_unit': 790.0,
                        'name': 'REGALO', 'tax_ids': [(6, 0, self.gratuito.ids)]}),
            ],
            l10n_pe_ne_forma_pago='Credito', invoice_date_due='2026-09-29',
            l10n_pe_ne_cuotas=[{'fecha': '2026-09-29', 'monto': 590.0}])
        errores = [f for f in move._l10n_pe_ne_validaciones() if f['nivel'] == 'error']
        self.assertFalse(errores, 'crédito con gratuito NO debe marcar error 3265')
        # y el guard no bloquea la construcción del request
        move._l10n_pe_build_invoice_request()

    # -- guard: si la invariante se viola, corta con un mensaje accionable (no SOAP fault) --
    def test_guard_bloquea_si_neto_supera_cobrar(self):
        move = self._move(l10n_pe_ne_forma_pago='Credito', invoice_date_due='2026-09-29',
                          l10n_pe_ne_cuotas=[{'fecha': '2026-09-29', 'monto': 590.0}])
        # Forzamos la violación de la invariante (que hoy no ocurre en el flujo normal): el
        # neto pendiente supera el importe a cobrar → el guard debe cortar antes de enviar.
        with patch.object(type(move), '_l10n_pe_credito_pendiente', return_value=99999.0):
            self.assertIn('3265', self._codes(move))
            with self.assertRaises(UserError) as e:
                move._l10n_pe_ne_asegurar_valido()
            self.assertIn('3265', str(e.exception))

    # -- aviso: cuotas que no cuadran con el neto (no bloquea) -----------------------------
    def test_cuotas_que_no_cuadran_avisan_sin_bloquear(self):
        move = self._move(l10n_pe_ne_forma_pago='Credito', invoice_date_due='2026-09-29',
                          l10n_pe_ne_cuotas=[{'fecha': '2026-09-29', 'monto': 1000.0}])
        findings = move._l10n_pe_ne_validaciones()
        avisos = [f for f in findings if f['code'] == 'cuotas-suma']
        self.assertTrue(avisos, 'cuotas que no suman el neto deben avisar')
        self.assertEqual(avisos[0]['nivel'], 'aviso')
        move._l10n_pe_ne_asegurar_valido()  # el aviso NO bloquea

    # -- aviso: Ventas al Estado con datos parciales (se omitirían todos) -------------------
    def test_estado_parcial_avisa(self):
        move = self._move(
            l10n_pe_ne_estado_expediente='EXP-2026-001',
            l10n_pe_ne_estado_proceso_seleccion='LP-002-2026')  # 2 de 4
        findings = move._l10n_pe_ne_validaciones()
        estado = [f for f in findings if f['code'] == '3146']
        self.assertTrue(estado, 'datos del Estado incompletos deben avisar')
        self.assertEqual(estado[0]['nivel'], 'aviso')

    def test_estado_completo_no_avisa(self):
        move = self._move(
            l10n_pe_ne_estado_expediente='EXP-2026-001',
            l10n_pe_ne_estado_unidad_ejecutora='001',
            l10n_pe_ne_estado_proceso_seleccion='LP-002-2026',
            l10n_pe_ne_estado_contrato='CTO-77-2026')  # los 4
        self.assertNotIn('3146', self._codes(move))

    # -- pre-flight: valida un payload SIN emitir ni persistir ------------------------------
    def _payload(self, lineas, **extra):
        return {
            'tipoDoc': '01', 'moneda': 'PEN', 'serie': 'F001',
            'cliente': {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'CLIENTE SAC'},
            'lineas': lineas, **extra,
        }

    def test_preflight_no_persiste_nada(self):
        """La prueba clave del savepoint: el pre-flight arma y postea el comprobante para
        validarlo, pero al revertir NO debe quedar ni comprobante ni producto ni stock."""
        Move = self.env['account.move']
        antes_mv = Move.search_count([])
        antes_pr = self.env['product.product'].search_count([])
        antes_sm = self.env['stock.move'].search_count([])
        Move.l10n_pe_ne_preflight(self._payload(
            [{'descripcion': 'PRODUCTO PREFLIGHT', 'cantidad': 1, 'precioUnitario': 500,
              'taxCode': '1000'}]))
        self.assertEqual(Move.search_count([]), antes_mv, 'el pre-flight dejó un comprobante')
        self.assertEqual(self.env['product.product'].search_count([]), antes_pr,
                         'el pre-flight dejó un producto en el catálogo')
        self.assertEqual(self.env['stock.move'].search_count([]), antes_sm,
                         'el pre-flight dejó un movimiento de stock')

    def test_preflight_credito_gratuito_no_bloquea(self):
        findings = self.env['account.move'].l10n_pe_ne_preflight(self._payload(
            [{'descripcion': 'ITEM', 'cantidad': 1, 'precioUnitario': 500, 'taxCode': '1000'},
             {'descripcion': 'REGALO', 'cantidad': 1, 'precioUnitario': 790, 'taxCode': '9996'}],
            formaPago={'tipo': 'Credito', 'cuotas': [{'fecha': '2026-09-29', 'monto': 590}]}))
        self.assertFalse([f for f in findings if f['nivel'] == 'error'],
                         'crédito con gratuito no debe bloquear (regresión 3265)')

    def test_preflight_estado_parcial_avisa(self):
        findings = self.env['account.move'].l10n_pe_ne_preflight(self._payload(
            [{'descripcion': 'ITEM', 'cantidad': 1, 'precioUnitario': 500, 'taxCode': '1000'}],
            ventaEstado={'expediente': 'EXP-1', 'procesoSeleccion': 'LP-2'}))  # 2 de 4
        self.assertIn('3146', {f['code'] for f in findings})

    def test_preflight_cuotas_descuadradas_avisan(self):
        findings = self.env['account.move'].l10n_pe_ne_preflight(self._payload(
            [{'descripcion': 'ITEM', 'cantidad': 1, 'precioUnitario': 500, 'taxCode': '1000'}],
            formaPago={'tipo': 'Credito', 'cuotas': [{'fecha': '2026-09-29', 'monto': 1000}]}))
        self.assertIn('cuotas-suma', {f['code'] for f in findings})

    # -- detracción (SPOT): reglas de rechazo garantizado --------------------------------
    def test_detraccion_sin_cuenta_bloquea(self):
        self.company.sudo().l10n_pe_ne_cuenta_detraccion = False  # ni en la empresa
        move = self._move(l10n_pe_ne_detraccion=True, l10n_pe_ne_detraccion_code='037',
                          l10n_pe_ne_detraccion_rate=12.0)  # sin cuenta en el comprobante
        codes = self._codes(move)
        self.assertIn('detraccion-cuenta', codes, 'detracción sin cuenta BN debe bloquear')
        self.assertNotIn('detraccion-monto', codes, 'el monto sí es > 0 (base 590 × 12%)')

    def test_detraccion_monto_cero_bloquea(self):
        move = self._move(l10n_pe_ne_detraccion=True, l10n_pe_ne_detraccion_code='028',
                          l10n_pe_ne_detraccion_rate=0.0,  # tasa 0 → monto 0
                          l10n_pe_ne_detraccion_cuenta='00-123-456789')
        self.assertIn('detraccion-monto', self._codes(move))

    def test_detraccion_completa_no_bloquea(self):
        move = self._move(l10n_pe_ne_detraccion=True, l10n_pe_ne_detraccion_code='037',
                          l10n_pe_ne_detraccion_rate=12.0,
                          l10n_pe_ne_detraccion_cuenta='00-123-456789')
        errores = [f for f in move._l10n_pe_ne_validaciones() if f['nivel'] == 'error']
        self.assertFalse(errores, 'detracción con cuenta y tasa válida no debe bloquear')

    # -- exportación (tipOperacion 0200): país del adquirente no domiciliado --------------
    def _export_move(self, pais=None):
        exp = self.env['account.move']._l10n_pe_ne_tax_by_code('9995')  # afectación exportación
        vals = {'name': 'FOREIGN BUYER CO'}
        if pais:
            vals['country_id'] = self.env['res.country'].search([('code', '=', pais)], limit=1).id
        partner = self.env['res.partner'].create(vals)
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': partner.id, 'invoice_date': '2026-07-29',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '261',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 1000.0, 'tax_ids': [(6, 0, exp.ids)]})]})
        move.action_post()
        return move

    def test_exportacion_sin_pais_bloquea(self):
        move = self._export_move(pais=None)
        self.assertEqual(move._l10n_pe_tipo_operacion(), '0200', 'debe ser exportación (0200)')
        self.assertIn('exportacion-pais', self._codes(move))

    def test_exportacion_con_pais_no_bloquea(self):
        move = self._export_move(pais='US')
        self.assertEqual(move._l10n_pe_tipo_operacion(), '0200')
        self.assertNotIn('exportacion-pais', self._codes(move))

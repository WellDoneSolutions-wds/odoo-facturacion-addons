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

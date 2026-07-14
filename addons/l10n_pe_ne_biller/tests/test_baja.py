from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, tagged
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestBillerBaja(TransactionCase):
    """Comunicación de baja (RA): anula un comprobante ya enviado vía /generator/resumenBaja.
    ID RA-AAAAMMDD-correlativo; ReferenceDate = emisión del doc anulado; IssueDate = fecha de la baja."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        self.ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.dni_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '1')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20100070970',
            'l10n_latam_identification_type_id': self.ruc_type.id})
        self.product = self.env['product.product'].create({'name': 'SERVICIO', 'default_code': 'S1'})

    def _factura(self, partner=None):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': (partner or self.partner).id,
            'invoice_date': '2026-06-20', 'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '123',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 500.0, 'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        return move

    def test_baja_request(self):
        move = self._factura()
        move.l10n_pe_biller_state = 'enviado'
        move.l10n_pe_ne_baja_motivo = 'ERROR EN EL MONTO'
        move.l10n_pe_ne_baja_correlativo = '1'
        move.l10n_pe_ne_baja_fecha = '2026-06-21'
        self.assertEqual(move.l10n_pe_ne_baja_doc, 'RA-20260621-1')   # compute del ID RA
        req = move._l10n_pe_build_baja_request()
        self.assertEqual(req['id']['ruc'], self.company.vat)
        self.assertEqual(req['id']['fechaGeneracion'], '20260621')   # patrón yyyyMMdd del facturador
        self.assertEqual(req['id']['correlativo'], '1')
        self.assertEqual(req['fecGeneracion'], '20260620')           # emisión del comprobante anulado
        self.assertEqual(req['fecComunicacion'], '20260621')
        self.assertEqual(len(req['resumenBajas']), 1)
        linea = req['resumenBajas'][0]
        self.assertEqual(linea['tipDocBaja'], '01')                  # factura
        self.assertEqual(linea['numDocBaja'], 'F001-00000123')       # serie-NUMERO (8 díg.)
        self.assertEqual(linea['desMotivoBaja'], 'ERROR EN EL MONTO')

    def test_baja_no_enviado_rechaza(self):
        move = self._factura()                       # estado por_enviar
        move.l10n_pe_ne_baja_motivo = 'X'
        with self.assertRaises(UserError):
            move._l10n_pe_check_baja()

    def test_baja_sin_motivo_rechaza(self):
        move = self._factura()
        move.l10n_pe_biller_state = 'enviado'
        with self.assertRaises(UserError):
            move._l10n_pe_check_baja()

    def test_boleta_rc_request(self):
        """Boleta (03) se anula por Resumen Diario (RC, tipEstado 3), no por RA."""
        if not self.dni_type:
            self.skipTest("sin tipo de documento DNI en la localización")
        consumidor = self.env['res.partner'].create({
            'name': 'CONSUMIDOR FINAL', 'vat': '12345678',
            'l10n_latam_identification_type_id': self.dni_type.id})
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': consumidor.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'B001', 'l10n_pe_correlativo': '77',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 100.0, 'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        self.assertEqual(move._l10n_pe_document_type(), '03')
        move.l10n_pe_biller_state = 'enviado'
        move.l10n_pe_ne_tipo_doc = '03'
        move.l10n_pe_ne_serie_emit = 'B001'
        move.l10n_pe_ne_corr_emit = '00000077'
        move.l10n_pe_ne_baja_motivo = 'ANULAR'
        move.l10n_pe_ne_baja_correlativo = '1'
        move.l10n_pe_ne_baja_fecha = '2026-06-21'
        move._l10n_pe_check_baja()                       # NO rechaza: la boleta va por RC
        self.assertEqual(move.l10n_pe_ne_baja_doc, 'RC-20260621-1')   # prefijo RC, no RA
        req = move._l10n_pe_build_rc_request()
        self.assertEqual(req['id']['fechaGeneracion'], '20260621')
        linea = req['resumenDiario'][0]
        self.assertEqual(linea['tipDocResumen'], '03')
        self.assertEqual(linea['idDocResumen'], 'B001-00000077')
        self.assertEqual(linea['tipEstado'], '3')        # 3 = anulación
        self.assertEqual(linea['fecEmision'], '2026-06-20')  # ISO en el XML
        self.assertEqual(linea['numDocUsuario'], '12345678')
        self.assertEqual(linea['totValGrabado'], '100.00')
        self.assertEqual(linea['totImpCpe'], '118.00')
        trib = linea['tributosDocResumen'][0]
        self.assertEqual(trib['ideTributoRd'], '1000')
        self.assertEqual(trib['mtoTributoRd'], '18.00')

    def _boleta_draft(self, partner, tax, correlativo='5'):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'B001', 'l10n_pe_correlativo': correlativo,
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 100.0, 'tax_ids': [(6, 0, tax.ids)]})]})
        move.l10n_pe_ne_tipo_doc = '03'
        move.l10n_pe_ne_serie_emit = 'B001'
        move.l10n_pe_ne_corr_emit = correlativo.zfill(8)
        move.l10n_pe_ne_baja_correlativo = '1'
        move.l10n_pe_ne_baja_fecha = '2026-06-21'
        return move

    def test_boleta_rc_consumidor_final(self):
        cf = self.env['res.partner'].create({'name': 'VARIOS'})   # sin vat ni tipo de documento
        linea = self._boleta_draft(cf, self.igv)._l10n_pe_build_rc_request()['resumenDiario'][0]
        self.assertEqual(linea['tipDocUsuario'], '0')             # consumidor final sin documento
        self.assertEqual(linea['numDocUsuario'], '00000000')

    def test_boleta_rc_exonerada_lleva_igv_cero(self):
        exo = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '9997')], limit=1)
        if not exo:
            self.skipTest("sin tax exonerada 9997 en la localización")
        cf = self.env['res.partner'].create({'name': 'VARIOS'})
        linea = self._boleta_draft(cf, exo, correlativo='6')._l10n_pe_build_rc_request()['resumenDiario'][0]
        self.assertEqual(linea['totValExoneado'], '100.00')
        igv = [t for t in linea['tributosDocResumen'] if t['ideTributoRd'] == '1000']
        self.assertEqual(len(igv), 1)                             # regla 2278: cada línea exige IGV 1000
        self.assertEqual(igv[0]['mtoTributoRd'], '0.00')          # en cero, no altera montos

    def _boleta_icbper(self, correlativo='8'):
        """Boleta B001 con una línea IGV + ICBPER (bolsa), lista para armar el RC.
        _l10n_pe_tributos() ya expone el ICBPER (7152) a nivel cabecera (regla 3279)."""
        if not self.igv:
            self.skipTest("sin IGV 1000 en la localización")
        icbper = self.env['account.tax'].create({
            'name': 'ICBPER', 'type_tax_use': 'sale', 'amount_type': 'fixed', 'amount': 0.50,
            'l10n_pe_edi_tax_code': '7152', 'tax_group_id': self.igv.tax_group_id.id})
        cf = self.env['res.partner'].create({'name': 'VARIOS'})
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': cf.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'B001', 'l10n_pe_correlativo': correlativo,
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 2.0,
                                         'price_unit': 45.0,
                                         'tax_ids': [(6, 0, [self.igv.id, icbper.id])]})]})
        move.l10n_pe_ne_tipo_doc = '03'
        move.l10n_pe_ne_serie_emit = 'B001'
        move.l10n_pe_ne_corr_emit = correlativo.zfill(8)
        move.l10n_pe_ne_baja_correlativo = '1'
        move.l10n_pe_ne_baja_fecha = '2026-06-21'
        return move

    def test_boleta_rc_baja_icbper_taxtotal_unico(self):
        """Regresión (obs 2355 'un solo cac:TaxTotal por tributo/ítem'): una boleta con ICBPER
        generaba DOS entradas 7152 en tributosDocResumen del RC de baja — _l10n_pe_tributos()
        ya lo incluía y además se re-agregaba. Debe quedar UNA sola."""
        linea = self._boleta_icbper()._l10n_pe_build_rc_request()['resumenDiario'][0]
        icbper = [t for t in linea['tributosDocResumen'] if t['ideTributoRd'] == '7152']
        self.assertEqual(len(icbper), 1, 'ICBPER (7152) duplicado en el RC de baja (obs 2355)')

    def test_boleta_rc_emision_icbper_taxtotal_unico(self):
        """Mismo bug/regresión en el Resumen Diario de EMISIÓN (_l10n_pe_rc_emision_item)."""
        item = self._boleta_icbper(correlativo='9')._l10n_pe_rc_emision_item(1)
        icbper = [t for t in item['tributosDocResumen'] if t['ideTributoRd'] == '7152']
        self.assertEqual(len(icbper), 1, 'ICBPER (7152) duplicado en el RC de emisión (obs 2355)')

    def test_baja_fuera_de_plazo_rechaza(self):
        vieja = fields.Date.context_today(self.partner) - timedelta(days=10)
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': vieja,
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '123',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 500.0, 'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        move.l10n_pe_biller_state = 'enviado'
        move.l10n_pe_ne_baja_motivo = 'TARDE'
        with self.assertRaises(UserError):           # factura > 7 días: se anula por nota de crédito
            move._l10n_pe_check_baja()

    def test_baja_anulado_no_reenvia(self):
        move = self._factura()
        move.l10n_pe_biller_state = 'anulado'
        move.l10n_pe_ne_baja_motivo = 'X'
        with patch('odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post') as p:
            move.action_l10n_pe_send_baja()
            p.assert_not_called()                    # un comprobante ya anulado no se reenvía
        self.assertEqual(move.l10n_pe_biller_state, 'anulado')

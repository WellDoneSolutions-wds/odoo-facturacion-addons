from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin

_TARGET = 'odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post'


@tagged('post_install', '-at_install')
class TestBillerTaxFaltante(EnvioSincronoMixin, TransactionCase):
    """Emisión cuando la tax de la afectación NO existe en el plan de la compañía.

    Caso real (demo): la BD solo tenía el IGV (1000). Una boleta exonerada (9997)
    creaba la línea SIN impuesto, `_l10n_pe_tax_info` la clasificaba con el default
    'gravado 1000' a tasa 0 y el XML salía con TaxableAmount>0 + TaxAmount=0.00 →
    rechazo SUNAT 3111 ("El monto de afectación de IGV por línea debe ser diferente
    a 0.00"). Las taxes 0% (exonerado/inafecto/exportación/gratuito) ahora se
    auto-crean como ICBPER/ISC; y una línea sin tax reconocible ya no llega al XML:
    corta antes con un error accionable."""

    def setUp(self):
        super().setUp()
        self.Move = self.env['account.move']
        self.company = self.env.company
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20448489885',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create(
            {'name': 'SERVICIO', 'default_code': 'S1'})

    def _quitar_tax(self, code):
        """Simula un plan sin la tax: le borra el código cat-05 (search deja de verla).
        TransactionCase revierte el write al terminar."""
        self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', code)]).write({'l10n_pe_edi_tax_code': False})

    def _emitir(self, lineas, **extra):
        ok = type('R', (), {'status_code': 200, 'text': '<?xml version="1.0"?><Invoice/>',
                            'headers': {}})()
        with patch(_TARGET, return_value=ok):
            res = self.Move.l10n_pe_ne_quick_emit({
                'tipoDoc': '01', 'moneda': 'PEN', 'serie': 'F001',
                'cliente': {'tipoDoc': '6', 'numDoc': '20448489885',
                            'razonSocial': 'CLIENTE SAC'},
                'lineas': lineas, **extra,
            })
        move = self.Move.browse(res['id'])
        self.assertTrue(move.exists(), 'la emisión tiene que haber creado el comprobante')
        return move

    # -- las taxes 0% se auto-crean --------------------------------------------------------

    def test_tax_cero_faltante_se_autocrea(self):
        for code in ('9997', '9998', '9995', '9996'):
            with self.subTest(code=code):
                self._quitar_tax(code)
                tax = self.Move._l10n_pe_ne_tax_by_code(code)
                self.assertTrue(tax, 'la tax %s debe auto-crearse si falta' % code)
                self.assertEqual(tax.l10n_pe_edi_tax_code, code)
                self.assertEqual(tax.amount, 0.0)
                self.assertEqual(tax.type_tax_use, 'sale')

    def test_emision_exonerada_sin_tax_en_bd(self):
        """El caso exacto de los rechazos 3111 de la demo: boleta/factura exonerada en una
        BD sin la tax 9997. Debe emitirse como EXONERADA de verdad (tipAfe 20, tributo
        9997), nunca como 'gravada con IGV 0'."""
        self._quitar_tax('9997')
        move = self._emitir([{'descripcion': 'MATRICULA 2026', 'cantidad': 1,
                              'precioUnitario': 500.0, 'taxCode': '9997',
                              'conceptoLibre': True}])
        payload = move._l10n_pe_build_invoice_request()
        d = payload['detalle'][0]
        self.assertEqual(d['tipAfeIGV'], '20')
        self.assertEqual(d['codTriIGV'], '9997')
        self.assertEqual(d['mtoIgvItem'], '0.00')
        self.assertEqual(move.amount_total, 500.0)   # exonerado: sin IGV encima
        self.assertNotIn('1000', [t['ideTributo'] for t in payload['tributos']])

    def test_emision_inafecta_sin_tax_en_bd(self):
        """Venta de lote/terreno (inafecta, 9998) — el otro caso real de la demo."""
        self._quitar_tax('9998')
        move = self._emitir([{'descripcion': 'ADELANTO DE LOTE', 'cantidad': 1,
                              'precioUnitario': 15000.0, 'taxCode': '9998',
                              'conceptoLibre': True}])
        d = move._l10n_pe_build_invoice_request()['detalle'][0]
        self.assertEqual(d['tipAfeIGV'], '30')
        self.assertEqual(d['codTriIGV'], '9998')
        self.assertEqual(move.amount_total, 15000.0)

    # -- IGV/IVAP faltante: error claro, no XML inválido ------------------------------------

    def test_igv_faltante_corta_con_error_claro(self):
        """El IGV no se auto-crea (su tasa es decisión contable): sin él la emisión debe
        cortar con un mensaje accionable, no armar una línea sin impuesto."""
        self._quitar_tax('1000')
        with self.assertRaises(UserError):
            self._emitir([{'descripcion': 'X', 'cantidad': 1,
                           'precioUnitario': 100.0, 'taxCode': '1000'}])

    # -- defensa en profundidad: línea sin tax nunca llega al XML ----------------------------

    def test_linea_sin_impuesto_no_emite_gravado_cero(self):
        """Docs creados por fuera de quick_emit (backend de Odoo, integraciones): una línea
        con precio > 0 y sin tax cat-05 reconocible corta antes de armar el XML (antes salía
        'gravada 0%' → 3111 recién en SUNAT, con mensaje críptico)."""
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': '2026-06-20', 'l10n_pe_serie': 'F001',
            'l10n_pe_correlativo': '77',
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id, 'quantity': 1.0,
                'price_unit': 100.0, 'tax_ids': [(6, 0, [])]})]})
        move.action_post()
        with self.assertRaises(UserError) as cm:
            move._l10n_pe_build_invoice_request()
        self.assertIn('impuesto', str(cm.exception).lower())

    def test_linea_precio_cero_sin_tax_no_bloquea(self):
        """La NC motivo 03 (corrección de texto) emite líneas de importe 0: sin base
        imponible no hay 3111 posible y no debe bloquearse."""
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': '2026-06-20', 'l10n_pe_serie': 'F001',
            'l10n_pe_correlativo': '78',
            'invoice_line_ids': [
                (0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                        'price_unit': 100.0,
                        'tax_ids': [(6, 0, self.Move._l10n_pe_ne_tax_by_code('1000').ids)]}),
                (0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                        'price_unit': 0.0, 'tax_ids': [(6, 0, [])]}),
            ]})
        move.action_post()
        move._l10n_pe_build_invoice_request()   # no debe levantar

    # -- anticipo sobre operación con tax auto-creada: mensaje claro, no XML inválido --------

    def test_anticipo_exonerado_mensaje_claro(self):
        """Regularizar anticipo sobre operación exonerada no está soportado (el descuento
        global 04 exige operación gravada homogénea). Con la tax 9997 recién auto-creada,
        el check existente debe seguir cortando con su mensaje claro — antes la línea sin
        tax se hacía pasar por 'gravada 0%' y el check no la frenaba."""
        self._quitar_tax('9997')
        exo = self.Move._l10n_pe_ne_tax_by_code('9997')
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': '2026-06-20', 'l10n_pe_serie': 'F001',
            'l10n_pe_correlativo': '79',
            'l10n_pe_ne_anticipos': [{'doc': 'F001-00000100', 'monto': 100.0}],
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id, 'quantity': 1.0,
                'price_unit': 500.0, 'tax_ids': [(6, 0, exo.ids)]})]})
        move.action_post()
        with self.assertRaises(UserError) as cm:
            move._l10n_pe_build_invoice_request()
        self.assertIn('gravada', str(cm.exception).lower())

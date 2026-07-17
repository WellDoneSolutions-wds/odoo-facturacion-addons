import base64

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin


@tagged('post_install', '-at_install')
class TestPleInventario(EnvioSincronoMixin, TransactionCase):
    """PLE 12.1 — Inventario Permanente en UNIDADES FÍSICAS.

    ⚠ La estructura está pendiente de validación contable: los anexos de SUNAT son escaneos.
    Estos tests fijan lo que sí se puede afirmar sin la norma: que las entradas y salidas
    caen donde deben, que el saldo se arrastra por producto, que el periodo filtra y que el
    movimiento apunta a su comprobante.

    Es el de unidades físicas y NO el valorizado a propósito: con la valorización periódica
    —el default de Odoo— el costo por movimiento sale en cero (verificado), y declararle a
    SUNAT un costo inventado sería peor que no declarar el libro.
    """

    # Periodo propio y lejano: contar los movimientos de un mes real haría que el test
    # dependiera de lo que ya haya en la BD.
    PERIODO = '202003'
    FECHA = '2020-03-10'

    def setUp(self):
        super().setUp()
        self.Move = self.env['account.move']
        self.wh = self.env['stock.warehouse'].search([('company_id', '=', self.env.company.id)], limit=1)
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.env.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        ruc = self.env['l10n_latam.identification.type'].search([('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE KARDEX', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc.id})
        self.prod = self.env['product.product'].create({
            'name': 'CLAVO KARDEX TEST', 'type': 'consu', 'is_storable': True,
            'default_code': 'CLV-K', 'l10n_pe_ne_unit_code': 'NIU'})

    def _comprar(self, qty, numero):
        return self.Move.l10n_pe_ne_create_compra({
            'proveedor': {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'PROV KARDEX'},
            'tipoComprobante': '01', 'serie': 'F001', 'numero': numero, 'fecha': self.FECHA,
            'total': qty * 2, 'lineas': [{'productId': self.prod.id, 'cantidad': qty, 'precioUnitario': 2}]})

    def _vender(self, qty, corr):
        v = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': self.FECHA,
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': corr,
            'invoice_line_ids': [(0, 0, {'product_id': self.prod.id, 'quantity': qty,
                                         'price_unit': 5, 'tax_ids': [(6, 0, self.igv.ids)]})]})
        v.action_post()
        v._l10n_pe_ne_mover_stock()
        return v

    def _lineas(self, periodo=None):
        r = self.Move.l10n_pe_ne_ple_inventario(periodo or self.PERIODO)
        txt = base64.b64decode(r['contentB64']).decode('latin-1')
        return [l for l in txt.split('\r\n') if l]

    def _mias(self, periodo=None):
        """Solo las de MI producto: la BD puede tener movimientos de otros."""
        return [l for l in self._lineas(periodo) if '|CLV-K|' in l]

    def test_la_compra_es_una_entrada_y_la_venta_una_salida(self):
        self._comprar(100, '70001')
        self._vender(30, '70002')
        ls = self._mias()
        self.assertEqual(len(ls), 2)
        entrada, salida = ls[0].split('|'), ls[1].split('|')
        self.assertEqual(entrada[7], '01')       # 8 tipo de operación: entrada
        self.assertEqual(entrada[13], '100.00')  # 14 entradas
        self.assertEqual(entrada[14], '0.00')    # 15 salidas
        self.assertEqual(salida[7], '02')        # 8 salida
        self.assertEqual(salida[13], '0.00')
        self.assertEqual(salida[14], '30.00')

    def test_el_saldo_se_arrastra(self):
        """Lo que hace legible un kardex: cada renglón dice con cuánto quedó la existencia."""
        self._comprar(100, '70010')
        self._vender(30, '70011')
        self._comprar(50, '70012')
        saldos = [l.split('|')[15] for l in self._mias()]   # 16 saldo final
        self.assertEqual(saldos, ['100.00', '70.00', '120.00'])

    def test_el_movimiento_apunta_a_su_comprobante(self):
        """Es lo que un kardex tiene que poder responder: de qué documento salió cada
        movimiento. Sale del enlace stock.move → account.move."""
        self._comprar(10, '70020')
        c = self._mias()[0].split('|')
        self.assertEqual(c[5], 'F001')    # 6 serie
        self.assertEqual(c[6], '00070020')  # 7 número
        self.assertEqual(c[4], '01')      # 5 tipo de documento

    def test_los_campos_son_posicionales(self):
        self._comprar(5, '70030')
        campos = self._mias()[0].split('|')
        self.assertEqual(len(campos), 18, 'cambió el número de campos del 12.1')
        self.assertEqual(campos[-1], '')

    def test_lleva_el_codigo_y_la_unidad_de_la_existencia(self):
        self._comprar(5, '70040')
        c = self._mias()[0].split('|')
        self.assertEqual(c[8], 'CLV-K')             # 9  código
        self.assertEqual(c[10], 'CLAVO KARDEX TEST')  # 11 descripción
        self.assertEqual(c[11], 'NIU')              # 12 unidad

    def test_el_periodo_filtra(self):
        self._comprar(7, '70050')
        self.assertEqual(len(self._mias(self.PERIODO)), 1)
        self.assertEqual(len(self._mias('202004')), 0)

    def test_un_servicio_no_entra_al_kardex(self):
        serv = self.env['product.product'].create({'name': 'SERVICIO KARDEX', 'type': 'service'})
        self.Move.l10n_pe_ne_create_compra({
            'proveedor': {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'PROV'},
            'tipoComprobante': '01', 'serie': 'F001', 'numero': '70060', 'fecha': self.FECHA,
            'total': 100, 'lineas': [{'productId': serv.id, 'cantidad': 1, 'precioUnitario': 100}]})
        self.assertFalse([l for l in self._lineas() if 'SERVICIO KARDEX' in l])

    def test_el_nombre_del_archivo_usa_el_libro_120100(self):
        self._comprar(3, '70070')
        r = self.Move.l10n_pe_ne_ple_inventario(self.PERIODO)
        self.assertIn('120100', r['filename'])
        self.assertTrue(r['filename'].startswith('LE%s' % self.env.company.vat))

    def test_periodo_invalido_rechaza(self):
        for malo in ('', '2020', '202013', 'xxxxxx'):
            with self.assertRaises(UserError):
                self.Move.l10n_pe_ne_ple_inventario(malo)

    def test_la_venta_lleva_su_serie_y_correlativo(self):
        """Regresión: se partía l10n_latam_document_number por "-", pero en una VENTA ese
        campo trae solo el número — la serie se llevaba el correlativo y el kardex declaraba
        'serie 00000063, número vacío'. La venta usa el helper de serie/correlativo."""
        self._comprar(20, '70080')
        self._vender(5, '70081')
        salida = [l for l in self._mias() if l.split('|')[7] == '02'][0].split('|')
        self.assertEqual(salida[5], 'F001', 'la serie debe ser la serie')      # 6
        self.assertEqual(salida[6], '70081', 'y el número el número')          # 7

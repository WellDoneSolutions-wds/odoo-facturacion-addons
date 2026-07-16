from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin

_TARGET = 'odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post'


@tagged('post_install', '-at_install')
class TestStockEmision(EnvioSincronoMixin, TransactionCase):
    """El puente: la emisión mueve el stock.

    En Odoo la factura NO mueve stock (account/ no menciona stock.move) — los movimientos
    vienen de un picking, que en el flujo estándar nace de un sale.order. Esta app no usa
    sale.order, así que el movimiento se crea al emitir, como hace el POS de Odoo.
    """

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        self.wh = self.env['stock.warehouse'].search([('company_id', '=', self.company.id)], limit=1)
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE STOCK', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})
        # Un BIEN con stock y un SERVICIO: la misma venta debe mover solo el primero.
        self.bien = self.env['product.product'].create({
            'name': 'BIEN CON STOCK', 'type': 'consu', 'is_storable': True})
        self.servicio = self.env['product.product'].create({
            'name': 'SERVICIO SIN STOCK', 'type': 'service'})

    def _stock(self, prod=None):
        p = prod or self.bien
        p.invalidate_recordset()
        return p.qty_available

    def _abastecer(self, qty, prod=None):
        q = self.env['stock.quant'].with_context(inventory_mode=True).create({
            'product_id': (prod or self.bien).id,
            'location_id': self.wh.lot_stock_id.id,
            'inventory_quantity': qty})
        q.action_apply_inventory()

    def _venta(self, prod, qty, tipo='01', serie='F001', corr='9001'):
        move = self.env['account.move'].create({
            'move_type': 'out_refund' if tipo == '07' else 'out_invoice',
            'partner_id': self.partner.id, 'invoice_date': '2026-07-16',
            'l10n_pe_serie': serie, 'l10n_pe_correlativo': corr,
            'invoice_line_ids': [(0, 0, {'product_id': prod.id, 'quantity': qty,
                                         'price_unit': 100.0, 'tax_ids': [(6, 0, self.igv.ids)]})]})
        move.action_post()
        return move

    # -- qué líneas mueven ---------------------------------------------------------------
    def test_solo_las_lineas_de_bien_con_stock_mueven(self):
        m = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': '2026-07-16', 'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '9010',
            'invoice_line_ids': [
                (0, 0, {'product_id': self.bien.id, 'quantity': 2, 'price_unit': 100.0,
                        'tax_ids': [(6, 0, self.igv.ids)]}),
                (0, 0, {'product_id': self.servicio.id, 'quantity': 5, 'price_unit': 50.0,
                        'tax_ids': [(6, 0, self.igv.ids)]}),
            ]})
        m.action_post()
        lineas = m._l10n_pe_ne_lineas_con_stock()
        self.assertEqual(len(lineas), 1, "solo el bien; el servicio nunca mueve stock")
        self.assertEqual(lineas.product_id, self.bien)

    def test_bien_sin_is_storable_no_mueve(self):
        """'consu' solo dice tangible; is_storable es el que decide que se rastrea."""
        suelto = self.env['product.product'].create({
            'name': 'BIEN SIN RASTREO', 'type': 'consu', 'is_storable': False})
        m = self._venta(suelto, 3, corr='9011')
        self.assertFalse(m._l10n_pe_ne_lineas_con_stock())
        self.assertFalse(m._l10n_pe_ne_mover_stock())

    # -- la dirección del movimiento ------------------------------------------------------
    def test_factura_descuenta(self):
        self._abastecer(10)
        self.assertEqual(self._stock(), 10)
        self._venta(self.bien, 3, corr='9020')._l10n_pe_ne_mover_stock()
        self.assertEqual(self._stock(), 7, "vender 3 de 10 deja 7")

    def test_nota_de_credito_repone(self):
        """Anular una venta devuelve el bien: sin esto el kardex se va en falso para siempre."""
        self._abastecer(10)
        self._venta(self.bien, 3, corr='9030')._l10n_pe_ne_mover_stock()
        self.assertEqual(self._stock(), 7)
        nc = self._venta(self.bien, 3, tipo='07', serie='FC01', corr='9031')
        nc.l10n_pe_ne_tipo_doc = '07'
        nc._l10n_pe_ne_mover_stock()
        self.assertEqual(self._stock(), 10, "la NC repone lo vendido")

    def test_nota_de_debito_no_mueve(self):
        """La ND es un cargo (mora, penalidad): no mueve bienes."""
        self._abastecer(10)
        nd = self._venta(self.bien, 1, corr='9040')
        nd.l10n_pe_ne_tipo_doc = '08'
        self.assertFalse(nd._l10n_pe_ne_mover_stock())
        self.assertEqual(self._stock(), 10)

    # -- no bloquear ----------------------------------------------------------------------
    def test_sin_existencias_vende_igual_y_queda_negativo(self):
        """Nunca se le impide vender a quien tiene el producto en la mano — coherente con la
        caja, que tampoco bloquea. El negativo es la señal visible de que falta un ajuste."""
        self._abastecer(1)
        m = self._venta(self.bien, 5, corr='9050')
        moves = m._l10n_pe_ne_mover_stock()
        self.assertTrue(moves, "el movimiento se hace igual")
        self.assertEqual(self._stock(), -4, "1 − 5 = −4, visible")

    def test_emitir_por_la_api_descuenta(self):
        """El camino real de la SPA (quick_emit), no el helper suelto."""
        self._abastecer(20)
        # El envío a SUNAT se dobla: aquí se prueba el stock, no la emisión.
        ok = type('R', (), {'status_code': 200, 'text': '<?xml version="1.0"?><Invoice/>',
                            'headers': {}})()
        with patch(_TARGET, return_value=ok):
            self.env['account.move'].l10n_pe_ne_quick_emit({
                'tipoDoc': '01', 'moneda': 'PEN',
                'cliente': {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'CLIENTE STOCK'},
                'lineas': [{'descripcion': self.bien.name, 'productId': self.bien.id,
                            'cantidad': 4, 'precioUnitario': 100, 'taxCode': '1000'}],
            })
        self.assertEqual(self._stock(), 16, "emitir por la API descuenta 4 de 20")

    # -- rechazo de SUNAT: no descontar dos veces -----------------------------------------
    def test_rechazo_revierte_el_movimiento(self):
        self._abastecer(10)
        m = self._venta(self.bien, 3, corr='9060')
        m._l10n_pe_ne_mover_stock()
        self.assertEqual(self._stock(), 7)
        m.l10n_pe_biller_state = 'rechazado'      # el write lo detecta y revierte
        self.assertEqual(self._stock(), 10, "un rechazado no existe para SUNAT: el bien vuelve")

    def test_rechazo_revierte_pero_no_borra_el_rastro(self):
        """Se compensa, no se borra: el kardex es un libro y debe mostrar que hubo intento."""
        self._abastecer(10)
        m = self._venta(self.bien, 3, corr='9061')
        m._l10n_pe_ne_mover_stock()
        m.l10n_pe_biller_state = 'rechazado'
        movs = self.env['stock.move'].search([('l10n_pe_ne_move_id', '=', m.id)])
        self.assertEqual(len(movs), 2, "queda el original y su reversa, no cero")
        self.assertEqual(len(movs.filtered('l10n_pe_ne_reversa')), 1)

    def test_rechazo_y_reemision_no_descuenta_dos_veces(self):
        """El escenario real: SUNAT rechaza, se corrige y se emite uno NUEVO. Sin la reversa,
        el bien salía dos veces del kardex por una sola venta."""
        self._abastecer(10)
        rechazada = self._venta(self.bien, 3, corr='9070')
        rechazada._l10n_pe_ne_mover_stock()
        rechazada.l10n_pe_biller_state = 'rechazado'
        # Se corrige y se emite de nuevo (otro comprobante).
        nueva = self._venta(self.bien, 3, corr='9071')
        nueva._l10n_pe_ne_mover_stock()
        self.assertEqual(self._stock(), 7, "una sola venta = un solo descuento")

    def test_revertir_es_idempotente(self):
        self._abastecer(10)
        m = self._venta(self.bien, 3, corr='9080')
        m._l10n_pe_ne_mover_stock()
        m.l10n_pe_biller_state = 'rechazado'
        self.assertEqual(self._stock(), 10)
        m._l10n_pe_ne_revertir_stock()            # a mano, otra vez
        m.write({'l10n_pe_biller_state': 'rechazado'})   # y de nuevo por el write
        self.assertEqual(self._stock(), 10, "no repone de más")

    def test_rechazo_sin_movimiento_no_hace_nada(self):
        """Un rechazado de puro servicio no tiene qué revertir."""
        m = self._venta(self.servicio, 2, corr='9090')
        m.l10n_pe_biller_state = 'rechazado'
        self.assertFalse(self.env['stock.move'].search([('l10n_pe_ne_move_id', '=', m.id)]))

    def test_rechazo_de_nota_de_credito_vuelve_a_sacar(self):
        """La reversa de una NC (que repone) es una salida: los XOR de la dirección."""
        self._abastecer(10)
        nc = self._venta(self.bien, 4, tipo='07', serie='FC01', corr='9095')
        nc.l10n_pe_ne_tipo_doc = '07'
        nc._l10n_pe_ne_mover_stock()
        self.assertEqual(self._stock(), 14, "la NC repuso")
        nc.l10n_pe_biller_state = 'rechazado'
        self.assertEqual(self._stock(), 10, "rechazada: lo repuesto se deshace")

    # -- llevaStock: lo que ACTIVA todo lo de arriba --------------------------------------
    def test_crear_producto_con_lleva_stock(self):
        """is_storable va en False por defecto en Odoo: sin mandarlo explícito, NINGÚN
        producto movería stock nunca y todo lo demás sería letra muerta."""
        Move = self.env['account.move']
        con = Move.l10n_pe_ne_create_producto(
            {'descripcion': 'BIEN CON STOCK API', 'precio': 10, 'llevaStock': True})
        self.assertTrue(con['llevaStock'])
        sin = Move.l10n_pe_ne_create_producto(
            {'descripcion': 'BIEN SIN STOCK API', 'precio': 10})
        self.assertFalse(sin['llevaStock'], "sin pedirlo, no lleva stock")

    def test_activar_stock_a_un_producto_existente(self):
        """El camino del backfill: un producto ya creado pasa a llevar existencias."""
        Move = self.env['account.move']
        p = Move.l10n_pe_ne_create_producto({'descripcion': 'RECLASIFICA STOCK', 'precio': 10})
        self.assertFalse(p['llevaStock'])
        out = Move.l10n_pe_ne_update_producto({'id': p['id'], 'llevaStock': True})
        self.assertTrue(out['llevaStock'])

    def test_el_dict_expone_las_existencias(self):
        self._abastecer(6)
        d = self.env['account.move']._l10n_pe_ne_product_dict(self.bien)
        self.assertTrue(d['llevaStock'])
        self.assertEqual(d['stock'], 6)

    def test_servicio_no_reporta_stock(self):
        """Un servicio no tiene existencias: 0 es 'no aplica', no 'se agotó'."""
        d = self.env['account.move']._l10n_pe_ne_product_dict(self.servicio)
        self.assertFalse(d['llevaStock'])
        self.assertEqual(d['stock'], 0)

    # -- COMPRAS: la otra mitad del kardex ------------------------------------------------
    def _compra(self, lineas=None, total=100, numero='5001'):
        payload = {'proveedor': {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'PROVEEDOR SAC'},
                   'tipoComprobante': '01', 'serie': 'F001', 'numero': numero,
                   'fecha': '2026-07-16', 'total': total, 'descripcion': 'COMPRA TEST'}
        if lineas is not None:
            payload['lineas'] = lineas
        return self.env['account.move'].l10n_pe_ne_create_compra(payload)

    def test_compra_con_detalle_ingresa_stock(self):
        self._abastecer(2)
        self._compra([{'productId': self.bien.id, 'cantidad': 8, 'precioUnitario': 10}], total=80)
        self.assertEqual(self._stock(), 10, "la compra detallada ENTRA al stock: 2 + 8")

    def test_compra_sin_detalle_no_mueve_nada(self):
        """El flujo de siempre (solo total) sigue igual: no toda compra es mercadería."""
        self._abastecer(5)
        self._compra(numero='5002')            # sin `lineas`
        self.assertEqual(self._stock(), 5)

    def test_compra_de_servicio_no_mueve(self):
        self._abastecer(5)
        self._compra([{'productId': self.servicio.id, 'cantidad': 3, 'precioUnitario': 50}],
                     total=150, numero='5003')
        self.assertEqual(self._stock(), 5)

    def test_compra_no_sale_por_el_camino_de_venta(self):
        """La trampa: _l10n_pe_document_type() no distingue compras — a un in_invoice le
        devuelve un tipo de VENTA (01 o 03 según el proveedor), así que sin la guarda de
        move_type una compra entraría por el camino de la venta y SACARÍA el stock."""
        self._abastecer(10)
        d = self._compra([{'productId': self.bien.id, 'cantidad': 4, 'precioUnitario': 10}],
                         total=40, numero='5004')
        compra = self.env['account.move'].browse(d['id'])
        self.assertIn(compra._l10n_pe_document_type(), ('01', '03'))   # la trampa sigue ahí
        self.assertFalse(compra._l10n_pe_ne_mover_stock(), "la guarda la corta")
        self.assertEqual(self._stock(), 14, "10 + 4: entró, no salió")

    def test_compra_no_crea_productos_en_el_catalogo(self):
        """El proveedor los llama a su manera: crearlos aquí llenaría el catálogo de
        duplicados. Sin producto elegido, la línea es solo un importe."""
        antes = self.env['product.product'].search_count([])
        self._compra([{'descripcion': 'ALGO QUE NO EXISTE EN EL CATALOGO', 'cantidad': 2,
                       'precioUnitario': 10}], total=20, numero='5005')
        self.assertEqual(self.env['product.product'].search_count([]), antes)

    def test_compra_con_cantidad_cero_rechaza(self):
        with self.assertRaises(UserError):
            self._compra([{'productId': self.bien.id, 'cantidad': 0, 'precioUnitario': 10}],
                         total=10, numero='5006')

    def test_compra_detalle_debe_cuadrar_con_el_total(self):
        """El detalle MANDA: la compra se registra por la suma de las líneas. Si el total no
        cuadra, lo que entra al Registro de Compras no es lo que el usuario cree — se corta."""
        with self.assertRaises(UserError):
            self._compra([{'productId': self.bien.id, 'cantidad': 10, 'precioUnitario': 5}],
                         total=200, numero='5007')   # detalle 50 vs total 200

    def test_compra_que_cuadra_pasa(self):
        d = self._compra([{'productId': self.bien.id, 'cantidad': 10, 'precioUnitario': 5}],
                         total=50, numero='5008')
        self.assertEqual(d['total'], 50)

    def test_compra_tolera_el_centimo(self):
        """El detalle se teclea a mano: no debe pelear por un céntimo de redondeo."""
        d = self._compra([{'productId': self.bien.id, 'cantidad': 3, 'precioUnitario': 3.33}],
                         total=10, numero='5009')    # 9.99 vs 10
        self.assertTrue(d['id'])

    def test_compra_costo_negativo_rechaza(self):
        with self.assertRaises(UserError):
            self._compra([{'productId': self.bien.id, 'cantidad': 1, 'precioUnitario': -5}],
                         total=-5, numero='5010')

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin

_TARGET = 'odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post'


@tagged('post_install', '-at_install')
class TestConceptoLibre(EnvioSincronoMixin, TransactionCase):
    """Conceptos de un solo uso: se emiten tal cual y NO entran al catálogo.

    El caso real es el detalle de un servicio, distinto en cada comprobante:

        POR EL SERVICIO DE TRANSPORTE LIMA-JULIACA MALLA ELECTROSOLDADA CON FORRO Y
        OTROS. DAM NRO. 118-2026-10-231336-01-7-00

    Como producto sería basura: uno nuevo por factura, cada uno con su número de DAM en el
    nombre, y el catálogo deja de servir para vender y para el kardex.
    """

    CONCEPTO = ('POR EL SERVICIO DE TRANSPORTE LIMA-JULIACA MALLA ELECTROSOLDADA '
                'CON FORRO Y OTROS. DAM NRO. 118-2026-10-231336-01-7-00')

    def setUp(self):
        super().setUp()
        self.Move = self.env['account.move']
        self.Product = self.env['product.product']

    def _emitir(self, lineas):
        """Emite por el camino real de la SPA y devuelve el account.move.

        El id sale del resultado de quick_emit: buscar por correlativo no sirve — lo asigna la
        serie, no el payload, y un search vacío devolvía un recordset falsy que después hacía
        pasar dominios como ('x', '=', False) contra TODA la tabla.
        """
        ok = type('R', (), {'status_code': 200, 'text': '<?xml version="1.0"?><Invoice/>',
                            'headers': {}})()
        with patch(_TARGET, return_value=ok):
            res = self.Move.l10n_pe_ne_quick_emit({
                'tipoDoc': '01', 'moneda': 'PEN', 'serie': 'F001',
                'cliente': {'tipoDoc': '6', 'numDoc': '20448489885',
                            'razonSocial': 'CORP FREDD IMPORT SAC'},
                'lineas': lineas,
            })
        move = self.Move.browse(res['id'])
        self.assertTrue(move.exists(), 'la emisión tiene que haber creado el comprobante')
        return move

    # -- el catálogo no se ensucia ---------------------------------------------------------
    def test_un_concepto_libre_no_crea_producto(self):
        antes = self.Product.search_count([])
        prod = self.Move._l10n_pe_ne_quick_product(
            {'descripcion': self.CONCEPTO, 'precioUnitario': 5150, 'conceptoLibre': True})
        self.assertFalse(prod, 'no hay producto que resolver')
        self.assertEqual(self.Product.search_count([]), antes, 'el catálogo no se tocó')
        self.assertFalse(self.Product.search([('name', '=', self.CONCEPTO)]))

    def test_sin_la_marca_sigue_creando(self):
        """El auto-crear es a propósito para quien teclea lo que vende (una tienda va armando
        su catálogo sola). El concepto libre es la salida, no el default."""
        prod = self.Move._l10n_pe_ne_quick_product(
            {'descripcion': 'PRODUCTO TECLEADO NORMAL TEST', 'precioUnitario': 10})
        self.assertTrue(prod, 'sin la marca, se crea como siempre')

    def test_no_se_engancha_a_un_producto_que_se_llame_igual(self):
        """Respetar la marca al pie de la letra: si se enganchara por nombre, la línea movería
        el stock de ese producto — justo lo que el usuario dijo que esto no era."""
        existente = self.Product.create({'name': 'FLETE LIMA-JULIACA TEST', 'is_storable': True})
        prod = self.Move._l10n_pe_ne_quick_product(
            {'descripcion': 'FLETE LIMA-JULIACA TEST', 'precioUnitario': 100,
             'conceptoLibre': True})
        self.assertFalse(prod, 'no se enlaza aunque exista uno con el mismo nombre')
        self.assertTrue(existente.exists(), 'y el que ya estaba no se toca')

    def test_emitir_por_la_api_no_deja_producto_atras(self):
        """El camino real de la SPA."""
        antes = self.Product.search_count([])
        self._emitir([{'descripcion': self.CONCEPTO, 'cantidad': 1, 'precioUnitario': 4364.407,
                       'taxCode': '1000', 'unidad': 'ZZ', 'conceptoLibre': True}])
        self.assertEqual(self.Product.search_count([]), antes,
                         'emitir un concepto no agrega productos')

    # -- pero el concepto SÍ se emite ------------------------------------------------------
    def test_el_concepto_llega_a_la_factura_y_al_xml(self):
        """La línea sin producto tiene que seguir contando: importe, IGV y desItem del XML.
        Si se cayera del detalle, la factura saldría sin lo único que se vendió."""
        move = self._emitir([{'descripcion': self.CONCEPTO, 'cantidad': 1,
                              'precioUnitario': 4364.407, 'taxCode': '1000', 'unidad': 'ZZ',
                              'conceptoLibre': True}])
        linea = move.invoice_line_ids[0]
        self.assertFalse(linea.product_id, 'la línea no quedó atada a ningún producto')
        self.assertEqual(linea.name, self.CONCEPTO)

        detalle = move._l10n_pe_detalle()
        self.assertEqual(len(detalle), 1, 'el concepto NO se cayó del detalle del XML')
        self.assertEqual(detalle[0]['desItem'], self.CONCEPTO)
        self.assertEqual(detalle[0]['codProducto'], '-', 'sin producto, el código va vacío')
        self.assertEqual(detalle[0]['codUnidadMedida'], 'ZZ', 'servicio (cat. 03)')

    def test_un_concepto_libre_no_mueve_stock(self):
        """Sin producto no hay existencias que descontar. Es la otra cara de no crearlo: con el
        auto-crear, cada factura inventaba un producto y le abría su kardex."""
        move = self._emitir([{'descripcion': self.CONCEPTO, 'cantidad': 1, 'precioUnitario': 100,
                              'taxCode': '1000', 'conceptoLibre': True}])
        movs = self.env['stock.move'].search([('l10n_pe_ne_move_id', '=', move.id)])
        self.assertFalse(movs, 'un concepto no tiene existencias')

    def test_conviven_un_producto_y_un_concepto_en_la_misma_factura(self):
        """Media factura del catálogo y media tecleada: cada línea sigue su propia regla."""
        bien = self.Product.create({'name': 'BIEN CATALOGO CONCEPTO TEST', 'type': 'consu',
                                    'is_storable': True, 'list_price': 118})
        antes = self.Product.search_count([])
        move = self._emitir([
            {'descripcion': bien.name, 'productId': bien.id, 'cantidad': 2,
             'precioUnitario': 100, 'taxCode': '1000'},
            {'descripcion': self.CONCEPTO, 'cantidad': 1, 'precioUnitario': 50,
             'taxCode': '1000', 'conceptoLibre': True},
        ])
        lineas = move.invoice_line_ids.sorted('id')
        self.assertEqual(lineas[0].product_id, bien, 'la del catálogo mantiene su producto')
        self.assertFalse(lineas[1].product_id, 'la tecleada no')
        self.assertEqual(self.Product.search_count([]), antes, 'y no nació ningún producto')

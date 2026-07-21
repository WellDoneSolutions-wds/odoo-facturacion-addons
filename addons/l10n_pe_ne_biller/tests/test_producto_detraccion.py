from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestProductoDetraccion(TransactionCase):
    """Sujeto a detracción (cat. 54) como dato del CATÁLOGO: se marca una vez en el
    producto y Emitir lo usa para detectar operaciones mixtas (RS 183-2004 art. 19:
    los comprobantes de operaciones sujetas al SPOT no incluyen operaciones distintas).
    Solo el CÓDIGO vive aquí — la tasa cambia por resolución y la sugiere el front."""

    def setUp(self):
        super().setUp()
        self.Move = self.env['account.move']

    def test_crear_producto_con_detraccion(self):
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'SERVICIO DE TRANSPORTE DE CARGA',
            'precio': 500.0, 'taxCode': '1000', 'tipo': 'servicio',
            'detraCod': '027',
        })
        self.assertEqual(d['detraCod'], '027')
        p = self.env['product.product'].browse(d['id'])
        self.assertEqual(p.l10n_pe_ne_detraccion_cod, '027')

    def test_producto_sin_detraccion_expone_vacio(self):
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'CEMENTO SOL', 'precio': 30.0, 'taxCode': '1000',
        })
        self.assertEqual(d['detraCod'], '')

    def test_update_cambia_y_limpia(self):
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'ALQUILER DE MAQUINARIA', 'precio': 1000.0, 'detraCod': '022',
        })
        d2 = self.Move.l10n_pe_ne_update_producto({'id': d['id'], 'detraCod': '019'})
        self.assertEqual(d2['detraCod'], '019')
        d3 = self.Move.l10n_pe_ne_update_producto({'id': d['id'], 'detraCod': ''})
        self.assertEqual(d3['detraCod'], '')

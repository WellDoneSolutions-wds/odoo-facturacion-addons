from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin


@tagged('post_install', '-at_install')
class TestMargen(EnvioSincronoMixin, TransactionCase):
    """Margen y actualización de precios desde la compra.

    Todo va CON IGV, que es la convención de la app: el margen se aplica sobre el bruto y el
    resultado es el precio de vitrina, sin desarmar el impuesto para pensar el negocio.
    """

    def setUp(self):
        super().setUp()
        self.Move = self.env['account.move']

    def _compra(self, prod_id, costo, numero, actualizar=False):
        return self.Move.l10n_pe_ne_create_compra({
            'proveedor': {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'PROV MARGEN'},
            'tipoComprobante': '01', 'serie': 'F001', 'numero': numero, 'fecha': '2026-07-16',
            'total': costo, 'lineas': [{'productId': prod_id, 'cantidad': 1,
                                        'precioUnitario': costo, 'actualizarPrecio': actualizar}]})

    # -- el cálculo ------------------------------------------------------------------------
    def test_el_margen_se_aplica_sobre_el_precio_con_igv(self):
        # 4.00 de costo + 30% = 5.20 de vitrina. Ambos con IGV.
        self.assertEqual(self.Move._l10n_pe_ne_precio_con_margen(4.00, 30), 5.20)
        self.assertEqual(self.Move._l10n_pe_ne_precio_con_margen(3.50, 30), 4.55)
        self.assertEqual(self.Move._l10n_pe_ne_precio_con_margen(10.00, 0), 10.00)

    def test_sin_margen_propio_usa_el_del_negocio(self):
        self.env['ir.config_parameter'].sudo().set_param('l10n_pe_ne.margen_default', '50')
        self.assertEqual(self.Move._l10n_pe_ne_precio_con_margen(10.00, None), 15.00)

    def test_el_default_del_negocio_es_configurable_en_caliente(self):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('l10n_pe_ne.margen_default', '25')
        self.assertEqual(self.Move._l10n_pe_ne_margen_default(), 25.0)
        icp.set_param('l10n_pe_ne.margen_default', 'no-es-un-numero')
        self.assertEqual(self.Move._l10n_pe_ne_margen_default(), 30.0, 'valor basura → default')

    # -- el producto guarda su margen ------------------------------------------------------
    def test_el_producto_guarda_su_margen(self):
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'BROCA MARGEN TEST', 'precio': 5.20, 'tipo': 'bien', 'margen': 30})
        self.assertEqual(d['margen'], 30)

    # -- la compra y los precios -----------------------------------------------------------
    def test_la_compra_guarda_el_costo_siempre(self):
        """El costo es un hecho del documento: es lo que se pagó."""
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'COSTO TEST', 'precio': 10, 'tipo': 'bien', 'margen': 30})
        self._compra(d['id'], 4.00, '80001')
        p = self.env['product.product'].browse(d['id'])
        p.invalidate_recordset()
        self.assertEqual(p.standard_price, 4.00)

    def test_sin_confirmar_NO_toca_el_precio_de_venta(self):
        """Cambiar la vitrina sin que nadie se entere sería peor que no hacerlo."""
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'PRECIO INTACTO TEST', 'precio': 9.99, 'tipo': 'bien', 'margen': 30})
        self._compra(d['id'], 4.00, '80002', actualizar=False)
        p = self.env['product.product'].browse(d['id'])
        p.invalidate_recordset()
        self.assertEqual(p.list_price, 9.99, 'el precio de venta no se toca sin confirmar')
        self.assertEqual(p.standard_price, 4.00, 'pero el costo sí se guarda')

    def test_confirmando_recalcula_el_precio_con_el_margen_del_producto(self):
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'PRECIO ACTUALIZA TEST', 'precio': 5.20, 'tipo': 'bien', 'margen': 30})
        self._compra(d['id'], 4.50, '80003', actualizar=True)   # subió el costo
        p = self.env['product.product'].browse(d['id'])
        p.invalidate_recordset()
        self.assertEqual(p.list_price, 5.85, '4.50 + 30%')
        self.assertEqual(p.standard_price, 4.50)

    def test_un_producto_sin_margen_propio_usa_el_del_negocio(self):
        self.env['ir.config_parameter'].sudo().set_param('l10n_pe_ne.margen_default', '30')
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'SIN MARGEN PROPIO TEST', 'precio': 1, 'tipo': 'bien'})
        self._compra(d['id'], 10.00, '80004', actualizar=True)
        p = self.env['product.product'].browse(d['id'])
        p.invalidate_recordset()
        self.assertEqual(p.list_price, 13.00)

    def test_margen_CERO_no_se_confunde_con_sin_margen(self):
        """En Python 0 == False: un `if not margen` convertiría el 0% en el default y el
        producto de promoción (vendido al costo) saldría 30% más caro sin pedirlo."""
        self.assertEqual(self.Move._l10n_pe_ne_precio_con_margen(10.00, 0), 10.00)
        self.assertNotEqual(self.Move._l10n_pe_ne_precio_con_margen(10.00, 0),
                            self.Move._l10n_pe_ne_precio_con_margen(10.00, None))

    # -- crear el producto desde la línea de la compra --------------------------------------
    def test_crear_desde_la_compra_guarda_costo_margen_y_precio(self):
        """Lo que manda el modal "Crear desde la compra": el costo sale de la factura y el
        precio ya viene calculado con el margen. Los tres tienen que quedar grabados — el
        producto se crea ANTES de registrar la compra, así que no puede depender de que el
        registro después le reponga el costo."""
        d = self.Move.l10n_pe_ne_create_producto({
            'descripcion': 'BROCA DESDE COMPRA TEST', 'codigo': 'BRC-DC-01',
            'precio': 5.20, 'costo': 4.00, 'margen': 30, 'tipo': 'bien', 'llevaStock': True})
        p = self.env['product.product'].browse(d['id'])
        self.assertEqual(p.standard_price, 4.00, 'el costo de la factura del proveedor')
        self.assertEqual(p.list_price, 5.20, 'con IGV, ya con el margen aplicado')
        self.assertEqual(p.l10n_pe_ne_margen, 30)
        self.assertTrue(p.is_storable)

    def test_emitir_no_le_inventa_costo_al_producto_que_auto_crea(self):
        """Al emitir no viene `costo` y no hay que deducirlo del precio de venta: el costo es
        lo que se pagó, no lo que se cobra."""
        p = self.Move._l10n_pe_ne_quick_product(
            {'descripcion': 'AUTO CREADO AL EMITIR TEST', 'precioUnitario': 100})
        self.assertEqual(p.standard_price, 0)

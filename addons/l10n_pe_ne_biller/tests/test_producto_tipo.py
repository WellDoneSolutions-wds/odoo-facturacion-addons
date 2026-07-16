from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestProductoTipo(TransactionCase):
    """Tipo del producto: 'bien' (consu) vs 'servicio' (service).

    Antes se creaba SIEMPRE como 'service', lo que además contradecía a SUNAT: sin unidad se
    emite NIU (un bien). Ahora el tipo lo manda el usuario y, cuando no hay quién elija (el
    producto se auto-crea al emitir), se deduce de la unidad — la única señal de la línea.
    """

    def setUp(self):
        super().setUp()
        self.Move = self.env['account.move']

    # -- la regla, aislada --------------------------------------------------------------
    def test_tipo_explicito_manda_sobre_la_unidad(self):
        # Un servicio facturado por hora (HUR) sigue siendo servicio si el usuario lo dijo.
        self.assertEqual(self.Move._l10n_pe_ne_tipo_producto('servicio', 'HUR'), 'service')
        # Y un bien con unidad de servicio (ZZ) es un bien si el usuario lo dijo.
        self.assertEqual(self.Move._l10n_pe_ne_tipo_producto('bien', 'ZZ'), 'consu')

    def test_sin_tipo_lo_deduce_de_la_unidad(self):
        self.assertEqual(self.Move._l10n_pe_ne_tipo_producto(None, 'ZZ'), 'service')
        for unidad in ('NIU', 'KGM', 'MTR', 'BX'):
            self.assertEqual(self.Move._l10n_pe_ne_tipo_producto(None, unidad), 'consu', unidad)

    def test_sin_tipo_ni_unidad_es_bien(self):
        """Coincide con lo que se le declara a SUNAT: sin unidad se emite NIU (bien), y con
        el default de Odoo (consu). Antes quedaba 'service' y se contradecían."""
        self.assertEqual(self.Move._l10n_pe_ne_tipo_producto(None, None), 'consu')
        self.assertEqual(self.Move._l10n_pe_ne_tipo_producto('', ''), 'consu')

    def test_acepta_el_vocabulario_de_odoo_y_variantes(self):
        self.assertEqual(self.Move._l10n_pe_ne_tipo_producto('SERVICIO'), 'service')
        self.assertEqual(self.Move._l10n_pe_ne_tipo_producto('service'), 'service')
        self.assertEqual(self.Move._l10n_pe_ne_tipo_producto('Bien'), 'consu')
        self.assertEqual(self.Move._l10n_pe_ne_tipo_producto('consu'), 'consu')

    # -- el CRUD que usa la SPA ---------------------------------------------------------
    def test_crear_producto_respeta_el_tipo(self):
        bien = self.Move.l10n_pe_ne_create_producto(
            {'descripcion': 'MARTILLO TIPO TEST', 'precio': 25, 'tipo': 'bien'})
        self.assertEqual(bien['tipo'], 'bien')
        serv = self.Move.l10n_pe_ne_create_producto(
            {'descripcion': 'ASESORIA TIPO TEST', 'precio': 100, 'tipo': 'servicio'})
        self.assertEqual(serv['tipo'], 'servicio')

    def test_crear_sin_tipo_deduce_de_unidad(self):
        serv = self.Move.l10n_pe_ne_create_producto(
            {'descripcion': 'CONSULTORIA TIPO TEST', 'precio': 100, 'unidad': 'ZZ'})
        self.assertEqual(serv['tipo'], 'servicio')

    def test_actualizar_producto_cambia_el_tipo(self):
        p = self.Move.l10n_pe_ne_create_producto(
            {'descripcion': 'RECLASIFICABLE TIPO TEST', 'precio': 10, 'tipo': 'servicio'})
        self.assertEqual(p['tipo'], 'servicio')
        out = self.Move.l10n_pe_ne_update_producto({'id': p['id'], 'tipo': 'bien'})
        self.assertEqual(out['tipo'], 'bien')

    def test_cambiar_la_unidad_no_reclasifica_a_su_espalda(self):
        """Un bien ya clasificado no se vuelve servicio por editarle la unidad a ZZ: en el
        update el tipo solo cambia si viene explícito."""
        p = self.Move.l10n_pe_ne_create_producto(
            {'descripcion': 'BIEN ESTABLE TIPO TEST', 'precio': 10, 'tipo': 'bien'})
        out = self.Move.l10n_pe_ne_update_producto({'id': p['id'], 'unidad': 'ZZ'})
        self.assertEqual(out['tipo'], 'bien')

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

    # -- backfill: los que quedaron mal por el default viejo -------------------------------
    def test_propone_los_servicios_que_parecen_bienes(self):
        """Hasta hace poco TODO producto nacía como servicio, así que un catálogo existente
        tiene tornillos declarados como servicios — y un servicio no lleva stock."""
        mal = self.env['product.product'].create({
            'name': 'MARTILLO MAL CLASIFICADO', 'type': 'service', 'l10n_pe_ne_unit_code': 'NIU'})
        r = self.Move.l10n_pe_ne_revisar_tipos()
        ids = [p['id'] for p in r['propuestas']]
        self.assertIn(mal.id, ids)
        prop = [p for p in r['propuestas'] if p['id'] == mal.id][0]
        self.assertEqual(prop['tipoPropuesto'], 'bien')
        self.assertEqual(prop['unidad'], 'NIU')

    def test_no_propone_tocar_un_servicio_de_verdad(self):
        """Unidad ZZ es la del catálogo de servicios: ese se queda como está."""
        serv = self.env['product.product'].create({
            'name': 'CONSULTORIA BIEN CLASIFICADA', 'type': 'service', 'l10n_pe_ne_unit_code': 'ZZ'})
        r = self.Move.l10n_pe_ne_revisar_tipos()
        self.assertNotIn(serv.id, [p['id'] for p in r['propuestas']])

    def test_no_propone_tocar_lo_ya_clasificado(self):
        bien = self.env['product.product'].create({'name': 'YA ES BIEN', 'type': 'consu'})
        r = self.Move.l10n_pe_ne_revisar_tipos()
        self.assertNotIn(bien.id, [p['id'] for p in r['propuestas']])

    def test_revisar_no_cambia_nada(self):
        """PROPONE, no decide: la regla puede equivocarse (el form trae NIU por defecto, así
        que una consultora que no lo cambió tendría servicios propuestos como bienes)."""
        mal = self.env['product.product'].create({
            'name': 'NO ME TOQUES TODAVIA', 'type': 'service', 'l10n_pe_ne_unit_code': 'NIU'})
        self.Move.l10n_pe_ne_revisar_tipos()
        mal.invalidate_recordset()
        self.assertEqual(mal.type, 'service', 'revisar no debe escribir nada')

    def test_aplica_solo_los_confirmados(self):
        """El usuario elige: se aplica a los ids que mandó, no a toda la propuesta."""
        a = self.env['product.product'].create({
            'name': 'RECLASIFICA A', 'type': 'service', 'l10n_pe_ne_unit_code': 'NIU'})
        b = self.env['product.product'].create({
            'name': 'RECLASIFICA B', 'type': 'service', 'l10n_pe_ne_unit_code': 'NIU'})
        out = self.Move.l10n_pe_ne_aplicar_tipos({'ids': [a.id], 'tipo': 'bien'})
        self.assertEqual(out['actualizados'], 1)
        a.invalidate_recordset(); b.invalidate_recordset()
        self.assertEqual(a.type, 'consu')
        self.assertEqual(b.type, 'service', 'el que no se confirmó no se toca')

    def test_aplicar_sin_ids_no_hace_nada(self):
        self.assertEqual(self.Move.l10n_pe_ne_aplicar_tipos({'ids': []})['actualizados'], 0)

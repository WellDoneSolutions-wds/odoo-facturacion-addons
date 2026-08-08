# -*- coding: utf-8 -*-
"""Régimen tributario (F1): resolución, muro de emisión, reglas L1, series, API y bitácora.

El caso que justifica la feature: un NRUS que emite una factura NO paga una multa — queda
INCLUIDO en el RMT/Régimen General de forma retroactiva al mes de emisión (D. Leg. 937 art.
16.2). Por eso el muro vive en el modelo y estos tests lo ejercen por la vía real (quick_emit),
no solo llamando al guard.

Y el invariante que ninguna de las dos cosas puede romper: compañía SIN régimen = legacy = sin
gating. Todos los tenants ya dados de alta caen ahí.
"""
import base64
import io
import json
from unittest.mock import patch

import xlsxwriter

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin, L10nPeSeedMixin
from ..models.l10n_pe_ne_regimen import REGIMENES, TIPOS_NRUS, TIPOS_TODOS

# Cabeceras de la hoja 'Ventas' de la emisión masiva (orden exacto de la plantilla).
_HEADERS_MASIVA = ["venta", "tipo", "serie", "fecha", "tipo doc cliente", "num doc cliente",
                   "cliente", "codigo producto", "producto", "cantidad", "precio unitario",
                   "descuento %", "afectacion", "bolsa", "moneda"]


@tagged('post_install', '-at_install')
class TestRegimen(L10nPeSeedMixin, EnvioSincronoMixin, TransactionCase):

    def setUp(self):
        super().setUp()   # RUC + IGV (self.igv)
        self.Move = self.env['account.move']
        self.company = self.env.company
        # Estado conocido: sin régimen. El test no debe depender de la BD que toque.
        self._set()
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20448489885',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create(
            {'name': 'SERVICIO REG', 'default_code': 'SREG'})

    # ------------------------------------------------------------------ helpers
    def _set(self, regimen=False, fecha=False, categoria=False):
        self.env.company.sudo().write({
            'l10n_pe_ne_regimen': regimen,
            'l10n_pe_ne_regimen_fecha': fecha,
            'l10n_pe_ne_nrus_categoria': categoria,
        })

    def _emisor(self):
        """Usuario emisor NORMAL (sin poderes de plataforma), el caso de todos los días."""
        return self.env['res.users'].sudo().create({
            'name': 'Emisor Regimen', 'login': 'emisor.regimen@test',
            'company_id': self.env.company.id, 'company_ids': [(6, 0, [self.env.company.id])],
            'group_ids': [(4, self.env.ref('base.group_user').id),
                          (4, self.env.ref('l10n_pe_ne_biller.group_l10n_pe_ne_emisor').id)],
        })

    def _payload(self, tipo='01', **extra):
        return dict({
            'tipoDoc': tipo, 'moneda': 'PEN',
            'cliente': {'tipoDoc': '6', 'numDoc': '20448489885', 'razonSocial': 'CLIENTE SAC'},
            'lineas': [{'descripcion': 'SERVICIO REG', 'cantidad': 1, 'precio': 100.0,
                        'taxCode': '1000'}],
        }, **extra)

    def _move(self, taxes=None, **extra):
        m = self.Move.create(dict({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_date': '2026-07-30', 'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id, 'quantity': 1.0, 'price_unit': 100.0,
                'tax_ids': [(6, 0, (taxes or self.igv).ids)]})],
        }, **extra))
        m.action_post()
        return m

    def _codes(self, move):
        return {f['code']: f for f in move._l10n_pe_ne_validaciones()}

    def _lote(self, rows):
        """Sube un Excel de emisión masiva por la vía real y devuelve el reporte de validación
        previa (mismo helper que test_masivo, replicado para no acoplar las dos baterías)."""
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {'in_memory': True})
        ws = wb.add_worksheet('Ventas')
        for c, h in enumerate(_HEADERS_MASIVA):
            ws.write(0, c, h)
        for r, row in enumerate(rows, 1):
            for c, val in enumerate(row):
                ws.write(r, c, val)
        wb.close()
        return self.env['l10n_pe_ne.lote'].l10n_pe_ne_crear_lote({
            'filename': 'regimen.xlsx',
            'contentB64': base64.b64encode(buf.getvalue()).decode('ascii')})

    # ============================================================ resolución
    def test_sin_regimen_es_legacy_sin_gating(self):
        """El invariante innegociable: ninguna compañía existente cambia de comportamiento."""
        self.assertIsNone(self.company.l10n_pe_ne_tipos_permitidos())
        for tipo in TIPOS_TODOS:
            self.assertTrue(self.company.l10n_pe_ne_tipo_permitido(tipo), tipo)

    def test_nrus_bloquea_los_cuatro_prohibidos(self):
        self._set('nrus')
        for tipo in ('01', '04', '20', '40'):
            self.assertFalse(self.company.l10n_pe_ne_tipo_permitido(tipo), tipo)

    def test_nrus_permite_boleta_notas_y_guias(self):
        self._set('nrus')
        for tipo in TIPOS_NRUS:
            self.assertTrue(self.company.l10n_pe_ne_tipo_permitido(tipo), tipo)
        # Decisión explícita (incertidumbre legal documentada): NC/ND quedan permitidas.
        self.assertTrue(self.company.l10n_pe_ne_tipo_permitido('07'))
        self.assertTrue(self.company.l10n_pe_ne_tipo_permitido('08'))

    def test_rer_rmt_general_permiten_todo(self):
        for regimen in ('rer', 'rmt', 'general'):
            self._set(regimen)
            self.assertEqual(self.company.l10n_pe_ne_tipos_permitidos(), set(TIPOS_TODOS),
                             regimen)

    def test_regimen_desconocido_se_trata_como_legacy(self):
        """Un valor sucio (migración a medias, escritura por RPC) NO puede reventar ni, peor,
        bloquear una emisión legítima. Se escribe por SQL: el Selection rechazaría el valor."""
        self.env.cr.execute(
            "UPDATE res_company SET l10n_pe_ne_regimen = 'marciano' WHERE id = %s",
            (self.company.id,))
        self.company.invalidate_recordset(['l10n_pe_ne_regimen'])
        self.assertIsNone(self.company.l10n_pe_ne_tipos_permitidos())
        self.assertTrue(self.company.l10n_pe_ne_tipo_permitido('01'))

    def test_catalogo_y_campo_no_divergen(self):
        """El Selection del campo se construye DESDE el catálogo: si alguien agrega un régimen
        en un solo sitio, esto lo caza."""
        del_campo = dict(self.company._fields['l10n_pe_ne_regimen'].selection)
        self.assertEqual(set(del_campo), set(REGIMENES))
        for cod, nombre in del_campo.items():
            self.assertEqual(nombre, REGIMENES[cod][0])

    # ================================================================== muro
    def test_muro_rechaza_y_audita(self):
        self._set('nrus')
        AM = self.env['account.move'].with_user(self._emisor())
        # try/except (NO assertRaises): el assertRaises de Odoo envuelve en savepoint y su
        # rollback se llevaría la fila de auditoría que justamente queremos verificar.
        lanzo = False
        try:
            AM._l10n_pe_ne_check_regimen('01')
        except UserError as e:
            lanzo = True
            self.assertIn('937', str(e))   # el mensaje dice POR QUÉ, no solo que no
        self.assertTrue(lanzo)
        rechazo = self.env['l10n_pe_ne.rubro_auditoria'].sudo().search(
            [('company_id', '=', self.company.id), ('campo', '=', 'rechazo-regimen:01')])
        self.assertTrue(rechazo)
        self.assertEqual(rechazo[0].antes, 'nrus')

    def test_muro_legacy_pasa(self):
        AM = self.env['account.move'].with_user(self._emisor())
        AM._l10n_pe_ne_check_regimen('01')   # no lanza

    def test_muro_sin_bypass_de_admin(self):
        """H3. `admin@ne.com` (el login documentado de la SPA) es miembro de
        base.group_system; env.user en TransactionCase también. Si el muro le hiciera bypass,
        el usuario con más poder del tenant sería el único capaz de sacar al negocio del
        NRUS."""
        self._set('nrus')
        self.assertTrue(self.env.user.has_group('base.group_system'))
        with self.assertRaises(UserError) as ctx:
            self.env['account.move']._l10n_pe_ne_check_regimen('01')
        self.assertIn('937', str(ctx.exception))

    def test_muro_sin_bypass_en_retencion_y_percepcion(self):
        """H3, el caso caro: 20/40 son account.payment — NO pasan por el motor L1 y el
        pre-flight los devuelve vacío, así que el muro es la ÚNICA barrera. Con bypass de
        admin, un NRUS emitía retención/percepción de punta a punta."""
        self._set('nrus')
        Pay = self.env['account.payment']   # como admin, a propósito
        with self.assertRaises(UserError) as ctx:
            Pay.l10n_pe_ne_quick_retencion({'proveedor': {}, 'documentos': []})
        self.assertIn('Comprobante de retención', str(ctx.exception))
        with self.assertRaises(UserError) as ctx:
            Pay.l10n_pe_ne_quick_percepcion({'cliente': {}, 'documentos': []})
        self.assertIn('Comprobante de percepción', str(ctx.exception))

    def test_muro_audita_tambien_al_admin(self):
        """H3 (efecto secundario): el rechazo del admin también deja rastro. Antes el bypass lo
        dejaba caer en el mensaje genérico del motor L1 y sin ninguna fila de bitácora."""
        self._set('nrus')
        lanzo = False
        try:
            self.env['account.move']._l10n_pe_ne_check_regimen('20')
        except UserError:
            lanzo = True
        self.assertTrue(lanzo)
        fila = self.env['l10n_pe_ne.rubro_auditoria'].sudo().search(
            [('company_id', '=', self.company.id), ('campo', '=', 'rechazo-regimen:20')])
        self.assertTrue(fila)
        self.assertEqual(fila[0].user_id, self.env.user)

    def test_muro_deja_pasar_lo_permitido(self):
        self._set('nrus')
        AM = self.env['account.move'].with_user(self._emisor())
        for tipo in TIPOS_NRUS:
            AM._l10n_pe_ne_check_regimen(tipo)   # no lanza

    def test_muro_se_dispara_en_quick_emit(self):
        """La vía real: el guard no sirve si la emisión no pasa por él."""
        self._set('nrus')
        AM = self.env['account.move'].with_user(self._emisor())
        with self.assertRaises(UserError) as ctx:
            AM.l10n_pe_ne_quick_emit(self._payload('01'), enviar=False)
        # Se comprueba el TEXTO: en estos caminos hay otros UserError posibles y un
        # assertRaises pelado pasaría en verde con el muro desconectado.
        self.assertIn('937', str(ctx.exception))

    def test_quick_emit_boleta_pasa_en_nrus(self):
        """Contracara obligatoria: el muro no puede sobre-bloquear. El NRUS boletea normal."""
        self._set('nrus')
        AM = self.env['account.move'].with_user(self._emisor())
        move = AM.l10n_pe_ne_quick_emit(self._payload('03'), enviar=False)
        self.assertTrue(move.id)

    def test_muro_se_dispara_en_liquidacion(self):
        self._set('nrus')
        AM = self.env['account.move'].with_user(self._emisor())
        with self.assertRaises(UserError) as ctx:
            AM.l10n_pe_ne_emitir_liquidacion(self._payload('04'), enviar=False)
        self.assertIn('Liquidación de compra', str(ctx.exception))

    def test_muro_se_dispara_en_retencion_y_percepcion(self):
        self._set('nrus')
        Pay = self.env['account.payment'].with_user(self._emisor())
        with self.assertRaises(UserError) as ctx:
            Pay.l10n_pe_ne_quick_retencion({'proveedor': {}, 'documentos': []})
        self.assertIn('Comprobante de retención', str(ctx.exception))
        with self.assertRaises(UserError) as ctx:
            Pay.l10n_pe_ne_quick_percepcion({'cliente': {}, 'documentos': []})
        self.assertIn('Comprobante de percepción', str(ctx.exception))

    def test_preflight_reporta_el_bloqueo(self):
        """El pre-flight de la SPA hereda el muro: avisa antes de gastar un envío."""
        self._set('nrus')
        AM = self.env['account.move'].with_user(self._emisor())
        errores = [f for f in AM.l10n_pe_ne_preflight(self._payload('01'))
                   if f['nivel'] == 'error']
        self.assertTrue(errores)
        self.assertTrue([e for e in errores if '937' in e['mensaje']])

    # ============================================================ reglas L1
    def test_regla_tipo_doc_es_error(self):
        self._set('nrus')
        move = self._move()   # out_invoice a cliente con RUC = Factura (01)
        finding = self._codes(move).get('regimen-tipo-doc')
        self.assertIsNotNone(finding)
        self.assertEqual(finding['nivel'], 'error')

    def test_regla_tipo_doc_no_dispara_sin_regimen(self):
        move = self._move()
        self.assertNotIn('regimen-tipo-doc', self._codes(move))

    def test_regla_tipo_doc_no_dispara_en_general(self):
        self._set('general')
        move = self._move()
        self.assertNotIn('regimen-tipo-doc', self._codes(move))

    def test_regla_exportacion_es_error_en_nrus(self):
        self._set('nrus')
        exp = self.Move._l10n_pe_ne_tax_by_code('9995')
        move = self._move(taxes=exp)
        codes = self._codes(move)
        self.assertIn('regimen-exportacion', codes)
        self.assertEqual(codes['regimen-exportacion']['nivel'], 'error')

    def test_regla_exportacion_no_dispara_fuera_de_nrus(self):
        self._set('rer')
        exp = self.Move._l10n_pe_ne_tax_by_code('9995')
        move = self._move(taxes=exp)
        self.assertNotIn('regimen-exportacion', self._codes(move))

    def test_regla_detraccion_es_aviso(self):
        """Aviso y NO error: la excepción del SPOT tiene su propia excepción (Sector Público)."""
        self._set('nrus')
        move = self._move(l10n_pe_ne_detraccion=True)
        codes = self._codes(move)
        self.assertIn('regimen-detraccion', codes)
        self.assertEqual(codes['regimen-detraccion']['nivel'], 'aviso')

    def test_regla_detraccion_no_dispara_fuera_de_nrus(self):
        self._set('general')
        move = self._move(l10n_pe_ne_detraccion=True)
        self.assertNotIn('regimen-detraccion', self._codes(move))

    def test_preflight_no_escribe_bitacora(self):
        """H6. El pre-flight simula la emisión y la revierte; la bitácora del muro se escribe en
        un cursor APARTE justamente para sobrevivir al rollback, así que en producción dejaría
        una fila de «intento de emitir Factura» por cada borrador abierto —y dos por cada
        emisión real—. La bitácora es para intentos de emisión REALES.

        Se espía el CREATE de la bitácora y no se cuenta la tabla: bajo `test_enable` la fila
        cae en el mismo cursor del test y el rollback del savepoint se la llevaría, escondiendo
        el bug precisamente en el único entorno donde no ocurre."""
        self._set('nrus')
        AM = self.env['account.move'].with_user(self._emisor())
        Auditoria = type(self.env['l10n_pe_ne.rubro_auditoria'])
        with patch.object(Auditoria, 'create') as spy:
            findings = AM.l10n_pe_ne_preflight(self._payload('01'))
            self.assertTrue([f for f in findings if f['nivel'] == 'error'])   # sí avisa…
            spy.assert_not_called()                                           # …y no registra
        # Contracara: el intento REAL sí queda registrado (si no, el assert de arriba pasaría
        # también con la bitácora del muro desconectada del todo).
        with patch.object(Auditoria, 'create') as spy:
            with self.assertRaises(UserError):
                AM.l10n_pe_ne_quick_emit(self._payload('01'), enviar=False)
            self.assertEqual([c.args[0]['campo'] for c in spy.call_args_list],
                             ['rechazo-regimen:01'])

    # ================================================================ series
    def test_serie_factura_bloqueada_en_nrus(self):
        self._set('nrus')
        Serie = self.env['l10n_pe_ne.serie']
        with self.assertRaises(UserError):
            Serie.l10n_pe_ne_serie_upsert({'serie': 'F900', 'tipoDoc': '01'})

    def test_serie_nota_de_credito_con_prefijo_f_permitida_en_nrus(self):
        """H2. Una NC/ND SOBRE UNA FACTURA exige serie con prefijo F (_l10n_pe_serie_prefix
        hereda la familia del documento afectado). Bloquear por la LETRA dejaba al negocio que
        pasó de RER a NRUS en enero sin poder anular sus facturas de diciembre: la NC quedaba
        inemitible, justo lo que se quiso evitar al permitir 07/08 en el NRUS."""
        self._set('nrus')
        Serie = self.env['l10n_pe_ne.serie']
        for codigo, tipo in (('FC01', '07'), ('FD01', '08')):
            rec = Serie.l10n_pe_ne_serie_upsert({'serie': codigo, 'tipoDoc': tipo})
            self.assertTrue(rec['id'], codigo)

    def test_serie_factura_preexistente_se_puede_editar_en_nrus(self):
        """H2. `l10n_pe_ne_serie_upsert` es alta Y edición. Bloquear la edición no borra la
        serie F que el negocio ya tenía: solo le impide apagarla o corregirle el local."""
        rec = self.env['l10n_pe_ne.serie'].l10n_pe_ne_serie_upsert(
            {'serie': 'F902', 'tipoDoc': '01'})       # dada de alta cuando aún facturaba
        self._set('nrus')                             # …y en enero baja al NRUS
        editada = self.env['l10n_pe_ne.serie'].l10n_pe_ne_serie_upsert(
            {'id': rec['id'], 'serie': 'F902', 'tipoDoc': '01', 'activa': False})
        self.assertFalse(editada['activa'])

    def test_serie_mensaje_de_bloqueo_es_accionable(self):
        """El mensaje no puede aconsejar algo imposible: tiene que decir la salida real."""
        self._set('nrus')
        with self.assertRaises(UserError) as ctx:
            self.env['l10n_pe_ne.serie'].l10n_pe_ne_serie_upsert(
                {'serie': 'F903', 'tipoDoc': '01'})
        self.assertIn('NOTA DE CRÉDITO', str(ctx.exception))

    def test_serie_boleta_permitida_en_nrus(self):
        self._set('nrus')
        rec = self.env['l10n_pe_ne.serie'].l10n_pe_ne_serie_upsert(
            {'serie': 'B900', 'tipoDoc': '03'})
        self.assertTrue(rec['id'])

    def test_serie_factura_permitida_sin_regimen(self):
        rec = self.env['l10n_pe_ne.serie'].l10n_pe_ne_serie_upsert(
            {'serie': 'F901', 'tipoDoc': '01'})
        self.assertTrue(rec['id'])

    # =================================================================== API
    def test_get_regimen_devuelve_catalogo_y_tipos_resueltos(self):
        self._set('nrus', '2026-01-01', '2')
        cfg = self.Move.l10n_pe_ne_regimen_config()
        self.assertEqual(cfg['regimen'], 'nrus')
        self.assertEqual(cfg['fechaInicio'], '2026-01-01')
        self.assertEqual(cfg['nrusCategoria'], '2')
        self.assertEqual(cfg['tiposPermitidos'], sorted(TIPOS_NRUS))
        self.assertEqual({c['codigo'] for c in cfg['catalogo']}, set(REGIMENES))
        self.assertTrue(cfg['puedeEditar'])

    def test_get_regimen_legacy_devuelve_tipos_none(self):
        cfg = self.Move.l10n_pe_ne_regimen_config()
        self.assertIsNone(cfg['regimen'])
        self.assertIsNone(cfg['tiposPermitidos'])   # None ≠ lista vacía: es «sin gating»

    def test_set_regimen_guarda_y_audita(self):
        self.Move.l10n_pe_ne_set_regimen(
            {'regimen': 'nrus', 'fechaInicio': '2026-01-01', 'nrusCategoria': '1'})
        self.assertEqual(self.company.l10n_pe_ne_regimen, 'nrus')
        self.assertEqual(self.company.l10n_pe_ne_nrus_categoria, '1')
        fila = self.env['l10n_pe_ne.rubro_auditoria'].sudo().search(
            [('company_id', '=', self.company.id), ('campo', '=', 'regimen')], limit=1)
        self.assertTrue(fila)
        self.assertEqual(json.loads(fila.despues)['regimen'], 'nrus')
        # El historial lo renderiza legible (mismo listado que el rubro).
        historial = self.Move.l10n_pe_ne_auditoria_list()
        self.assertTrue([h for h in historial if h['titulo'] == 'Cambio de régimen tributario'])

    def test_set_regimen_vacio_vuelve_a_legacy(self):
        self._set('nrus')
        self.Move.l10n_pe_ne_set_regimen({'regimen': ''})
        self.assertFalse(self.company.l10n_pe_ne_regimen)
        self.assertIsNone(self.company.l10n_pe_ne_tipos_permitidos())

    def test_set_regimen_limpia_la_categoria_fuera_de_nrus(self):
        self.Move.l10n_pe_ne_set_regimen({'regimen': 'nrus', 'nrusCategoria': '1'})
        self.Move.l10n_pe_ne_set_regimen({'regimen': 'rer', 'nrusCategoria': '1'})
        self.assertFalse(self.company.l10n_pe_ne_nrus_categoria)

    def test_set_regimen_rechaza_entrada_basura(self):
        """Entrada inválida = error legible, nunca un traceback."""
        for payload in ({'regimen': 'marciano'},
                        {'regimen': 123},
                        {'regimen': 'nrus', 'fechaInicio': 'ayer'},
                        {'regimen': 'nrus', 'nrusCategoria': '9'},
                        {'regimen': 'nrus', 'nrusCategoria': 7}):
            with self.assertRaises(UserError, msg=repr(payload)):
                self.Move.l10n_pe_ne_set_regimen(payload)
        with self.assertRaises(UserError):
            self.Move.l10n_pe_ne_set_regimen(['nrus'])
        # …y nada de eso dejó el campo a medio escribir.
        self.assertFalse(self.company.l10n_pe_ne_regimen)

    def test_set_regimen_exige_permiso(self):
        AM = self.env['account.move'].with_user(self._emisor())
        with self.assertRaises(AccessError):
            AM.l10n_pe_ne_set_regimen({'regimen': 'nrus'})

    def test_config_expone_regimen_y_tipos(self):
        self._set('nrus')
        cfg = self.Move.l10n_pe_ne_config()
        self.assertEqual(cfg['regimen'], 'nrus')
        self.assertEqual(cfg['tiposPermitidos'], sorted(TIPOS_NRUS))

    def test_config_legacy_no_expone_las_claves(self):
        cfg = self.Move.l10n_pe_ne_config()
        self.assertNotIn('regimen', cfg)
        self.assertNotIn('tiposPermitidos', cfg)   # ausente = la SPA no oculta nada

    def test_perfil_expone_regimen(self):
        perfil = self.env.user.l10n_pe_ne_perfil()
        self.assertIsNone(perfil['regimen'])
        self.assertFalse(perfil['regimenConfigurado'])
        self._set('rmt')
        perfil = self.env.user.l10n_pe_ne_perfil()
        self.assertEqual(perfil['regimen'], 'rmt')
        self.assertTrue(perfil['regimenConfigurado'])

    # ==================================================== fecha de vigencia (H4)
    def test_fecha_de_vigencia_no_gatea_y_el_help_no_lo_promete(self):
        """H4. El gating es INMEDIATO: la fecha no se mira (F4 queda fuera de alcance). Lo que
        no puede pasar es que el `help` prometa lo contrario — un campo que promete lo que no
        cumple es peor que no tenerlo, porque el dueño confía en él para no perder el régimen."""
        futuro = '2099-01-01'
        self._set('nrus', futuro)
        # Régimen «vigente desde 2099» y aun así gatea hoy: ese ES el comportamiento actual.
        self.assertFalse(self.company.l10n_pe_ne_tipo_permitido('01'))
        ayuda = self.company._fields['l10n_pe_ne_regimen_fecha'].help
        self.assertNotIn('se juzgó', ayuda)   # la promesa que no se cumple
        self.assertIn('informativa', ayuda)

    # ===================================================== emisión masiva (H5)
    def test_lote_valida_el_regimen_en_la_validacion_previa(self):
        """H5. El corte por fila (en _l10n_pe_ne_procesar_fila) llega tarde: el lote queda
        'validado' y las 200 filas caen a 'error' una por una. La validación previa existe para
        decirlo ANTES, con el Excel todavía corregible."""
        self._set('nrus')
        rep = self._lote([
            ["", "FACTURA", "", "", "RUC", "20100070970", "OK SAC", "", "ITEM", 1, 10.0, 0,
             "GRAVADO", "NO", "PEN"],
        ])
        self.assertEqual(rep['estado'], 'con_errores')
        self.assertTrue([e for e in rep['errores'] if 'régimen' in e['mensaje']])
        self.assertTrue([e for e in rep['errores'] if 'BOLETA' in e['mensaje']],
                        "el error debe decir cómo salir del paso")
        self.assertFalse(self.env['l10n_pe_ne.lote'].browse(rep['id']).fila_ids,
                         "un lote con errores no crea filas procesables")

    def test_lote_boleta_pasa_en_nrus(self):
        """Contracara: la masiva no puede sobre-bloquear. El NRUS sube boletas con normalidad."""
        self._set('nrus')
        rep = self._lote([
            ["", "BOLETA", "", "", "", "", "", "", "ITEM", 1, 10.0, 0, "GRAVADO", "NO", "PEN"],
        ])
        self.assertEqual(rep['estado'], 'validado')

    def test_lote_factura_pasa_sin_regimen(self):
        rep = self._lote([
            ["", "FACTURA", "", "", "RUC", "20100070970", "OK SAC", "", "ITEM", 1, 10.0, 0,
             "GRAVADO", "NO", "PEN"],
        ])
        self.assertEqual(rep['estado'], 'validado')

    # =========================================================== provisioning
    def test_provisioning_acepta_regimen(self):
        self.env['res.company'].l10n_pe_ne_provision_tenant({
            'ruc': self.company.vat, 'razonSocial': self.company.name,
            'login': 'tenant.regimen@test', 'password': 'Prov12345678',
            'regimen': 'nrus', 'nrusCategoria': '2'})
        self.assertEqual(self.company.l10n_pe_ne_regimen, 'nrus')
        self.assertEqual(self.company.l10n_pe_ne_nrus_categoria, '2')

    def test_provisioning_rechaza_regimen_desconocido(self):
        with self.assertRaises(UserError):
            self.env['res.company'].l10n_pe_ne_provision_tenant({
                'ruc': self.company.vat, 'razonSocial': self.company.name,
                'login': 'tenant.regimen2@test', 'password': 'Prov12345678',
                'regimen': 'marciano'})

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin, L10nPeSeedMixin

_TARGET = 'odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post'


@tagged('post_install', '-at_install')
class TestVentaEstado(L10nPeSeedMixin, EnvioSincronoMixin, TransactionCase):
    """Contratación pública (proveedor del Estado): profundización a maduro.

    P2 · Penalidad del contrato (reduce el neto a cobrar, no el total ni el IGV).
    P3 · Control de saldo del contrato (reusa el modelo proyecto: tope QA-039 también en Estado).
    P4 · Conformidad / acta de recepción (dato de registro + aviso pre-emisión).
    P5 · Detracción del rubro (el motor SPOT convive con los 4 datos del Estado).
    """

    def setUp(self):
        super().setUp()  # L10nPeSeedMixin: RUC + IGV (self.igv)
        self.Move = self.env['account.move']
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'MUNICIPALIDAD X', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create({
            'name': 'SERVICIO AL ESTADO', 'default_code': 'E1'})

    _ESTADO = {'expediente': 'EXP-2026-001', 'unidadEjecutora': '001',
               'procesoSeleccion': 'LP-2026-007', 'contrato': 'CTO-2026-042'}

    def _payload(self, monto, **extra):
        p = {
            'tipoDoc': '01', 'moneda': 'PEN', 'serie': 'F001',
            'cliente': {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'MUNICIPALIDAD X'},
            'lineas': [{'descripcion': 'Servicio', 'productId': self.product.id,
                        'cantidad': 1, 'precioUnitario': monto, 'taxCode': '1000'}],
        }
        p.update(extra)
        return p

    def _emitir(self, payload):
        ok = type('R', (), {'status_code': 200, 'text': '<?xml version="1.0"?><Invoice/>',
                            'headers': {}})()
        with patch(_TARGET, return_value=ok):
            res = self.Move.l10n_pe_ne_quick_emit(payload)
        return self.Move.browse(res['id'])

    # ---- P2 · Penalidad -------------------------------------------------------------------
    def test_penalidad_reduce_el_neto_no_el_total(self):
        m = self._emitir(self._payload(1000, penalidad=118.0))  # base 1000 → total 1180
        cobrar = m._l10n_pe_importe_cobrar()  # 1180 c/IGV
        self.assertAlmostEqual(m.amount_total, 1180.0, delta=0.02)  # el total NO baja
        self.assertAlmostEqual(m._l10n_pe_neto_pendiente(), round(cobrar - 118.0, 2), delta=0.01)
        self.assertLessEqual(m._l10n_pe_neto_pendiente(), cobrar + 0.005)  # invariante 3265

    def test_penalidad_expuesta_en_el_detalle(self):
        m = self._emitir(self._payload(1000, penalidad=118.0))
        self.assertEqual(m.l10n_pe_ne_comprobante_detalle()['penalidad'], 118.0)

    def test_sin_penalidad_no_expone_nada(self):
        m = self._emitir(self._payload(1000))
        self.assertIsNone(m.l10n_pe_ne_comprobante_detalle()['penalidad'])

    def test_deducciones_que_exceden_el_cobro_bloquean(self):
        # Penalidad mayor al importe a cobrar → neto negativo → error 'deducciones-exceden'.
        m = self.Move.create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-07-29',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 100.0, 'tax_ids': [(6, 0, self.igv.ids)]})],
            'l10n_pe_ne_penalidad': 999.0,
        })
        m.action_post()
        codes = {f['code'] for f in m._l10n_pe_ne_validaciones()}
        self.assertIn('deducciones-exceden', codes)

    # ---- P3 · Control de saldo del contrato (reuso del proyecto) --------------------------
    def test_factura_al_estado_respeta_el_tope_del_contrato(self):
        proyecto = self.env['l10n_pe_ne.proyecto'].create({
            'name': 'SUMINISTRO 2026', 'valor_total': 5000.0})
        m1 = self._emitir(self._payload(3000, proyectoId=proyecto.id, ventaEstado=dict(self._ESTADO)))
        proyecto.invalidate_recordset()
        self.assertAlmostEqual(proyecto.facturado, m1.amount_total, delta=0.01)
        self.assertLess(proyecto.saldo, 5000.0)
        # Un segundo comprobante que pasa el valor total del contrato se rechaza (QA-039).
        with self.assertRaises(UserError):
            self._emitir(self._payload(4000, proyectoId=proyecto.id, ventaEstado=dict(self._ESTADO)))

    # ---- P4 · Conformidad / acta ---------------------------------------------------------
    def test_conformidad_se_guarda_y_se_expone(self):
        ve = dict(self._ESTADO, conformidad='ACTA-RECEP-88')
        m = self._emitir(self._payload(1000, ventaEstado=ve))
        self.assertEqual(m.l10n_pe_ne_conformidad, 'ACTA-RECEP-88')
        self.assertEqual(m.l10n_pe_ne_comprobante_detalle()['conformidad'], 'ACTA-RECEP-88')

    def test_estado_sin_conformidad_avisa(self):
        m = self._emitir(self._payload(1000, ventaEstado=dict(self._ESTADO)))
        codes = {f['code'] for f in m._l10n_pe_ne_validaciones()}
        self.assertIn('estado-conformidad', codes)
        # Es aviso, no bloqueo: no hay error de este código.
        errores = [f for f in m._l10n_pe_ne_validaciones()
                   if f['nivel'] == 'error' and f['code'] == 'estado-conformidad']
        self.assertFalse(errores)

    def test_estado_con_conformidad_no_avisa(self):
        ve = dict(self._ESTADO, conformidad='ACTA-1')
        m = self._emitir(self._payload(1000, ventaEstado=ve))
        self.assertNotIn('estado-conformidad', {f['code'] for f in m._l10n_pe_ne_validaciones()})

    # ---- P5 · Detracción del rubro (SPOT convive con los datos del Estado) ----------------
    def test_detraccion_del_rubro_convive_con_los_datos_del_estado(self):
        m = self._emitir(self._payload(
            2000, ventaEstado=dict(self._ESTADO),
            detraccion={'codBien': '037', 'tasa': 12, 'cuentaBN': '00-000-123456'}))
        # Los 4 datos del Estado se guardaron y la detracción SPOT se aplicó a la vez.
        self.assertEqual(m.l10n_pe_ne_estado_contrato, 'CTO-2026-042')
        self.assertTrue(m.l10n_pe_ne_detraccion)
        self.assertEqual(m.l10n_pe_ne_detraccion_code, '037')
        self.assertGreater(m._l10n_pe_detraccion_monto(), 0.0)
        # La tasa 12% coincide con la oficial del 037 → sin aviso de tasa; sin errores de detracción.
        codes = {f['code'] for f in m._l10n_pe_ne_validaciones()}
        self.assertNotIn('detraccion-tasa', codes)
        self.assertNotIn('detraccion-monto', codes)
        self.assertNotIn('detraccion-cuenta', codes)

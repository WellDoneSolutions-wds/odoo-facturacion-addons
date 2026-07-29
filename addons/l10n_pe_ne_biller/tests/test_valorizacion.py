from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin, L10nPeSeedMixin

_TARGET = 'odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post'


@tagged('post_install', '-at_install')
class TestValorizacion(L10nPeSeedMixin, EnvioSincronoMixin, TransactionCase):
    """C2 · Emitir la factura DESDE la valorización (avance de obra).

    Cada emisión contra un contrato es una valorización numerada; el comprobante declara el
    avance acumulado y el proyecto lleva el saldo. El tope del 100% (QA-039) sigue vigente.
    """

    def setUp(self):
        super().setUp()  # L10nPeSeedMixin: RUC + IGV (self.igv)
        self.Move = self.env['account.move']
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'MUNICIPALIDAD X', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create({'name': 'AVANCE OBRA', 'default_code': 'OB1'})
        self.proyecto = self.env['l10n_pe_ne.proyecto'].create({
            'name': 'CARRETERA KM 10', 'valor_total': 100000.0})

    def _emitir(self, monto):
        ok = type('R', (), {'status_code': 200, 'text': '<?xml version="1.0"?><Invoice/>',
                            'headers': {}})()
        with patch(_TARGET, return_value=ok):
            res = self.Move.l10n_pe_ne_quick_emit({
                'tipoDoc': '01', 'moneda': 'PEN', 'serie': 'F001',
                'cliente': {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'MUNICIPALIDAD X'},
                'lineas': [{'descripcion': 'Avance de obra', 'productId': self.product.id,
                            'cantidad': 1, 'precioUnitario': monto, 'taxCode': '1000'}],
                'proyectoId': self.proyecto.id,
            })
        return self.Move.browse(res['id'])

    def test_valorizaciones_se_numeran_y_acumulan(self):
        m1 = self._emitir(4000)
        self.assertEqual(m1.l10n_pe_ne_valorizacion_nro, 1)
        self.assertIn('Valorización N° 1', m1.narration or '', 'la glosa declara la valorización')
        m2 = self._emitir(3000)
        self.assertEqual(m2.l10n_pe_ne_valorizacion_nro, 2)
        self.proyecto.invalidate_recordset()
        self.assertAlmostEqual(self.proyecto.facturado, m1.amount_total + m2.amount_total, delta=0.01)
        self.assertEqual(self.proyecto.valorizaciones, 2)
        self.assertGreater(self.proyecto.avance, 0.0)

    def test_detalle_expone_la_valorizacion(self):
        m1 = self._emitir(4000)
        det = m1.l10n_pe_ne_comprobante_detalle()
        self.assertEqual(det['valorizacionNro'], 1)
        self.assertTrue(det['proyecto'], 'el detalle trae el estado del contrato')
        self.assertEqual(det['proyecto']['name'], 'CARRETERA KM 10')
        self.assertGreater(det['proyecto']['avance'], 0.0)

    def test_no_supera_el_valor_del_contrato(self):
        # Una valorización que pasa el 100% del contrato se rechaza (QA-039).
        with self.assertRaises(UserError):
            self._emitir(200000)  # 200000 + IGV >> 100000

    def test_respeta_la_observacion_del_emisor(self):
        # Si el emisor puso su propia observación, no se pisa con la glosa automática.
        ok = type('R', (), {'status_code': 200, 'text': '<Invoice/>', 'headers': {}})()
        with patch(_TARGET, return_value=ok):
            res = self.Move.l10n_pe_ne_quick_emit({
                'tipoDoc': '01', 'moneda': 'PEN', 'serie': 'F001',
                'cliente': {'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'MUNICIPALIDAD X'},
                'lineas': [{'descripcion': 'Avance', 'productId': self.product.id, 'cantidad': 1,
                            'precioUnitario': 4000, 'taxCode': '1000'}],
                'proyectoId': self.proyecto.id, 'observacion': 'CONFORMIDAD ACTA 12',
            })
        m = self.Move.browse(res['id'])
        self.assertIn('CONFORMIDAD ACTA 12', m.narration or '')
        self.assertNotIn('avance acumulado', m.narration or '')
        self.assertEqual(m.l10n_pe_ne_valorizacion_nro, 1)  # se numera igual

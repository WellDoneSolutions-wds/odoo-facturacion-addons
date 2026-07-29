from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged('post_install', '-at_install')
class TestModeloDinero(L10nPeSeedMixin, TransactionCase):
    """L3 · Invariantes del modelo de dinero.

    Fija por test las relaciones entre las cuatro magnitudes (ver «MODELO DE DINERO» en
    account_move_biller.py). Confundirlas fue la raíz del rechazo 3265 (el neto pendiente
    incluía los bienes gratuitos). Estas invariantes valen para CUALQUIER combinación de
    gratuitos, detracción, crédito, inicial al contado, etc.:

        neto_pendiente ≤ importe_cobrar ≤ amount_total
        importe_cobrar ≥ 0 ;  detracción ≥ 0
        sum(cuotas del crédito) == neto pendiente del crédito
    """

    def setUp(self):
        super().setUp()  # L10nPeSeedMixin: RUC + IGV (self.igv)
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create({'name': 'ITEM', 'default_code': 'I1'})

    def _build(self, gravado=None, gratuito=None, detraccion=False, credito_cuotas=None,
               inicial=0.0, retencion=0.0):
        lines = []
        if gravado:
            lines.append((0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                  'price_unit': gravado, 'tax_ids': [(6, 0, self.igv.ids)]}))
        if gratuito:
            g = self.env['account.move']._l10n_pe_ne_tax_by_code('9996')
            lines.append((0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                  'price_unit': gratuito, 'name': 'REGALO', 'tax_ids': [(6, 0, g.ids)]}))
        vals = {'move_type': 'out_invoice', 'partner_id': self.partner.id,
                'invoice_date': '2026-07-29', 'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
                'invoice_line_ids': lines}
        if detraccion:
            vals.update({'l10n_pe_ne_detraccion': True, 'l10n_pe_ne_detraccion_code': '037',
                         'l10n_pe_ne_detraccion_rate': 12.0,
                         'l10n_pe_ne_detraccion_cuenta': '00-123-456789'})
        if credito_cuotas is not None:
            vals.update({'l10n_pe_ne_forma_pago': 'Credito', 'l10n_pe_ne_cuotas': credito_cuotas})
        if inicial:
            vals['l10n_pe_ne_inicial_contado'] = inicial
        if retencion:
            vals['l10n_pe_ne_retencion_garantia_rate'] = retencion
        move = self.env['account.move'].create(vals)
        move.action_post()
        return move

    def _assert_invariantes(self, m, nombre):
        total = m.amount_total or 0.0
        cobrar = m._l10n_pe_importe_cobrar()
        neto = m._l10n_pe_neto_pendiente()
        det = m._l10n_pe_detraccion_monto() if m.l10n_pe_ne_detraccion else 0.0
        self.assertGreaterEqual(cobrar, -0.005, '%s: importe a cobrar negativo (%.2f)' % (nombre, cobrar))
        self.assertLessEqual(cobrar, total + 0.005, '%s: cobrar %.2f > total %.2f' % (nombre, cobrar, total))
        self.assertGreaterEqual(det, 0.0, '%s: detracción negativa' % nombre)
        self.assertLessEqual(neto, cobrar + 0.005,
                             '%s: neto pendiente %.2f > importe a cobrar %.2f (invariante 3265)' % (nombre, neto, cobrar))
        if m.l10n_pe_ne_forma_pago == 'Credito':
            pend = m._l10n_pe_credito_pendiente()   # el valor que va al XML (mtoNetoPendientePago)
            self.assertLessEqual(pend, cobrar + 0.005,
                                 '%s: crédito pendiente %.2f > importe a cobrar %.2f' % (nombre, pend, cobrar))
            suma = sum(c['monto'] for c in m._l10n_pe_cuotas_netas())
            self.assertAlmostEqual(suma, pend, delta=0.02,
                                   msg='%s: sum(cuotas) %.2f != neto pendiente %.2f' % (nombre, suma, pend))

    def test_invariantes_del_modelo_de_dinero(self):
        casos = [
            ('contado simple', dict(gravado=500)),
            ('contado + gratuito', dict(gravado=500, gratuito=200)),
            ('crédito', dict(gravado=500, credito_cuotas=[{'fecha': '2026-12-31', 'monto': 590}])),
            ('crédito + gratuito (F001-247)', dict(gravado=500, gratuito=200,
                                                   credito_cuotas=[{'fecha': '2026-12-31', 'monto': 590}])),
            ('contado + detracción', dict(gravado=1000, detraccion=True)),
            ('crédito + detracción + inicial', dict(gravado=1000, detraccion=True, inicial=200,
                                                    credito_cuotas=[{'fecha': '2026-12-31', 'monto': 500}])),
            ('crédito + inicial (mixto)', dict(gravado=1000, inicial=280,
                                               credito_cuotas=[{'fecha': '2026-12-31', 'monto': 900}])),
            ('crédito con cuotas descuadradas (se escalan al neto)',
             dict(gravado=500, credito_cuotas=[{'fecha': '2026-12-31', 'monto': 9999}])),
            ('obra con retención de garantía 10%',
             dict(gravado=10000, retencion=10.0,
                  credito_cuotas=[{'fecha': '2026-12-31', 'monto': 10000}])),
            ('obra con detracción + retención de garantía',
             dict(gravado=10000, detraccion=True, retencion=10.0,
                  credito_cuotas=[{'fecha': '2026-12-31', 'monto': 8000}])),
        ]
        for nombre, kw in casos:
            with self.subTest(caso=nombre):
                self._assert_invariantes(self._build(**kw), nombre)

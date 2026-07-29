from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin

_TARGET = 'odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post'


@tagged('post_install', '-at_install')
class TestIcbperCantidad(EnvioSincronoMixin, TransactionCase):
    """ICBPER (bolsa plástica) con cantidad NO entera.

    SUNAT cuenta la bolsa como unidad DISCRETA: `ctdBolsasTriIcbperItem` es un entero y no
    existe media bolsa. El bug: si la línea llegaba con cantidad decimal, Odoo computaba la
    tax fija del ICBPER sobre la cantidad real (2.6 × 0.50 = 1.30) dentro de price_total, pero
    el XML reportaba el ICBPER sobre el entero (3 × 0.50 = 1.50). El reparto IGV/ICBPER del
    ítem quedaba descuadrado y `mtoBaseIgvItem × 18% != mtoIgvItem`. La normalización al entero
    DESDE quick_emit deja base, IGV, ICBPER y ctdBolsas consistentes.
    """

    def setUp(self):
        super().setUp()
        self.Move = self.env['account.move']
        # El IGV (1000) lo aporta el plan contable l10n_pe; en una BD sembrada ya existe.
        # Lo aseguramos para que el test sea hermético (no depende del plan cargado).
        Tax = self.env['account.tax'].sudo()
        if not Tax.search([('company_id', '=', self.env.company.id),
                           ('type_tax_use', '=', 'sale'),
                           ('l10n_pe_edi_tax_code', '=', '1000')], limit=1):
            Tax.create({'name': 'IGV 18% (test)', 'amount_type': 'percent', 'amount': 18.0,
                        'type_tax_use': 'sale', 'l10n_pe_edi_tax_code': '1000',
                        'company_id': self.env.company.id})

    def _emitir(self, lineas):
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

    def test_bolsa_con_cantidad_decimal_se_normaliza_al_entero(self):
        # 2.6 bolsas → 3. precioUnitario en el payload es SIN IGV (valor base por bolsa).
        move = self._emitir([{'descripcion': 'BOLSA PLASTICA', 'cantidad': 2.6,
                              'precioUnitario': 100.0, 'taxCode': '1000', 'icbper': True}])
        # La línea entró a Odoo con cantidad entera: la base la computa Odoo sobre 3, no sobre 2.6.
        self.assertEqual(move.invoice_line_ids[0].quantity, 3.0)

        d = move._l10n_pe_detalle()[0]
        self.assertEqual(d['ctdBolsasTriIcbperItem'], '3')          # nº de bolsas entero
        self.assertEqual(d['mtoTriIcbperUnidad'], '0.50')
        self.assertEqual(d['mtoTriIcbperItem'], '1.50')             # 3 × 0.50
        self.assertEqual(d['mtoValorVentaItem'], '300.00')         # 3 bolsas × 100 (base)
        self.assertEqual(d['mtoBaseIgvItem'], '300.00')
        self.assertEqual(d['mtoIgvItem'], '54.00')                 # 300 × 18%, SIN el ICBPER
        # El total de tributos del ítem = IGV + ICBPER, y el IGV cuadra contra la base × 18%.
        self.assertEqual(d['sumTotTributosItem'], '55.50')         # 54.00 + 1.50
        self.assertEqual(
            float(d['mtoBaseIgvItem']) * 0.18, float(d['mtoIgvItem']),
            'el IGV del ítem debe cuadrar contra mtoBaseIgvItem × 18% (split IGV/ICBPER)')

        # Cabecera: el ICBPER suma al total de tributos y aparece como su propio TaxSubtotal 7152.
        cab = move._l10n_pe_build_invoice_request()
        self.assertEqual(cab['cabecera']['sumTotTributos'], '55.50')
        self.assertEqual({t['ideTributo'] for t in cab['tributos']}, {'1000', '7152'})
        self.assertEqual(
            [t['mtoTributo'] for t in cab['tributos'] if t['ideTributo'] == '7152'][0], '1.50')

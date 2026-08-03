from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import EnvioSincronoMixin, L10nPeSeedMixin


@tagged('post_install', '-at_install')
class TestLiquidacion(L10nPeSeedMixin, EnvioSincronoMixin, TransactionCase):
    """Liquidación de compra (tipo 04) — vertical agropecuario.

    La emite el COMPRADOR (con RUC) a un productor SIN RUC (con DNI). Es una COMPRA: se modela
    como in_invoice, la mercadería ENTRA al stock, pero además se emite a SUNAT como
    SelfBilledInvoice (el facturador intercambia los roles). Este test fija ese contrato.
    """

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        # Un diario de compras (la liquidación es una compra).
        self.pjournal = self.env['account.journal'].search(
            [('type', '=', 'purchase'), ('company_id', '=', self.company.id)], limit=1)
        if not self.pjournal:
            self.pjournal = self.env['account.journal'].create({
                'name': 'Compras', 'type': 'purchase', 'code': 'COMP',
                'company_id': self.company.id})
        self.wh = self.env['stock.warehouse'].search(
            [('company_id', '=', self.company.id)], limit=1)

    def _payload(self, **over):
        p = {
            'tipoDoc': '04', 'moneda': 'PEN', 'serie': 'E001',
            'proveedor': {'tipoDoc': '1', 'numDoc': '44556677', 'razonSocial': 'JUAN PRODUCTOR'},
            'lineas': [{'descripcion': 'PAPA', 'cantidad': 120.5, 'precioUnitario': 1.20,
                        'taxCode': '9997', 'unidad': 'KGM'}],
        }
        p.update(over)
        return p

    def test_liquidacion_es_compra_tipo_04(self):
        """El move nace como compra (in_invoice), marcado liquidación, y su tipo SUNAT es 04 con
        serie de prefijo E."""
        move = self.env['account.move'].l10n_pe_ne_emitir_liquidacion(self._payload(), enviar=False)
        self.assertEqual(move.move_type, 'in_invoice')
        self.assertTrue(move.l10n_pe_ne_liquidacion)
        self.assertEqual(move._l10n_pe_document_type(), '04')
        self.assertEqual(move._l10n_pe_serie_prefix(), 'E')

    def test_build_emite_04_con_productor_como_proveedor(self):
        """El request al facturador lleva documentType 04 y el productor (DNI) en los campos del
        'usuario' que la plantilla de liquidación coloca como Supplier (vendedor sin RUC)."""
        move = self.env['account.move'].l10n_pe_ne_emitir_liquidacion(self._payload(), enviar=False)
        req = move._l10n_pe_build_invoice_request()
        self.assertEqual(req['id']['documentType'], '04')
        cab = req['cabecera']
        self.assertEqual(cab['tipDocUsuario'], '1')          # DNI
        self.assertEqual(cab['numDocUsuario'], '44556677')
        det = req['detalle'][0]
        self.assertEqual(det['codUnidadMedida'], 'KGM')
        self.assertEqual(det['tipAfeIGV'], '20')             # exonerado

    def test_liquidacion_entra_stock(self):
        """La mercadería comprada ENTRA al inventario (kardex de compra), no sale."""
        move = self.env['account.move'].l10n_pe_ne_emitir_liquidacion(self._payload(), enviar=False)
        prod = move.invoice_line_ids.product_id
        prod.invalidate_recordset()
        self.assertAlmostEqual(prod.qty_available, 120.5, places=3)

    def test_rechaza_proveedor_con_ruc(self):
        """SUNAT no admite liquidación a un vendedor con RUC: se corta antes de emitir."""
        payload = self._payload(proveedor={
            'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'AGRO SAC'})
        with self.assertRaises(UserError):
            self.env['account.move'].l10n_pe_ne_emitir_liquidacion(payload, enviar=False)

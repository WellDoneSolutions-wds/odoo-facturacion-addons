from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestCotizacionMoneda(TransactionCase):
    """La proforma toma la moneda del payload (PEN/USD), con PEN (soles) por defecto y SIN
    heredar la moneda de la compañía. Con compañías configuradas en USD, heredarla dejaba
    toda cotización en dólares y la lista mostraba '$' donde correspondía 'S/'."""

    def setUp(self):
        super().setUp()
        self.Cot = self.env["l10n_pe_ne.cotizacion"]
        self.pen = self.env.ref("base.PEN")
        self.usd = self.env.ref("base.USD")
        self.partner = self.env["res.partner"].create(
            {"name": "CLIENTE COTIZA SAC", "vat": "20100070970"})
        # Producto solo para tener catálogo; la línea va con descripción libre.
        self.env["product.product"].create({"name": "Servicio Cotizado", "list_price": 100.0})

    def _payload(self, **over):
        p = {
            "clienteId": self.partner.id,
            "items": [{"descripcion": "Servicio Cotizado", "cantidad": 1,
                       "precio": 118.0, "afectoIgv": True}],
        }
        p.update(over)
        return p

    def _crear(self, cot_model, **over):
        return cot_model.browse(
            cot_model.l10n_pe_ne_quick_cotizar(self._payload(**over))["id"])

    def test_default_pen_aunque_compania_sea_usd(self):
        """Sin 'moneda', la cotización es PEN incluso bajo una compañía en dólares
        (regresión del bug: antes heredaba currency_id de la compañía -> USD)."""
        co = self.env["res.company"].with_context(
            l10n_pe_ne_allow_company_create=True).create(
            {"name": "EXPORT USD SAC", "currency_id": self.usd.id})
        cot = self._crear(self.Cot.with_company(co))
        self.assertEqual(cot.currency_id, self.pen)

    def test_usd_explicita(self):
        cot = self._crear(self.Cot, moneda="USD")
        self.assertEqual(cot.currency_id, self.usd)

    def test_pen_explicita(self):
        cot = self._crear(self.Cot, moneda="PEN")
        self.assertEqual(cot.currency_id, self.pen)

    def test_moneda_no_soportada_cae_a_pen(self):
        """Solo PEN/USD; cualquier otra (o basura) cae a soles, no rompe."""
        cot = self._crear(self.Cot, moneda="EUR")
        self.assertEqual(cot.currency_id, self.pen)

    def test_update_cambia_moneda(self):
        cot = self._crear(self.Cot, moneda="PEN")
        self.Cot.l10n_pe_ne_update_cotizacion({"id": cot.id, "moneda": "USD"})
        self.assertEqual(cot.currency_id, self.usd)

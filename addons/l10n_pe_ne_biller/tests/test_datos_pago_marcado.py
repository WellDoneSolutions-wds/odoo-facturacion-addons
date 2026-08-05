from markupsafe import Markup

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDatosPagoMarcado(TransactionCase):
    """res.company._l10n_pe_ne_datos_pago_marcado: resalta el nombre del banco (hasta el primer
    dígito de cada línea) en <b>, mismo formato para cotización (QWeb) y comprobante (Jasper)."""

    def _marcar(self, texto):
        self.env.company.sudo().l10n_pe_ne_datos_pago = texto
        return self.env.company._l10n_pe_ne_datos_pago_marcado()

    def test_negrita_hasta_primer_digito(self):
        r = self._marcar('BCP Soles 191-1234567-0-01 · CCI 002-191-001234567001-52')
        self.assertEqual(
            r, Markup('<b>BCP Soles</b> 191-1234567-0-01 · CCI 002-191-001234567001-52'))

    def test_yape_con_dos_puntos(self):
        r = self._marcar('Yape / Plin: 987 654 321')
        self.assertEqual(r, Markup('<b>Yape / Plin:</b> 987 654 321'))

    def test_varias_lineas(self):
        r = self._marcar('BCP Soles 191-1234567-0-01\nBBVA Soles 0011-0298')
        self.assertEqual(r, Markup('<b>BCP Soles</b> 191-1234567-0-01\n<b>BBVA Soles</b> 0011-0298'))

    def test_vacio_devuelve_markup_vacio(self):
        self.assertEqual(self._marcar(''), Markup(''))
        self.assertEqual(self._marcar('   \n  '), Markup(''))

    def test_escapa_html_del_usuario(self):
        # Un < en el texto del usuario NO debe romper el markup: se escapa a &lt;.
        r = self._marcar('Banco <X> 12-34')
        self.assertEqual(r, Markup('<b>Banco &lt;X&gt;</b> 12-34'))

    def test_linea_sin_digitos_no_lleva_negrita(self):
        r = self._marcar('Consultar por WhatsApp')
        self.assertEqual(r, Markup('Consultar por WhatsApp'))

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestDistritos(TransactionCase):
    """El selector de ubigeo llena el código automáticamente: se busca por distrito,
    código, provincia o departamento y devuelve el ubigeo de 6 dígitos."""

    def _buscar(self, q):
        return self.env["account.move"].l10n_pe_ne_buscar_distrito(q=q)

    def test_busca_por_nombre_de_distrito(self):
        res = self._buscar("Miraflores")
        self.assertTrue(any(r["code"] == "150122" for r in res),
                        "debe encontrar Miraflores de Lima (150122)")
        r = next(r for r in res if r["code"] == "150122")
        self.assertEqual(r["provincia"], "Lima")
        self.assertEqual(r["departamento"], "Lima")

    def test_busca_por_codigo(self):
        res = self._buscar("150122")
        self.assertTrue(any(r["code"] == "150122" for r in res))

    def test_busca_por_departamento(self):
        # 'Arequipa' NO es un distrito llamado así por sí solo suficiente: la mejora hace que
        # matchee todos los distritos del departamento/provincia Arequipa.
        res = self._buscar("Arequipa")
        self.assertTrue(len(res) > 1, "buscar por departamento debe traer varios distritos")
        self.assertTrue(all("code" in r and "name" in r for r in res))

    def test_query_vacia_no_falla(self):
        # Contrato existente (lo usa Negocio): q vacío devuelve una lista (primeros N), sin error.
        res = self._buscar("")
        self.assertIsInstance(res, list)

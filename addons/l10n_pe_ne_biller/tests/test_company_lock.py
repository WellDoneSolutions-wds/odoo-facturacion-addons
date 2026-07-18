from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestCompanyLock(TransactionCase):
    """Modelo multi-DB (1 empresa = 1 base = 1 RUC): no se crea una res.company
    adicional desde la UI/ORM; solo el provisioning sancionado, que pasa el bypass
    'l10n_pe_ne_allow_company_create' por contexto, puede hacerlo."""

    def test_create_sin_bypass_bloqueado(self):
        with self.assertRaises(UserError):
            self.env["res.company"].create(
                {"name": "Empresa Fantasma SAC", "vat": "20999999990"}
            )

    def test_create_con_bypass_permitido(self):
        company = self.env["res.company"].with_context(
            l10n_pe_ne_allow_company_create=True
        ).create({"name": "Empresa Provisionada SAC", "vat": "20999999980"})
        self.assertTrue(company.id)
        self.assertEqual(company.vat, "20999999980")

    def test_provision_tenant_sigue_creando(self):
        """El provisioning (modo multi-RUC) sigue creando la company vía el bypass."""
        admin = self.env.ref("base.user_admin")
        admin.group_ids = [(4, self.env.ref("base.group_system").id)]
        res = self.env["res.company"].with_user(admin).l10n_pe_ne_provision_tenant({
            "ruc": "20999999970",
            "razonSocial": "Emisor Provision SAC",
            "login": "emisor_provision_test",
            "password": "test-pass-123",
        })
        self.assertTrue(res["createdCompany"])
        self.assertEqual(res["ruc"], "20999999970")
        company = self.env["res.company"].search([("vat", "=", "20999999970")], limit=1)
        self.assertTrue(company.id)

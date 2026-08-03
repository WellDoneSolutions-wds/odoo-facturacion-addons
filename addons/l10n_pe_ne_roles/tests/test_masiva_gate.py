from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMasivaGate(TransactionCase):
    """La emisión masiva (l10n_pe_ne.lote) queda reservada a supervisor/dueño. El ACL base de
    `emisor` la concede a todo rol operativo (cajero incluido) y la SPA solo la oculta del menú,
    así que un cajero podía crear/procesar un lote por URL o endpoint. El gate es el muro real."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company

    def _usuario(self, login, grupos):
        return self.env["res.users"].create({
            "name": login, "login": login,
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(4, self.env.ref(g).id) for g in grupos],
        })

    def test_cajero_no_puede_emitir_masivo(self):
        cajero = self._usuario("mg_cajero", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        Lote = self.env["l10n_pe_ne.lote"].with_user(cajero)
        with self.assertRaises(AccessError):
            Lote._l10n_pe_ne_masiva_gate()
        # El muro cubre el endpoint real: el gate salta ANTES de parsear el payload.
        with self.assertRaises(AccessError):
            Lote.l10n_pe_ne_crear_lote({})

    def test_vendedor_tampoco(self):
        vend = self._usuario("mg_vend", ["l10n_pe_ne_roles.group_l10n_pe_ne_ventas"])
        with self.assertRaises(AccessError):
            self.env["l10n_pe_ne.lote"].with_user(vend)._l10n_pe_ne_masiva_gate()

    def test_supervisor_puede(self):
        sup = self._usuario("mg_sup", ["l10n_pe_ne_roles.group_l10n_pe_ne_supervisor"])
        # No lanza: pasa el muro (lo que siga es lógica de negocio, fuera de este test).
        self.env["l10n_pe_ne.lote"].with_user(sup)._l10n_pe_ne_masiva_gate()

    def test_duenio_puede_por_implied(self):
        due = self._usuario("mg_due", ["l10n_pe_ne_roles.group_l10n_pe_ne_duenio"])
        self.env["l10n_pe_ne.lote"].with_user(due)._l10n_pe_ne_masiva_gate()

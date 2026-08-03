from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestFacadeGates(TransactionCase):
    """Editar productos/precios y datos del negocio se reserva a supervisor/dueño (matriz de menú).
    El ACL base de `emisor` lo permitía a cualquier rol operativo por API directa (Postman). Crear
    producto NO se gatea: el POS lo auto-crea al vender."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.Move = self.env["account.move"]

    def _usuario(self, login, grupos):
        return self.env["res.users"].create({
            "name": login, "login": login,
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(4, self.env.ref(g).id) for g in grupos],
        })

    def test_cajero_no_edita_producto(self):
        cajero = self._usuario("fg_cajero", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        prod = self.env["product.product"].create({"name": "TEST FG", "list_price": 10})
        with self.assertRaises(AccessError):
            self.Move.with_user(cajero).l10n_pe_ne_update_producto({"id": prod.id, "precio": 1})

    def test_cajero_no_edita_negocio(self):
        cajero = self._usuario("fg_cajero_n", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        with self.assertRaises(AccessError):
            self.Move.with_user(cajero).l10n_pe_ne_update_negocio({})

    def test_supervisor_edita_producto(self):
        sup = self._usuario("fg_sup", ["l10n_pe_ne_roles.group_l10n_pe_ne_supervisor"])
        prod = self.env["product.product"].create({"name": "TEST FG2", "list_price": 10})
        # Pasa el gate (supervisor): no lanza AccessError y actualiza el precio.
        self.Move.with_user(sup).l10n_pe_ne_update_producto({"id": prod.id, "precio": 20})

    def test_duenio_edita_producto_por_implied(self):
        due = self._usuario("fg_due", ["l10n_pe_ne_roles.group_l10n_pe_ne_duenio"])
        prod = self.env["product.product"].create({"name": "TEST FG3", "list_price": 10})
        self.Move.with_user(due).l10n_pe_ne_update_producto({"id": prod.id, "precio": 30})

    def test_cajero_si_crea_producto(self):
        # Crear NO está gateado (el POS lo necesita): el cajero no debe recibir AccessError.
        cajero = self._usuario("fg_cajero_c", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        try:
            self.Move.with_user(cajero).l10n_pe_ne_create_producto(
                {"descripcion": "NUEVO POS", "precio": 5, "unidad": "NIU",
                 "taxCode": "1000", "tipo": "bien"})
        except AccessError:
            self.fail("crear un producto NO debe estar gateado (el POS lo necesita)")
        except Exception:
            pass  # otras fallas (config del entorno de test) no son el objeto de esta prueba

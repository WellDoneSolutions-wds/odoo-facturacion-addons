from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestGasto(TransactionCase):
    """Integridad del gasto (iteración 2): autoría (D-3) e inmutabilidad append-only (D-2).
    Un gasto no se edita ni se borra; corregir = contra-asiento (reversa en negativo)."""

    def setUp(self):
        super().setUp()
        self.Gasto = self.env["l10n_pe_ne.gasto"]
        self.company = self.env.company

    # ── D-3: autoría ──────────────────────────────────────────────────────────
    def test_autoria_en_el_dict(self):
        d = self.Gasto.l10n_pe_ne_create_gasto({"descripcion": "Luz", "monto": 40, "cuenta": "Efectivo"})
        g = self.Gasto.browse(d["id"])
        self.assertEqual(g.usuario_id, self.env.user)
        self.assertEqual(d["usuario"], self.env.user.name)
        self.assertFalse(d["esReversa"])

    # ── D-2: append-only ──────────────────────────────────────────────────────
    def test_update_bloqueado(self):
        d = self.Gasto.l10n_pe_ne_create_gasto({"descripcion": "Agua", "monto": 20})
        with self.assertRaisesRegex(UserError, "no se puede editar"):
            self.Gasto.l10n_pe_ne_update_gasto({"id": d["id"], "monto": 5})

    def test_write_campo_negocio_bloqueado(self):
        """La guarda vive en el ORM: ni por la ruta /web se reescribe el monto."""
        d = self.Gasto.l10n_pe_ne_create_gasto({"descripcion": "Gaseosas", "monto": 15})
        g = self.Gasto.browse(d["id"])
        with self.assertRaisesRegex(UserError, "no se puede editar"):
            g.write({"monto": 1})
        # el bypass del sistema (migraciones) sí puede
        g.with_context(l10n_pe_ne_bypass_lock=True).write({"monto": 15})

    def test_unlink_bloqueado(self):
        d = self.Gasto.l10n_pe_ne_create_gasto({"descripcion": "Flete", "monto": 100})
        g = self.Gasto.browse(d["id"])
        with self.assertRaisesRegex(UserError, "no se puede eliminar"):
            g.unlink()

    def test_unlink_bloqueado_por_acl(self):
        """La ACL del emisor tiene perm_unlink=0. El override append-only lanza PRIMERO (un
        UserError base), así que para ejercitar la capa de ACL de Odoo hay que saltarlo con el
        contexto de bypass: entonces super().unlink() llega a check_access y da AccessError."""
        d = self.Gasto.l10n_pe_ne_create_gasto({"descripcion": "Movilidad", "monto": 12})
        user = self.env["res.users"].create({
            "name": "Emisor G", "login": "emisor_gasto_it2",
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(4, self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id)],
        })
        with self.assertRaises(AccessError):
            self.Gasto.browse(d["id"]).with_user(user).with_context(
                l10n_pe_ne_bypass_lock=True).unlink()

    def test_reversa_crea_contra_asiento(self):
        d = self.Gasto.l10n_pe_ne_create_gasto({"descripcion": "Compra útiles", "monto": 80})
        rev = self.Gasto.l10n_pe_ne_reversar_gasto(d["id"], motivo="Devuelto")
        self.assertEqual(rev["monto"], -80.0)
        self.assertTrue(rev["esReversa"])
        self.assertEqual(rev["reversaDe"], d["id"])
        # el neto del periodo queda en 0 (se netean original + reversa)
        rg = self.Gasto.browse(rev["id"])
        self.assertEqual(rg.gasto_reversado_id.id, d["id"])

    def test_no_reversar_dos_veces(self):
        d = self.Gasto.l10n_pe_ne_create_gasto({"descripcion": "Taxi", "monto": 25})
        self.Gasto.l10n_pe_ne_reversar_gasto(d["id"])
        with self.assertRaisesRegex(UserError, "ya fue reversado"):
            self.Gasto.l10n_pe_ne_reversar_gasto(d["id"])

    def test_no_reversar_una_reversa(self):
        d = self.Gasto.l10n_pe_ne_create_gasto({"descripcion": "Cena", "monto": 60})
        rev = self.Gasto.l10n_pe_ne_reversar_gasto(d["id"])
        with self.assertRaisesRegex(UserError, "reversa"):
            self.Gasto.l10n_pe_ne_reversar_gasto(rev["id"])

    def test_delete_endpoint_ahora_reversa(self):
        """El endpoint viejo de borrado ahora reversa (no rompe a un cliente que aún lo llame)."""
        d = self.Gasto.l10n_pe_ne_create_gasto({"descripcion": "Error de tipeo", "monto": 33})
        res = self.Gasto.l10n_pe_ne_delete_gasto(d["id"])
        self.assertTrue(res["esReversa"])
        self.assertEqual(res["monto"], -33.0)

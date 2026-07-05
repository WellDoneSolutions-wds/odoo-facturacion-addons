from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestCaja(TransactionCase):
    """QW07: caja — modelo, aislamiento multi-compañía, índice de sesión única, ciclo
    abrir/movimiento/cerrar/arqueo y amarre de ventas (aritmética en tools/caja_arqueo)."""

    def setUp(self):
        super().setUp()
        self.Sesion = self.env["l10n_pe_ne.caja.sesion"]
        self.Movimiento = self.env["l10n_pe_ne.caja.movimiento"]
        self.company = self.env.company

    def test_campos_defaults(self):
        ses = self.Sesion.create({"saldo_inicial": 100.0})
        # defaults de la sesión
        self.assertEqual(ses.estado, "abierta")
        self.assertTrue(ses.fecha_apertura)
        self.assertEqual(ses.usuario_apertura_id, self.env.user)
        self.assertEqual(ses.currency_id, self.company.currency_id)
        self.assertEqual(ses.company_id, self.company)
        self.assertEqual(ses.saldo_inicial, 100.0)
        # movimiento: company_id PROPIO (no related) con default env.company
        mov = self.Movimiento.create({
            "sesion_id": ses.id, "tipo": "ingreso", "motivo": "Fondo inicial", "monto": 50.0,
        })
        self.assertEqual(mov.company_id, self.company)
        self.assertEqual(mov.usuario_id, self.env.user)
        self.assertEqual(mov.currency_id, self.company.currency_id)
        self.assertTrue(mov.fecha)
        self.assertIn(mov, ses.movimiento_ids)

    def test_multicompania(self):
        ses_a = self.Sesion.create({"saldo_inicial": 100.0})
        mov_a = self.Movimiento.create({
            "sesion_id": ses_a.id, "tipo": "ingreso", "motivo": "Fondo", "monto": 10.0,
        })
        company_b = self.env["res.company"].create({"name": "CAJA B SAC"})
        user_b = self.env["res.users"].create({
            "name": "Cajero B", "login": "cajero_b_qw07",
            "company_id": company_b.id, "company_ids": [(6, 0, [company_b.id])],
            "group_ids": [(4, self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id)],
        })
        # La compañía B no ve la sesión de A ni por search ni por read directo.
        self.assertFalse(self.Sesion.with_user(user_b).search([("id", "=", ses_a.id)]))
        with self.assertRaises(AccessError):
            ses_a.with_user(user_b).read(["estado"])
        # El movimiento (company_id PROPIO) también queda aislado por su ir.rule.
        self.assertFalse(self.Movimiento.with_user(user_b).search([("id", "=", mov_a.id)]))
        with self.assertRaises(AccessError):
            mov_a.with_user(user_b).read(["tipo"])

    def test_indice_unica_abierta(self):
        self.Sesion.create({"saldo_inicial": 0.0})
        self.env.flush_all()
        # Segundo INSERT 'abierta' en la misma compañía -> viola el índice único parcial.
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                self.Sesion.create({"saldo_inicial": 0.0})
                self.env.flush_all()

    def test_no_unlink(self):
        """La ACL del emisor tiene perm_unlink=0 (auditoría): borrar una sesión -> AccessError."""
        ses = self.Sesion.create({"saldo_inicial": 0.0})
        user = self.env["res.users"].create({
            "name": "Cajero A", "login": "cajero_a_qw07",
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(4, self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id)],
        })
        with self.assertRaises(AccessError):
            ses.with_user(user).unlink()

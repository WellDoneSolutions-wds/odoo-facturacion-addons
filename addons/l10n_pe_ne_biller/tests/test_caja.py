from datetime import timedelta

from psycopg2 import IntegrityError

from odoo import fields
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

    # ------------------------------------------------------------ métodos (Task 3)
    def test_abrir_y_esperado_efectivo(self):
        d = self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": 150, "nota": "sencillo"})
        self.assertEqual(d["estado"], "abierta")
        self.assertEqual(d["saldoInicial"], 150.0)
        efec = {f["medio"]: f["monto"] for f in d["esperado"]}
        self.assertEqual(efec["Efectivo"], 150.0)     # sin ventas ni movs
        self.assertEqual(d["esperadoTotal"], 150.0)

    def test_abrir_guardas(self):
        self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": 10})
        with self.assertRaisesRegex(UserError, "Ya hay una caja abierta"):
            self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": 10})
        # cerrar para poder probar el saldo negativo en una nueva apertura
        self.Sesion.l10n_pe_ne_cerrar_caja({"conteos": [{"medio": "Efectivo", "contado": 10}]})
        with self.assertRaisesRegex(UserError, "no puede ser negativo"):
            self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": -1})

    def test_movimientos_afectan_efectivo(self):
        self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": 150})
        d = self.Sesion.l10n_pe_ne_caja_movimiento({"tipo": "retiro", "motivo": "Pago proveedor", "monto": 80})
        efec = {f["medio"]: f["monto"] for f in d["esperado"]}
        self.assertEqual(efec["Efectivo"], 70.0)      # 150 - 80
        self.assertEqual(d["retiros"], 80.0)
        d = self.Sesion.l10n_pe_ne_caja_movimiento({"tipo": "ingreso", "motivo": "Sencillo del dueño", "monto": 50})
        efec = {f["medio"]: f["monto"] for f in d["esperado"]}
        self.assertEqual(efec["Efectivo"], 120.0)     # 70 + 50
        self.assertEqual(d["ingresos"], 50.0)
        self.assertEqual(len(d["movimientos"]), 2)

    def test_movimiento_validaciones(self):
        # sin caja abierta
        with self.assertRaisesRegex(UserError, "No hay una caja abierta"):
            self.Sesion.l10n_pe_ne_caja_movimiento({"tipo": "ingreso", "motivo": "x", "monto": 1})
        self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": 0})
        with self.assertRaisesRegex(UserError, "ingreso o retiro"):
            self.Sesion.l10n_pe_ne_caja_movimiento({"tipo": "otro", "motivo": "x", "monto": 1})
        with self.assertRaisesRegex(UserError, "necesita un motivo"):
            self.Sesion.l10n_pe_ne_caja_movimiento({"tipo": "ingreso", "motivo": "  ", "monto": 1})
        with self.assertRaisesRegex(UserError, "mayor a 0"):
            self.Sesion.l10n_pe_ne_caja_movimiento({"tipo": "ingreso", "motivo": "ok", "monto": 0})

    def test_cerrar_y_snapshot(self):
        self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": 100})
        self.Sesion.l10n_pe_ne_caja_movimiento({"tipo": "retiro", "motivo": "banco", "monto": 20})
        arq = self.Sesion.l10n_pe_ne_cerrar_caja({
            "conteos": [{"medio": "Efectivo", "contado": 75}], "nota": "cierre"})
        self.assertEqual(arq["estado"], "cerrada")
        efec = {f["medio"]: f for f in arq["arqueo"]}
        self.assertEqual(efec["Efectivo"]["esperado"], 80.0)     # 100 - 20
        self.assertEqual(efec["Efectivo"]["diferencia"], -5.0)   # 75 - 80
        self.assertEqual(arq["diferenciaTotal"], -5.0)
        sid = arq["id"]
        # re-cerrar / mover sobre cerrada -> UserError
        with self.assertRaisesRegex(UserError, "No hay una caja abierta"):
            self.Sesion.l10n_pe_ne_cerrar_caja({"conteos": [{"medio": "Efectivo", "contado": 75}]})
        # cerrar sin conteos -> UserError
        self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": 0})
        with self.assertRaisesRegex(UserError, "al menos un medio"):
            self.Sesion.l10n_pe_ne_cerrar_caja({"conteos": []})
        # snapshot inmutable: el arqueo de la cerrada no cambia entre llamadas
        a1 = self.Sesion.l10n_pe_ne_caja_arqueo(sid)
        a2 = self.Sesion.l10n_pe_ne_caja_arqueo(sid)
        self.assertEqual(a1["arqueo"], a2["arqueo"])
        self.assertEqual(a1["diferenciaTotal"], -5.0)

    def test_actual_y_list(self):
        self.assertEqual(self.Sesion.l10n_pe_ne_caja_actual(), {"abierta": False, "sesion": None})
        d = self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": 30})
        act = self.Sesion.l10n_pe_ne_caja_actual()
        self.assertTrue(act["abierta"] and act["sesion"]["id"] == d["id"])
        filas = self.Sesion.l10n_pe_ne_list_cajas()
        self.assertIn(d["id"], [f["id"] for f in filas])
        fila = next(f for f in filas if f["id"] == d["id"])
        self.assertIsNone(fila["contadoTotal"])     # abierta -> contado/diferencia null

    # ------------------------------------------------- amarre de ventas (hermético)
    def _caja_fixtures(self):
        """Fixtures de facturación (misma localización PE que usan los demás tests)."""
        self._caja_igv = self.env["account.tax"].search([
            ("company_id", "=", self.company.id), ("type_tax_use", "=", "sale"),
            ("l10n_pe_edi_tax_code", "=", "1000")], limit=1)
        self.assertTrue(self._caja_igv, "Falta el IGV 1000 de la localización PE")
        ruc_type = self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", "6")], limit=1)
        self._caja_partner = self.env["res.partner"].create({
            "name": "CLIENTE CAJA SAC", "vat": "20100070970",
            "l10n_latam_identification_type_id": ruc_type.id})
        self._caja_product = self.env["product.product"].create(
            {"name": "PRODUCTO CAJA", "default_code": "QW7C"})

    def _caja_abrir(self, saldo=100):
        """Abre la caja y ANCLA fecha_apertura en el pasado para que la ventana
        [fecha_apertura, now()] incluya de forma DETERMINISTA las ventas que se crean a
        continuación. Sin esto el test es flaky: create_date lo pone Postgres al INICIO de
        la transacción (constante en toda la TransactionCase), mientras fecha_apertura es un
        Python now() truncado al segundo; al cruzar un borde de segundo create_date puede
        caer una fracción ANTES de fecha_apertura y la venta queda fuera de la ventana."""
        d = self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": saldo})
        sesion = self.Sesion.browse(d["id"])
        sesion.fecha_apertura = fields.Datetime.now() - timedelta(minutes=5)
        self._caja_sesion = sesion
        return sesion

    def _venta_enviada(self, correlativo, forma="Contado", medios=None, moneda=None):
        """Crea una venta posted+'enviado' cuya create_date se fija DENTRO de la ventana de
        la sesión (sin red; el amarre real vs SUNAT beta es caja_flow.py). Requiere que la
        sesión se haya abierto con _caja_abrir. Devuelve el account.move."""
        vals = {
            "move_type": "out_invoice", "partner_id": self._caja_partner.id,
            "invoice_date": "2026-07-02", "l10n_pe_serie": "F001",
            "l10n_pe_correlativo": correlativo, "l10n_pe_ne_forma_pago": forma,
            "invoice_line_ids": [(0, 0, {
                "product_id": self._caja_product.id, "quantity": 1.0,
                "price_unit": 100.0, "tax_ids": [(6, 0, self._caja_igv.ids)]})],
        }
        if moneda:
            vals["currency_id"] = moneda.id
        move = self.env["account.move"].create(vals)
        if medios is not None:
            move.l10n_pe_ne_medios_pago = medios
        move.action_post()
        move.l10n_pe_biller_state = "enviado"
        # create_date normalmente es read-only y la fija Postgres; la forzamos a un instante
        # holgadamente dentro de la ventana para eliminar el flake del borde del segundo.
        move.flush_recordset()
        dentro = self._caja_sesion.fecha_apertura + timedelta(seconds=5)
        self.env.cr.execute(
            "UPDATE account_move SET create_date=%s WHERE id=%s", (dentro, move.id))
        move.invalidate_recordset(["create_date"])
        return move

    def test_amarre_ventas(self):
        self._caja_fixtures()
        usd = self.env.ref("base.USD")
        usd.active = True

        # abrir la caja ANTES de emitir para que las ventas caigan en la ventana create_date
        self._caja_abrir(100)
        v_medios = self._venta_enviada("1101", medios=[{"medio": "Yape", "monto": 30},
                                                       {"medio": "Efectivo", "monto": 20}])
        v_efec = self._venta_enviada("1102", medios=None)                 # Contado sin medios
        v_cred = self._venta_enviada("1103", forma="Credito", medios=None)  # Crédito: excluido
        v_usd = self._venta_enviada("1104", moneda=usd)                   # USD: aparte

        d = self.Sesion.l10n_pe_ne_caja_actual()["sesion"]
        efec = {f["medio"]: f["monto"] for f in d["esperado"]}
        # Yape = 30 (solo el medio detallado); Efectivo = saldo + medio Efectivo(20) + v_efec total
        self.assertEqual(efec["Yape"], 30.0)
        self.assertEqual(efec["Efectivo"], round(100 + 20 + v_efec.amount_total, 2))
        # Crédito NO aporta a ningún medio; USD no aparece en porMedio
        self.assertNotIn("USD", efec)
        # ventas PEN: v_medios, v_efec, v_cred (3); un solo sinMedio (v_efec); USD aparte
        self.assertEqual(d["ventas"]["count"], 3)
        self.assertEqual(d["ventas"]["sinMedio"], 1)
        self.assertEqual(d["ventas"]["countUsd"], 1)
        self.assertEqual(d["ventas"]["totalUsd"], round(v_usd.amount_total, 2))
        self.assertEqual(d["ventas"]["total"],
                         round(v_medios.amount_total + v_efec.amount_total + v_cred.amount_total, 2))

    def test_snapshot_inmutable_bajo_mutacion(self):
        """HU4: el arqueo de una sesión CERRADA lee los snapshots congelados
        (conteos_cierre/ventas_cierre), NO re-consulta las ventas. Se prueba MUTANDO una
        venta amarrada tras el cierre (anulándola): si el arqueo re-consultara, la venta
        saldría de la ventana y el conteo bajaría; como está congelado, el arqueo no cambia."""
        self._caja_fixtures()
        sesion = self._caja_abrir(100)
        # una venta contado Efectivo 118 amarrada a la sesión
        self._venta_enviada("1201", medios=[{"medio": "Efectivo", "monto": 118}])
        d = self.Sesion.l10n_pe_ne_caja_actual()["sesion"]
        self.assertEqual(d["ventas"]["count"], 1)     # amarrada mientras está abierta
        esp = {f["medio"]: f["monto"] for f in d["esperado"]}
        self.assertEqual(esp["Efectivo"], 218.0)      # saldo 100 + medio 118

        # cerrar: congela conteos_cierre + ventas_cierre (arqueo con diferencia -2.30)
        arq = self.Sesion.l10n_pe_ne_cerrar_caja({
            "conteos": [{"medio": "Efectivo", "contado": esp["Efectivo"] - 2.30}]})
        sid = arq["id"]
        self.assertEqual(arq["diferenciaTotal"], -2.30)

        before = self.Sesion.l10n_pe_ne_caja_arqueo(sid)
        self.assertEqual(before["ventas"]["count"], 1)

        # MUTACIÓN post-cierre: anular la venta amarrada. Una re-consulta la excluiría
        # (el filtro exige l10n_pe_biller_state == 'enviado'); el snapshot NO debe moverse.
        venta = self.env["account.move"].search(
            [("l10n_pe_correlativo", "=", "1201"), ("company_id", "=", self.company.id)], limit=1)
        self.assertTrue(venta)
        venta.l10n_pe_biller_state = "anulado"

        after = self.Sesion.l10n_pe_ne_caja_arqueo(sid)
        # el arqueo histórico completo es idéntico: lee los snapshots, no re-consulta
        self.assertEqual(after, before)
        self.assertEqual(after["ventas"]["count"], 1)          # sigue contando la venta congelada
        self.assertEqual(after["diferenciaTotal"], -2.30)

    def test_arqueo_cross_tenant(self):
        d = self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": 10})
        company_b = self.env["res.company"].create({"name": "CAJA C SAC"})
        user_b = self.env["res.users"].create({
            "name": "Cajero C", "login": "cajero_c_qw07",
            "company_id": company_b.id, "company_ids": [(6, 0, [company_b.id])],
            "group_ids": [(4, self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id)],
        })
        with self.assertRaises(AccessError):
            self.Sesion.with_user(user_b).l10n_pe_ne_caja_arqueo(d["id"])

from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestCotizacionFlujo(TransactionCase):
    """CN-01: la cotización como modelo de flujo. Freeze H4, transiciones por rol, conversión de
    IGV (regresión), despacho gateado, colas y vigencia (P6). La emisión real (quick_emit ->
    microservicio) se valida en e2e; aquí se prueba el payload y los guards."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.Cot = self.env["l10n_pe_ne.cotizacion"]
        ruc_type = self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", "6")], limit=1)
        self.cliente = self.env["res.partner"].create({
            "name": "CLIENTE CN01 SAC", "vat": "20100070970",
            "l10n_latam_identification_type_id": ruc_type.id})
        self.producto = self.env["product.product"].create({"name": "PROD CN01", "default_code": "CN01"})

    def _cot(self, afecto=True, precio=118.0, estado="borrador"):
        cot = self.Cot.create({
            "partner_id": self.cliente.id,
            "line_ids": [(0, 0, {"product_id": self.producto.id, "descripcion": "PROD CN01",
                                 "cantidad": 1.0, "precio_unitario": precio, "afecto_igv": afecto})],
        })
        if estado != "borrador":
            cot.write({"estado": estado})
        return cot

    def _user(self, login, grupos):
        return self.env["res.users"].create({
            "name": login, "login": login,
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(4, self.env.ref("base.group_user").id)]
                         + [(4, self.env.ref(g).id) for g in grupos],
        })

    # ── Freeze H4 (piso del biller) ─────────────────────────────────────────────
    def test_freeze_update_convertida(self):
        cot = self._cot(estado="convertida")
        with self.assertRaisesRegex(UserError, "no se puede editar"):
            self.Cot.l10n_pe_ne_update_cotizacion({"id": cot.id, "notas": "x"})

    def test_freeze_delete_convertida(self):
        cot = self._cot(estado="convertida")
        with self.assertRaisesRegex(UserError, "No se puede borrar"):
            self.Cot.l10n_pe_ne_delete_cotizacion(cot.id)

    def test_set_estado_solo_transiciones_validas(self):
        cot = self._cot()
        with self.assertRaisesRegex(UserError, "No se puede pasar"):
            cot.l10n_pe_ne_set_estado("convertida")   # nunca a mano
        cot.write({"estado": "convertida"})
        with self.assertRaisesRegex(UserError, "No se puede pasar"):
            cot.l10n_pe_ne_set_estado("borrador")     # no se sale de convertida

    # ── Transiciones por rol ────────────────────────────────────────────────────
    def test_aceptar_gateado_por_ventas(self):
        cot = self._cot()
        cajero = self._user("caj_cn01", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        with self.assertRaises(AccessError):
            cot.with_user(cajero).l10n_pe_ne_aceptar()
        vendedor = self._user("ven_cn01", ["l10n_pe_ne_roles.group_l10n_pe_ne_ventas"])
        cot.with_user(vendedor).l10n_pe_ne_aceptar()   # salto borrador->aceptada de un clic (D4)
        self.assertEqual(cot.estado, "aceptada")

    def test_no_convertida_a_mano(self):
        cot = self._cot(estado="aceptada")
        with self.assertRaisesRegex(UserError, "No se puede pasar"):
            cot._avanzar("convertida")   # 'convertida' no es arista del mixin

    def test_rechazar_exige_motivo(self):
        # con un vendedor explícito (env.user de TransactionCase es root, SIN grupos → el eje
        # de grupo saltaría antes que el de motivo).
        cot = self._cot(estado="aceptada")
        vendedor = self._user("ven_rech_cn01", ["l10n_pe_ne_roles.group_l10n_pe_ne_ventas"])
        cotv = cot.with_user(vendedor)
        with self.assertRaisesRegex(UserError, "motivo"):
            cotv.l10n_pe_ne_rechazar()
        cotv.l10n_pe_ne_rechazar("cliente desistió")
        self.assertEqual(cot.estado, "rechazada")

    # ── Conversión de IGV (regresión crítica) ───────────────────────────────────
    def test_payload_emision_convierte_igv(self):
        cot = self._cot(afecto=True, precio=118.0)   # precio CON IGV
        payload = cot._l10n_pe_ne_payload_emision()
        self.assertEqual(payload["tipoDoc"], "01")   # RUC 11 díg -> factura
        linea = payload["lineas"][0]
        # quick_emit espera SIN IGV: 118 / 1.18 = 100 (no 118 -> el comprobante saldría +18%)
        self.assertAlmostEqual(linea["precioUnitario"], 100.0, places=2)
        self.assertEqual(linea["taxCode"], "1000")

    def test_payload_no_gravado_sin_conversion(self):
        cot = self._cot(afecto=False, precio=50.0)
        linea = cot._l10n_pe_ne_payload_emision()["lineas"][0]
        self.assertEqual(linea["precioUnitario"], 50.0)   # no gravado: tal cual
        self.assertEqual(linea["taxCode"], "9997")

    # ── Despacho (P5) ───────────────────────────────────────────────────────────
    def test_entregar_gateado_y_solo_cobrada(self):
        # cotización "cobrada" simulada: convertida + despacho pendiente
        cot = self._cot(estado="convertida")
        cot.estado_despacho = "pendiente"
        cajero = self._user("caj2_cn01", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        with self.assertRaises(AccessError):
            cot.with_user(cajero).l10n_pe_ne_entregar()
        desp = self._user("des_cn01", ["l10n_pe_ne_roles.group_l10n_pe_ne_despacho"])
        cot.with_user(desp).l10n_pe_ne_entregar("Juan Perez", "43609977")
        self.assertEqual(cot.estado_despacho, "entregado")
        self.assertEqual(cot.receptor_nombre, "Juan Perez")

    def test_entregar_solo_pendiente(self):
        # convertida pero SIN despacho pendiente (estado_despacho=no_aplica por default): el
        # despachador SÍ la ve (su ir.rule = convertida) pero la guarda de realidad la frena. Una
        # 'aceptada' ni la vería (ir.rule del despacho = solo convertida → AccessError, no la guarda).
        cot = self._cot(estado="convertida")
        desp = self._user("des2_cn01", ["l10n_pe_ne_roles.group_l10n_pe_ne_despacho"])
        with self.assertRaisesRegex(UserError, "cobrada"):
            cot.with_user(desp).l10n_pe_ne_entregar()

    # ── Colas + ir.rule por rol ─────────────────────────────────────────────────
    def test_colas_y_segregacion(self):
        c_acept = self._cot(estado="aceptada")
        c_conv = self._cot(estado="convertida")
        c_conv.estado_despacho = "pendiente"
        self._cot(estado="borrador")
        # cola de cobro = aceptadas sin convertir
        cobro = self.Cot.l10n_pe_ne_cola_cobro()
        self.assertIn(c_acept.id, [i["id"] for i in cobro["items"]])
        self.assertNotIn(c_conv.id, [i["id"] for i in cobro["items"]])
        # cola de despacho = convertidas pendientes
        desp = self.Cot.l10n_pe_ne_cola_despacho()
        self.assertIn(c_conv.id, [i["id"] for i in desp["items"]])
        # ir.rule: un cajero puro NO ve borradores (solo aceptada/convertida)
        cajero = self._user("caj3_cn01", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        visibles = self.Cot.with_user(cajero).search([]).mapped("estado")
        self.assertNotIn("borrador", visibles)
        # un despachador puro solo ve convertidas
        desp_u = self._user("des3_cn01", ["l10n_pe_ne_roles.group_l10n_pe_ne_despacho"])
        est_desp = set(self.Cot.with_user(desp_u).search([]).mapped("estado"))
        self.assertTrue(est_desp <= {"convertida"})

    # ── P6 · vigencia vinculante ────────────────────────────────────────────────
    def test_cobrar_vencida_bloqueado(self):
        cot = self._cot(estado="aceptada")
        cot.write({"fecha": fields.Date.context_today(self) - timedelta(days=40), "validez_dias": 15})
        self.assertTrue(cot._l10n_pe_ne_vencida())
        with self.assertRaisesRegex(UserError, "venció"):
            cot._l10n_pe_ne_guard_cobrable()

    def test_cron_marca_vencidas(self):
        cot = self._cot(estado="aceptada")
        cot.write({"fecha": fields.Date.context_today(self) - timedelta(days=40), "validez_dias": 15})
        self.Cot._l10n_pe_ne_cron_vencer()
        self.assertEqual(cot.estado, "vencida")

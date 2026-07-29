import json
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo.tests import HttpCase, tagged

from odoo.addons.l10n_pe_ne_biller.tests.common import EnvioSincronoMixin

# La emisión a SUNAT se DOBLA: se prueba el flujo por el controller (segregación por rol REAL con
# with_user+has_group, que los tests unitarios corriendo como root NO ejercen), no la emisión fiscal.
_EMIT = "odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post"
_OK = type("R", (), {"status_code": 200, "text": '<?xml version="1.0"?><Invoice/>', "headers": {}})()


@tagged("post_install", "-at_install")
class TestCn01Http(EnvioSincronoMixin, HttpCase):
    """CN-01 (mostrador) e2e por /ne/api/*: cotiza → cobra → despacho, gateado por rol de verdad.

    REQUISITOS DE ENTORNO para los 2 tests que emiten (los de segregación pasan sin esto):
      · Plan Contable l10n_pe en la compañía (diario `sale` + IGV de venta `l10n_pe_edi_tax_code=1000`),
        mismo supuesto que l10n_pe_ne_biller/tests/test_stock_emision.py.
      · `stock_account` instalado (auto_install): el cobro mueve stock corriendo como el CAJERO
        (no-root), que accede a stock.move por la cadena emisor→account.group_account_invoice. Sin
        ese módulo, el `create` del stock.move daría AccessError→403.
    """

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self._keys = {}
        ruc_type = self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", "6")], limit=1)
        self.cliente = self.env["res.partner"].create({
            "name": "CLIENTE CN01 HTTP", "vat": "20100070970",
            "l10n_latam_identification_type_id": ruc_type.id})
        # almacenable: sin is_storable el eje de despacho no se abre (estadoDespacho=no_aplica).
        self.producto = self.env["product.product"].create(
            {"name": "PROD CN01 HTTP", "default_code": "CN01H", "type": "consu", "is_storable": True})
        self.vendedor = self._user("ven_http", ["ventas"])
        self.cajero = self._user("caj_http", ["caja"])
        self.despachador = self._user("des_http", ["despacho"])
        self.modal = self._user("modal_http", ["ventas", "caja", "despacho"])

    # ── infra HTTP (Bearer scoped key por usuario, como el BFF) ─────────────────
    def _user(self, login, roles):
        u = self.env["res.users"].create({
            "name": login, "login": login,
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(4, self.env.ref("base.group_user").id)]
                         + [(4, self.env.ref("l10n_pe_ne_roles.group_l10n_pe_ne_" + r).id) for r in roles],
        })
        self._keys[u.id] = self.env["res.users.apikeys"].with_user(u)._generate(
            "l10n_pe_ne", "http-" + login, datetime.now() + timedelta(hours=12))
        return u

    def _req(self, method, path, user, body=None):
        headers = {"Authorization": "Bearer %s" % self._keys[user.id],
                   "Content-Type": "application/json"}
        r = self.url_open("/ne/api" + path,
                          data=(json.dumps(body).encode() if body is not None else None),
                          headers=headers, method=method)
        return r.status_code, (r.json() if r.content else {})

    def _get(self, path, user):
        return self._req("GET", path, user)

    def _post(self, path, user, body=None):
        return self._req("POST", path, user, {} if body is None else body)

    def _crear(self, user):
        return self._post("/cotizaciones", user, {
            "clienteId": self.cliente.id,
            "items": [{"productId": self.producto.id, "descripcion": "PROD CN01",
                       "cantidad": 1, "precio": 118.0, "afectoIgv": True}]})

    # ── camino feliz segregado (3 personas) ─────────────────────────────────────
    def test_camino_feliz_segregado(self):
        sc, cot = self._crear(self.vendedor)
        self.assertEqual(sc, 200, cot)
        cid = cot["id"]
        sc, r = self._post("/cotizaciones/%s/aceptar" % cid, self.vendedor)
        self.assertEqual(sc, 200)
        self.assertEqual(r["estado"], "aceptada")
        # el cajero ve su cola de cobro
        sc, cola = self._get("/cotizaciones/cola-cobro", self.cajero)
        self.assertEqual(sc, 200)
        self.assertIn(cid, [i["id"] for i in cola["items"]])
        # el cajero cobra (emisión doblada) -> convertida + despacho pendiente
        with patch(_EMIT, return_value=_OK):
            sc, r = self._post("/cotizaciones/%s/cobrar-entregar" % cid, self.cajero, {"entregar": False})
        self.assertEqual(sc, 200, r)
        self.assertEqual(r["estado"], "convertida")
        self.assertEqual(r["estadoDespacho"], "pendiente")
        # IGV: el comprobante sale por 118 total (valor venta 100 + IGV 18), no 118+18
        self.env.invalidate_all()
        move = self.env["account.move"].browse(r["comprobanteId"])
        self.assertAlmostEqual(move.amount_total, 118.0, places=2)
        self.assertAlmostEqual(move.amount_untaxed, 100.0, places=2)
        self.assertAlmostEqual(move.amount_tax, 18.0, places=2)
        # el despachador entrega
        sc, r = self._post("/despacho/%s/entregar" % cid, self.despachador,
                           {"receptorNombre": "Juan Perez", "receptorDoc": "43609977"})
        self.assertEqual(sc, 200)
        self.assertEqual(r["estadoDespacho"], "entregado")

    # ── escala libre: 1 usuario con todos los roles ─────────────────────────────
    def test_escala_libre_un_usuario(self):
        sc, cot = self._crear(self.modal)
        cid = cot["id"]
        self._post("/cotizaciones/%s/aceptar" % cid, self.modal)
        # al modal le aparece el fold completo
        sc, acc = self._get("/cotizaciones/%s/acciones" % cid, self.modal)
        keys = [a["key"] for a in acc]
        self.assertIn("cobrar-entregar", keys)
        with patch(_EMIT, return_value=_OK):
            sc, r = self._post("/cotizaciones/%s/cobrar-entregar" % cid, self.modal, {"entregar": True})
        self.assertEqual(sc, 200, r)
        self.assertEqual(r["estado"], "convertida")
        self.assertEqual(r["estadoDespacho"], "entregado")   # cobró y entregó en un commit

    # ── segregación (cada rol solo su tramo) ────────────────────────────────────
    def test_segregacion_por_rol(self):
        sc, cot = self._crear(self.vendedor)
        cid = cot["id"]
        # el cajero NO acepta (no es ventas) -> 403
        sc, _ = self._post("/cotizaciones/%s/aceptar" % cid, self.cajero)
        self.assertEqual(sc, 403)
        self._post("/cotizaciones/%s/aceptar" % cid, self.vendedor)
        # el vendedor NO cobra (no es caja) -> 403
        sc, _ = self._post("/cotizaciones/%s/cobrar-entregar" % cid, self.vendedor, {"entregar": False})
        self.assertEqual(sc, 403)

    def test_cajero_no_ve_borradores(self):
        self._crear(self.vendedor)   # queda en borrador
        sc, res = self._get("/cotizaciones?page=1&pageSize=100", self.cajero)
        self.assertEqual(sc, 200)
        items = res["items"] if isinstance(res, dict) else res
        self.assertTrue(all(i["estado"] in ("aceptada", "convertida") for i in items),
                        "el cajero solo ve aceptadas/convertidas, nunca borradores")

    def test_rechazar_exige_motivo(self):
        sc, cot = self._crear(self.vendedor)
        cid = cot["id"]
        self._post("/cotizaciones/%s/aceptar" % cid, self.vendedor)
        sc, err = self._post("/cotizaciones/%s/rechazar" % cid, self.vendedor, {})
        self.assertEqual(sc, 400)
        self.assertIn("motivo", err["message"])
        sc, r = self._post("/cotizaciones/%s/rechazar" % cid, self.vendedor, {"motivo": "cliente desistió"})
        self.assertEqual(sc, 200)
        self.assertEqual(r["estado"], "rechazada")

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo.tests import HttpCase, tagged

from odoo.addons.l10n_pe_ne_biller.tests.common import EnvioSincronoMixin

_EMIT = "odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post"
_OK = type("R", (), {"status_code": 200, "text": '<?xml version="1.0"?><Invoice/>', "headers": {}})()


@tagged("post_install", "-at_install")
class TestCn02Http(EnvioSincronoMixin, HttpCase):
    """CN-02 (taller) e2e por /ne/api/*: adelanto → cola → toma → saldo, gateado por rol de verdad.
    Verifica también que el adelanto cuadra el arqueo POR SU MEDIO (endpoint /ne/api/caja).

    REQUISITO DE ENTORNO para el test que emite (los de segregación/arqueo pasan sin esto): Plan
    Contable l10n_pe en la compañía (diario `sale` + IGV `1000`). El servicio no mueve stock, así
    que aquí no aplica lo de stock_account (ver test_cn01_http)."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self._keys = {}
        ruc_type = self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", "6")], limit=1)
        self.cliente = self.env["res.partner"].create({
            "name": "CLIENTE CN02 HTTP", "vat": "20100070970",
            "l10n_latam_identification_type_id": ruc_type.id})
        self.servicio = self.env["product.product"].create(
            {"name": "SERVICIO CN02 HTTP", "default_code": "SVC02H", "type": "service"})
        self.recepcion = self._user("rec_http", ["ventas"])
        self.cajero = self._user("caj2_http", ["caja"])
        self.operario = self._user("ope_http", ["taller"])
        self.supervisor = self._user("sup_http", ["supervisor"])
        self.modal = self._user("modal2_http", ["ventas", "caja", "taller"])

    # ── infra HTTP ──────────────────────────────────────────────────────────────
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

    def _abrir_caja(self, user):
        return self._post("/caja/abrir", user, {"saldoInicial": 0})

    def _crear_orden(self, user):
        return self._post("/ordenes", user, {
            "clienteId": self.cliente.id,
            "items": [{"productId": self.servicio.id, "descripcion": "Mantenimiento",
                       "cantidad": 1, "precio": 118.0, "afectoIgv": True}]})

    # ── camino feliz segregado (recepción → caja → taller → caja) ───────────────
    def test_camino_feliz_segregado(self):
        self._abrir_caja(self.cajero)
        sc, orden = self._crear_orden(self.recepcion)
        self.assertEqual(sc, 200, orden)
        oid = orden["id"]
        self.assertEqual(orden["estado"], "borrador")
        self.assertTrue(orden["enCola"])       # nace SIN dueño
        self.assertEqual(orden["saldo"], 118.0)
        # el cajero cobra el adelanto -> encolada
        sc, r = self._post("/ordenes/%s/adelanto" % oid, self.cajero, {"monto": 50, "medio": "Yape"})
        self.assertEqual(sc, 200, r)
        self.assertEqual(r["estado"], "encolada")
        self.assertEqual(r["adelanto"], 50.0)
        self.assertEqual(r["saldo"], 68.0)
        # el adelanto cuadra el arqueo POR SU MEDIO (Yape). Con el CONTEO CIEGO (D-1, iter2) la
        # sesión abierta solo sirve los NOMBRES de los medios — el adelanto siembra su fila de
        # conteo; el monto se verifica al CIERRE (abajo).
        sc, caja = self._get("/caja", self.cajero)
        self.assertEqual(sc, 200)
        self.assertIn("Yape", caja["sesion"]["medios"])
        self.assertNotIn("esperado", caja["sesion"])         # D-1: sin montos en vivo
        self.assertEqual(caja["sesion"]["ingresos"], 0.0)   # no es un ingreso genérico
        # el operario toma de la cola (toma atómica NULL->yo)
        sc, cola = self._get("/ordenes/cola", self.operario)
        self.assertIn(oid, [i["id"] for i in cola["items"]])
        sc, r = self._post("/ordenes/%s/tomar" % oid, self.operario)
        self.assertEqual(sc, 200, r)
        self.assertEqual(r["estado"], "en_proceso")
        self.assertEqual(r["responsable"], self.operario.name)
        # termina el trabajo
        sc, r = self._post("/ordenes/%s/terminar" % oid, self.operario)
        self.assertEqual(sc, 200)
        self.assertEqual(r["estado"], "terminada")
        # el cliente vuelve, el cajero cobra el saldo y entrega (emisión doblada)
        sc, cola = self._get("/ordenes/cola-saldo", self.cajero)
        self.assertIn(oid, [i["id"] for i in cola["items"]])
        with patch(_EMIT, return_value=_OK):
            sc, r = self._post("/ordenes/%s/cobrar-saldo" % oid, self.cajero, {"medio": "Efectivo"})
        self.assertEqual(sc, 200, r)
        self.assertEqual(r["estado"], "entregada")
        self.assertEqual(r["saldoCobrado"], 68.0)
        # CIERRE: el arqueo congelado revela los montos (D-1) y cuenta el adelanto POR SU MEDIO
        # (seam) + la venta por el saldo — Yape 50 y Efectivo 68, sin doble conteo.
        sc, arq = self._post("/caja/cerrar", self.cajero,
                             {"conteos": [{"medio": "Efectivo", "contado": 68},
                                          {"medio": "Yape", "contado": 50}]})
        self.assertEqual(sc, 200, arq)
        esperados = {f["medio"]: f["esperado"] for f in arq["arqueo"]}
        self.assertEqual(esperados.get("Yape"), 50.0)
        self.assertEqual(esperados.get("Efectivo"), 68.0)

    # ── camino feliz Vía A (anticipo facturado, activado por HTTP) ──────────────
    def test_camino_feliz_via_a(self):
        # el dueño/supervisor activa la Vía A por su endpoint de políticas
        sc, pol = self._post("/politicas/adelanto-facturado", self.supervisor, {"activo": True})
        self.assertEqual(sc, 200, pol)
        self.assertTrue(pol["adelantoFacturado"])
        self._abrir_caja(self.cajero)
        sc, orden = self._crear_orden(self.recepcion)
        self.assertEqual(sc, 200, orden)
        oid = orden["id"]
        # el cajero cobra el adelanto -> EMITE el comprobante del anticipo (la respuesta lo trae)
        with patch(_EMIT, return_value=_OK):
            sc, r = self._post("/ordenes/%s/adelanto" % oid, self.cajero, {"monto": 50, "medio": "Yape"})
        self.assertEqual(sc, 200, r)
        self.assertEqual(r["estado"], "encolada")
        self.assertEqual(r["adelanto"], 50.0)
        self.assertTrue(r["anticipoFacturaId"])
        anticipo_numero = r["anticipoNumero"]
        self.assertTrue(anticipo_numero)
        # el operario toma y termina
        sc, r = self._post("/ordenes/%s/tomar" % oid, self.operario)
        self.assertEqual(sc, 200, r)
        sc, r = self._post("/ordenes/%s/terminar" % oid, self.operario)
        self.assertEqual(sc, 200, r)
        # el cliente vuelve, el cajero cobra el saldo y entrega: el final referencia el anticipo
        with patch(_EMIT, return_value=_OK):
            sc, r = self._post("/ordenes/%s/cobrar-saldo" % oid, self.cajero, {"medio": "Efectivo"})
        self.assertEqual(sc, 200, r)
        self.assertEqual(r["estado"], "entregada")
        self.assertEqual(r["saldoCobrado"], 68.0)
        # otra orden con el adelanto ya facturado: anularla se BLOQUEA (hay que emitir la NC primero),
        # y el mensaje nombra el comprobante del anticipo.
        sc, orden2 = self._crear_orden(self.recepcion)
        oid2 = orden2["id"]
        with patch(_EMIT, return_value=_OK):
            sc, r2 = self._post("/ordenes/%s/adelanto" % oid2, self.cajero,
                                {"monto": 50, "medio": "Efectivo"})
        self.assertEqual(sc, 200, r2)
        numero2 = r2["anticipoNumero"]
        sc, err = self._post("/ordenes/%s/anular" % oid2, self.supervisor, {"motivo": "cliente desistió"})
        self.assertEqual(sc, 400, err)
        self.assertIn("nota de crédito", err["message"])
        self.assertIn(numero2, err["message"])

    # ── escala libre: 1 usuario con todos los roles ─────────────────────────────
    def test_escala_libre_un_usuario(self):
        self._abrir_caja(self.modal)
        sc, orden = self._crear_orden(self.modal)
        oid = orden["id"]
        self._post("/ordenes/%s/adelanto" % oid, self.modal, {"monto": 50, "medio": "Efectivo"})
        self._post("/ordenes/%s/tomar" % oid, self.modal)
        self._post("/ordenes/%s/terminar" % oid, self.modal)
        with patch(_EMIT, return_value=_OK):
            sc, r = self._post("/ordenes/%s/cobrar-saldo" % oid, self.modal, {"medio": "Efectivo"})
        self.assertEqual(sc, 200, r)
        self.assertEqual(r["estado"], "entregada")   # el modal recorre todo sin atascarse

    # ── segregación por rol ─────────────────────────────────────────────────────
    def test_segregacion_por_rol(self):
        self._abrir_caja(self.cajero)
        sc, orden = self._crear_orden(self.recepcion)
        oid = orden["id"]
        # el operario NO cobra el adelanto (no es caja) -> 403
        sc, _ = self._post("/ordenes/%s/adelanto" % oid, self.operario, {"monto": 50, "medio": "Efectivo"})
        self.assertEqual(sc, 403)
        # lo cobra el cajero -> encolada
        self._post("/ordenes/%s/adelanto" % oid, self.cajero, {"monto": 50, "medio": "Efectivo"})
        # el cajero NO toma órdenes (no es taller) -> 403
        sc, _ = self._post("/ordenes/%s/tomar" % oid, self.cajero)
        self.assertEqual(sc, 403)

    def test_operario_solo_ve_su_cola(self):
        self._abrir_caja(self.cajero)
        sc, orden = self._crear_orden(self.recepcion)
        oid = orden["id"]
        # en borrador NO está en la cola del taller
        sc, cola = self._get("/ordenes/cola", self.operario)
        self.assertNotIn(oid, [i["id"] for i in cola["items"]])
        # tras el adelanto (encolada) SÍ aparece
        self._post("/ordenes/%s/adelanto" % oid, self.cajero, {"monto": 50, "medio": "Efectivo"})
        sc, cola = self._get("/ordenes/cola", self.operario)
        self.assertIn(oid, [i["id"] for i in cola["items"]])

    # ── PUENTE cotización → orden de taller (e2e por rol REAL) ──────────────────
    def test_puente_cotizacion_a_orden(self):
        # recepción cotiza y ACEPTA; POST /ordenes {cotizacionId} abre la orden con las líneas
        # copiadas; repetir sobre la misma cotización → 400 (una cotización abre UNA orden).
        sc, cot = self._post("/cotizaciones", self.recepcion, {
            "clienteId": self.cliente.id,
            "items": [{"productId": self.servicio.id, "descripcion": "Mantenimiento mayor",
                       "cantidad": 2, "precio": 59.0, "afectoIgv": True}]})
        self.assertEqual(sc, 200, cot)
        cid = cot["id"]
        sc, r = self._post("/cotizaciones/%s/aceptar" % cid, self.recepcion)
        self.assertEqual(sc, 200, r)
        self.assertEqual(r["estado"], "aceptada")
        # SOLO cotizacionId (sin items) → la orden NACE de la cotización
        sc, orden = self._post("/ordenes", self.recepcion, {"cotizacionId": cid})
        self.assertEqual(sc, 200, orden)
        self.assertEqual(orden["cotizacionId"], cid)
        self.assertEqual(len(orden["lineas"]), 1)
        self.assertEqual(orden["lineas"][0]["cantidad"], 2.0)
        self.assertEqual(orden["lineas"][0]["precio"], 59.0)
        self.assertEqual(orden["total"], 118.0)   # 2 × 59 (precio CON IGV)
        # repetir sobre la misma cotización → 400 nombrando la orden ya abierta
        sc, err = self._post("/ordenes", self.recepcion, {"cotizacionId": cid})
        self.assertEqual(sc, 400, err)
        self.assertIn(orden["name"], err["message"])

    # ── reglas de negocio del adelanto ──────────────────────────────────────────
    def test_adelanto_debe_ser_parcial(self):
        self._abrir_caja(self.cajero)
        sc, orden = self._crear_orden(self.recepcion)
        sc, err = self._post("/ordenes/%s/adelanto" % orden["id"], self.cajero,
                             {"monto": 118, "medio": "Efectivo"})
        self.assertEqual(sc, 400)
        self.assertIn("PARCIAL", err["message"])

    def test_adelanto_exige_caja_abierta(self):
        # sin abrir caja
        sc, orden = self._crear_orden(self.recepcion)
        sc, err = self._post("/ordenes/%s/adelanto" % orden["id"], self.cajero,
                             {"monto": 50, "medio": "Efectivo"})
        self.assertEqual(sc, 400)
        self.assertIn("caja abierta", err["message"])

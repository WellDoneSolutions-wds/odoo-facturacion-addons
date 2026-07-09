import json
from datetime import datetime, timedelta

from odoo.tests import HttpCase, tagged


@tagged("post_install", "-at_install")
class TestCajaHttp(HttpCase):
    """QW07: las 6 rutas /ne/api/caja* (Bearer scoped key, with_user/with_company)."""

    def setUp(self):
        super().setUp()
        self.user = self.env["res.users"].create({
            "name": "Cajero HTTP", "login": "cajero_http_qw07",
            "group_ids": [(4, self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id)],
        })
        self.key = self.env["res.users.apikeys"].with_user(self.user)._generate(
            "l10n_pe_ne", "test-qw07", datetime.now() + timedelta(hours=12))

    def _h(self):
        return {"Authorization": "Bearer %s" % self.key, "Content-Type": "application/json"}

    def _post(self, path, body):
        r = self.url_open("/ne/api" + path, data=json.dumps(body).encode(),
                          headers=self._h(), method="POST")
        return r.status_code, (r.json() if r.content else {})

    def test_flujo(self):
        # sin caja abierta
        r = self.url_open("/ne/api/caja", headers=self._h())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"abierta": False, "sesion": None})
        # abrir
        sc, ses = self._post("/caja/abrir", {"saldoInicial": 150})
        self.assertEqual(sc, 200)
        self.assertEqual(ses["estado"], "abierta")
        # doble apertura -> 400 con mensaje amigable
        sc, err = self._post("/caja/abrir", {"saldoInicial": 10})
        self.assertEqual(sc, 400)
        self.assertIn("Ya hay una caja abierta", err["message"])
        # movimiento (retiro) -> sesion actualizada
        sc, ses = self._post("/caja/movimientos",
                             {"tipo": "retiro", "motivo": "Pago proveedor", "monto": 80})
        self.assertEqual(sc, 200)
        self.assertEqual(ses["retiros"], 80.0)
        # GET caja: ahora abierta
        r = self.url_open("/ne/api/caja", headers=self._h())
        self.assertTrue(r.json()["abierta"])
        self.assertEqual(r.json()["sesion"]["estado"], "abierta")
        # cerrar -> arqueo
        sc, arq = self._post("/caja/cerrar", {"conteos": [{"medio": "Efectivo", "contado": 70}]})
        self.assertEqual(sc, 200)
        self.assertEqual(arq["estado"], "cerrada")
        cid = arq["id"]
        # arqueo por id -> 200 con el snapshot congelado
        r = self.url_open("/ne/api/caja/%s/arqueo" % cid, headers=self._h())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], cid)
        # historial: la sesión cerrada aparece
        r = self.url_open("/ne/api/caja/historial", headers=self._h())
        self.assertEqual(r.status_code, 200)
        self.assertIn(cid, [x["id"] for x in r.json()])
        # 404 arqueo inexistente
        r = self.url_open("/ne/api/caja/999999/arqueo", headers=self._h())
        self.assertEqual(r.status_code, 404)

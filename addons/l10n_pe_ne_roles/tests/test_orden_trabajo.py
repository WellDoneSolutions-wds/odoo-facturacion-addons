from unittest.mock import patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.l10n_pe_ne_biller.tests.common import EnvioSincronoMixin

# Doble del POST al facturador: mismo contrato que el HTTP de CN-02 (text = XML firmado, sin CDR en
# headers). Con el camino SÍNCRONO fijado por EnvioSincronoMixin, la emisión mockeada deja el
# comprobante en 'enviado' — así podemos ejercer la Vía A (que EMITE) dentro de un TransactionCase.
_EMIT = "odoo.addons.l10n_pe_ne_biller.models.account_move_biller.requests.post"
_OK = type("R", (), {"status_code": 200, "text": '<?xml version="1.0"?><Invoice/>', "headers": {}})()


@tagged("post_install", "-at_install")
class TestOrdenTrabajo(TransactionCase):
    """CN-02: la orden de trabajo como flujo. Cola con TOMA atómica, adelanto a cuenta en caja
    (Vía B) que cuadra el arqueo por medio sin doble conteo, transiciones por rol y segregación.
    La emisión real del saldo (quick_emit -> microservicio) se valida en e2e; aquí, payload+guards."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.Orden = self.env["l10n_pe_ne.orden.trabajo"]
        self.Sesion = self.env["l10n_pe_ne.caja.sesion"]
        ruc_type = self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", "6")], limit=1)
        self.cliente = self.env["res.partner"].create({
            "name": "TALLER CLIENTE SAC", "vat": "20100070970",
            "l10n_latam_identification_type_id": ruc_type.id})
        self.producto = self.env["product.product"].create(
            {"name": "SERVICIO CN02", "default_code": "SVC02"})

    def _orden(self, precio=118.0, afecto=True, estado=None, adelanto=0.0):
        orden = self.Orden.create({
            "partner_id": self.cliente.id,
            "linea_ids": [(0, 0, {"product_id": self.producto.id, "descripcion": "SERVICIO CN02",
                                  "cantidad": 1.0, "precio_unitario": precio, "afecto_igv": afecto})],
        })
        vals = {}
        if adelanto:
            vals["adelanto_monto"] = adelanto
        if estado:
            vals["estado"] = estado
        if vals:
            orden.write(vals)
        return orden

    def _user(self, login, grupos):
        return self.env["res.users"].create({
            "name": login, "login": login,
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(4, self.env.ref("base.group_user").id)]
                         + [(4, self.env.ref(g).id) for g in grupos],
        })

    def _abrir_caja(self):
        self.Sesion.search([("estado", "=", "abierta")]).write({"estado": "cerrada"})
        return self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": 0})

    # ── nace en cola (sin dueño) ────────────────────────────────────────────────
    def test_crear_orden_nace_sin_dueno(self):
        orden = self.Orden.l10n_pe_ne_crear_orden({
            "clienteId": self.cliente.id,
            "items": [{"descripcion": "Cambio de aceite", "cantidad": 1, "precio": 118.0}]})
        rec = self.Orden.browse(orden["id"])
        self.assertEqual(rec.estado, "borrador")
        self.assertFalse(rec.user_id, "la orden nace SIN dueño (en cola)")
        self.assertEqual(rec.amount_total, 118.0)
        self.assertEqual(rec.saldo, 118.0)   # sin adelanto todavía

    # ── toma atómica de la cola ─────────────────────────────────────────────────
    def test_toma_atomica_gateada_por_taller(self):
        orden = self._orden(estado="encolada")
        self.assertFalse(orden.user_id)
        cajero = self._user("caj_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        with self.assertRaises(AccessError):
            orden.with_user(cajero).l10n_pe_ne_tomar()   # el cajero no toma órdenes
        taller = self._user("tal_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_taller"])
        orden.with_user(taller).l10n_pe_ne_tomar()
        self.assertEqual(orden.estado, "en_proceso")
        self.assertEqual(orden.user_id, taller, "toma atómica: quien la toma se la queda (NULL→yo)")

    def test_terminar_gateado_por_taller(self):
        orden = self._orden(estado="en_proceso")
        taller = self._user("tal2_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_taller"])
        orden.with_user(taller).l10n_pe_ne_terminar()
        self.assertEqual(orden.estado, "terminada")

    # ── adelanto a cuenta (Vía B) ───────────────────────────────────────────────
    def test_registrar_adelanto_encola_y_registra_en_caja(self):
        self._abrir_caja()
        orden = self._orden(precio=118.0)
        cajero = self._user("caj2_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        orden.with_user(cajero).l10n_pe_ne_registrar_adelanto(50.0, "Yape")
        self.assertEqual(orden.estado, "encolada")
        self.assertFalse(orden.user_id, "encolada = en cola, SIN dueño hasta que el taller la tome")
        self.assertEqual(orden.adelanto_monto, 50.0)
        self.assertEqual(orden.saldo, 68.0)   # 118 - 50
        mov = orden.adelanto_movimiento_id
        self.assertTrue(mov)
        self.assertEqual(mov.tipo, "adelanto")
        self.assertEqual(mov.medio, "Yape")
        self.assertEqual(mov.orden_trabajo_id, orden)

    def test_adelanto_debe_ser_parcial(self):
        self._abrir_caja()
        orden = self._orden(precio=118.0)
        cajero = self._user("caj3_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        with self.assertRaisesRegex(UserError, "PARCIAL"):
            orden.with_user(cajero).l10n_pe_ne_registrar_adelanto(118.0, "Efectivo")

    def test_adelanto_gateado_por_caja(self):
        self._abrir_caja()
        orden = self._orden(precio=118.0)
        taller = self._user("tal3_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_taller"])
        with self.assertRaises(AccessError):
            orden.with_user(taller).l10n_pe_ne_registrar_adelanto(50.0, "Efectivo")

    def test_adelanto_exige_caja_abierta(self):
        self.Sesion.search([("estado", "=", "abierta")]).write({"estado": "cerrada"})
        orden = self._orden(precio=118.0)
        cajero = self._user("caj4_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        with self.assertRaisesRegex(UserError, "caja abierta"):
            orden.with_user(cajero).l10n_pe_ne_registrar_adelanto(50.0, "Efectivo")

    # ── el adelanto cuadra el arqueo POR MEDIO, no como ingreso genérico ─────────
    def test_adelanto_entra_al_arqueo_por_su_medio(self):
        self._abrir_caja()
        orden = self._orden(precio=118.0)
        cajero = self._user("caj5_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        orden.with_user(cajero).l10n_pe_ne_registrar_adelanto(50.0, "Yape")
        sesion = self.Sesion.search([("estado", "=", "abierta")], limit=1)
        # El adelanto suma al esperado por SU medio (Yape), no a Efectivo.
        por_medio = sesion._l10n_pe_ne_por_medio_arqueo({"porMedio": {}})
        self.assertEqual(por_medio.get("Yape"), 50.0)
        # Y NO cuenta como ingreso genérico (que iría solo a Efectivo).
        ingresos, _retiros = sesion._l10n_pe_ne_ingresos_retiros()
        self.assertEqual(ingresos, 0.0)

    # ── cobro del saldo: guardas + regla dura ───────────────────────────────────
    def test_cobrar_saldo_solo_terminada(self):
        orden = self._orden(estado="encolada")
        cajero = self._user("caj6_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        with self.assertRaisesRegex(UserError, "TERMINADA"):
            orden.with_user(cajero).l10n_pe_ne_cobrar_saldo({})

    def test_cobrar_saldo_anti_doble(self):
        # una orden ya con comprobante final no se re-cobra (antes de tocar quick_emit).
        move = self.env["account.move"].create(
            {"move_type": "out_invoice", "partner_id": self.cliente.id})
        orden = self._orden(estado="terminada")
        orden.factura_final_id = move.id
        cajero = self._user("caj7_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        with self.assertRaisesRegex(UserError, "ya se cobró"):
            orden.with_user(cajero).l10n_pe_ne_cobrar_saldo({})

    def test_cobrar_saldo_gateado_por_caja(self):
        orden = self._orden(estado="terminada")
        taller = self._user("tal4_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_taller"])
        with self.assertRaises(AccessError):
            orden.with_user(taller).l10n_pe_ne_cobrar_saldo({})

    def test_cobrar_saldo_saldo_invalido(self):
        # A6 (revisión Fable): si el adelanto iguala/supera el total (saldo<=0), cobrar_saldo lo
        # rechaza ANTES de emitir con medios negativos (una fila que desaparecería del arqueo).
        orden = self._orden(precio=118.0, estado="terminada", adelanto=118.0)   # saldo = 0
        cajero = self._user("caj_saldoinv_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        with self.assertRaisesRegex(UserError, "frente al total"):
            orden.with_user(cajero).l10n_pe_ne_cobrar_saldo({})

    def test_no_entregada_a_mano(self):
        orden = self._orden(estado="terminada")
        with self.assertRaisesRegex(UserError, "No se puede pasar"):
            orden._avanzar("entregada")   # 'entregada' no es arista: solo por el fold de cobro

    def test_entregada_sin_comprobante_bloqueada_a_nivel_modelo(self):
        # cierra el hueco de un write RPC directo que salte cobrar_saldo: 'entregada' exige factura.
        # con el flag (simula un método interno erróneo) el write() guard no aplica -> salta la constraint.
        orden = self._orden(estado="terminada")
        with self.assertRaises(ValidationError):
            orden.with_context(l10n_pe_ne_flujo_ok=True).write({"estado": "entregada"})

    # ── blindaje de la máquina de estados (iter 7) ──────────────────────────────
    def test_estado_no_por_write_rpc_directo(self):
        # un usuario real NO cambia estado por un write directo (se saltaría _avanzar: grupo, toma,
        # guarda, gate, reglas duras). Solo por las acciones del flujo.
        orden = self._orden()  # borrador
        cajero = self._user("caj_blind_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        with self.assertRaisesRegex(UserError, "no escribiéndolo directamente"):
            orden.with_user(cajero).write({"estado": "terminada"})

    def test_dinero_no_por_write_rpc_directo(self):
        # A7: el registro del cobro (adelanto/factura) no se reescribe por fuera de las acciones.
        orden = self._orden(estado="terminada", adelanto=50.0)
        cajero = self._user("caj_dinero_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        with self.assertRaisesRegex(UserError, "no escribiéndolo directamente"):
            orden.with_user(cajero).write({"adelanto_monto": 1.0})
        move = self.env["account.move"].create(
            {"move_type": "out_invoice", "partner_id": self.cliente.id})
        with self.assertRaisesRegex(UserError, "no escribiéndolo directamente"):
            orden.with_user(cajero).write({"factura_final_id": move.id})

    def test_lineas_congeladas_fuera_de_borrador(self):
        # A7: el detalle se edita solo en borrador; después, cambiar el precio divergiría el saldo
        # del adelanto cobrado (o del comprobante emitido).
        orden = self._orden(estado="terminada")
        cajero = self._user("caj_linea_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        with self.assertRaisesRegex(UserError, "ya no se edita"):
            orden.linea_ids[0].with_user(cajero).write({"precio_unitario": 999.0})
        with self.assertRaisesRegex(UserError, "ya no se edita"):
            orden.linea_ids[0].with_user(cajero).unlink()
        # en borrador SÍ se edita (la orden aún no tiene dinero encima)
        o2 = self._orden()
        o2.linea_ids[0].with_user(cajero).write({"precio_unitario": 99.0})
        self.assertEqual(o2.linea_ids[0].precio_unitario, 99.0)

    def test_estado_no_por_create_rpc_directo(self):
        # un documento nuevo NACE en su estado inicial; no se crea directamente en uno avanzado.
        cajero = self._user("caj_crea_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        with self.assertRaisesRegex(UserError, "nace en su estado inicial"):
            self.Orden.with_user(cajero).create({
                "partner_id": self.cliente.id, "estado": "terminada",
                "linea_ids": [(0, 0, {"descripcion": "x", "cantidad": 1.0, "precio_unitario": 10.0})],
            })

    # ── payload de emisión: IGV + medios = SOLO el saldo (no re-cuenta el adelanto) ──
    def test_payload_convierte_igv(self):
        orden = self._orden(precio=118.0, afecto=True)
        payload = orden._l10n_pe_ne_payload_emision()
        self.assertEqual(payload["tipoDoc"], "01")   # RUC 11 díg -> factura
        linea = payload["lineas"][0]
        self.assertAlmostEqual(linea["precioUnitario"], 100.0, places=2)   # 118/1.18
        self.assertEqual(linea["taxCode"], "1000")

    def test_payload_medios_es_solo_el_saldo(self):
        # con adelanto de 50 sobre 118, el comprobante final se emite por el TOTAL pero sus
        # 'medios' registran solo el saldo (68) -> la sesión del recojo no re-cuenta el adelanto.
        orden = self._orden(precio=118.0, adelanto=50.0)
        self.assertEqual(orden.saldo, 68.0)
        medios = orden._l10n_pe_ne_payload_emision()["formaPago"]["medios"]
        self.assertEqual(len(medios), 1)
        self.assertEqual(medios[0]["monto"], 68.0)

    # ── lote menores (A14/A16/A17) ──────────────────────────────────────────────
    def test_anular_en_proceso(self):
        # A17: cancelar con el trabajo en curso existe (supervisor + motivo); el operario no puede.
        orden = self._orden(estado="en_proceso")
        taller = self._user("tal_anula_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_taller"])
        with self.assertRaises(AccessError):
            orden.with_user(taller).l10n_pe_ne_anular("se retiró")
        sup = self._user("sup_anula_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_supervisor"])
        orden.with_user(sup).l10n_pe_ne_anular("cliente desistió")
        self.assertEqual(orden.estado, "anulada")

    def test_crear_orden_cotizacion_invalida(self):
        # A16: un cotizacionId inexistente no se siembra crudo — queda False, sin crash.
        r = self.Orden.l10n_pe_ne_crear_orden({
            "clienteId": self.cliente.id, "cotizacionId": 999999,
            "items": [{"descripcion": "Trabajo x", "cantidad": 1, "precio": 10}]})
        self.assertFalse(self.Orden.browse(r["id"]).cotizacion_id)

    def test_ticket_menciona_adelanto(self):
        # A14: el ticket del comprobante final explica el cobro en dos tiempos.
        orden = self._orden(estado="terminada", adelanto=50.0)
        orden.medio_adelanto = "Yape"
        move = self.env["account.move"].create(
            {"move_type": "out_invoice", "partner_id": self.cliente.id})
        orden.factura_final_id = move.id
        self.assertIn("Adelanto a cuenta: S/ 50.00 (Yape)", move._l10n_pe_ne_ticket_adicional())

    def test_cola_adelanto(self):
        # Hallazgo del e2e segregado: el cajero necesita SU bandeja de borradores por cobrar
        # (sin ella, una orden creada por recepción era invisible en la UI del cajero).
        o_borr = self._orden()                      # borrador → en la cola
        o_enc = self._orden(estado="encolada")      # ya adelantada → fuera
        cola = self.Orden.l10n_pe_ne_cola_adelanto()
        ids = [i["id"] for i in cola["items"]]
        self.assertIn(o_borr.id, ids)
        self.assertNotIn(o_enc.id, ids)

    # ── colas + segregación por ir.rule ─────────────────────────────────────────
    def test_colas_y_segregacion(self):
        o_enc = self._orden(estado="encolada")
        o_proc = self._orden(estado="en_proceso")
        o_term = self._orden(estado="terminada")
        self._orden(estado="borrador")
        # cola del taller = encolada + en_proceso
        cola = self.Orden.l10n_pe_ne_cola_ordenes()
        ids_cola = [i["id"] for i in cola["items"]]
        self.assertIn(o_enc.id, ids_cola)
        self.assertIn(o_proc.id, ids_cola)
        self.assertNotIn(o_term.id, ids_cola)
        # cola de saldo = terminada sin factura
        saldo = self.Orden.l10n_pe_ne_cola_saldo()
        self.assertIn(o_term.id, [i["id"] for i in saldo["items"]])
        # ir.rule: un operario puro NO ve borradores (solo su pipeline)
        taller = self._user("tal5_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_taller"])
        vis_taller = set(self.Orden.with_user(taller).search([]).mapped("estado"))
        self.assertNotIn("borrador", vis_taller)
        self.assertTrue(vis_taller <= {"encolada", "en_proceso", "terminada"})
        # ir.rule: un cajero puro NO ve el trabajo en curso del taller (en_proceso)
        cajero = self._user("caj8_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        vis_caja = set(self.Orden.with_user(cajero).search([]).mapped("estado"))
        self.assertNotIn("en_proceso", vis_caja)

    # ── PUENTE cotización → orden de taller ─────────────────────────────────────
    def _cotizacion(self, estado="aceptada", partner=None, lineas=None):
        """Cotización de ORIGEN para el puente. Su cliente es OTRO (distinto al de la orden) por
        defecto —así se puede verificar que la orden hereda el partner de la cotización— y trae dos
        líneas (una gravada, una no) para cubrir la copia de afecto_igv. Se fija el estado por write
        de sistema (su): es un fixture, no ejerce el eje grupo del flujo (eso lo prueba CN-01)."""
        partner = partner or self.env["res.partner"].create({"name": "CLIENTE COTIZA SAC"})
        if lineas is None:
            lineas = [
                (0, 0, {"product_id": self.producto.id, "descripcion": "Cambio de aceite",
                        "cantidad": 2.0, "precio_unitario": 59.0, "afecto_igv": True}),
                (0, 0, {"descripcion": "Mano de obra exonerada", "cantidad": 1.0,
                        "precio_unitario": 40.0, "afecto_igv": False}),
            ]
        cot = self.env["l10n_pe_ne.cotizacion"].create(
            {"partner_id": partner.id, "line_ids": lineas})
        if estado:
            cot.write({"estado": estado})
        return cot

    def test_puente_copia_lineas_partner_y_total(self):
        # aceptada + SIN items → la orden NACE de la cotización: copia las líneas (mismo número,
        # descripción, cantidad, precio CON IGV y afecto_igv), hereda el partner y calza el total.
        cot = self._cotizacion(estado="aceptada")
        r = self.Orden.l10n_pe_ne_crear_orden({"cotizacionId": cot.id})
        orden = self.Orden.browse(r["id"])
        self.assertEqual(orden.cotizacion_id, cot)
        self.assertEqual(orden.partner_id, cot.partner_id)
        self.assertNotEqual(orden.partner_id, self.cliente, "el partner sale de la cotización")
        self.assertEqual(len(orden.linea_ids), len(cot.line_ids))
        por_desc = {l.descripcion: l for l in orden.linea_ids}
        for cl in cot.line_ids:
            ol = por_desc[cl.descripcion]
            self.assertEqual(ol.cantidad, cl.cantidad)
            self.assertEqual(ol.precio_unitario, cl.precio_unitario)   # mismo precio CON IGV
            self.assertEqual(ol.afecto_igv, cl.afecto_igv)
        self.assertEqual(orden.amount_total, cot.amount_total)

    def test_puente_exige_cotizacion_aceptada(self):
        # borrador y convertida NO abren orden: sin acuerdo cerrado / ya vendida por mostrador
        # (doble venta). El mensaje nombra el estado real por honestidad.
        cot_borr = self._cotizacion(estado="borrador")
        with self.assertRaisesRegex(UserError, "ACEPTADA"):
            self.Orden.l10n_pe_ne_crear_orden({"cotizacionId": cot_borr.id})
        cot_conv = self._cotizacion(estado="convertida")
        with self.assertRaisesRegex(UserError, "ACEPTADA"):
            self.Orden.l10n_pe_ne_crear_orden({"cotizacionId": cot_conv.id})

    def test_puente_una_orden_por_cotizacion(self):
        # una cotización aceptada abre UNA orden; el segundo intento nombra la primera. Tras ANULAR
        # la primera (ya no la referencia una orden viva), el puente vuelve a abrir.
        cot = self._cotizacion(estado="aceptada")
        r1 = self.Orden.l10n_pe_ne_crear_orden({"cotizacionId": cot.id})
        orden1 = self.Orden.browse(r1["id"])
        with self.assertRaises(UserError) as cm:
            self.Orden.l10n_pe_ne_crear_orden({"cotizacionId": cot.id})
        self.assertIn(orden1.name, str(cm.exception))
        cajero = self._user("caj_puente_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        orden1.with_user(cajero).l10n_pe_ne_anular("cliente desistió")
        r2 = self.Orden.l10n_pe_ne_crear_orden({"cotizacionId": cot.id})
        orden2 = self.Orden.browse(r2["id"])
        self.assertNotEqual(orden2.id, orden1.id)
        self.assertEqual(orden2.cotizacion_id, cot)

    def test_puente_items_explicitos_mandan(self):
        # con items en el payload, los ítems GANAN (nº de líneas = las pasadas); la cotización queda
        # solo como referencia trazable y el partner sale del clienteId, no de la cotización.
        cot = self._cotizacion(estado="aceptada")   # trae 2 líneas
        r = self.Orden.l10n_pe_ne_crear_orden({
            "clienteId": self.cliente.id, "cotizacionId": cot.id,
            "items": [{"descripcion": "Diagnóstico express", "cantidad": 1, "precio": 30.0}]})
        orden = self.Orden.browse(r["id"])
        self.assertEqual(len(orden.linea_ids), 1, "mandan los items, no las 2 líneas de la cotización")
        self.assertEqual(orden.linea_ids.descripcion, "Diagnóstico express")
        self.assertEqual(orden.cotizacion_id, cot, "la referencia trazable queda")
        self.assertEqual(orden.partner_id, self.cliente)

    def test_cotizacion_expone_orden_creada(self):
        # el dict de la cotización expone la orden de taller que abrió (para que la SPA navegue y no
        # vuelva a ofrecer "crear orden"). Vacío mientras no exista.
        cot = self._cotizacion(estado="aceptada")
        d0 = cot._l10n_pe_ne_cotizacion_dict()
        self.assertIsNone(d0["ordenTrabajoId"])
        self.assertEqual(d0["ordenTrabajoName"], "")
        r = self.Orden.l10n_pe_ne_crear_orden({"cotizacionId": cot.id})
        orden = self.Orden.browse(r["id"])
        d1 = cot._l10n_pe_ne_cotizacion_dict()
        self.assertEqual(d1["ordenTrabajoId"], orden.id)
        self.assertEqual(d1["ordenTrabajoName"], orden.name)

    # ── FIFO de las colas ───────────────────────────────────────────────────────
    def test_cola_ordenes_fifo_por_fecha_encolada(self):
        # FIFO por LLEGADA a la cola (fecha_encolada), no por id: X se creó ANTES que Y (id menor)
        # pero Y se adelantó primero → Y debe salir primero. Datetime.now() trunca a segundos y dos
        # adelantos del test caerían en el mismo segundo; se fijan fechas distintas para probar el
        # ORDER BY, no el reloj (el estampado real lo cubre test_fecha_encolada_se_estampa).
        self._abrir_caja()
        cajero = self._user("caj_fifo_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        x = self._orden(precio=118.0)
        y = self._orden(precio=118.0)
        self.assertLess(x.id, y.id, "X se creó antes (id menor)")
        y.with_user(cajero).l10n_pe_ne_registrar_adelanto(50.0, "Efectivo")   # Y llega primero
        x.with_user(cajero).l10n_pe_ne_registrar_adelanto(50.0, "Efectivo")
        y.sudo().write({"fecha_encolada": "2026-07-01 08:00:00"})
        x.sudo().write({"fecha_encolada": "2026-07-01 09:00:00"})
        cola = self.Orden.l10n_pe_ne_cola_ordenes()
        ids = [i["id"] for i in cola["items"] if i["id"] in (x.id, y.id)]
        self.assertEqual(ids, [y.id, x.id], "fecha_encolada manda, no el id")

    def test_fecha_encolada_se_estampa(self):
        # la fecha de encolado se estampa al ADELANTAR (no al crear el borrador) y sale en el dict.
        self._abrir_caja()
        orden = self._orden(precio=118.0)
        self.assertFalse(orden.fecha_encolada, "borrador: aún sin turno de cola")
        self.assertEqual(orden._l10n_pe_ne_orden_dict()["fechaEncolada"], "")
        cajero = self._user("caj_enc_ot", ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])
        orden.with_user(cajero).l10n_pe_ne_registrar_adelanto(50.0, "Efectivo")
        self.assertTrue(orden.fecha_encolada, "el adelanto estampa la llegada a la cola")
        self.assertTrue(orden._l10n_pe_ne_orden_dict()["fechaEncolada"])

    def test_cola_adelanto_fifo_por_id(self):
        # cola de cobro del adelanto: FIFO por registro (id asc) — la que entró antes sale primero.
        a = self._orden()   # borrador
        b = self._orden()   # borrador
        cola = self.Orden.l10n_pe_ne_cola_adelanto()
        ids = [i["id"] for i in cola["items"] if i["id"] in (a.id, b.id)]
        self.assertEqual(ids, [a.id, b.id])


@tagged("post_install", "-at_install")
class TestOrdenTrabajoViaA(EnvioSincronoMixin, TransactionCase):
    """CN-02 · Vía A (anticipo FACTURADO ante SUNAT). Con company.l10n_pe_ne_adelanto_facturado ON,
    cada adelanto EMITE su propio comprobante gravado; el final lo referencia y lo descuenta (anticipo
    04 + relacionados + sumTotalAnticipos), y el arqueo NO re-cuenta ese adelanto por su medio (esa
    plata ya entra por los medios del comprobante). La emisión al facturador se dobla con _EMIT (mismo
    contrato que el HttpCase); EnvioSincronoMixin fija el camino síncrono para que el mock aterrice.

    Clase aparte de TestOrdenTrabajo (no la subclasea) para no re-correr la batería de Vía B y para
    traer el mixin de envío solo donde se EMITE."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.company.l10n_pe_ne_adelanto_facturado = True   # Vía A encendida para toda la clase
        self.Orden = self.env["l10n_pe_ne.orden.trabajo"]
        self.Sesion = self.env["l10n_pe_ne.caja.sesion"]
        ruc_type = self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", "6")], limit=1)
        self.cliente = self.env["res.partner"].create({
            "name": "TALLER VIA A SAC", "vat": "20100070970",
            "l10n_latam_identification_type_id": ruc_type.id})
        self.producto = self.env["product.product"].create(
            {"name": "SERVICIO VIA A", "default_code": "SVCA"})

    # ── fixtures (mismos que TestOrdenTrabajo; replicados para no acoplar la batería) ──
    def _orden(self, precio=118.0, afecto=True):
        return self.Orden.create({
            "partner_id": self.cliente.id,
            "linea_ids": [(0, 0, {"product_id": self.producto.id, "descripcion": "SERVICIO VIA A",
                                  "cantidad": 1.0, "precio_unitario": precio, "afecto_igv": afecto})],
        })

    def _user(self, login, grupos):
        return self.env["res.users"].create({
            "name": login, "login": login,
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(4, self.env.ref("base.group_user").id)]
                         + [(4, self.env.ref(g).id) for g in grupos],
        })

    def _cajero(self, login):
        return self._user(login, ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])

    def _abrir_caja(self):
        self.Sesion.search([("estado", "=", "abierta")]).write({"estado": "cerrada"})
        return self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": 0})

    def _adelanto_emitido(self, cajero, precio=118.0, monto=50.0, medio="Yape"):
        """Orden con su adelanto ya cobrado + FACTURADO (anticipo emitido con el mock)."""
        orden = self._orden(precio=precio)
        with patch(_EMIT, return_value=_OK):
            orden.with_user(cajero).l10n_pe_ne_registrar_adelanto(monto, medio)
        return orden

    # ── 1) registrar_adelanto EMITE el anticipo y encola ────────────────────────
    def test_via_a_registrar_adelanto_emite(self):
        self._abrir_caja()
        cajero = self._cajero("caj_va1")
        orden = self._adelanto_emitido(cajero)
        # la orden quedó encolada y el adelanto registrado
        self.assertEqual(orden.estado, "encolada")
        self.assertEqual(orden.adelanto_monto, 50.0)
        self.assertEqual(orden.saldo, 68.0)
        # Vía A: se EMITIÓ el comprobante del anticipo (no es Vía B)
        ant = orden.anticipo_factura_id
        self.assertTrue(ant, "Vía A: el adelanto emite su propio comprobante")
        self.assertEqual(ant.l10n_pe_biller_state, "enviado")   # el mock lo deja enviado
        self.assertEqual(ant.move_type, "out_invoice")
        # Modelo formal del biller: el doc del anticipo queda MARCADO como doc. A (es_anticipo) —
        # entra a "anticipos pendientes" del cliente y lleva saldo aplicado/disponible.
        self.assertTrue(ant.l10n_pe_ne_es_anticipo)
        self.assertEqual(ant.l10n_pe_ne_anticipo_saldo, ant.amount_total)   # aún sin regularizar
        # el movimiento de caja sigue ligado a la orden (aunque el arqueo lo salte, la traza queda)
        mov = orden.adelanto_movimiento_id
        self.assertTrue(mov)
        self.assertEqual(mov.tipo, "adelanto")
        self.assertEqual(mov.orden_trabajo_id, orden)

    # ── 2) arqueo: el seam NO re-cuenta el adelanto facturado ────────────────────
    def test_via_a_arqueo_sin_doble_conteo(self):
        self._abrir_caja()
        cajero = self._cajero("caj_va2")
        self._adelanto_emitido(cajero, medio="Yape")
        sesion = self.Sesion.search([("estado", "=", "abierta")], limit=1)
        # Se pasa un por-medio base con OTRO medio ya presente: el seam debe devolverlo TAL CUAL,
        # sin sumar el adelanto (esa plata ya entra por los medios del comprobante del anticipo).
        base = {"porMedio": {"Efectivo": 68.0}}
        por_medio = sesion._l10n_pe_ne_por_medio_arqueo(base)
        self.assertEqual(por_medio, {"Efectivo": 68.0})
        self.assertNotIn("Yape", por_medio)   # el adelanto facturado NO infla el arqueo por su medio

    # ── 3) cobrar_saldo: el final referencia y descuenta el anticipo ─────────────
    def test_via_a_cobrar_saldo_referencia_anticipo(self):
        self._abrir_caja()
        cajero = self._cajero("caj_va3")
        orden = self._adelanto_emitido(cajero)
        ant = orden.anticipo_factura_id
        orden.write({"estado": "terminada"})   # su: saltar el tramo del taller, probamos el cobro
        with patch(_EMIT, return_value=_OK):
            orden.with_user(cajero).l10n_pe_ne_cobrar_saldo({"medio": "Efectivo"})
        self.assertEqual(orden.estado, "entregada")
        final = orden.factura_final_id
        self.assertTrue(final)
        # el final trae el anticipo con su total y su doc en formato SERIE-00000000, derivado del
        # comprobante del anticipo REALMENTE emitido.
        self.assertEqual(final.l10n_pe_ne_anticipo_total, orden.adelanto_monto)
        doc_esperado = "%s-%s" % (ant.l10n_pe_ne_serie_emit, (ant.l10n_pe_ne_corr_emit or "").zfill(8))
        self.assertEqual(final.l10n_pe_ne_anticipo_doc, doc_esperado)
        self.assertRegex(final.l10n_pe_ne_anticipo_doc, r"^F\d{3}-\d{8}$")   # factura → serie F
        self.assertEqual(final.l10n_pe_ne_anticipo_tipo, "02")              # RUC → factura (cat. 12)
        # Modelo formal: la regularización ENLAZA el doc. A (origen) y consume su saldo — la
        # validación del biller impediría aplicar este anticipo otra vez.
        self.assertEqual(final.l10n_pe_ne_anticipo_origen_id, ant)
        self.assertEqual(ant.l10n_pe_ne_anticipo_aplicado, orden.adelanto_monto)
        self.assertEqual(ant.l10n_pe_ne_anticipo_saldo, 0.0)
        # contrato del biller (test_anticipo): el XML descuenta el anticipo y lo informa.
        req = final._l10n_pe_build_invoice_request()
        self.assertEqual(req["cabecera"]["sumTotalAnticipos"], "50.00")
        vg = [v for v in req["variablesGlobales"] if v["codTipoVariableGlobal"] == "04"]
        self.assertEqual(len(vg), 1)                       # descuento global por anticipo presente
        rel = req["relacionados"][0]
        self.assertEqual(rel["numDocRelacionado"], doc_esperado)
        self.assertEqual(rel["tipDocRelacionado"], "02")
        self.assertEqual(rel["mtoDocRelacionado"], "50.00")

    # ── 4) guarda: no se cobra el saldo con el anticipo en 'error' ───────────────
    def test_via_a_cobrar_saldo_bloquea_anticipo_en_error(self):
        self._abrir_caja()
        cajero = self._cajero("caj_va4")
        orden = self._adelanto_emitido(cajero)
        orden.write({"estado": "terminada"})
        # el comprobante del anticipo quedó en error: el final NO puede referenciar un doc que SUNAT
        # no reconoce. Se fuerza el estado con sudo (simula un rechazo del facturador).
        orden.anticipo_factura_id.sudo().l10n_pe_biller_state = "error"
        with self.assertRaisesRegex(UserError, "anticipo"):
            orden.with_user(cajero).l10n_pe_ne_cobrar_saldo({"medio": "Efectivo"})

    # ── 5) anular: bloqueada con anticipo vivo; permitida si el anticipo ya se anuló ──
    def test_via_a_anular_bloqueada_con_anticipo_vivo(self):
        self._abrir_caja()
        cajero = self._cajero("caj_va5")
        orden = self._adelanto_emitido(cajero)
        ant = orden.anticipo_factura_id
        sup = self._user("sup_va5", ["l10n_pe_ne_roles.group_l10n_pe_ne_supervisor"])
        # anticipo 'enviado' (vivo): anular la orden lo dejaría sin regularizar → UserError con el número
        with self.assertRaises(UserError) as cm:
            orden.with_user(sup).l10n_pe_ne_anular("cliente desistió")
        self.assertIn(ant.name, str(cm.exception))
        self.assertEqual(orden.estado, "encolada")   # no avanzó
        # con el anticipo ya ANULADO (su), la anulación de la orden sí procede
        ant.sudo().l10n_pe_biller_state = "anulado"
        orden.with_user(sup).l10n_pe_ne_anular("cliente desistió")
        self.assertEqual(orden.estado, "anulada")

    # ── 6) Vía B intacta con el switch APAGADO ──────────────────────────────────
    def test_via_b_intacta_con_switch_apagado(self):
        self.company.l10n_pe_ne_adelanto_facturado = False   # apagar: recibo interno, sin emisión
        self._abrir_caja()
        cajero = self._cajero("caj_vb6")
        orden = self._orden(precio=118.0)
        # sin patch: Vía B NO llama al facturador
        orden.with_user(cajero).l10n_pe_ne_registrar_adelanto(50.0, "Yape")
        self.assertEqual(orden.estado, "encolada")
        self.assertFalse(orden.anticipo_factura_id, "Vía B: no se emite comprobante de anticipo")
        # y el seam del arqueo SÍ suma el adelanto por su medio (el comportamiento base de CN-02)
        sesion = self.Sesion.search([("estado", "=", "abierta")], limit=1)
        por_medio = sesion._l10n_pe_ne_por_medio_arqueo({"porMedio": {}})
        self.assertEqual(por_medio.get("Yape"), 50.0)

    # ── 7) política set_adelanto_facturado: supervisor OK, cajero AccessError ─────
    def test_set_adelanto_facturado_gateado_por_supervisor(self):
        self.company.l10n_pe_ne_adelanto_facturado = False
        Company = self.env["res.company"]
        sup = self._user("sup_va7", ["l10n_pe_ne_roles.group_l10n_pe_ne_supervisor"])
        pol = Company.with_user(sup).l10n_pe_ne_set_adelanto_facturado(True)
        self.assertTrue(pol["adelantoFacturado"])                    # el dict de políticas lo refleja
        self.assertTrue(self.company.l10n_pe_ne_adelanto_facturado)  # y quedó persistido
        # un cajero NO cambia políticas de control
        cajero = self._cajero("caj_va7")
        with self.assertRaises(AccessError):
            Company.with_user(cajero).l10n_pe_ne_set_adelanto_facturado(False)


@tagged("post_install", "-at_install")
class TestOrdenTrabajoReserva(EnvioSincronoMixin, TransactionCase):
    """CN-02 · RESERVA (layaway/apartado). El MISMO modelo de flujo con tipo='reserva': un producto YA
    TERMINADO que el cliente APARTA con N abonos a cuenta y recoge al completar el pago. SIN cola de
    taller ni operario (no hay trabajo que hacer). Hereda del taller el arqueo por medio, el cobro en
    dos tiempos y el blindaje del dinero; cambian sus transiciones (borrador→reservada la escribe el
    PRIMER abono; como aristas solo quedan las anulaciones) y el registro N-abonos.

    RECIBO INTERNO SIEMPRE (Vía B): el abono no emite comprobante ni con la Vía A encendida (el biller
    referencia UN doc de anticipo por final, y una reserva lleva N abonos). Al recoger, cobrar_saldo
    emite UN comprobante por el total con medios=saldo. Esa emisión se dobla con _EMIT + el mixin
    síncrono; la reserva vive ENTERA en la caja, por eso el cajero segregado (solo grupo caja) debe
    poder recorrerla completa —lo que ejerce esta batería con with_user(cajero)."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.Orden = self.env["l10n_pe_ne.orden.trabajo"]
        self.Sesion = self.env["l10n_pe_ne.caja.sesion"]
        ruc_type = self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", "6")], limit=1)
        self.cliente = self.env["res.partner"].create({
            "name": "RESERVA CLIENTE SAC", "vat": "20100070970",
            "l10n_latam_identification_type_id": ruc_type.id})
        self.producto = self.env["product.product"].create(
            {"name": "PRODUCTO RESERVA", "default_code": "RSV01"})

    # ── fixtures ────────────────────────────────────────────────────────────────
    def _reserva(self, precio=118.0, afecto=True, estado=None, adelanto=0.0):
        # env.su en el TransactionCase: se puede sembrar 'tipo' (blindado, _campos_flujo) y forzar
        # estado/adelanto como fixture (lo mismo que hace _orden en TestOrdenTrabajo con el taller).
        orden = self.Orden.create({
            "partner_id": self.cliente.id, "tipo": "reserva",
            "linea_ids": [(0, 0, {"product_id": self.producto.id, "descripcion": "PRODUCTO RESERVA",
                                  "cantidad": 1.0, "precio_unitario": precio, "afecto_igv": afecto})],
        })
        vals = {}
        if adelanto:
            vals["adelanto_monto"] = adelanto
        if estado:
            vals["estado"] = estado
        if vals:
            orden.write(vals)
        return orden

    def _taller(self, precio=118.0, estado=None):
        orden = self.Orden.create({
            "partner_id": self.cliente.id,
            "linea_ids": [(0, 0, {"product_id": self.producto.id, "descripcion": "SERVICIO TALLER",
                                  "cantidad": 1.0, "precio_unitario": precio, "afecto_igv": True})],
        })
        if estado:
            orden.write({"estado": estado})
        return orden

    def _user(self, login, grupos):
        return self.env["res.users"].create({
            "name": login, "login": login,
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(4, self.env.ref("base.group_user").id)]
                         + [(4, self.env.ref(g).id) for g in grupos],
        })

    def _cajero(self, login):
        return self._user(login, ["l10n_pe_ne_roles.group_l10n_pe_ne_caja"])

    def _abrir_caja(self):
        self.Sesion.search([("estado", "=", "abierta")]).write({"estado": "cerrada"})
        return self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": 0})

    # ── 1) camino feliz: apartar con N abonos y recoger ─────────────────────────
    def test_camino_feliz_reserva(self):
        self._abrir_caja()
        cajero = self._cajero("caj_rsv1")
        # nace por el MISMO endpoint que el taller, con tipo='reserva' (se valida y queda inmutable).
        r = self.Orden.l10n_pe_ne_crear_orden({
            "clienteId": self.cliente.id, "tipo": "reserva",
            "items": [{"productId": self.producto.id, "descripcion": "Licuadora Oster",
                       "cantidad": 1, "precio": 118.0, "afectoIgv": True}]})
        orden = self.Orden.browse(r["id"])
        self.assertEqual(orden.tipo, "reserva")
        self.assertEqual(orden.estado, "borrador")
        self.assertEqual(orden.amount_total, 118.0)
        self.assertFalse(orden.fecha_encolada, "borrador: aún sin turno de la bandeja de reservas")
        # 1er abono: encola la reserva (borrador→reservada) y estampa su llegada.
        d1 = orden.with_user(cajero).l10n_pe_ne_registrar_abono(30.0, "Yape")
        self.assertEqual(orden.estado, "reservada")
        self.assertEqual(orden.adelanto_monto, 30.0)
        self.assertTrue(orden.fecha_encolada, "el 1er abono estampa la llegada a la bandeja")
        self.assertEqual(len(d1["abonos"]), 1)
        # 2o abono: solo suma; sigue reservada, la llegada NO se re-estampa.
        primera_llegada = orden.fecha_encolada
        d2 = orden.with_user(cajero).l10n_pe_ne_registrar_abono(50.0, "Efectivo")
        self.assertEqual(orden.estado, "reservada")
        self.assertEqual(orden.adelanto_monto, 80.0)
        self.assertEqual(orden.saldo, 38.0)                       # 118 - 80
        self.assertEqual(orden.fecha_encolada, primera_llegada, "el turno se toma al apartar")
        self.assertEqual(len(d2["abonos"]), 2, "historial con los 2 abonos a cuenta")
        # recoge: el cajero cobra el saldo y entrega (emisión doblada). El final se emite por el total,
        # con medios=SALDO (el adelanto acumulado no se re-cuenta entre sesiones).
        with patch(_EMIT, return_value=_OK):
            res = orden.with_user(cajero).l10n_pe_ne_cobrar_saldo({"medio": "Efectivo"})
        self.assertEqual(orden.estado, "entregada")
        self.assertTrue(orden.factura_final_id, "el recojo emite el comprobante final")
        self.assertEqual(res["comprobanteId"], orden.factura_final_id.id)
        self.assertEqual(res["saldoCobrado"], 38.0)
        self.assertEqual(res["saldoCobrado"], res["total"] - 80.0)

    # ── 2) guardas del abono ────────────────────────────────────────────────────
    def test_abono_monto_cero(self):
        self._abrir_caja()
        cajero = self._cajero("caj_rsv2")
        reserva = self._reserva()
        with self.assertRaisesRegex(UserError, "mayor a 0"):
            reserva.with_user(cajero).l10n_pe_ne_registrar_abono(0, "Efectivo")

    def test_abono_no_completa_el_total(self):
        # ESTRICTO: un abono jamás iguala/supera el total — el ÚLTIMO pago es el SALDO al recoger y lo
        # emite cobrar_saldo (si un abono cerrara el total, el final saldría con medios=0). Redirige.
        self._abrir_caja()
        cajero = self._cajero("caj_rsv3")
        reserva = self._reserva(precio=118.0)
        with self.assertRaisesRegex(UserError, "SALDO"):
            reserva.with_user(cajero).l10n_pe_ne_registrar_abono(118.0, "Efectivo")

    def test_abono_solo_sobre_reserva(self):
        # una orden de TALLER usa el adelanto único, no abonos (mensaje honesto).
        cajero = self._cajero("caj_rsv4")
        taller = self._taller()
        with self.assertRaisesRegex(UserError, "reservas"):
            taller.with_user(cajero).l10n_pe_ne_registrar_abono(30.0, "Efectivo")

    def test_abono_gateado_por_caja(self):
        # el operario (taller) NO cobra abonos: eje 2 (grupo caja) antes de tocar nada.
        reserva = self._reserva()
        operario = self._user("ope_rsv4", ["l10n_pe_ne_roles.group_l10n_pe_ne_taller"])
        with self.assertRaises(AccessError):
            reserva.with_user(operario).l10n_pe_ne_registrar_abono(30.0, "Efectivo")

    # ── 3) la reserva NO pisa el taller ─────────────────────────────────────────
    def test_reserva_no_pasa_por_el_taller(self):
        # tomar rebota sobre una reserva: esa arista no existe en tipo='reserva' (error de transición,
        # no de permiso — se prueba como superusuario para aislar la AUSENCIA de la arista).
        reservada = self._reserva(estado="reservada")
        with self.assertRaisesRegex(UserError, "No se puede pasar"):
            reservada.l10n_pe_ne_tomar()

    def test_reserva_fuera_de_la_cola_del_taller(self):
        reservada = self._reserva(estado="reservada")
        # la cola del TALLER (tipo=taller) no lista la reserva…
        cola_taller = self.Orden.l10n_pe_ne_cola_ordenes()
        self.assertNotIn(reservada.id, [i["id"] for i in cola_taller["items"]])
        # …pero la bandeja de RESERVAS (tipo=reserva + reservada) sí.
        cola_reservas = self.Orden.l10n_pe_ne_cola_reservas()
        self.assertIn(reservada.id, [i["id"] for i in cola_reservas["items"]])
        # y cola_adelanto lista los BORRADORES de ambos tipos (el 1er abono también se cobra ahí).
        borr_taller = self._taller()
        borr_reserva = self._reserva()
        ids_adelanto = [i["id"] for i in self.Orden.l10n_pe_ne_cola_adelanto()["items"]]
        self.assertIn(borr_taller.id, ids_adelanto)
        self.assertIn(borr_reserva.id, ids_adelanto)

    # ── 4) Vía A encendida: el abono NO emite; el taller SÍ (no se rompió) ───────
    def test_via_a_abono_no_emite_pero_taller_si(self):
        self.company.l10n_pe_ne_adelanto_facturado = True   # Vía A ON para toda la compañía
        self._abrir_caja()
        cajero = self._cajero("caj_rsv5")
        # RESERVA: el abono es SIEMPRE recibo interno (no emite comprobante de anticipo).
        reserva = self._reserva(precio=118.0)
        with patch(_EMIT, return_value=_OK) as post:
            reserva.with_user(cajero).l10n_pe_ne_registrar_abono(30.0, "Yape")
        self.assertEqual(reserva.estado, "reservada")
        self.assertFalse(reserva.anticipo_factura_id, "el abono NO factura anticipo ni con Vía A ON")
        self.assertFalse(post.called, "el abono no llama al facturador")
        # TALLER con la MISMA Vía A ON: el adelanto único SÍ emite su comprobante (no se rompió).
        taller = self._taller(precio=118.0)
        with patch(_EMIT, return_value=_OK):
            taller.with_user(cajero).l10n_pe_ne_registrar_adelanto(50.0, "Efectivo")
        self.assertEqual(taller.estado, "encolada")
        self.assertTrue(taller.anticipo_factura_id, "Vía A: el adelanto del taller sí factura")

    # ── 5) arqueo: cada abono entra por SU medio ────────────────────────────────
    def test_abonos_entran_al_arqueo_por_su_medio(self):
        self._abrir_caja()
        cajero = self._cajero("caj_rsv6")
        reserva = self._reserva(precio=118.0)
        reserva.with_user(cajero).l10n_pe_ne_registrar_abono(30.0, "Yape")
        reserva.with_user(cajero).l10n_pe_ne_registrar_abono(50.0, "Efectivo")
        sesion = self.Sesion.search([("estado", "=", "abierta")], limit=1)
        por_medio = sesion._l10n_pe_ne_por_medio_arqueo({"porMedio": {}})
        # cada abono suma al esperado por SU medio, sin mezclarse ni inflar un genérico.
        self.assertEqual(por_medio.get("Yape"), 30.0)
        self.assertEqual(por_medio.get("Efectivo"), 50.0)
        # y NO cuenta como ingreso genérico.
        ingresos, _retiros = sesion._l10n_pe_ne_ingresos_retiros()
        self.assertEqual(ingresos, 0.0)

    # ── 6) anular la reserva ────────────────────────────────────────────────────
    def test_anular_reserva_reservada_exige_supervisor(self):
        # con abonos ya cobrados, anular exige supervisor (reembolso manual v1); el cajero no puede.
        reservada = self._reserva(estado="reservada")
        cajero = self._cajero("caj_rsv7")
        with self.assertRaises(AccessError):
            reservada.with_user(cajero).l10n_pe_ne_anular("cliente desistió")
        sup = self._user("sup_rsv7", ["l10n_pe_ne_roles.group_l10n_pe_ne_supervisor"])
        reservada.with_user(sup).l10n_pe_ne_anular("cliente desistió")
        self.assertEqual(reservada.estado, "anulada")

    def test_unlink_reserva_con_abonos_bloqueado(self):
        # una reserva con plata encima no se borra (sus abonos viven como movimientos ligados por
        # orden_trabajo_id, NO en adelanto_movimiento_id): se anula, no se destruye el origen del cobro.
        self._abrir_caja()
        cajero = self._cajero("caj_rsv8")
        reserva = self._reserva(precio=118.0)
        reserva.with_user(cajero).l10n_pe_ne_registrar_abono(30.0, "Efectivo")
        self.assertFalse(reserva.adelanto_movimiento_id, "el abono no usa el m2o del adelanto único")
        with self.assertRaisesRegex(UserError, "No se puede borrar"):
            reserva.unlink()

    # ── 7) 'tipo' inmutable tras crear ──────────────────────────────────────────
    def test_tipo_inmutable_por_write_rpc(self):
        # 'tipo' está blindado (_campos_flujo): un write RPC no reconvierte una reserva en taller (eso
        # divergiría cola, aristas y registro del dinero). Solo lo siembra crear_orden con flujo_ok.
        reserva = self._reserva()
        cajero = self._cajero("caj_rsv9")
        with self.assertRaisesRegex(UserError, "no escribiéndolo directamente"):
            reserva.with_user(cajero).write({"tipo": "taller"})

    # ── 8) PUENTE cotización → reserva ──────────────────────────────────────────
    def test_puente_cotizacion_a_reserva(self):
        # reservar un producto ya cotizado es legítimo: el puente copia las líneas y respeta tipo.
        partner = self.env["res.partner"].create({"name": "CLIENTE COTIZA RESERVA SAC"})
        cot = self.env["l10n_pe_ne.cotizacion"].create({
            "partner_id": partner.id,
            "line_ids": [(0, 0, {"product_id": self.producto.id, "descripcion": "Licuadora Oster",
                                 "cantidad": 1.0, "precio_unitario": 118.0, "afecto_igv": True})]})
        cot.write({"estado": "aceptada"})
        r = self.Orden.l10n_pe_ne_crear_orden({"cotizacionId": cot.id, "tipo": "reserva"})
        orden = self.Orden.browse(r["id"])
        self.assertEqual(orden.tipo, "reserva")
        self.assertEqual(orden.cotizacion_id, cot)
        self.assertEqual(len(orden.linea_ids), len(cot.line_ids))
        self.assertEqual(orden.linea_ids.descripcion, "Licuadora Oster")
        self.assertEqual(orden.amount_total, cot.amount_total)

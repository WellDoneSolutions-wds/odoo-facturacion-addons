from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged


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

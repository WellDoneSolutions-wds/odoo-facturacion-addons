# -*- coding: utf-8 -*-
"""Caja por local (S4): las dos sucursales operan en paralelo y cada una cierra la suya.

Hasta ahora la caja era del RUC: el índice único parcial `(company_id) WHERE estado='abierta'`
le impedía a San Isidro abrir su turno mientras Miraflores tuviera el suyo, y
`_l10n_pe_ne_ventas_sesion` filtraba solo por compañía y ventana de fecha —así que el esperado
de efectivo de un local incluía las ventas del otro, el conteo ciego SIEMPRE daba diferencia y
esa diferencia quedaba congelada e inmutable en `conteos_cierre`.

Esto es dinero, así que se prueba en los dos sentidos: que el segundo local pueda abrir, y que
el tenant que NO usa sucursales siga sin poder abrir dos cajas a la vez (el `COALESCE` del
índice: en Postgres NULL != NULL, y sin él dos sesiones sin local no se ven como duplicadas).
"""
from datetime import timedelta

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged("post_install", "-at_install")
class TestCajaLocal(L10nPeSeedMixin, TransactionCase):
    def setUp(self):
        super().setUp()   # RUC de la compañía + IGV
        self.Sesion = self.env["l10n_pe_ne.caja.sesion"]
        self.Estab = self.env["l10n_pe_ne.establecimiento"]
        self.company = self.env.company
        self.miraflores = self.Estab.create(
            {"codigo": "0002", "ubigeo": "150122", "direccion": "Av. Larco 100, Miraflores"})
        self.san_isidro = self.Estab.create(
            {"codigo": "0003", "ubigeo": "150131", "direccion": "Av. Rivera 200, San Isidro"})
        ruc_type = self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", "6")], limit=1)
        self.partner = self.env["res.partner"].create({
            "name": "CLIENTE CAJA LOCAL SAC", "vat": "20100070970",
            "l10n_latam_identification_type_id": ruc_type.id})
        self.product = self.env["product.product"].create(
            {"name": "PRODUCTO CAJA LOCAL", "default_code": "S4CL"})

    # ---------------------------------------------------------------- utilidades
    def _cajero(self, login):
        """Cajero de la MISMA compañía. Sin rol NE (solo Emisor) a propósito: es el perfil
        legacy que los gates de roles dejan operar la caja tal cual."""
        return self.env["res.users"].create({
            "name": login, "login": login,
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(4, self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id)],
        })

    def _abrir(self, user=None, **datos):
        Sesion = self.Sesion.with_user(user) if user else self.Sesion
        d = Sesion.l10n_pe_ne_abrir_caja(dict({"saldoInicial": 0}, **datos))
        sesion = self.Sesion.browse(d["id"])
        # ANCLA la apertura en el pasado: create_date la fija Postgres al INICIO de la
        # transacción (constante en toda la TransactionCase) mientras fecha_apertura es un
        # now() de Python, así que sin esto la ventana puede dejar fuera las ventas por una
        # fracción de segundo (mismo flake que documenta test_caja).
        sesion.fecha_apertura = fields.Datetime.now() - timedelta(minutes=5)
        return sesion

    def _venta(self, correlativo, local, medios=None, precio=100.0):
        """Venta cobrada y declarada en `local`, con su create_date dentro de la ventana."""
        move = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": self.partner.id,
            "invoice_date": "2026-08-02", "l10n_pe_serie": "F001",
            "l10n_pe_correlativo": correlativo, "l10n_pe_ne_forma_pago": "Contado",
            "l10n_pe_ne_cod_establecimiento": local,
            "invoice_line_ids": [(0, 0, {
                "product_id": self.product.id, "quantity": 1.0,
                "price_unit": precio, "tax_ids": [(6, 0, self.igv.ids)]})],
        })
        if medios is not None:
            move.l10n_pe_ne_medios_pago = medios
        move.action_post()
        move.l10n_pe_biller_state = "enviado"
        move.flush_recordset()
        self.env.cr.execute("UPDATE account_move SET create_date=%s WHERE id=%s",
                            (fields.Datetime.now() - timedelta(minutes=1), move.id))
        move.invalidate_recordset(["create_date"])
        return move

    def _efectivo(self, monto):
        return [{"medio": "Efectivo", "monto": monto}]

    # ------------------------------------------------- el segundo local arranca
    def test_dos_locales_abren_caja_al_mismo_tiempo(self):
        """Lo que desbloquea la fase: Miraflores con su turno abierto no puede impedirle a San
        Isidro abrir el suyo. Antes el índice único era por compañía y el segundo local se
        quedaba sin caja el día 1."""
        a = self._abrir(saldoInicial=100, codEstablecimiento="0002")
        b = self._abrir(saldoInicial=50, codEstablecimiento="0003")
        self.env.flush_all()          # el índice de BD es quien tiene la última palabra
        self.assertEqual(a.establecimiento_id, self.miraflores)
        self.assertEqual(b.establecimiento_id, self.san_isidro)
        self.assertEqual(
            self.Sesion.search_count([("estado", "=", "abierta"),
                                      ("company_id", "=", self.company.id)]), 2)

    def test_el_local_tambien_se_elige_por_id(self):
        """La SPA de Series habla de ids y la de emisión de códigos: la apertura acepta los dos
        para no obligar a traducir en el cliente."""
        sesion = self._abrir(establecimientoId=self.miraflores.id)
        self.assertEqual(sesion.establecimiento_id, self.miraflores)
        self.assertEqual(sesion._l10n_pe_ne_sesion_dict()["establecimiento"], "0002")

    def test_segunda_caja_del_mismo_local_rebota(self):
        """La unicidad se mudó a (compañía, local), no desapareció: el mismo mostrador no puede
        tener dos turnos abiertos, o el arqueo de uno se comería las ventas del otro."""
        self._abrir(codEstablecimiento="0002")
        with self.assertRaisesRegex(UserError, "Ya hay una caja abierta en el local 0002"):
            self._abrir(codEstablecimiento="0002")

    def test_indice_unico_por_local_es_la_ultima_linea(self):
        """La guarda amigable vive en el método; el índice parcial es la defensa contra la
        carrera de dos cajeros pulsando 'Abrir' a la vez."""
        self.Sesion.create({"saldo_inicial": 0.0, "establecimiento_id": self.miraflores.id})
        self.env.flush_all()
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                self.Sesion.create({"saldo_inicial": 0.0,
                                    "establecimiento_id": self.miraflores.id})
                self.env.flush_all()

    def test_sin_locales_sigue_habiendo_una_sola_caja(self):
        """EL `COALESCE`: en Postgres NULL != NULL, así que sin él estas dos filas no se verían
        como duplicadas y un tenant que no usa sucursales —la mayoría— podría abrir DOS cajas a
        la vez. Sería una regresión silenciosa de dinero: cada arqueo contaría las ventas de
        ambas y los dos cerrarían con diferencia."""
        self._abrir()
        with self.assertRaisesRegex(UserError, "Ya hay una caja abierta"):
            self._abrir()
        self.env.flush_all()
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                self.Sesion.create({"saldo_inicial": 0.0})
                self.env.flush_all()

    def test_el_domicilio_fiscal_ocupa_el_mismo_hueco_que_la_caja_sin_local(self):
        """'0000' no tiene fila (es sintético), así que su caja y la de siempre comparten el
        hueco del índice. Es lo correcto: son el mismo mostrador físico."""
        self._abrir(codEstablecimiento="0000")
        with self.assertRaisesRegex(UserError, "Ya hay una caja abierta"):
            self._abrir()

    def test_abrir_en_un_local_inexistente_rebota(self):
        """Abrir caja en un '0009' tecleado a mano terminaría emitiendo con un codLocalEmisor
        que SUNAT rechaza, ya con el correlativo quemado. El mensaje separa «no está en tu
        catálogo» de «no está dado de alta ante SUNAT», que es trámite externo."""
        with self.assertRaisesRegex(UserError, "no está en tu catálogo"):
            self._abrir(codEstablecimiento="0009")

    # ------------------------------------------------------------- arqueo por local
    def test_arqueo_de_cada_local_cuadra_por_separado(self):
        """El corazón de la rebanada: con el conteo ciego, si el esperado de Miraflores
        arrastrara las ventas de San Isidro el cajero nunca podría cuadrar —y la diferencia
        quedaría congelada e inmutable en conteos_cierre—."""
        self._abrir(saldoInicial=100, codEstablecimiento="0002")
        self._abrir(saldoInicial=50, codEstablecimiento="0003")
        v_a = self._venta("9101", "0002", self._efectivo(118.0))
        v_b = self._venta("9102", "0003", self._efectivo(118.0))
        self._venta("9103", "0003", [{"medio": "Yape", "monto": 118.0}])

        arq_a = self.Sesion.l10n_pe_ne_cerrar_caja({
            "codEstablecimiento": "0002",
            "conteos": [{"medio": "Efectivo", "contado": 100 + v_a.amount_total}]})
        self.assertEqual(arq_a["establecimiento"], "0002")
        self.assertEqual(arq_a["ventas"]["count"], 1)
        self.assertEqual(arq_a["diferenciaTotal"], 0.0)

        arq_b = self.Sesion.l10n_pe_ne_cerrar_caja({
            "codEstablecimiento": "0003",
            "conteos": [{"medio": "Efectivo", "contado": 50 + v_b.amount_total},
                        {"medio": "Yape", "contado": 118.0}]})
        self.assertEqual(arq_b["establecimiento"], "0003")
        self.assertEqual(arq_b["ventas"]["count"], 2)
        self.assertEqual(arq_b["diferenciaTotal"], 0.0)

    def test_la_caja_de_un_local_no_ve_los_movimientos_del_otro(self):
        """Ingresos y retiros cuelgan de SU sesión: un retiro registrado en Miraflores no puede
        restarle efectivo al esperado de San Isidro."""
        a = self._abrir(saldoInicial=200, codEstablecimiento="0002")
        b = self._abrir(saldoInicial=200, codEstablecimiento="0003")
        d = self.Sesion.l10n_pe_ne_caja_movimiento({
            "codEstablecimiento": "0002", "tipo": "retiro",
            "motivo": "Pago proveedor", "monto": 80})
        self.assertEqual(d["id"], a.id)
        self.assertEqual(d["retiros"], 80.0)
        self.assertEqual(b._l10n_pe_ne_sesion_dict()["retiros"], 0.0)
        self.assertFalse(b.movimiento_ids)

    def test_el_domicilio_fiscal_cuadra_solo_con_sus_ventas(self):
        """El local principal también arquea aparte mientras el anexo vende en paralelo, sin
        materializar el '0000' como fila (D3): lo marca el flag de apertura."""
        casa = self._abrir(saldoInicial=100, codEstablecimiento="0000")
        self._abrir(saldoInicial=0, codEstablecimiento="0002")
        v_casa = self._venta("9201", "0000", self._efectivo(118.0))
        self._venta("9202", "0002", self._efectivo(118.0))

        self.assertTrue(casa.domicilio_fiscal)
        self.assertFalse(casa.establecimiento_id)
        self.assertEqual(casa._l10n_pe_ne_ventas_sesion(), v_casa)
        arq = self.Sesion.l10n_pe_ne_cerrar_caja({
            "codEstablecimiento": "0000",
            "conteos": [{"medio": "Efectivo", "contado": 100 + v_casa.amount_total}]})
        self.assertEqual(arq["diferenciaTotal"], 0.0)

    def test_la_caja_sin_local_sigue_contando_toda_la_compania(self):
        """Retrocompatibilidad, y es plata: un tenant que ya tenía anexos y UNA caja cerraba
        cuadrando con las ventas de sus anexos incluidas. Filtrarlas ahora le sacaría dinero del
        esperado el día del upgrade sin que nadie haya cambiado nada."""
        sesion = self._abrir(saldoInicial=100)
        self._venta("9301", "0000", self._efectivo(118.0))
        self._venta("9302", "0002", self._efectivo(118.0))
        self.assertEqual(len(sesion._l10n_pe_ne_ventas_sesion()), 2)
        self.assertEqual(sesion._l10n_pe_ne_sesion_dict()["establecimiento"], "")
        arq = self.Sesion.l10n_pe_ne_cerrar_caja({
            "conteos": [{"medio": "Efectivo", "contado": 100 + 118.0 + 118.0}]})
        self.assertEqual(arq["ventas"]["count"], 2)
        self.assertEqual(arq["diferenciaTotal"], 0.0)

    # -------------------------------------------------- qué caja es "la mía"
    def test_caja_actual_devuelve_la_del_propio_usuario(self):
        """'La sesión de la compañía' dejó de identificar nada: cada cajero opera la suya."""
        otro = self._cajero("cajero_s4_otro")
        propia = self._abrir(saldoInicial=10, codEstablecimiento="0002")
        self._abrir(user=otro, saldoInicial=20, codEstablecimiento="0003")
        act = self.Sesion.l10n_pe_ne_caja_actual()
        self.assertTrue(act["abierta"])
        self.assertEqual(act["sesion"]["id"], propia.id)
        self.assertEqual(act["sesion"]["establecimiento"], "0002")

    def test_caja_actual_pregunta_en_vez_de_adivinar(self):
        """Con varias abiertas y ninguna del usuario NO se elige una: cobrar en la caja del otro
        local descuadraría los dos arqueos a la vez. Se devuelve la lista para que la SPA
        pregunte, y su respuesta vuelve como `establecimiento`."""
        uno, dos = self._cajero("cajero_s4_uno"), self._cajero("cajero_s4_dos")
        a = self._abrir(user=uno, codEstablecimiento="0002")
        self._abrir(user=dos, codEstablecimiento="0003")
        act = self.Sesion.l10n_pe_ne_caja_actual()
        self.assertFalse(act["abierta"])
        self.assertTrue(act["requiereLocal"])
        self.assertEqual(sorted(l["establecimiento"] for l in act["locales"]), ["0002", "0003"])

        elegida = self.Sesion.l10n_pe_ne_caja_actual(establecimiento="0002")
        self.assertTrue(elegida["abierta"])
        self.assertEqual(elegida["sesion"]["id"], a.id)

    def test_caja_actual_conserva_su_contrato_de_siempre(self):
        """El tenant de una sola caja no ve ninguna clave nueva: dos claves, como ayer."""
        self.assertEqual(self.Sesion.l10n_pe_ne_caja_actual(),
                         {"abierta": False, "sesion": None})
        sesion = self._abrir(saldoInicial=30)
        act = self.Sesion.l10n_pe_ne_caja_actual()
        self.assertEqual(set(act), {"abierta", "sesion"})
        self.assertEqual(act["sesion"]["id"], sesion.id)

    def test_operar_con_varias_cajas_exige_decir_desde_donde(self):
        """Un movimiento sin saber la caja es dinero en el mostrador equivocado: mejor lanzar
        que adivinar. Con el local dicho, entra donde debe."""
        uno, dos = self._cajero("cajero_s4_mov1"), self._cajero("cajero_s4_mov2")
        a = self._abrir(user=uno, saldoInicial=100, codEstablecimiento="0002")
        self._abrir(user=dos, saldoInicial=100, codEstablecimiento="0003")
        with self.assertRaisesRegex(UserError, "Hay varias cajas abiertas"):
            self.Sesion.l10n_pe_ne_caja_movimiento(
                {"tipo": "ingreso", "motivo": "sencillo", "monto": 10})
        d = self.Sesion.l10n_pe_ne_caja_movimiento(
            {"codEstablecimiento": "0002", "tipo": "ingreso", "motivo": "sencillo", "monto": 10})
        self.assertEqual(d["id"], a.id)

    def test_el_relevo_sin_cerrar_sigue_operando_la_unica_caja(self):
        """Retrocompat del día a día: si hay UNA sola caja abierta, cualquier cajero del RUC la
        opera aunque no la haya abierto él (el que releva al del turno anterior)."""
        otro = self._cajero("cajero_s4_relevo")
        sesion = self._abrir(user=otro, saldoInicial=100)
        act = self.Sesion.l10n_pe_ne_caja_actual()
        self.assertEqual(act["sesion"]["id"], sesion.id)
        d = self.Sesion.l10n_pe_ne_caja_movimiento(
            {"tipo": "ingreso", "motivo": "sencillo", "monto": 10})
        self.assertEqual(d["id"], sesion.id)

    # ------------------------------------------------- enganche con la emisión
    def test_el_resolver_de_emision_no_adivina_local_con_varias_cajas(self):
        """El escalón 4 del resolver mira la caja del turno. Con dos abiertas y ninguna del
        usuario, devolver 'cualquiera' declararía las ventas del domicilio fiscal en la sucursal
        del vecino: se cae a '0000', que es lo que hacía todo el mundo hasta esta fase."""
        uno, dos = self._cajero("cajero_s4_res1"), self._cajero("cajero_s4_res2")
        self._abrir(user=uno, codEstablecimiento="0002")
        self._abrir(user=dos, codEstablecimiento="0003")
        self.assertEqual(self.Sesion._l10n_pe_ne_local_abierto(), "")
        self.assertEqual(
            self.env["account.move"]._l10n_pe_ne_resolver_establecimiento({}), "0000")

    def test_la_caja_del_domicilio_fiscal_no_arrastra_la_venta_a_otro_local(self):
        """El cajero del local principal tiene SU caja abierta: sus ventas se declaran en '0000'
        aunque el anexo tenga la suya abierta al lado."""
        otro = self._cajero("cajero_s4_anexo")
        self._abrir(codEstablecimiento="0000")
        self._abrir(user=otro, codEstablecimiento="0002")
        self.assertEqual(self.Sesion._l10n_pe_ne_local_abierto(), "0000")
        self.assertEqual(
            self.env["account.move"]._l10n_pe_ne_resolver_establecimiento({}), "0000")

    def test_el_local_con_caja_se_archiva_pero_no_se_borra(self):
        """Un arqueo cerrado NOMBRA su local: borrar la fila reescribiría un arqueo que es
        inmutable por diseño, así que 'eliminar' pasa a ser archivar."""
        self._abrir(codEstablecimiento="0003")
        res = self.Estab.l10n_pe_ne_delete_establecimiento(self.san_isidro.id)
        self.assertEqual(res["modo"], "archivado")
        self.assertFalse(self.san_isidro.active)

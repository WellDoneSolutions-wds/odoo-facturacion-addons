# -*- coding: utf-8 -*-
"""C3 · Gastos que cuadran y egresos por cualquier medio.

Dos agujeros con el mismo síntoma —el arqueo no refleja el dinero real— y el mismo perjudicado,
el cajero honesto que cierra con un faltante que no puede explicar:

(a) EL GASTO NO TOCABA LA CAJA. Pagabas S/ 50 de gaseosas con la plata del cajón, lo registrabas
    como gasto, y el arqueo seguía esperando esos S/ 50. El único egreso que la caja veía era el
    'retiro' de su propia pantalla, así que había que registrar DOS veces la misma plata y
    acordarse de hacerlo. Ahora el gasto se marca «se pagó del cajón» y el sistema crea su
    movimiento de caja, ligado al gasto: una acción, los dos libros cuadrados.

(b) EL EGRESO ERA SOLO EFECTIVO. Si el negocio le pagaba al proveedor por Yape con la plata que
    entró por Yape, no había dónde registrarlo: o no se anotaba (y el arqueo esperaba un saldo de
    Yape que ya no estaba) o se anotaba como retiro de efectivo (y descuadraban DOS bolsillos de
    golpe). Ahora el ingreso/retiro tiene medio, con guard de disponible POR MEDIO.
"""
from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestCajaGastoEgreso(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.Sesion = self.env["l10n_pe_ne.caja.sesion"]
        self.Movimiento = self.env["l10n_pe_ne.caja.movimiento"]
        self.Gasto = self.env["l10n_pe_ne.gasto"]

    # ---------------------------------------------------------------- utilidades
    def _abrir(self, saldo=0.0):
        d = self.Sesion.l10n_pe_ne_abrir_caja({"saldoInicial": saldo})
        sesion = self.Sesion.browse(d["id"])
        # Igual que el resto de los tests de caja: ancla la apertura en el pasado para que la
        # ventana del arqueo no dependa de fracciones de segundo.
        sesion.fecha_apertura = fields.Datetime.now() - timedelta(minutes=5)
        return sesion

    def _fondear(self, medio, monto):
        """Mete plata a un bolsillo (el equivalente a la venta cobrada por ese medio)."""
        return self.Sesion.l10n_pe_ne_caja_movimiento(
            {"tipo": "ingreso", "motivo": "Fondeo de prueba", "monto": monto, "medio": medio})

    def _esperado(self, sesion, medio):
        """Esperado de un medio LEÍDO DEL CIERRE (que es donde se revela, D-1)."""
        arq = self.Sesion.l10n_pe_ne_cerrar_caja(
            {"conteos": [{"medio": medio, "contado": 0.0}], "motivoDescuadre": "prueba de arqueo"})
        return {f["medio"]: f["esperado"] for f in arq["arqueo"]}.get(medio)

    # ═════════════════════════════════════════ (a) el gasto del cajón mueve la caja
    def test_gasto_del_cajon_resta_del_esperado_de_efectivo(self):
        """EL caso: S/ 200 en el cajón, se pagan S/ 50 de gaseosas del cajón. El arqueo tiene que
        esperar S/ 150, no S/ 200 — antes esperaba 200 y el cajero cerraba con un faltante de 50
        que solo podía explicar de memoria."""
        sesion = self._abrir(saldo=200.0)
        d = self.Gasto.l10n_pe_ne_create_gasto(
            {"descripcion": "Gaseosas", "monto": 50.0, "cuenta": "Efectivo", "pagaCaja": True})
        self.assertTrue(d["pagaCaja"])
        self.assertTrue(d["movimientoCajaId"])
        self.assertEqual(d["sesionCajaId"], sesion.id)
        mv = self.Movimiento.browse(d["movimientoCajaId"])
        self.assertEqual(mv.tipo, "retiro")
        self.assertEqual(mv.medio, "Efectivo")
        self.assertEqual(mv.monto, 50.0)
        self.assertEqual(mv.gasto_id.id, d["id"])
        self.assertEqual(self._esperado(sesion, "Efectivo"), 150.0)

    def test_gasto_del_cajon_resta_del_medio_correcto(self):
        """El gasto pagado por Yape descuenta de YAPE, no del cajón. Restarlo del efectivo (lo
        único que se podía hacer antes) descuadraba dos bolsillos: sobraba efectivo y faltaba
        Yape, y el neto engañosamente cuadraba."""
        sesion = self._abrir(saldo=100.0)
        self._fondear("Yape", 80.0)
        self.Gasto.l10n_pe_ne_create_gasto(
            {"descripcion": "Proveedor de bolsas", "monto": 30.0, "cuenta": "Yape",
             "pagaCaja": True})
        arq = self.Sesion.l10n_pe_ne_cerrar_caja({"conteos": [
            {"medio": "Efectivo", "contado": 100.0}, {"medio": "Yape", "contado": 50.0}]})
        filas = {f["medio"]: f for f in arq["arqueo"]}
        self.assertEqual(filas["Efectivo"]["esperado"], 100.0)   # el cajón no se tocó
        self.assertEqual(filas["Yape"]["esperado"], 50.0)        # 80 − 30
        self.assertEqual(arq["diferenciaTotal"], 0.0)

    def test_gasto_por_banco_no_toca_la_caja(self):
        """El gasto que NO sale del cajón (banco, tarjeta personal, bolsillo del dueño) no mueve
        la caja. Es la conducta de siempre y la que hace que la casilla signifique algo."""
        sesion = self._abrir(saldo=200.0)
        d = self.Gasto.l10n_pe_ne_create_gasto(
            {"descripcion": "Internet del local", "monto": 89.0, "cuenta": "BCP",
             "pagaCaja": False})
        self.assertFalse(d["pagaCaja"])
        self.assertIsNone(d["movimientoCajaId"])
        self.assertFalse(sesion.movimiento_ids)
        self.assertEqual(self._esperado(sesion, "Efectivo"), 200.0)

    def test_gasto_sin_marcar_no_toca_la_caja_por_defecto(self):
        """RETROCOMPAT: el cliente que no manda `pagaCaja` (toda integración anterior a C3) sigue
        registrando gastos que no descuentan de ningún arqueo. Nada cambia sin configurar nada."""
        sesion = self._abrir(saldo=200.0)
        d = self.Gasto.l10n_pe_ne_create_gasto({"descripcion": "Luz", "monto": 40.0})
        self.assertFalse(d["pagaCaja"])
        self.assertFalse(sesion.movimiento_ids)

    def test_default_del_gasto_de_caja_es_del_negocio(self):
        """El default NO es un número en el código: vive en res.company. La bodega que paga todo
        del cajón lo enciende una vez y deja de marcar la casilla doscientas veces al mes."""
        self.company.l10n_pe_ne_gasto_de_caja = True
        self._abrir(saldo=300.0)
        d = self.Gasto.l10n_pe_ne_create_gasto({"descripcion": "Escoba", "monto": 15.0})
        self.assertTrue(d["pagaCaja"])
        self.assertTrue(d["movimientoCajaId"])
        # y el usuario puede desmarcarlo gasto por gasto (el default no es una regla)
        d2 = self.Gasto.l10n_pe_ne_create_gasto(
            {"descripcion": "Alquiler", "monto": 900.0, "pagaCaja": False})
        self.assertFalse(d2["pagaCaja"])

    def test_gasto_del_cajon_sin_caja_abierta_rebota_con_salida(self):
        """Sin caja abierta el egreso quedaría fuera de todo arqueo. A diferencia de una venta
        (que nunca se bloquea), un gasto SÍ se puede detener: no hay un cliente en el mostrador.
        El mensaje tiene que ofrecer las dos salidas reales."""
        with self.assertRaisesRegex(UserError, "no hay ninguna caja abierta"):
            self.Gasto.l10n_pe_ne_create_gasto(
                {"descripcion": "Gaseosas", "monto": 50.0, "pagaCaja": True})
        # y el gasto NO queda a medias
        self.assertFalse(self.Gasto.search([("descripcion", "=", "Gaseosas")]))

    def test_gasto_del_cajon_sobre_lo_disponible_rebota(self):
        """No se saca del cajón más de lo que hay. Sin este guard el esperado de efectivo queda
        negativo: físicamente imposible, y esconde el error real (el medio mal elegido)."""
        self._abrir(saldo=40.0)
        with self.assertRaisesRegex(UserError, "excede lo que hay en la caja"):
            self.Gasto.l10n_pe_ne_create_gasto(
                {"descripcion": "Compra grande", "monto": 300.0, "pagaCaja": True})

    def test_reversa_devuelve_la_plata_a_la_caja(self):
        """Reversar un gasto del cajón tiene que devolver el dinero al arqueo. Si no, la caja
        seguiría descontando un egreso que ya no existe y el cajero cerraría con un SOBRANTE
        inexplicable — el mismo problema al revés."""
        sesion = self._abrir(saldo=200.0)
        g = self.Gasto.l10n_pe_ne_create_gasto(
            {"descripcion": "Gaseosas", "monto": 50.0, "pagaCaja": True})
        rev = self.Gasto.l10n_pe_ne_reversar_gasto(g["id"], motivo="Se pagó por Yape, no del cajón")
        self.assertTrue(rev["pagaCaja"])
        self.assertNotIn("avisoCaja", rev)
        mv = self.Movimiento.browse(rev["movimientoCajaId"])
        self.assertEqual(mv.tipo, "ingreso")
        self.assertEqual(mv.monto, 50.0)
        self.assertEqual(mv.medio, "Efectivo")
        # el egreso y su reversa netean: el esperado vuelve a ser el saldo inicial
        self.assertEqual(self._esperado(sesion, "Efectivo"), 200.0)

    def test_reversa_de_gasto_por_banco_no_toca_la_caja(self):
        """La reversa espeja al original: si el gasto no salió del cajón, su reversa no le mete
        plata a la caja de nadie."""
        sesion = self._abrir(saldo=200.0)
        g = self.Gasto.l10n_pe_ne_create_gasto(
            {"descripcion": "Internet", "monto": 89.0, "cuenta": "BCP", "pagaCaja": False})
        rev = self.Gasto.l10n_pe_ne_reversar_gasto(g["id"])
        self.assertFalse(rev["pagaCaja"])
        self.assertIsNone(rev["movimientoCajaId"])
        self.assertFalse(sesion.movimiento_ids)

    def test_reversa_con_sesion_cerrada_no_reescribe_el_arqueo(self):
        """D-2: el arqueo de una sesión CERRADA es inmutable. La reversa contable se hace igual
        (el gasto no puede quedar vivo), pero no se le mete un movimiento a un turno cerrado ni
        se le devuelve la plata a la caja de HOY —que contaría un dinero que nadie puso en ese
        cajón—. Se avisa por escrito, que es lo único honesto."""
        sesion = self._abrir(saldo=200.0)
        g = self.Gasto.l10n_pe_ne_create_gasto(
            {"descripcion": "Gaseosas", "monto": 50.0, "pagaCaja": True})
        arq = self.Sesion.l10n_pe_ne_cerrar_caja(
            {"conteos": [{"medio": "Efectivo", "contado": 150.0}]})
        self.assertEqual(arq["diferenciaTotal"], 0.0)
        congelado = list(sesion.conteos_cierre)

        rev = self.Gasto.l10n_pe_ne_reversar_gasto(g["id"], motivo="No era gasto del negocio")
        self.assertFalse(rev["pagaCaja"])
        self.assertIsNone(rev["movimientoCajaId"])
        self.assertIn("cerrada", rev["avisoCaja"])
        # el arqueo congelado no se movió ni le apareció un movimiento nuevo
        self.assertEqual(list(sesion.conteos_cierre), congelado)
        self.assertEqual(len(sesion.movimiento_ids), 1)

    def test_el_gasto_del_cajon_es_inmutable(self):
        """D-2: `paga_caja` no se reescribe. Cambiarlo después dejaría un gasto diciendo que
        salió del cajón sin su movimiento (o al revés), que es el descuadre que C3 vino a cerrar."""
        self._abrir(saldo=200.0)
        d = self.Gasto.l10n_pe_ne_create_gasto(
            {"descripcion": "Gaseosas", "monto": 50.0, "pagaCaja": True})
        with self.assertRaisesRegex(UserError, "no se puede editar"):
            self.Gasto.browse(d["id"]).write({"paga_caja": False})

    # ═════════════════════════════════════ (b) ingresos y retiros por cualquier medio
    def test_retiro_por_yape_descuenta_de_yape(self):
        """Pagar al proveedor por Yape con la plata del día: sale de Yape y el cajón no se
        entera. Antes esto no se podía registrar sin descuadrar dos bolsillos."""
        self._abrir(saldo=100.0)
        self._fondear("Yape", 200.0)
        s = self.Sesion.l10n_pe_ne_caja_movimiento(
            {"tipo": "retiro", "motivo": "Pago proveedor por Yape", "monto": 120.0,
             "medio": "Yape"})
        mv = s["movimientos"][-1]
        self.assertEqual(mv["medio"], "Yape")
        arq = self.Sesion.l10n_pe_ne_cerrar_caja({"conteos": [
            {"medio": "Efectivo", "contado": 100.0}, {"medio": "Yape", "contado": 80.0}]})
        filas = {f["medio"]: f for f in arq["arqueo"]}
        self.assertEqual(filas["Efectivo"]["esperado"], 100.0)
        self.assertEqual(filas["Yape"]["esperado"], 80.0)        # 200 − 120
        self.assertEqual(arq["diferenciaTotal"], 0.0)

    def test_retiro_por_yape_sobre_su_disponible_rebota(self):
        """No se saca por Yape más de lo que entró por Yape: la caja no es una bolsa común. Que
        haya S/ 1000 en el cajón no habilita un retiro de S/ 300 por Yape."""
        self._abrir(saldo=1000.0)
        self._fondear("Yape", 200.0)
        with self.assertRaisesRegex(UserError, "excede lo que hay en la caja"):
            self.Sesion.l10n_pe_ne_caja_movimiento(
                {"tipo": "retiro", "motivo": "Pago proveedor", "monto": 300.0, "medio": "Yape"})
        # el efectivo tampoco financia a Yape al revés: el bolsillo vacío rebota aunque el cajón
        # esté lleno
        with self.assertRaisesRegex(UserError, "excede lo que hay en la caja"):
            self.Sesion.l10n_pe_ne_caja_movimiento(
                {"tipo": "retiro", "motivo": "Pago con Plin", "monto": 10.0, "medio": "Plin"})

    def test_retiro_sin_medio_sigue_siendo_efectivo(self):
        """RETROCOMPAT: el cliente que no manda `medio` (toda la SPA anterior a C3, y todo el
        histórico ya escrito) sigue moviendo el efectivo, exactamente como antes."""
        self._abrir(saldo=500.0)
        s = self.Sesion.l10n_pe_ne_caja_movimiento(
            {"tipo": "retiro", "motivo": "mototaxi", "monto": 50.0})
        self.assertEqual(s["movimientos"][-1]["medio"], "Efectivo")
        self.assertEqual(s["retiros"], 50.0)

    def test_el_medio_del_movimiento_se_canoniza(self):
        """'yape' y 'Yape' son el MISMO bolsillo: si no, el cajero cuenta su Yape dos veces y una
        de las dos filas cierra con diferencia sin que falte un sol."""
        self._abrir(saldo=0.0)
        self._fondear("YAPE", 100.0)
        s = self._fondear("  yape ", 50.0)
        self.assertEqual([m["medio"] for m in s["movimientos"]], ["Yape", "Yape"])
        arq = self.Sesion.l10n_pe_ne_cerrar_caja({"conteos": [
            {"medio": "Efectivo", "contado": 0.0}, {"medio": "Yape", "contado": 150.0}]})
        filas = {f["medio"]: f for f in arq["arqueo"]}
        self.assertEqual(len([m for m in filas if m.lower() == "yape"]), 1)
        self.assertEqual(filas["Yape"]["esperado"], 150.0)

    def test_retiro_por_yape_sobre_umbral_exige_voucher(self):
        """D-4 sigue aplicando a cualquier medio: sacar S/ 800 por Yape necesita el mismo
        respaldo que sacarlos del cajón —cambia el bolsillo, no el dinero—."""
        self._abrir(saldo=0.0)
        self._fondear("Yape", 1000.0)
        with self.assertRaisesRegex(UserError, "voucher"):
            self.Sesion.l10n_pe_ne_caja_movimiento(
                {"tipo": "retiro", "motivo": "Pago proveedor", "monto": 800.0, "medio": "Yape"})
        self.Sesion.l10n_pe_ne_caja_movimiento(
            {"tipo": "retiro", "motivo": "Pago proveedor", "monto": 800.0, "medio": "Yape",
             "voucherRef": "YP-1234", "fechaVoucher": "2026-08-02"})

    def test_el_conteo_ciego_sigue_intacto(self):
        """D-1: nada de esto revela el esperado con la sesión abierta. El movimiento por medio
        siembra el NOMBRE del bolsillo para que el cajero sepa qué contar, y nada más."""
        self._abrir(saldo=100.0)
        self._fondear("Yape", 200.0)
        self.Gasto.l10n_pe_ne_create_gasto(
            {"descripcion": "Gaseosas", "monto": 50.0, "pagaCaja": True})
        ses = self.Sesion.l10n_pe_ne_caja_actual()["sesion"]
        self.assertIn("Yape", ses["medios"])
        self.assertNotIn("esperado", ses)
        arq = self.Sesion.l10n_pe_ne_caja_arqueo(ses["id"])
        self.assertIsNone(arq["esperadoTotal"])
        for f in arq["arqueo"]:
            self.assertIsNone(f["esperado"])

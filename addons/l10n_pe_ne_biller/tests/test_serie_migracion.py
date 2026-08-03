# -*- coding: utf-8 -*-
"""Migración 19.0.1.21.0 (S5): el upgrade sobre una base ya desplegada.

Lo que se prueba aquí no es una función: es que un tenant en producción pueda correr
`-u l10n_pe_ne_biller` sin sorpresas. Tres invariantes, en orden de qué duele más si falla:

  1. NO siembra el registro de series ni toca las secuencias (D5): la retrocompatibilidad
     vive en el código, y una migración de datos que deduce series es la forma de convertir
     un fallback probado en datos por tenant que derivan.
  2. Es IDEMPOTENTE: correrla dos veces (o después de que `init()` hizo lo suyo) no cambia
     nada más ni revienta.
  3. Tira el índice de caja viejo, que es lo único que impide de verdad que el segundo local
     arranque.
"""
import importlib.util
from pathlib import Path

from odoo.tests import TransactionCase, tagged

import odoo.addons.l10n_pe_ne_biller as _biller

_RUTA = Path(_biller.__file__).parent / "migrations" / "19.0.1.21.0" / "pre-migrate.py"


def _cargar_migracion():
    """El paquete de migraciones no es importable (la carpeta se llama '19.0.1.21.0' y no hay
    __init__.py): se carga por ruta, igual que hace el propio loader de Odoo."""
    spec = importlib.util.spec_from_file_location("ne_pre_migrate_19_0_1_21_0", _RUTA)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@tagged("post_install", "-at_install")
class TestSerieMigracion(TransactionCase):
    def setUp(self):
        super().setUp()
        self.migracion = _cargar_migracion()
        self.company = self.env.company
        self.Serie = self.env["l10n_pe_ne.serie"]
        self.Caja = self.env["l10n_pe_ne.caja.sesion"]
        self.partner = self.env["res.partner"].create({"name": "CLIENTE MIGRACION SAC"})

    # ------------------------------------------------------------------ utilidades
    def _migrar(self):
        self.migracion.migrate(self.env.cr, "19.0.1.20.0")

    def _indexdef(self):
        self.env.cr.execute("SELECT indexdef FROM pg_indexes WHERE indexname = %s",
                            ("l10n_pe_ne_caja_sesion_unica_abierta",))
        fila = self.env.cr.fetchone()
        return (fila[0] if fila else "") or ""

    def _indice_viejo(self):
        """Deja la BD como la de un tenant anterior a esta fase: el índice de sesión única
        por compañía a secas, con el MISMO nombre que el nuevo."""
        self.env.cr.execute("DROP INDEX IF EXISTS l10n_pe_ne_caja_sesion_unica_abierta")
        self.env.cr.execute("""
            CREATE UNIQUE INDEX l10n_pe_ne_caja_sesion_unica_abierta
            ON l10n_pe_ne_caja_sesion (company_id) WHERE estado = 'abierta'
        """)

    def _move(self, cod="0000"):
        return self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": self.partner.id,
            "l10n_pe_ne_cod_establecimiento": cod})

    def _secuencias(self):
        return self.env["ir.sequence"].sudo().search_count(
            [("code", "=like", "l10n_pe.ne.cpe.%")])

    # ----------------------------------------------------- D5: no siembra nada
    def test_no_siembra_el_registro_de_series(self):
        """El registro arranca vacío y sigue vacío tras el upgrade: lo que el dueño ve en
        Series el día del despliegue es lo mismo que veía ayer."""
        m = self._move()
        m.l10n_pe_ne_serie_emit = "F001"
        m.l10n_pe_ne_corr_emit = "00000021"
        self._migrar()
        self.assertFalse(self.Serie.search([("company_id", "=", self.company.id)]))

    def test_dos_diarios_con_la_misma_serie_no_tumban_el_upgrade(self):
        """Nada impide hoy dos diarios de venta con la misma serie. Sembrar el registro
        obligaría a deduplicar y a decidir cuál gana; como no se siembra, el caso ni siquiera
        existe — y el upgrade termina igual de limpio."""
        for code in ("SERA", "SERB"):
            self.env["account.journal"].create({
                "name": "Ventas %s" % code, "code": code, "type": "sale",
                "company_id": self.company.id, "l10n_pe_ne_serie": "F001"})
        self._migrar()
        self.assertFalse(self.Serie.search([("codigo", "=", "F001")]))

    def test_no_toca_las_secuencias_existentes(self):
        """Las ir.sequence de las series vivas no se siembran, ni se reinician, ni se
        renombran: reiniciar un contador fiscal es duplicar comprobantes."""
        seq = self.env["ir.sequence"].sudo().create({
            "name": "CPE F001", "code": "l10n_pe.ne.cpe.F001", "implementation": "no_gap",
            "padding": 8, "number_next": 42, "company_id": self.company.id})
        antes = self._secuencias()
        self._migrar()
        self.assertEqual(self._secuencias(), antes)
        self.assertEqual(seq.number_next_actual, 42)

    # ---------------------------------------------------------- idempotencia
    def test_correrla_dos_veces_no_cambia_nada(self):
        """Un `-u` repetido (o un reintento tras un fallo posterior) tiene que ser inofensivo:
        el índice queda bien y no aparecen filas duplicadas por ningún lado."""
        self._indice_viejo()
        self._migrar()
        self.Caja.init()
        primero = self._indexdef()
        series_1 = self.Serie.search_count([])
        self._migrar()
        self.Caja.init()
        self.assertEqual(self._indexdef(), primero)
        self.assertEqual(self.Serie.search_count([]), series_1)
        self.env.cr.execute("""
            SELECT count(*) FROM pg_indexes
            WHERE indexname = 'l10n_pe_ne_caja_sesion_unica_abierta'
        """)
        self.assertEqual(self.env.cr.fetchone()[0], 1)

    # ------------------------------------------------- índice de caja (D7)
    def test_tira_el_indice_viejo_para_que_init_lo_recree_por_local(self):
        """`CREATE UNIQUE INDEX IF NOT EXISTS` NO recrea un índice que ya existe con ese
        nombre: sin el DROP, el índice por compañía sobrevive al upgrade y San Isidro no puede
        abrir caja mientras Miraflores tenga la suya."""
        self._indice_viejo()
        self.assertNotIn("COALESCE", self._indexdef().upper())
        self._migrar()
        self.assertEqual(self._indexdef(), "")   # tirado: init() es quien lo recrea
        self.Caja.init()
        self.assertIn("COALESCE", self._indexdef().upper())

    def test_con_el_indice_nuevo_no_lo_toca(self):
        """No tira a ciegas: si ya es el índice por local, dejarlo caer aunque sea un instante
        abriría la ventana para dos cajas simultáneas del mismo local."""
        self.Caja.init()
        antes = self._indexdef()
        self.assertIn("COALESCE", antes.upper())
        self._migrar()
        self.assertEqual(self._indexdef(), antes)

    def test_tras_migrar_el_segundo_local_puede_abrir_su_caja(self):
        """El efecto que se buscaba, medido donde duele: dos turnos simultáneos, uno por local,
        y el tenant sin locales sigue con UNA sola sesión (el COALESCE)."""
        self._indice_viejo()
        self._migrar()
        self.Caja.init()
        Estab = self.env["l10n_pe_ne.establecimiento"]
        miraflores = Estab.create({"codigo": "0002", "ubigeo": "150122",
                                   "direccion": "Av. Larco 100"})
        san_isidro = Estab.create({"codigo": "0003", "ubigeo": "150131",
                                   "direccion": "Av. Camino Real 200"})
        self.Caja.create({"estado": "abierta", "establecimiento_id": miraflores.id})
        self.Caja.create({"estado": "abierta", "establecimiento_id": san_isidro.id})
        self.env.flush_all()   # si el índice viejo siguiera vivo, aquí reventaría

    # --------------------------------------------- normalización defensiva
    def test_el_local_nulo_pasa_a_domicilio_fiscal(self):
        """Un NULL sería una venta que no aparece en NINGÚN local del reporte ni del arqueo.
        '0000' es exactamente lo que ese comprobante declaró en su XML, solo que escrito."""
        m = self._move()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET l10n_pe_ne_cod_establecimiento = NULL WHERE id = %s",
            (m.id,))
        m.invalidate_recordset(["l10n_pe_ne_cod_establecimiento"])
        self.assertFalse(m.l10n_pe_ne_cod_establecimiento)
        self._migrar()
        m.invalidate_recordset(["l10n_pe_ne_cod_establecimiento"])
        self.assertEqual(m.l10n_pe_ne_cod_establecimiento, "0000")

    def test_no_reescribe_el_local_ya_declarado(self):
        """Historia fiscal: el comprobante que declaró un anexo se queda con su anexo."""
        m = self._move("0002")
        self.env.flush_all()
        self._migrar()
        m.invalidate_recordset(["l10n_pe_ne_cod_establecimiento"])
        self.assertEqual(m.l10n_pe_ne_cod_establecimiento, "0002")

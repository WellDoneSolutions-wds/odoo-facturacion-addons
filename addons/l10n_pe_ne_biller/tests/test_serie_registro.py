# -*- coding: utf-8 -*-
"""Registro de series por establecimiento (S1): retrocompatibilidad con el registro vacío,
unicidad de la serie dentro del RUC, predeterminada por (local, tipo), serie del domicilio
fiscal y el muro de configuración —que cubre también el CRUD de establecimientos—."""
from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestSerieRegistro(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.Serie = self.env["l10n_pe_ne.serie"]
        self.Estab = self.env["l10n_pe_ne.establecimiento"]
        self.miraflores = self.Estab.create(
            {"codigo": "0002", "ubigeo": "150122", "direccion": "Av. Larco 100, Miraflores"})
        self.san_isidro = self.Estab.create(
            {"codigo": "0003", "ubigeo": "150131", "direccion": "Av. Camino Real 200"})

    # ------------------------------------------------------------------ utilidades
    def _usuario(self, login, xmlid):
        return self.env["res.users"].create({
            "name": login, "login": login,
            "company_id": self.company.id, "company_ids": [(6, 0, [self.company.id])],
            "group_ids": [(6, 0, [self.env.ref(xmlid).id])],
        })

    def _move(self):
        """Comprobante mínimo, solo para colgar de él los helpers de serie."""
        partner = self.env["res.partner"].create({"name": "CLIENTE SERIES SAC"})
        return self.env["account.move"].create(
            {"move_type": "out_invoice", "partner_id": partner.id})

    # -------------------------------------------------- retrocompatibilidad (D5)
    def test_registro_arranca_vacio(self):
        """El registro no se siembra por migración ni por data: nace vacío y la
        retrocompatibilidad vive en el código, no en los datos."""
        self.assertFalse(self.Serie.search([("company_id", "=", self.company.id)]))

    def test_registro_vacio_no_cambia_series_habilitadas(self):
        """Sin ninguna fila, QA-074 valida exactamente el mismo conjunto que antes: los seis
        defaults del sistema siguen ahí y nada se cae."""
        move = self._move()
        habilitadas = move._l10n_pe_ne_series_habilitadas()
        self.assertLessEqual(
            {"F001", "B001", "FC01", "FD01", "BC01", "BD01"}, habilitadas)
        self.assertNotIn("F002", habilitadas)

    def test_registro_entra_en_union_y_desactivar_la_saca(self):
        """UNIÓN, nunca reemplazo: F002 declarada se habilita y los defaults siguen valiendo.
        Apagarla la saca del conjunto sin tocar nada más."""
        move = self._move()
        base = move._l10n_pe_ne_series_habilitadas()
        serie = self.Serie.create(
            {"codigo": "F002", "tipo_doc": "01", "establecimiento_id": self.miraflores.id})
        self.assertEqual(move._l10n_pe_ne_series_habilitadas(), base | {"F002"})
        serie.activa = False
        self.assertEqual(move._l10n_pe_ne_series_habilitadas(), base)

    def test_get_series_es_aditivo(self):
        """El contrato de GET /ne/api/series no se rompe: conserva sus claves y suma las
        nuevas. Con el registro vacío, todo lo que sale viene del uso."""
        m = self._move()
        m.l10n_pe_ne_serie_emit = "F001"
        m.l10n_pe_ne_corr_emit = "00000007"
        filas = self.env["account.move"].l10n_pe_ne_series()
        f001 = next(f for f in filas if f["serie"] == "F001")
        self.assertLessEqual(
            {"serie", "tipoDoc", "tipo", "emitidos", "ultimo", "proximo"}, set(f001))
        self.assertEqual(f001["ultimo"], "00000007")
        self.assertEqual(f001["proximo"], "00000008")
        self.assertEqual(f001["origen"], "uso")
        self.assertIsNone(f001["establecimiento"])

        self.Serie.create({"codigo": "F002", "tipo_doc": "01",
                           "establecimiento_id": self.miraflores.id, "predeterminada": True})
        f002 = next(f for f in self.env["account.move"].l10n_pe_ne_series()
                    if f["serie"] == "F002")
        self.assertEqual(f002["origen"], "config")
        self.assertEqual(f002["establecimiento"], "0002")
        self.assertEqual(f002["emitidos"], 0)
        self.assertEqual(f002["ultimo"], "—")
        self.assertEqual(f002["proximo"], "00000001")
        self.assertTrue(f002["predeterminada"])

    # ------------------------------------------------------------ unicidad (D2)
    def test_misma_serie_en_dos_locales_explica_la_regla(self):
        """La intuición del dueño («F001 en mis dos locales») choca con una regla dura: el
        correlativo es por (RUC, serie). El error la EXPLICA, no solo la niega."""
        self.Serie.l10n_pe_ne_serie_upsert(
            {"serie": "F001", "tipoDoc": "01", "establecimientoId": self.miraflores.id})
        with self.assertRaises(ValidationError) as ctx:
            self.Serie.l10n_pe_ne_serie_upsert(
                {"serie": "F001", "tipoDoc": "01", "establecimientoId": self.san_isidro.id})
        msg = str(ctx.exception)
        self.assertIn("0002", msg)
        self.assertIn("SUNAT", msg)
        self.assertIn("duplicados", msg)
        # y la asignación original NO se movió en silencio
        serie = self.Serie.search([("codigo", "=", "F001")])
        self.assertEqual(serie.establecimiento_id, self.miraflores)

    def test_unicidad_es_tambien_de_base_de_datos(self):
        """Defensa de última línea contra la carrera: aunque nadie pase por el método, la
        constraint impide dos filas con la misma serie en el RUC."""
        self.Serie.create({"codigo": "F004", "tipo_doc": "01"})
        self.env.flush_all()
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                self.Serie.create(
                    {"codigo": "F004", "tipo_doc": "01",
                     "establecimiento_id": self.san_isidro.id})
                self.env.flush_all()

    def test_reenviar_la_misma_serie_del_mismo_local_es_idempotente(self):
        a = self.Serie.l10n_pe_ne_serie_upsert(
            {"serie": "F002", "tipoDoc": "01", "establecimientoId": self.miraflores.id})
        b = self.Serie.l10n_pe_ne_serie_upsert(
            {"serie": "f002 ", "tipoDoc": "01", "establecimientoId": self.miraflores.id,
             "predeterminada": True})
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(b["predeterminada"])

    # ------------------------------------------------------------ formato/familia
    def test_prefijo_incoherente_con_tipo_doc(self):
        with self.assertRaises(ValidationError):
            self.Serie.create({"codigo": "B002", "tipo_doc": "01"})   # boleta en factura
        with self.assertRaises(ValidationError):
            self.Serie.create({"codigo": "F002", "tipo_doc": "03"})   # factura en boleta

    def test_formato_de_serie_invalido(self):
        for malo in ("X001", "F01", "F0011", "0001"):
            with self.assertRaises(ValidationError):
                with self.env.cr.savepoint():
                    self.Serie.create({"codigo": malo, "tipo_doc": "01"})

    def test_notas_admiten_las_dos_familias(self):
        """Una NC hereda la familia del documento afectado: FC02 (de factura) y BC02 (de
        boleta) son las dos legales."""
        self.Serie.create({"codigo": "FC02", "tipo_doc": "07",
                           "establecimiento_id": self.miraflores.id})
        self.Serie.create({"codigo": "BC02", "tipo_doc": "07"})

    # ------------------------------------------------- predeterminada por local
    def test_dos_predeterminadas_del_mismo_local_y_tipo(self):
        self.Serie.create({"codigo": "F002", "tipo_doc": "01", "predeterminada": True,
                           "establecimiento_id": self.miraflores.id})
        self.env.flush_all()
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                self.Serie.create({"codigo": "F005", "tipo_doc": "01", "predeterminada": True,
                                   "establecimiento_id": self.miraflores.id})
                self.env.flush_all()

    def test_predeterminada_del_domicilio_fiscal_convive_con_la_del_anexo(self):
        """El COALESCE del índice: NULL != NULL en Postgres, así que sin él el domicilio fiscal
        no tendría garantía; con él, cada local (incluido el sintético '0000') tiene la suya."""
        self.Serie.create({"codigo": "F001", "tipo_doc": "01", "predeterminada": True})
        self.Serie.create({"codigo": "F002", "tipo_doc": "01", "predeterminada": True,
                           "establecimiento_id": self.miraflores.id})
        self.env.flush_all()
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                self.Serie.create({"codigo": "F006", "tipo_doc": "01", "predeterminada": True})
                self.env.flush_all()

    def test_upsert_desmarca_la_predeterminada_anterior(self):
        """Marcar una nueva por defecto es lo que el dueño pide; el índice queda como garantía
        de carrera, no como error que él tenga que resolver."""
        vieja = self.Serie.create({"codigo": "F002", "tipo_doc": "01", "predeterminada": True,
                                   "establecimiento_id": self.miraflores.id})
        self.Serie.l10n_pe_ne_serie_upsert(
            {"serie": "F007", "tipoDoc": "01", "establecimientoId": self.miraflores.id,
             "predeterminada": True})
        self.assertFalse(vieja.predeterminada)

    # -------------------------------------------------- domicilio fiscal (D3)
    def test_serie_del_domicilio_fiscal(self):
        """'0000' sigue siendo sintético: la serie del domicilio fiscal se representa con
        establecimiento_id NULL, sin materializar ninguna fila."""
        d = self.Serie.l10n_pe_ne_serie_upsert({"serie": "F001", "tipoDoc": "01"})
        self.assertIsNone(d["establecimientoId"])
        self.assertEqual(d["establecimiento"], "0000")
        serie = self.Serie.browse(d["id"])
        self.assertFalse(serie.establecimiento_id)
        # y el payload con '0000' explícito significa lo mismo (no crea el anexo)
        d2 = self.Serie.l10n_pe_ne_serie_upsert(
            {"serie": "B001", "tipoDoc": "03", "establecimiento": "0000"})
        self.assertIsNone(d2["establecimientoId"])
        self.assertFalse(self.Estab.search([("codigo", "=", "0000")]))

    def test_establecimiento_inexistente_rebota(self):
        with self.assertRaises(UserError):
            self.Serie.l10n_pe_ne_serie_upsert(
                {"serie": "F002", "tipoDoc": "01", "establecimientoId": 999999})

    # ------------------------------------------------------------- baja lógica
    def test_toggle_desactiva_sin_borrar(self):
        d = self.Serie.l10n_pe_ne_serie_upsert(
            {"serie": "F002", "tipoDoc": "01", "establecimientoId": self.miraflores.id,
             "predeterminada": True})
        out = self.Serie.l10n_pe_ne_serie_toggle(d["id"], activa=False)
        self.assertFalse(out["activa"])
        self.assertFalse(out["predeterminada"])   # una serie apagada no puede ser la de por defecto
        self.assertTrue(self.Serie.browse(d["id"]).exists())
        # sigue en el registro (baja lógica): el listado la muestra apagada
        fila = next(f for f in self.Serie.l10n_pe_ne_serie_list() if f["serie"] == "F002")
        self.assertFalse(fila["activa"])
        # y editarla NO la revive por accidente: encenderla es una acción explícita
        self.Serie.l10n_pe_ne_serie_upsert(
            {"id": d["id"], "serie": "F002", "tipoDoc": "01",
             "establecimientoId": self.san_isidro.id})
        self.assertFalse(self.Serie.browse(d["id"]).activa)
        self.assertTrue(self.Serie.l10n_pe_ne_serie_toggle(d["id"], activa=True)["activa"])

    # -------------------------------------------------------- multi-compañía
    def test_aislamiento_por_ruc(self):
        self.Serie.create({"codigo": "F002", "tipo_doc": "01",
                           "establecimiento_id": self.miraflores.id})
        company_b = self.env["res.company"].with_context(
            l10n_pe_ne_allow_company_create=True).create({"name": "SERIES B SAC"})
        user_b = self.env["res.users"].create({
            "name": "Emisor B", "login": "emisor_b_series_s1",
            "company_id": company_b.id, "company_ids": [(6, 0, [company_b.id])],
            "group_ids": [(6, 0, [self.env.ref(
                "l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id])]})
        self.assertFalse(self.Serie.with_user(user_b).search([("codigo", "=", "F002")]))

    # ------------------------------------------------------------- muro (D6)
    def test_muro_serie_upsert_rebota_sin_el_grupo(self):
        """Un cajero factura, pero no renumera la empresa. Sin sudo() en el test: el muro real
        es el has_group DENTRO del método."""
        cajero = self._usuario("cajero_series_s1",
                               "l10n_pe_ne_biller.group_l10n_pe_ne_emisor")
        Serie = self.Serie.with_user(cajero)
        with self.assertRaises(AccessError):
            Serie.l10n_pe_ne_serie_upsert(
                {"serie": "F002", "tipoDoc": "01", "establecimientoId": self.miraflores.id})
        with self.assertRaises(AccessError):
            Serie.l10n_pe_ne_serie_toggle(1, activa=False)

    def test_muro_establecimientos_rebota_sin_el_grupo(self):
        """El agujero que cerró S1: /ne/api/establecimientos POST y DELETE no validaban nada.
        En cuanto el local determina la serie, eso era 'un cajero cambia la numeración'."""
        cajero = self._usuario("cajero_estab_s1",
                               "l10n_pe_ne_biller.group_l10n_pe_ne_emisor")
        Estab = self.Estab.with_user(cajero)
        with self.assertRaises(AccessError):
            Estab.l10n_pe_ne_upsert(
                {"codigo": "0009", "ubigeo": "150101", "direccion": "Av. Pirata"})
        with self.assertRaises(AccessError):
            Estab.l10n_pe_ne_delete_establecimiento(self.miraflores.id)
        self.assertTrue(self.miraflores.exists())

    def test_con_el_grupo_de_configuracion_si_puede(self):
        """La contracara del muro: el supervisor/dueño (que hereda este grupo) configura sin
        ser administrador de plataforma."""
        config = self._usuario("config_series_s1",
                               "l10n_pe_ne_biller.group_l10n_pe_ne_config_series")
        estab = self.Estab.with_user(config).l10n_pe_ne_upsert(
            {"codigo": "0007", "ubigeo": "150101", "direccion": "Av. Nueva 700"})
        self.assertEqual(estab.codigo, "0007")
        d = self.Serie.with_user(config).l10n_pe_ne_serie_upsert(
            {"serie": "F007", "tipoDoc": "01", "establecimientoId": estab.id})
        self.assertEqual(d["establecimiento"], "0007")

    def test_perfil_expone_la_capacidad(self):
        cajero = self._usuario("cajero_perfil_s1",
                               "l10n_pe_ne_biller.group_l10n_pe_ne_emisor")
        config = self._usuario("config_perfil_s1",
                               "l10n_pe_ne_biller.group_l10n_pe_ne_config_series")
        self.assertFalse(cajero.l10n_pe_ne_perfil()["puedeConfigSeries"])
        self.assertTrue(config.l10n_pe_ne_perfil()["puedeConfigSeries"])

    # ------------------------------------- establecimiento: archivar, no borrar
    def test_borrar_establecimiento_libre_lo_elimina(self):
        res = self.Estab.l10n_pe_ne_delete_establecimiento(self.san_isidro.id)
        self.assertEqual(res["modo"], "eliminado")
        self.assertFalse(self.san_isidro.exists())

    def test_borrar_establecimiento_con_series_archiva_y_apaga_sus_series(self):
        """Su código viaja congelado en el XML de lo ya emitido, así que se archiva. Y sus
        series se apagan: dejarlas vivas seguiría emitiendo con un local dado de baja."""
        serie = self.Serie.create(
            {"codigo": "F002", "tipo_doc": "01", "predeterminada": True,
             "establecimiento_id": self.miraflores.id})
        res = self.Estab.l10n_pe_ne_delete_establecimiento(self.miraflores.id)
        self.assertEqual(res["modo"], "archivado")
        self.assertTrue(self.miraflores.exists())
        self.assertFalse(self.miraflores.active)
        self.assertFalse(serie.activa)
        self.assertFalse(serie.predeterminada)
        # y desaparece del listado que alimenta a la SPA
        self.assertFalse(any(e["codigo"] == "0002" for e in self.Estab.l10n_pe_ne_list()))

    def test_alta_de_un_codigo_archivado_lo_reactiva(self):
        self.Serie.create({"codigo": "F002", "tipo_doc": "01",
                           "establecimiento_id": self.miraflores.id})
        self.Estab.l10n_pe_ne_delete_establecimiento(self.miraflores.id)
        rec = self.Estab.l10n_pe_ne_upsert(
            {"codigo": "0002", "ubigeo": "150122", "direccion": "Av. Larco 100"})
        self.assertEqual(rec, self.miraflores)
        self.assertTrue(rec.active)

    def test_lista_de_establecimientos_reporta_sus_series(self):
        self.Serie.create({"codigo": "F002", "tipo_doc": "01",
                           "establecimiento_id": self.miraflores.id})
        self.Serie.create({"codigo": "F001", "tipo_doc": "01"})   # domicilio fiscal
        filas = {e["codigo"]: e for e in self.Estab.l10n_pe_ne_list()}
        self.assertEqual(filas["0002"]["seriesCount"], 1)
        self.assertEqual(filas["0003"]["seriesCount"], 0)
        if "0000" in filas:   # la fila sintética solo existe si la compañía tiene dirección
            self.assertEqual(filas["0000"]["seriesCount"], 1)

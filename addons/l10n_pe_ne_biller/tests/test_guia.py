import base64
import io
import zipfile
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestGuiaBase(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Guia = self.env["l10n_pe_ne.guia_remision"]
        self.cliente = self.env["res.partner"].create({"name": "Cliente GRE", "vat": "20601030013"})
        self.producto = self.env["product.product"].create({"name": "Caja de tornillos"})

    def _vals(self, **extra):
        vals = {
            "partner_id": self.cliente.id,
            "ubigeo_partida": "150101", "dir_partida": "Av. Uno 100",
            "ubigeo_llegada": "150102", "dir_llegada": "Av. Dos 200",
            "num_placa": "ABC123", "conductor_num_doc": "12345678",
            "conductor_nombres": "Juan", "conductor_apellidos": "Pérez",
            "conductor_licencia": "Q12345678",
            "line_ids": [(0, 0, {"descripcion": "Caja de tornillos", "cantidad": 2,
                                  "product_id": self.producto.id})],
        }
        vals.update(extra)
        return vals


class TestGuiaNumeracion(TestGuiaBase):
    """Numeración de la guía. Estos tests afirman correlativos ABSOLUTOS ("T001-1"), así que
    necesitan series vírgenes: en la compañía base de una BD con guías previas, T001 ya está
    avanzada y afirmaban el estado de la BD, no el comportamiento. Una compañía propia se las
    da (la secuencia es por compañía — es justo lo que prueba test_correlativo_por_compania,
    y por eso ese pasaba mientras los demás fallaban)."""

    def setUp(self):
        super().setUp()
        self.company = self.env["res.company"].create(
            {"name": "GRE Numeracion SAC", "vat": "20999999992"})
        self.Guia = self.Guia.with_company(self.company)

    def _vals(self, **extra):
        vals = super()._vals(**extra)
        vals.setdefault("company_id", self.company.id)
        return vals

    def test_correlativo_por_serie(self):
        g1 = self.Guia.create(self._vals())
        g2 = self.Guia.create(self._vals())
        g3 = self.Guia.create(self._vals(serie="T002"))
        self.assertEqual(g1.name, "T001-1")
        self.assertEqual(g2.name, "T001-2")
        self.assertEqual(g3.name, "T002-1")  # cada serie arranca en 1

    def test_correlativo_por_compania(self):
        self.Guia.create(self._vals())  # T001-1 en la compañía base
        otra = self.env["res.company"].create({"name": "Otra Empresa SAC", "vat": "20999999991"})
        g = self.Guia.with_company(otra).create(self._vals(company_id=otra.id))
        self.assertEqual(g.name, "T001-1")  # no comparte secuencia entre RUCs

    def test_siembra_tras_correlativo_existente(self):
        # Migración: guías numeradas por la secuencia global vieja no deben colisionar.
        g_viejo = self.Guia.create(self._vals())
        g_viejo.write({"serie": "T009", "correlativo": "41", "name": "T009-41"})
        g = self.Guia.create(self._vals(serie="T009"))
        self.assertEqual(g.name, "T009-42")

    def test_batch_create_misma_serie_nueva(self):
        g1, g2 = self.Guia.create([self._vals(serie="T005"), self._vals(serie="T005")])
        self.assertEqual(g1.name, "T005-1")
        self.assertEqual(g2.name, "T005-2")

    def test_update_no_cambia_serie(self):
        g = self.Guia.create(self._vals())  # T001-1
        self.Guia.l10n_pe_ne_update_guia({"id": g.id, "serie": "T002"})
        self.assertEqual(g.serie, "T001")
        self.assertEqual(g.name, "T001-1")

    def test_indice_unico_secuencia_guia(self):
        # La carrera de creación concurrente debe morir en IntegrityError, no duplicar.
        from odoo.tools import mute_logger
        Seq = self.env["ir.sequence"].sudo()
        vals = {"name": "GRE T777 (test)", "code": "l10n_pe.ne.guia_remision.T777",
                "company_id": self.env.company.id, "padding": 1, "implementation": "no_gap"}
        Seq.create(vals)
        with mute_logger("odoo.sql_db"), self.assertRaises(Exception) as ctx:
            with self.env.cr.savepoint():
                Seq.create(dict(vals, name="GRE T777 duplicada"))
        self.assertIn("ir_sequence_gre_code_company_uniq", str(ctx.exception))


class TestGuiaMultiCompany(TestGuiaBase):
    def test_rule_aisla_companias(self):
        self.Guia.create(self._vals())
        otra = self.env["res.company"].create({"name": "Otra SAC", "vat": "20999999991"})
        user_b = self.env["res.users"].create({
            "name": "Emisor B", "login": "emisor_b_gre",
            "company_id": otra.id, "company_ids": [(6, 0, [otra.id])],
            "group_ids": [(4, self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id)],
        })
        visibles = self.Guia.with_user(user_b).with_company(otra).search([])
        self.assertFalse(visibles, "un emisor de otra compañía no debe ver estas guías")

    def test_list_filtra_por_company_activa(self):
        self.Guia.create(self._vals())
        otra = self.env["res.company"].create({"name": "Otra SAC 2", "vat": "20999999992"})
        res = self.Guia.with_company(otra).l10n_pe_ne_list_guias(offset=0)
        self.assertEqual(res["total"], 0)

    def test_rule_aisla_lineas(self):
        # La rule de las líneas también aísla (no solo la del padre).
        g = self.Guia.create(self._vals())
        otra = self.env["res.company"].create({"name": "Otra SAC 3", "vat": "20999999993"})
        user_b = self.env["res.users"].create({
            "name": "Emisor B2", "login": "emisor_b2_gre",
            "company_id": otra.id, "company_ids": [(6, 0, [otra.id])],
            "group_ids": [(4, self.env.ref("l10n_pe_ne_biller.group_l10n_pe_ne_emisor").id)],
        })
        lineas = self.env["l10n_pe_ne.guia_remision.line"].with_user(user_b).with_company(otra).search([])
        self.assertFalse(lineas, "las líneas de otra compañía no deben ser visibles")

    def test_list_filtra_con_busqueda(self):
        # El ancla de compañía debe sobrevivir cuando hay término de búsqueda (domain +=).
        self.Guia.create(self._vals())
        otra = self.env["res.company"].create({"name": "Otra SAC 4", "vat": "20999999994"})
        res = self.Guia.with_company(otra).l10n_pe_ne_list_guias(query="T001", offset=0)
        self.assertEqual(res["total"], 0)


class TestGuiaPayload(TestGuiaBase):
    def test_payload_proveedor_motivo_compra(self):
        prov = self.env["res.partner"].create({"name": "Proveedor SAC", "vat": "20507639024"})
        g = self.Guia.create(self._vals(motivo_traslado="02", proveedor_id=prov.id))
        cab = g._l10n_pe_ne_build_gre_payload()["cabecera"]
        self.assertEqual(cab["numDocProveedor"], "20507639024")
        self.assertEqual(cab["tipDocProveedor"], "6")
        self.assertEqual(cab["rznSocialProveedor"], "Proveedor SAC")

    def test_payload_sin_proveedor_no_manda_claves(self):
        g = self.Guia.create(self._vals())  # motivo 01
        cab = g._l10n_pe_ne_build_gre_payload()["cabecera"]
        self.assertNotIn("numDocProveedor", cab)


class TestGuiaValidaciones(TestGuiaBase):
    def _rechaza(self, msg_frag, **vals):
        g = self.Guia.create(self._vals(**vals))
        with self.assertRaisesRegex(UserError, msg_frag):
            g._l10n_pe_ne_validar()

    def test_peso_cero(self):
        self._rechaza("peso bruto", peso_bruto=0)

    def test_ubigeo_invalido(self):
        self._rechaza("6 dígitos", ubigeo_partida="15A")

    def test_inicio_antes_de_emision(self):
        self._rechaza("no puede ser anterior",
                      fecha_emision="2026-07-13", fecha_inicio_traslado="2026-07-10")

    def test_destinatario_doc_invalido(self):
        self.cliente.vat = "123"
        self._rechaza("RUC .* o DNI")

    def test_motivo_no_soportado(self):
        # '04' pasó a SUPPORTED_MOTIVOS (traslado entre establecimientos, ver
        # TestGuiaWizard/amendment 3c); se usa '08' (Importación), que sigue sin XML.
        self._rechaza("no soportado", motivo_traslado="08")

    def test_motivo_otros_sin_descripcion(self):
        self._rechaza("requiere describir", motivo_traslado="13")

    def test_motivo_compra_sin_proveedor(self):
        self._rechaza("requiere indicar el proveedor", motivo_traslado="02")

    def test_privado_conductor_incompleto(self):
        self._rechaza("licencia", conductor_licencia=False)

    def test_publico_transportista_sin_ruc(self):
        t = self.env["res.partner"].create({"name": "Transp", "vat": "12345678"})
        self._rechaza("RUC", modalidad_traslado="01", transportista_id=t.id)

    def test_no_reemite_aceptada(self):
        g = self.Guia.create(self._vals())
        g.estado = "enviado"
        with self.assertRaisesRegex(UserError, "ya fue emitida"):
            g._l10n_pe_ne_validar()

    def test_valida_ok(self):
        g = self.Guia.create(self._vals())
        g._l10n_pe_ne_validar()  # no lanza


class _Resp:
    def __init__(self, status=200, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


def _cdr_zip_b64(response_code="0", extra_xml=""):
    xml = (
        '<ar:ApplicationResponse xmlns:ar="urn:ar" xmlns:cbc="urn:cbc" xmlns:cac="urn:cac">'
        + extra_xml +
        '<cac:DocumentResponse><cac:Response>'
        '<cbc:ResponseCode>%s</cbc:ResponseCode>'
        '<cbc:Description>ACEPTADA</cbc:Description>'
        '</cac:Response></cac:DocumentResponse></ar:ApplicationResponse>' % response_code
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("R-20123456789-09-T001-1.xml", xml)
    return base64.b64encode(buf.getvalue()).decode()


RUTA = "odoo.addons.l10n_pe_ne_biller.models.l10n_pe_ne_guia_remision.requests"


class TestGuiaTicket(TestGuiaBase):
    def test_emitir_guarda_ticket_cuando_no_hay_cdr(self):
        g = self.Guia.create(self._vals())
        resp = _Resp(text="<DespatchAdvice/>", headers={"X-Sunat-Ticket": "156123"})
        with patch(RUTA + ".post", return_value=resp):
            g.l10n_pe_ne_emitir_guia()
        self.assertEqual(g.estado, "en_proceso")
        self.assertEqual(g.num_ticket, "156123")

    def test_consultar_ticket_aplica_cdr(self):
        g = self.Guia.create(self._vals())
        g.write({"estado": "en_proceso", "num_ticket": "156123"})
        resp = _Resp(text='{"codRespuesta":"0"}', headers={"X-Sunat-Cdr": _cdr_zip_b64("0")})
        with patch(RUTA + ".get", return_value=resp):
            g.l10n_pe_ne_consultar_ticket()
        self.assertEqual(g.estado, "enviado")
        self.assertTrue(g.l10n_pe_biller_cdr)

    def test_consultar_en_proceso_sigue_igual(self):
        g = self.Guia.create(self._vals())
        g.write({"estado": "en_proceso", "num_ticket": "156123"})
        resp = _Resp(text='{"codRespuesta":"98"}')
        with patch(RUTA + ".get", return_value=resp):
            g.l10n_pe_ne_consultar_ticket()
        self.assertEqual(g.estado, "en_proceso")

    def test_consultar_sin_ticket_rechaza(self):
        g = self.Guia.create(self._vals())
        with self.assertRaises(UserError):
            g.l10n_pe_ne_consultar_ticket()

    def test_cdr_ilegible_mantiene_en_proceso(self):
        # Un CDR corrupto NO es un rechazo de SUNAT: debe poder reintentarse.
        g = self.Guia.create(self._vals())
        g.write({"estado": "en_proceso", "num_ticket": "156123"})
        resp = _Resp(text='{"codRespuesta":"0"}', headers={"X-Sunat-Cdr": "AAAA"})  # b64 válido, zip inválido
        with patch(RUTA + ".get", return_value=resp):
            g.l10n_pe_ne_consultar_ticket()
        self.assertEqual(g.estado, "en_proceso")
        self.assertIn("ilegible", g.l10n_pe_biller_message)


class TestGuiaFechaEmision(TestGuiaBase):
    def test_emitir_estampa_fecha_y_hora_lima(self):
        g = self.Guia.create(self._vals(hora_emision="08:00:00",
                                        fecha_emision="2026-01-01",
                                        fecha_inicio_traslado="2026-12-31"))
        resp = _Resp(text="<DespatchAdvice/>", headers={"X-Sunat-Ticket": "1"})
        with patch(RUTA + ".post", return_value=resp):
            g.l10n_pe_ne_emitir_guia()
        self.assertNotEqual(str(g.fecha_emision), "2026-01-01")  # ya no es la fecha del borrador
        self.assertNotEqual(g.hora_emision, "08:00:00")

    def test_emitir_no_pisa_fecha_de_aceptada(self):
        # Una guía ya aceptada no debe ver su fecha/hora pisadas ni aunque el intento falle.
        g = self.Guia.create(self._vals(hora_emision="08:00:00", fecha_emision="2026-01-01"))
        g.write({"estado": "enviado"})
        with self.assertRaises(UserError):
            g.l10n_pe_ne_emitir_guia()
        self.assertEqual(str(g.fecha_emision), "2026-01-01")
        self.assertEqual(g.hora_emision, "08:00:00")


class TestGuiaQr(TestGuiaBase):
    def test_extrae_url_del_cdr(self):
        g = self.Guia.create(self._vals())
        g.write({"estado": "en_proceso", "num_ticket": "1"})
        nota = "|https://e-factura.sunat.gob.pe/v1/contribuyente/gem/comprobantes/xyz123|"
        cdr = _cdr_zip_b64("0", extra_xml="<cbc:Note>%s</cbc:Note>" % nota)
        resp = _Resp(text='{"codRespuesta":"0"}', headers={"X-Sunat-Cdr": cdr})
        with patch(RUTA + ".get", return_value=resp):
            g.l10n_pe_ne_consultar_ticket()
        self.assertEqual(g.l10n_pe_ne_qr_url,
                         "https://e-factura.sunat.gob.pe/v1/contribuyente/gem/comprobantes/xyz123")
        self.assertEqual(g.l10n_pe_ne_qr_data(), g.l10n_pe_ne_qr_url)

    def test_sin_cdr_no_hay_qr(self):
        g = self.Guia.create(self._vals())
        self.assertEqual(g.l10n_pe_ne_qr_data(), "")


class TestGuiaWizard(TestGuiaBase):
    def _vals_wizard(self, **extra):
        v = self._vals()
        v.pop('num_placa', None); v.pop('conductor_num_doc', None)
        v.update({
            'vehiculo_ids': [(0, 0, {'placa': 'BET714', 'principal': True,
                                     'ent_autorizacion': '06', 'num_autorizacion': '00786756'})],
            'conductor_ids': [(0, 0, {'tipo_doc': '1', 'num_doc': '71958406', 'nombres': 'Hernan',
                                      'apellidos': 'Vilca', 'licencia': 'U71958406', 'principal': True})],
            'ind_retorno_vacio': True, 'cod_estab_partida': '0000',
        })
        v.update(extra)
        return v

    def test_payload_wizard_completo(self):
        g = self.Guia.create(self._vals_wizard(ind_transbordo=True))
        p = g._l10n_pe_ne_build_gre_payload()
        cab = p['cabecera']
        self.assertEqual(cab['indTransbordoProgDatosEnvio'], '1')
        self.assertEqual(cab['indRetornoVehiculoVacio'], '1')
        self.assertNotIn('indTrasladoVehiculoM1L', cab)     # apagado = ausente
        self.assertEqual(cab['codEstabPartida'], '0000')
        self.assertEqual(cab['numPlacaTransPrivado'], 'BET714')  # principal alimenta el legado
        self.assertEqual(cab['entAutorizacionVehiculoPrincipal'], '06')

    def test_payload_secundarios(self):
        v = self._vals_wizard()
        v['vehiculo_ids'].append((0, 0, {'placa': 'XYZ999', 'principal': False}))
        v['conductor_ids'].append((0, 0, {'tipo_doc': '1', 'num_doc': '12345678', 'nombres': 'Juan',
                                          'apellidos': 'Quispe', 'licencia': 'Q12345678', 'principal': False}))
        g = self.Guia.create(v)
        p = g._l10n_pe_ne_build_gre_payload()
        self.assertEqual(p['cabecera']['vehiculosSecundarios'], [
            {'numPlaca': 'XYZ999', 'entAutorizacion': '', 'numAutorizacion': ''}])
        self.assertEqual(p['cabecera']['conductoresSecundarios'][0]['numDoc'], '12345678')

    def test_max_dos_secundarios(self):
        v = self._vals_wizard()
        for i in range(3):
            v['vehiculo_ids'].append((0, 0, {'placa': 'S%03d' % i, 'principal': False}))
        g = self.Guia.create(v)
        with self.assertRaisesRegex(UserError, 'máximo 2'):
            g._l10n_pe_ne_validar()

    def test_compat_legado(self):
        g = self.Guia.create(self._vals())  # payload viejo con num_placa/conductor_*
        p = g._l10n_pe_ne_build_gre_payload()
        self.assertEqual(p['cabecera']['numPlacaTransPrivado'], 'ABC123')

    def test_multiples_comprobantes(self):
        v = self._vals_wizard()
        g = self.Guia.create(v)
        d = g.l10n_pe_ne_guia_detalle()
        self.assertIn('comprobanteIds', d)
        self.assertIn('vehiculos', d)
        self.assertTrue(d['vehiculos'][0]['principal'])

    # ---------------------------------------------------- 3a: modalidad 01
    def test_publico_sin_fecha_entrega_transportista(self):
        t = self.env['res.partner'].create({'name': 'Transportista GRE', 'vat': '20100190797'})
        g = self.Guia.create(self._vals(modalidad_traslado='01', transportista_id=t.id))
        with self.assertRaisesRegex(UserError, 'entrega'):
            g._l10n_pe_ne_validar()

    # ------------------------------------------------------ 3b: motivo 02
    def test_compra_con_estab_partida_rechaza(self):
        prov = self.env['res.partner'].create({'name': 'Proveedor GRE', 'vat': '20507639024'})
        g = self.Guia.create(self._vals(motivo_traslado='02', proveedor_id=prov.id,
                                        cod_estab_partida='0001'))
        with self.assertRaisesRegex(UserError, 'no admite establecimiento'):
            g._l10n_pe_ne_validar()

    # ------------------------------------------------------ 3c: motivo 04
    def test_motivo_04_sin_estab_rechaza(self):
        g = self.Guia.create(self._vals(motivo_traslado='04'))
        with self.assertRaisesRegex(UserError, 'establecimiento en partida y llegada'):
            g._l10n_pe_ne_validar()

    def test_motivo_04_con_ambos_estab_pasa(self):
        # F4: rucEstabPartida/rucEstabLlegada exigen company.vat configurado.
        self.env.company.vat = '20601030013'
        g = self.Guia.create(self._vals(motivo_traslado='04', cod_estab_partida='0000',
                                        cod_estab_llegada='0001'))
        g._l10n_pe_ne_validar()  # no lanza
        cab = g._l10n_pe_ne_build_gre_payload()['cabecera']
        self.assertEqual(cab['rucEstabPartida'], '20601030013')
        self.assertEqual(cab['rucEstabLlegada'], '20601030013')

    # ---------------------------------------------------- exención M1L
    def test_m1l_sin_vehiculo_ni_conductor_pasa(self):
        g = self.Guia.create(self._vals(
            modalidad_traslado='02', ind_m1l=True,
            num_placa=False, conductor_num_doc=False, conductor_nombres=False,
            conductor_apellidos=False, conductor_licencia=False,
        ))
        g._l10n_pe_ne_validar()  # no lanza

    # ------------------------------------------------- ambigüedad de principal
    def test_dos_vehiculos_principales_rechaza(self):
        v = self._vals_wizard()
        v['vehiculo_ids'].append((0, 0, {'placa': 'XYZ999', 'principal': True}))
        g = self.Guia.create(v)
        with self.assertRaisesRegex(UserError, 'un vehículo principal'):
            g._l10n_pe_ne_validar()

    # --------------------------------------------------------- F1 regresión
    def test_conductor_vacio_con_vehiculo_en_lista_rechaza(self):
        # Antes bastaba con que el LADO vehículo tuviera datos (en cualquier
        # representación) para que el lado conductor se colara vacío hasta el biller.
        g = self.Guia.create(self._vals(
            modalidad_traslado='02',
            num_placa=False, conductor_num_doc=False, conductor_nombres=False,
            conductor_apellidos=False, conductor_licencia=False,
            vehiculo_ids=[(0, 0, {'placa': 'BET714', 'principal': True})],
        ))
        with self.assertRaisesRegex(UserError, 'conductor'):
            g._l10n_pe_ne_validar()

    # --------------------------------------------------------- F2 regresión
    def test_header_vals_vehiculo_sin_placa_rechaza(self):
        with self.assertRaisesRegex(UserError, 'placa'):
            self.Guia._l10n_pe_ne_guia_header_vals({'vehiculos': [{'principal': True}]})

    def test_header_vals_conductor_incompleto_rechaza(self):
        with self.assertRaisesRegex(UserError, 'conductor'):
            self.Guia._l10n_pe_ne_guia_header_vals(
                {'conductores': [{'nombres': 'Juan', 'principal': True}]})

    # --------------------------------------------------------- F4 regresión
    def test_estab_partida_sin_vat_compania_rechaza(self):
        self.env.company.vat = False
        g = self.Guia.create(self._vals(cod_estab_partida='0000'))
        with self.assertRaisesRegex(UserError, 'RUC de la compañía'):
            g._l10n_pe_ne_build_gre_payload()

    # ------------------------------------------ comprobante no emitido (silent-drop)
    def test_comprobante_relacionado_no_emitido_rechaza(self):
        # Un account.move vinculado que nunca pasó por la emisión de este addon (sin
        # l10n_pe_ne_serie_emit) sería descartado en silencio de docRelacionado por
        # _l10n_pe_ne_build_gre_payload, mientras el PDF mostraría igual una fila para
        # él. _l10n_pe_ne_validar debe rechazarlo con un mensaje explícito.
        igv = self.env['account.tax'].search([
            ('company_id', '=', self.env.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        partner = self.env['res.partner'].create({
            'name': 'CLIENTE GRE SAC', 'vat': '20605145648',
            'l10n_latam_identification_type_id': ruc_type.id})
        move = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': partner.id, 'invoice_date': '2026-06-19',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '1',
            'invoice_line_ids': [(0, 0, {'product_id': self.producto.id, 'quantity': 1.0,
                                         'price_unit': 7.20, 'tax_ids': [(6, 0, igv.ids)]})]})
        move.action_post()  # posteado pero jamás emitido por este addon: sin serie_emit
        g = self.Guia.create(self._vals(comprobante_ids=[(6, 0, [move.id])]))
        with self.assertRaisesRegex(UserError, 'no ha sido emitido'):
            g._l10n_pe_ne_validar()

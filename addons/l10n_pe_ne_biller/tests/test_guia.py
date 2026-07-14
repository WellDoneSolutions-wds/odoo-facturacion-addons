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
        self._rechaza("no soportado", motivo_traslado="04")

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

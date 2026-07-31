from odoo.tests import TransactionCase, tagged
from .common import L10nPeSeedMixin


@tagged('post_install', '-at_install')
class TestOrdenCompra(L10nPeSeedMixin, TransactionCase):
    """Orden de compra del cliente → cac:OrderReference (documento relacionado indDocRelacionado 3).

    Debe ir PRIMERO en la lista de relacionados: en el UBL Invoice el OrderReference precede a
    DespatchDocumentReference (guía) y AdditionalDocumentReference (anticipos/otros), que es el
    orden de elementos que exige el XSD de SUNAT."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20100070970',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create({'name': 'SERVICIO', 'default_code': 'S1'})

    def _move(self, **vals):
        base = {
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'l10n_pe_serie': 'F001', 'l10n_pe_correlativo': '9',
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 500.0, 'tax_ids': [(6, 0, self.igv.ids)]})]}
        base.update(vals)
        move = self.env['account.move'].create(base)
        move.action_post()
        return move

    def test_orden_compra_emite_relacionado_ind3(self):
        payload = self._move(l10n_pe_ne_orden_compra='OC-2026-000457')._l10n_pe_build_invoice_request()
        rel = payload['relacionados'][0]
        self.assertEqual(rel['indDocRelacionado'], '3')
        self.assertEqual(rel['numDocRelacionado'], 'OC-2026-000457')
        self.assertEqual(rel['numDocEmisor'], self.company.vat)

    def test_orden_compra_va_antes_de_la_guia(self):
        # OC + guía juntas: el OrderReference (OC) debe ir ANTES que el DespatchDocumentReference
        # (guía) por el orden de elementos del UBL Invoice.
        payload = self._move(
            l10n_pe_ne_orden_compra='OC-1',
            l10n_pe_ne_guia_ref='T001-00000123')._l10n_pe_build_invoice_request()
        inds = [r['indDocRelacionado'] for r in payload['relacionados']]
        self.assertEqual(inds, ['3', '1'])  # OC primero, guía después

    def test_sin_orden_compra_no_emite_ind3(self):
        payload = self._move()._l10n_pe_build_invoice_request()
        rels = payload.get('relacionados', [])
        self.assertEqual([r for r in rels if r['indDocRelacionado'] == '3'], [])

    def test_venta_estado_emite_4_propiedades_cat55(self):
        # Ventas al Estado: las 4 propiedades cat-55 (5000-5003) como cac:AdditionalItemProperty,
        # con codTipoVariable "-" para no disparar el descuento por ítem.
        payload = self._move(
            l10n_pe_ne_estado_expediente='EXP-1',
            l10n_pe_ne_estado_unidad_ejecutora='UE-1',
            l10n_pe_ne_estado_proceso_seleccion='PS-1',
            l10n_pe_ne_estado_contrato='CTO-1')._l10n_pe_build_invoice_request()
        props = [d for d in payload['adicionalDetalle'] if d.get('nomPropiedad', '-') != '-']
        self.assertEqual(sorted(p['codPropiedad'] for p in props), ['5000', '5001', '5002', '5003'])
        for p in props:
            self.assertEqual(p['codTipoVariable'], '-')
            self.assertEqual(p['idLinea'], '1')  # una sola línea de producto

    def test_venta_estado_es_todo_o_nada(self):
        # SUNAT 3146-3149 exige las 4 juntas: con 3 de 4 no se emite ninguna.
        payload = self._move(
            l10n_pe_ne_estado_expediente='EXP-1',
            l10n_pe_ne_estado_unidad_ejecutora='UE-1',
            l10n_pe_ne_estado_proceso_seleccion='PS-1')._l10n_pe_build_invoice_request()
        props = [d for d in payload.get('adicionalDetalle', []) if d.get('nomPropiedad', '-') != '-']
        self.assertEqual(props, [])

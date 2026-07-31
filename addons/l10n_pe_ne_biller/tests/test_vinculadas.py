import base64

from odoo.tests import TransactionCase, tagged

from .common import L10nPeSeedMixin


@tagged('post_install', '-at_install')
class TestVinculadas(L10nPeSeedMixin, TransactionCase):
    """Partes vinculadas — precios de transferencia (V1–V4).

    V3 · tipo de vínculo + no domiciliada (derivada del país).
    V2 · aviso L1 de valor de mercado al emitir a una parte vinculada.
    V4 · umbrales de obligación de la DJ (2300 UIT ingresos / 400 UIT operaciones).
    V1 · reporte de operaciones con vinculadas (sustento del Reporte Local).
    """

    def setUp(self):
        super().setUp()  # RUC + IGV (self.igv)
        self.Move = self.env['account.move']
        self.Partner = self.env['res.partner']
        self.ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.pas_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '7')], limit=1)  # 7 = Pasaporte (no domiciliado)
        self.product = self.env['product.product'].create({'name': 'BIEN', 'default_code': 'B1'})
        self.env.company.l10n_pe_ne_uit = 1.0  # UIT chica → umbrales bajos (2300 / 400) para el test

    @staticmethod
    def _ruc(base10):
        """RUC de 11 dígitos con dígito verificador válido (mod-11) desde 10 dígitos base;
        evita que base_vat rechace el partner al setearle país (validación del RUC peruano)."""
        weights = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
        s = sum(int(d) * w for d, w in zip(base10, weights))
        r = 11 - (s % 11)
        dv = 0 if r == 10 else (1 if r == 11 else r)
        return base10 + str(dv)

    def _partner(self, name, base10, vinculada=False, tipo=None, pais=None):
        # Extranjero (país ≠ PE): pasaporte + vat alfanumérico (no domiciliado, sin RUC peruano).
        # Domiciliado / sin país: RUC con verificador válido.
        foreign = bool(pais) and pais != 'PE'
        vals = {'name': name, 'l10n_pe_ne_parte_vinculada': vinculada}
        if foreign:
            vals.update({'l10n_latam_identification_type_id': self.pas_type.id,
                         'vat': 'X' + base10[:9]})
        else:
            vals.update({'l10n_latam_identification_type_id': self.ruc_type.id,
                         'vat': self._ruc(base10)})
        if tipo:
            vals['l10n_pe_ne_tipo_vinculo'] = tipo
        if pais:
            vals['country_id'] = self.env['res.country'].search([('code', '=', pais)], limit=1).id
        return self.Partner.create(vals)

    _corr = 0

    def _emitido(self, partner, base, refund=False):
        type(self)._corr += 1
        m = self.Move.create({
            'move_type': 'out_refund' if refund else 'out_invoice',
            'partner_id': partner.id, 'invoice_date': '2026-06-15',
            'l10n_pe_serie': 'FC01' if refund else 'F001',
            'l10n_pe_correlativo': str(type(self)._corr),
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': base, 'tax_ids': [(6, 0, self.igv.ids)]})],
        })
        m.action_post()
        m.sudo().l10n_pe_biller_state = 'enviado'  # simula aceptado (el reporte excluye no emitidos)
        return m

    # ---- V3 · tipo de vínculo + no domiciliada -------------------------------------------
    def test_tipo_vinculo_round_trip_en_el_dict(self):
        res = self.Move.l10n_pe_ne_create_cliente({
            'tipoDoc': '6', 'numDoc': '20100070970', 'razonSocial': 'GRUPO SA',
            'parteVinculada': True, 'tipoVinculo': '01'})
        self.assertTrue(res['parteVinculada'])
        self.assertEqual(res['tipoVinculo'], '01')
        self.assertFalse(res['noDomiciliada'])  # sin país o PE → domiciliada

    def test_no_domiciliada_se_deriva_del_pais(self):
        p = self._partner('MATRIZ INC', '2010007097', vinculada=True, tipo='05', pais='US')
        self.assertTrue(p.l10n_pe_ne_no_domiciliada)
        pe = self._partner('LOCAL SA', '2010007098', vinculada=True, tipo='01', pais='PE')
        self.assertFalse(pe.l10n_pe_ne_no_domiciliada)

    # ---- V2 · aviso de valor de mercado --------------------------------------------------
    def test_aviso_valor_mercado_a_parte_vinculada(self):
        p = self._partner('VINCULADA SA', '2010007099', vinculada=True, tipo='01')
        m = self._emitido(p, 100)
        avisos = [f for f in m._l10n_pe_ne_validaciones() if f['code'] == 'vinculada-valor-mercado']
        self.assertEqual(len(avisos), 1)
        self.assertEqual(avisos[0]['nivel'], 'aviso')  # no bloquea

    def test_sin_vinculo_no_avisa(self):
        p = self._partner('TERCERO SA', '2010007100')
        m = self._emitido(p, 100)
        self.assertNotIn('vinculada-valor-mercado',
                         {f['code'] for f in m._l10n_pe_ne_validaciones()})

    # ---- V4 · umbrales de obligación -----------------------------------------------------
    def test_umbrales_marcan_obligacion(self):
        vinc = self._partner('VINC SA', '2010007101', vinculada=True, tipo='01')
        tercero = self._partner('OTRO SA', '2010007102')
        self._emitido(vinc, 3000)     # operación con vinculada (> 400 UIT) y suma a ingresos
        self._emitido(tercero, 2000)  # solo ingresos
        u = self.env['res.company'].l10n_pe_ne_vinculadas_umbrales(2026)
        self.assertEqual(u['ejercicio'], 2026)
        self.assertGreater(u['ingresos'], u['umbralReporteLocal'])       # ingresos > 2300 UIT
        self.assertGreater(u['operacionesVinculadas'], u['umbralOperaciones'])  # > 400 UIT
        self.assertTrue(u['obligadoReporteLocal'])

    def test_notas_credito_restan_de_las_operaciones(self):
        vinc = self._partner('VINC2 SA', '2010007103', vinculada=True, tipo='01')
        self._emitido(vinc, 1000)
        self._emitido(vinc, 300, refund=True)  # NC resta
        u = self.env['res.company'].l10n_pe_ne_vinculadas_umbrales(2026)
        # 1180 (con IGV) − 354 (NC con IGV) = 826 aprox.
        self.assertAlmostEqual(u['operacionesVinculadas'], 1180.0 - 354.0, delta=0.02)

    # ---- V1 · reporte de vinculadas ------------------------------------------------------
    def test_reporte_agrupa_por_cliente_vinculado(self):
        vinc = self._partner('VINC3 SA', '2010007104', vinculada=True, tipo='03', pais='CL')
        tercero = self._partner('NORMAL SA', '2010007105')
        self._emitido(vinc, 500)
        self._emitido(vinc, 700)
        self._emitido(tercero, 900)  # NO vinculada → no aparece
        rep = self.Move.l10n_pe_ne_reporte_vinculadas(2026)
        self.assertEqual(rep['clientes'], 1)
        self.assertEqual(rep['comprobantes'], 2)
        item = rep['items'][0]
        self.assertEqual(item['cliente'], 'VINC3 SA')
        self.assertEqual(item['tipoVinculo'], '03')
        self.assertTrue(item['noDomiciliada'])  # país CL
        self.assertAlmostEqual(item['total'], round((500 + 700) * 1.18, 2), delta=0.02)
        self.assertIn('umbrales', rep)  # cruza con V4

    # ---- Export DJ (CSV) -----------------------------------------------------------------
    def test_export_csv_lleva_cabecera_de_umbrales_y_la_tabla(self):
        vinc = self._partner('VINC-CSV SA', '2010007106', vinculada=True, tipo='03', pais='CL')
        self._emitido(vinc, 500)
        self._emitido(vinc, 700)
        res = self.Move.l10n_pe_ne_reporte_vinculadas_csv(2026)
        self.assertEqual(res['count'], 1)
        self.assertTrue(res['filename'].endswith('-2026.csv'))
        csv = base64.b64decode(res['contentB64']).decode('utf-8')
        self.assertTrue(csv.startswith('﻿'))                 # BOM para Excel
        self.assertIn('Reporte de operaciones con partes vinculadas', csv)
        self.assertIn('Obligado a presentar Reporte Local', csv)
        self.assertIn('VINC-CSV SA', csv)                          # fila del cliente
        self.assertIn('%.2f' % round((500 + 700) * 1.18, 2), csv)  # total del cliente
        self.assertIn('TOTAL', csv)                                # fila de totales

    def test_export_csv_vacio_no_revienta(self):
        res = self.Move.l10n_pe_ne_reporte_vinculadas_csv(2026)
        self.assertEqual(res['count'], 0)
        csv = base64.b64decode(res['contentB64']).decode('utf-8')
        self.assertIn('Documento', csv)   # la tabla existe aunque no haya filas

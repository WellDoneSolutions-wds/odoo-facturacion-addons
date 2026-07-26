import re

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestBillerSerie(TransactionCase):
    """Serie por diario + correlativo auto-incremental (del folio del número de asiento), con
    override manual."""

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.igv = self.env['account.tax'].search([
            ('company_id', '=', self.company.id), ('type_tax_use', '=', 'sale'),
            ('l10n_pe_edi_tax_code', '=', '1000')], limit=1)
        self.journal = self.env['account.journal'].search([
            ('company_id', '=', self.company.id), ('type', '=', 'sale')], limit=1)
        ruc_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '6')], limit=1)
        self.partner = self.env['res.partner'].create({
            'name': 'CLIENTE SAC', 'vat': '20605145648',
            'l10n_latam_identification_type_id': ruc_type.id})
        self.product = self.env['product.product'].create({'name': 'PROD', 'default_code': 'P1'})

    def _move(self, **kw):
        vals = {
            'move_type': 'out_invoice', 'partner_id': self.partner.id, 'invoice_date': '2026-06-20',
            'journal_id': self.journal.id,
            'invoice_line_ids': [(0, 0, {'product_id': self.product.id, 'quantity': 1.0,
                                         'price_unit': 10.0, 'tax_ids': [(6, 0, self.igv.ids)]})]}
        vals.update(kw)
        return self.env['account.move'].create(vals)

    def test_serie_por_diario(self):
        """La serie del move se toma del diario (l10n_pe_ne_serie) por defecto."""
        self.journal.l10n_pe_ne_serie = 'F999'
        move = self._move()
        self.assertEqual(move.l10n_pe_serie, 'F999')

    def test_correlativo_del_folio(self):
        """Sin correlativo manual, se usa el folio (auto-incremental) del número del asiento."""
        self.journal.l10n_pe_ne_serie = 'F001'
        move = self._move()
        move.action_post()
        folio = re.findall(r'\d+', (move.name or '').replace(' ', ''))[-1]
        payload = move._l10n_pe_build_invoice_request()
        self.assertEqual(payload['id']['serie'], 'F001')
        self.assertEqual(payload['id']['correlativo'], folio.zfill(8))

    def test_correlativo_manual_override(self):
        """El correlativo manual tiene prioridad sobre el folio."""
        move = self._move(l10n_pe_serie='F777', l10n_pe_correlativo='7')
        move.action_post()
        payload = move._l10n_pe_build_invoice_request()
        self.assertEqual(payload['id']['serie'], 'F777')
        self.assertEqual(payload['id']['correlativo'], '00000007')

    def test_serie_boleta_ajusta_letra(self):
        """Cliente sin RUC (boleta): la serie por defecto del diario (F…) cambia a B…."""
        dni_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '1')], limit=1)
        if not dni_type:
            self.skipTest('sin tipo de documento DNI en la localización')
        consumidor = self.env['res.partner'].create({
            'name': 'CONSUMIDOR FINAL', 'vat': '45678912',
            'l10n_latam_identification_type_id': dni_type.id})
        self.journal.l10n_pe_ne_serie = 'F001'
        move = self._move(partner_id=consumidor.id)
        self.assertEqual(move._l10n_pe_document_type(), '03')
        self.assertEqual(move.l10n_pe_serie, 'B001')

    def test_serie_boleta_cliente_ruc_tipo_elegido(self):
        """Cliente RUC pero tipo Boleta elegido en el comprobante: la serie también pasa a B…."""
        self.journal.l10n_pe_ne_serie = 'F001'
        boleta_type = self.env.ref('l10n_pe.document_type02')
        move = self._move(l10n_latam_document_type_id=boleta_type.id)
        self.assertEqual(move._l10n_pe_document_type(), '03')
        self.assertEqual(move.l10n_pe_serie, 'B001')

    def test_serie_familia_equivocada_bloquea_emision(self):
        """Serie F… en una boleta (o B… en factura) corta la emisión antes de ir a SUNAT."""
        from odoo.exceptions import UserError
        dni_type = self.env['l10n_latam.identification.type'].search(
            [('l10n_pe_vat_code', '=', '1')], limit=1)
        if not dni_type:
            self.skipTest('sin tipo de documento DNI en la localización')
        consumidor = self.env['res.partner'].create({
            'name': 'CONSUMIDOR FINAL DOS', 'vat': '45678913',
            'l10n_latam_identification_type_id': dni_type.id})
        boleta = self._move(partner_id=consumidor.id, l10n_pe_serie='F001',
                            l10n_pe_correlativo='9')
        boleta.action_post()
        with self.assertRaises(UserError):
            boleta._l10n_pe_target()
        factura = self._move(l10n_pe_serie='B001', l10n_pe_correlativo='9')
        factura.action_post()
        with self.assertRaises(UserError):
            factura._l10n_pe_target()

    def _asigna(self, serie):
        """Simula el chokepoint de emisión: postea y fija la identidad fiscal (sin ir a SUNAT)."""
        move = self._move(l10n_pe_serie=serie)
        move.action_post()
        move._l10n_pe_ne_assign_numero()
        return move

    def test_correlativo_por_serie_no_comparte_contador(self):
        """El correlativo es POR SERIE: intercalar emisiones de F888 y F777 no crea huecos en
        ninguna. Antes el folio del diario era un contador global compartido entre series (F001
        se saltaba números cuando una boleta/nota tomaba el correlativo intermedio)."""
        a1 = self._asigna('F888')
        b1 = self._asigna('F777')
        a2 = self._asigna('F888')
        a3 = self._asigna('F888')
        b2 = self._asigna('F777')
        self.assertEqual((a1.l10n_pe_ne_serie_emit, a1.l10n_pe_ne_corr_emit), ('F888', '00000001'))
        self.assertEqual(a2.l10n_pe_ne_corr_emit, '00000002')
        self.assertEqual(a3.l10n_pe_ne_corr_emit, '00000003')  # consecutivo pese a las F777 en medio
        self.assertEqual((b1.l10n_pe_ne_serie_emit, b1.l10n_pe_ne_corr_emit), ('F777', '00000001'))
        self.assertEqual(b2.l10n_pe_ne_corr_emit, '00000002')

    def test_asignar_numero_es_idempotente(self):
        """Fijar el número dos veces no lo avanza: la identidad fiscal se asigna una sola vez."""
        move = self._asigna('F888')
        corr = move.l10n_pe_ne_corr_emit
        move._l10n_pe_ne_assign_numero()
        self.assertEqual(move.l10n_pe_ne_corr_emit, corr)
        # y _l10n_pe_serie_correlativo devuelve el valor CONGELADO, no el folio del asiento
        self.assertEqual(move._l10n_pe_serie_correlativo(), ('F888', str(int(corr))))

    def test_correlativo_manual_sigue_teniendo_prioridad(self):
        """Un correlativo manual se RESPETA (no lo pisa la secuencia). Y como queda EMITIDO en la
        serie, siembra la secuencia automática: la auto continúa DESPUÉS del manual, evitando
        colisión (reiniciar en 1 chocaría con el F888-500 manual). Mismo principio de sembrado que
        test_secuencia_siembra_desde_lo_ya_emitido."""
        manual = self._move(l10n_pe_serie='F888', l10n_pe_correlativo='500')
        manual.action_post()
        manual._l10n_pe_ne_assign_numero()
        self.assertEqual(manual.l10n_pe_ne_corr_emit, '00000500')
        # El manual quedó emitido (F888-500): la auto siembra tras él → 501 (no reinicia en 1 ni
        # colisiona con el manual).
        auto = self._asigna('F888')
        self.assertEqual(auto.l10n_pe_ne_corr_emit, '00000501')

    def test_secuencia_siembra_desde_lo_ya_emitido(self):
        """Al primer uso, la secuencia por serie se siembra tras el correlativo más alto ya
        emitido en esa serie (migración transparente desde el folio global previo)."""
        previo = self._move(l10n_pe_serie='F888')
        previo.action_post()
        # simula un histórico emitido con corr 42 (como los que dejó el folio global)
        previo.l10n_pe_ne_serie_emit = 'F888'
        previo.l10n_pe_ne_corr_emit = '00000042'
        nuevo = self._asigna('F888')
        self.assertEqual(nuevo.l10n_pe_ne_corr_emit, '00000043')

    def test_serie_no_habilitada_bloquea(self):
        """QA-074: emitir con una serie inventada (no configurada ni default) se bloquea antes de
        asignar correlativo; una serie por defecto (F001) sí pasa."""
        from odoo.exceptions import UserError
        self.journal.l10n_pe_ne_serie = 'F001'
        inventada = self._move(l10n_pe_serie='F099')
        inventada.action_post()
        self.assertEqual(inventada.l10n_pe_serie, 'F099')
        with self.assertRaises(UserError):
            inventada._l10n_pe_check_serie()
        ok = self._move()   # toma F001 del diario
        ok.action_post()
        ok._l10n_pe_check_serie()   # no levanta

    def test_serie_configurada_en_diario_habilitada(self):
        """Una serie no-default pero configurada en un diario de venta sí está habilitada."""
        self.journal.l10n_pe_ne_serie = 'F050'
        move = self._move(l10n_pe_serie='F050')
        move.action_post()
        move._l10n_pe_check_serie()   # no levanta
        self.assertIn('F050', move._l10n_pe_ne_series_habilitadas())

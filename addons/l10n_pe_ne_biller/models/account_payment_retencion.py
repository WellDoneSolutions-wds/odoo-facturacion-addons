import base64
import io
import re
import zipfile

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Régimen de Retenciones del IGV (cat. 23 de SUNAT): 01 = tasa 3%. MVP con el régimen general;
# otros regímenes/tasas se parametrizarían a futuro (campo en la compañía o el pago).
RETENCION_REGIMEN = '01'
RETENCION_TASA = 3.0

# Régimen de Percepciones (cat. 22): 01 = venta interna, tasa 2%.
PERCEPCION_REGIMEN = '01'
PERCEPCION_TASA = 2.0


def _l10n_pe_parse_cdr_zip(cdr_bytes):
    """Lee (ResponseCode, Description) del ApplicationResponse dentro del zip del CDR."""
    code = desc = ''
    try:
        with zipfile.ZipFile(io.BytesIO(cdr_bytes)) as zf:
            xml_name = next((n for n in zf.namelist() if n.lower().endswith('.xml')), None)
            content = zf.read(xml_name) if xml_name else b''
        m = re.search(rb'<cbc:ResponseCode>([^<]*)</cbc:ResponseCode>', content)
        code = m.group(1).decode() if m else ''
        m = re.search(rb'<cbc:Description>([^<]*)</cbc:Description>', content)
        desc = m.group(1).decode('utf-8', 'replace') if m else ''
    except Exception:
        pass
    return code, desc


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    l10n_pe_ret_state = fields.Selection(
        selection=[('por_enviar', 'Por enviar'), ('enviado', 'Enviado'),
                   ('rechazado', 'Rechazado'), ('error', 'Error')],
        string='Estado Retención', default='por_enviar', copy=False)
    l10n_pe_ret_serie = fields.Char(string='Serie Retención', default='R001', copy=False)
    l10n_pe_ret_correlativo = fields.Char(string='Correlativo Retención', copy=False)
    l10n_pe_ret_xml = fields.Many2one('ir.attachment', string='XML Retención', copy=False)
    l10n_pe_ret_cdr = fields.Many2one('ir.attachment', string='CDR Retención', copy=False)
    l10n_pe_ret_pdf = fields.Many2one('ir.attachment', string='PDF Retención', copy=False)
    l10n_pe_ret_message = fields.Text(string='Mensaje Retención', copy=False)

    l10n_pe_per_state = fields.Selection(
        selection=[('por_enviar', 'Por enviar'), ('enviado', 'Enviado'),
                   ('rechazado', 'Rechazado'), ('error', 'Error')],
        string='Estado Percepción', default='por_enviar', copy=False)
    l10n_pe_per_serie = fields.Char(string='Serie Percepción', default='P001', copy=False)
    l10n_pe_per_correlativo = fields.Char(string='Correlativo Percepción', copy=False)
    l10n_pe_per_xml = fields.Many2one('ir.attachment', string='XML Percepción', copy=False)
    l10n_pe_per_cdr = fields.Many2one('ir.attachment', string='CDR Percepción', copy=False)
    l10n_pe_per_pdf = fields.Many2one('ir.attachment', string='PDF Percepción', copy=False)
    l10n_pe_per_message = fields.Text(string='Mensaje Percepción', copy=False)

    # --------------------------------------------------------------- helpers
    def _l10n_pe_ret_aplica(self):
        """La retención del IGV aplica a pagos salientes a proveedores con facturas reconciliadas."""
        self.ensure_one()
        return (self.payment_type == 'outbound' and self.partner_type == 'supplier'
                and self.reconciled_bill_ids)

    @staticmethod
    def _l10n_pe_ret_fmt(amount):
        return "%.2f" % (amount or 0.0)

    def _l10n_pe_ret_doc_relacionado(self, bill):
        """(tipo, número) del comprobante del proveedor que se retiene."""
        tipo = (bill.l10n_latam_document_type_id.code or '01') if hasattr(bill, 'l10n_latam_document_type_id') else '01'
        numero = bill.l10n_latam_document_number or bill.ref or bill.name or ''
        return tipo, numero

    def _l10n_pe_emisor(self):
        """Datos de empresa del emisor (desde res.company) para el request; datos NO secretos.
        Las credenciales y el certificado quedan en el servidor indexados por RUC."""
        self.ensure_one()
        company = self.company_id
        partner = company.partner_id
        emisor = {
            "razonSocial": company.name or "",
            "nombreComercial": company.name or "",
        }
        # Dirección todo-o-nada: solo si el distrito (ubigeo) está configurado (ver account_move_biller).
        distrito = partner.l10n_pe_district
        if distrito:
            emisor["direccion"] = {
                "ubigeo": distrito.code or "",
                "direccion": partner.street or "",
                "departamento": partner.state_id.name or "",
                "provincia": (distrito.city_id.name or partner.city or ""),
                "distrito": distrito.name or "",
                "urbanizacion": partner.street2 or "",
            }
        return emisor

    def _l10n_pe_ret_payload(self):
        self.ensure_one()
        fmt = self._l10n_pe_ret_fmt
        partner = self.partner_id
        fecha = (self.date or fields.Date.context_today(self)).strftime("%Y-%m-%d")
        moneda = self.currency_id.name or "PEN"

        detalle = []
        total_ret = total_neto = 0.0
        for bill in self.reconciled_bill_ids:
            tot = bill.amount_total
            ret = round(tot * RETENCION_TASA / 100.0, 2)
            neto = round(tot - ret, 2)
            total_ret += ret
            total_neto += neto
            tipo, numero = self._l10n_pe_ret_doc_relacionado(bill)
            bmon = bill.currency_id.name or "PEN"
            detalle.append({
                "tipDocRelacionado": tipo,
                "nroDocRelacionado": numero,
                "fecEmiDocRelacionado": (bill.invoice_date or self.date).strftime("%Y-%m-%d"),
                "mtoImpTotDocRelacionado": fmt(tot),
                "tipMonDocRelacionado": bmon,
                "fecPagDocRelacionado": fecha,
                "nroPagDocRelacionado": "1",
                "mtoPagDocRelacionado": fmt(tot),
                "tipMonPagDocRelacionado": bmon,
                "mtoRetDocRelacionado": fmt(ret),
                "tipMonRetDocRelacionado": "PEN",
                "fecRetDocRelacionado": fecha,
                "mtoTotPagNetoDocRelacionado": fmt(neto),
                "tipMonTotPagNetoDocRelacionado": bmon,
                "tipMonRefTipCambio": "PEN", "tipMonObjTipCambio": "PEN",
                "facTipCambio": "", "fecTipCambio": "",
            })

        cabecera = {
            "fecEmision": fecha,
            "tipDocIdeReceptor": partner.l10n_latam_identification_type_id.l10n_pe_vat_code or "6",
            "nroDocIdeReceptor": partner.vat or "",
            "desNomComReceptor": partner.name or "",
            "rznSocialReceptor": partner.name or "",
            "desUbiReceptor": (partner.state_id and partner.state_id.code) or "-",
            "desDirReceptor": partner.street or partner.contact_address or "-",
            "desUrbReceptor": "-",
            "desDepReceptor": (partner.state_id and partner.state_id.name) or "-",
            "desProReceptor": partner.city or "-",
            "desDisReceptor": partner.city or "-",
            "codPaisReceptor": (partner.country_id and partner.country_id.code) or "PE",
            "codRegRetencion": RETENCION_REGIMEN,
            "tasRetencion": fmt(RETENCION_TASA),
            "desObsRetencion": "Retencion del IGV tasa %s%%" % fmt(RETENCION_TASA),
            "mtoTotRetencion": fmt(total_ret),
            "tipMonRetencion": "PEN",
            "mtoImpTotPagRetencion": fmt(total_neto),
            "tipMonImpTotPagRetencion": "PEN",
        }
        return {
            "id": {
                "ruc": self.company_id.vat or "",
                "serie": self.l10n_pe_ret_serie or "R001",
                "correlativo": (self.l10n_pe_ret_correlativo or "1").zfill(8),
            },
            "emisor": self._l10n_pe_emisor(),
            "cabecera": cabecera,
            "detalle": detalle,
        }

    def _l10n_pe_ret_store_cdr(self, cdr_b64):
        self.ensure_one()
        try:
            cdr_bytes = base64.b64decode(cdr_b64)
        except Exception:
            return '', ''
        att = self.env['ir.attachment'].create({
            'name': 'R%s-%s-%s.zip' % (self.company_id.vat or '', self.l10n_pe_ret_serie or 'R001',
                                       (self.l10n_pe_ret_correlativo or '1').zfill(8)),
            'res_model': 'account.payment', 'res_id': self.id,
            'mimetype': 'application/zip', 'raw': cdr_bytes})
        self.l10n_pe_ret_cdr = att.id
        return _l10n_pe_parse_cdr_zip(cdr_bytes)

    # ---------------------------------------------------------------- acción
    def action_l10n_pe_send_retencion(self):
        icp = self.env['ir.config_parameter'].sudo()
        base = icp.get_param('l10n_pe_ne_biller.url', 'http://localhost:8090').rstrip('/')
        timeout = int(icp.get_param('l10n_pe_ne_biller.timeout', '60'))
        for pay in self:
            if pay.l10n_pe_ret_state == 'enviado' or not pay._l10n_pe_ret_aplica():
                continue
            payload = pay._l10n_pe_ret_payload()
            headers = {'X-Api-Key': pay.company_id.sudo().l10n_pe_ne_api_key or ''}
            try:
                resp = requests.post(base + '/generator/retencion', json=payload, headers=headers, timeout=(5, timeout))
            except requests.RequestException as exc:
                pay.l10n_pe_ret_state = 'error'
                pay.l10n_pe_ret_message = _("Error de conexión con el facturador: %s") % exc
                continue
            if resp.status_code == 200 and '<Retention' in resp.text:
                att = self.env['ir.attachment'].create({
                    'name': '%s-20-%s-%s.xml' % (pay.company_id.vat, pay.l10n_pe_ret_serie or 'R001',
                                                 (pay.l10n_pe_ret_correlativo or '1').zfill(8)),
                    'res_model': 'account.payment', 'res_id': pay.id,
                    'mimetype': 'application/xml', 'raw': resp.text.encode('utf-8')})
                pay.l10n_pe_ret_xml = att.id
                pay.l10n_pe_ret_state = 'enviado'
                cdr_b64 = resp.headers.get('X-Sunat-Cdr')
                code, desc = pay._l10n_pe_ret_store_cdr(cdr_b64) if cdr_b64 else ('', '')
                if code == '0':
                    pay.l10n_pe_ret_message = _("Aceptado por SUNAT — CDR ResponseCode 0. %s") % (desc or '')
                elif code:
                    pay.l10n_pe_ret_message = _("CDR de SUNAT (ResponseCode %s). %s") % (code, desc or '')
                else:
                    pay.l10n_pe_ret_message = _("Aceptado por el facturador (HTTP 200).")
            else:
                pay.l10n_pe_ret_state = 'rechazado'
                pay.l10n_pe_ret_message = (resp.text or '')[:2000]
        return True

    # ============================== PERCEPCIÓN (40) ==============================
    def _l10n_pe_per_aplica(self):
        """La percepción aplica a cobros (entrantes) de clientes reconciliados con sus ventas."""
        self.ensure_one()
        return (self.payment_type == 'inbound' and self.partner_type == 'customer'
                and self.reconciled_invoice_ids)

    def _l10n_pe_per_payload(self):
        self.ensure_one()
        fmt = self._l10n_pe_ret_fmt
        partner = self.partner_id
        fecha = (self.date or fields.Date.context_today(self)).strftime("%Y-%m-%d")
        detalle = []
        total_per = total_neto = 0.0
        for inv in self.reconciled_invoice_ids:
            tot = inv.amount_total
            per = round(tot * PERCEPCION_TASA / 100.0, 2)
            neto = round(tot + per, 2)  # el cliente paga el total de la venta MÁS la percepción
            total_per += per
            total_neto += neto
            serie, corr = inv._l10n_pe_serie_correlativo()
            imon = inv.currency_id.name or "PEN"
            detalle.append({
                "tipDocRelacionado": inv._l10n_pe_document_type(),
                "nroDocRelacionado": "%s-%s" % (serie, corr.zfill(8)),
                "fecEmiDocRelacionado": (inv.invoice_date or self.date).strftime("%Y-%m-%d"),
                "mtoImpTotDocRelacionado": fmt(tot),
                "tipMonDocRelacionado": imon,
                "fecPagDocRelacionado": fecha,
                "nroPagDocRelacionado": "1",
                "mtoPagDocRelacionado": fmt(tot),
                "tipMonPagDocRelacionado": imon,
                "mtoPerDocRelacionado": fmt(per),
                "tipMonPerDocRelacionado": "PEN",
                "fecPerDocRelacionado": fecha,
                "mtoTotPagNetoDocRelacionado": fmt(neto),
                "tipMonTotPagNetoDocRelacionado": imon,
                "tipMonRefTipCambio": "PEN", "tipMonObjTipCambio": "PEN",
                "facTipCambio": "", "fecTipCambio": "",
            })
        cabecera = {
            "fecEmision": fecha,
            "tipDocIdeReceptor": partner.l10n_latam_identification_type_id.l10n_pe_vat_code or "6",
            "nroDocIdeReceptor": partner.vat or "",
            "desNomComReceptor": partner.name or "",
            "rznSocialReceptor": partner.name or "",
            "desUbiReceptor": (partner.state_id and partner.state_id.code) or "-",
            "desDirReceptor": partner.street or partner.contact_address or "-",
            "desUrbReceptor": "-",
            "desDepReceptor": (partner.state_id and partner.state_id.name) or "-",
            "desProReceptor": partner.city or "-",
            "desDisReceptor": partner.city or "-",
            "codPaisReceptor": (partner.country_id and partner.country_id.code) or "PE",
            "codRegPercepcion": PERCEPCION_REGIMEN,
            "tasPercepcion": fmt(PERCEPCION_TASA),
            "desObsPercepcion": "Percepcion venta interna tasa %s%%" % fmt(PERCEPCION_TASA),
            "mtoTotPercepcion": fmt(total_per),
            "tipMonPercepcion": "PEN",
            "mtoImpTotPagPercepcion": fmt(total_neto),
            "tipMonImpTotPagPercepcion": "PEN",
        }
        return {
            "id": {
                "ruc": self.company_id.vat or "",
                "serie": self.l10n_pe_per_serie or "P001",
                "correlativo": (self.l10n_pe_per_correlativo or "1").zfill(8),
            },
            "emisor": self._l10n_pe_emisor(),
            "cabecera": cabecera,
            "detalle": detalle,
        }

    def _l10n_pe_per_store_cdr(self, cdr_b64):
        self.ensure_one()
        try:
            cdr_bytes = base64.b64decode(cdr_b64)
        except Exception:
            return '', ''
        att = self.env['ir.attachment'].create({
            'name': 'R%s-%s-%s.zip' % (self.company_id.vat or '', self.l10n_pe_per_serie or 'P001',
                                       (self.l10n_pe_per_correlativo or '1').zfill(8)),
            'res_model': 'account.payment', 'res_id': self.id,
            'mimetype': 'application/zip', 'raw': cdr_bytes})
        self.l10n_pe_per_cdr = att.id
        return _l10n_pe_parse_cdr_zip(cdr_bytes)

    def action_l10n_pe_send_percepcion(self):
        icp = self.env['ir.config_parameter'].sudo()
        base = icp.get_param('l10n_pe_ne_biller.url', 'http://localhost:8090').rstrip('/')
        timeout = int(icp.get_param('l10n_pe_ne_biller.timeout', '60'))
        for pay in self:
            if pay.l10n_pe_per_state == 'enviado' or not pay._l10n_pe_per_aplica():
                continue
            payload = pay._l10n_pe_per_payload()
            headers = {'X-Api-Key': pay.company_id.sudo().l10n_pe_ne_api_key or ''}
            try:
                resp = requests.post(base + '/generator/percepcion', json=payload, headers=headers, timeout=(5, timeout))
            except requests.RequestException as exc:
                pay.l10n_pe_per_state = 'error'
                pay.l10n_pe_per_message = _("Error de conexión con el facturador: %s") % exc
                continue
            if resp.status_code == 200 and '<Perception' in resp.text:
                att = self.env['ir.attachment'].create({
                    'name': '%s-40-%s-%s.xml' % (pay.company_id.vat, pay.l10n_pe_per_serie or 'P001',
                                                 (pay.l10n_pe_per_correlativo or '1').zfill(8)),
                    'res_model': 'account.payment', 'res_id': pay.id,
                    'mimetype': 'application/xml', 'raw': resp.text.encode('utf-8')})
                pay.l10n_pe_per_xml = att.id
                pay.l10n_pe_per_state = 'enviado'
                cdr_b64 = resp.headers.get('X-Sunat-Cdr')
                code, desc = pay._l10n_pe_per_store_cdr(cdr_b64) if cdr_b64 else ('', '')
                if code == '0':
                    pay.l10n_pe_per_message = _("Aceptado por SUNAT — CDR ResponseCode 0. %s") % (desc or '')
                elif code:
                    pay.l10n_pe_per_message = _("CDR de SUNAT (ResponseCode %s). %s") % (code, desc or '')
                else:
                    pay.l10n_pe_per_message = _("Aceptado por el facturador (HTTP 200).")
            else:
                pay.l10n_pe_per_state = 'rechazado'
                pay.l10n_pe_per_message = (resp.text or '')[:2000]
        return True

    # ---------------------------- API ligera (BFF NE Express, /json/2): Ret(20) / Per(40)
    @api.model
    def l10n_pe_ne_quick_retencion(self, payload):
        """Emite una Retención (20) desde un payload plano: halla/crea el proveedor y sus comprobantes
        de compra, registra el pago reconciliado (3% IGV) con el wizard estándar de Odoo y lo envía a
        SUNAT. Devuelve el resultado. Lo consume el BFF stateless por /json/2."""
        AM = self.env['account.move']
        prov = AM._l10n_pe_ne_quick_partner(payload.get('proveedor') or payload.get('cliente') or {})
        if not prov.supplier_rank:
            prov.supplier_rank = 1
        bills = self._l10n_pe_ne_quick_related(payload, prov, 'in_invoice')
        if not bills:
            raise UserError(_("La retención necesita comprobantes de compra (documentos o comprobantes)."))
        pay = self._l10n_pe_ne_register_payment(bills)
        pay.l10n_pe_ret_serie = payload.get('serie') or pay.l10n_pe_ret_serie or 'R001'
        pay.l10n_pe_ret_correlativo = str(payload.get('correlativo') or self._l10n_pe_ne_next_corr(
            'l10n_pe_ret_serie', 'l10n_pe_ret_correlativo', pay.l10n_pe_ret_serie))
        pay.action_l10n_pe_send_retencion()
        return pay._l10n_pe_ne_otrocpe_result('ret')

    @api.model
    def l10n_pe_ne_quick_percepcion(self, payload):
        """Emite una Percepción (40) desde un payload plano: halla/crea el cliente y sus ventas, registra
        el cobro reconciliado (2%) y lo envía a SUNAT."""
        AM = self.env['account.move']
        cli = AM._l10n_pe_ne_quick_partner(payload.get('cliente') or {})
        invs = self._l10n_pe_ne_quick_related(payload, cli, 'out_invoice')
        if not invs:
            raise UserError(_("La percepción necesita comprobantes de venta (documentos o comprobantes)."))
        pay = self._l10n_pe_ne_register_payment(invs)
        pay.l10n_pe_per_serie = payload.get('serie') or pay.l10n_pe_per_serie or 'P001'
        pay.l10n_pe_per_correlativo = str(payload.get('correlativo') or self._l10n_pe_ne_next_corr(
            'l10n_pe_per_serie', 'l10n_pe_per_correlativo', pay.l10n_pe_per_serie))
        pay.action_l10n_pe_send_percepcion()
        return pay._l10n_pe_ne_otrocpe_result('per')

    @api.model
    def _l10n_pe_ne_quick_related(self, payload, partner, move_type):
        """Comprobantes relacionados del otro-CPE: por id existentes (payload.comprobantes) o creados
        desde payload.documentos [{numero/serie, total, fecha, descripcion}] con línea única = total sin
        impuesto (el XML de ret/per solo usa el monto total del documento relacionado)."""
        AM = self.env['account.move']
        ids = payload.get('comprobantes') or []
        if ids:
            return AM.browse([int(i) for i in ids]).exists()
        jtype = 'purchase' if move_type == 'in_invoice' else 'sale'
        journal = self.env['account.journal'].search(
            [('type', '=', jtype), ('company_id', '=', self.env.company.id)], limit=1)
        # Retención (20) y Percepción (40) son SIEMPRE en soles. Si la move se crea sin
        # currency_id explícita, hereda la del diario/compañía; en entornos donde el diario
        # de compras quedó en USD, el documento relacionado sale en dólares y el XML pone
        # `SUNATNetTotalPaid/currencyID="USD"` -> SUNAT rechaza (errorCode 2748). Forzamos PEN
        # para que el otro-CPE sea consistente sin depender de la config de la compañía.
        pen = self.env.ref('base.PEN', raise_if_not_found=False) \
            or self.env['res.currency'].search([('name', '=', 'PEN')], limit=1)
        moves = AM
        for doc in payload.get('documentos') or []:
            vals = {
                'move_type': move_type,
                'partner_id': partner.id,
                'journal_id': journal.id,
                **({'currency_id': pen.id} if pen else {}),
                'invoice_date': doc.get('fecha') or fields.Date.context_today(self),
                'invoice_line_ids': [(0, 0, {
                    'name': doc.get('descripcion') or 'OPERACION',
                    'quantity': 1,
                    'price_unit': float(doc.get('total') or 0),
                    'tax_ids': [(6, 0, [])],
                })],
            }
            if move_type == 'in_invoice':
                numero = doc.get('numero') or ''
                vals['ref'] = numero
                dt = self.env['l10n_latam.document.type'].search(
                    [('code', '=', doc.get('tipoComprobante') or '01'), ('country_id.code', '=', 'PE')], limit=1)
                if dt:
                    vals['l10n_latam_document_type_id'] = dt.id
                if numero:
                    vals['l10n_latam_document_number'] = numero
            else:
                vals['l10n_pe_serie'] = doc.get('serie') or 'F001'
                if doc.get('correlativo'):
                    vals['l10n_pe_correlativo'] = str(doc['correlativo'])
            m = AM.create(vals)
            m.action_post()
            moves |= m
        return moves

    def _l10n_pe_ne_register_payment(self, moves):
        """Registra UN pago reconciliado con los comprobantes (wizard estándar account.payment.register).
        El tipo (entrante/saliente, cliente/proveedor) lo deriva el wizard de los comprobantes."""
        reg = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=moves.ids).create({})
        pays = reg._create_payments()
        return pays[0]

    def _l10n_pe_ne_next_corr(self, serie_field, corr_field, serie):
        pays = self.search([(serie_field, '=', serie)])
        nums = [int(p[corr_field]) for p in pays if (p[corr_field] or '').isdigit()]
        return str((max(nums) if nums else 0) + 1)

    def _l10n_pe_ne_otrocpe_result(self, kind):
        self.ensure_one()
        if kind == 'ret':
            tipo, state, msg = '20', self.l10n_pe_ret_state, self.l10n_pe_ret_message
            serie, corr = self.l10n_pe_ret_serie, self.l10n_pe_ret_correlativo
        else:
            tipo, state, msg = '40', self.l10n_pe_per_state, self.l10n_pe_per_message
            serie, corr = self.l10n_pe_per_serie, self.l10n_pe_per_correlativo
        m = re.search(r'ResponseCode (\d+)', msg or '')
        return {
            'id': self.id, 'tipoDoc': tipo, 'serie': serie or '',
            'correlativo': (corr or '1').zfill(8), 'estado': state,
            'responseCode': m.group(1) if m else '', 'mensaje': msg or '',
            'cliente': self.partner_id.name or '',
        }

    def l10n_pe_ne_get_files(self, kind=None):
        """{xml, cdr, pdf} base64 del otro-CPE (retención 20 / percepción 40), para que el BFF los sirva.
        `kind` se ignora: el otro-CPE nunca tiene ticket 80mm (el controller responde 404 para 'ticket')."""
        self.ensure_one()

        def b64(att):
            v = att.datas
            return v.decode('ascii') if isinstance(v, (bytes, bytearray)) else (v or '')

        es_ret = bool(self.l10n_pe_ret_xml) or self.l10n_pe_ret_state == 'enviado'
        if es_ret:
            xml, cdr, pdf = self.l10n_pe_ret_xml, self.l10n_pe_ret_cdr, self.l10n_pe_ret_pdf
            tipo, serie, corr = '20', self.l10n_pe_ret_serie or 'R001', self.l10n_pe_ret_correlativo
        else:
            xml, cdr, pdf = self.l10n_pe_per_xml, self.l10n_pe_per_cdr, self.l10n_pe_per_pdf
            tipo, serie, corr = '40', self.l10n_pe_per_serie or 'P001', self.l10n_pe_per_correlativo
        out = {}
        if xml:
            out['xml'] = b64(xml)
        if cdr:
            out['cdr'] = b64(cdr)
        try:
            if not pdf and xml:
                pdf = self._l10n_pe_otrocpe_pdf(xml, tipo, serie, corr)
                if es_ret:
                    self.l10n_pe_ret_pdf = pdf.id
                else:
                    self.l10n_pe_per_pdf = pdf.id
            if pdf:
                out['pdf'] = b64(pdf)
        except Exception:
            pass
        return out

    # ------------------------------------------------- descargas / PDF (SFS 2.4)
    @staticmethod
    def _l10n_pe_download_url(attachment):
        """Acción de descarga directa del adjunto vía /web/content."""
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % attachment.id,
            'target': 'self',
        }

    def _l10n_pe_otrocpe_pdf(self, xml_att, tipo_doc, serie, correlativo):
        """Genera el PDF del otro-CPE (retención 20 / percepción 40) vía el micro /report/pdf,
        usando el XML firmado guardado como adjunto."""
        self.ensure_one()
        if not xml_att:
            raise UserError(_("El comprobante no tiene XML firmado; emítalo primero a SUNAT."))
        icp = self.env['ir.config_parameter'].sudo()
        base = icp.get_param('l10n_pe_ne_biller.url', 'http://localhost:8090').rstrip('/')
        # pdf_timeout separado del timeout de emisión (mismo criterio que en
        # account_move_biller): subir el de emisión no debe alargar el del PDF.
        timeout = int(icp.get_param('l10n_pe_ne_biller.pdf_timeout', '60'))
        payload = {'ruc': self.company_id.vat or '', 'tipoDoc': tipo_doc,
                   'xml': (xml_att.raw or b'').decode('utf-8')}
        headers = {'X-Api-Key': self.company_id.sudo().l10n_pe_ne_api_key or ''}
        try:
            resp = requests.post(base + '/report/pdf', json=payload, headers=headers, timeout=(5, timeout))
        except requests.RequestException as exc:
            raise UserError(_("Error de conexión con el facturador: %s") % exc)
        if resp.status_code != 200 or not resp.content.startswith(b'%PDF'):
            raise UserError(_("El facturador no devolvió un PDF (HTTP %s): %s")
                            % (resp.status_code, (resp.text or '')[:500]))
        return self.env['ir.attachment'].create({
            'name': '%s-%s-%s-%s.pdf' % (self.company_id.vat or '', tipo_doc, serie,
                                         (correlativo or '1').zfill(8)),
            'res_model': 'account.payment', 'res_id': self.id,
            'mimetype': 'application/pdf', 'raw': resp.content})

    def action_l10n_pe_download_ret_pdf(self):
        self.ensure_one()
        if not self.l10n_pe_ret_pdf:
            self.l10n_pe_ret_pdf = self._l10n_pe_otrocpe_pdf(
                self.l10n_pe_ret_xml, '20', self.l10n_pe_ret_serie or 'R001',
                self.l10n_pe_ret_correlativo).id
        return self._l10n_pe_download_url(self.l10n_pe_ret_pdf)

    def action_l10n_pe_download_per_pdf(self):
        self.ensure_one()
        if not self.l10n_pe_per_pdf:
            self.l10n_pe_per_pdf = self._l10n_pe_otrocpe_pdf(
                self.l10n_pe_per_xml, '40', self.l10n_pe_per_serie or 'P001',
                self.l10n_pe_per_correlativo).id
        return self._l10n_pe_download_url(self.l10n_pe_per_pdf)

    def action_l10n_pe_download_ret_xml(self):
        self.ensure_one()
        if not self.l10n_pe_ret_xml:
            raise UserError(_("No hay XML de retención."))
        return self._l10n_pe_download_url(self.l10n_pe_ret_xml)

    def action_l10n_pe_download_ret_cdr(self):
        self.ensure_one()
        if not self.l10n_pe_ret_cdr:
            raise UserError(_("No hay CDR de retención."))
        return self._l10n_pe_download_url(self.l10n_pe_ret_cdr)

    def action_l10n_pe_download_per_xml(self):
        self.ensure_one()
        if not self.l10n_pe_per_xml:
            raise UserError(_("No hay XML de percepción."))
        return self._l10n_pe_download_url(self.l10n_pe_per_xml)

    def action_l10n_pe_download_per_cdr(self):
        self.ensure_one()
        if not self.l10n_pe_per_cdr:
            raise UserError(_("No hay CDR de percepción."))
        return self._l10n_pe_download_url(self.l10n_pe_per_cdr)

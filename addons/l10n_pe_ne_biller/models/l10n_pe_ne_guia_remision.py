"""Guía de Remisión Electrónica (GRE) — Remitente (tipo 09).

A diferencia de los comprobantes (factura/boleta/NC/ND), la GRE NO es un account.move:
es un documento de traslado propio, con su serie (T###) y su canal en el biller
(`POST /generator/guia`, REST/OAuth2 — ver ms-ne-biller). Este modelo arma el payload que
espera el biller (mismas claves que `GreCabeceraRequest`) y guarda el resultado.

El controller (`/ne/api/guias`), el PDF QWeb con el QR de SUNAT y la re-consulta del
ticket (botón + cron) viven también en este addon; ver controllers/main.py y
report/guia_report.xml.
"""
import base64
import io
import json
import logging
import re
import zipfile

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Catálogo 20 SUNAT — motivo de traslado (los más comunes).
MOTIVOS_TRASLADO = [
    ('01', 'Venta'),
    ('02', 'Compra'),
    ('04', 'Traslado entre establecimientos de la misma empresa'),
    ('08', 'Importación'),
    ('09', 'Exportación'),
    ('13', 'Otros'),
    ('14', 'Venta sujeta a confirmación del comprador'),
    ('18', 'Traslado emisor itinerante de comprobantes de pago'),
    ('19', 'Traslado a zona primaria'),
]
# Catálogo 18 SUNAT — modalidad de traslado.
MODALIDADES_TRASLADO = [
    ('01', 'Transporte público'),
    ('02', 'Transporte privado'),
]
UNIDADES_PESO = [('KGM', 'Kilogramos'), ('TNE', 'Toneladas')]

# Motivos cuyo XML el biller sustenta completo hoy. 04 necesita código de establecimiento
# anexo (AddressTypeCode), 08/09 contenedor/puerto — ampliar biller + este set al soportarlos.
SUPPORTED_MOTIVOS = ('01', '02', '13', '14', '18')

# Estados desde los que se puede (re)emitir o editar: aún sin CDR aceptado.
ESTADOS_EMITIBLES = ('borrador', 'error', 'rechazado')


class L10nPeNeGuiaRemision(models.Model):
    _name = 'l10n_pe_ne.guia_remision'
    _description = 'Guía de Remisión Electrónica (Remitente)'
    _order = 'fecha_emision desc, id desc'

    name = fields.Char(string='Número', required=True, copy=False, readonly=True,
                       default=lambda s: _('Nueva'), index=True)
    serie = fields.Char(string='Serie', default='T001', required=True)
    correlativo = fields.Char(string='Correlativo', copy=False, readonly=True)
    company_id = fields.Many2one('res.company', required=True, index=True,
                                 default=lambda s: s.env.company)
    # Estado espeja al del comprobante (para reusar la UI de estados en el front).
    estado = fields.Selection([
        ('borrador', 'Borrador'),
        ('en_proceso', 'En proceso'),
        ('enviado', 'Aceptado'),
        ('rechazado', 'Rechazado'),
        ('error', 'Error'),
        ('anulado', 'Anulado'),
    ], string='Estado', default='borrador', required=True, copy=False)

    # -------------------------------------------------------------- cabecera
    fecha_emision = fields.Date(string='Fecha de emisión', required=True,
                               default=fields.Date.context_today)
    hora_emision = fields.Char(string='Hora de emisión', default='08:00:00')
    obs_guia = fields.Char(string='Observación')

    # Destinatario (a quién se le entrega). El tipo/num doc se derivan del partner.
    partner_id = fields.Many2one('res.partner', string='Destinatario', required=True, index=True)

    # Datos del traslado.
    motivo_traslado = fields.Selection(MOTIVOS_TRASLADO, string='Motivo de traslado',
                                       default='01', required=True)
    des_motivo_traslado = fields.Char(string='Descripción del motivo')
    peso_bruto = fields.Float(string='Peso bruto total', required=True, default=1.0)
    uni_medida_peso = fields.Selection(UNIDADES_PESO, string='Unidad de peso',
                                       default='KGM', required=True)
    num_bultos = fields.Integer(string='N° de bultos', default=1)
    modalidad_traslado = fields.Selection(MODALIDADES_TRASLADO, string='Modalidad',
                                          default='02', required=True)
    fecha_inicio_traslado = fields.Date(string='Fecha de inicio del traslado', required=True,
                                        default=fields.Date.context_today)

    # Transporte público (modalidad 01): datos del transportista.
    transportista_id = fields.Many2one('res.partner', string='Transportista')
    num_reg_mtc = fields.Char(string='Registro MTC')

    # Proveedor (motivo 02 Compra): SellerSupplierParty en el XML.
    proveedor_id = fields.Many2one('res.partner', string='Proveedor')

    # Transporte privado (modalidad 02): vehículo + conductor.
    num_placa = fields.Char(string='Placa del vehículo')
    conductor_tipo_doc = fields.Selection([('1', 'DNI'), ('4', 'Carné ext.'), ('7', 'Pasaporte')],
                                          string='Tipo doc. conductor', default='1')
    conductor_num_doc = fields.Char(string='N° doc. conductor')
    conductor_nombres = fields.Char(string='Nombres del conductor')
    conductor_apellidos = fields.Char(string='Apellidos del conductor')
    conductor_licencia = fields.Char(string='Licencia de conducir')

    # Puntos de partida y llegada (ubigeo cat. 13 + dirección).
    ubigeo_partida = fields.Char(string='Ubigeo de partida', required=True)
    dir_partida = fields.Char(string='Dirección de partida', required=True)
    ubigeo_llegada = fields.Char(string='Ubigeo de llegada', required=True)
    dir_llegada = fields.Char(string='Dirección de llegada', required=True)

    # Comprobante relacionado (docRelacionado): la factura/boleta que origina el traslado.
    comprobante_id = fields.Many2one('account.move', string='Comprobante relacionado',
                                     copy=False, index=True)

    line_ids = fields.One2many('l10n_pe_ne.guia_remision.line', 'guia_id',
                               string='Bienes', copy=True)

    # Resultado del biller.
    l10n_pe_biller_xml = fields.Many2one('ir.attachment', string='XML firmado', copy=False)
    l10n_pe_biller_cdr = fields.Many2one('ir.attachment', string='CDR', copy=False)
    l10n_pe_biller_message = fields.Char(string='Mensaje del facturador', copy=False)
    num_ticket = fields.Char(string='N° de ticket SUNAT', copy=False)
    l10n_pe_ne_qr_url = fields.Char(string='URL del QR (SUNAT)', copy=False,
                                    help='Viene en el CDR aceptado; es el QR válido para sustentar el traslado.')

    def init(self):
        # Único parcial sobre las secuencias de guía: bajo REPEATABLE READ el lock
        # consultivo no basta (el snapshot de una transacción concurrente puede no ver
        # la secuencia recién commiteada y crearla de nuevo, duplicando correlativos).
        # El índice convierte esa carrera en IntegrityError: un request fallido,
        # nunca un correlativo duplicado ante SUNAT.
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ir_sequence_gre_code_company_uniq
            ON ir_sequence (code, company_id)
            WHERE code LIKE 'l10n_pe.ne.guia_remision.%'
        """)

    @api.model
    def _l10n_pe_ne_next_correlativo(self, company, serie):
        """Correlativo por (compañía, serie): SUNAT exige numeración correlativa por serie y
        por RUC. Crea la secuencia al primer uso, sembrada tras el correlativo más alto ya
        emitido en esa serie (migración desde la secuencia global inicial)."""
        code = 'l10n_pe.ne.guia_remision.%s' % serie
        # Lock consultivo: serializa el primer uso de una (serie, compañía) en el caso
        # común. La garantía dura la pone el índice único parcial (ver init()): bajo
        # REPEATABLE READ una transacción concurrente puede no ver el commit ajeno.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", ('%s/%s' % (code, company.id),))
        Seq = self.env['ir.sequence'].sudo()
        seq = Seq.search([('code', '=', code), ('company_id', '=', company.id)], limit=1)
        if not seq:
            ultimo = 0
            for g in self.sudo().search([('company_id', '=', company.id), ('serie', '=', serie)]):
                try:
                    ultimo = max(ultimo, int(g.correlativo or 0))
                except ValueError:
                    pass
            seq = Seq.create({
                'name': 'GRE %s (%s)' % (serie, company.display_name),
                'code': code,
                'company_id': company.id,
                'padding': 1,
                'number_increment': 1,
                'implementation': 'no_gap',
                'number_next': ultimo + 1,
            })
        return seq.next_by_id()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals.get('name') == _('Nueva'):
                serie = vals.get('serie') or 'T001'
                company = self.env['res.company'].browse(
                    vals.get('company_id') or self.env.company.id)
                corr = self._l10n_pe_ne_next_correlativo(company, serie)
                vals['correlativo'] = corr
                vals['name'] = '%s-%s' % (serie, corr)
        return super().create(vals_list)

    # -------------------------------------------------------- payload biller
    def _l10n_pe_ne_doc_tipo(self, partner):
        """Tipo de documento SUNAT (cat. 6): 6=RUC (11 dígitos), 1=DNI (8). Cualquier otra
        cosa se rechaza acá — mandarlo mal es rechazo seguro de SUNAT."""
        vat = (partner.vat or '').strip()
        if len(vat) == 11:
            return '6'
        if len(vat) == 8:
            return '1'
        raise UserError(_('El documento de "%s" debe ser RUC (11 dígitos) o DNI (8); tiene "%s".')
                        % (partner.display_name, vat or '—'))

    def _l10n_pe_ne_build_gre_payload(self):
        """Arma el JSON que espera el biller (`GreRequest`). Claves = campos de GreCabeceraRequest."""
        self.ensure_one()
        dest = self.partner_id
        cab = {
            'ublVersionId': '2.1',
            'customizationId': '2.0',
            'fecEmision': self.fecha_emision.strftime('%Y-%m-%d') if self.fecha_emision else '',
            'horEmision': self.hora_emision or '08:00:00',
            'obsGuia': self.obs_guia or '',
            'tipDocDestinatario': self._l10n_pe_ne_doc_tipo(dest),
            'numDocDestinatario': dest.vat or '',
            'rznSocialDestinatario': dest.name or '',
            'motTrasladoDatosEnvio': self.motivo_traslado,
            'desMotivoTrasladoDatosEnvio': self.des_motivo_traslado
                or dict(MOTIVOS_TRASLADO).get(self.motivo_traslado, ''),
            'psoBrutoTotalBienesDatosEnvio': '%.3f' % (self.peso_bruto or 0.0),
            'uniMedidaPesoBrutoDatosEnvio': self.uni_medida_peso or 'KGM',
            'numBultosDatosEnvio': str(self.num_bultos or 1),
            'modTrasladoDatosEnvio': self.modalidad_traslado,
            'fecInicioTrasladoDatosEnvio': self.fecha_inicio_traslado.strftime('%Y-%m-%d')
                if self.fecha_inicio_traslado else '',
            'ubiPartida': self.ubigeo_partida or '',
            'dirPartida': self.dir_partida or '',
            'ubiLlegada': self.ubigeo_llegada or '',
            'dirLlegada': self.dir_llegada or '',
        }
        if self.motivo_traslado == '02' and self.proveedor_id:
            prov = self.proveedor_id
            cab.update({
                'tipDocProveedor': self._l10n_pe_ne_doc_tipo(prov),
                'numDocProveedor': prov.vat or '',
                'rznSocialProveedor': prov.name or '',
            })
        if self.modalidad_traslado == '01':  # transporte público
            t = self.transportista_id
            cab.update({
                'tipDocTransportista': self._l10n_pe_ne_doc_tipo(t) if t else '6',
                'numDocTransportista': (t.vat or '') if t else '',
                'nomTransportista': (t.name or '') if t else '',
                'numRegMtcTransportista': self.num_reg_mtc or '',
            })
        else:  # transporte privado
            cab.update({
                'numPlacaTransPrivado': self.num_placa or '',
                'tipDocIdeConductorTransPrivado': self.conductor_tipo_doc or '1',
                'numDocIdeConductorTransPrivado': self.conductor_num_doc or '',
                'nomConductorTransPrivado': self.conductor_nombres or '',
                'apeConductorTransPrivado': self.conductor_apellidos or '',
                'licConductorTransPrivado': self.conductor_licencia or '',
            })
        detalle = [{
            'canItem': '%.2f' % (l.cantidad or 0.0),
            'uniMedidaItem': l.unidad or 'NIU',
            'desItem': l.descripcion or (l.product_id.display_name or ''),
            'codItem': (l.product_id.default_code or '') if l.product_id else '',
        } for l in self.line_ids]
        doc_rel = []
        if self.comprobante_id and self.comprobante_id.l10n_pe_ne_serie_emit:
            doc_rel.append({
                'codTipDocRel': self.comprobante_id.l10n_pe_ne_tipo_doc or '01',
                'numDocRel': '%s-%s' % (self.comprobante_id.l10n_pe_ne_serie_emit,
                                        self.comprobante_id.l10n_pe_ne_corr_emit or ''),
            })
        return {
            'id': {
                'ruc': self.company_id.vat or '',
                'serie': self.serie or 'T001',
                'correlativo': self.correlativo or '1',
            },
            'cabecera': cab,
            'detalle': detalle,
            'docRelacionado': doc_rel,
        }

    # ------------------------------------------------------------- emisión
    def _l10n_pe_ne_validar(self):
        self.ensure_one()
        if self.estado not in ESTADOS_EMITIBLES:
            raise UserError(_('La guía %s ya fue emitida (estado: %s).') % (self.name, self.estado))
        if not self.line_ids:
            raise UserError(_('La guía necesita al menos un bien.'))
        if not self.peso_bruto or self.peso_bruto <= 0:
            raise UserError(_('El peso bruto debe ser mayor a 0.'))
        for campo, etiqueta in (('ubigeo_partida', 'partida'), ('ubigeo_llegada', 'llegada')):
            if not re.fullmatch(r'\d{6}', self[campo] or ''):
                raise UserError(_('El ubigeo de %s debe tener 6 dígitos.') % etiqueta)
        if self.fecha_inicio_traslado and self.fecha_emision \
                and self.fecha_inicio_traslado < self.fecha_emision:
            raise UserError(_('La fecha de inicio del traslado no puede ser anterior a la emisión.'))
        self._l10n_pe_ne_doc_tipo(self.partner_id)  # valida RUC/DNI del destinatario
        if self.motivo_traslado not in SUPPORTED_MOTIVOS:
            raise UserError(_('El motivo de traslado %s aún no soportado para emisión.')
                            % self.motivo_traslado)
        if self.motivo_traslado == '13' and not (self.des_motivo_traslado or '').strip():
            raise UserError(_('El motivo "Otros" requiere describir el motivo del traslado.'))
        if self.motivo_traslado == '02' and not self.proveedor_id:
            raise UserError(_('El motivo "Compra" requiere indicar el proveedor.'))
        if self.modalidad_traslado == '02':
            faltantes = [etiqueta for campo, etiqueta in (
                ('num_placa', _('la placa del vehículo')),
                ('conductor_num_doc', _('el documento del conductor')),
                ('conductor_nombres', _('los nombres del conductor')),
                ('conductor_apellidos', _('los apellidos del conductor')),
                ('conductor_licencia', _('la licencia de conducir')),
            ) if not (self[campo] or '').strip()]
            if faltantes:
                raise UserError(_('Transporte privado: falta %s.') % ', '.join(faltantes))
        else:
            if not self.transportista_id:
                raise UserError(_('Transporte público: indica el transportista.'))
            if len((self.transportista_id.vat or '').strip()) != 11:
                raise UserError(_('El transportista debe tener RUC (11 dígitos).'))

    def _l10n_pe_ne_extraer_qr_url(self, cdr_bytes):
        """URL del código QR que SUNAT incluye en el CDR aceptado de la GRE. El QR de la
        representación impresa DEBE ser este (RS 123-2022) — no uno generado localmente.
        Busca la primera URL http(s) en los cbc:Note / cbc:DocumentDescription del
        ApplicationResponse (regex sobre bytes, tolerante a namespaces)."""
        try:
            with zipfile.ZipFile(io.BytesIO(cdr_bytes)) as zf:
                xml_name = next((n for n in zf.namelist() if n.lower().endswith('.xml')), None)
                content = zf.read(xml_name) if xml_name else b''
        except Exception:  # noqa: BLE001
            return ''
        for m in re.finditer(rb'<cbc:(?:Note|DocumentDescription)>([^<]*)</cbc:(?:Note|DocumentDescription)>', content):
            um = re.search(r'https?://[^\s|<>"]+', m.group(1).decode('utf-8', 'replace'))
            if um:
                return um.group(0)
        return ''

    def _l10n_pe_ne_store_cdr(self, cdr_b64):
        """Guarda el CDR (zip base64 del header X-Sunat-Cdr) como adjunto en l10n_pe_biller_cdr
        y devuelve (responseCode, description). Reusa el parser del CDR de account.move."""
        self.ensure_one()
        try:
            cdr_bytes = base64.b64decode(cdr_b64)
        except Exception:  # noqa: BLE001
            return '', ''
        att = self.env['ir.attachment'].create({
            'name': 'R%s-09-%s.zip' % (self.company_id.vat or '', self.name),
            'res_model': 'l10n_pe_ne.guia_remision',
            'res_id': self.id,
            'mimetype': 'application/zip',
            'raw': cdr_bytes,
        })
        self.l10n_pe_biller_cdr = att.id
        self.l10n_pe_ne_qr_url = self._l10n_pe_ne_extraer_qr_url(cdr_bytes) or self.l10n_pe_ne_qr_url
        return self.env['account.move']._l10n_pe_parse_cdr_codes(cdr_bytes)

    def _l10n_pe_ne_aplicar_cdr(self, cdr_b64):
        """Guarda el CDR y fija estado/mensaje según su ResponseCode (0 = aceptada).
        Camino común de la emisión síncrona y de la re-consulta del ticket."""
        code, desc = self._l10n_pe_ne_store_cdr(cdr_b64)
        if code == '0':
            self.estado = 'enviado'
            self.l10n_pe_biller_message = _('Aceptada por SUNAT — CDR ResponseCode 0. %s') % (desc or '')
        elif not code:
            # CDR ilegible (base64/zip corrupto o sin ResponseCode): NO es un rechazo
            # de SUNAT — queda en_proceso para que el botón/cron reintenten con el ticket.
            self.estado = 'en_proceso'
            self.l10n_pe_biller_message = _('CDR recibido pero ilegible; se reintentará la consulta del ticket.')
        else:
            self.estado = 'rechazado'
            self.l10n_pe_biller_message = _('Rechazada por SUNAT (ResponseCode %s). %s') % (code or '—', desc or '')

    def l10n_pe_ne_emitir_guia(self):
        """Emite la GRE al biller (`POST /generator/guia`): firma, envía a SUNAT y recoge el CDR.
        El biller devuelve el XML firmado en el body y el CDR (zip base64) en el header
        `X-Sunat-Cdr` (igual que la factura). Guarda ambos y fija el estado según el ResponseCode
        del CDR (0 = aceptado)."""
        self.ensure_one()
        # SUNAT valida fecEmision/horEmision contra el momento del envío: se estampan al
        # emitir (hora de Lima), no al crear el borrador. Solo en estados emitibles — una
        # guía ya aceptada no debe ver su fecha pisada ni siquiera antes del UserError.
        if self.estado in ESTADOS_EMITIBLES:
            ahora_lima = fields.Datetime.context_timestamp(
                self.with_context(tz='America/Lima'), fields.Datetime.now())
            self.fecha_emision = ahora_lima.date()
            self.hora_emision = ahora_lima.strftime('%H:%M:%S')
        self._l10n_pe_ne_validar()
        icp = self.env['ir.config_parameter'].sudo()
        base = icp.get_param('l10n_pe_ne_biller.url', 'http://localhost:8090').rstrip('/')
        timeout = int(icp.get_param('l10n_pe_ne_biller.timeout', '240'))
        headers = {'X-Api-Key': self.company_id.sudo().l10n_pe_ne_api_key or ''}
        payload = self._l10n_pe_ne_build_gre_payload()
        try:
            resp = requests.post(base + '/generator/guia', json=payload, headers=headers,
                                 timeout=(5, timeout))
        except requests.RequestException as exc:
            self.estado = 'error'
            self.l10n_pe_biller_message = _('Error de conexión con el facturador: %s') % exc
            return self._l10n_pe_ne_guia_dict()
        body = resp.text or ''
        if resp.status_code == 200 and any(t in body for t in ('<DespatchAdvice', '<ext:UBLExtensions')):
            att = self.env['ir.attachment'].create({
                'name': '%s-09-%s.xml' % (self.company_id.vat, self.name),
                'res_model': 'l10n_pe_ne.guia_remision',
                'res_id': self.id,
                'mimetype': 'application/xml',
                'raw': body.encode('utf-8'),
            })
            self.l10n_pe_biller_xml = att.id
            self.num_ticket = resp.headers.get('X-Sunat-Ticket') or self.num_ticket
            cdr_b64 = resp.headers.get('X-Sunat-Cdr')
            if cdr_b64:
                self._l10n_pe_ne_aplicar_cdr(cdr_b64)
            else:
                # Firmada y enviada, pero SUNAT aún no devolvió el CDR: queda el ticket
                # para re-consultar (botón en la SPA + cron cada 10 min).
                self.estado = 'en_proceso'
                self.l10n_pe_biller_message = _('Firmada y enviada; SUNAT aún no devolvió el CDR.')
        else:
            self.estado = 'rechazado' if resp.status_code == 400 else 'error'
            self.l10n_pe_biller_message = ('HTTP %s: %s' % (resp.status_code, body))[:2000]
        return self._l10n_pe_ne_guia_dict()

    def l10n_pe_ne_consultar_ticket(self):
        """Re-consulta al biller el ticket de una guía en_proceso
        (GET /generator/guia/ticket/{numTicket}) y aplica el CDR si ya está."""
        self.ensure_one()
        if self.estado != 'en_proceso' or not self.num_ticket:
            raise UserError(_('Solo se puede consultar una guía en proceso con ticket de SUNAT.'))
        icp = self.env['ir.config_parameter'].sudo()
        base = icp.get_param('l10n_pe_ne_biller.url', 'http://localhost:8090').rstrip('/')
        headers = {'X-Api-Key': self.company_id.sudo().l10n_pe_ne_api_key or ''}
        try:
            resp = requests.get('%s/generator/guia/ticket/%s' % (base, self.num_ticket),
                                headers=headers, timeout=(5, 60))
        except requests.RequestException as exc:
            raise UserError(_('Error de conexión con el facturador: %s') % exc)
        cdr_b64 = resp.headers.get('X-Sunat-Cdr')
        if resp.status_code == 200 and cdr_b64:
            self._l10n_pe_ne_aplicar_cdr(cdr_b64)
        elif resp.status_code != 200:
            self.l10n_pe_biller_message = ('HTTP %s: %s' % (resp.status_code, resp.text or ''))[:2000]
        return self._l10n_pe_ne_guia_dict()

    @api.model
    def _cron_consultar_en_proceso(self):
        """Cron: re-consulta todas las guías en_proceso con ticket. Best-effort."""
        for g in self.search([('estado', '=', 'en_proceso'), ('num_ticket', '!=', False)]):
            try:
                g.l10n_pe_ne_consultar_ticket()
            except Exception as exc:  # noqa: BLE001 — reintenta al próximo cron
                _logger.warning('GRE %s: re-consulta falló: %s', g.name, exc)

    # ------------------------------------------------------- serialización
    def _l10n_pe_ne_guia_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'numero': self.name,
            'destinatario': self.partner_id.name or '',
            'destinatarioDoc': self.partner_id.vat or '',
            'fecha': self.fecha_emision.strftime('%Y-%m-%d') if self.fecha_emision else '',
            'estado': self.estado,
            'motivo': dict(MOTIVOS_TRASLADO).get(self.motivo_traslado, self.motivo_traslado),
            'modalidad': dict(MODALIDADES_TRASLADO).get(self.modalidad_traslado, ''),
            'items': len(self.line_ids),
            'mensaje': self.l10n_pe_biller_message or '',
        }

    def l10n_pe_ne_guia_detalle(self):
        """Detalle completo para el formulario/PDF: cabecera + bienes."""
        self.ensure_one()
        c = self
        return {
            **self._l10n_pe_ne_guia_dict(),
            'serie': c.serie, 'correlativo': c.correlativo or '',
            'destinatarioId': c.partner_id.id,
            'horaEmision': c.hora_emision or '',
            'obsGuia': c.obs_guia or '',
            'motivoTraslado': c.motivo_traslado, 'desMotivoTraslado': c.des_motivo_traslado or '',
            'modalidadTraslado': c.modalidad_traslado,
            'pesoBruto': c.peso_bruto, 'uniMedidaPeso': c.uni_medida_peso, 'numBultos': c.num_bultos,
            'fechaInicioTraslado': c.fecha_inicio_traslado.strftime('%Y-%m-%d') if c.fecha_inicio_traslado else '',
            'ubigeoPartida': c.ubigeo_partida or '', 'dirPartida': c.dir_partida or '',
            'ubigeoLlegada': c.ubigeo_llegada or '', 'dirLlegada': c.dir_llegada or '',
            'numPlaca': c.num_placa or '',
            'conductorTipoDoc': c.conductor_tipo_doc or '1', 'conductorNumDoc': c.conductor_num_doc or '',
            'conductorNombres': c.conductor_nombres or '', 'conductorApellidos': c.conductor_apellidos or '',
            'conductorLicencia': c.conductor_licencia or '',
            'transportistaId': c.transportista_id.id if c.transportista_id else None,
            'transportista': c.transportista_id.name if c.transportista_id else '',
            'numRegMtc': c.num_reg_mtc or '',
            'proveedorId': c.proveedor_id.id if c.proveedor_id else None,
            'proveedor': c.proveedor_id.name if c.proveedor_id else '',
            'comprobanteId': c.comprobante_id.id if c.comprobante_id else None,
            'bienes': [{
                'descripcion': l.descripcion, 'cantidad': l.cantidad, 'unidad': l.unidad or 'NIU',
                'productId': l.product_id.id or None, 'codigo': l.product_id.default_code or '',
            } for l in c.line_ids],
        }

    def l10n_pe_ne_get_files(self, kind=None):
        """Devuelve {xml, cdr, pdf} en base64, para que el controller los sirva. El PDF (QWeb) se
        renderiza a demanda; xml/cdr salen de los adjuntos guardados al emitir."""
        self.ensure_one()
        def b64(att):
            v = att.datas
            return v.decode('ascii') if isinstance(v, (bytes, bytearray)) else (v or '')
        out = {}
        if self.l10n_pe_biller_xml:
            out['xml'] = b64(self.l10n_pe_biller_xml)
        if self.l10n_pe_biller_cdr:
            out['cdr'] = b64(self.l10n_pe_biller_cdr)
        if kind in (None, 'pdf'):
            try:
                pdf, _ct = self.env['ir.actions.report']._render_qweb_pdf(
                    'l10n_pe_ne_biller.action_report_guia', res_ids=self.ids)
                out['pdf'] = base64.b64encode(pdf).decode()
            except Exception:  # noqa: BLE001
                if kind == 'pdf':
                    raise
        return out

    # --------------------------------------------------- representación impresa
    def l10n_pe_ne_qr_data(self):
        """Contenido del QR de la representación impresa: la URL que SUNAT devolvió en el
        CDR aceptado. Sin CDR aceptado no hay QR (la guía aún no sustenta el traslado)."""
        self.ensure_one()
        return self.l10n_pe_ne_qr_url or ''

    def l10n_pe_ne_qr_datauri(self):
        """QR como data-URI PNG para la representación impresa. '' si no se puede generar."""
        self.ensure_one()
        if not self.l10n_pe_ne_qr_data():
            return ''
        try:
            png = self.env['ir.actions.report'].barcode('QR', self.l10n_pe_ne_qr_data(), width=220, height=220)
            return 'data:image/png;base64,' + base64.b64encode(png).decode()
        except Exception:  # noqa: BLE001
            return ''

    # ------------------------------------------------------------- API React
    @api.model
    def l10n_pe_ne_list_guias(self, query=None, limit=100, offset=None):
        """Lista de guías (para la UI). Paginación opt-in: con `offset` devuelve {items,total}."""
        domain = [('company_id', '=', self.env.company.id)]
        if query:
            q = query.strip()
            domain += ['|', ('name', 'ilike', q), ('partner_id.name', 'ilike', q)]
        recs = self.search(domain, limit=limit, offset=offset or 0)
        items = [g._l10n_pe_ne_guia_dict() for g in recs]
        if offset is None:
            return items
        return {'items': items, 'total': self.search_count(domain)}

    def _l10n_pe_ne_resolve_destinatario(self, payload):
        partner = False
        if payload.get('destinatarioId'):
            partner = self.env['res.partner'].browse(int(payload['destinatarioId'])).exists()
        if not partner and payload.get('destinatario'):
            partner = self.env['account.move']._l10n_pe_ne_quick_partner(payload['destinatario'])
        if not partner:
            raise UserError(_('Indica el destinatario de la guía.'))
        return partner

    def _l10n_pe_ne_build_guia_lines(self, items):
        vals = []
        for it in (items or []):
            desc = (it.get('descripcion') or '').strip()
            prod = False
            if it.get('productId'):
                prod = self.env['product.product'].browse(int(it['productId'])).exists()
                if prod and not desc:
                    desc = prod.display_name
            if not desc:
                raise UserError(_('Cada bien necesita una descripción (o un producto).'))
            vals.append((0, 0, {
                'product_id': prod.id if prod else False,
                'descripcion': desc,
                'cantidad': float(it.get('cantidad') or 1),
                'unidad': it.get('unidad') or (prod.l10n_pe_ne_unit_code if prod else '') or 'NIU',
            }))
        return vals

    def _l10n_pe_ne_guia_header_vals(self, payload):
        """Traduce la cabecera del payload de React a vals de escritura (sin partner ni líneas)."""
        vals = {}
        strmap = {
            'serie': 'serie', 'obsGuia': 'obs_guia', 'horaEmision': 'hora_emision',
            'motivoTraslado': 'motivo_traslado', 'desMotivoTraslado': 'des_motivo_traslado',
            'modalidadTraslado': 'modalidad_traslado', 'uniMedidaPeso': 'uni_medida_peso',
            'numPlaca': 'num_placa', 'conductorTipoDoc': 'conductor_tipo_doc',
            'conductorNumDoc': 'conductor_num_doc', 'conductorNombres': 'conductor_nombres',
            'conductorApellidos': 'conductor_apellidos', 'conductorLicencia': 'conductor_licencia',
            'numRegMtc': 'num_reg_mtc',
            'ubigeoPartida': 'ubigeo_partida', 'dirPartida': 'dir_partida',
            'ubigeoLlegada': 'ubigeo_llegada', 'dirLlegada': 'dir_llegada',
        }
        for k, f in strmap.items():
            if k in payload:
                vals[f] = payload.get(k) or False
        if payload.get('pesoBruto') is not None:
            vals['peso_bruto'] = float(payload['pesoBruto'] or 0)
        if payload.get('numBultos') is not None:
            vals['num_bultos'] = int(payload['numBultos'] or 1)
        if payload.get('fecha'):
            vals['fecha_emision'] = payload['fecha']
        if payload.get('fechaInicioTraslado'):
            vals['fecha_inicio_traslado'] = payload['fechaInicioTraslado']
        if 'transportistaId' in payload:
            vals['transportista_id'] = int(payload['transportistaId']) if payload.get('transportistaId') else False
        if 'proveedorId' in payload:
            vals['proveedor_id'] = int(payload['proveedorId']) if payload.get('proveedorId') else False
        if 'comprobanteId' in payload:
            vals['comprobante_id'] = int(payload['comprobanteId']) if payload.get('comprobanteId') else False
        return vals

    @api.model
    def l10n_pe_ne_quick_guia(self, payload):
        """Crea una guía (borrador) desde el payload de React."""
        payload = payload or {}
        partner = self._l10n_pe_ne_resolve_destinatario(payload)
        lines = self._l10n_pe_ne_build_guia_lines(payload.get('items') or payload.get('bienes'))
        if not lines:
            raise UserError(_('La guía necesita al menos un bien.'))
        vals = self._l10n_pe_ne_guia_header_vals(payload)
        vals.update({'company_id': self.env.company.id, 'partner_id': partner.id, 'line_ids': lines})
        g = self.create(vals)
        return g._l10n_pe_ne_guia_dict()

    @api.model
    def l10n_pe_ne_update_guia(self, payload):
        """Reemplaza cabecera + bienes de una guía en borrador."""
        payload = payload or {}
        g = self.browse(int(payload.get('id') or 0)).exists()
        if not g:
            raise UserError(_('Guía no encontrada.'))
        if g.estado not in ESTADOS_EMITIBLES:
            raise UserError(_('Solo se puede editar una guía en borrador.'))
        vals = g._l10n_pe_ne_guia_header_vals(payload)
        # La serie es inmutable una vez numerada la guía (el correlativo depende de
        # ella): honrarla aquí emitiría p.ej. T002-1 duplicando el de otra guía.
        vals.pop('serie', None)
        if payload.get('destinatarioId') or payload.get('destinatario'):
            vals['partner_id'] = g._l10n_pe_ne_resolve_destinatario(payload).id
        if payload.get('items') is not None or payload.get('bienes') is not None:
            vals['line_ids'] = [(5, 0, 0)] + g._l10n_pe_ne_build_guia_lines(
                payload.get('items') or payload.get('bienes'))
        g.write(vals)
        return g._l10n_pe_ne_guia_dict()

    @api.model
    def l10n_pe_ne_guia_prefill(self, move_id):
        """Datos para precargar una guía desde un comprobante (factura/boleta): destinatario +
        bienes (líneas del comprobante) + el comprobante como documento relacionado. NO crea nada;
        el front abre el formulario de guía con esto y el usuario completa el traslado/transporte."""
        move = self.env['account.move'].browse(int(move_id or 0)).exists()
        if not move:
            raise UserError(_('Comprobante no encontrado.'))
        p = move.partner_id
        bienes = [{
            'descripcion': ln.name or (ln.product_id.display_name or ''),
            'cantidad': ln.quantity or 1.0,
            'unidad': move._l10n_pe_unit_code(ln),
            'productId': ln.product_id.id or None,
            'codigo': ln.product_id.default_code or '',
        } for ln in move._l10n_pe_product_lines()]
        part = move.company_id.partner_id
        return {
            'comprobanteId': move.id,
            'comprobanteNumero': '%s-%s' % (move.l10n_pe_ne_serie_emit or '', move.l10n_pe_ne_corr_emit or ''),
            'destinatario': {'id': p.id, 'razonSocial': p.name or '', 'numDoc': p.vat or ''},
            'bienes': bienes,
            'ubigeoPartida': (part.l10n_pe_district.code or '') if part.l10n_pe_district else '',
            'dirPartida': part.street or '',
            'dirLlegada': p.street or '',
        }

    @api.model
    def l10n_pe_ne_delete_guia(self, rec_id):
        g = self.browse(int(rec_id or 0)).exists()
        if g:
            if g.estado == 'enviado':
                raise UserError(_('No se puede eliminar una guía ya aceptada por SUNAT.'))
            g.unlink()
        return {'ok': True, 'modo': 'eliminado'}


class L10nPeNeGuiaRemisionLine(models.Model):
    _name = 'l10n_pe_ne.guia_remision.line'
    _description = 'Bien de la guía de remisión'
    _order = 'id'

    guia_id = fields.Many2one('l10n_pe_ne.guia_remision', string='Guía',
                              required=True, ondelete='cascade', index=True)
    product_id = fields.Many2one('product.product', string='Producto')
    descripcion = fields.Char(string='Descripción', required=True)
    cantidad = fields.Float(string='Cantidad', default=1.0)
    unidad = fields.Char(string='Unidad (cat. 03)', default='NIU')
    company_id = fields.Many2one(related='guia_id.company_id', store=True, index=True)

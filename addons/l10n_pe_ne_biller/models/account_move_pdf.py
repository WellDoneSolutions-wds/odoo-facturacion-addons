# -*- coding: utf-8 -*-
"""account.move — Descargas / representación impresa (SFS 2.4).
Extraído de account_move_biller.py (refactor sin cambio de comportamiento)."""
import io
import requests
import zipfile
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    # ------------------------------------------------- descargas / PDF (SFS 2.4)
    @staticmethod
    def _l10n_pe_download_url(attachment):
        """Acción de descarga directa del adjunto vía /web/content."""
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=true" % attachment.id,
            "target": "self",
        }

    def _l10n_pe_ne_medios_pago_texto(self):
        """Detalle de medios de pago del POS ('Efectivo S/ 50.00, Yape S/ 68.00') para la
        representación impresa. NO va al XML SUNAT (es interno del punto de venta). Devuelve
        "" si no hay medios con importe (medio sin detallar → no se muestra el bloque). Lo usan
        el ticket 80mm (dentro del bloque POS `adicionalTxt`) y el A4 (param `MEDIOS_PAGO`)."""
        self.ensure_one()
        medios = self.l10n_pe_ne_medios_pago or []

        def _txt(m):
            base = "%s S/ %.2f" % (m.get("medio") or "", float(m.get("monto") or 0))
            op = str(m.get("numOp") or "").strip()
            return "%s (Op. %s)" % (base, op) if op else base

        return ", ".join(_txt(m) for m in medios if float(m.get("monto") or 0) > 0)

    def _l10n_pe_ne_observacion_impresa(self):
        """Observación general del comprobante para la representación impresa (ticket + A4).
        Print-only (NO va al XML firmado). Devuelve 'Observación: <texto>' o '' si no hay nota."""
        self.ensure_one()
        nota = html2plaintext(self.narration or "").strip()
        return ("Observación: " + nota) if nota else ""

    def _l10n_pe_ne_inicial_credito_lineas(self):
        """Líneas print-only de la INICIAL AL CONTADO de una venta al crédito. El inicial ya pagado
        NO va al XML SUNAT (allí solo van el saldo pendiente y las cuotas), así que sin esto el
        impreso mostraría cuotas que suman MENOS que el total, con un hueco sin explicar. Devuelve
        ['Inicial pagada al contado: S/ X', 'Saldo a crédito: S/ Y'] o [] si no aplica."""
        self.ensure_one()
        if self.l10n_pe_ne_forma_pago != "Credito":
            return []
        inicial = self.l10n_pe_ne_inicial_contado or 0.0
        if inicial <= 0:
            return []
        return [
            "Inicial pagada al contado: S/ %.2f" % inicial,
            "Saldo a crédito: S/ %.2f" % self._l10n_pe_credito_pendiente(),
        ]

    def _l10n_pe_ne_cobro_efectivo(self):
        """(redondeo, a_pagar, pagado, vuelto) del cobro en efectivo — dato de CAJA, no del XML.
        El comprobante mantiene amount_total; en efectivo se cobra a_pagar = amount_total + redondeo
        (redondeo ≤ 0, a favor del consumidor), y el vuelto = pagado − a_pagar. Fuente única para el
        ticket 80mm y el A4 (que antes solo mostraba los medios, sin el vuelto)."""
        self.ensure_one()
        medios = self.l10n_pe_ne_medios_pago or []
        redondeo = self.l10n_pe_ne_redondeo or 0.0
        a_pagar = round((self.amount_total or 0.0) + redondeo, 2)
        pagado = sum(float(m.get("monto") or 0) for m in medios)
        vuelto = round(pagado - a_pagar, 2)
        return redondeo, a_pagar, pagado, vuelto

    def _l10n_pe_ne_medios_pago_a4(self):
        """Texto de medios de pago para el A4 (param MEDIOS_PAGO), enriquecido con el redondeo de
        efectivo y el vuelto — el ticket 80mm ya los trae en su bloque POS, el A4 solo mostraba los
        medios. Todos son datos de caja (NO van al XML SUNAT). "" si no hay medios detallados."""
        self.ensure_one()
        partes = []
        det = self._l10n_pe_ne_medios_pago_texto()
        if det:
            partes.append(det)
            redondeo, a_pagar, _pagado, vuelto = self._l10n_pe_ne_cobro_efectivo()
            if redondeo:
                partes.append("Redondeo S/ %.2f" % redondeo)
                partes.append("A pagar efectivo S/ %.2f" % a_pagar)
            if vuelto > 0:
                partes.append("Vuelto S/ %.2f" % vuelto)
        # Inicial al contado del crédito: se muestra aunque no haya medios POS detallados (venta a
        # crédito desde "Nuevo comprobante"), para que el impreso cuadre con el total.
        partes.extend(self._l10n_pe_ne_inicial_credito_lineas())
        return " · ".join(partes)

    def _l10n_pe_ne_ticket_adicional(self):
        """Bloque de pago del ticket 80mm (se manda como `adicionalTxt`): medios de pago del
        POS, vuelto, cajero y observación. Estos datos NO van al XML SUNAT (son internos del
        punto de venta), pero sí a la representación impresa. Devuelve HTML simple (el textField
        usa markup html) o "" si no hay nada que mostrar."""
        self.ensure_one()
        partes = []
        det = self._l10n_pe_ne_medios_pago_texto()
        if det:
            partes.append("Pago: " + det)
            # Redondeo de efectivo (≤ 0): el comprobante mantiene amount_total, pero en efectivo se
            # cobra 'a pagar' = amount_total + redondeo. El vuelto se calcula contra ese importe.
            redondeo, a_pagar, _pagado, vuelto = self._l10n_pe_ne_cobro_efectivo()
            if redondeo:
                partes.append("Redondeo: S/ %.2f" % redondeo)
                partes.append("A pagar efectivo: S/ %.2f" % a_pagar)
            if vuelto > 0:
                partes.append("Vuelto: S/ %.2f" % vuelto)
        # Inicial al contado del crédito (no va al XML; imprescindible en el impreso para explicar
        # por qué las cuotas suman menos que el total).
        partes.extend(self._l10n_pe_ne_inicial_credito_lineas())
        if self.invoice_user_id:
            partes.append("Atendido por: " + (self.invoice_user_id.name or ""))
        obs = self._l10n_pe_ne_observacion_impresa()
        if obs:
            partes.append(obs)
        # El micro (sanitizarAdicional) escapa el HTML y traduce '\n' -> <br/>; se envía texto plano.
        return "\n".join(partes)

    def _l10n_pe_get_pdf_attachment(self, formato="A4"):
        """Devuelve (o genera y cachea) el PDF de la representación impresa pidiéndolo al micro
        (POST /report/pdf con el XML firmado). El micro lo renderiza con las plantillas del SFS 2.4.
        formato: 'A4' (SFS 2.4) o 'TICKET' (80mm, solo 01/03; otros tipos caen al A4)."""
        self.ensure_one()
        tipo, serie, correlativo = self._l10n_pe_baja_identidad()
        es_ticket = formato == "TICKET" and tipo in ("01", "03")
        if formato == "TICKET" and not es_ticket:
            return self._l10n_pe_get_pdf_attachment()  # fallback A4 (NC/ND/retención…)
        cache_field = "l10n_pe_biller_pdf_ticket" if es_ticket else "l10n_pe_biller_pdf"
        # Cache-busting: el PDF cacheado se etiqueta con la versión del template
        # (config `pdf_ver`). Si esa versión cambió (mejora del template) o el PDF
        # viejo no la trae, se descarta y se regenera → nadie ve representaciones
        # desactualizadas. Para forzar regeneración masiva, subir el parámetro.
        pdf_ver = self._l10n_pe_pdf_ver()
        cached = self[cache_field]
        if cached:
            if cached.description == pdf_ver:
                return cached
            cached.sudo().unlink()  # template cambió → descartar el PDF viejo
        if not self.l10n_pe_biller_xml:
            raise UserError(
                _("El comprobante no tiene XML firmado; envíelo primero a SUNAT.")
            )
        icp = self.env["ir.config_parameter"].sudo()
        base = icp.get_param("l10n_pe_ne_biller.url", "http://localhost:8090").rstrip(
            "/"
        )
        # Clave propia: reusar l10n_pe_ne_biller.timeout hacía que subir el
        # timeout de emisión (240s) arrastrara también la espera de un PDF.
        timeout = int(icp.get_param("l10n_pe_ne_biller.pdf_timeout", "60"))
        # Chofer(es) del/los vehículo(s) de combustible (acompañan a la placa por línea). NO va al
        # XML SUNAT (no es campo electrónico; solo la placa 7000 lo es) — es un dato de impresión,
        # como "Atendido por". Se juntan los distintos no vacíos de las líneas (grifo = uno solo).
        chofer = " / ".join(dict.fromkeys(
            c for c in ((l.l10n_pe_ne_chofer or "").strip() for l in self.invoice_line_ids) if c
        ))
        payload = {
            "ruc": self.company_id.vat or "",
            "tipoDoc": tipo,
            "xml": (self.l10n_pe_biller_xml.raw or b"").decode("utf-8"),
            # Serie-correlativo AUTORITATIVO desde Odoo (no se depende del xpath /Invoice/ID
            # de la plantilla, que en algún entorno no resolvía y dejaba el número en blanco).
            "numComprobante": "%s-%s" % (serie, (correlativo or "").zfill(8)),
            # Dirección del adquiriente para la representación impresa (no va al XML SUNAT: el
            # biller no la incluye en el bloque del cliente). Toma calle + urbanización si hay.
            "dirCliente": ", ".join(
                p for p in (self.partner_id.street, self.partner_id.street2) if p
            ),
            # Vendedor/cajero que atendió (no va al XML SUNAT): va en ambos formatos como
            # "Atendido por" (el ticket ya lo traía en el bloque POS; ahora también el A4).
            "atendidoPor": self.invoice_user_id.name or "",
            # Chofer del vehículo (combustible): dato de impresión, NO va al XML SUNAT (ver arriba).
            "chofer": chofer,
            # Medios de pago del POS (Efectivo/Yape/Plin…): NO van al XML SUNAT. El A4 los
            # muestra junto a la forma de pago (param MEDIOS_PAGO); el ticket ya los trae en
            # el bloque POS (adicionalTxt). "" si no hay medios detallados.
            "mediosPago": self._l10n_pe_ne_medios_pago_texto(),
            # Datos de pago del emisor (cuentas bancarias / CCI / Yape-Plin): texto libre de la
            # compañía (el MISMO campo que ya imprime la cotización). NO va al XML SUNAT; el biller
            # lo imprime en el bloque "DATOS DE PAGO" del pie. Se manda con el nombre del banco en
            # <b> (styled text de Jasper) para distinguirlo de los números — mismo formato que la
            # cotización. "" si no está configurado → el PDF no dibuja el bloque.
            "datosPago": str(self.company_id._l10n_pe_ne_datos_pago_marcado()),
        }
        # Logo del emisor (si lo tiene): va en ambos formatos (A4 y ticket).
        logo = self.company_id.logo
        if logo:
            payload["logo"] = logo.decode() if isinstance(logo, bytes) else logo
        if es_ticket:
            payload["formato"] = "TICKET"
            # Bloque de pago (medios/vuelto/cajero/nota) — solo en el ticket 80mm.
            adic = self._l10n_pe_ne_ticket_adicional()
            if adic:
                payload["adicionalTxt"] = adic
            # Contacto del emisor (no va al XML SUNAT): teléfono y correo de la compañía.
            contacto = "   ·   ".join(
                p for p in (
                    ("Tel: " + self.company_id.phone) if self.company_id.phone else "",
                    self.company_id.email or "",
                ) if p
            )
            if contacto:
                payload["contactoEmisor"] = contacto
        else:
            # A4: la observación va como adicionalTxt. El bloque POS (medios + redondeo + vuelto) no
            # tiene recuadro propio en el A4 como en el ticket, pero el A4 sí imprime MEDIOS_PAGO junto
            # a "Forma de pago" — así que se enriquece con el redondeo/vuelto (el ticket ya los trae en
            # su bloque). El param MEDIOS_PAGO NO lo usa la plantilla del ticket, no hay duplicación.
            payload["mediosPago"] = self._l10n_pe_ne_medios_pago_a4()
            obs = self._l10n_pe_ne_observacion_impresa()
            if obs:
                payload["adicionalTxt"] = obs
        headers = {"X-Api-Key": self.company_id.sudo().l10n_pe_ne_api_key or ""}
        try:
            resp = requests.post(
                base + "/report/pdf",
                json=payload,
                headers=headers,
                timeout=(5, timeout),
            )
        except requests.RequestException as exc:
            raise UserError(_("Error de conexión con el facturador: %s") % exc)
        if resp.status_code != 200 or not resp.content.startswith(b"%PDF"):
            raise UserError(
                _("El facturador no devolvió un PDF (HTTP %s): %s")
                % (resp.status_code, (resp.text or "")[:500])
            )
        att = self.env["ir.attachment"].create(
            {
                "name": "%s-%s-%s%s.pdf"
                % (
                    self.company_id.vat or "",
                    serie,
                    correlativo.zfill(8),
                    "-ticket" if es_ticket else "",
                ),
                "res_model": "account.move",
                "res_id": self.id,
                "mimetype": "application/pdf",
                "raw": resp.content,
                "description": pdf_ver,   # etiqueta de versión para el cache-busting
            }
        )
        self[cache_field] = att.id
        return att

    def _l10n_pe_ne_is_aceptado(self):
        """True solo si el comprobante fue aceptado por SUNAT: estado 'enviado',
        con CDR guardado y ResponseCode 0. Re-parsea el CDR (no confía en el texto
        de l10n_pe_biller_message)."""
        self.ensure_one()
        if self.l10n_pe_biller_state != "enviado" or not self.l10n_pe_biller_cdr:
            return False
        code, _desc = self._l10n_pe_parse_cdr_codes(self.l10n_pe_biller_cdr.raw or b"")
        return code == "0"

    def l10n_pe_ne_email_comprobante(self, to=None, cc=None):
        """Envía el comprobante aceptado al cliente por correo, adjuntando el PDF
        (representación impresa SFS) y el XML firmado, vía la plantilla
        l10n_pe_ne_biller.mail_template_comprobante."""
        self.ensure_one()
        if not self._l10n_pe_ne_is_aceptado():
            raise UserError(
                _("El comprobante no está aceptado por SUNAT; no se puede enviar.")
            )
        to = (to or self.partner_id.email or "").strip()
        if not to:
            raise UserError(
                _("El cliente no tiene correo configurado; indica un destinatario.")
            )
        pdf = self._l10n_pe_get_pdf_attachment()
        xml = self.l10n_pe_biller_xml
        template = self.env.ref("l10n_pe_ne_biller.mail_template_comprobante")
        template.send_mail(
            self.id,
            force_send=True,
            raise_exception=True,
            email_values={
                "email_to": to,
                "email_cc": cc or "",
                "attachment_ids": [(6, 0, [pdf.id, xml.id])],
            },
        )
        self.message_post(body=_("Comprobante enviado por correo a %s") % to)
        return {"ok": True, "to": to}

    def action_l10n_pe_download_pdf(self):
        self.ensure_one()
        return self._l10n_pe_download_url(self._l10n_pe_get_pdf_attachment())

    def action_l10n_pe_download_ticket(self):
        self.ensure_one()
        return self._l10n_pe_download_url(self._l10n_pe_get_pdf_attachment(formato="TICKET"))

    def action_l10n_pe_download_xml(self):
        self.ensure_one()
        if not self.l10n_pe_biller_xml:
            raise UserError(_("El comprobante no tiene XML firmado."))
        return self._l10n_pe_download_url(self.l10n_pe_biller_xml)

    def action_l10n_pe_download_cdr(self):
        self.ensure_one()
        if not self.l10n_pe_biller_cdr:
            raise UserError(_("El comprobante no tiene CDR de SUNAT."))
        return self._l10n_pe_download_url(self.l10n_pe_biller_cdr)

    def action_l10n_pe_download_zip(self):
        """Empaqueta en un ZIP el XML firmado, el CDR y el PDF de los comprobantes seleccionados.
        El PDF se genera al vuelo (best-effort) si aún no existe."""
        buf = io.BytesIO()
        incluidos = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for move in self:
                if move.l10n_pe_biller_xml:
                    zf.writestr(
                        move.l10n_pe_biller_xml.name, move.l10n_pe_biller_xml.raw or b""
                    )
                    incluidos += 1
                if move.l10n_pe_biller_cdr:
                    zf.writestr(
                        move.l10n_pe_biller_cdr.name, move.l10n_pe_biller_cdr.raw or b""
                    )
                    incluidos += 1
                try:
                    pdf = (
                        move._l10n_pe_get_pdf_attachment()
                        if move.l10n_pe_biller_xml
                        else False
                    )
                    if pdf:
                        zf.writestr(pdf.name, pdf.raw or b"")
                        incluidos += 1
                except UserError as exc:
                    # PDF best-effort: si el micro falla, el ZIP igual lleva XML + CDR; se registra el motivo.
                    _logger.warning(
                        "No se pudo generar el PDF de %s para el ZIP: %s",
                        move.name,
                        exc,
                    )
        if not incluidos:
            raise UserError(
                _("Los comprobantes seleccionados no tienen archivos para descargar.")
            )
        att = self.env["ir.attachment"].create(
            {
                "name": "comprobantes_sunat.zip",
                "mimetype": "application/zip",
                "raw": buf.getvalue(),
            }
        )
        return self._l10n_pe_download_url(att)


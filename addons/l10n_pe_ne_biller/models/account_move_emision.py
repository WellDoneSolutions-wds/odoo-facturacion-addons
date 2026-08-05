# -*- coding: utf-8 -*-
"""account.move — Emisión asíncrona + acción de envío.
Extraído de account_move_biller.py (refactor sin cambio de comportamiento)."""
import base64
import json
import requests
import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from .account_move_biller import _BOTO_CLIENTS
from . import account_move_biller as _base

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    # -------------------------------------------------------- emisión asíncrona
    # Toggle: ir.config_parameter `l10n_pe_ne_biller.async_enabled` = "1".
    # Odoo encola en SQS (rol IAM del EC2, patrón del sibling partner_lookup) y
    # responde al instante; el Lambda facturas-worker procesa contra biller-core
    # con idempotencia (DynamoDB) y deja XML/CDR en S3; el cron de abajo recoge.

    @api.model
    def _l10n_pe_boto_client(self, service, region):
        """Cliente boto3 memoizado por (service, region). Crear un cliente
        cuesta 100-400ms de CPU (carga los modelos JSON del servicio) y se
        pagaba dos veces POR EMISIÓN (dynamodb + sqs). El cache vive por
        worker de Odoo (prefork: se puebla post-fork, sin estado compartido
        entre procesos; los clientes boto3 son thread-safe para invocar).
        Se reconstruye si el módulo boto3 cambió (tests que lo parchean)."""
        key = (service, region)
        cached = _BOTO_CLIENTS.get(key)
        if cached is not None and cached[0] is _base.boto3:
            return cached[1]
        client = _base.boto3.client(service, region_name=region)
        _BOTO_CLIENTS[key] = (_base.boto3, client)
        return client

    def _l10n_pe_enqueue_emission(self, icp):
        self.ensure_one()
        queue_url = icp.get_param("l10n_pe_ne_biller.sqs_queue_url", "")
        region = icp.get_param("l10n_pe_ne_biller.aws_region", "us-east-1")
        if not _base.boto3 or not queue_url:
            self.l10n_pe_biller_state = "error"
            self.l10n_pe_biller_message = _(
                "Modo asíncrono activo pero falta boto3 o el parámetro "
                "l10n_pe_ne_biller.sqs_queue_url."
            )
            return
        endpoint, payload = self._l10n_pe_target()
        serie, correlativo = self._l10n_pe_serie_correlativo()
        msg = {
            "ruc": self.company_id.vat or "",
            "serie_correlativo": "%s-%s" % (serie, correlativo.zfill(8)),
            "db": self.env.cr.dbname,
            "move_id": self.id,
            "path": "/generator/" + endpoint,
            "api_key": self.company_id.sudo().l10n_pe_ne_api_key or "",
            # tipoDoc (01/03/07/08) para que el worker pre-genere el PDF
            "doc_type": self._l10n_pe_document_type(),
            "payload": payload,
        }
        # Reintento tras un rechazo: borra el resultado viejo ANTES de encolar,
        # para que el cron no aplique el resultado obsoleto mientras el worker
        # procesa el intento nuevo (best-effort: si no existe, no pasa nada).
        table = icp.get_param("l10n_pe_ne_biller.results_table", "")
        if table:
            try:
                self._l10n_pe_boto_client("dynamodb", region).delete_item(
                    TableName=table,
                    Key={
                        "ruc_emisor": {"S": msg["ruc"]},
                        "serie_correlativo": {"S": msg["serie_correlativo"]},
                    },
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning("async biller: no se pudo limpiar resultado previo: %s", exc)
        try:
            self._l10n_pe_boto_client("sqs", region).send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(msg, ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001
            self.l10n_pe_biller_state = "error"
            self.l10n_pe_biller_message = _("No se pudo encolar la emisión: %s") % exc
            return
        # Re-emisión tras rechazado/error: el XML firmado y los PDFs del intento
        # anterior quedan obsoletos (el worker firmará uno nuevo). Sin esto, el
        # cache pdfver serviría la representación vieja para siempre y el PDF
        # nuevo del worker jamás se adjuntaría (guard "ya hay PDF" del attach).
        if self.l10n_pe_biller_xml:
            self.l10n_pe_biller_xml.unlink()
        self._l10n_pe_invalidar_pdfs()
        self.l10n_pe_biller_state = "en_proceso"
        self.l10n_pe_biller_message = _(
            "Encolado para envío a SUNAT — el resultado llega en unos minutos "
            "(aparece en el chatter; recargá la vista para ver el estado final)."
        )
        self._l10n_pe_trigger_poll_async(seconds=20)

    @api.model
    def _l10n_pe_trigger_poll_async(self, seconds=20):
        """Adelanta el próximo run del cron de recogida: sin esto el resultado
        espera el beat base de 2 min aunque el worker ya lo haya dejado en
        DynamoDB. Ojo con la expectativa: el scheduler de Odoo duerme beats
        fijos de ~60s y un trigger futuro NO lo despierta a call_at (ni con
        ODOO_NOTIFY_CRON_CHANGES: ese NOTIFY sale al commit, cuando el trigger
        aún no venció) — el pickup real es el primer beat posterior a call_at,
        o sea hasta ~60-70s después. Best-effort: si falla, el beat base sigue."""
        try:
            self.env.ref(
                "l10n_pe_ne_biller.ir_cron_l10n_pe_ne_poll_async"
            ).sudo()._trigger(at=fields.Datetime.now() + timedelta(seconds=seconds))
        except Exception as exc:  # noqa: BLE001
            _logger.warning("async biller: no se pudo adelantar el cron: %s", exc)

    def _l10n_pe_pdf_ver(self):
        """Etiqueta de versión del template de PDF (`description` del adjunto):
        _l10n_pe_get_pdf_attachment solo sirve el cache si coincide con el
        `pdf_ver` vigente; cualquier PDF que se adjunte debe llevarla."""
        return "pdfver:" + self.env["ir.config_parameter"].sudo().get_param(
            "l10n_pe_ne_biller.pdf_ver", "1"
        )

    def _l10n_pe_invalidar_pdfs(self):
        """Descarta los PDFs cacheados (A4 y ticket). Debe llamarse siempre que
        el XML firmado cambie (re-emisión tras rechazo/error): la representación
        impresa de un XML anterior no debe sobrevivir — el cache por `pdfver`
        solo detecta cambios de template, no de contenido."""
        self.ensure_one()
        for campo in ("l10n_pe_biller_pdf", "l10n_pe_biller_pdf_ticket"):
            att = self[campo]
            if att:
                try:
                    att.sudo().unlink()
                except Exception as exc:  # noqa: BLE001 — best-effort
                    _logger.warning(
                        "no se pudo descartar el PDF cacheado de %s: %s",
                        self.name, exc,
                    )

    def _l10n_pe_attach_async_pdf(self, s3c, bucket, item):
        """Adjunta el PDF pre-generado por el worker (pdf_s3_key del item), si
        ya existe y el move no tiene uno. Best-effort: si falta, el botón
        Descargar PDF cae al camino síncrono de siempre."""
        self.ensure_one()
        # El worker pre-genera el A4 SIN logo del emisor, dirección del cliente ni datos de pago
        # del emisor (el mensaje de la cola no los lleva, ver _l10n_pe_enqueue_emission). Si el
        # emisor tiene logo o datos de pago, o el cliente tiene dirección, ese PDF saldría
        # incompleto: NO lo adjuntamos y dejamos que la descarga lo regenere por la ruta síncrona
        # (_l10n_pe_get_pdf_attachment), que sí los incluye. Si no hay nada que agregar, reusamos
        # el del worker (más rápido, sin diferencia).
        if (self.company_id.logo or self.partner_id.street or self.partner_id.street2
                or self.company_id.l10n_pe_ne_datos_pago):
            return
        pdf_s3 = (item.get("pdf_s3_key") or {}).get("S", "")
        if not pdf_s3 or self.l10n_pe_biller_pdf:
            return
        try:
            pdf_bytes = s3c.get_object(Bucket=bucket, Key=pdf_s3)["Body"].read()
            if not pdf_bytes.startswith(b"%PDF"):
                _logger.warning(
                    "async biller: pdf_s3_key de %s no es un PDF; se ignora",
                    self.name,
                )
                return
            serie = self.l10n_pe_ne_serie_emit
            corr = self.l10n_pe_ne_corr_emit
            if not serie or not corr:
                serie, corr = self._l10n_pe_serie_correlativo()
                corr = corr.zfill(8)
            att = self.env["ir.attachment"].create(
                {
                    "name": "%s-%s-%s.pdf"
                    % (self.company_id.vat or "", serie, corr),
                    "res_model": "account.move",
                    "res_id": self.id,
                    "mimetype": "application/pdf",
                    "raw": pdf_bytes,
                    # Sin la etiqueta, la primera descarga vía API lo descartaba
                    # (cache-busting) y re-renderizaba contra el micro (~hasta 60s).
                    "description": self._l10n_pe_pdf_ver(),
                }
            )
            self.l10n_pe_biller_pdf = att.id
        except Exception as exc:  # noqa: BLE001 — PDF es best-effort
            _logger.warning(
                "async biller: PDF no adjuntado en %s: %s", self.name, exc
            )

    def _l10n_pe_async_attach_firmado(self, s3c, bucket, item):
        """Modo async: cuando el worker publica un item intermedio (status no
        terminal, p.ej. "firmado") con `xml_s3_key`, adjunta el XML firmado a
        `l10n_pe_biller_xml` y toma el PDF del worker si ya está (`pdf_s3_key`).
        Con el XML adjunto, la descarga funciona estando en_proceso aunque el PDF
        aún no llegue (el botón cae al camino on-demand de siempre). NO cambia el
        estado (sigue en_proceso) y NO genera el PDF localmente — el worker es el
        único generador; ver nota al final del cuerpo. Best-effort e idempotente:
        sin `xml_s3_key` no hace nada; con el XML ya adjunto solo intenta traer
        el PDF del worker."""
        self.ensure_one()
        if self.l10n_pe_biller_xml:
            # Ya adjuntado en una corrida previa: solo traer el PDF del worker si aún no está.
            self._l10n_pe_attach_async_pdf(s3c, bucket, item)
            return
        xml_key = (item.get("xml_s3_key") or {}).get("S", "")
        if not xml_key:
            return
        try:
            body = (
                s3c.get_object(Bucket=bucket, Key=xml_key)["Body"]
                .read()
                .decode("iso-8859-1")
            )
        except Exception as exc:  # noqa: BLE001 — aún no está en S3: se reintenta al próximo poll
            _logger.warning(
                "async biller: XML firmado aún no disponible en %s: %s", self.name, exc
            )
            return
        if not any(tag in body for tag in ("<Invoice", "<CreditNote", "<DebitNote")):
            return
        serie, correlativo = self._l10n_pe_serie_correlativo()
        att = self.env["ir.attachment"].create(
            {
                "name": "%s-%s-%s.xml"
                % (self.company_id.vat, serie, correlativo.zfill(8)),
                "res_model": "account.move",
                "res_id": self.id,
                "mimetype": "application/xml",
                # Normalizado a utf-8 igual que _l10n_pe_apply_emission_response, para que
                # el render del PDF (que decodifica utf-8) no rompa con tildes/ñ.
                "raw": body.encode("utf-8"),
            }
        )
        self.l10n_pe_biller_xml = att.id
        self.l10n_pe_ne_tipo_doc = self._l10n_pe_document_type()
        self.l10n_pe_ne_serie_emit = serie
        self.l10n_pe_ne_corr_emit = correlativo.zfill(8)
        # PDF: SOLO el pre-generado por el worker (pdf_s3_key). NO generarlo acá:
        # en la ventana "firmado" el worker ya está invocando biller-pdf con este
        # mismo XML — hacerlo también desde el cron duplicaba renders (A4+ticket
        # síncronos de hasta ~60s c/u DENTRO del loop del poll: un import masivo
        # bloqueaba el cron varios minutos) y el PDF del worker terminaba
        # descartado. Si el usuario descarga antes de que llegue, el botón usa el
        # camino on-demand de siempre — posible porque el XML ya quedó adjunto.
        self._l10n_pe_attach_async_pdf(s3c, bucket, item)

    @api.model
    def _l10n_pe_cron_poll_async(self):
        """Recoge resultados de emisiones asíncronas: lee el item del worker en
        DynamoDB (PK ruc_emisor / SK serie_correlativo) y, si terminó, baja el
        XML/CDR de S3 y lo aplica con el mismo código del flujo síncrono."""
        icp = self.env["ir.config_parameter"].sudo()
        if icp.get_param("l10n_pe_ne_biller.async_enabled", "").strip().lower() not in ("1", "true"):
            return
        table = icp.get_param("l10n_pe_ne_biller.results_table", "")
        bucket = icp.get_param("l10n_pe_ne_biller.results_bucket", "")
        region = icp.get_param("l10n_pe_ne_biller.aws_region", "us-east-1")
        if not _base.boto3 or not table or not bucket:
            _logger.warning(
                "async biller: faltan parámetros results_table/results_bucket o boto3"
            )
            return
        ddb = self._l10n_pe_boto_client("dynamodb", region)
        s3c = self._l10n_pe_boto_client("s3", region)
        moves = self.search([("l10n_pe_biller_state", "=", "en_proceso")], limit=25)
        for move in moves:
            try:
                serie, correlativo = move._l10n_pe_serie_correlativo()
                key = {
                    "ruc_emisor": {"S": move.company_id.vat or ""},
                    "serie_correlativo": {"S": "%s-%s" % (serie, correlativo.zfill(8))},
                }
                item = ddb.get_item(TableName=table, Key=key).get("Item")
                if not item:  # aún en cola o procesándose
                    continue
                status = item["status"]["S"]
                if status == "enviado":
                    xml_key = (item.get("xml_s3_key") or {}).get("S", "")
                    body = (
                        s3c.get_object(Bucket=bucket, Key=xml_key)["Body"]
                        .read()
                        .decode("iso-8859-1")
                    )
                    cdr_b64 = ""
                    cdr_key = (item.get("cdr_s3_key") or {}).get("S", "")
                    if cdr_key:
                        cdr_b64 = base64.b64encode(
                            s3c.get_object(Bucket=bucket, Key=cdr_key)["Body"].read()
                        ).decode()
                    move._l10n_pe_apply_emission_response(True, body, cdr_b64)
                    # PDF pre-generado por el worker: el botón "Descargar PDF"
                    # lo sirve cacheado, sin llamada síncrona al facturador.
                    move._l10n_pe_attach_async_pdf(s3c, bucket, item)
                elif status in ("rechazado", "error"):
                    move.l10n_pe_biller_state = status
                    move.l10n_pe_biller_message = (
                        (item.get("message") or {}).get("S") or ""
                    )[:2000]
                else:
                    # Item intermedio (p.ej. "firmado"): el worker ya firmó pero SUNAT aún
                    # no responde. Adjunta el XML firmado + PDF para que ticket/PDF estén
                    # disponibles AL TOQUE en en_proceso, sin esperar el CDR. Sigue en
                    # en_proceso: sin transición de estado no se postea al chatter ni se
                    # notifica (evita spam en cada corrida mientras el item no es final).
                    move._l10n_pe_async_attach_firmado(s3c, bucket, item)
                    continue
                # El form no refresca solo cuando escribe un cron: el chatter sí.
                move.message_post(
                    body=_("Facturador (async): %s — %s")
                    % (
                        dict(
                            move._fields["l10n_pe_biller_state"].selection
                        ).get(move.l10n_pe_biller_state, move.l10n_pe_biller_state),
                        (move.l10n_pe_biller_message or "")[:500],
                    )
                )
                # ...y el statusbar en vivo va por el bus (websocket): el JS
                # biller_live_statusbar recarga el form abierto al recibir esto.
                self.env["bus.bus"]._sendone(
                    "l10n_pe_biller_updates",
                    "l10n_pe_biller_update",
                    {"move_id": move.id, "state": move.l10n_pe_biller_state},
                )
            except Exception as exc:  # noqa: BLE001 — un move malo no frena al resto
                _logger.warning("async biller: error procesando %s: %s", move.name, exc)
        # Segundo pase — PDFs rezagados: el worker publica "enviado" ANTES de
        # generar el PDF, así que el pase de arriba suele aplicar el resultado
        # cuando pdf_s3_key aún no existe; se re-lee el item hasta que aparezca
        # (ventana corta: biller-pdf tarda segundos, ~2 min en cold start).
        sin_pdf = self.search(
            [
                ("l10n_pe_biller_state", "=", "enviado"),
                ("l10n_pe_biller_pdf", "=", False),
                ("write_date", ">=", fields.Datetime.now() - timedelta(minutes=15)),
            ],
            limit=25,
        )
        for move in sin_pdf:
            try:
                serie = move.l10n_pe_ne_serie_emit
                corr = move.l10n_pe_ne_corr_emit
                if not serie or not corr:
                    serie, corr = move._l10n_pe_serie_correlativo()
                    corr = corr.zfill(8)
                item = ddb.get_item(
                    TableName=table,
                    Key={
                        "ruc_emisor": {"S": move.company_id.vat or ""},
                        "serie_correlativo": {"S": "%s-%s" % (serie, corr)},
                    },
                ).get("Item")
                if item:
                    move._l10n_pe_attach_async_pdf(s3c, bucket, item)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "async biller: reconciliación PDF %s: %s", move.name, exc
                )
        # Re-poll corto mientras quede trabajo FRESCO (emisiones en curso o
        # PDFs por reconciliar). Acotado por edad: si el worker nunca escribió
        # el item (p.ej. mensaje muerto en la DLQ), el move zombi vuelve al
        # beat base de 2 min en vez de re-disparar el cron para siempre.
        limite = fields.Datetime.now() - timedelta(minutes=30)
        pendientes = moves.filtered(
            lambda m: m.l10n_pe_biller_state == "en_proceso"
            and m.write_date
            and m.write_date >= limite
        )
        if pendientes or sin_pdf.filtered(lambda m: not m.l10n_pe_biller_pdf):
            self._l10n_pe_trigger_poll_async(seconds=30)

    # ------------------------------------------------------------------ acción
    def action_l10n_pe_send_to_biller(self):
        _logger.info("Enviando facturas a Biller: %s", self.ids)
        icp = self.env["ir.config_parameter"].sudo()
        base = icp.get_param("l10n_pe_ne_biller.url", "http://localhost:8090").rstrip(
            "/"
        )
        _logger.info("URL: %s", base)
        # >240 es inalcanzable: limit_time_real=240 mata el worker de Odoo
        # antes (SIGKILL con rollback), con el POST quizá ya aceptado en SUNAT.
        timeout = int(icp.get_param("l10n_pe_ne_biller.timeout", "240"))
        _logger.info("Timeout: %s", timeout)
        use_async = icp.get_param(
            "l10n_pe_ne_biller.async_enabled", ""
        ).strip().lower() in ("1", "true")
        use_instant = icp.get_param(
            "l10n_pe_ne_biller.instant_enabled", ""
        ).strip().lower() in ("1", "true")
        for move in self:
            _logger.info(
                "Procesando factura: %s (%s)", move.name, move.l10n_pe_biller_state
            )
            if move.l10n_pe_biller_state in ("enviado", "en_proceso"):
                _logger.info("Factura ya enviada o en proceso: %s", move.name)
                continue
            # Guarda: no aplicar percepción a un cliente exceptuado del régimen (QA-028). El cobro
            # adicional no corresponde; se bloquea con un mensaje claro en vez de emitir mal.
            if move.l10n_pe_ne_percepcion and move.partner_id.l10n_pe_ne_exceptuado_percepcion:
                raise UserError(_(
                    "El cliente %s está exceptuado del régimen de percepciones; no corresponde "
                    "aplicarle percepción. Desactivá la percepción para emitir este comprobante."
                ) % (move.partner_id.display_name or ""))
            # Valida la serie (familia correcta + habilitada, QA-074) ANTES de asignar el
            # correlativo, para no consumir un número si la serie se rechaza.
            move._l10n_pe_check_serie()
            # Y que la serie sea la del local que el comprobante declara: la incoherencia
            # (serie de Miraflores declarando San Isidro) se corta aquí, también antes del
            # correlativo. Va en el envío y no solo en quick_emit para que cubra igual al
            # comprobante armado desde el backend de Odoo.
            move._l10n_pe_check_serie_establecimiento()
            # Fija la serie+correlativo fiscal ANTES de construir el payload/firmar, desde la
            # secuencia POR SERIE (no el folio del diario). A partir de aquí el número es estable
            # e igual en payload, XML firmado, QR, PDF y una eventual baja. Va DESPUÉS del guard
            # para no consumir un correlativo en un comprobante que se bloquea.
            move._l10n_pe_ne_assign_numero()
            if use_async:
                move._l10n_pe_enqueue_emission(icp)
                continue
            if use_instant:
                # Modo instantáneo: FIRMAR (rápido, sin SUNAT) → ticket/PDF ya disponibles y
                # estado 'en_proceso'. El cron _l10n_pe_cron_enviar_pendientes envía a SUNAT.
                endpoint, payload = move._l10n_pe_target()
                headers = {"X-Api-Key": move.company_id.sudo().l10n_pe_ne_api_key or ""}
                try:
                    resp = requests.post(
                        base + "/generator/" + endpoint + "/firmar",
                        json=payload, headers=headers, timeout=(5, 30),
                    )
                    if resp.status_code == 200:
                        move._l10n_pe_apply_signed(resp.json())
                    else:
                        move.l10n_pe_biller_state = "error"
                        move.l10n_pe_biller_message = (
                            "Firma HTTP %s: %s" % (resp.status_code, resp.text)
                        )[:2000]
                except requests.RequestException as exc:
                    move.l10n_pe_biller_state = "error"
                    move.l10n_pe_biller_message = (
                        _("Error de conexión con el facturador (firma): %s") % exc
                    )
                continue
            endpoint, payload = move._l10n_pe_target()
            _logger.info("AAAEnviando %s: %s", endpoint, payload)
            headers = {"X-Api-Key": move.company_id.sudo().l10n_pe_ne_api_key or ""}
            try:
                _logger.info("EEEEnviando %s: %s", endpoint, payload)
                resp = requests.post(
                    base + "/generator/" + endpoint,
                    json=payload,
                    headers=headers,
                    # connect corto aparte: un endpoint inalcanzable (SG, DNS)
                    # falla en 5s en vez de colgar el worker hasta el read.
                    timeout=(5, timeout),
                )
                _logger.info(
                    "RESP %s -> POST %s/generator/%s -> HTTP %s | %s",
                    move.name,
                    base,
                    endpoint,
                    resp.status_code,
                    resp.text[:500],
                )
                _logger.info("Respuesta: %s", resp.text)
            except requests.RequestException as exc:
                move.l10n_pe_biller_state = "error"
                _logger.error("Error: %s", exc)
                move.l10n_pe_biller_message = (
                    _("Error de conexión con el facturador: %s") % exc
                )
                continue
            # El biller devuelve el XML firmado como body y el CDR de SUNAT en
            # el header X-Sunat-Cdr (base64 del zip).
            move._l10n_pe_apply_emission_response(
                resp.status_code == 200, resp.text, resp.headers.get("X-Sunat-Cdr")
            )
        return True


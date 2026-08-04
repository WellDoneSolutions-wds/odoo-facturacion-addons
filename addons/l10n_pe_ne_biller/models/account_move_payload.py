# -*- coding: utf-8 -*-
"""account.move — Constructores del payload al facturador.
Extraído de account_move_biller.py (refactor sin cambio de comportamiento)."""
import base64
import io
import re
import requests
import zipfile
import logging

from odoo import _, api, fields, models
from .account_move_biller import ND_MOTIVO_DESC

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    # ----------------------------------------------------------- constructores
    def _l10n_pe_emisor(self):
        """Datos de empresa del emisor (desde res.company) para el request. Las credenciales y el
        certificado de firma quedan en el servidor indexados por RUC; aquí solo van datos NO secretos.
        El microservicio prefiere estos sobre su registro por RUC, campo a campo."""
        self.ensure_one()
        company = self.company_id
        partner = company.partner_id
        emisor = {
            "razonSocial": company.name or "",
            "nombreComercial": company.name or "",
        }
        # Dirección todo-o-nada: solo se envía si el distrito (ubigeo) está configurado, para no mezclar
        # datos reales con los del registro del micro campo a campo (coalesce).
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

    def _l10n_pe_build_invoice_request(self):
        """Factura (01) / Boleta (03) — endpoint /generator/factura."""
        _logger.info("---------------------------------------- Invoice request ------------------------------------------------")
        _logger.info(
            "%s %s %s",
            self._l10n_pe_id_block(with_document_type=True),
            self._l10n_pe_emisor(),
            self._l10n_pe_cabecera(),
        )
        _logger.info("---------------------------------------- Invoice request ------------------------------------------------")
        self.ensure_one()
        self._l10n_pe_check_lineas_impuesto()
        self._l10n_pe_check_anticipo()
        self._l10n_pe_ne_asegurar_valido()   # L1: reglas SUNAT (3265, boleta>700, detracción, …)
        _logger.info("Product lines: %s", len(self._l10n_pe_product_lines()))
        req = {
            "id": self._l10n_pe_id_block(with_document_type=True),
            "emisor": self._l10n_pe_emisor(),
            "cabecera": self._l10n_pe_cabecera(),
            "datoPago": self._l10n_pe_dato_pago(),
            "tributos": self._l10n_pe_tributos(),
            "detalle": self._l10n_pe_detalle(),
            "adicionalDetalle": self._l10n_pe_adicional_detalle(),
            "variablesGlobales": self._l10n_pe_variables_globales(),
            "leyendas": self._l10n_pe_leyendas(),
        }
        if self.l10n_pe_ne_forma_pago == "Credito":
            req["detallePago"] = self._l10n_pe_detalle_pago()
        relacionados = self._l10n_pe_relacionados()
        if relacionados:
            req["relacionados"] = relacionados
        return req

    def _l10n_pe_adicional_detalle(self):
        """Descuentos por ítem (cat. 53 código 00, que afecta la base del IGV) — hace explícito en
        el comprobante el descuento de cada línea con `discount` > 0. La línea ya va por su valor
        neto (IGV sobre el neto); este bloque solo lo muestra, no cambia los totales."""
        fmt = self._l10n_pe_fmt
        moneda = self.currency_id.name or "PEN"
        out = []
        idx = 0
        for line in self._l10n_pe_product_lines():
            idx += 1
            if not line.discount:
                continue
            gross = round(line.price_unit * line.quantity, 2)
            disc = round(gross - line.price_subtotal, 2)
            out.append(
                {
                    "idLinea": str(idx),
                    # "-" en las propiedades para que la plantilla salte el bloque AdditionalItemProperty
                    # (la misma lista sirve para descuentos y propiedades; sin esto el render falla).
                    "nomPropiedad": "-",
                    "codBienPropiedad": "-",
                    "tipVariable": "false",
                    "codTipoVariable": "00",
                    # Factor con 5 decimales: SUNAT valida mtoVariable ≈ base × porVariable (error 3290,
                    # "cargo/descuento por ítem difiere"). Con 2 decimales, un descuento en monto fijo
                    # (p.ej. S/50 sobre 470 → 10.6383% → 0.11) descuadra y se rechaza; 5 decimales
                    # reconstruyen el monto dentro de la tolerancia.
                    "porVariable": "%.5f" % (line.discount / 100.0),
                    "monMontoVariable": moneda,
                    "mtoVariable": fmt(disc),
                    "monBaseImponibleVariable": moneda,
                    "mtoBaseImpVariable": fmt(gross),
                }
            )
        # Ventas al Estado: 4 propiedades del proceso de contratación pública (cat. 55) por CADA
        # línea, como cac:AdditionalItemProperty. SUNAT (reglas 3146-3149) las valida como GRUPO:
        # van las 4 juntas o ninguna → solo se emiten si están las 4 completas.
        estado = [
            ("5000", "Numero de Expediente", self.l10n_pe_ne_estado_expediente),
            ("5001", "Codigo de Unidad Ejecutora", self.l10n_pe_ne_estado_unidad_ejecutora),
            ("5002", "Numero de Proceso de Seleccion", self.l10n_pe_ne_estado_proceso_seleccion),
            ("5003", "Numero de Contrato", self.l10n_pe_ne_estado_contrato),
        ]
        if all((v or "").strip() for _c, _n, v in estado):
            for li in range(1, idx + 1):  # idx = nº de líneas de producto contadas arriba
                for cod, nom, val in estado:
                    out.append(
                        {
                            "idLinea": str(li),
                            # no es descuento/cargo: "-" salta el loop de AllowanceCharge por ítem
                            "codTipoVariable": "-",
                            # dispara el bloque cac:AdditionalItemProperty en el FTL
                            "nomPropiedad": nom,
                            "codPropiedad": cod,
                            "valPropiedad": val.strip(),
                            "codBienPropiedad": "-",
                            "fecInicioPropiedad": "-",
                            "horInicioPropiedad": "-",
                            "fecFinPropiedad": "-",
                            "numDiasPropiedad": "-",
                        }
                    )
        # Placa del vehículo (factura de combustible): cac:AdditionalItemProperty cat-55 código 7000
        # (Gastos Art. 37 Renta) en CADA línea. Solo factura (la deducción Art. 37 es factura-only).
        # l10n_pe_ne_tipo_doc recién se congela al emitir (_l10n_pe_apply_emission_response /
        # _l10n_pe_apply_signed): en la primera emisión, mientras se arma este payload, todavía
        # está vacío. Usar `or "01"` aquí lo hacía SIEMPRE factura y filtraba la placa también
        # en boletas. El idioma correcto (igual que en el resto del archivo) es
        # `l10n_pe_ne_tipo_doc or _l10n_pe_document_type()`.
        # Placa POR LÍNEA: cada línea de combustible con su propia placa (varios vehículos por
        # factura); las líneas sin placa (no combustible) no la llevan. Fallback de compatibilidad:
        # si NINGUNA línea trae placa pero el move tiene una de cabecera (flujo viejo/compras), se
        # replica en todas —conserva el comportamiento anterior.
        if (self.l10n_pe_ne_tipo_doc or self._l10n_pe_document_type()) == "01":
            plines = list(self._l10n_pe_product_lines())
            hay_por_linea = any((l.l10n_pe_ne_placa or "").strip() for l in plines)
            fallback = (self.l10n_pe_ne_placa or "").strip() if not hay_por_linea else ""
            for li, line in enumerate(plines, start=1):
                placa = (line.l10n_pe_ne_placa or "").strip() or fallback
                if not placa:
                    continue
                out.append({
                    "idLinea": str(li),
                    "codTipoVariable": "-",
                    "nomPropiedad": "Numero de Placa",
                    "codPropiedad": "7000",
                    "valPropiedad": placa,
                    "codBienPropiedad": "-",
                    "fecInicioPropiedad": "-",
                    "horInicioPropiedad": "-",
                    "fecFinPropiedad": "-",
                    "numDiasPropiedad": "-",
                })
        return out

    def _l10n_pe_build_note_request(self):
        """Nota de Crédito (07) / Débito (08) — referencia al documento afectado."""
        self.ensure_one()
        self._l10n_pe_check_lineas_impuesto()
        dt = self._l10n_pe_document_type()
        origin = self.reversed_entry_id if dt == "07" else self.debit_origin_id
        cabecera = self._l10n_pe_cabecera()
        if origin:
            o_serie, o_corr = origin._l10n_pe_serie_correlativo()
            cabecera["numDocAfectado"] = "%s-%s" % (o_serie, o_corr.zfill(8))
            cabecera["tipDocAfectado"] = origin._l10n_pe_document_type()
        else:
            cabecera["numDocAfectado"] = ""
            cabecera["tipDocAfectado"] = "01"
        cabecera["codMotivo"] = self.l10n_pe_motivo_code or (
            "01" if dt == "07" else "02"
        )
        if dt == "08":
            # Sustento libre si el usuario lo escribió; si no, descripción del catálogo.
            cabecera["desMotivo"] = (self.l10n_pe_motivo_desc or "").strip() or ND_MOTIVO_DESC.get(
                cabecera["codMotivo"], "Aumento en el valor"
            )
        req = {
            "id": self._l10n_pe_id_block(with_document_type=False),
            "emisor": self._l10n_pe_emisor(),
            "cabecera": cabecera,
            "tributos": self._l10n_pe_tributos(),
            "detalle": self._l10n_pe_detalle(),
            "leyendas": self._l10n_pe_leyendas(),
        }
        # Nota de Crédito (07): el CreditNoteMapper del biller exige forma de pago y
        # fuerza el <cbc:Amount> de PaymentTerms. "Contado" rebota (errorCode 2071/3246)
        # y omitirlo rebota (3245). El único patrón que valida en el SFS es "Credito"
        # con una cuota = total (campos válidos del contrato SFS, no se toca el biller).
        # La ND (08) valida sin datoPago, así que no se le agrega.
        # EXCEPCIÓN: una NC de importe 0 (motivo 03, corrección de descripción) NO puede
        # llevar el Amount de la cuota Crédito (SUNAT rechaza cac:PaymentTerms/cbc:Amount
        # "0.00"), y omitir la FormaPago rebota con errorCode 3245. El patrón válido es
        # "Contado" SIN <cbc:Amount>. El mapper del biller (GenericBillingMapper) defaultea
        # el monto a "0.00" y la moneda a "" salvo que se le mande el sentinel "-", que le
        # dice que NO setee esos campos → el FTL entonces omite el <cbc:Amount>.
        if dt == "07":
            if self.amount_total:
                total = self._l10n_pe_fmt(self.amount_total)
                fecha = self.invoice_date.strftime("%Y-%m-%d") if self.invoice_date else ""
                moneda = self.currency_id.name or "PEN"
                req["datoPago"] = {
                    "formaPago": "Credito",
                    "mtoNetoPendientePago": total,
                    "tipMonedaMtoNetoPendientePago": moneda,
                }
                req["detallePago"] = [
                    {
                        "mtoCuotaPago": total,
                        "fecCuotaPago": fecha,
                        "tipMonedaCuotaPago": moneda,
                    }
                ]
            else:
                # NC de importe 0 (motivo 03): SUNAT exige FormaPago=Credito con Amount>0
                # (Contado→3246, omitir→3245, Amount 0.00→2071). Se referencia el total del
                # comprobante afectado como monto de la cuota (el documento en sí va en 0).
                ref = self._l10n_pe_fmt((origin.amount_total if origin else 0) or 0)
                fecha = self.invoice_date.strftime("%Y-%m-%d") if self.invoice_date else ""
                moneda = self.currency_id.name or "PEN"
                req["datoPago"] = {
                    "formaPago": "Credito",
                    "mtoNetoPendientePago": ref,
                    "tipMonedaMtoNetoPendientePago": moneda,
                }
                req["detallePago"] = [
                    {"mtoCuotaPago": ref, "fecCuotaPago": fecha, "tipMonedaCuotaPago": moneda}
                ]
        return req

    def _l10n_pe_target(self):
        """(endpoint, payload) según el tipo de comprobante."""
        self._l10n_pe_check_serie()
        dt = self._l10n_pe_document_type()
        if dt == "07":
            return ("notaCredito", self._l10n_pe_build_note_request())
        if dt == "08":
            return ("notaDebito", self._l10n_pe_build_note_request())
        return ("factura", self._l10n_pe_build_invoice_request())

    def _l10n_pe_store_cdr(self, cdr_b64):
        """Guarda el CDR de SUNAT (zip en base64, del header X-Sunat-Cdr) como adjunto en
        l10n_pe_biller_cdr y devuelve (responseCode, description) del ApplicationResponse."""
        self.ensure_one()
        try:
            cdr_bytes = base64.b64decode(cdr_b64)
        except Exception:
            return "", ""
        serie, correlativo = self._l10n_pe_serie_correlativo()
        name = "R%s-%s-%s.zip" % (
            self.company_id.vat or "",
            serie,
            correlativo.zfill(8),
        )
        att = self.env["ir.attachment"].create(
            {
                "name": name,
                "res_model": "account.move",
                "res_id": self.id,
                "mimetype": "application/zip",
                "raw": cdr_bytes,
            }
        )
        self.l10n_pe_biller_cdr = att.id
        return self._l10n_pe_parse_cdr_codes(cdr_bytes)

    def _l10n_pe_parse_cdr_codes(self, cdr_bytes):
        """(responseCode, description) del ApplicationResponse dentro del zip CDR."""
        code = desc = ""
        try:
            with zipfile.ZipFile(io.BytesIO(cdr_bytes)) as zf:
                xml_name = next(
                    (n for n in zf.namelist() if n.lower().endswith(".xml")), None
                )
                content = zf.read(xml_name) if xml_name else b""
            m = re.search(rb"<cbc:ResponseCode>([^<]*)</cbc:ResponseCode>", content)
            code = m.group(1).decode() if m else ""
            m = re.search(rb"<cbc:Description>([^<]*)</cbc:Description>", content)
            desc = m.group(1).decode("utf-8", "replace") if m else ""
        except Exception:
            pass
        return code, desc

    def _l10n_pe_apply_emission_response(self, ok, body_text, cdr_b64):
        """Aplica al move el resultado de una emisión — mismo tratamiento para el
        flujo síncrono (respuesta HTTP directa) y el asíncrono (cron que recoge
        XML/CDR desde S3 vía el worker): adjunta el XML firmado, guarda el CDR,
        congela la identidad emitida y fija estado + mensaje."""
        self.ensure_one()
        signed = ok and any(
            tag in (body_text or "")
            for tag in ("<Invoice", "<CreditNote", "<DebitNote")
        )
        if not signed:
            self.l10n_pe_biller_state = "rechazado"
            self.l10n_pe_biller_message = (body_text or "")[:2000]
            return
        serie, correlativo = self._l10n_pe_serie_correlativo()
        # Si el XML ya se adjuntó estando en_proceso (firma del modo instant o
        # item "firmado" del worker async), reemplazarlo: sin esto quedaban DOS
        # adjuntos idénticos colgados del move (el viejo huérfano en el panel).
        if self.l10n_pe_biller_xml:
            # Contenido distinto = re-emisión con XML corregido: los PDFs
            # cacheados renderizan el XML viejo y quedarían servidos por
            # siempre (el cache pdfver no detecta cambios de contenido).
            if (self.l10n_pe_biller_xml.raw or b"") != body_text.encode("utf-8"):
                self._l10n_pe_invalidar_pdfs()
            self.l10n_pe_biller_xml.unlink()
        att = self.env["ir.attachment"].create(
            {
                "name": "%s-%s-%s.xml"
                % (self.company_id.vat, serie, correlativo.zfill(8)),
                "res_model": "account.move",
                "res_id": self.id,
                "mimetype": "application/xml",
                "raw": body_text.encode("utf-8"),
            }
        )
        self.l10n_pe_biller_xml = att.id
        self.l10n_pe_biller_state = "enviado"
        # Congela la identidad emitida para una eventual baja (no recomputar luego del partner/nombre).
        self.l10n_pe_ne_tipo_doc = self._l10n_pe_document_type()
        self.l10n_pe_ne_serie_emit = serie
        self.l10n_pe_ne_corr_emit = correlativo.zfill(8)
        code, desc = self._l10n_pe_store_cdr(cdr_b64) if cdr_b64 else ("", "")
        if code == "0":
            self.l10n_pe_biller_message = _(
                "Aceptado por SUNAT — CDR ResponseCode 0. %s"
            ) % (desc or "")
            # Automatización (opt-in): al aceptarse, enviar el comprobante (XML + PDF + CDR) al
            # correo del cliente. Gateado por config para no mandar correos sin querer; nunca
            # rompe la emisión (un fallo de correo se loguea y sigue).
            if self.env["ir.config_parameter"].sudo().get_param(
                "l10n_pe_ne_biller.email_on_accept", ""
            ).strip().lower() in ("1", "true"):
                try:
                    self._l10n_pe_ne_email_comprobante()
                except Exception as e:  # noqa: BLE001
                    _logger.warning("email comprobante %s: %s", self.name, e)
        elif code:
            self.l10n_pe_biller_message = _(
                "CDR de SUNAT (ResponseCode %s). %s"
            ) % (code, desc or "")
        else:
            self.l10n_pe_biller_message = _("Aceptado por el facturador (HTTP 200).")

    def _l10n_pe_ne_email_comprobante(self):
        """Envía el comprobante aceptado (XML firmado + PDF A4 + CDR) al correo del cliente.
        Automatiza la entrega manual. No-op si el cliente no tiene correo; nunca lanza (el
        llamador lo envuelve, pero igual usamos send sin excepción)."""
        self.ensure_one()
        email = (self.partner_id.email or "").strip()
        if not email:
            _logger.info("email comprobante %s: cliente sin correo, se omite", self.name)
            return False
        atts = self.env["ir.attachment"]
        if self.l10n_pe_biller_xml:
            atts |= self.l10n_pe_biller_xml
        try:
            pdf = self._l10n_pe_get_pdf_attachment(formato="A4")
            if pdf:
                atts |= pdf
        except Exception:  # noqa: BLE001 — el PDF es deseable pero no bloquea el correo
            pass
        if self.l10n_pe_biller_cdr:
            atts |= self.l10n_pe_biller_cdr
        serie, corr = self._l10n_pe_serie_correlativo()
        num = "%s-%s" % (serie, corr)
        subject = _("Comprobante electrónico %s") % num
        body = _(
            "<p>Estimado cliente,</p>"
            "<p>Adjuntamos su comprobante electrónico <b>%(num)s</b> emitido por "
            "<b>%(emisor)s</b> y aceptado por SUNAT.</p>"
            "<p>Se incluyen el XML firmado, la representación impresa (PDF) y el CDR.</p>"
        ) % {"num": num, "emisor": self.company_id.name or ""}
        mail = self.env["mail.mail"].sudo().create({
            "subject": subject,
            "body_html": body,
            "email_to": email,
            "email_from": self.company_id.email or self.env.user.email_formatted,
            "attachment_ids": [(6, 0, atts.ids)],
            "auto_delete": False,
        })
        mail.send(raise_exception=False)
        _logger.info("email comprobante %s enviado a %s (%d adjuntos)", num, email, len(atts))
        return True

    def _l10n_pe_apply_signed(self, firma):
        """Modo instantáneo: aplica el resultado de la FIRMA (sin enviar a SUNAT). Adjunta el
        XML firmado (con eso el ticket/PDF ya funcionan), congela la identidad, guarda el ZIP
        de ENVI + filename/canal para el envío en 2º plano y deja el estado en 'en_proceso'."""
        self.ensure_one()
        firma = firma or {}
        xml = firma.get("xmlFirmado") or ""
        if not any(tag in xml for tag in ("<Invoice", "<CreditNote", "<DebitNote")):
            self.l10n_pe_biller_state = "error"
            self.l10n_pe_biller_message = _("La firma no devolvió un XML válido.")
            return False
        serie, correlativo = self._l10n_pe_serie_correlativo()
        # Re-firma (re-emisión tras rechazo/error en modo instant): reemplaza el
        # XML anterior (evita el adjunto huérfano) e invalida los PDFs cacheados
        # del intento previo antes de pre-generar los nuevos.
        if self.l10n_pe_biller_xml:
            if (self.l10n_pe_biller_xml.raw or b"") != xml.encode("utf-8"):
                self._l10n_pe_invalidar_pdfs()
            self.l10n_pe_biller_xml.unlink()
        att = self.env["ir.attachment"].create(
            {
                "name": "%s-%s-%s.xml" % (self.company_id.vat, serie, correlativo.zfill(8)),
                "res_model": "account.move",
                "res_id": self.id,
                "mimetype": "application/xml",
                "raw": xml.encode("utf-8"),
            }
        )
        self.l10n_pe_biller_xml = att.id
        self.l10n_pe_ne_tipo_doc = self._l10n_pe_document_type()
        self.l10n_pe_ne_serie_emit = serie
        self.l10n_pe_ne_corr_emit = correlativo.zfill(8)
        self.l10n_pe_ne_envi_zip = firma.get("enviZip") or ""
        self.l10n_pe_ne_biller_filename = firma.get("filename") or ""
        self.l10n_pe_ne_biller_canal = firma.get("canal") or "GEM"
        self.l10n_pe_ne_envio_intentos = 0
        self.l10n_pe_biller_state = "en_proceso"
        self.l10n_pe_biller_message = _("Firmado — ticket listo. Pendiente de envío a SUNAT.")
        # Pre-generar la representación impresa YA (con el XML firmado) para que la
        # descarga sea instantánea: así el adjunto existe cuando el usuario da clic y
        # no depende de un cold-start del micro en ese momento (que llegaba a expirar y
        # dejaba la sensación de "no se puede descargar mientras procesa"). No es fatal:
        # si el micro falla aquí, queda como fallback la generación on-demand.
        try:
            self._l10n_pe_get_pdf_attachment()  # A4
            if self.l10n_pe_ne_tipo_doc in ("01", "03"):
                self._l10n_pe_get_pdf_attachment(formato="TICKET")  # 80mm
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "No se pudo pre-generar el PDF tras firmar %s: %s",
                self.name or self.id, exc,
            )
        return True

    @api.model
    def _l10n_pe_cron_enviar_pendientes(self):
        """Modo instantáneo: envía a SUNAT (2º plano) los comprobantes ya FIRMADOS que quedaron
        en 'en_proceso' con ZIP pendiente. Al recibir el CDR pasa a aceptado/rechazado y limpia
        el ZIP. Reintentable: un fallo de red deja el move en en_proceso para la próxima corrida
        (con tope de intentos para no reintentar por siempre un rechazo)."""
        icp = self.env["ir.config_parameter"].sudo()
        if icp.get_param("l10n_pe_ne_biller.instant_enabled", "").strip().lower() not in ("1", "true"):
            return
        base = icp.get_param("l10n_pe_ne_biller.url", "http://localhost:8090").rstrip("/")
        timeout = int(icp.get_param("l10n_pe_ne_biller.timeout", "240"))
        max_intentos = int(icp.get_param("l10n_pe_ne_biller.max_intentos_envio", "30"))
        domain = [("l10n_pe_biller_state", "=", "en_proceso"), ("l10n_pe_ne_envi_zip", "!=", False)]
        # Si las boletas van por Resumen Diario, se excluyen del envío individual (las manda el RC).
        if icp.get_param("l10n_pe_ne_biller.boletas_resumen", "").strip().lower() in ("1", "true"):
            domain.append(("l10n_pe_ne_tipo_doc", "!=", "03"))
        pend = self.search(domain, limit=50)
        for move in pend:
            headers = {"X-Api-Key": move.company_id.sudo().l10n_pe_ne_api_key or ""}
            signed_xml = (move.l10n_pe_biller_xml.raw or b"").decode("utf-8") if move.l10n_pe_biller_xml else ""
            body = {
                "ruc": move.company_id.vat or "",
                "filename": move.l10n_pe_ne_biller_filename or "",
                "canal": move.l10n_pe_ne_biller_canal or "GEM",
                "enviZip": move.l10n_pe_ne_envi_zip or "",
                "signedXml": signed_xml,
            }
            ok = False
            try:
                resp = requests.post(base + "/generator/enviar", json=body, headers=headers, timeout=(5, timeout))
                if resp.status_code == 200:
                    data = resp.json() or {}
                    if data.get("rechazado"):
                        # SUNAT rechazó (regla de negocio) → estado final, NO reintentar.
                        move.l10n_pe_biller_state = "rechazado"
                        move.l10n_pe_biller_message = (_("Rechazado por SUNAT: %s") % (data.get("motivo") or ""))[:2000]
                        move.l10n_pe_ne_envi_zip = False
                    else:
                        move._l10n_pe_apply_emission_response(True, signed_xml, data.get("cdr") or "")
                        move.l10n_pe_ne_envi_zip = False  # enviado; nada pendiente
                    ok = True
                else:
                    move.l10n_pe_biller_message = ("Envío HTTP %s: %s" % (resp.status_code, resp.text))[:2000]
            except Exception as e:  # noqa: BLE001 — red/SUNAT: reintentar
                _logger.warning("enviar pendiente %s: %s (reintenta)", move.name, e)
                move.l10n_pe_biller_message = ("Reintentando envío: %s" % e)[:2000]
            if not ok:
                move.l10n_pe_ne_envio_intentos = (move.l10n_pe_ne_envio_intentos or 0) + 1
                if move.l10n_pe_ne_envio_intentos >= max_intentos:
                    move.l10n_pe_biller_state = "error"
            self.env["bus.bus"]._sendone(
                "l10n_pe_biller_updates",
                "l10n_pe_biller_update",
                {"move_id": move.id, "state": move.l10n_pe_biller_state},
            )
            self.env.cr.commit()


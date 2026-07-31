# -*- coding: utf-8 -*-
"""account.move — Comunicación de baja (RA) + Resumen Diario de Boletas (RC).
Extraído de account_move_biller.py (refactor sin cambio de comportamiento)."""
import base64
import re
import requests
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    # ---------------------------------------------- comunicación de baja (RA)
    @api.depends(
        "l10n_pe_ne_baja_fecha", "l10n_pe_ne_baja_correlativo", "l10n_pe_ne_tipo_doc"
    )
    def _compute_l10n_pe_ne_baja_doc(self):
        for m in self:
            if m.l10n_pe_ne_baja_fecha and m.l10n_pe_ne_baja_correlativo:
                # Boletas (03) se anulan por Resumen Diario (RC); el resto por Comunicación de Baja (RA).
                prefijo = "RC" if m.l10n_pe_ne_tipo_doc == "03" else "RA"
                m.l10n_pe_ne_baja_doc = "%s-%s-%s" % (
                    prefijo,
                    m.l10n_pe_ne_baja_fecha.strftime("%Y%m%d"),
                    m.l10n_pe_ne_baja_correlativo,
                )
            else:
                m.l10n_pe_ne_baja_doc = False

    def _l10n_pe_baja_identidad(self):
        """(tipo, serie, correlativo) realmente emitidos. Usa lo congelado al enviar; si falta (comprobante
        enviado por una versión previa), recae en el cálculo del momento."""
        self.ensure_one()
        tipo = self.l10n_pe_ne_tipo_doc or self._l10n_pe_document_type()
        serie = self.l10n_pe_ne_serie_emit or self._l10n_pe_serie_correlativo()[0]
        correlativo = self.l10n_pe_ne_corr_emit or self._l10n_pe_serie_correlativo()[1]
        return tipo, serie, correlativo

    def _l10n_pe_check_baja(self):
        """Guardas de la comunicación de baja: solo factura/NC/ND ya enviadas, con serie válida, motivo,
        fecha y (para facturas) dentro del plazo de 7 días."""
        self.ensure_one()
        if self.l10n_pe_biller_state not in ("enviado", "anulado"):
            raise UserError(
                _(
                    "Solo puede comunicarse la baja de un comprobante ya enviado a SUNAT."
                )
            )
        tipo, serie, _corr = self._l10n_pe_baja_identidad()
        if tipo not in ("01", "03", "07", "08"):
            raise UserError(
                _(
                    "La anulación aplica a factura, boleta, nota de crédito y nota de débito."
                )
            )
        # Una factura/boleta con NC VIGENTES no se da de baja: la baja anula el documento
        # COMPLETO y las notas ya acreditaron parte (crédito duplicado), además de dejar
        # esas NC referenciando un comprobante dado de baja. Primero se anulan las NC,
        # o se acredita el saldo con otra NC en lugar de la baja.
        if tipo in ("01", "03"):
            ncs = self._l10n_pe_ne_nc_previas()
            if ncs:
                raise UserError(
                    _(
                        "No se puede anular %(doc)s: tiene %(n)d nota(s) de crédito "
                        "vigente(s) por %(monto)s (%(lista)s). Anularla duplicaría el "
                        "crédito — anule primero esas notas, o acredite el saldo con "
                        "una nota de crédito en lugar de la baja."
                    )
                    % {
                        "doc": "%s-%s" % (serie or "", (_corr or "").zfill(8)),
                        "n": len(ncs),
                        "monto": "%.2f" % sum(ncs.mapped("amount_total")),
                        "lista": ", ".join(
                            "%s-%s" % m._l10n_pe_ne_doc_id() for m in ncs
                        ),
                    }
                )
        # Serie con prefijo B (boleta) / F / S, o numérica: refleja el formato del comprobante emitido.
        if not re.match(r"^([BFS][A-Z0-9]{3}|\d{1,4})$", serie or ""):
            raise UserError(
                _(
                    "La serie del comprobante (%s) no tiene un formato válido para la anulación."
                )
                % serie
            )
        if not (self.l10n_pe_ne_baja_motivo or "").strip():
            raise UserError(_("Indique el motivo de la baja."))
        if not self.invoice_date:
            raise UserError(_("El comprobante no tiene fecha de emisión."))
        # Plazo de 7 días calendario para anular una FACTURA por baja (las NC/ND no tienen este límite).
        if tipo == "01":
            limite = int(
                self.env["ir.config_parameter"]
                .sudo()
                .get_param("l10n_pe_ne_biller.baja_plazo_dias", "7")
            )
            dias = (fields.Date.context_today(self) - self.invoice_date).days
            if dias > limite:
                raise UserError(
                    _(
                        "Fuera del plazo de baja: han pasado %s días desde la emisión de la factura "
                        "(máximo %s días calendario). Para anularla, emita una nota de crédito."
                    )
                    % (dias, limite)
                )
        # Boleta mayor a S/ 700 exige el documento de identidad del adquirente (igual que en su emisión).
        if (
            tipo == "03"
            and (self.amount_total or 0.0) > 700
            and not (self.partner_id.vat or "").strip()
        ):
            raise UserError(
                _(
                    "Una boleta mayor a S/ 700 requiere el documento de identidad del cliente para anularse "
                    "por resumen diario."
                )
            )

    def _l10n_pe_build_baja_request(self):
        """Comunicación de Baja (RA) — endpoint /generator/resumenBaja. Da de baja este comprobante."""
        self.ensure_one()
        tipo, serie, correlativo = self._l10n_pe_baja_identidad()
        return {
            "id": {
                "ruc": self.company_id.vat or "",
                # El DTO del facturador deserializa estas fechas con patrón yyyyMMdd (sin guiones).
                "fechaGeneracion": self.l10n_pe_ne_baja_fecha.strftime("%Y%m%d"),
                "correlativo": self.l10n_pe_ne_baja_correlativo or "1",
            },
            "emisor": self._l10n_pe_emisor(),
            # ReferenceDate = fecha de emisión del comprobante que se anula; IssueDate = fecha de la baja.
            "fecGeneracion": self.invoice_date.strftime("%Y%m%d"),
            "fecComunicacion": self.l10n_pe_ne_baja_fecha.strftime("%Y%m%d"),
            "resumenBajas": [
                {
                    "tipDocBaja": tipo,
                    "numDocBaja": "%s-%s" % (serie, correlativo.zfill(8)),
                    "desMotivoBaja": self.l10n_pe_ne_baja_motivo or "",
                }
            ],
        }

    # --- Resumen Diario de Boletas (RC): anula boletas (tipEstado 3) ---
    _RC_CATEGORIA = {
        "1000": "gravado",
        "1016": "gravado",
        "9997": "exonerado",
        "9998": "inafecto",
        "9995": "exportado",
        "9996": "gratuito",
    }

    def _l10n_pe_rc_totales(self):
        """Totales de valor por categoría (gravado/exonerado/inafecto/exportado/gratuito) para el RC."""
        self.ensure_one()
        cats = dict.fromkeys(
            ("gravado", "exonerado", "inafecto", "exportado", "gratuito"), 0.0
        )
        for line in self._l10n_pe_product_lines():
            (_tip, cod_tri, *_), _por = self._l10n_pe_tax_info(line)
            base, _igv, _isc, _icb = self._l10n_pe_line_amounts(line)
            cats[self._RC_CATEGORIA.get(cod_tri, "gravado")] += base
        return {k: round(v, 2) for k, v in cats.items()}

    def _l10n_pe_build_rc_request(self):
        """Resumen Diario de Boletas (RC) — endpoint /generator/resumenBoleta. Anula esta boleta (tipEstado 3)."""
        self.ensure_one()
        fmt = self._l10n_pe_fmt
        _tipo, serie, correlativo = self._l10n_pe_baja_identidad()
        partner = self.partner_id
        cats = self._l10n_pe_rc_totales()
        tributos = [
            {
                "idLineaRd": "1",
                "ideTributoRd": t["ideTributo"],
                "nomTributoRd": t["nomTributo"],
                "codTipTributoRd": t["codTipTributo"],
                "mtoBaseImponibleRd": t["mtoBaseImponible"],
                "mtoTributoRd": t["mtoTributo"],
            }
            for t in self._l10n_pe_tributos()
        ]
        # ICBPER (7152): NO se agrega aquí. _l10n_pe_tributos() ya lo incluye (regla 3279) y entra al
        # RC por el comprehension de arriba. Duplicarlo generaba un segundo cac:TaxTotal con el mismo
        # código de tributo → SUNAT rechazaba el RC (obs 2355: un solo TaxTotal por tributo/ítem). La
        # suma de componentes con totImpCpe (obs 4027) sigue cuadrando: el ICBPER está presente una vez.
        # El validador exige que CADA línea del RC tenga el tributo IGV '1000'. Si la boleta no es
        # gravada (exo/inafecto/exportación/gratuita/IVAP), se agrega uno en cero (regla 2278).
        if not any(t["ideTributoRd"] == "1000" for t in tributos):
            tributos.append(
                {
                    "idLineaRd": "1",
                    "ideTributoRd": "1000",
                    "nomTributoRd": "IGV",
                    "codTipTributoRd": "VAT",
                    "mtoBaseImponibleRd": "0.00",
                    "mtoTributoRd": "0.00",
                }
            )
        # Adquirente: consumidor final sin documento → tipo "0" y número "00000000" (catálogo SUNAT).
        vat = (partner.vat or "").strip()
        cod_doc = partner.l10n_latam_identification_type_id.l10n_pe_vat_code or ""
        if not vat:
            cod_doc, vat = "0", "00000000"
        elif not cod_doc:
            cod_doc = "6" if (len(vat) == 11 and vat.isdigit()) else "1"
        return {
            "id": {
                "ruc": self.company_id.vat or "",
                "fechaGeneracion": self.l10n_pe_ne_baja_fecha.strftime("%Y%m%d"),
                "correlativo": self.l10n_pe_ne_baja_correlativo or "1",
            },
            "emisor": self._l10n_pe_emisor(),
            "resumenDiario": [
                {
                    # ReferenceDate = emisión de la boleta; IssueDate = fecha del resumen (ISO en el XML).
                    "fecEmision": self.invoice_date.strftime("%Y-%m-%d"),
                    "fecResumen": self.l10n_pe_ne_baja_fecha.strftime("%Y-%m-%d"),
                    "tipDocResumen": "03",
                    "idDocResumen": "%s-%s" % (serie, correlativo.zfill(8)),
                    "tipDocUsuario": cod_doc,
                    "numDocUsuario": vat,
                    "tipMoneda": self.currency_id.name or "PEN",
                    "totValGrabado": fmt(cats["gravado"]),
                    "totValExoneado": fmt(cats["exonerado"]),
                    "totValInafecto": fmt(cats["inafecto"]),
                    "totValExportado": fmt(cats["exportado"]),
                    "totValGratuito": fmt(cats["gratuito"]),
                    "totOtroCargo": "0.00",
                    "totImpCpe": fmt(self.amount_total),
                    "tipDocModifico": "",
                    "serDocModifico": "",
                    "numDocModifico": "",
                    "tipRegPercepcion": "",
                    "porPercepcion": "",
                    "monBasePercepcion": "",
                    "monPercepcion": "",
                    "monTotIncPercepcion": "",
                    "tipEstado": "3",  # 3 = anulación/baja de la boleta
                    "tributosDocResumen": tributos,
                }
            ],
        }

    def _l10n_pe_rc_emision_item(self, id_linea):
        """Un ítem del Resumen Diario para EMISIÓN (tipEstado 1 = registrar la boleta ante SUNAT).
        Misma estructura que el de anulación pero con la identidad EMITIDA y estado 1."""
        self.ensure_one()
        fmt = self._l10n_pe_fmt
        serie = self.l10n_pe_ne_serie_emit or self._l10n_pe_serie_correlativo()[0]
        correlativo = self.l10n_pe_ne_corr_emit or self._l10n_pe_serie_correlativo()[1]
        partner = self.partner_id
        cats = self._l10n_pe_rc_totales()
        idl = str(id_linea)
        tributos = [
            {
                "idLineaRd": idl, "ideTributoRd": t["ideTributo"], "nomTributoRd": t["nomTributo"],
                "codTipTributoRd": t["codTipTributo"], "mtoBaseImponibleRd": t["mtoBaseImponible"],
                "mtoTributoRd": t["mtoTributo"],
            }
            for t in self._l10n_pe_tributos()
        ]
        # ICBPER (7152): ya viene de _l10n_pe_tributos() por el comprehension; no re-agregarlo o SUNAT
        # rechaza el RC con un TaxTotal duplicado (obs 2355).
        if not any(t["ideTributoRd"] == "1000" for t in tributos):
            tributos.append({"idLineaRd": idl, "ideTributoRd": "1000", "nomTributoRd": "IGV",
                             "codTipTributoRd": "VAT", "mtoBaseImponibleRd": "0.00", "mtoTributoRd": "0.00"})
        vat = (partner.vat or "").strip()
        cod_doc = partner.l10n_latam_identification_type_id.l10n_pe_vat_code or ""
        if not vat:
            cod_doc, vat = "0", "00000000"
        elif not cod_doc:
            cod_doc = "6" if (len(vat) == 11 and vat.isdigit()) else "1"
        return {
            "fecEmision": self.invoice_date.strftime("%Y-%m-%d"),
            "fecResumen": fields.Date.context_today(self).strftime("%Y-%m-%d"),
            "tipDocResumen": "03",
            "idDocResumen": "%s-%s" % (serie, (correlativo or "").zfill(8)),
            "tipDocUsuario": cod_doc, "numDocUsuario": vat,
            "tipMoneda": self.currency_id.name or "PEN",
            "totValGrabado": fmt(cats["gravado"]), "totValExoneado": fmt(cats["exonerado"]),
            "totValInafecto": fmt(cats["inafecto"]), "totValExportado": fmt(cats["exportado"]),
            "totValGratuito": fmt(cats["gratuito"]), "totOtroCargo": "0.00",
            "totImpCpe": fmt(self.amount_total),
            "tipDocModifico": "", "serDocModifico": "", "numDocModifico": "",
            "tipRegPercepcion": "", "porPercepcion": "", "monBasePercepcion": "",
            "monPercepcion": "", "monTotIncPercepcion": "",
            "tipEstado": "1",  # 1 = adicionar/registrar la boleta
            "tributosDocResumen": tributos,
        }

    def _l10n_pe_build_rc_emision(self, fecha_gen, correlativo):
        """RC de EMISIÓN para un CONJUNTO de boletas (self = recordset; misma compañía y fecha)."""
        first = self[0]
        return {
            "id": {
                "ruc": first.company_id.vat or "",
                "fechaGeneracion": fecha_gen.strftime("%Y%m%d"),
                "correlativo": str(correlativo),
            },
            "emisor": first._l10n_pe_emisor(),
            "resumenDiario": [b._l10n_pe_rc_emision_item(i + 1) for i, b in enumerate(self)],
        }

    @api.model
    def _l10n_pe_cron_resumen_boletas(self):
        """Boletas por Resumen Diario (RC, tipEstado 1) IDEMPOTENTE, en dos fases:
        A) ENVIAR: agrupa las boletas firmadas SIN ticket (por compañía+fecha), manda el RC vía
           /resumenBoleta/enviar (firma + sendSummary) y GUARDA el ticket en cada boleta. No las
           re-envía en la próxima corrida (ya tienen ticket) → no duplica el resumen en SUNAT.
        B) CONSULTAR: pollea los grupos que ya tienen ticket vía /ticket/estado; al llegar el CDR
           marca las boletas aceptado/rechazado y libera el ticket. Requiere instant + boletas_resumen."""
        icp = self.env["ir.config_parameter"].sudo()
        if icp.get_param("l10n_pe_ne_biller.boletas_resumen", "").strip().lower() not in ("1", "true"):
            return
        base = icp.get_param("l10n_pe_ne_biller.url", "http://localhost:8090").rstrip("/")
        timeout = int(icp.get_param("l10n_pe_ne_biller.resumen_timeout", "80"))
        STATUS_EN_PROCESO = 98

        def _bus(b):
            self.env["bus.bus"]._sendone(
                "l10n_pe_biller_updates", "l10n_pe_biller_update",
                {"move_id": b.id, "state": b.l10n_pe_biller_state})

        # ── FASE B — consultar los grupos que YA tienen ticket (idempotente: no re-envía) ──
        con_ticket = self.search(
            [("l10n_pe_biller_state", "=", "en_proceso"), ("l10n_pe_ne_rc_ticket", "!=", False)],
            limit=200,
        )
        por_ticket = {}
        for m in con_ticket:
            por_ticket.setdefault(m.l10n_pe_ne_rc_ticket, self.browse())
            por_ticket[m.l10n_pe_ne_rc_ticket] |= m
        for ticket, boletas in por_ticket.items():
            company = boletas[0].company_id
            headers = {"X-Api-Key": company.sudo().l10n_pe_ne_api_key or ""}
            body = {"ruc": company.vat or "", "ticket": ticket, "canal": "GEM"}
            try:
                resp = requests.post(base + "/generator/ticket/estado", json=body, headers=headers, timeout=(5, timeout))
            except Exception as e:  # noqa: BLE001 — red: reintenta con el MISMO ticket
                _logger.warning("ticket %s: %s (reintenta)", ticket, e)
                continue
            if resp.status_code != 200:
                continue  # transitorio: reintenta con el mismo ticket
            data = resp.json() or {}
            status = int(data.get("statusCode") or -1)
            cdr = data.get("cdr") or ""
            if status == STATUS_EN_PROCESO:
                continue  # SUNAT aún procesa el resumen: reintenta luego (mismo ticket)
            if cdr:
                code, desc = boletas[0]._l10n_pe_parse_cdr_codes(base64.b64decode(cdr))
                estado = "enviado" if code == "0" else "rechazado"
                for b in boletas:
                    b.l10n_pe_biller_state = estado
                    b.l10n_pe_biller_message = (
                        _("Aceptado por SUNAT vía Resumen Diario (RC corr %s). %s") % (b.l10n_pe_ne_rc_correlativo or "", desc or "")
                        if code == "0" else
                        _("Rechazado en el Resumen Diario (RC corr %s): ResponseCode %s. %s") % (b.l10n_pe_ne_rc_correlativo or "", code, desc or ""))[:2000]
                    b.l10n_pe_ne_rc_ticket = False
                    b.l10n_pe_ne_envi_zip = False
                    b._l10n_pe_store_cdr(cdr)
                    _bus(b)
            else:
                for b in boletas:
                    b.l10n_pe_biller_state = "rechazado"
                    b.l10n_pe_biller_message = _("Resumen Diario RC: SUNAT terminó con statusCode %s sin CDR.") % status
                    b.l10n_pe_ne_rc_ticket = False
                    _bus(b)
            self.env.cr.commit()

        # ── FASE A — enviar las boletas firmadas SIN ticket todavía (una llamada = un ticket) ──
        sin_ticket = self.search(
            [("l10n_pe_biller_state", "=", "en_proceso"), ("l10n_pe_ne_tipo_doc", "=", "03"),
             ("l10n_pe_ne_serie_emit", "!=", False), ("l10n_pe_ne_rc_ticket", "=", False)],
            limit=200,
        )
        grupos = {}
        for m in sin_ticket:
            grupos.setdefault((m.company_id.id, m.invoice_date), self.browse())
            grupos[(m.company_id.id, m.invoice_date)] |= m
        for (cid, fecha), boletas in grupos.items():
            company = boletas[0].company_id
            correlativo = self.env["ir.sequence"].next_by_code("l10n_pe.ne.rc") or "1"
            fecha_gen = fields.Date.context_today(boletas[0])
            payload = boletas._l10n_pe_build_rc_emision(fecha_gen, correlativo)
            headers = {"X-Api-Key": company.sudo().l10n_pe_ne_api_key or ""}
            try:
                resp = requests.post(base + "/generator/resumenBoleta/enviar", json=payload, headers=headers, timeout=(5, timeout))
            except Exception as e:  # noqa: BLE001 — no se envió: reintenta con un correlativo fresco
                _logger.warning("resumen boletas %s/%s: %s (reintenta)", cid, fecha, e)
                continue
            if resp.status_code == 200 and (resp.json() or {}).get("ticket"):
                ticket = resp.json()["ticket"]
                for b in boletas:
                    b.l10n_pe_ne_rc_ticket = ticket
                    b.l10n_pe_ne_rc_correlativo = str(correlativo)
                    b.l10n_pe_ne_rc_fecha = fecha_gen
                    b.l10n_pe_biller_message = _("Resumen Diario enviado (RC corr %s), ticket %s — esperando SUNAT.") % (correlativo, ticket)
            else:
                msg = ("Resumen RC HTTP %s: %s" % (resp.status_code, resp.text))[:1500]
                for b in boletas:
                    b.l10n_pe_biller_message = msg
            self.env.cr.commit()

    def _l10n_pe_store_baja_cdr(self, cdr_b64):
        """Guarda el CDR de la baja en un adjunto propio (no pisa el CDR original) y devuelve (code, desc)."""
        self.ensure_one()
        try:
            cdr_bytes = base64.b64decode(cdr_b64)
        except Exception:
            return "", ""
        att = self.env["ir.attachment"].create(
            {
                "name": "R%s-%s.zip"
                % (self.company_id.vat or "", self.l10n_pe_ne_baja_doc or "RA"),
                "res_model": "account.move",
                "res_id": self.id,
                "mimetype": "application/zip",
                "raw": cdr_bytes,
            }
        )
        self.l10n_pe_ne_baja_cdr = att.id
        return self._l10n_pe_parse_cdr_codes(cdr_bytes)

    def action_l10n_pe_send_baja(self):
        """Anula en SUNAT cada comprobante seleccionado: boletas por Resumen Diario (RC, tipEstado 3),
        facturas/NC/ND por Comunicación de Baja (RA)."""
        icp = self.env["ir.config_parameter"].sudo()
        base = icp.get_param("l10n_pe_ne_biller.url", "http://localhost:8090").rstrip(
            "/"
        )
        # En producción el biller está tras API Gateway (tope duro 30s): esperar
        # 120s solo alargaba el error. 40 = margen sobre el 504 del gateway; el
        # cron/reintento resuelve las bajas que SUNAT termina aceptando después.
        timeout = int(icp.get_param("l10n_pe_ne_biller.baja_timeout", "40"))
        for move in self:
            if move.l10n_pe_biller_state == "anulado":
                continue  # ya dado de baja: no reenviar el mismo RA (SUNAT lo rechaza por duplicado)
            move._l10n_pe_check_baja()
            es_boleta = move._l10n_pe_baja_identidad()[0] == "03"
            # Boletas → Resumen Diario (RC, tipEstado 3); el resto → Comunicación de Baja (RA).
            seq, endpoint, root = (
                ("l10n_pe.ne.rc", "/generator/resumenBoleta", "<SummaryDocuments")
                if es_boleta
                else ("l10n_pe.ne.ra", "/generator/resumenBaja", "<VoidedDocuments")
            )
            # Correlativo del resumen asignado una sola vez; un reintento limpio reusa el mismo.
            if not move.l10n_pe_ne_baja_correlativo:
                move.l10n_pe_ne_baja_correlativo = (
                    self.env["ir.sequence"].next_by_code(seq) or "1"
                )
                move.l10n_pe_ne_baja_fecha = fields.Date.context_today(move)
            ra = move.l10n_pe_ne_baja_doc
            payload = (
                move._l10n_pe_build_rc_request()
                if es_boleta
                else move._l10n_pe_build_baja_request()
            )
            headers = {"X-Api-Key": move.company_id.sudo().l10n_pe_ne_api_key or ""}
            try:
                resp = requests.post(
                    base + endpoint,
                    json=payload,
                    headers=headers,
                    timeout=(5, timeout),
                )
            except requests.RequestException as exc:
                # Nunca llegó a SUNAT: libera el resumen para reintentar con uno fresco.
                move._l10n_pe_baja_liberar()
                move.l10n_pe_biller_message = _(
                    "Error de conexión con el facturador (anulación %s): %s"
                ) % (ra, exc)
                continue
            if resp.status_code == 200 and root in resp.text:
                cdr_b64 = resp.headers.get("X-Sunat-Cdr")
                code, desc = (
                    move._l10n_pe_store_baja_cdr(cdr_b64) if cdr_b64 else ("", "")
                )
                if code == "0":
                    move.l10n_pe_biller_state = "anulado"
                    move.l10n_pe_biller_message = _(
                        "Anulación %s aceptada por SUNAT (ResponseCode 0). %s"
                    ) % (ra, desc or "")
                elif code:
                    # SUNAT lo recibió y rechazó: libera el resumen; el usuario corrige y reintenta con uno nuevo.
                    move._l10n_pe_baja_liberar()
                    move.l10n_pe_biller_message = _(
                        "Anulación %s rechazada por SUNAT (ResponseCode %s). %s"
                    ) % (ra, code, desc or "")
                else:
                    # 200 + XML firmado pero sin CDR legible: resultado indeterminado; no marcar anulado ni
                    # liberar el resumen (SUNAT pudo haberlo aceptado). El usuario verifica antes de reintentar.
                    move.l10n_pe_biller_message = (
                        _(
                            "Anulación %s enviada; respuesta de SUNAT indeterminada (sin CDR legible). Verifique en SUNAT."
                        )
                        % ra
                    )
            else:
                move._l10n_pe_baja_liberar()
                move.l10n_pe_biller_message = _(
                    "Anulación %s rechazada por el facturador: %s"
                ) % (ra, (resp.text or "")[:1200])
        return True

    def _l10n_pe_baja_liberar(self):
        """Libera el correlativo/fecha del resumen tras un fallo, para un reintento limpio."""
        self.l10n_pe_ne_baja_correlativo = False
        self.l10n_pe_ne_baja_fecha = False

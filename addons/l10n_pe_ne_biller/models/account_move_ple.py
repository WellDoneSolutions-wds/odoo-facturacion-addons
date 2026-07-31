# -*- coding: utf-8 -*-
"""account.move — Libros electrónicos PLE (14.1 ventas, 8.1 compras, 12.1 inventario).
Extraído de account_move_biller.py (refactor sin cambio de comportamiento)."""
import base64
import io
import logging
import re
import zipfile

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_round
from .account_move_biller import DEFAULT_UNIT_CODE, _percep_float

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    # ============================================================== PLE 14.1
    # Registro de Ventas e Ingresos Electrónico (PLE, formato 14.1). Estructura
    # oficial SUNAT (Anexo RS 286-2009 y modif.): campos 1-34, separador '|',
    # palote final, líneas CRLF, codificación ISO-8859-1. El archivo que el
    # CONTADOR sube al PLE de SUNAT, generado desde los comprobantes emitidos.

    @staticmethod
    def _l10n_pe_ne_ple_num(v):
        return "%.2f" % (v or 0.0)

    def _l10n_pe_ne_ple_breakdown(self):
        """Desglose por afectación para el PLE (cuadra con el XML emitido)."""
        self.ensure_one()
        gravado = exonerado = inafecto = exportacion = igv = icbper = 0.0
        for ln in self.invoice_line_ids:
            codes = ln.tax_ids.mapped("l10n_pe_edi_tax_code")
            base = ln.price_subtotal or 0.0
            if "9997" in codes:
                exonerado += base
            elif "9998" in codes:
                inafecto += base
            elif "9995" in codes:
                exportacion += base
            elif any(c in codes for c in ("1000", "1016")):
                gravado += base
        for tl in self.line_ids.filtered(lambda l: l.tax_line_id):
            code = tl.tax_line_id.l10n_pe_edi_tax_code or ""
            amt = abs(tl.amount_currency or 0.0)
            if code == "7152":
                icbper += amt
            elif code in ("1000", "1016"):
                igv += amt
        return {
            "gravado": gravado,
            "exonerado": exonerado,
            "inafecto": inafecto,
            "exportacion": exportacion,
            "igv": igv,
            "icbper": icbper,
            "total": self.amount_total or 0.0,
        }

    def _l10n_pe_ne_doc_id(self):
        """(serie, número) del comprobante para el PLE: prefiere el correlativo
        EMITIDO; si no, el folio (parte numérica final) del `name`, zfill 8. NO usa
        l10n_pe_correlativo (datos antiguos lo tienen con basura)."""
        self.ensure_one()
        serie = (
            self.l10n_pe_ne_serie_emit
            or self.l10n_pe_serie
            or self.journal_id.l10n_pe_ne_serie
            or "F001"
        )
        numero = (self.l10n_pe_ne_corr_emit or "").strip()
        if not numero:
            folios = re.findall(r"\d+", (self.name or "").replace(" ", ""))
            numero = (folios[-1] if folios else "1").zfill(8)
        return serie, numero

    def _l10n_pe_ne_ple_origen(self):
        """(fecha, tipo, serie, numero) del comprobante que se modifica (NC/ND)."""
        orig = self.reversed_entry_id or getattr(self, "debit_origin_id", False)
        if not orig:
            return "", "", "", ""
        fecha = orig.invoice_date.strftime("%d/%m/%Y") if orig.invoice_date else ""
        tipo = orig.l10n_pe_ne_tipo_doc or orig._l10n_pe_document_type()
        serie, num = orig._l10n_pe_ne_doc_id()
        return fecha, tipo, serie, num

    def _l10n_pe_ne_ple_linea(self, periodo8, cuo):
        """Una línea del PLE 14.1 (campos 1-34, '|' separador + palote final)."""
        self.ensure_one()
        num = self._l10n_pe_ne_ple_num
        b = self._l10n_pe_ne_ple_breakdown()
        tipo = self.l10n_pe_ne_tipo_doc or self._l10n_pe_document_type()
        serie, corr = self._l10n_pe_ne_doc_id()
        tdoc, ndoc = self._l10n_pe_cliente_doc()
        con_doc = bool(ndoc) and ndoc != "00000000"
        fecha = self.invoice_date.strftime("%d/%m/%Y") if self.invoice_date else ""
        estado = "2" if self.l10n_pe_biller_state == "anulado" else "1"
        of, ot, os_, on = self._l10n_pe_ne_ple_origen()
        moneda = self.currency_id.name or "PEN"
        campos = [
            periodo8,  # 1 Periodo (AAAAMM00)
            str(self.id),  # 2 CUO (único)
            "",  # 3 Nro correlativo (solo estado 8/9)
            fecha,  # 4 Fecha emisión
            "",  # 5 Fecha vencimiento
            tipo,  # 6 Tipo comprobante (tabla 10)
            serie,  # 7 Serie
            corr,  # 8 Número
            "",  # 9 Número final (consolidado)
            tdoc if con_doc else "",  # 10 Tipo doc cliente (tabla 2)
            ndoc if con_doc else "",  # 11 Nro doc cliente
            (self.partner_id.name or "").upper(),  # 12 Razón social
            num(b["exportacion"]),  # 13 Valor exportación
            num(b["gravado"]),  # 14 Base imponible gravada
            "0.00",  # 15 Descuento base
            num(b["igv"]),  # 16 IGV / IPM
            "0.00",  # 17 Descuento IGV
            num(b["exonerado"]),  # 18 Exonerado
            num(b["inafecto"]),  # 19 Inafecto
            "0.00",  # 20 ISC
            "0.00",  # 21 Base IVAP
            "0.00",  # 22 IVAP
            num(b["icbper"]),  # 23 Otros tributos (ICBPER)
            num(b["total"]),  # 24 Importe total
            moneda,  # 25 Moneda (tabla 4)
            "1.000"
            if moneda == "PEN"
            else "%.3f"
            % (
                1.0
                / (self.currency_id.with_context(date=self.invoice_date).rate or 1.0)
            ),  # 26 Tipo cambio
            of,  # 27 Fecha doc modificado
            ot,  # 28 Tipo doc modificado
            os_,  # 29 Serie doc modificado
            on,  # 30 Nro doc modificado
            "",  # 31 Contrato/proyecto
            "",  # 32 Error tipo 1
            "",  # 33 Indicador medio de pago
            estado,  # 34 Estado
        ]
        return "|".join(campos) + "|"

    @api.model
    def _l10n_pe_ne_ventas_periodo(self, periodo):
        """Comprobantes de venta válidos (01/03/07/08) del periodo YYYYMM, ordenados.
        Excluye borradores/rechazados; aislado por compañía. Compartido PLE + SIRE."""
        import calendar

        periodo = (periodo or "").strip()
        if len(periodo) != 6 or not periodo.isdigit():
            raise UserError(_("Periodo inválido. Usa YYYYMM (p.ej. 202606)."))
        year, month = int(periodo[:4]), int(periodo[4:6])
        if not (1 <= month <= 12):
            raise UserError(_("Mes inválido en el periodo."))
        last = calendar.monthrange(year, month)[1]
        d0 = fields.Date.to_date("%04d-%02d-01" % (year, month))
        d1 = fields.Date.to_date("%04d-%02d-%02d" % (year, month, last))
        return self.search(
            [
                ("move_type", "in", ("out_invoice", "out_refund")),
                ("state", "=", "posted"),
                ("invoice_date", ">=", d0),
                ("invoice_date", "<=", d1),
                ("l10n_pe_biller_state", "not in", ("por_enviar", "rechazado", False)),
            ],
            order="invoice_date, id",
        )

    @api.model
    def l10n_pe_ne_reporte_vinculadas(self, ejercicio=None):
        """Reporte de operaciones con partes vinculadas del ejercicio (año, default el actual) —
        sustento de la DJ Informativa de Precios de Transferencia / Reporte Local (V1). Lista los
        comprobantes emitidos a clientes marcados como parte vinculada, agrupados por cliente, con
        el tipo de vínculo, si es no domiciliada y el total (las NC restan). Aislado por compañía."""
        company = self.env.company
        year = int(ejercicio) if ejercicio else fields.Date.context_today(self).year
        d0 = fields.Date.to_date("%04d-01-01" % year)
        d1 = fields.Date.to_date("%04d-12-31" % year)
        moves = self.search(
            [
                ("company_id", "=", company.id),
                ("move_type", "in", ("out_invoice", "out_refund")),
                ("state", "=", "posted"),
                ("invoice_date", ">=", d0), ("invoice_date", "<=", d1),
                ("l10n_pe_biller_state", "not in", ("por_enviar", "rechazado", False)),
                ("partner_id.l10n_pe_ne_parte_vinculada", "=", True),
            ],
            order="partner_id, invoice_date, id",
        )
        tipos = dict(self.env["res.partner"]._fields["l10n_pe_ne_tipo_vinculo"].selection)
        por_cliente = {}
        total = 0.0
        for m in moves:
            p = m.partner_id
            g = por_cliente.setdefault(p.id, {
                "cliente": p.name or "", "numDoc": p.vat or "",
                "tipoVinculo": p.l10n_pe_ne_tipo_vinculo or "",
                "tipoVinculoNombre": tipos.get(p.l10n_pe_ne_tipo_vinculo, ""),
                "noDomiciliada": p.l10n_pe_ne_no_domiciliada,
                "pais": p.country_id.code or "", "comprobantes": 0, "total": 0.0,
            })
            signo = 1.0 if m.move_type == "out_invoice" else -1.0
            g["comprobantes"] += 1
            g["total"] = round(g["total"] + signo * (m.amount_total or 0.0), 2)
            total += signo * (m.amount_total or 0.0)
        items = sorted(por_cliente.values(), key=lambda x: -x["total"])
        return {
            "ejercicio": year,
            "items": items,
            "clientes": len(items),
            "comprobantes": len(moves),
            "total": round(total, 2),
            "moneda": company.currency_id.name or "PEN",
            # Cruce con los umbrales de obligación (V4) para que el reporte diga si hay que presentar.
            "umbrales": self.env["res.company"].l10n_pe_ne_vinculadas_umbrales(year),
        }

    @api.model
    def l10n_pe_ne_reporte_vinculadas_csv(self, ejercicio=None):
        """Descarga el reporte de operaciones con partes vinculadas como CSV — el sustento que el
        contador presenta con la Declaración Jurada (Reporte Local, precios de transferencia).

        Devuelve {filename, contentB64, count}, mismo contrato que los PLE. CSV separado por ';'
        (Excel es-PE) y en UTF-8 con BOM para que las tildes (vínculo, país) salgan bien. Lleva
        una cabecera con el cruce de umbrales (¿obligado al Reporte Local?) y la tabla por cliente."""
        rep = self.l10n_pe_ne_reporte_vinculadas(ejercicio)
        u = rep["umbrales"]
        mon = rep["moneda"]

        def celda(v):
            # Escapa comillas/;/salto de línea para un CSV robusto.
            s = "" if v is None else str(v)
            return '"%s"' % s.replace('"', '""') if any(c in s for c in ';"\n') else s

        def fila(*vals):
            return ";".join(celda(v) for v in vals)

        fmt = self._l10n_pe_fmt   # 2 decimales
        si_no = lambda b: _("Sí") if b else _("No")
        lineas = [
            fila(_("Reporte de operaciones con partes vinculadas")),
            fila(_("Ejercicio"), rep["ejercicio"]),
            fila(_("UIT"), fmt(u["uit"])),
            fila(_("Ingresos del ejercicio"), fmt(u["ingresos"]), _("Umbral Reporte Local (2300 UIT)"), fmt(u["umbralReporteLocal"])),
            fila(_("Operaciones con vinculadas"), fmt(u["operacionesVinculadas"]), _("Umbral operaciones (400 UIT)"), fmt(u["umbralOperaciones"])),
            fila(_("Obligado a presentar Reporte Local"), si_no(u["obligadoReporteLocal"])),
            "",
            fila(_("Documento"), _("Cliente"), _("Tipo de vínculo"), _("Cód. vínculo"),
                 _("No domiciliada"), _("País"), _("Comprobantes"), _("Total (%s)") % mon),
        ]
        for it in rep["items"]:
            lineas.append(fila(
                it["numDoc"], it["cliente"], it["tipoVinculoNombre"], it["tipoVinculo"],
                si_no(it["noDomiciliada"]), it["pais"], it["comprobantes"], fmt(it["total"])))
        lineas.append(fila("", _("TOTAL"), "", "", "", "", rep["comprobantes"], fmt(rep["total"])))

        content = "﻿" + "\r\n".join(lineas) + "\r\n"
        ruc = self.env.company.vat or ""
        return {
            "filename": "vinculadas-%s-%s.csv" % (ruc, rep["ejercicio"]),
            "contentB64": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "count": rep["clientes"],
        }

    @api.model
    def l10n_pe_ne_ple_ventas(self, periodo):
        """Genera el PLE 14.1 (Registro de Ventas) del periodo YYYYMM desde los
        comprobantes emitidos (01/03/07/08) de la compañía actual. Devuelve
        {filename, contentB64, count, periodo, total}. contentB64 = base64 del
        txt en ISO-8859-1 (lo que sube el contador al PLE de SUNAT)."""
        import base64

        periodo = (periodo or "").strip()
        moves = self._l10n_pe_ne_ventas_periodo(periodo)
        periodo8 = periodo + "00"
        lines = [m._l10n_pe_ne_ple_linea(periodo8, i) for i, m in enumerate(moves, 1)]
        content = ("\r\n".join(lines) + "\r\n") if lines else ""
        ruc = (self.env.company.vat or "").strip()
        ind_cont = "1" if lines else "0"  # contenido: con/sin información
        # LE + RUC + AAAAMM + DD(00) + 140100 + indOper(1) + indCont + moneda(1=PEN) + libro(1)
        filename = "LE%s%s00140100%s11.txt" % (ruc, periodo, "1" + ind_cont)
        return {
            "filename": filename,
            "contentB64": base64.b64encode(content.encode("latin-1", "replace")).decode(
                "ascii"
            ),
            "count": len(lines),
            "periodo": periodo,
            "total": sum(moves.mapped("amount_total")),
        }

    # ------------------------------------------------------------- PLE 8.1 (compras)
    #
    # ⚠ LA ESTRUCTURA DE ESTE FORMATO ESTÁ PENDIENTE DE VALIDACIÓN CONTABLE.
    #
    # Los anexos de SUNAT con el layout del 8.1 (RS 286-2009 anexo 2, y sus modificatorias)
    # se publican como PDF ESCANEADO: no hay de dónde extraerlo de forma fiable. Lo de abajo
    # espeja las convenciones del 14.1 de este mismo addon —que sí está en producción— y el
    # orden de campos que documenta SUNAT para el registro de compras, pero NADIE lo verificó
    # contra la norma vigente.
    #
    # Cada campo va NUMERADO a propósito, para que un contador pueda auditarlo uno por uno y
    # decir "el 14 no es ese" sin leer Python.
    #
    # El modo de fallo es benigno: el validador del PLE de SUNAT revisa la estructura, así que
    # un layout corrido se RECHAZA al subirlo — no entra mal en silencio. Aun así, que nadie
    # lo presente sin que su contador lo confirme primero.

    def _l10n_pe_ne_ple_compra_breakdown(self):
        """Desglose de una compra por afectación. Espeja _l10n_pe_ne_ple_breakdown (ventas),
        pero el registro de compras separa la base gravada por su DESTINO (a operaciones
        gravadas / mixtas / no gravadas), no por el tipo de tributo.

        Hoy todo va al destino "gravadas" (campo 14): es el caso de un negocio que vende
        gravado, que es el de esta app. Prorratear a operaciones no gravadas exige saber a qué
        se destina cada compra, dato que no se pide en ningún lado — sería inventarlo."""
        self.ensure_one()
        gravado = exonerado = inafecto = igv = 0.0
        for ln in self.invoice_line_ids:
            codes = ln.tax_ids.mapped("l10n_pe_edi_tax_code")
            base = ln.price_subtotal or 0.0
            if "1000" in codes:
                gravado += base
            elif "9997" in codes:
                exonerado += base
            else:
                inafecto += base
        igv = (self.amount_total or 0.0) - (self.amount_untaxed or 0.0)
        rnd = self.currency_id.rounding or 0.01
        return {
            "gravado": float_round(gravado, precision_rounding=rnd),
            "exonerado": float_round(exonerado, precision_rounding=rnd),
            "inafecto": float_round(inafecto, precision_rounding=rnd),
            "igv": float_round(igv, precision_rounding=rnd),
            "total": self.amount_total or 0.0,
        }

    def _l10n_pe_ne_ple_compra_linea(self, periodo8, cuo):
        """Una línea del PLE 8.1. Campos numerados: son POSICIONALES y separados por '|', así
        que un campo de más o de menos corre todos los siguientes."""
        self.ensure_one()
        num = self._l10n_pe_ne_ple_num
        b = self._l10n_pe_ne_ple_compra_breakdown()
        doc = self.l10n_latam_document_number or self.ref or ""
        serie, _sep, corr = doc.partition("-")
        tipo = (
            self.l10n_latam_document_type_id.code
            if self.l10n_latam_document_type_id
            else "01"
        )
        fecha = self.invoice_date.strftime("%d/%m/%Y") if self.invoice_date else ""
        moneda = self.currency_id.name or "PEN"
        # Tipo de documento del proveedor (tabla 2): 6 = RUC. Sin RUC no hay crédito fiscal,
        # así que el caso normal de este registro es 6.
        ndoc = (self.partner_id.vat or "").strip()
        tdoc = "6" if len(ndoc) == 11 else ("1" if ndoc else "0")
        campos = [
            periodo8,  # 1  Periodo (AAAAMM00)
            str(self.id),  # 2  CUO (único por operación)
            "",  # 3  Nro correlativo del asiento (solo estados 8/9)
            fecha,  # 4  Fecha de emisión del comprobante
            "",  # 5  Fecha de vencimiento o pago
            tipo,  # 6  Tipo de comprobante (tabla 10)
            serie,  # 7  Serie del comprobante
            "",  # 8  Año de emisión de la DUA/DSI (solo importaciones)
            corr,  # 9  Número del comprobante
            "",  # 10 Número final (rango) / DUA
            tdoc,  # 11 Tipo de documento del proveedor (tabla 2)
            ndoc,  # 12 Número de documento del proveedor
            (self.partner_id.name or "").upper(),  # 13 Razón social del proveedor
            num(b["gravado"]),  # 14 Base imponible destinada a operaciones GRAVADAS
            num(b["igv"]),  # 15 IGV/IPM de 14
            "0.00",  # 16 Base destinada a operaciones gravadas Y no gravadas
            "0.00",  # 17 IGV/IPM de 16
            "0.00",  # 18 Base destinada a operaciones NO gravadas
            "0.00",  # 19 IGV/IPM de 18
            num(b["exonerado"] + b["inafecto"]),  # 20 Valor de adquisiciones no gravadas
            "0.00",  # 21 ISC
            "0.00",  # 22 ICBPER
            "0.00",  # 23 Otros tributos y cargos
            num(b["total"]),  # 24 Importe total
            "",  # 25 Código de la moneda (tabla 4) — ver nota abajo
            "",  # 26 Tipo de cambio
            "",  # 27 Fecha de emisión del comprobante modificado
            "",  # 28 Tipo del comprobante modificado
            "",  # 29 Serie del comprobante modificado
            "",  # 30 Número del comprobante modificado
            "",  # 31 Fecha de la constancia de detracción
            "",  # 32 Número de la constancia de detracción
            "",  # 33 Marca del comprobante sujeto a retención
            "",  # 34 Clasificación de bienes y servicios
            "",  # 35 Identificación del contrato o proyecto
            "",  # 36 Error tipo 1
            "",  # 37 Error tipo 9
            "",  # 38 Errores tipo 4
            "",  # 39 Indicador de comprobante de pago cancelado con medio de pago
            "1",  # 40 Estado (1 = registro que corresponde al periodo)
        ]
        # Moneda y tipo de cambio: se llenan acá y no en la lista para no repetir el cálculo.
        campos[24] = moneda
        campos[25] = (
            "1.000"
            if moneda == "PEN"
            else "%.3f"
            % (1.0 / (self.currency_id.with_context(date=self.invoice_date).rate or 1.0))
        )
        return "|".join(campos) + "|"

    @api.model
    def _l10n_pe_ne_compras_periodo(self, periodo):
        """Compras posteadas del periodo YYYYMM, ordenadas. Aislado por compañía.
        Espeja _l10n_pe_ne_ventas_periodo, con move_type de proveedor."""
        import calendar

        periodo = (periodo or "").strip()
        if len(periodo) != 6 or not periodo.isdigit():
            raise UserError(_("Periodo inválido. Usa YYYYMM (p.ej. 202606)."))
        year, month = int(periodo[:4]), int(periodo[4:6])
        if not (1 <= month <= 12):
            raise UserError(_("Mes inválido en el periodo."))
        last = calendar.monthrange(year, month)[1]
        d0 = fields.Date.to_date("%04d-%02d-01" % (year, month))
        d1 = fields.Date.to_date("%04d-%02d-%02d" % (year, month, last))
        return self.search(
            [
                ("move_type", "in", ("in_invoice", "in_refund")),
                ("state", "=", "posted"),
                ("invoice_date", ">=", d0),
                ("invoice_date", "<=", d1),
            ],
            order="invoice_date, id",
        )

    @api.model
    def l10n_pe_ne_ple_compras(self, periodo):
        """PLE 8.1 (Registro de Compras) del periodo YYYYMM. Devuelve
        {filename, contentB64, count, periodo, total}. Espeja l10n_pe_ne_ple_ventas.

        ⚠ Estructura pendiente de validación contable (ver la nota del bloque)."""
        import base64

        periodo = (periodo or "").strip()
        moves = self._l10n_pe_ne_compras_periodo(periodo)
        periodo8 = periodo + "00"
        lines = [
            m._l10n_pe_ne_ple_compra_linea(periodo8, i) for i, m in enumerate(moves, 1)
        ]
        content = ("\r\n".join(lines) + "\r\n") if lines else ""
        ruc = (self.env.company.vat or "").strip()
        ind_cont = "1" if lines else "0"
        # Mismo patrón que el 14.1, cambiando el código del libro: 140100 → 080100.
        filename = "LE%s%s00080100%s11.txt" % (ruc, periodo, "1" + ind_cont)
        return {
            "filename": filename,
            "contentB64": base64.b64encode(content.encode("latin-1", "replace")).decode(
                "ascii"
            ),
            "count": len(lines),
            "periodo": periodo,
            "total": sum(moves.mapped("amount_total")),
        }

    # ------------------------------------- PLE 12.1 (inventario en unidades físicas)
    #
    # ⚠ ESTRUCTURA PENDIENTE DE VALIDACIÓN CONTABLE, igual que el 8.1: los anexos de SUNAT
    # con el layout se publican como PDF escaneado. Cada campo va numerado para auditarlo.
    #
    # Se hace el de UNIDADES FÍSICAS y NO el valorizado, y no por comodidad: el valorizado
    # exige el costo de cada movimiento, y con la valorización en `periodic` —el default de
    # Odoo, el que esta app deja puesto— `stock.move.value` y `price_unit` salen en CERO.
    # Verificado sobre los movimientos reales de la BD. Inventar ese costo (p. ej. usando el
    # precio de lista) sería declararle a SUNAT un número que no salió de ningún lado.
    # Para el valorizado hay que pasar la compañía a valorización perpetua, que cambia los
    # asientos contables: es una decisión del contador, no un efecto colateral de un reporte.

    @api.model
    def _l10n_pe_ne_kardex_periodo(self, periodo):
        """Movimientos de inventario del periodo YYYYMM, ordenados. Solo los que cruzan la
        frontera del almacén (entradas y salidas reales); los internos no son del kardex."""
        import calendar

        periodo = (periodo or "").strip()
        if len(periodo) != 6 or not periodo.isdigit():
            raise UserError(_("Periodo inválido. Usa YYYYMM (p.ej. 202606)."))
        year, month = int(periodo[:4]), int(periodo[4:6])
        if not (1 <= month <= 12):
            raise UserError(_("Mes inválido en el periodo."))
        last = calendar.monthrange(year, month)[1]
        return self.env["stock.move.line"].search(
            [
                ("state", "=", "done"),
                ("company_id", "=", self.env.company.id),
                ("date", ">=", "%04d-%02d-01 00:00:00" % (year, month)),
                ("date", "<=", "%04d-%02d-%02d 23:59:59" % (year, month, last)),
                ("product_id.is_storable", "=", True),
                # Solo lo que entra o sale del almacén: un traslado interno no es del kardex.
                "|",
                ("location_id.usage", "in", ("supplier", "customer", "inventory")),
                ("location_dest_id.usage", "in", ("supplier", "customer", "inventory")),
            ],
            order="date, id",
        )

    @api.model
    def _l10n_pe_ne_kardex_linea(self, ml, periodo8, cuo, saldo):
        """Una línea del PLE 12.1. Campos POSICIONALES separados por '|'."""
        num = self._l10n_pe_ne_ple_num
        entra = ml.location_dest_id.usage == "internal"
        cant = abs(ml.quantity or 0)
        doc = ml.move_id.l10n_pe_ne_move_id
        if doc and doc.move_type in ("out_invoice", "out_refund"):
            # VENTA: la serie/correlativo salen del mismo helper que usa la emisión y la
            # baja. Partir l10n_latam_document_number por "-" no sirve: en una venta ese
            # campo trae solo el número, y la serie terminaba llevándose el correlativo.
            serie = doc.l10n_pe_ne_serie_emit or doc._l10n_pe_serie_correlativo()[0]
            corr = doc.l10n_pe_ne_corr_emit or doc._l10n_pe_serie_correlativo()[1]
            tipo = doc.l10n_pe_ne_tipo_doc or doc._l10n_pe_document_type()
            fecha = doc.invoice_date.strftime("%d/%m/%Y") if doc.invoice_date else ""
        elif doc:
            # COMPRA: acá el documento es del proveedor y sí viene como "F001-00095001".
            serie, _sep, corr = (doc.l10n_latam_document_number or doc.ref or "").partition("-")
            tipo = (
                doc.l10n_latam_document_type_id.code
                if doc.l10n_latam_document_type_id
                else "01"
            )
            fecha = doc.invoice_date.strftime("%d/%m/%Y") if doc.invoice_date else ""
        else:
            # Ajuste de inventario: no nace de un comprobante. Tipo 00 = "otros" (tabla 10).
            serie, corr, tipo = "", "", "00"
            fecha = ml.date.strftime("%d/%m/%Y") if ml.date else ""
        p = ml.product_id
        campos = [
            periodo8,  # 1  Periodo (AAAAMM00)
            str(ml.id),  # 2  CUO (único por movimiento)
            "",  # 3  Nro correlativo del asiento
            fecha,  # 4  Fecha de emisión del documento
            tipo,  # 5  Tipo de documento (tabla 10)
            serie,  # 6  Serie del documento
            corr,  # 7  Número del documento
            "01" if entra else "02",  # 8  Tipo de operación (tabla 12): 01 entrada, 02 salida
            p.default_code or "",  # 9  Código de la existencia
            "01",  # 10 Tipo de existencia (tabla 5): 01 mercadería
            (p.name or "")[:100],  # 11 Descripción de la existencia
            p.l10n_pe_ne_unit_code or "NIU",  # 12 Unidad de medida (tabla 6)
            "",  # 13 Método de valuación (solo en el valorizado)
            num(cant) if entra else "0.00",  # 14 Entradas — cantidad
            "0.00" if entra else num(cant),  # 15 Salidas — cantidad
            num(saldo),  # 16 Saldo final — cantidad
            "1",  # 17 Estado (1 = del periodo)
        ]
        return "|".join(campos) + "|"

    @api.model
    def l10n_pe_ne_ple_inventario(self, periodo):
        """PLE 12.1 (Registro de Inventario Permanente en Unidades Físicas) del periodo.

        ⚠ Estructura pendiente de validación contable (ver la nota del bloque)."""
        import base64
        from collections import defaultdict

        periodo = (periodo or "").strip()
        lineas_ml = self._l10n_pe_ne_kardex_periodo(periodo)
        periodo8 = periodo + "00"
        # El saldo se arrastra POR PRODUCTO en el orden de los movimientos: es lo que hace
        # legible un kardex — cada renglón muestra con cuánto quedó esa existencia.
        saldos = defaultdict(float)
        lines = []
        for i, ml in enumerate(lineas_ml, 1):
            entra = ml.location_dest_id.usage == "internal"
            cant = abs(ml.quantity or 0)
            saldos[ml.product_id.id] += cant if entra else -cant
            lines.append(
                self._l10n_pe_ne_kardex_linea(ml, periodo8, i, saldos[ml.product_id.id])
            )
        content = ("\r\n".join(lines) + "\r\n") if lines else ""
        ruc = (self.env.company.vat or "").strip()
        ind_cont = "1" if lines else "0"
        # Mismo patrón que el 14.1/8.1, con el código del libro 120100.
        filename = "LE%s%s00120100%s11.txt" % (ruc, periodo, "1" + ind_cont)
        return {
            "filename": filename,
            "contentB64": base64.b64encode(content.encode("latin-1", "replace")).decode(
                "ascii"
            ),
            "count": len(lines),
            "periodo": periodo,
            "total": 0.0,
        }

    @api.model
    def l10n_pe_ne_dashboard(self, periodo=None):
        """Datos del dashboard de ventas del periodo (YYYYMM, default mes actual):
        serie diaria (para el gráfico), desglose por tipo de comprobante y KPIs.
        Reusa el filtro de ventas; aislado por compañía."""
        import calendar

        if not periodo:
            periodo = fields.Date.context_today(self).strftime("%Y%m")
        moves = self._l10n_pe_ne_ventas_periodo(periodo)
        year, month = int(periodo[:4]), int(periodo[4:6])
        por_dia, por_tipo = {}, {}
        total = anulados = 0.0
        for m in moves:
            key = m.invoice_date.strftime("%Y-%m-%d") if m.invoice_date else ""
            por_dia[key] = por_dia.get(key, 0.0) + (m.amount_total or 0.0)
            t = m.l10n_pe_ne_tipo_doc or m._l10n_pe_document_type()
            agg = por_tipo.setdefault(t, {"count": 0, "total": 0.0})
            agg["count"] += 1
            agg["total"] += m.amount_total or 0.0
            total += m.amount_total or 0.0
            if m.l10n_pe_biller_state == "anulado":
                anulados += 1
        ndays = calendar.monthrange(year, month)[1]
        serie = [
            {
                "dia": d,
                "total": round(
                    por_dia.get("%04d-%02d-%02d" % (year, month, d), 0.0), 2
                ),
            }
            for d in range(1, ndays + 1)
        ]
        tipos = [
            {
                "tipoDoc": t,
                "count": v["count"],
                "total": round(v["total"], 2),
            }
            for t, v in sorted(por_tipo.items())
        ]
        gastos = self.env["l10n_pe_ne.gasto"].l10n_pe_ne_total_gastos(periodo)
        return {
            "periodo": periodo,
            "total": round(total, 2),
            "count": len(moves),
            "anulados": int(anulados),
            "gastos": gastos,
            "neto": round(total - gastos, 2),
            "porDia": serie,
            "porTipo": tipos,
        }

    @api.model
    def l10n_pe_ne_reporte_ventas(self, periodo=None):
        """Reportes de ventas del periodo (YYYYMM, default mes actual): resumen,
        ventas de hoy, top por producto y top por cliente. Reusa el filtro de
        ventas; aislado por compañía. (Suma amount_total en su moneda; mezclar
        PEN/USD es aproximado para el MVP.)"""
        if not periodo:
            periodo = fields.Date.context_today(self).strftime("%Y%m")
        moves = self._l10n_pe_ne_ventas_periodo(periodo)
        today = fields.Date.context_today(self)
        prod, cli = {}, {}
        hoy_count, hoy_total = 0, 0.0
        for m in moves:
            for ln in m.invoice_line_ids:
                key = (
                    ln.product_id.display_name if ln.product_id else (ln.name or "ITEM")
                )
                a = prod.setdefault(
                    key, {"cantidad": 0.0, "total": 0.0, "base": 0.0, "costo": 0.0}
                )
                a["cantidad"] += ln.quantity or 0.0
                a["total"] += ln.price_total or 0.0
                # Rentabilidad: valor de venta SIN IGV (price_subtotal) vs costo del
                # producto (standard_price × cantidad). El costo es 0 si el producto no
                # lo tiene registrado → la utilidad de esa línea queda sobrestimada.
                a["base"] += ln.price_subtotal or 0.0
                a["costo"] += (ln.quantity or 0.0) * (
                    ln.product_id.standard_price or 0.0
                )
            kc = (m.partner_id.name or "—", m.partner_id.vat or "")
            c = cli.setdefault(kc, {"count": 0, "total": 0.0})
            c["count"] += 1
            c["total"] += m.amount_total or 0.0
            if m.invoice_date == today:
                hoy_count += 1
                hoy_total += m.amount_total or 0.0
        por_producto = sorted(
            (
                {
                    "producto": k,
                    "cantidad": round(v["cantidad"], 2),
                    "total": round(v["total"], 2),
                    "venta": round(v["base"], 2),
                    "costo": round(v["costo"], 2),
                    "utilidad": round(v["base"] - v["costo"], 2),
                    # Margen % sobre el valor de venta. None si el producto no tiene costo
                    # registrado (no se puede calcular una utilidad real).
                    "margen": round((v["base"] - v["costo"]) / v["base"] * 100, 1)
                    if v["base"] and v["costo"]
                    else None,
                }
                for k, v in prod.items()
            ),
            key=lambda x: -x["total"],
        )[:50]
        # Resumen de rentabilidad del periodo. Se calcula SOLO sobre productos con costo
        # registrado (los de costo 0 inflarían la utilidad como si todo fuera ganancia).
        # `conCosto`/`totalProductos` le dice al front qué tan completa es la estimación.
        rent_venta = sum(v["base"] for v in prod.values() if v["costo"])
        rent_costo = sum(v["costo"] for v in prod.values() if v["costo"])
        rentabilidad = {
            "venta": round(rent_venta, 2),
            "costo": round(rent_costo, 2),
            "utilidad": round(rent_venta - rent_costo, 2),
            "margen": round((rent_venta - rent_costo) / rent_venta * 100, 1)
            if rent_venta and rent_costo
            else None,
            "conCosto": sum(1 for v in prod.values() if v["costo"]),
            "totalProductos": len(prod),
        }
        por_cliente = sorted(
            (
                {
                    "cliente": k[0],
                    "ruc": k[1],
                    "count": v["count"],
                    "total": round(v["total"], 2),
                }
                for k, v in cli.items()
            ),
            key=lambda x: -x["total"],
        )[:50]
        return {
            "periodo": periodo,
            "resumen": {
                "count": len(moves),
                "total": round(sum(moves.mapped("amount_total")), 2),
            },
            "hoy": {"count": hoy_count, "total": round(hoy_total, 2)},
            "rentabilidad": rentabilidad,
            "porProducto": por_producto,
            "porCliente": por_cliente,
        }

    @api.model
    def l10n_pe_ne_export(self, tipo, periodo=None):
        """Centro de descargas: exporta a XLSX. tipo = ventas|productos|clientes
        (ventas usa el periodo). Devuelve {filename, contentB64, count}."""
        import base64
        import io

        import xlsxwriter

        tipo = (tipo or "ventas").strip().lower()
        if tipo == "ventas":
            if not periodo:
                periodo = fields.Date.context_today(self).strftime("%Y%m")
            moves = self._l10n_pe_ne_ventas_periodo(periodo)
            headers = [
                "Serie",
                "Número",
                "Tipo",
                "Fecha",
                "Cliente",
                "Doc. cliente",
                "Gravada",
                "Exonerada",
                "Inafecta",
                "IGV",
                "ICBPER",
                "Total",
                "Moneda",
                "Estado",
            ]
            rows = []
            for m in moves:
                b = m._l10n_pe_ne_ple_breakdown()
                serie, num = m._l10n_pe_ne_doc_id()
                rows.append(
                    [
                        serie,
                        num,
                        m.l10n_pe_ne_tipo_doc or m._l10n_pe_document_type(),
                        m.invoice_date.strftime("%d/%m/%Y") if m.invoice_date else "",
                        m.partner_id.name or "",
                        m.partner_id.vat or "",
                        round(b["gravado"], 2),
                        round(b["exonerado"], 2),
                        round(b["inafecto"], 2),
                        round(b["igv"], 2),
                        round(b["icbper"], 2),
                        round(b["total"], 2),
                        m.currency_id.name or "PEN",
                        m.l10n_pe_biller_state or "",
                    ]
                )
            sheet, base = "Ventas", "ventas-%s" % periodo
        elif tipo == "productos":
            prods = self.l10n_pe_ne_list_productos(limit=10000)
            headers = ["Código", "Descripción", "Precio", "Afectación"]
            rows = [
                [
                    p.get("codigo", ""),
                    p.get("descripcion", ""),
                    p.get("precio", 0),
                    p.get("taxCode", ""),
                ]
                for p in prods
            ]
            sheet, base = "Productos", "productos"
        elif tipo == "clientes":
            clis = self.l10n_pe_ne_list_clientes(limit=10000)
            headers = [
                "Razón social",
                "Tipo doc",
                "Número",
                "Email",
                "Teléfono",
                "Dirección",
            ]
            rows = [
                [
                    c.get("razonSocial", ""),
                    c.get("tipoDocNombre", ""),
                    c.get("numDoc", ""),
                    c.get("email", ""),
                    c.get("telefono", ""),
                    c.get("direccion", ""),
                ]
                for c in clis
            ]
            sheet, base = "Clientes", "clientes"
        else:
            raise UserError(
                _("Tipo de exporte no soportado (ventas|productos|clientes).")
            )
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        ws = wb.add_worksheet(sheet)
        head = wb.add_format(
            {"bold": True, "bg_color": "#2563eb", "font_color": "white", "border": 1}
        )
        for c, h in enumerate(headers):
            ws.write(0, c, h, head)
            ws.set_column(c, c, max(12, len(h) + 2))
        for r, row in enumerate(rows, 1):
            for c, val in enumerate(row):
                ws.write(r, c, val)
        ws.autofilter(0, 0, max(1, len(rows)), len(headers) - 1)
        ws.freeze_panes(1, 0)
        wb.close()
        ruc = (self.env.company.vat or "").strip()
        return {
            "filename": "%s-%s.xlsx" % (base, ruc),
            "count": len(rows),
            "contentB64": base64.b64encode(buf.getvalue()).decode("ascii"),
        }

    @api.model
    def l10n_pe_ne_rvie_reemplazo(self, periodo):
        """SIRE — archivo de REEMPLAZO de la propuesta del RVIE (Registro de Ventas)
        del periodo YYYYMM, empaquetado en ZIP, para que el contador reemplace la
        propuesta de SUNAT. Reusa el motor de líneas del PLE (mismo desglose).

        Nombre SIRE (35 chars): LE + RUC + AAAA + MM + 00 + 140000(libro RVIE) +
        02(reemplazo) + O(operaciones) + I(contenido) + M(moneda) + 2(fijo) + NN(secuencia).
        Devuelve {zipFilename, txtFilename, contentB64 (zip base64), count, total}.
        OJO: el layout EXACTO de campos del Anexo 3 se valida/ajusta contra el PVSIRE."""
        import base64
        import io
        import zipfile

        periodo = (periodo or "").strip()
        moves = self._l10n_pe_ne_ventas_periodo(periodo)
        periodo8 = periodo + "00"
        lines = [m._l10n_pe_ne_ple_linea(periodo8, i) for i, m in enumerate(moves, 1)]
        content = ("\r\n".join(lines) + "\r\n") if lines else ""
        ruc = (self.env.company.vat or "").strip()
        cont = "1" if lines else "0"  # I: con/sin información
        # LE+RUC+AAAAMM+00 + 140000(libro RVIE) + 02(reemplazo) + O=1 + I=cont + M=1(soles) + 2 + NN=00
        txt_name = "LE%s%s00140000021%s1200.txt" % (ruc, periodo, cont)
        zip_name = txt_name[:-4] + ".zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(txt_name, content.encode("latin-1", "replace"))
        return {
            "zipFilename": zip_name,
            "txtFilename": txt_name,
            "contentB64": base64.b64encode(buf.getvalue()).decode("ascii"),
            "count": len(lines),
            "periodo": periodo,
            "total": sum(moves.mapped("amount_total")),
        }

    @api.model
    def _l10n_pe_ne_margen_default(self):
        """Margen por defecto del negocio, en %. Configurable en caliente sin redeploy.
        30% es un punto de partida razonable para el retail peruano, no una verdad: cada
        negocio lo ajusta, y cada producto puede tener el suyo."""
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "l10n_pe_ne.margen_default", "30"
        )
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 30.0

    @api.model
    def _l10n_pe_ne_precio_con_margen(self, costo, margen):
        """Costo → precio de venta, ambos CON IGV.

        El margen se aplica sobre el bruto porque toda la app trabaja con precios de vitrina:
        así el número que sale es el que va en la etiqueta, sin desarmar el impuesto para
        pensar el negocio. Redondea a 2: es un precio, no una base imponible.

        `margen=None` usa el default del negocio; `margen=0` es un margen de CERO (vender al
        costo) y se respeta. Se distingue con `is None` a propósito: en Python 0 == False, y
        un `if not margen` convertiría el 0% en el default — el producto de promoción saldría
        30% más caro sin que nadie lo pidiera.

        El campo del producto es Float y no distingue "sin margen" de "0%": su 0 significa
        "usa el default", y quien llama lo traduce a None."""
        c = float(costo or 0)
        m = self._l10n_pe_ne_margen_default() if margen is None else float(margen)
        return round(c * (1 + m / 100.0), 2)

    @api.model
    def _l10n_pe_ne_rastreo_producto(self, rastreo):
        """Rastreo en Odoo: 'lot' | 'serial' | 'none'. La API habla el vocabulario del
        negocio ("lote"/"serie"), no el de Odoo.

        Solo tiene sentido con existencias: Odoo no rastrea lo que no cuenta. Y el rastreo
        NO se decide solo — es del producto: la misma caja de paracetamol necesita lote y un
        tornillo no."""
        r = (rastreo or "").strip().lower()
        if r in ("lote", "lot"):
            return "lot"
        if r in ("serie", "serial", "imei"):
            return "serial"
        return "none"

    @api.model
    def _l10n_pe_ne_tipo_producto(self, tipo=None, unidad=None):
        """Tipo del producto en Odoo: 'consu' (bien) o 'service' (servicio).

        Manda lo que el usuario eligió (`tipo`: "bien"/"servicio"). Si no eligió —el producto
        se auto-crea al emitir, donde no hay quién responda— se deduce de la UNIDAD, que es la
        única señal real que tiene la línea: ZZ es la unidad de servicio del catálogo 03 de
        SUNAT; cualquier otra (NIU, KGM, …) describe algo tangible.

        Antes esto era "service" fijo, lo que además se contradecía con SUNAT: sin unidad se
        emite NIU (DEFAULT_UNIT_CODE), o sea que al mismo producto se le declaraba BIEN a SUNAT
        y servicio en Odoo. Ahora ambos dicen lo mismo, y el default coincide con el de Odoo.

        No se toca `is_storable` (llevar stock o no): ese campo solo existe con el módulo
        `stock` instalado, y hoy no lo está. Es una decisión aparte, por producto.
        """
        t = (tipo or "").strip().lower()
        if t in ("bien", "bienes", "producto", "consu"):
            return "consu"
        if t in ("servicio", "servicios", "service"):
            return "service"
        return "service" if (unidad or "").strip().upper() == "ZZ" else "consu"
    def _l10n_pe_ne_lineas_con_stock(self):
        """Líneas del comprobante cuyo producto lleva existencias. En Odoo 19 eso es
        type='consu' Y is_storable=True: 'consu' solo dice que es tangible; el booleano decide
        si se rastrea. Un servicio nunca mueve stock."""
        self.ensure_one()
        return self.invoice_line_ids.filtered(
            lambda l: l.product_id
            and l.product_id.type == "consu"
            and l.product_id.is_storable
            and (l.quantity or 0) > 0
        )

    def _l10n_pe_ne_mover_stock(self, reversa=False):
        """Descuenta (o repone) el stock de las líneas de bien del comprobante.

        `reversa=True` invierte la dirección y marca los movimientos como reversa: lo usa
        _l10n_pe_ne_revertir_stock cuando SUNAT rechaza.

        La factura NO mueve stock en Odoo: los movimientos nacen de un stock.picking, que en
        el flujo estándar viene de un sale.order. Esta app no usa sale.order —emite el
        account.move directo— así que el movimiento se crea aquí, igual que hace el POS de
        Odoo (pos_order._create_order_picking() corre aparte de _generate_pos_order_invoice()).

        Dirección según el documento:
          * 01/03 (factura/boleta) → SALIDA: existencias → cliente.
          * 07 (nota de crédito)   → ENTRADA: cliente → existencias. Sin esto, anular una
            venta dejaría el stock descontado para siempre y el kardex se iría en falso.
          * 08 (nota de débito)    → nada: es un cargo (mora, penalidad), no mueve bienes.

        NUNCA bloquea la venta: si no hay existencias el movimiento igual se hace y el stock
        queda negativo — coherente con la caja, que tampoco bloquea. Un negativo es una señal
        visible de que falta un ajuste, y es preferible a impedirle vender a quien tiene el
        producto en la mano. Los fallos se tragan a propósito: el comprobante ya es válido
        ante SUNAT y no puede caerse porque el inventario no cuadre.
        """
        self.ensure_one()
        # Solo documentos de VENTA. _l10n_pe_document_type() no distingue: para un `in_invoice`
        # (una compra) devuelve '03', así que sin esta guarda una compra entraría por acá y
        # SACARÍA el stock en vez de meterlo. La compra va por _l10n_pe_ne_mover_stock_compra.
        if self.move_type not in ("out_invoice", "out_refund"):
            return self.env["stock.move"].browse()
        tipo = self.l10n_pe_ne_tipo_doc or self._l10n_pe_document_type()
        if tipo not in ("01", "03", "07"):
            return self.env["stock.move"].browse()
        lineas = self._l10n_pe_ne_lineas_con_stock()
        if not lineas:
            return self.env["stock.move"].browse()
        wh = self.env["stock.warehouse"].search(
            [("company_id", "=", self.company_id.id)], limit=1
        )
        clientes = self.env.ref(
            "stock.stock_location_customers", raise_if_not_found=False
        )
        if not wh or not clientes:
            _logger.warning(
                "stock: sin almacén o ubicación de clientes para %s; no se mueve stock",
                self.name,
            )
            return self.env["stock.move"].browse()
        # La NC va al revés que la factura; y una reversa va al revés de lo que sea.
        # Los dos XOR: la reversa de una NC vuelve a ser una salida.
        entrada = (tipo == "07") != bool(reversa)
        origen, destino = (
            (clientes, wh.lot_stock_id) if entrada else (wh.lot_stock_id, clientes)
        )
        return self._l10n_pe_ne_stock_aplicar(lineas, origen, destino, reversa=reversa)

    def _l10n_pe_ne_lote_de(self, linea):
        """stock.lot de una línea de compra, creándolo si hace falta. None si el producto no
        se rastrea o la línea no trae lote.

        Solo la ENTRADA define el lote. En la salida no se pide: Odoo lo asigna al reservar,
        por su estrategia de salida — con vencimiento, lo que caduca antes sale primero, que
        es justo lo que una farmacia necesita. Verificado contra Odoo 19."""
        prod = linea.product_id
        if not prod or prod.tracking == "none":
            return None
        nombre = (linea.l10n_pe_ne_lote or "").strip()
        if not nombre:
            return None
        Lot = self.env["stock.lot"]
        lote = Lot.search(
            [("name", "=", nombre), ("product_id", "=", prod.id),
             ("company_id", "=", self.company_id.id)], limit=1)
        if not lote:
            vals = {"name": nombre, "product_id": prod.id, "company_id": self.company_id.id}
            lote = Lot.create(vals)
        # El vencimiento se escribe aparte: el campo lo agrega product_expiry y solo tiene
        # sentido si el producto lo usa. Se pisa solo si la línea trae uno.
        if linea.l10n_pe_ne_vence and prod.use_expiration_date:
            lote.expiration_date = linea.l10n_pe_ne_vence
        return lote

    def _l10n_pe_ne_stock_qty(self, line):
        """Cantidad a mover en la UoM del producto (el empaque). Normal = |cantidad|. Fraccionada
        = |cantidad| / unidades_por_empaque: la línea va en sub-unidades pero el stock se lleva en
        empaques, así vender 5 unidades de una caja de 30 descuenta 5/30 de caja."""
        qty = abs(line.quantity or 0.0)
        if line.l10n_pe_ne_fraccionado:
            factor = line.product_id.l10n_pe_ne_unidades_por_empaque or 0.0
            if factor > 0:
                return qty / factor
        return qty

    def _l10n_pe_ne_stock_aplicar(self, lineas, origen, destino, reversa=False, con_lote=False):
        """Motor común: crea y valida los movimientos de `lineas` entre dos ubicaciones.

        Lo comparten la venta (existencias → cliente), la devolución por NC, la reversa de un
        rechazo y la compra (proveedor → existencias). Lo único que cambia entre ellas son las
        dos ubicaciones y el sentido; la mecánica —y el "nunca bloquear"— es la misma.

        `con_lote`: la ENTRADA asigna el lote que trae la línea. La salida no lo necesita —
        Odoo lo asigna al reservar.
        """
        self.ensure_one()
        moves = self.env["stock.move"].browse()
        lotes = {}
        for l in lineas:
            if con_lote:
                lotes[l.id] = self._l10n_pe_ne_lote_de(l)
            # Sin 'name': stock.move no lo tiene en Odoo 19 (su `reference` se computa).
            # `origin` deja el rastro legible; l10n_pe_ne_move_id es el enlace real (por id).
            moves |= self.env["stock.move"].create(
                {
                    "product_id": l.product_id.id,
                    "product_uom_qty": self._l10n_pe_ne_stock_qty(l),
                    "product_uom": l.product_uom_id.id,
                    "location_id": origen.id,
                    "location_dest_id": destino.id,
                    "company_id": self.company_id.id,
                    "origin": self.name or "",
                    "l10n_pe_ne_move_id": self.id,
                    "l10n_pe_ne_reversa": reversa,
                }
            )
        try:
            moves._action_confirm()
            moves._action_assign()
            for m, l in zip(moves, lineas):
                lote = lotes.get(l.id)
                if lote:
                    # Entrada de un producto rastreado: el lote va en la LÍNEA del movimiento
                    # (stock.move_line), no en el move. Sin esto Odoo lanza "debe proporcionar
                    # un número de serie o lote" y la mercadería no entraría.
                    if not m.move_line_ids:
                        m.move_line_ids = [(0, 0, {
                            "product_id": m.product_id.id,
                            "location_id": m.location_id.id,
                            "location_dest_id": m.location_dest_id.id,
                            "company_id": m.company_id.id,
                        })]
                    m.move_line_ids.write({"lot_id": lote.id})
                # quantity explícito: sin esto _action_done mueve solo lo reservado, y sin
                # existencias no reserva nada → la salida quedaría en 0 y el kardex mentiría.
                m.quantity = m.product_uom_qty
                m.picked = True
            moves._action_done()
            # La fecha del movimiento es la del DOCUMENTO, no la de cuando se registró.
            # Odoo pone `date` = ahora al validar; sin corregirlo, una compra de marzo
            # cargada en julio caería en el kardex de julio y el libro del periodo saldría
            # mal. Se escribe después de _action_done porque antes lo pisa él.
            if self.invoice_date:
                moves.write({"date": self.invoice_date})
                moves.move_line_ids.write({"date": self.invoice_date})
        except Exception as e:  # noqa: BLE001 — el documento ya existe: el stock no lo tumba
            # Se traga a propósito: el comprobante ya es válido ante SUNAT y no puede caerse
            # porque el inventario no cuadre. Pero se deja RASTRO en el documento, no solo en
            # el log: un movimiento que no ocurre y nadie ve es un kardex mintiendo en
            # silencio. El caso típico es un producto rastreado sin existencias en ningún
            # lote — ahí Odoo no puede inventar de dónde sale.
            _logger.exception("stock: no se pudo mover el stock de %s: %s", self.name, e)
            self.l10n_pe_ne_stock_aviso = (
                _("No se pudo mover el inventario de este documento: %s") % e
            )[:500]
            return self.env["stock.move"].browse()
        self.l10n_pe_ne_stock_aviso = False
        return moves

    @api.model
    def _l10n_pe_ne_asegurar_fefo(self, wh):
        """Pone la ubicación de existencias en FEFO: sale primero lo que vence antes.

        El default de Odoo es FIFO —sale lo que entró primero—, y para lo que caduca eso está
        MAL: comprobado con dos lotes (uno vence 2026, otro 2028), la venta se llevó el de
        2028 y dejó el de 2026 pudriéndose en el almacén. En una farmacia eso es plata tirada
        y riesgo sanitario.

        FEFO no perjudica a lo que no vence: sin fecha de caducidad, Odoo cae de vuelta al
        orden de entrada. Por eso se pone en la ubicación y no producto por producto.

        Idempotente: si ya está, no toca nada. Se llama al ingresar mercadería porque es
        cuando la ubicación empieza a importar — no hay un lugar mejor sin un asistente de
        configuración, que esta app no tiene."""
        fefo = self.env.ref("product_expiry.removal_fefo", raise_if_not_found=False)
        loc = wh.lot_stock_id if wh else None
        if fefo and loc and not loc.removal_strategy_id:
            loc.sudo().removal_strategy_id = fefo.id

    def _l10n_pe_ne_mover_stock_compra(self):
        """Entrada de mercadería por una compra: proveedor → existencias.

        Es la otra mitad del kardex. Sin esto solo hay salidas y todo negocio deriva a
        negativo: un inventario permanente es entradas MENOS salidas.

        Va aparte de _l10n_pe_ne_mover_stock y no reusa su dirección a propósito: aquella
        deduce el sentido de _l10n_pe_document_type(), que para un `in_invoice` devuelve '03'
        — o sea que trataría la compra como una boleta y SACARÍA el stock en vez de meterlo.
        """
        self.ensure_one()
        if self.move_type != "in_invoice":
            return self.env["stock.move"].browse()
        lineas = self._l10n_pe_ne_lineas_con_stock()
        if not lineas:
            return self.env["stock.move"].browse()
        wh = self.env["stock.warehouse"].search(
            [("company_id", "=", self.company_id.id)], limit=1
        )
        proveedores = self.env.ref(
            "stock.stock_location_suppliers", raise_if_not_found=False
        )
        if not wh or not proveedores:
            _logger.warning(
                "stock: sin almacén o ubicación de proveedores para %s; no entra mercadería",
                self.name,
            )
            return self.env["stock.move"].browse()
        # La mercadería que entra decide cómo saldrá: FEFO para que lo que vence antes se
        # venda primero (el default de Odoo, FIFO, dejaría caducar el lote más viejo).
        self._l10n_pe_ne_asegurar_fefo(wh)
        # con_lote: la entrada es la única que define el lote (la salida lo asigna Odoo).
        return self._l10n_pe_ne_stock_aplicar(
            lineas, proveedores, wh.lot_stock_id, con_lote=True
        )

    def _l10n_pe_ne_revertir_stock(self):
        """Deshace el movimiento de un comprobante que SUNAT rechazó.

        Un rechazado NO existe para SUNAT: hay que corregir y emitir uno NUEVO. Ese nuevo
        comprobante vuelve a descontar, así que si el rechazado se queda con su movimiento,
        el bien sale DOS VECES del kardex por una sola venta.

        Se REVIERTE, no se borra: el kardex es un libro: se compensa con el movimiento
        contrario y queda el rastro de que hubo un intento. Borrar el original escondería que
        pasó algo, que es justo lo que un inventario permanente no debe hacer.

        Idempotente: si ya se revirtió, no hace nada. Lo llama el write() al detectar la
        transición a 'rechazado' — por ahí pasan los tres caminos que la fijan (el envío
        síncrono, el cron de pendientes y el resumen diario de boletas), y también cualquiera
        que se agregue después.
        """
        self.ensure_one()
        Move = self.env["stock.move"]
        hechos = Move.search(
            [
                ("l10n_pe_ne_move_id", "=", self.id),
                ("l10n_pe_ne_reversa", "=", False),
                ("state", "=", "done"),
            ]
        )
        if not hechos:
            return Move.browse()
        ya = Move.search_count(
            [("l10n_pe_ne_move_id", "=", self.id), ("l10n_pe_ne_reversa", "=", True)]
        )
        if ya:
            return Move.browse()
        return self._l10n_pe_ne_mover_stock(reversa=True)

    def write(self, vals):
        """Revierte el stock al pasar a 'rechazado'.

        Va en el write y no en cada sitio que fija el estado porque son tres (envío síncrono,
        cron de pendientes, resumen diario de boletas) y mañana pueden ser cuatro: la
        invariante no debe depender de que alguien se acuerde de llamar al helper.

        Solo los que ENTRAN a rechazado (los que ya lo estaban no se re-revierten).
        """
        revertir = self.browse()
        if vals.get("l10n_pe_biller_state") == "rechazado":
            revertir = self.filtered(
                lambda m: m.l10n_pe_biller_state != "rechazado"
            )
        res = super().write(vals)
        for m in revertir:
            m._l10n_pe_ne_revertir_stock()
        return res

    def _l10n_pe_ne_quick_product(self, ln, tax=None, create=True, precio_con_igv=True):
        """Resuelve el product.product de una línea para que el documento USE un registro de Odoo:
        busca por id, por código (default_code) o por nombre exacto; si no existe y hay datos, lo
        CREA simplificado y lo enlaza (igual que el cliente por vat). Devuelve recordset vacío si la
        línea no aporta nada por lo que crear (queda como texto libre, compatible hacia atrás).
        Con create=False solo resuelve y NUNCA crea: las líneas de notas (07/08) pueden traer
        texto sintético (p. ej. "DICE: … DEBE DECIR: …" del motivo 03) que no debe convertirse
        en producto del catálogo.
        `precio_con_igv`: la convención del catálogo es list_price CON IGV (Productos, POS e
        import lo tratan como precio de vitrina). El payload de EMISIÓN trae el valor unitario
        SIN IGV (ni ISC): quick_emit pasa False y al crear se repone el impuesto — sin esto el
        producto auto-creado quedaba ~15% más barato al revenderlo desde el catálogo."""
        Product = self.env["product.product"]
        # `conceptoLibre`: el usuario dijo que esto NO es un producto, sino el detalle de un
        # servicio, distinto en cada comprobante ("POR EL SERVICIO DE TRANSPORTE LIMA-JULIACA …
        # DAM NRO. …"). No hay nada que resolver ni que crear, y se respeta al pie de la letra:
        # engancharlo a uno del catálogo que se llame igual movería su stock, que es justo lo
        # que el usuario dijo que no era. Uno por factura, además, volvería basura el catálogo.
        if ln.get("conceptoLibre"):
            return Product.browse()
        pid = ln.get("productId")
        if pid:
            prod = Product.browse(int(pid)).exists()
            if prod:
                return prod
        cod = (ln.get("productCod") or ln.get("codProducto") or "").strip()
        if cod:
            found = Product.search([("default_code", "=", cod)], limit=1)
            if found:
                return found
        desc = (ln.get("descripcion") or "").strip()
        if desc:
            found = Product.search([("name", "=", desc)], limit=1)
            if found:
                return found
        if not (cod or desc) or not create:
            return Product.browse()
        precio = float(ln.get("precioUnitario") or 0)
        if not precio_con_igv and tax and (tax.amount or 0) > 0:
            # Valor SIN IGV (ni ISC) del payload de emisión → precio de vitrina CON IGV.
            isc = float(ln.get("isc") or 0)
            precio = round(precio * (1 + isc / 100.0) * (1 + (tax.amount or 0) / 100.0), 4)
        uni = (ln.get("unidad") or "").strip()
        vals = {
            "name": desc or cod or "PRODUCTO",
            "type": self._l10n_pe_ne_tipo_producto(ln.get("tipo"), uni),
            "sale_ok": True,
            "list_price": precio,
            # is_storable va en False por defecto en Odoo: sin decirlo explícito, el producto
            # NO llevaría existencias y nunca movería stock. El auto-creado al emitir se queda
            # sin stock a propósito (nadie eligió); el catálogo lo manda por llevaStock.
            "is_storable": bool(ln.get("llevaStock")),
            "tracking": self._l10n_pe_ne_rastreo_producto(ln.get("rastreo")),
            # use_expiration_date lo agrega product_expiry; solo tiene sentido con rastreo.
            "use_expiration_date": bool(ln.get("vence")),
            "l10n_pe_ne_margen": float(ln.get("margen") or 0),
            # company_id del emisor: aísla el producto por RUC (igual que el cliente).
            "company_id": self.env.company.id,
        }
        # Costo: solo lo trae quien lo conoce (crear desde una línea de compra sabe cuánto se
        # pagó). Al emitir no viene, y ahí no se toca: el costo de venta no es el de compra.
        costo = float(ln.get("costo") or 0)
        if costo > 0:
            vals["standard_price"] = costo
        if cod:
            vals["default_code"] = cod
        bc = (ln.get("barcode") or "").strip()
        if bc:
            vals["barcode"] = bc
        cs = (ln.get("codSunat") or "").strip()
        if cs:
            vals["l10n_pe_ne_cod_producto_sunat"] = cs
        if ln.get("detraCod"):
            vals["l10n_pe_ne_detraccion_cod"] = str(ln["detraCod"]).strip()
        if ln.get("percepTasa"):
            vals["l10n_pe_ne_percepcion_tasa"] = _percep_float(ln["percepTasa"])
        if uni:
            vals["l10n_pe_ne_unit_code"] = uni
        if tax:
            vals["taxes_id"] = [(6, 0, tax.ids)]
        return Product.create(vals)

    def _l10n_pe_ne_product_dict(self, p):
        sale_taxes = p.taxes_id.filtered(lambda t: t.type_tax_use == "sale")
        # El ICBPER (7152) NO es la afectación: es un tributo aparte (bolsa plástica). Se
        # deriva como flag propio y se excluye al elegir la afectación (IGV) del producto.
        icbper = bool(sale_taxes.filtered(lambda t: t.l10n_pe_edi_tax_code == "7152"))
        tax = sale_taxes.filtered(lambda t: t.l10n_pe_edi_tax_code != "7152")[:1]
        return {
            "id": p.id,
            "descripcion": p.name or "",
            "codigo": p.default_code or "",
            "barcode": p.barcode or "",
            "codSunat": p.l10n_pe_ne_cod_producto_sunat or "",
            "detraCod": p.l10n_pe_ne_detraccion_cod or "",
            "percepTasa": p.l10n_pe_ne_percepcion_tasa or 0.0,
            "precio": p.list_price,
            "taxCode": (tax.l10n_pe_edi_tax_code or "1000") if tax else "1000",
            "unidad": p.l10n_pe_ne_unit_code or "",
            "icbper": icbper,
            # Fraccionamiento (farma): el producto se puede vender por sub-unidad. El front muestra
            # el toggle "fraccionar" solo cuando `fraccionable`; `unidadFraccion` es la sub-unidad SUNAT.
            "fraccionable": (p.l10n_pe_ne_unidades_por_empaque or 0.0) > 0,
            "unidadesPorEmpaque": p.l10n_pe_ne_unidades_por_empaque or 0.0,
            "unidadFraccion": p.l10n_pe_ne_unidad_fraccion or "",
            "registroSanitario": p.l10n_pe_ne_registro_sanitario or "",
            "controlado": bool(p.l10n_pe_ne_controlado),
            # "bien" | "servicio" — el vocabulario del negocio, no el de Odoo (consu/service).
            # 'combo' no lo usa esta app; si apareciera, se trata como bien (es tangible).
            "tipo": "servicio" if p.type == "service" else "bien",
            # ¿Se le llevan existencias? (Odoo: is_storable). Va en False por defecto, así que
            # SIN esto ningún producto movería stock nunca: es lo que activa _l10n_pe_ne_mover_stock.
            "llevaStock": bool(p.is_storable),
            # Existencias actuales. Solo tiene sentido si llevaStock; si no, va en 0 y la UI
            # muestra un guion (no es "cero unidades", es "no aplica").
            "stock": p.qty_available if p.is_storable else 0.0,
            # Costo (con IGV) y margen: lo que hace falta para proponer el precio de venta
            # cuando una compra trae un costo distinto.
            "costo": p.standard_price or 0.0,
            "margen": p.l10n_pe_ne_margen or 0.0,
            # Rastreo por lote o serie (Odoo: tracking). "lote" agrupa unidades (farmacia,
            # alimentos); "serie" es un número por unidad (celulares, equipos).
            "rastreo": {"lot": "lote", "serial": "serie"}.get(p.tracking, "ninguno"),
            # ¿Los lotes llevan vencimiento? Solo aplica con rastreo por lote/serie.
            "vence": bool(p.use_expiration_date),
        }

    def _l10n_pe_ne_partner_dict(self, p):
        return {
            "id": p.id,
            "razonSocial": p.name or "",
            "numDoc": p.vat or "",
            "tipoDoc": p.l10n_latam_identification_type_id.l10n_pe_vat_code or "",
            "tipoDocNombre": p.l10n_latam_identification_type_id.name or "",
            "email": p.email or "",
            "telefono": p.phone or "",
            "direccion": p.street or "",
            "pais": p.country_id.code or "",
            "exceptuadoPercepcion": p.l10n_pe_ne_exceptuado_percepcion,
            "parteVinculada": p.l10n_pe_ne_parte_vinculada,
            "tipoVinculo": p.l10n_pe_ne_tipo_vinculo or "",
            "noDomiciliada": p.l10n_pe_ne_no_domiciliada,
        }

    def _l10n_pe_ne_ident_type(self, tipoDoc):
        return self.env["l10n_latam.identification.type"].search(
            [("l10n_pe_vat_code", "=", tipoDoc or "6")], limit=1
        )

    def _l10n_pe_ne_partner_apply(self, p, c):
        """Aplica los campos simplificados (los del caso común de facturación) a un res.partner."""
        vals = {}
        if c.get("razonSocial"):
            vals["name"] = c["razonSocial"]
        if c.get("numDoc") is not None:
            vals["vat"] = (c.get("numDoc") or "").strip() or False
        if c.get("tipoDoc"):
            t = self._l10n_pe_ne_ident_type(c["tipoDoc"])
            if t:
                vals["l10n_latam_identification_type_id"] = t.id
        # País del adquirente (exportación / no domiciliado): ISO 3166 alpha-2 = res.country.code.
        # Alimenta codPaisCliente en la cabecera 0200. "" limpia el país.
        if "pais" in c:
            code = (c.get("pais") or "").strip().upper()
            country = self.env["res.country"].search([("code", "=", code)], limit=1) if code else False
            vals["country_id"] = country.id if country else False
        for key, field in (
            ("email", "email"),
            ("telefono", "phone"),
            ("direccion", "street"),
            ("exceptuadoPercepcion", "l10n_pe_ne_exceptuado_percepcion"),
            ("parteVinculada", "l10n_pe_ne_parte_vinculada"),
            ("tipoVinculo", "l10n_pe_ne_tipo_vinculo"),
        ):
            if key in c:
                vals[field] = c.get(key) or False
        if vals:
            p.write(vals)
        return p

    @api.model
    def l10n_pe_ne_list_clientes(self, query=None, limit=50, offset=None):
        """Clientes de Odoo para que React liste/autocomplete (no reinventa el padrón).

        Paginación opt-in: con `offset` (aunque sea 0) devuelve el envelope
        {items, total}; sin `offset` (None) devuelve la lista plana de siempre
        —así el autocomplete del POS/Emitir sigue recibiendo un array."""
        domain = [("customer_rank", ">", 0)]
        if query:
            domain = [
                "&",
                ("customer_rank", ">", 0),
                "|",
                ("name", "ilike", query),
                ("vat", "ilike", query),
            ]
        Partner = self.env["res.partner"]
        parts = Partner.search(domain, order="name", limit=limit, offset=offset or 0)
        items = [self._l10n_pe_ne_partner_dict(p) for p in parts]
        if offset is None:
            return items
        return {"items": items, "total": Partner.search_count(domain)}

    @api.model
    def l10n_pe_ne_create_cliente(self, cliente):
        """Crea (o reusa por vat) un cliente con los campos PE correctos; lo guarda EN Odoo."""
        cliente = cliente or {}
        p = self._l10n_pe_ne_quick_partner(cliente)
        self._l10n_pe_ne_partner_apply(p, cliente)
        if not p.customer_rank:
            p.customer_rank = 1
        return self._l10n_pe_ne_partner_dict(p)

    @api.model
    def l10n_pe_ne_update_cliente(self, cliente):
        """Actualiza un cliente existente (por id) con los campos simplificados."""
        cliente = cliente or {}
        p = self.env["res.partner"].browse(int(cliente.get("id") or 0)).exists()
        if not p:
            raise UserError(_("Cliente no encontrado."))
        self._l10n_pe_ne_partner_apply(p, cliente)
        return self._l10n_pe_ne_partner_dict(p)

    @api.model
    def l10n_pe_ne_delete_cliente(self, rec_id):
        """Elimina el cliente; si está referenciado (comprobantes), lo archiva en su lugar."""
        p = self.env["res.partner"].browse(int(rec_id or 0)).exists()
        if not p:
            return {"ok": True, "modo": "inexistente"}
        try:
            p.unlink()
            return {"ok": True, "modo": "eliminado"}
        except Exception:
            p.active = False
            return {"ok": True, "modo": "archivado"}

    @api.model
    def l10n_pe_ne_list_productos(self, query=None, limit=50, offset=None):
        """Productos de Odoo para que React liste/autocomplete y los documentos los referencien.
        Busca por nombre, código interno (default_code) o código de barras (barcode).

        Paginación opt-in: con `offset` devuelve {items, total}; sin él, lista plana."""
        domain = [("sale_ok", "=", True)]
        if query:
            domain = [
                "&",
                ("sale_ok", "=", True),
                "|",
                "|",
                ("name", "ilike", query),
                ("default_code", "ilike", query),
                ("barcode", "ilike", query),
            ]
        Product = self.env["product.product"]
        prods = Product.search(domain, order="name", limit=limit, offset=offset or 0)
        items = [self._l10n_pe_ne_product_dict(p) for p in prods]
        if offset is None:
            return items
        return {"items": items, "total": Product.search_count(domain)}

    @api.model
    def l10n_pe_ne_producto_por_barcode(self, code):
        """Resuelve UN producto por código de barras exacto (para el escaneo en el POS).
        Devuelve el dict del producto o None si no hay coincidencia. Aislado por compañía."""
        code = (code or "").strip()
        if not code:
            return None
        p = self.env["product.product"].search(
            [("sale_ok", "=", True), ("barcode", "=", code)], limit=1
        )
        return self._l10n_pe_ne_product_dict(p) if p else None

    @api.model
    def l10n_pe_ne_create_producto(self, producto):
        """Crea (o reusa por código/nombre) un producto simplificado; lo guarda EN Odoo."""
        _logger.info("l10n_pe_ne_create_producto: %s", producto)
        producto = producto or {}
        desc = producto.get("descripcion") or producto.get("nombre")
        _logger.info("desc: %s", desc)
        if not desc and not producto.get("codigo"):
            raise UserError(
                _("El producto necesita al menos una descripción o un código.")
            )
        tax = self._l10n_pe_ne_tax_by_code(producto.get("taxCode") or "1000")
        _logger.info("tax: %s", tax)
        p = self._l10n_pe_ne_quick_product(
            {
                "descripcion": desc,
                "productCod": producto.get("codigo"),
                "barcode": producto.get("barcode"),
                "codSunat": producto.get("codSunat"),
                "detraCod": producto.get("detraCod"),
                "percepTasa": producto.get("percepTasa"),
                "precioUnitario": producto.get("precio"),
                "unidad": producto.get("unidad"),
                "tipo": producto.get("tipo"),
                "llevaStock": producto.get("llevaStock"),
                "rastreo": producto.get("rastreo"),
                "vence": producto.get("vence"),
                "margen": producto.get("margen"),
                "costo": producto.get("costo"),
            },
            tax,
        )
        _logger.info("p: %s", p)
        return self._l10n_pe_ne_product_dict(p)

    @api.model
    def l10n_pe_ne_update_producto(self, producto):
        """Actualiza un producto (por id): descripción, código, precio e impuesto (afectación)."""
        producto = producto or {}
        p = self.env["product.product"].browse(int(producto.get("id") or 0)).exists()
        if not p:
            raise UserError(_("Producto no encontrado."))
        vals = {}
        if producto.get("descripcion"):
            vals["name"] = producto["descripcion"]
        if "codigo" in producto:
            vals["default_code"] = (producto.get("codigo") or "").strip() or False
        if "barcode" in producto:
            vals["barcode"] = (producto.get("barcode") or "").strip() or False
        if "codSunat" in producto:
            vals["l10n_pe_ne_cod_producto_sunat"] = (producto.get("codSunat") or "").strip() or False
        if "detraCod" in producto:
            vals["l10n_pe_ne_detraccion_cod"] = (producto.get("detraCod") or "").strip() or False
        if "registroSanitario" in producto:
            vals["l10n_pe_ne_registro_sanitario"] = (producto.get("registroSanitario") or "").strip() or False
        if "controlado" in producto:
            vals["l10n_pe_ne_controlado"] = bool(producto.get("controlado"))
        if producto.get("percepTasa") is not None:
            vals["l10n_pe_ne_percepcion_tasa"] = _percep_float(producto.get("percepTasa"))
        if "unidad" in producto:
            vals["l10n_pe_ne_unit_code"] = (producto.get("unidad") or "").strip() or False
        if producto.get("tipo"):
            # Solo si viene explícito: aquí NO se deduce de la unidad. Cambiar la unidad de un
            # producto ya clasificado no debe reclasificarlo a su espalda.
            vals["type"] = self._l10n_pe_ne_tipo_producto(producto["tipo"])
        if "llevaStock" in producto:
            vals["is_storable"] = bool(producto.get("llevaStock"))
        if "rastreo" in producto:
            vals["tracking"] = self._l10n_pe_ne_rastreo_producto(producto.get("rastreo"))
        if producto.get("margen") is not None:
            vals["l10n_pe_ne_margen"] = float(producto.get("margen") or 0)
        if producto.get("costo") is not None:
            vals["standard_price"] = float(producto.get("costo") or 0)
        if "vence" in producto:
            vals["use_expiration_date"] = bool(producto.get("vence"))
        if producto.get("precio") is not None:
            vals["list_price"] = float(producto.get("precio") or 0)
        if producto.get("taxCode"):
            tax = self._l10n_pe_ne_tax_by_code(producto["taxCode"])
            vals["taxes_id"] = [(6, 0, tax.ids if tax else [])]
        if vals:
            p.write(vals)
        return self._l10n_pe_ne_product_dict(p)

    @api.model
    def l10n_pe_ne_delete_producto(self, rec_id):
        """Elimina el producto; si está referenciado (en comprobantes), lo archiva en su lugar."""
        p = self.env["product.product"].browse(int(rec_id or 0)).exists()
        if not p:
            return {"ok": True, "modo": "inexistente"}
        try:
            p.unlink()
            return {"ok": True, "modo": "eliminado"}
        except Exception:
            p.active = False
            return {"ok": True, "modo": "archivado"}

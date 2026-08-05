# -*- coding: utf-8 -*-
"""account.move — Negocio/config: config, países, series, datos del negocio, resumen.
Extraído de account_move_biller.py (refactor sin cambio de comportamiento)."""
import base64

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = "account.move"

    @api.model
    def l10n_pe_ne_config(self):
        """Parámetros que React debe leer DESDE Odoo (no hardcodear): tasa IGV y monto ICBPER por unidad."""
        cfg = {
            "igv": 18.0,
            "icbperRate": self._l10n_pe_ne_ensure_icbper_tax().amount,
            "agentePercepcion": bool(self.env.company.l10n_pe_ne_agente_percepcion),
            # Redondeo de efectivo: el POS lo aplica en vivo con estos parámetros (ver lib/redondeo.ts).
            "redondeoActivo": bool(self.env.company.l10n_pe_ne_redondeo_activo),
            "redondeoModo": self.env.company.l10n_pe_ne_redondeo_modo or "favor",
            # C1: con la auto-apertura activa, la falta de caja abierta YA NO impide cobrar (el
            # backend abre el turno al emitir). El POS lo necesita para no seguir deshabilitando
            # el botón por una condición que dejó de ser un bloqueo: si la pantalla mantuviera
            # su propia regla, la función aprobada sería inalcanzable justo desde el mostrador.
            # Apagado, el POS vuelve a exigir caja —sin auto-apertura la venta quedaría huérfana,
            # y ahí bloquear sí es lo honesto.
            "cajaAutoapertura": bool(self.env.company.l10n_pe_ne_caja_autoapertura),
        }
        # Capa 1 (rubro → módulos): igual que en el perfil, la clave solo viaja con rubro
        # configurado — Emitir/POS gatean los regímenes con esto (ausente = sin gating).
        # El admin de plataforma queda fuera (ve/opera todo, doctrina del menú por rol).
        if not (self.env.user.has_group("base.group_system")
                or self.env.user.has_group("base.group_erp_manager")):
            efectivos = self.env.company.l10n_pe_ne_modulos_efectivos()
            if efectivos is not None:
                cfg["modulos"] = sorted(efectivos)
        return cfg

    @api.model
    def l10n_pe_ne_paises(self):
        """Catálogo de países (ISO 3166 alpha-2) para el selector del cliente extranjero en la
        factura de exportación. Perú primero (default habitual) y el resto por nombre."""
        paises = self.env["res.country"].search([("code", "!=", False)], order="name")
        return [{"code": c.code, "name": c.name} for c in paises]

    @api.model
    def l10n_pe_ne_series(self, limit=None, offset=None):
        """Series del emisor: las DECLARADAS en el registro por local (l10n_pe_ne.serie,
        `origen: 'config'`) unidas a las realmente EN USO, agregadas desde los comprobantes
        emitidos (`origen: 'uso'`). Por serie: tipo, cuántos emitidos, último correlativo y el
        próximo a emitir. Incluye las series de retención/percepción (account.payment). Aislado
        por RUC vía el contexto de compañía.

        El contrato es ADITIVO: las cinco claves de siempre (serie/tipoDoc/tipo/emitidos/
        ultimo/proximo) y su paginación opt-in no cambian, y con el registro vacío la respuesta
        es exactamente la de antes —todas las filas con `origen: 'uso'`—."""
        TIPO = {
            "01": "Factura",
            "03": "Boleta",
            "07": "Nota de crédito",
            "08": "Nota de débito",
            "20": "Retención",
            "40": "Percepción",
        }
        agg = {}

        def add(serie, tipo, corr):
            # Solo cuenta CPE realmente emitidos: con correlativo asignado (n>=1). Un
            # account.payment lleva R001 y P001 por defecto, pero solo se emite uno; el
            # otro queda 'por_enviar' con correlativo vacío y no debe contarse.
            n = int(corr) if (corr or "").strip().isdigit() else 0
            if not serie or n < 1:
                return
            cur = agg.setdefault(
                serie, {"serie": serie, "tipoDoc": tipo, "emitidos": 0, "ultimo": 0}
            )
            cur["emitidos"] += 1
            if n > cur["ultimo"]:
                cur["ultimo"] = n

        for m in self.search([("l10n_pe_ne_serie_emit", "!=", False)]):
            add(
                m.l10n_pe_ne_serie_emit,
                m.l10n_pe_ne_tipo_doc or m._l10n_pe_document_type(),
                m.l10n_pe_ne_corr_emit,
            )
        for p in self.env["account.payment"].search(
            [("company_id", "=", self.env.company.id)]
        ):
            add(p.l10n_pe_ret_serie, "20", p.l10n_pe_ret_correlativo)
            add(p.l10n_pe_per_serie, "40", p.l10n_pe_per_correlativo)

        # Registro por local: se fusiona por CÓDIGO con el agregado de uso, así una serie
        # declarada que ya emitió sale una sola vez y con sus contadores reales, y una recién
        # declarada aparece con 0 emitidos y su próximo en 00000001.
        registro = {
            s.codigo: s
            for s in self.env["l10n_pe_ne.serie"].sudo().search(
                [("company_id", "=", self.env.company.id)]
            )
        }
        for codigo, s in registro.items():
            agg.setdefault(
                codigo,
                {"serie": codigo, "tipoDoc": s.tipo_doc, "emitidos": 0, "ultimo": 0},
            )

        filas = []
        for s in sorted(agg.values(), key=lambda x: x["serie"]):
            reg = registro.get(s["serie"])
            estab = reg.establecimiento_id if reg else None
            filas.append({
                "serie": s["serie"],
                "tipoDoc": s["tipoDoc"],
                "tipo": TIPO.get(s["tipoDoc"], s["tipoDoc"]),
                "emitidos": s["emitidos"],
                "ultimo": str(s["ultimo"]).zfill(8) if s["ultimo"] else "—",
                "proximo": str(s["ultimo"] + 1).zfill(8),
                # --- aditivo (registro por local). Una fila 'uso' no sabe de qué local es:
                # nadie lo declaró, así que se dice null en vez de inventar '0000'.
                "id": reg.id if reg else None,
                "origen": "config" if reg else "uso",
                "establecimiento": ((estab.codigo or "0000") if estab else "0000") if reg else None,
                "establecimientoId": (estab.id or None) if reg else None,
                "establecimientoDireccion": (estab.direccion or "") if reg else "",
                "activa": reg.activa if reg else True,
                "predeterminada": reg.predeterminada if reg else False,
            })
        # Paginación opt-in sobre el agregado ya construido (no hay search directo).
        if offset is None:
            return filas
        return {"items": filas[offset:offset + limit] if limit else filas[offset:],
                "total": len(filas)}

    # ============================================================ datos negocio
    @api.model
    def l10n_pe_ne_negocio(self):
        """Datos del emisor (negocio) que alimentan el bloque `emisor` del XML, leídos desde
        res.company + su partner. El RUC es de solo lectura (identidad del emisor, indexa el
        certificado de firma en el servidor)."""
        company = self.env.company
        p = company.partner_id
        d = p.l10n_pe_district
        return {
            "ruc": p.vat or "",
            "razonSocial": company.name or "",
            "direccion": p.street or "",
            "urbanizacion": p.street2 or "",
            "telefono": p.phone or "",
            "email": p.email or "",
            "distritoId": d.id if d else None,
            "distrito": d.name if d else "",
            "ubigeo": d.code if d else "",
            "provincia": (d.city_id.name if d and d.city_id else (p.city or "")),
            "departamento": p.state_id.name or "",
            "datosPago": company.l10n_pe_ne_datos_pago or "",
            "hasLogo": bool(company.logo),
            "agentePercepcion": bool(company.l10n_pe_ne_agente_percepcion),
            "redondeoActivo": bool(company.l10n_pe_ne_redondeo_activo),
            "redondeoModo": company.l10n_pe_ne_redondeo_modo or "favor",
            # C1: auto-apertura de caja al cobrar (ver res_company). Se expone donde el usuario
            # la puede cambiar — un parámetro de negocio no se hardcodea ni se esconde en el ORM.
            "cajaAutoapertura": bool(company.l10n_pe_ne_caja_autoapertura),
            # C2: desde qué diferencia el cierre de caja exige una explicación escrita. Mismo
            # motivo: la tolerancia de una bodega no es la de un local que factura S/ 50 000 al
            # día, y quien lo sabe es el dueño, no el código.
            "toleranciaDescuadre": round(company.l10n_pe_ne_cierre_tolerancia or 0.0, 2),
            # C3: si en este negocio los gastos salen del cajón por defecto. Lo lee el formulario
            # de gastos para precargar la casilla: no es una regla del sistema, es cómo paga este
            # negocio, y eso solo lo sabe el dueño.
            "gastoDeCaja": bool(company.l10n_pe_ne_gasto_de_caja),
        }

    def l10n_pe_ne_get_logo(self):
        """(bytes, content_type) del logo del emisor para servirlo por HTTP, o (None, None)."""
        logo = self.env.company.logo
        if not logo:
            return None, None
        raw = base64.b64decode(logo)
        ct = (
            "image/png" if raw[:4] == b"\x89PNG"
            else "image/jpeg" if raw[:2] == b"\xff\xd8"
            else "application/octet-stream"
        )
        return raw, ct

    def _l10n_pe_ne_set_logo(self, company, logo_b64):
        """Valida y guarda el logo del emisor. Vacío/None → lo quita. Acepta data-URI o base64
        pelado. Exige PNG/JPEG y ≤ ~1.4 MB (mismo tope que valida biller-pdf al imprimir)."""
        if not logo_b64:
            company.logo = False
            return
        if isinstance(logo_b64, str) and logo_b64.startswith("data:"):
            logo_b64 = logo_b64.split(",", 1)[-1]
        try:
            raw = base64.b64decode(logo_b64, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise UserError(_("El logo no es una imagen válida.")) from exc
        if len(raw) > 1_400_000:
            raise UserError(_("El logo es demasiado grande (máx. ~1.4 MB)."))
        if not (raw[:4] == b"\x89PNG" or raw[:2] == b"\xff\xd8"):
            raise UserError(_("El logo debe ser PNG o JPEG."))
        company.logo = base64.b64encode(raw)

    @api.model
    def l10n_pe_ne_buscar_distrito(self, q=None, limit=20):
        """Busca distritos (ubigeo) por nombre, código, provincia o departamento — así el
        selector llena el ubigeo automáticamente sin tipear los 6 dígitos (escribes 'Miraflores'
        o 'Arequipa' y sale el distrito con su código)."""
        q = (q or "").strip()
        dom = (["|", "|", "|",
                ("name", "ilike", q), ("code", "ilike", q),
                ("city_id.name", "ilike", q), ("city_id.state_id.name", "ilike", q)]
               if q else [])
        recs = self.env["l10n_pe.res.city.district"].search(dom, limit=limit)
        return [
            {
                "id": r.id,
                "code": r.code or "",
                "name": r.name or "",
                "provincia": r.city_id.name or "",
                "departamento": r.city_id.state_id.name or "",
            }
            for r in recs
        ]

    @api.model
    def l10n_pe_ne_update_negocio(self, vals):
        """Actualiza los datos editables del emisor (razón social, dirección, contacto y
        distrito). El RUC nunca se toca. Al fijar un distrito se sincronizan también provincia
        (city) y departamento (state) para que el bloque `emisor` quede consistente. Los cambios
        fluyen al PRÓXIMO XML emitido vía _l10n_pe_emisor."""
        # env.company lo fija el servidor desde el usuario (with_company), así que estas
        # escrituras SIEMPRE recaen sobre la empresa del propio emisor. res.company solo es
        # escribible por "Access Rights" (que el emisor no tiene); usamos sudo acotado a su
        # propia empresa para no exigirle ese rol global.
        company = self.env.company.sudo()
        p = company.partner_id
        razon = (vals.get("razonSocial") or "").strip()
        if "razonSocial" in vals and razon:
            company.name = razon
        pvals = {}
        for key, field in (
            ("direccion", "street"),
            ("urbanizacion", "street2"),
            ("telefono", "phone"),
            ("email", "email"),
        ):
            if key in vals:
                pvals[field] = (vals.get(key) or "").strip() or False
        did = vals.get("distritoId")
        if did:
            d = self.env["l10n_pe.res.city.district"].sudo().browse(int(did)).exists()
            if d:
                pvals["l10n_pe_district"] = d.id
                if d.city_id:
                    pvals["city"] = d.city_id.name
                    if d.city_id.state_id:
                        pvals["state_id"] = d.city_id.state_id.id
                    if d.city_id.country_id:
                        pvals["country_id"] = d.city_id.country_id.id
        if pvals:
            p.write(pvals)
        if "datosPago" in vals:
            company.l10n_pe_ne_datos_pago = (vals.get("datosPago") or "").strip() or False
        if "agentePercepcion" in vals:
            company.l10n_pe_ne_agente_percepcion = bool(vals.get("agentePercepcion"))
        if "redondeoActivo" in vals:
            company.l10n_pe_ne_redondeo_activo = bool(vals.get("redondeoActivo"))
        if vals.get("redondeoModo") in ("favor", "cercano"):
            company.l10n_pe_ne_redondeo_modo = vals["redondeoModo"]
        if "cajaAutoapertura" in vals:
            company.l10n_pe_ne_caja_autoapertura = bool(vals.get("cajaAutoapertura"))
        if "gastoDeCaja" in vals:
            company.l10n_pe_ne_gasto_de_caja = bool(vals.get("gastoDeCaja"))
        # C2: tolerancia de descuadre al cerrar caja. El vacío NO es 0: es "no lo toques" —el
        # formulario del negocio manda todos sus campos en cada guardado, y un input que quedó
        # en blanco por una recarga a medias no puede dejar al RUC en tolerancia cero (que
        # obligaría a justificar hasta el céntimo). Para tolerancia cero se escribe 0.
        tol_raw = vals.get("toleranciaDescuadre")
        if tol_raw is not None and str(tol_raw).strip() != "":
            try:
                tol = float(tol_raw)
            except (TypeError, ValueError):
                raise UserError(_("La tolerancia de descuadre debe ser un monto válido."))
            if tol < 0:
                raise UserError(_("La tolerancia de descuadre no puede ser negativa."))
            company.l10n_pe_ne_cierre_tolerancia = round(tol, 2)
        if "logo" in vals:
            self._l10n_pe_ne_set_logo(company, vals.get("logo"))
        return self.l10n_pe_ne_negocio()

    # ============================================================ resumen estado
    @api.model
    def l10n_pe_ne_resumen(self):
        """Resumen de estado del emisor, calculado en Odoo (no en React): actividad emitida
        hoy y en el mes en curso —separando PEN/USD para no mezclar monedas— y el desglose por
        estado SUNAT de todos los comprobantes de venta. Aislado por RUC vía la compañía."""
        today = fields.Date.context_today(self)
        mes0 = today.replace(day=1)
        sales = [
            ("move_type", "in", ("out_invoice", "out_refund")),
            ("company_id", "=", self.env.company.id),
        ]
        emitidos = sales + [("l10n_pe_biller_state", "in", ("enviado", "anulado"))]

        def bucket(moves):
            pen = usd = 0.0
            for m in moves:
                if (m.currency_id.name or "PEN") == "USD":
                    usd += m.amount_total or 0.0
                else:
                    pen += m.amount_total or 0.0
            return {"count": len(moves), "pen": round(pen, 2), "usd": round(usd, 2)}

        hoy = self.search(emitidos + [("invoice_date", "=", today)])
        mes = self.search(
            emitidos + [("invoice_date", ">=", mes0), ("invoice_date", "<=", today)]
        )

        # Desglose por estado SUNAT (toda la historia de ventas de la compañía).
        estados = {
            "aceptado": 0,
            "anulado": 0,
            "rechazado": 0,
            "pendiente": 0,
            "error": 0,
        }
        MAP = {
            "enviado": "aceptado",
            "anulado": "anulado",
            "rechazado": "rechazado",
            "por_enviar": "pendiente",
            "error": "error",
        }
        for m in self.search(sales):
            k = MAP.get(m.l10n_pe_biller_state)
            if k:
                estados[k] += 1

        return {
            "hoy": bucket(hoy),
            "mes": dict(bucket(mes), periodo=today.strftime("%Y%m")),
            "estados": estados,
            "porAtender": estados["rechazado"] + estados["error"],
        }



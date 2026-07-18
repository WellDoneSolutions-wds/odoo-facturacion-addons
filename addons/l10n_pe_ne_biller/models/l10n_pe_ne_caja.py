# -*- coding: utf-8 -*-
"""Caja (NE Express) — apertura/cierre/arqueo por medio de pago, estilo POS/bodega.

Dos modelos propios de Odoo: TODA la lógica (CRUD + serialización + amarre de ventas)
vive en el addon; React solo llama. Aislado por compañía (reglas multi-compañía en
security). La aritmética del arqueo se delega a tools/caja_arqueo.py (puro, testeado sin
Odoo). La caja NUNCA bloquea una venta (modo informativo, coherente con stock v1)."""
import calendar

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools.caja_arqueo import agrupar_ventas, calcular_arqueo, EFECTIVO


class L10nPeNeCajaSesion(models.Model):
    _name = "l10n_pe_ne.caja.sesion"
    _description = "Sesión de caja (NE Express)"
    _order = "fecha_apertura desc, id desc"

    estado = fields.Selection(
        [("abierta", "Abierta"), ("cerrada", "Cerrada")],
        default="abierta", required=True, index=True,
    )
    fecha_apertura = fields.Datetime(required=True, default=fields.Datetime.now)
    fecha_cierre = fields.Datetime()
    usuario_apertura_id = fields.Many2one("res.users", required=True, default=lambda s: s.env.user)
    usuario_cierre_id = fields.Many2one("res.users")
    saldo_inicial = fields.Monetary(currency_field="currency_id")  # >= 0, validado en abrir
    nota_apertura = fields.Char()
    nota_cierre = fields.Char()
    # snapshots congelados al cierre:
    conteos_cierre = fields.Json()   # [{'medio','esperado','contado','diferencia'}]
    ventas_cierre = fields.Json()    # {'count','total','sinMedio','countUsd','totalUsd'}
    movimiento_ids = fields.One2many("l10n_pe_ne.caja.movimiento", "sesion_id")
    currency_id = fields.Many2one("res.currency", required=True,
                                  default=lambda s: s.env.company.currency_id)
    company_id = fields.Many2one("res.company", required=True, index=True,
                                 default=lambda s: s.env.company)

    def init(self):
        # Índice único parcial: imposibilita la carrera de doble apertura simultánea
        # (una sola sesión 'abierta' por compañía). La guarda amigable vive en el método
        # l10n_pe_ne_abrir_caja; este índice es la defensa de última línea (race).
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS l10n_pe_ne_caja_sesion_unica_abierta
            ON l10n_pe_ne_caja_sesion (company_id) WHERE estado = 'abierta'
        """)

    # -------------------------------------------------------- helpers privados
    def _l10n_pe_ne_fmt_dt(self, dt):
        """Datetime -> 'YYYY-MM-DD HH:mm' en hora local del usuario (America/Lima)."""
        if not dt:
            return ""
        return fields.Datetime.context_timestamp(self, dt).strftime("%Y-%m-%d %H:%M")

    def _l10n_pe_ne_ventas_sesion(self):
        """account.move amarrados a la sesión (ventana por create_date).

        La caja refleja DINERO FÍSICO: la venta cuenta desde el COBRO, aunque la emisión
        async siga en cola (por_enviar/en_proceso) — antes se exigía 'enviado' (CDR de
        SUNAT aplicado por el cron) y el cajero no veía su venta por ~1 minuto, con
        riesgo de cerrar caja descuadrada. Si la emisión falla en definitiva
        (rechazado/error/anulado), la venta sale del esperado en vivo: el cajero debe
        re-emitirla (la nueva sí cuenta) o el descuadre aflora en el cierre."""
        self.ensure_one()
        return self.env["account.move"].search([
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("l10n_pe_biller_state", "not in", ("rechazado", "error", "anulado")),
            ("company_id", "=", self.company_id.id),
            ("create_date", ">=", self.fecha_apertura),
            ("create_date", "<=", self.fecha_cierre or fields.Datetime.now()),
        ])

    def _l10n_pe_ne_ventas_planas(self):
        """Ventas de la sesión como dicts planos para tools.caja_arqueo.agrupar_ventas.
        El total es lo COBRADO: amount_total + redondeo de caja (QW04). Se lee redondeo con
        getattr por robustez (en el stack QW06 el campo existe; ver Decisión 2)."""
        self.ensure_one()
        out = []
        for m in self._l10n_pe_ne_ventas_sesion():
            total = (m.amount_total or 0.0) + (getattr(m, "l10n_pe_ne_redondeo", 0.0) or 0.0)
            out.append({
                "total": total,
                "moneda": m.currency_id.name or "PEN",
                "formaPago": m.l10n_pe_ne_forma_pago or "Contado",
                "medios": m.l10n_pe_ne_medios_pago or [],
            })
        return out

    def _l10n_pe_ne_ingresos_retiros(self):
        self.ensure_one()
        ingresos = sum(mv.monto for mv in self.movimiento_ids if mv.tipo == "ingreso")
        retiros = sum(mv.monto for mv in self.movimiento_ids if mv.tipo == "retiro")
        return round(ingresos, 2), round(retiros, 2)

    def _l10n_pe_ne_por_medio_arqueo(self, agr):
        """Por-medio que alimenta el esperado del arqueo. Seam de extensión: por defecto es el
        por-medio de las ventas; el addon de roles (CN-02) le SUMA los adelantos 'a cuenta' por su
        medio, para que el prepago físico del cliente cuadre el arqueo sin mezclarse con el fondo
        propio (que iría por ingresos genéricos, solo Efectivo). Devuelve una copia (no muta agr)."""
        self.ensure_one()
        return dict(agr.get("porMedio") or {})

    def _l10n_pe_ne_movimientos_dicts(self):
        self.ensure_one()
        return [{
            "id": mv.id,
            "tipo": mv.tipo,
            "motivo": mv.motivo or "",
            "monto": mv.monto or 0.0,
            "fecha": self._l10n_pe_ne_fmt_dt(mv.fecha),
            "usuario": mv.usuario_id.name or "",
        } for mv in self.movimiento_ids]

    def _l10n_pe_ne_sesion_dict(self):
        """Sesión con movimientos + esperado EN VIVO por medio (contrato GET /ne/api/caja)."""
        self.ensure_one()
        agr = agrupar_ventas(self._l10n_pe_ne_ventas_planas())
        ingresos, retiros = self._l10n_pe_ne_ingresos_retiros()
        filas, esperado_total, _c, _d = calcular_arqueo(
            self.saldo_inicial, self._l10n_pe_ne_por_medio_arqueo(agr), ingresos, retiros, None)
        return {
            "id": self.id,
            "estado": self.estado,
            "fechaApertura": self._l10n_pe_ne_fmt_dt(self.fecha_apertura),
            "fechaCierre": self._l10n_pe_ne_fmt_dt(self.fecha_cierre),
            "usuarioApertura": self.usuario_apertura_id.name or "",
            "usuarioCierre": self.usuario_cierre_id.name or "",
            "saldoInicial": self.saldo_inicial or 0.0,
            "notaApertura": self.nota_apertura or "",
            "notaCierre": self.nota_cierre or "",
            "moneda": self.currency_id.name or "PEN",
            "movimientos": self._l10n_pe_ne_movimientos_dicts(),
            "ingresos": ingresos,
            "retiros": retiros,
            "ventas": {"count": agr["count"], "total": agr["total"], "sinMedio": agr["sinMedio"],
                       "countUsd": agr["countUsd"], "totalUsd": agr["totalUsd"]},
            "esperado": [{"medio": f["medio"], "monto": f["esperado"]} for f in filas],
            "esperadoTotal": esperado_total,
        }

    def _l10n_pe_ne_arqueo_dict(self):
        """Contrato GET /ne/api/caja/<id>/arqueo. Cerrada -> snapshots congelados; abierta ->
        cálculo en vivo con arqueo parcial (contado/diferencia = null)."""
        self.ensure_one()
        base = self._l10n_pe_ne_sesion_dict()
        if self.estado == "cerrada" and self.conteos_cierre is not None:
            arqueo = self.conteos_cierre or []
            ventas = self.ventas_cierre or base["ventas"]
            esperado_total = round(sum(f.get("esperado") or 0.0 for f in arqueo), 2)
            contado_total = round(sum(f.get("contado") or 0.0 for f in arqueo), 2)
            diferencia_total = round(contado_total - esperado_total, 2)
        else:
            agr = agrupar_ventas(self._l10n_pe_ne_ventas_planas())
            ingresos, retiros = self._l10n_pe_ne_ingresos_retiros()
            arqueo, esperado_total, contado_total, diferencia_total = calcular_arqueo(
                self.saldo_inicial, self._l10n_pe_ne_por_medio_arqueo(agr), ingresos, retiros, None)
            ventas = base["ventas"]
        d = dict(base)
        d.pop("esperado", None)
        d.update({
            "empresa": {"razonSocial": self.company_id.name or "", "ruc": self.company_id.vat or ""},
            "ventas": ventas,
            "arqueo": arqueo,
            "esperadoTotal": esperado_total,
            "contadoTotal": contado_total,
            "diferenciaTotal": diferencia_total,
        })
        return d

    def _l10n_pe_ne_fila_dict(self):
        """Fila resumida del historial (sin movimientos). Abierta -> contado/diferencia null."""
        self.ensure_one()
        if self.estado == "cerrada" and self.conteos_cierre is not None:
            arqueo = self.conteos_cierre or []
            esperado_total = round(sum(f.get("esperado") or 0.0 for f in arqueo), 2)
            contado_total = round(sum(f.get("contado") or 0.0 for f in arqueo), 2)
            diferencia_total = round(contado_total - esperado_total, 2)
        else:
            agr = agrupar_ventas(self._l10n_pe_ne_ventas_planas())
            ingresos, retiros = self._l10n_pe_ne_ingresos_retiros()
            _f, esperado_total, contado_total, diferencia_total = calcular_arqueo(
                self.saldo_inicial, self._l10n_pe_ne_por_medio_arqueo(agr), ingresos, retiros, None)
        return {
            "id": self.id,
            "estado": self.estado,
            "fechaApertura": self._l10n_pe_ne_fmt_dt(self.fecha_apertura),
            "fechaCierre": self._l10n_pe_ne_fmt_dt(self.fecha_cierre),
            "usuarioApertura": self.usuario_apertura_id.name or "",
            "usuarioCierre": self.usuario_cierre_id.name or "",
            "saldoInicial": self.saldo_inicial or 0.0,
            "esperadoTotal": esperado_total,
            "contadoTotal": contado_total,
            "diferenciaTotal": diferencia_total,
        }

    def _l10n_pe_ne_sesion_abierta(self):
        sesion = self.search([("estado", "=", "abierta"),
                              ("company_id", "=", self.env.company.id)], limit=1)
        if not sesion:
            raise UserError(_("No hay una caja abierta."))
        return sesion

    # -------------------------------------------------------- métodos públicos
    @api.model
    def l10n_pe_ne_caja_actual(self):
        sesion = self.search([("estado", "=", "abierta"),
                              ("company_id", "=", self.env.company.id)], limit=1)
        return {"abierta": bool(sesion),
                "sesion": sesion._l10n_pe_ne_sesion_dict() if sesion else None}

    @api.model
    def l10n_pe_ne_abrir_caja(self, datos):
        datos = datos or {}
        if self.search_count([("estado", "=", "abierta"),
                              ("company_id", "=", self.env.company.id)]):
            raise UserError(_("Ya hay una caja abierta para tu negocio. Ciérrala antes de abrir otra."))
        saldo = float(datos.get("saldoInicial") or 0.0)
        if saldo < 0:
            raise UserError(_("El saldo inicial no puede ser negativo."))
        sesion = self.create({
            "saldo_inicial": round(saldo, 2),
            "nota_apertura": (datos.get("nota") or "").strip() or False,
        })
        return sesion._l10n_pe_ne_sesion_dict()

    @api.model
    def l10n_pe_ne_caja_movimiento(self, datos):
        datos = datos or {}
        sesion = self._l10n_pe_ne_sesion_abierta()
        tipo = datos.get("tipo")
        if tipo not in ("ingreso", "retiro"):
            raise UserError(_("Elige ingreso o retiro."))
        # Mínimo 3 caracteres, igual que la validación del formulario (antes el
        # backend aceptaba cualquier motivo no vacío — divergencia con la UI).
        motivo = (datos.get("motivo") or "").strip()
        if len(motivo) < 3:
            raise UserError(_("El motivo debe tener al menos 3 caracteres."))
        monto = float(datos.get("monto") or 0.0)
        if monto <= 0:
            raise UserError(_("El monto debe ser mayor a 0."))
        # Un RETIRO no puede superar el efectivo disponible = saldo inicial + ventas en efectivo
        # + ingresos − retiros previos (misma fórmula que el esperado del arqueo). Sin esto, el
        # esperado en efectivo podía quedar NEGATIVO (imposible físicamente en una caja).
        if tipo == "retiro":
            agr = agrupar_ventas(sesion._l10n_pe_ne_ventas_planas())
            ingresos, retiros = sesion._l10n_pe_ne_ingresos_retiros()
            disponible = round(sesion.saldo_inicial
                               + sesion._l10n_pe_ne_por_medio_arqueo(agr).get(EFECTIVO, 0.0)
                               + ingresos - retiros, 2)
            if round(monto, 2) > disponible:
                raise UserError(_(
                    "No puedes retirar S/ %(monto).2f: la caja solo tiene S/ %(disp).2f "
                    "en efectivo.", monto=round(monto, 2), disp=disponible))
        self.env["l10n_pe_ne.caja.movimiento"].create({
            "sesion_id": sesion.id, "tipo": tipo,
            "motivo": motivo, "monto": round(monto, 2),
        })
        return sesion._l10n_pe_ne_sesion_dict()

    @api.model
    def l10n_pe_ne_cerrar_caja(self, datos):
        datos = datos or {}
        sesion = self._l10n_pe_ne_sesion_abierta()
        conteos = datos.get("conteos") or []
        if not conteos:
            raise UserError(_("Indica el conteo de al menos un medio."))
        agr = agrupar_ventas(sesion._l10n_pe_ne_ventas_planas())
        ingresos, retiros = sesion._l10n_pe_ne_ingresos_retiros()
        filas, _et, _ct, _dt = calcular_arqueo(
            sesion.saldo_inicial, sesion._l10n_pe_ne_por_medio_arqueo(agr), ingresos, retiros, conteos)
        sesion.write({
            "estado": "cerrada",
            "fecha_cierre": fields.Datetime.now(),
            "usuario_cierre_id": self.env.user.id,
            "nota_cierre": (datos.get("nota") or "").strip() or False,
            "conteos_cierre": filas,
            "ventas_cierre": {"count": agr["count"], "total": agr["total"],
                              "sinMedio": agr["sinMedio"], "countUsd": agr["countUsd"],
                              "totalUsd": agr["totalUsd"]},
        })
        return sesion._l10n_pe_ne_arqueo_dict()

    @api.model
    def l10n_pe_ne_list_cajas(self, periodo=None, limit=120):
        """Historial (abierta primero por fecha desc, luego cerradas desc). Filtro periodo
        YYYYMM sobre fecha_apertura (mismo patrón monthrange que l10n_pe_ne_list_gastos)."""
        domain = []
        if periodo and len(str(periodo)) == 6 and str(periodo).isdigit():
            y, mo = int(str(periodo)[:4]), int(str(periodo)[4:6])
            last = calendar.monthrange(y, mo)[1]
            domain += [("fecha_apertura", ">=", "%04d-%02d-01 00:00:00" % (y, mo)),
                       ("fecha_apertura", "<=", "%04d-%02d-%02d 23:59:59" % (y, mo, last))]
        return [s._l10n_pe_ne_fila_dict() for s in self.search(domain, limit=limit)]

    @api.model
    def l10n_pe_ne_caja_arqueo(self, rec_id):
        """Arqueo por id (shell/E2E). browse+read: cross-tenant -> AccessError (ir.rule);
        inexistente -> UserError. El controller hace su propio 404 (ver Decisión 5)."""
        sesion = self.browse(int(rec_id or 0))
        if not sesion.exists():
            raise UserError(_("Sesión de caja no encontrada."))
        return sesion._l10n_pe_ne_arqueo_dict()


class L10nPeNeCajaMovimiento(models.Model):
    _name = "l10n_pe_ne.caja.movimiento"
    _description = "Movimiento de caja (NE Express)"
    _order = "fecha desc, id desc"

    sesion_id = fields.Many2one("l10n_pe_ne.caja.sesion", required=True, index=True,
                                ondelete="cascade")
    tipo = fields.Selection([("ingreso", "Ingreso"), ("retiro", "Retiro")], required=True)
    motivo = fields.Char(required=True)
    monto = fields.Monetary(currency_field="currency_id")  # > 0, validado en método
    fecha = fields.Datetime(default=fields.Datetime.now)
    usuario_id = fields.Many2one("res.users", default=lambda s: s.env.user)
    currency_id = fields.Many2one("res.currency", default=lambda s: s.env.company.currency_id)
    # company_id PROPIO (no related) para que la ir.rule aplique directa sobre el movimiento.
    company_id = fields.Many2one("res.company", required=True, index=True,
                                 default=lambda s: s.env.company)

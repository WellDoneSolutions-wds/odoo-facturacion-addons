# -*- coding: utf-8 -*-
"""Caja (NE Express) — apertura/cierre/arqueo por medio de pago, estilo POS/bodega.

Dos modelos propios de Odoo: TODA la lógica (CRUD + serialización + amarre de ventas)
vive en el addon; React solo llama. Aislado por compañía (reglas multi-compañía en
security). La aritmética del arqueo se delega a tools/caja_arqueo.py (puro, testeado sin
Odoo). La caja NUNCA bloquea una venta (modo informativo, coherente con stock v1)."""
import calendar
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..tools.caja_arqueo import (agrupar_ventas, calcular_arqueo, descuadre_arqueo,
                                 disponible_medio, normalizar_medio, sumar_medio)

_logger = logging.getLogger(__name__)

# Mínimo de un motivo escrito en caja (movimiento o descuadre de cierre). Un solo número para
# los dos: si el movimiento exige 3 caracteres y el descuadre aceptara 1, el cajero aprendería
# que hay un texto que "no cuenta" y lo usaría siempre.
_MOTIVO_MIN = 3

# C2: grupos que reciben el aviso de un cierre descuadrado. Viven en el addon de ROLES, que es
# OPCIONAL: se resuelven con raise_if_not_found=False y, si no están, el aviso queda igualmente
# en el chatter de la sesión. El biller nunca se rompe por la ausencia de roles.
_GRUPOS_AVISO_DESCUADRE = ("l10n_pe_ne_roles.group_l10n_pe_ne_duenio",
                           "l10n_pe_ne_roles.group_l10n_pe_ne_supervisor")


class L10nPeNeCajaSesion(models.Model):
    _name = "l10n_pe_ne.caja.sesion"
    _description = "Sesión de caja (NE Express)"
    # C2: mail.thread para el AVISO de descuadre. Coste cero ('mail' ya es dependencia del
    # biller) y es el patrón de mensajería que ya usa el resto del producto (message_post en el
    # comprobante y en los flujos de roles). Aporta las dos mitades que hacen falta: el REGISTRO
    # permanente colgado del arqueo —quién cerró, con qué diferencia y por qué— y la NOTIFICACIÓN
    # al dueño/supervisor, sin inventar una tabla de avisos que nadie mantendría.
    _inherit = ["mail.thread"]
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
    # Local desde el que opera el turno. Lo lee el resolver de emisión como escalón previo al
    # domicilio fiscal: el cajero declara su sucursal UNA vez al abrir y no una vez por venta
    # (la doctrina de los 3 toques no admite un paso más por cobro).
    # ondelete='restrict': un arqueo cerrado NOMBRA su local; si borrar el establecimiento
    # pusiera la FK a NULL, el arqueo inmutable pasaría a decir que fue del negocio entero.
    # El borrado real pasa por el archivado del establecimiento (_l10n_pe_ne_en_uso).
    establecimiento_id = fields.Many2one("l10n_pe_ne.establecimiento", string="Establecimiento",
                                         index=True, ondelete="restrict")
    # Sin FK hay DOS cosas distintas, y confundirlas descuadra un arqueo:
    #   * la caja del DOMICILIO FISCAL ('0000'), que no tiene fila donde apuntar porque el
    #     '0000' es sintético (D3) — cuenta solo lo declarado en '0000';
    #   * la caja de siempre, anterior a esta fase, que no declaró local — cuenta TODA la
    #     compañía, que es lo que contaba ayer y lo que su cajero espera al cerrar.
    # Este flag las separa sin materializar el '0000': se marca cuando el cajero elige
    # "Domicilio fiscal" al abrir. Sin él, el local principal no podría cuadrar por separado
    # mientras el anexo vende en paralelo, que es justo el caso de dos locales.
    domicilio_fiscal = fields.Boolean(string="Caja del domicilio fiscal")
    # C1: el turno lo abrió una VENTA, no el cajero (no había caja y el cobro habría quedado
    # fuera de todo arqueo). Se guarda porque cambia lo que hay que decirle al cajero: su saldo
    # inicial es 0 aunque el cajón tuviera sencillo, y al querer abrir "su" caja se va a topar
    # con esta. Sin el flag, la única pista sería el texto de nota_apertura — frágil y no
    # consultable.
    apertura_automatica = fields.Boolean(string="Abierta automáticamente al cobrar", readonly=True)
    # C2: justificación escrita del descuadre. Se exige SOLO cuando la diferencia supera la
    # tolerancia del RUC, y entonces es obligatoria: hasta hoy el mismo cajero cerraba con +1.00
    # y con -2469.41 y las dos pasaban en silencio, así que el arqueo no distinguía «me sobró
    # sencillo» de «falta el efectivo de media tarde». Sin este texto no hay nada que revisar
    # mañana: la cifra sola no dice si fue un error de vuelto, una venta cobrada por otro medio
    # o una plata que no está.
    descuadre_motivo = fields.Char(string="Motivo del descuadre", readonly=True)
    # ¿se emitió el aviso al dueño/supervisor? Es parte del arqueo congelado porque responde a
    # «¿alguien se enteró?», que es justo lo que un auditor pregunta seis meses después.
    descuadre_avisado = fields.Boolean(string="Descuadre avisado", readonly=True)
    currency_id = fields.Many2one("res.currency", required=True,
                                  default=lambda s: s.env.company.currency_id)
    company_id = fields.Many2one("res.company", required=True, index=True,
                                 default=lambda s: s.env.company)

    # D-2 (integridad): campos del arqueo que quedan congelados al cerrar la sesión. Incluye
    # QUIÉN cerró y su nota/motivo: una justificación de descuadre que se pudiera reescribir
    # después —o un cierre al que se le cambiara el firmante— no es evidencia de nada.
    _CAMPOS_SNAPSHOT = ("conteos_cierre", "ventas_cierre", "saldo_inicial", "estado",
                        "fecha_apertura", "fecha_cierre", "usuario_cierre_id", "nota_cierre",
                        "descuadre_motivo", "descuadre_avisado")

    def init(self):
        # Índice único parcial: imposibilita la carrera de doble apertura simultánea
        # (una sola sesión 'abierta' por compañía y LOCAL). La guarda amigable vive en el método
        # l10n_pe_ne_abrir_caja; este índice es la defensa de última línea (race).
        #
        # El COALESCE es obligatorio: en Postgres NULL != NULL, así que sin él dos sesiones sin
        # local NO se verían como duplicadas y un tenant que no usa sucursales —la mayoría—
        # podría abrir DOS cajas a la vez. Sería una regresión silenciosa de dinero: cada
        # arqueo contaría las ventas de ambas y los dos cerrarían con diferencia.
        #
        # El índice anterior llevaba este MISMO nombre sobre (company_id) a secas, y
        # CREATE ... IF NOT EXISTS no lo recrea: hay que tirarlo cuando su definición es la
        # vieja o el segundo local jamás podría abrir su caja.
        self.env.cr.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = %s",
            ("l10n_pe_ne_caja_sesion_unica_abierta",))
        previo = self.env.cr.fetchone()
        if previo and "COALESCE" not in (previo[0] or "").upper():
            self.env.cr.execute("DROP INDEX l10n_pe_ne_caja_sesion_unica_abierta")
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS l10n_pe_ne_caja_sesion_unica_abierta
            ON l10n_pe_ne_caja_sesion (company_id, COALESCE(establecimiento_id, 0))
            WHERE estado = 'abierta'
        """)

    def write(self, vals):
        """D-2: una sesión cerrada es inmutable en su arqueo. Se comprueba el estado ALMACENADO
        (aún 'abierta' durante la transición de cierre, así l10n_pe_ne_cerrar_caja no se rompe);
        una vez 'cerrada', reescribir el snapshot por la ruta /web queda bloqueado. El contexto
        l10n_pe_ne_bypass_lock deja pasar migraciones/mantenimiento."""
        if not self.env.context.get("l10n_pe_ne_bypass_lock") and (set(vals) & set(self._CAMPOS_SNAPSHOT)):
            for s in self:
                if s.estado == "cerrada":
                    raise UserError(_("La sesión de caja está cerrada: su arqueo es inmutable."))
        return super().write(vals)

    # -------------------------------------------------------- helpers privados
    def _l10n_pe_ne_fmt_dt(self, dt):
        """Datetime -> 'YYYY-MM-DD HH:mm' en hora local del usuario (America/Lima)."""
        if not dt:
            return ""
        return fields.Datetime.context_timestamp(self, dt).strftime("%Y-%m-%d %H:%M")

    def _l10n_pe_ne_cod_local(self):
        """Código del local del turno: '0002' si la caja declaró un anexo, '0000' si se abrió
        en el domicilio fiscal y '' si no declaró ninguno.

        El '' NO es lo mismo que '0000': marca la caja del negocio entero (toda caja anterior
        a esta fase), que cuadra contra TODAS las ventas del RUC."""
        self.ensure_one()
        if self.establecimiento_id:
            return self.establecimiento_id.codigo or ""
        return "0000" if self.domicilio_fiscal else ""

    def _l10n_pe_ne_ventas_sesion(self):
        """account.move amarrados a la sesión (ventana por create_date y LOCAL del turno).

        La caja refleja DINERO FÍSICO: la venta cuenta desde el COBRO, aunque la emisión
        async siga en cola (por_enviar/en_proceso) — antes se exigía 'enviado' (CDR de
        SUNAT aplicado por el cron) y el cajero no veía su venta por ~1 minuto, con
        riesgo de cerrar caja descuadrada. Si la emisión falla en definitiva
        (rechazado/error/anulado), la venta sale del esperado en vivo: el cajero debe
        re-emitirla (la nueva sí cuenta) o el descuadre aflora en el cierre.

        El filtro por local es dinero, no cosmética: sin él el esperado de efectivo de
        Miraflores incluiría las ventas de San Isidro, el conteo ciego SIEMPRE daría
        diferencia y esa diferencia quedaría congelada e inmutable en conteos_cierre.

        La caja SIN local declarado no filtra: cuenta toda la compañía, exactamente como antes
        de esta fase. Un tenant que ya tenía anexos y una sola caja cerraba cuadrando con las
        ventas de sus anexos incluidas; filtrarlas ahora le sacaría plata del esperado el día
        del upgrade, sin que nadie haya cambiado nada."""
        self.ensure_one()
        dominio = [
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("l10n_pe_biller_state", "not in", ("rechazado", "error", "anulado")),
            ("company_id", "=", self.company_id.id),
            ("create_date", ">=", self.fecha_apertura),
            ("create_date", "<=", self.fecha_cierre or fields.Datetime.now()),
        ]
        cod = self._l10n_pe_ne_cod_local()
        if cod == "0000":
            # El comprobante viejo pudo quedarse sin código (el campo se creó después): un
            # NULL es domicilio fiscal, igual que el default de la columna.
            dominio.append(("l10n_pe_ne_cod_establecimiento", "in", ("0000", False)))
        elif cod:
            dominio.append(("l10n_pe_ne_cod_establecimiento", "=", cod))
        return self.env["account.move"].search(dominio)

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
        # Notas de venta (no-CPE) cobradas en esta sesión: son plata real -> entran al arqueo igual
        # que las ventas de account.move. Amarradas por caja_sesion_id; excluye anuladas. Total =
        # líneas + redondeo de efectivo. Contado (una nota de venta se cobra al momento).
        notas = self.env["l10n_pe_ne.nota_venta"].search([
            ("caja_sesion_id", "=", self.id), ("estado", "!=", "anulada")])
        for nv in notas:
            out.append({
                "total": (nv.amount_total or 0.0) + (nv.redondeo or 0.0),
                "moneda": nv.currency_id.name or "PEN",
                "formaPago": "Contado",
                "medios": nv.medios_pago or [],
            })
        return out

    def _l10n_pe_ne_ingresos_retiros_por_medio(self):
        """C3: (ingresos, retiros) de la sesión agrupados POR MEDIO — {'Efectivo': 50, 'Yape': 30}.

        El movimiento sin medio escrito (todo lo anterior a C3, cuando el ingreso/retiro solo
        podía ser efectivo) cae en 'Efectivo' por `normalizar_medio('')`. Por eso no hace falta
        migrar ni una fila: la historia sigue cuadrando exactamente donde cuadraba."""
        self.ensure_one()
        ingresos, retiros = {}, {}
        for mv in self.movimiento_ids:
            # 'adelanto' (addon de roles) NO es ninguno de los dos: entra al esperado por su
            # propio seam (_l10n_pe_ne_por_medio_arqueo), no como movimiento de caja.
            destino = ingresos if mv.tipo == "ingreso" else retiros if mv.tipo == "retiro" else None
            if destino is None:
                continue
            sumar_medio(destino, mv.medio, mv.monto or 0.0)
        return ingresos, retiros

    def _l10n_pe_ne_ingresos_retiros(self):
        """Totales de ingresos y retiros (todos los medios juntos). Es el resumen que ve el
        cajero en pantalla; la aritmética del esperado usa la versión POR MEDIO."""
        self.ensure_one()
        ingresos, retiros = self._l10n_pe_ne_ingresos_retiros_por_medio()
        return round(sum(ingresos.values()), 2), round(sum(retiros.values()), 2)

    # ------------------------------------------- guard del egreso por medio (C3)
    def _l10n_pe_ne_disponible(self, medio):
        """Cuánto hay AHORA en un bolsillo de ESTA caja (mismo esperado que revelará el cierre).

        NO se sirve nunca al front: es el esperado, y el conteo ciego (D-1) se apoya en que el
        cajero no lo conozca antes de contar. Solo se usa puertas adentro, para decidir si un
        egreso cabe."""
        self.ensure_one()
        agr = agrupar_ventas(self._l10n_pe_ne_ventas_planas())
        ingresos, retiros = self._l10n_pe_ne_ingresos_retiros_por_medio()
        return disponible_medio(self.saldo_inicial, self._l10n_pe_ne_por_medio_arqueo(agr),
                                ingresos, retiros, medio)

    def _l10n_pe_ne_check_egreso(self, medio, monto):
        """Un egreso (retiro manual o gasto pagado del cajón) no puede superar lo disponible EN
        SU MEDIO: no se saca por Yape más de lo que entró por Yape, ni del cajón más de lo que
        hay en el cajón. Sin esto el esperado de un medio quedaría negativo, que es físicamente
        imposible y además esconde el error real (el medio mal elegido).

        El mensaje NO revela el disponible (hallazgo de auditoría): con la cifra exacta, un
        egreso imposible sirve de sonda para leer el esperado ANTES de contar y vaciar el conteo
        ciego (D-1)."""
        self.ensure_one()
        if round(monto or 0.0, 2) > self._l10n_pe_ne_disponible(medio):
            raise UserError(_(
                "No puedes sacar S/ %(monto).2f por %(medio)s: excede lo que hay en la caja por "
                "ese medio. Revisa el medio elegido —el dinero de cada bolsillo se cuenta "
                "aparte—.", monto=round(monto or 0.0, 2), medio=normalizar_medio(medio)))

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
            # C3: de qué bolsillo salió/entró. Sin el medio en la lista, un retiro por Yape y uno
            # de efectivo se ven idénticos y el cajero no puede auditar su propio turno.
            # El movimiento histórico (sin medio) es efectivo, igual que lo era antes de C3.
            "medio": normalizar_medio(mv.medio),
            "fecha": self._l10n_pe_ne_fmt_dt(mv.fecha),
            "usuario": mv.usuario_id.name or "",
            "voucherRef": mv.voucher_ref or "",
            "fechaVoucher": mv.fecha_voucher.strftime("%Y-%m-%d") if mv.fecha_voucher else "",
            "destino": mv.destino or "",
            # C3: el movimiento nació de un gasto registrado (no lo tecleó el cajero aquí). Se
            # expone para que la pantalla lo marque y no invite a "corregirlo" por su cuenta:
            # se corrige reversando el gasto, que es lo que mantiene los dos libros cuadrados.
            "gastoId": mv.gasto_id.id or None,
        } for mv in self.movimiento_ids]

    # ------------------------------------------------- descuadre del cierre (C2)
    def _l10n_pe_ne_tolerancia(self):
        """Tolerancia de descuadre del RUC (parámetro de negocio, nunca un número en el código)."""
        self.ensure_one()
        return round(self.company_id.l10n_pe_ne_cierre_tolerancia or 0.0, 2)

    def _l10n_pe_ne_sobre_tolerancia(self, descuadre):
        """¿este descuadre SUPERA la tolerancia? Estrictamente mayor y con la precisión de la
        moneda (compare_amounts, nunca `>` a pelo sobre floats): un arqueo que descuadra
        EXACTAMENTE la tolerancia está dentro —el tope es «hasta aquí no pasa nada»—, y sin
        redondear a la moneda un 5.000000001 nacido de la aritmética flotante le pediría al
        cajero que justifique un descuadre que no existe."""
        self.ensure_one()
        return self.currency_id.compare_amounts(
            abs(round(descuadre or 0.0, 2)), self._l10n_pe_ne_tolerancia()) > 0

    def _l10n_pe_ne_descuadre(self):
        """Bloque `descuadre` del contrato de arqueo.

        El monto NO es un campo: se deriva de `conteos_cierre`, que YA está congelado (D-2). Así
        el histórico anterior a esta rebanada —los cierres que nadie explicó— aparece medido con
        la misma vara, sin migrar nada. Con la sesión abierta el monto es None: el conteo ciego
        (D-1) no se rompe por la puerta de atrás. `tolerancia` sí viaja siempre: es una política
        publicada (está en Ajustes), no un dato del arqueo, y el cajero tiene derecho a saber
        desde cuándo le van a pedir una explicación ANTES de contar."""
        self.ensure_one()
        tolerancia = self._l10n_pe_ne_tolerancia()
        cerrada = self.estado == "cerrada" and self.conteos_cierre is not None
        monto = descuadre_arqueo(self.conteos_cierre or []) if cerrada else None
        return {
            "monto": monto,
            "tolerancia": tolerancia,
            "sobreTolerancia": (self._l10n_pe_ne_sobre_tolerancia(monto)
                                if monto is not None else None),
            "motivo": self.descuadre_motivo or "",
            "avisado": bool(self.descuadre_avisado),
        }

    def _l10n_pe_ne_destinatarios_aviso(self):
        """Partners a los que se notifica un cierre descuadrado: dueño y supervisores del RUC.

        NO se excluye a quien cierra. Primero por doctrina (aquí no se compara la identidad de
        dos usuarios) y sobre todo porque en el negocio de una sola persona el dueño ES el
        cajero: excluirlo dejaría el aviso sin destinatario justo en el tenant más común. Odoo
        no le manda notificación al autor de su propio mensaje, así que no hay ruido: el registro
        queda igual, que es el punto.

        sudo en la búsqueda: el cajero no tiene por qué poder leer la ficha de su supervisor, y
        un permiso que falte no puede impedir que el dueño se entere de un faltante."""
        self.ensure_one()
        grupos = self.env["res.groups"]
        for xmlid in _GRUPOS_AVISO_DESCUADRE:
            g = self.env.ref(xmlid, raise_if_not_found=False)
            if g:
                grupos |= g
        if not grupos:
            return self.env["res.partner"]
        # all_group_ids (no group_ids): el dueño tiene supervisor por IMPLICACIÓN, y con
        # group_ids —solo los explícitos— se quedaría fuera del aviso.
        usuarios = self.env["res.users"].sudo().search([
            ("company_ids", "in", self.company_id.ids),
            ("all_group_ids", "in", grupos.ids),
            ("share", "=", False),
        ])
        return usuarios.partner_id

    def _l10n_pe_ne_cuerpo_aviso(self, descuadre, motivo):
        self.ensure_one()
        local = self._l10n_pe_ne_cod_local()
        return _(
            "Cierre de caja con descuadre de S/ %(monto).2f (tolerancia S/ %(tol).2f). "
            "Sesión N° %(sid)s%(local)s, abierta el %(apertura)s. Cerró: %(quien)s. "
            "Motivo declarado: %(motivo)s",
            monto=round(descuadre or 0.0, 2), tol=self._l10n_pe_ne_tolerancia(), sid=self.id,
            local=(_(" · local %s") % local) if local else "",
            apertura=self._l10n_pe_ne_fmt_dt(self.fecha_apertura),
            quien=self.env.user.name or "",
            motivo=motivo or _("(no declarado)"))

    def _l10n_pe_ne_avisar_descuadre(self, descuadre, motivo):
        """C2: avisa al dueño/supervisor de un cierre fuera de tolerancia. Devuelve si se avisó.

        NO bloquea el cierre —esa fue la decisión de negocio: en una bodega de tres personas
        esperar a que un supervisor apruebe es un cajero que no puede irse a su casa—, así que
        el control es de DETECCIÓN: la plata se cierra igual, pero alguien se entera hoy y no
        cuando cuadre el mes.

        Nada de lo que pase aquí puede tumbar un cierre YA CONTADO: si la notificación falla
        (sin destinatarios, un permiso raro, mail mal configurado) se registra en el log y el
        arqueo se cierra igual. Por eso el savepoint: un error de base dentro de message_post
        abortaría la transacción entera y el cajero perdería el conteo que acaba de teclear."""
        self.ensure_one()
        cuerpo = self._l10n_pe_ne_cuerpo_aviso(descuadre, motivo)
        try:
            with self.env.cr.savepoint():
                destinatarios = self._l10n_pe_ne_destinatarios_aviso()
                # sudo: el mensaje se postea sobre la sesión (el cajero la puede escribir) pero
                # notifica a partners que quizá no puede leer. El AUTOR sigue siendo el cajero:
                # sudo cambia los permisos, no el usuario.
                self.sudo().message_post(body=cuerpo, partner_ids=destinatarios.ids,
                                         subtype_xmlid="mail.mt_note")
            return True
        except Exception as e:  # noqa: BLE001 — un aviso jamás rompe un cierre
            _logger.warning("Caja %s: no se pudo avisar el descuadre (%s)", self.id, e)
            return False

    def _l10n_pe_ne_sesion_dict(self):
        """Sesión con movimientos (contrato GET /ne/api/caja).

        D-1 CONTEO CIEGO: con la sesión abierta NO se sirve el 'esperado' por medio ni el
        'esperadoTotal'. Si el cajero ve cuánto debería haber en la misma pantalla donde
        teclea el conteo, el arqueo es una DECLARACIÓN, no una medición: puede copiar el
        esperado y el descuadre nunca aflora. Solo se emiten los NOMBRES de los medios
        (para sembrar las filas de conteo en la SPA, incluidos los no estándar). El esperado
        y la diferencia se revelan recién en la respuesta del CIERRE (_l10n_pe_ne_arqueo_dict
        con estado 'cerrada')."""
        self.ensure_one()
        agr = agrupar_ventas(self._l10n_pe_ne_ventas_planas())
        ingresos, retiros = self._l10n_pe_ne_ingresos_retiros()
        ing_medio, ret_medio = self._l10n_pe_ne_ingresos_retiros_por_medio()
        # MERGE conteo-ciego × adelantos: se calcula el arqueo solo para extraer los NOMBRES de
        # medios con movimiento (los importes NO se serializan con la sesión abierta, D-1), pero
        # con el SEAM — así un adelanto por Yape (CN-02) también siembra su fila de conteo.
        # C3: los movimientos van POR MEDIO, así un retiro por Yape también siembra su fila.
        filas, _et, _c, _d = calcular_arqueo(
            self.saldo_inicial, self._l10n_pe_ne_por_medio_arqueo(agr), ing_medio, ret_medio, None)
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
            # C1: la SPA pinta un aviso — este turno lo abrió una venta, no el cajero, y su saldo
            # inicial es 0 aunque hubiera sencillo en el cajón (el sobrante saldrá en el cierre).
            "aperturaAutomatica": bool(self.apertura_automatica),
            "moneda": self.currency_id.name or "PEN",
            # Local del turno: la SPA lo pinta como chip para que el cajero vea DÓNDE está
            # cobrando antes de cobrar (con dos locales, equivocarse descuadra dos arqueos).
            # '' = caja sin local (ve todas las ventas); '0000' = domicilio fiscal.
            "establecimientoId": self.establecimiento_id.id or None,
            "establecimiento": self._l10n_pe_ne_cod_local(),
            "establecimientoDireccion": self.establecimiento_id.direccion or "",
            "movimientos": self._l10n_pe_ne_movimientos_dicts(),
            "ingresos": ingresos,
            "retiros": retiros,
            "ventas": {"count": agr["count"], "total": agr["total"], "sinMedio": agr["sinMedio"],
                       "countUsd": agr["countUsd"], "totalUsd": agr["totalUsd"]},
            # Solo nombres (sin montos): la SPA siembra una fila de conteo por cada medio.
            "medios": [f["medio"] for f in filas],
            # C2: desde qué diferencia habrá que explicarse. Viaja con la sesión ABIERTA a
            # propósito y sin romper el conteo ciego (es la política del negocio, no el
            # esperado): la pantalla de cierre puede pedir el motivo en el mismo formulario en
            # vez de rebotarle al cajero un error después de teclear todo el conteo.
            "toleranciaDescuadre": self._l10n_pe_ne_tolerancia(),
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
            # D-1 CONTEO CIEGO: el "corte parcial" mid-turno (Imprimir corte) tampoco revela
            # el esperado mientras la sesión esté abierta — solo los medios con movimiento.
            # El esperado/contado/diferencia vuelven en el cierre (rama cerrada, arriba).
            agr = agrupar_ventas(self._l10n_pe_ne_ventas_planas())
            ing_medio, ret_medio = self._l10n_pe_ne_ingresos_retiros_por_medio()
            # MERGE conteo-ciego × adelantos: nombres por el SEAM (el adelanto siembra su medio),
            # importes en null hasta el cierre (D-1).
            filas, _et, _c, _d = calcular_arqueo(
                self.saldo_inicial, self._l10n_pe_ne_por_medio_arqueo(agr), ing_medio, ret_medio, None)
            arqueo = [{"medio": f["medio"], "esperado": None, "contado": None,
                       "diferencia": None} for f in filas]
            esperado_total = contado_total = diferencia_total = None
            ventas = base["ventas"]
        d = dict(base)
        d.pop("esperado", None)
        # "medios" es la semilla del conteo ciego (nombres en vivo); no pertenece a la vista
        # de arqueo y, en una sesión cerrada, re-calcularla en vivo rompería la inmutabilidad
        # del snapshot (una venta anulada tras el cierre cambiaría los nombres).
        d.pop("medios", None)
        d.update({
            "empresa": {"razonSocial": self.company_id.name or "", "ruc": self.company_id.vat or ""},
            "ventas": ventas,
            "arqueo": arqueo,
            # C2: el descuadre, su justificación y si se avisó. El arqueo impreso es el papel que
            # se archiva: sin el motivo al lado de la cifra, el papel dice cuánto faltó pero no
            # por qué, que es lo único que se puede revisar meses después.
            "descuadre": self._l10n_pe_ne_descuadre(),
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
            # D-1 CONTEO CIEGO: la fila abierta del historial no revela el esperado en vivo
            # (igual que contado/diferencia). Se conoce recién al cerrar.
            esperado_total = contado_total = diferencia_total = None
        return {
            "id": self.id,
            "estado": self.estado,
            "fechaApertura": self._l10n_pe_ne_fmt_dt(self.fecha_apertura),
            "fechaCierre": self._l10n_pe_ne_fmt_dt(self.fecha_cierre),
            "usuarioApertura": self.usuario_apertura_id.name or "",
            "usuarioCierre": self.usuario_cierre_id.name or "",
            "saldoInicial": self.saldo_inicial or 0.0,
            "establecimiento": self._l10n_pe_ne_cod_local(),
            "establecimientoDireccion": self.establecimiento_id.direccion or "",
            "esperadoTotal": esperado_total,
            "contadoTotal": contado_total,
            "diferenciaTotal": diferencia_total,
            # C2: el historial es la pantalla donde el dueño mira los cierres de la semana. Sin
            # el motivo aquí, tendría que abrir uno por uno para saber cuál se explicó y cuál no
            # —y en la práctica no abriría ninguno—.
            "descuadre": self._l10n_pe_ne_descuadre(),
        }

    def _l10n_pe_ne_local_dict(self):
        """Fila mínima para que la SPA PREGUNTE en qué local está el cajero cuando hay varias
        cajas abiertas a la vez. Se sirve en vez de elegir una: cobrar en la caja equivocada
        descuadra dos arqueos de golpe, el de origen y el de destino."""
        self.ensure_one()
        return {"sesionId": self.id,
                "establecimientoId": self.establecimiento_id.id or None,
                "establecimiento": self._l10n_pe_ne_cod_local(),
                "establecimientoDireccion": self.establecimiento_id.direccion or "",
                "usuarioApertura": self.usuario_apertura_id.name or "",
                "fechaApertura": self._l10n_pe_ne_fmt_dt(self.fecha_apertura)}

    # ------------------------------------------------- resolución de la sesión en curso
    def _l10n_pe_ne_abiertas(self):
        """Sesiones abiertas de la compañía, en orden estable de apertura."""
        return self.search([("estado", "=", "abierta"),
                            ("company_id", "=", self.env.company.id)], order="id")

    def _l10n_pe_ne_elegir_sesion(self, abiertas, cod_estab=None):
        """Con qué caja opera ESTE usuario, entre las abiertas del RUC.

        Desde que cada local abre la suya, «la sesión de la compañía» dejó de existir como
        concepto. El orden es:

          1. el local pedido explícitamente (la SPA lo manda cuando ya preguntó);
          2. la caja que abrió el propio usuario — su turno, la que tiene el dinero delante;
          3. la única abierta, si hay una sola (el tenant de siempre, y el cajero que releva a
             otro sin cerrar);
          4. nada: con varias abiertas y sin forma de decidir NO se adivina. Elegir la primera
             cobraría en la caja de otro local y descuadraría los dos arqueos."""
        cod_estab = (cod_estab or "").strip()
        if cod_estab:
            return abiertas.filtered(lambda s: s._l10n_pe_ne_cod_local() == cod_estab)[:1]
        propia = abiertas.filtered(lambda s: s.usuario_apertura_id.id == self.env.uid)
        if propia:
            return propia[:1]
        return abiertas if len(abiertas) == 1 else self.browse()

    def _l10n_pe_ne_error_varias_cajas(self, abiertas):
        locales = ", ".join(
            (s._l10n_pe_ne_cod_local() or _("sin local")) + " (%s)" % (s.usuario_apertura_id.name or "")
            for s in abiertas)
        return UserError(_(
            "Hay varias cajas abiertas y ninguna es la tuya: %(locales)s. Indica desde qué "
            "local operas —o abre la tuya—: cobrar en la caja de otro local descuadraría los "
            "dos arqueos.") % {"locales": locales})

    def _l10n_pe_ne_sesion_abierta(self, cod_estab=None):
        abiertas = self._l10n_pe_ne_abiertas()
        if not abiertas:
            raise UserError(_("No hay una caja abierta."))
        sesion = self._l10n_pe_ne_elegir_sesion(abiertas, cod_estab)
        if not sesion and cod_estab:
            raise UserError(_("No hay una caja abierta en el local %s.") % cod_estab)
        if not sesion:
            raise self._l10n_pe_ne_error_varias_cajas(abiertas)
        if not sesion._l10n_pe_ne_bloquear():
            raise UserError(_("La caja se acaba de cerrar; abre una nueva para continuar."))
        return sesion

    def _l10n_pe_ne_bloquear(self):
        """Bloquea la fila de la sesión y re-lee su estado bajo el lock; devuelve si sigue
        abierta. Serializa cualquier movimiento (ingreso/retiro/adelanto/gasto del cajón) contra
        el CIERRE de caja: sin esto, un movimiento puede colgarse de una sesión ya cerrada
        —dinero movido fuera de todo arqueo—."""
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM l10n_pe_ne_caja_sesion WHERE id = %s FOR UPDATE", (self.id,))
        self.invalidate_recordset(["estado"])
        return self.estado == "abierta"

    @api.model
    def _l10n_pe_ne_local_abierto(self):
        """Código del local de la caja abierta, para el resolver de la emisión. Devuelve '' si
        no hay caja, si la que hay no declaró local, o si hay varias y ninguna es del usuario:
        entonces la cadena sigue hasta el domicilio fiscal, que es lo que hacía todo el mundo
        hasta ahora.

        Misma resolución que el resto de la caja (_l10n_pe_ne_elegir_sesion) y por el mismo
        motivo: si con dos cajas abiertas se tomara «cualquiera de la compañía», el cajero del
        domicilio fiscal declararía sus ventas en la sucursal del vecino.

        No reusa _l10n_pe_ne_sesion_abierta: ese lanza si no hay caja y toma la fila FOR UPDATE.
        Elegir el local de un comprobante no debe impedir emitir sin caja ni serializar las
        emisiones contra el cierre.

        C1: las cajas de APERTURA AUTOMÁTICA no cuentan como escalón. Una caja que abrió el
        cajero es una DECLARACIÓN («hoy atiendo en Miraflores»); una que abrió sola al cobrar no
        la declaró nadie: es una inferencia sacada de UN comprobante, creada con el único fin de
        que esa plata caiga en algún arqueo. Si además fijara el local de los comprobantes
        siguientes, se invertirían causa y efecto: una venta que declaró '0002' —por payload o
        por su serie— dejaría toda la emisión posterior colgada de '0002', cambiándole la SERIE y
        el codLocalEmisor a documentos que nadie mandó a la sucursal. Eso quema correlativos en
        el local equivocado, que solo se arregla con una nota de crédito, y es justo lo que la
        fase de series vino a impedir. Ignorarlas deja este resolver respondiendo EXACTAMENTE lo
        que respondía antes de C1 (la auto-apertura no cambia dónde se emite, solo dónde se
        cuenta el dinero)."""
        # sudo: la emisión también corre desde el cron y el lote, sin grupo que valga; la
        # compañía va explícita en el domain de _l10n_pe_ne_abiertas.
        caja = self.sudo()
        abiertas = caja._l10n_pe_ne_abiertas().filtered(lambda s: not s.apertura_automatica)
        sesion = caja._l10n_pe_ne_elegir_sesion(abiertas)
        return sesion._l10n_pe_ne_cod_local() if sesion else ""

    # ------------------------------------------------- auto-apertura (venta sin caja)
    def _l10n_pe_ne_cubre_local(self, cod):
        """¿el arqueo de ESTA sesión contaría una venta declarada en el local `cod`?

        Espeja el filtro de _l10n_pe_ne_ventas_sesion, que es quien manda: la caja sin local
        declarado ('' — toda caja anterior a la fase de sucursales) cuenta TODA la compañía, así
        que cubre cualquier local; la que sí declaró uno cubre solo el suyo."""
        self.ensure_one()
        propio = self._l10n_pe_ne_cod_local()
        return not propio or propio == (cod or "0000")

    @api.model
    def _l10n_pe_ne_inicio_transaccion(self):
        """Instante de INICIO de la transacción según el reloj de la BD, truncado al segundo y
        con un segundo de holgura. Es el mismo reloj con el que Postgres sella `create_date`, que
        es lo que la ventana del arqueo compara (ver _l10n_pe_ne_ventas_sesion)."""
        self.env.cr.execute(
            "SELECT date_trunc('second', now() AT TIME ZONE 'UTC') - interval '1 second'")
        return self.env.cr.fetchone()[0]

    @api.model
    def _l10n_pe_ne_asegurar_sesion(self, cod_estab=None):
        """C1: garantiza que un cobro caiga en ALGÚN arqueo. Devuelve (sesion, abierta_ahora).

        El POS emitía con la caja cerrada y esa venta no entraba en NINGÚN arqueo: la sesión
        anterior está congelada (D-2) y la siguiente arranca después, así que era dinero físico
        sin rastro —y el descuadre aparecía como un faltante inexplicable en el cierre del día
        siguiente, o en ninguno—. Ahora, si no hay caja que la cuente, se abre una con saldo
        inicial 0 y se informa en la respuesta.

        La regla «la caja NUNCA bloquea una venta» se conserva: esto no rechaza nada, y si la
        apertura falla (carrera, ACL) la venta sigue su curso — el peor caso es el de hoy.

        Respeta la caja POR LOCAL: se abre la del local que el resolver ya decidió para el
        comprobante, no «una caja de la compañía». Abrirla en el local equivocado descuadraría
        dos arqueos, que es justo lo que la fase anterior vino a evitar.

        DELIBERADAMENTE conservador: solo se abre cuando NO hay ninguna caja abierta en el RUC.
        Si ya hay un turno abierto y esta venta se declara en OTRO local (el cajero de San Isidro
        facturando por Miraflores, escalón 2 del resolver), abrirle una caja a Miraflores sería
        peor que el problema: su arqueo esperaría una plata que está físicamente en San Isidro,
        descuadrarían los dos, y el cajero de Miraflores se encontraría con un turno abierto que
        no puede cerrar honestamente —y que le impide abrir el suyo—. Esa venta cruzada sigue sin
        caer en un arqueo, igual que hoy; queda anotado como limitación conocida."""
        cod = (cod_estab or "0000").strip() or "0000"
        # sudo en TODO el método: la emisión también corre desde flujos sin el grupo de caja
        # (cobro de cotización, orden de trabajo, controlador del POS). La compañía va explícita
        # en el domain de _l10n_pe_ne_abiertas, así que el aislamiento por RUC se mantiene.
        caja = self.sudo()
        abiertas = caja._l10n_pe_ne_abiertas()
        if abiertas:
            # Ya hay turno(s) abierto(s): no se abre nada (ni se toca la caja que el cajero tiene
            # delante). Se devuelve la que contaría esta venta, si alguna la cuenta.
            cubren = abiertas.filtered(lambda s: s._l10n_pe_ne_cubre_local(cod))
            return (cubren[:1] if cubren else caja.browse()), False
        estab = self.env["l10n_pe_ne.establecimiento"].sudo().browse()
        if cod != "0000":
            estab = self.env["l10n_pe_ne.establecimiento"].sudo().search(
                [("codigo", "=", cod), ("company_id", "=", self.env.company.id)], limit=1)
        vals = {
            "saldo_inicial": 0.0,
            # El cajero no declaró fondo: el esperado arranca en 0 y si había sencillo en el
            # cajón el cierre lo mostrará como sobrante — visible y explicable, que es
            # infinitamente mejor que una venta sin arqueo.
            "nota_apertura": _("Apertura automática al cobrar (no había caja abierta)"),
            "apertura_automatica": True,
            "establecimiento_id": estab.id or False,
            # Solo se marca domicilio fiscal si el comprobante lo DECLARA y no hay anexo: así la
            # caja nueva cuenta exactamente las ventas que el resolver manda a '0000'.
            "domicilio_fiscal": cod == "0000",
            "usuario_apertura_id": self.env.uid,
            "company_id": self.env.company.id,
            "currency_id": self.env.company.currency_id.id,
            # La ventana del arqueo amarra las ventas con `create_date >= fecha_apertura`, y el
            # create_date de un comprobante creado en ESTA MISMA transacción lo sella Postgres al
            # INICIO de la transacción —o sea, ANTES de que este código corra—. Con el now() de
            # Python (el default del campo) la venta que dispara la apertura quedaría JUSTO fuera
            # de su propia caja: sesión abierta de adorno y la venta igual de huérfana. Se ancla
            # al mismo reloj que sella create_date, truncado al segundo y con un segundo de
            # holgura por el redondeo del campo Datetime.
            "fecha_apertura": self._l10n_pe_ne_inicio_transaccion(),
        }
        try:
            # savepoint: dos terminales del mismo local cobrando a la vez chocarían contra el
            # índice único parcial, y sin el savepoint ese IntegrityError abortaría la
            # transacción ENTERA — la venta se perdería por intentar salvarle el arqueo.
            with self.env.cr.savepoint():
                sesion = caja.create(vals)
        except Exception:  # noqa: BLE001
            abiertas = caja._l10n_pe_ne_abiertas()
            cubren = abiertas.filtered(lambda s: s._l10n_pe_ne_cubre_local(cod))
            # La otra terminal ganó la carrera: su caja es la buena. Si ni así hay caja, se
            # devuelve vacío y la venta continúa (comportamiento de hoy).
            return (cubren[:1], False) if cubren else (caja.browse(), False)
        # Se devuelve en sudo a propósito: el llamador solo lee el aviso, y un permiso de caja
        # que faltara no puede reventar una emisión ya cobrada.
        return sesion, True

    def _l10n_pe_ne_aviso_apertura(self):
        """Bloque que la respuesta del cobro le devuelve a la SPA para avisar «se abrió tu caja».
        El cajero TIENE que enterarse: al cerrar contará un cajón cuyo turno no abrió él."""
        self.ensure_one()
        return {"sesionId": self.id,
                "establecimiento": self._l10n_pe_ne_cod_local(),
                "establecimientoDireccion": self.establecimiento_id.direccion or "",
                "saldoInicial": self.saldo_inicial or 0.0,
                "fechaApertura": self._l10n_pe_ne_fmt_dt(self.fecha_apertura),
                "mensaje": _("No había una caja abierta: se abrió una con saldo inicial S/ 0.00 "
                             "para que esta venta entre en tu arqueo.")}

    # -------------------------------------------------------- métodos públicos
    @api.model
    def l10n_pe_ne_caja_actual(self, establecimiento=None):
        """La caja EN LA QUE ESTÁ este usuario, no «la sesión de la compañía»: desde que cada
        local abre la suya, esa frase ya no identifica nada.

        Con varias abiertas y ninguna del usuario se responde `requiereLocal` con la lista, para
        que la SPA PREGUNTE en vez de adivinar. `establecimiento` es la respuesta a esa pregunta
        ('0000' para el domicilio fiscal)."""
        abiertas = self._l10n_pe_ne_abiertas()
        sesion = self._l10n_pe_ne_elegir_sesion(abiertas, establecimiento)
        if sesion:
            return {"abierta": True, "sesion": sesion._l10n_pe_ne_sesion_dict()}
        if len(abiertas) > 1:
            return {"abierta": False, "sesion": None, "requiereLocal": True,
                    "locales": [s._l10n_pe_ne_local_dict() for s in abiertas]}
        # Sin cajas (o con una sola que no es del local pedido): contrato de siempre, dos claves.
        return {"abierta": False, "sesion": None}

    def _l10n_pe_ne_local_apertura(self, datos):
        """Local del turno, elegido AL ABRIR y no una vez por venta. Devuelve el
        establecimiento (vacío = sin anexo) y si el domicilio fiscal se eligió EXPLÍCITAMENTE.

        Acepta `establecimientoId` (el id que ya usa el registro de series) o
        `codEstablecimiento` (el código que habla el resto de la emisión). Sin ninguno de los
        dos, la caja queda como las de siempre: del negocio entero."""
        Estab = self.env["l10n_pe_ne.establecimiento"]
        estab_id = datos.get("establecimientoId")
        if estab_id not in (None, "", 0, "0", False):
            estab = Estab.browse(int(estab_id)).exists()
            if not estab or estab.company_id != self.env.company:
                raise UserError(_("El establecimiento indicado no existe en tu catálogo."))
            if not estab.active:
                raise UserError(_(
                    "El establecimiento %s está archivado: ya no se emite desde él.")
                    % estab.codigo)
            return estab, True
        cod = (datos.get("codEstablecimiento") or "").strip()
        if not cod:
            return Estab.browse(), False
        # Valida contra el catálogo con el mensaje que separa «no está en tu catálogo» de «no
        # está dado de alta ante SUNAT»: abrir caja en un local inventado terminaría emitiendo
        # con un codLocalEmisor que SUNAT rechaza, ya con el correlativo quemado.
        cod = Estab._l10n_pe_ne_check_codigo(cod)
        if cod == "0000":
            return Estab.browse(), True
        return Estab.search([("codigo", "=", cod),
                             ("company_id", "=", self.env.company.id)], limit=1), True

    @api.model
    def l10n_pe_ne_abrir_caja(self, datos):
        datos = datos or {}
        estab, explicito = self._l10n_pe_ne_local_apertura(datos)
        # La unicidad es por (compañía, LOCAL): que Miraflores tenga su caja abierta no puede
        # impedirle a San Isidro abrir la suya. El índice único parcial es la defensa contra la
        # carrera; esta es la guarda que el cajero entiende.
        previa = self.search([("estado", "=", "abierta"),
                              ("company_id", "=", self.env.company.id),
                              ("establecimiento_id", "=", estab.id or False)], limit=1)
        if previa:
            # C1: si el turno que estorba lo abrió una VENTA (auto-apertura), el cajero no tiene
            # ni idea de que existe. Decírselo es la diferencia entre «el sistema está roto» y
            # «ciérralo y abre el tuyo con tu sencillo».
            if previa.apertura_automatica:
                raise UserError(_(
                    "Ya hay una caja abierta: se abrió sola al cobrar la primera venta (con saldo "
                    "inicial S/ 0.00) para que esa venta no quedara fuera del arqueo. Ciérrala "
                    "—contando lo que hay— y abre la tuya con el sencillo del cajón."))
            if estab:
                raise UserError(_(
                    "Ya hay una caja abierta en el local %s. Ciérrala antes de abrir otra.")
                    % estab.codigo)
            raise UserError(_("Ya hay una caja abierta para tu negocio. Ciérrala antes de abrir otra."))
        saldo = float(datos.get("saldoInicial") or 0.0)
        if saldo < 0:
            raise UserError(_("El saldo inicial no puede ser negativo."))
        sesion = self.create({
            "saldo_inicial": round(saldo, 2),
            "nota_apertura": (datos.get("nota") or "").strip() or False,
            "establecimiento_id": estab.id or False,
            # Solo si el cajero eligió: una caja que no declaró nada sigue siendo la del negocio
            # entero y su arqueo cuenta todas las ventas, como antes de esta fase.
            "domicilio_fiscal": bool(explicito and not estab),
        })
        return sesion._l10n_pe_ne_sesion_dict()

    @api.model
    def l10n_pe_ne_caja_movimiento(self, datos):
        datos = datos or {}
        # codEstablecimiento: con varias cajas abiertas la SPA manda el local que ya eligió, para
        # que el ingreso/retiro caiga en la caja que el cajero tiene delante y no en otra.
        sesion = self._l10n_pe_ne_sesion_abierta(datos.get("codEstablecimiento"))
        tipo = datos.get("tipo")
        if tipo not in ("ingreso", "retiro"):
            raise UserError(_("Elige ingreso o retiro."))
        # Mínimo 3 caracteres, igual que la validación del formulario (antes el
        # backend aceptaba cualquier motivo no vacío — divergencia con la UI).
        motivo = (datos.get("motivo") or "").strip()
        if len(motivo) < _MOTIVO_MIN:
            raise UserError(_("El motivo debe tener al menos %s caracteres.") % _MOTIVO_MIN)
        monto = float(datos.get("monto") or 0.0)
        if monto <= 0:
            raise UserError(_("El monto debe ser mayor a 0."))
        # C3: el movimiento tiene MEDIO. Sin `medio` en el payload sigue siendo efectivo, que es
        # lo único que existía hasta ahora: el cliente viejo no cambia de conducta.
        medio = normalizar_medio(datos.get("medio"))
        voucher_ref = (datos.get("voucherRef") or "").strip()
        fecha_voucher = datos.get("fechaVoucher") or False
        if tipo == "retiro":
            # Un RETIRO no puede superar lo disponible EN SU MEDIO (misma fórmula que el esperado
            # del arqueo). Sin esto el esperado del medio quedaría NEGATIVO, que es físicamente
            # imposible en una caja.
            sesion._l10n_pe_ne_check_egreso(medio, monto)
            # D-4 (integridad): un retiro sobre el umbral del RUC exige contraparte documental.
            # Sin esto, un retiro solo resta del esperado y el arqueo cuadra sin rastro de a
            # dónde fue la plata. Aplica a CUALQUIER medio: sacar S/ 800 por Yape necesita el
            # mismo respaldo que sacarlos del cajón —el bolsillo cambia, el dinero no—.
            umbral = sesion.company_id.l10n_pe_ne_retiro_umbral or 0.0
            if round(monto, 2) > umbral and not (voucher_ref and fecha_voucher):
                raise UserError(_(
                    "Un retiro de S/ %(monto).2f necesita número de voucher/depósito y fecha "
                    "(el negocio exige respaldo para retiros mayores a S/ %(umbral).2f).",
                    monto=round(monto, 2), umbral=round(umbral, 2)))
        self.env["l10n_pe_ne.caja.movimiento"].create({
            "sesion_id": sesion.id, "tipo": tipo,
            "motivo": motivo, "monto": round(monto, 2),
            "medio": medio,
            "voucher_ref": voucher_ref or False,
            "fecha_voucher": fecha_voucher,
            "destino": (datos.get("destino") or "").strip() or False,
        })
        return sesion._l10n_pe_ne_sesion_dict()

    @api.model
    def l10n_pe_ne_cerrar_caja(self, datos):
        """Cierra el turno y congela su arqueo.

        C2 — descuadre: dentro de la tolerancia del RUC el cierre pasa exactamente como siempre,
        sin una sola pregunta (el 95% de los días). Por encima, se EXIGE un motivo escrito y se
        avisa al dueño/supervisor, pero NO se bloquea: bloquear un cierre hasta que aparezca un
        supervisor es, en una bodega de tres personas, un problema diario — decisión de negocio
        explícita. El control es de detección, no de prevención.

        `motivoDescuadre` es el texto del cajero. Se guarda SIEMPRE que venga (aunque el arqueo
        cuadre): si alguien se tomó el trabajo de explicar algo, no se tira."""
        datos = datos or {}
        sesion = self._l10n_pe_ne_sesion_abierta(datos.get("codEstablecimiento"))
        conteos = datos.get("conteos") or []
        if not conteos:
            raise UserError(_("Indica el conteo de al menos un medio."))
        agr = agrupar_ventas(sesion._l10n_pe_ne_ventas_planas())
        ing_medio, ret_medio = sesion._l10n_pe_ne_ingresos_retiros_por_medio()
        filas, _et, _ct, _dt = calcular_arqueo(
            sesion.saldo_inicial, sesion._l10n_pe_ne_por_medio_arqueo(agr),
            ing_medio, ret_medio, conteos)
        motivo = (datos.get("motivoDescuadre") or "").strip()
        descuadre = descuadre_arqueo(filas)
        sobre = sesion._l10n_pe_ne_sobre_tolerancia(descuadre)
        if sobre and len(motivo) < _MOTIVO_MIN:
            # El mensaje NO dice cuánto descuadra ni cuál es el esperado (mismo criterio que el
            # guard del retiro): revelar la cifra al rebotar el cierre convertiría este error en
            # una sonda para leer el esperado y RE-declarar un conteo que cuadre — vaciando el
            # conteo ciego (D-1) justo en el escenario que este control persigue. Queda el
            # residual conocido: el rebote confirma «te pasaste», y con reintentos se puede
            # acotar la cifra. Por eso se registra en el log cada rebote: doce intentos seguidos
            # de un mismo cajero son un patrón visible, y el aviso al supervisor es la otra mitad.
            _logger.warning(
                "Caja %s: cierre rechazado por descuadre sin motivo (usuario id=%s).",
                sesion.id, self.env.uid)
            raise UserError(_(
                "El arqueo descuadra más de lo que tu negocio acepta sin explicación "
                "(S/ %(tol).2f). Escribe qué pasó —aunque sea «faltó vuelto» o «una venta se "
                "cobró por Yape»— y podrás cerrar: no necesitas que nadie te apruebe nada, pero "
                "queda anotado en el arqueo y se le avisa a quien supervisa.",
                tol=sesion._l10n_pe_ne_tolerancia()))
        # El aviso se emite ANTES de la escritura para que cierre y aviso sean UN hecho atómico:
        # si algo fallara después, no queda un arqueo congelado diciendo «avisado» sin aviso
        # (ni al revés). La sesión sigue 'abierta' en la BD en esta línea, así que la guarda D-2
        # —que lee el estado ALMACENADO— deja pasar la escritura de abajo.
        avisado = sesion._l10n_pe_ne_avisar_descuadre(descuadre, motivo) if sobre else False
        sesion.write({
            "estado": "cerrada",
            "fecha_cierre": fields.Datetime.now(),
            "usuario_cierre_id": self.env.user.id,
            "nota_cierre": (datos.get("nota") or "").strip() or False,
            "descuadre_motivo": motivo or False,
            "descuadre_avisado": avisado,
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
    # C3: bolsillo del movimiento. Hasta aquí el ingreso/retiro era efectivo por construcción y
    # el negocio que pagaba al proveedor por Yape no tenía dónde registrarlo: o no lo anotaba (y
    # el arqueo esperaba un saldo de Yape que ya no estaba) o lo anotaba como retiro de efectivo
    # (y descuadraban dos bolsillos de golpe). VACÍO = Efectivo, que es lo que vale todo el
    # histórico: por eso no hace falta migrar ninguna fila.
    # El campo vivía en el addon de ROLES (solo lo usaba el adelanto CN-02); baja al biller
    # porque ahora TODO movimiento tiene medio, y la caja es del biller.
    medio = fields.Char(string="Medio de pago")
    fecha = fields.Datetime(default=fields.Datetime.now)
    usuario_id = fields.Many2one("res.users", default=lambda s: s.env.user)
    # D-4 (integridad): contraparte documental de un retiro sobre umbral (texto libre, no
    # verificable en línea, pero deja rastro auditable de a dónde fue la plata).
    voucher_ref = fields.Char(string="N° de voucher / depósito")
    fecha_voucher = fields.Date(string="Fecha del voucher")
    destino = fields.Char(string="Destino (banco / cuenta)")
    # C3: gasto que originó este movimiento (egreso del cajón). ondelete='set null' y no cascade:
    # si alguna vez se borra un gasto por mantenimiento, el DINERO que salió de la caja no se
    # puede borrar con él —el arqueo ya lo contó—; se queda huérfano y visible, que es lo honesto.
    gasto_id = fields.Many2one("l10n_pe_ne.gasto", string="Gasto", index=True,
                               ondelete="set null", copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        """C3: el medio se canoniza en el ORM, no en cada llamador. Mismo choke point que usa
        account.move con l10n_pe_ne_medios_pago: 'yape' y 'Yape' tienen que ser el MISMO bolsillo
        al leer y al escribir, o el cajero cuenta su Yape dos veces."""
        for vals in vals_list:
            if "medio" in vals:
                vals["medio"] = normalizar_medio(vals.get("medio"))
        return super().create(vals_list)

    def write(self, vals):
        """D-2: un movimiento de una sesión cerrada no se edita (el unlink ya lo tapa la ACL)."""
        if not self.env.context.get("l10n_pe_ne_bypass_lock"):
            for mv in self:
                if mv.sesion_id.estado == "cerrada":
                    raise UserError(_("La sesión de caja está cerrada: sus movimientos son inmutables."))
        if vals.get("medio"):
            vals = dict(vals, medio=normalizar_medio(vals["medio"]))
        return super().write(vals)
    currency_id = fields.Many2one("res.currency", default=lambda s: s.env.company.currency_id)
    # company_id PROPIO (no related) para que la ir.rule aplique directa sobre el movimiento.
    company_id = fields.Many2one("res.company", required=True, index=True,
                                 default=lambda s: s.env.company)

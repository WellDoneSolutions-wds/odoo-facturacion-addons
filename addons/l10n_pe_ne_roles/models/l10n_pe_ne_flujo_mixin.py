# -*- coding: utf-8 -*-
"""Mixin de flujo de NE Express — el cimiento de los procesos de negocio con roles.

Todos los procesos (CN-01 mostrador, CN-02 taller, y los demás del catálogo) son la
misma figura: un DOCUMENTO con `estado` + un RESPONSABLE (`user_id`, NULL = en cola) +
HANDOFF (una transición que solo cierto rol puede hacer) + COLA filtrada en el servidor +
AUDITORÍA (mail.thread). Este AbstractModel implementa esa figura una sola vez.

LA REGLA NUCLEAR (ver docs/procesos-negocio/decision-escala-libre.md): en una transición
se validan EXACTAMENTE tres ejes —¿existe la transición?, ¿el usuario tiene el grupo?,
¿la realidad lo permite?— y JAMÁS se compara la identidad de dos usuarios. Un predicado de
identidad (`aprobador != solicitante`) no es monótono en los grupos del usuario: no se
satisface añadiendo roles, sino PERSONAS. Con un solo usuario el conjunto de quien puede
avanzar queda vacío y el documento se atasca para siempre. El producto debe funcionar con
1 usuario que lleva todos los sombreros (el cliente MODAL) y con N segregados, sin una sola
rama que pregunte cuánta gente hay.

Iteración 1: solo el motor de transiciones (3 ejes + folds + acciones). El motor de gates
de política por RUC (`off/aviso/bloqueo`) llega en la iteración 4 y se engancha en el hook
`_politica_de` (aquí es un no-op). Ningún modelo hereda todavía este mixin; los primeros
serán la cotización (CN-01) y el pedido (CN-02).
"""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

# Marca de contexto que autoriza escribir 'estado': la ponen SOLO las acciones controladas
# (_avanzar y los escritores internos con nombre). Un write RPC directo no la trae y se bloquea.
_FLUJO_OK = "l10n_pe_ne_flujo_ok"


class L10nPeNeFlujoMixin(models.AbstractModel):
    _name = 'l10n_pe_ne.flujo.mixin'
    _description = 'Flujo NE Express (estado + responsable + cola + auditoría)'
    # Coste CERO: 'mail' ya es dependencia transitiva del biller. Con esto todo modelo
    # de flujo gana chatter y actividades sin escribir una línea, y "quién avanzó esto"
    # queda auditado (hoy ningún modelo propio del biller lo tiene).
    _inherit = ['mail.thread', 'mail.activity.mixin']

    # OJO: el mixin NO declara `estado`. Cada modelo concreto ya tiene el suyo con su
    # propio Selection (p. ej. l10n_pe_ne.cotizacion). Declararlo aquí con selection=[]
    # arriesga PISAR ese Selection al mezclar el MRO. Cada modelo añade `tracking=True`
    # a su `estado` con un override parcial. Los métodos de abajo asumen que `self.estado`
    # existe: solo se llaman sobre modelos concretos que lo declaran.
    user_id = fields.Many2one(
        'res.users', string='Responsable', index=True, tracking=True,
        help="Quién tiene el documento. NULL = en cola: nadie lo ha tomado todavía.")
    priority = fields.Selection(
        [('0', 'Normal'), ('1', 'Urgente')], string='Prioridad',
        default='0', index=True)

    # Aprobación de gates (iteración 4). NO es un estado del Selection (un estado que espera a un
    # humano que quizá no exista es la violación blanda de la escala libre): es un ATRIBUTO. Un
    # documento que cruzó un gate en modo aviso/bloquea queda 'excepcion' hasta que se revise.
    control_estado = fields.Selection(
        [('normal', 'Normal'), ('excepcion', 'Excepción'), ('revisado', 'Revisado')],
        string='Control', default='normal', index=True, tracking=True)
    excepcion_motivo = fields.Char(string='Motivo de la excepción')
    excepcion_magnitud = fields.Float(string='Magnitud de la excepción')
    aprobador_id = fields.Many2one('res.users', string='Aprobado por', tracking=True)
    fecha_aprobacion = fields.Datetime(string='Fecha de aprobación')
    # es_auto_aprobacion: quien operó también aprobó (el caso del negocio de 1 persona). NO se
    # salta el gate: se atraviesa y se FIRMA. El registro es el punto (cae en el reporte del dueño).
    es_auto_aprobacion = fields.Boolean(string='Auto-aprobado', readonly=True)

    # ─────────────────────────────────────────────── tabla de transiciones
    @api.model
    def _transiciones(self):
        """(estado_origen, estado_destino) -> dict con la definición de la transición.

        Es un MÉTODO y no un atributo de clase porque los atributos Python planos NO se
        combinan por _inherit (el MRO los pisa en silencio). Un módulo que extienda un
        flujo hace: return {**super()._transiciones(), ('x', 'y'): {...}}.

        Claves reconocidas:
          grupo    xmlid del grupo que HABILITA la transición (None = cualquiera). Es el
                   ÚNICO predicado sobre el usuario. Nunca se compara identidad.
          guarda   nombre de un método que valida la REALIDAD (plazo SUNAT, saldo, stock).
                   PURO: valida y lanza; no escribe. Ningún grupo lo levanta, ni el dueño.
          motivo   True -> exige justificación escrita (queda como evidencia en el chatter).
          toma     True -> al avanzar, si el documento está en cola (user_id NULL) se
                   asigna a quien ejecuta (NULL -> yo). "Tomar" es atómico.
          cadena   True -> la transición puede formar parte de una acción compuesta (fold).
                   Las ramas de excepción (rechazar, anular, vencer) NUNCA la llevan: un
                   clic de más no puede rechazarle una cotización a un cliente.
          gate     (iteración 4) key de política por RUC que puede exigir aprobación.
          label    texto del botón. Lo decide el addon; la SPA solo lo pinta.
        """
        return {}

    @api.model
    def _estados_terminales(self):
        """Estados desde los que no sale ninguna transición (para el test-invariante)."""
        return ()

    def _cadenas_sugeridas(self):
        """[(estado_destino, label)] de folds que la SPA debe ofrecer como un botón único
        (p. ej. 'Cobrar y entregar'). Cada modelo define los suyos."""
        return []

    def _estado_label(self, key):
        if not key:
            return _("(sin estado)")
        sel = dict(self._fields['estado']._description_selection(self.env))
        return sel.get(key, key)

    # ══════════════════════════════════════════════════════ LA REGLA NUCLEAR
    def _check_transicion(self, destino, motivo=None, sonda=False):
        """Valida una transición por sus TRES ejes y devuelve su definición.

        Lo que NO se valida jamás, en ningún modelo, por ninguna razón:
            self.env.user != <cualquier otro usuario>
        Ver el docstring del módulo. La segregación de funciones no vive aquí (prevención)
        sino en el eje de detección (registro + revisión asíncrona, iteración 4).

        sonda=True: solo evalúa si la transición SERÍA posible (para pintar botones), sin
        exigir el motivo ni ejecutar guardas con efectos de lectura costosos.
        """
        self.ensure_one()
        t = self._transiciones().get((self.estado, destino))
        if t is None:
            raise UserError(_(
                "No se puede pasar de «%(o)s» a «%(d)s».",
                o=self._estado_label(self.estado), d=self._estado_label(destino)))

        # EJE 2 — CAPACIDAD. El único predicado sobre el usuario. Monótono en los grupos:
        # acumular roles solo puede habilitar, nunca deshabilitar.
        grupo = t.get('grupo')
        if grupo and not self.env.user.has_group(grupo):
            raise AccessError(_(
                "No tienes permiso para «%s». Pídeselo al dueño del negocio.",
                t.get('label') or destino))

        # EVIDENCIA — no es un permiso; es el control por defecto (registro).
        if t.get('motivo') and not sonda and not (motivo or '').strip():
            raise UserError(_("Escribe el motivo: queda en el historial del documento."))

        # EJE 3 — REALIDAD. Ningún grupo la levanta. Ni el dueño. Ni el admin.
        guarda = t.get('guarda')
        if guarda and not sonda:
            getattr(self, guarda)()
        return t

    # ─────────────────────────────────────────────── política de gates (iter 4)
    def _politica_de(self, t, magnitud=None):
        """(modo_efectivo, magnitud) de la compuerta de política de esta transición. Lee la
        política del RUC (res.company: off/aviso/bloquea + umbral). Sin gate en la transición o
        sin company -> 'off'. La magnitud la calcula el MODELO (t['magnitud']); el modo lo decide
        el RUC."""
        key = t.get('gate')
        if not key or 'company_id' not in self._fields or not self.company_id:
            return 'off', magnitud
        if magnitud is None and t.get('magnitud'):
            magnitud = getattr(self, t['magnitud'])()
        return self.company_id.l10n_pe_ne_gate(key, magnitud), magnitud

    def _puede_aprobar(self, t):
        key = t.get('gate')
        if not key:
            return False
        from .res_company_gates import _GATES
        grupo = _GATES.get(key, {}).get('grupo')
        return bool(grupo) and self.env.user.has_group(grupo)

    def _politica_texto(self, t, modo, magnitud):
        key = t.get('gate')
        if not key or not self.company_id:
            return ''
        from .res_company_gates import _GATES
        umbral = None
        cfg = _GATES.get(key, {})
        if cfg.get('umbral'):
            umbral = self.company_id[cfg['umbral']]
        return self.company_id._l10n_pe_ne_politica_frase(key, modo, umbral)

    def _aplicar_politica(self, t, modo, magnitud):
        """Vals que la política añade a la escritura de la transición. TRES caminos; el negocio de
        1 persona solo conoce el primero (off) y la auto-aprobación."""
        if modo == 'off':
            return {}
        exc = {
            'control_estado': 'excepcion',
            'excepcion_motivo': self._politica_texto(t, modo, magnitud),
            'excepcion_magnitud': float(magnitud or 0.0),
        }
        if modo == 'aviso':
            # NO bloquea: la operación sigue, la excepción queda marcada para revisión.
            return exc
        # modo == 'bloquea'
        if self.aprobador_id:
            return exc   # ya fue aprobado antes
        if self._puede_aprobar(t) and not self.company_id.l10n_pe_ne_exigir_segregacion:
            # AUTO-APROBACIÓN registrada (100% de los casos con 1 usuario). No se salta: se firma.
            return {**exc, 'aprobador_id': self.env.user.id,
                    'fecha_aprobacion': fields.Datetime.now(), 'es_auto_aprobacion': True}
        # No puede aprobar (o el RUC exige segregación): NO se ejecuta la transición.
        raise UserError(_(
            "«%(a)s» necesita la aprobación de otra persona. Quedó pendiente.",
            a=t.get('label') or self._estado_label(t.get('_destino', ''))))

    # ─────────────────────────────────────────────── blindaje de la máquina de estados
    @api.model_create_multi
    def create(self, vals_list):
        """Un documento de flujo NACE en su estado inicial (el default del campo), no directamente
        en uno avanzado. Cierra el vector espejo del write: crear una cotización ya 'convertida' (sin
        comprobante) o una orden ya 'entregada' por un create RPC. Sistema (su) y acciones internas
        (_FLUJO_OK) sí pueden sembrar estados (migraciones, fixtures)."""
        if not self.env.su and not self.env.context.get(_FLUJO_OK):
            if any(v.get("estado") for v in vals_list):
                raise UserError(_(
                    "Un documento nuevo nace en su estado inicial; su avance va por las acciones "
                    "del flujo, no se crea directamente en otro estado."))
        return super().create(vals_list)

    def write(self, vals):
        """'estado' NO se escribe por un write RPC directo (que se saltaría _avanzar: has_group de
        la arista, la toma atómica, la guarda de realidad, el gate del RUC y las reglas duras). Solo
        pasa desde una acción controlada (que marca _FLUJO_OK) o en modo SISTEMA (env.su: migraciones,
        cron, tests de fábrica). Cierra a nivel de MODELO el hueco de 'confiar en que solo el BFF
        llama los métodos buenos' — vale para todos los modelos de flujo (CN-01 cotización, CN-02
        orden, y los que vengan). La ir.rule limita QUÉ estados se ven; esto limita las TRANSICIONES.

        GUARDARRAÍL: la solidez de _FLUJO_OK depende de que NINGÚN endpoint reenvíe el contexto del
        cliente al ORM. Hoy los controllers solo exponen métodos con nombre (el _body alimenta
        argumentos, no contexto); si algún día se expone un call_kw/execute_kw genérico, el flag se
        volvería falsificable y habría que sacar 'estado' del write de emisor por otra vía."""
        if "estado" in vals and not self.env.su and not self.env.context.get(_FLUJO_OK):
            if any(rec.estado != vals["estado"] for rec in self):
                raise UserError(_(
                    "El estado de este documento se cambia por sus acciones (los botones del "
                    "flujo), no escribiéndolo directamente."))
        return super().write(vals)

    # ─────────────────────────────────────────────── serialización de fila
    def _l10n_pe_ne_lock(self):
        """Bloquea la fila (SELECT … FOR UPDATE) y re-lee su estado real. Lo usan _avanzar Y los
        FOLDS de cobro (cobrar_entregar/cobrar_saldo/registrar_adelanto): dos POST concurrentes
        (doble clic) sobre el mismo documento no pueden emitir DOS comprobantes a SUNAT para la
        misma venta — el segundo bloquea aquí, y al re-leer ve el comprobante ya vinculado y aborta
        ANTES del efecto externo. flush ANTES del SQL crudo (el ORM no lo hace por cr.execute), e
        invalidate DESPUÉS para forzar la re-lectura bajo el lock."""
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM %s WHERE id = %%s FOR UPDATE" % self._table, (self.id,))
        self.invalidate_recordset()

    # ─────────────────────────────────────────────── avanzar (una transición)
    def _avanzar(self, destino, motivo=None, vals=None, magnitud=None):
        """Ejecuta UNA transición: valida los tres ejes, aplica política, escribe y audita."""
        self.ensure_one()
        # Serializa la fila ANTES de validar: dos usuarios que avanzan el MISMO documento a la vez
        # no se pisan. El segundo bloquea hasta que el primero commitea, luego re-lee el estado ya
        # cambiado y su transición deja de existir (p. ej. la TOMA de cola: el segundo operario ve
        # la orden ya en 'en_proceso' y _check_transicion lanza). La 'toma' es atómica de verdad.
        self._l10n_pe_ne_lock()
        t = self._check_transicion(destino, motivo)
        w = dict(vals or {})
        w['estado'] = destino
        # 'tomar' es atómico: un documento en cola pasa a ser de quien lo avanza.
        if not self.user_id and t.get('toma'):
            w['user_id'] = self.env.user.id
        # Política del RUC (gate): off/aviso/bloquea. En 'bloquea' sin aprobación, LANZA (la
        # transición no ocurre). En 'aviso' o auto-aprobación, añade la marca de excepción.
        modo, mag = self._politica_de(t, magnitud)
        w.update(self._aplicar_politica({**t, '_destino': destino}, modo, mag))
        # Escritura AUTORIZADA de estado (viene de una transición validada por los 3 ejes).
        self.with_context(**{_FLUJO_OK: True}).write(w)
        self.message_post(body=self._bitacora(t, destino, motivo))
        return self

    def l10n_pe_ne_aprobar(self, key_gate=None, motivo=None):
        """Firma la aprobación de una excepción pendiente. NO cambia el estado del documento: el
        operador reintenta su acción y ahora pasa (aprobador_id ya está). Es el ÚNICO sitio con una
        comparación de identidades, y SOLO cuando el RUC activó exigir_segregacion."""
        self.ensure_one()
        t = {'gate': key_gate} if key_gate else None
        if t and not self._puede_aprobar(t):
            raise AccessError(_("No tienes permiso para aprobar esto."))
        exigir = ('company_id' in self._fields and self.company_id
                  and self.company_id.l10n_pe_ne_exigir_segregacion)
        if exigir and self.env.user == (self.user_id or self.create_uid):
            raise UserError(_("Tu negocio exige que apruebe otra persona."))
        self.write({'aprobador_id': self.env.user.id, 'fecha_aprobacion': fields.Datetime.now(),
                    'es_auto_aprobacion': self.env.user == (self.user_id or self.create_uid)})
        self.message_post(body=_("Aprobado por %s. %s") % (self.env.user.name, motivo or ''))
        return True

    def _bitacora(self, t, destino, motivo):
        """Línea de auditoría para el chatter. Un modelo puede enriquecerla."""
        txt = _("%(estado)s — por %(user)s",
                estado=self._estado_label(destino), user=self.env.user.name)
        if motivo:
            txt += _(". Motivo: %s", motivo)
        return txt

    # ─────────────────────────────────────────────── folds (acciones compuestas)
    def _ruta(self, origen, destino):
        """Camino más corto de `origen` a `destino` por transiciones marcadas cadena=True
        (BFS). Las ramas de excepción, al no llevar cadena=True, quedan fuera: un fold
        nunca puede rechazar ni anular por accidente."""
        if origen == destino:
            return []
        trans = self._transiciones()
        cola, visto = [(origen, [])], {origen}
        while cola:
            actual, camino = cola.pop(0)
            for (o, d), t in trans.items():
                if o != actual or d in visto or not t.get('cadena'):
                    continue
                if d == destino:
                    return camino + [d]
                visto.add(d)
                cola.append((d, camino + [d]))
        return []

    def _puede_ruta(self, ruta):
        """¿El usuario puede recorrer toda la ruta? Solo mira el eje GRUPO: las guardas de
        realidad dependen de un estado que aún no existe (p. ej. payment_state todavía no es
        'paid'). Si una guarda corta el fold en ejecución, el documento queda en la cola del
        que sí puede seguir y se le dice por qué — comportamiento correcto, no fallo."""
        actual = self.estado
        for d in ruta:
            t = self._transiciones().get((actual, d))
            if not t:
                return False
            grupo = t.get('grupo')
            if grupo and not self.env.user.has_group(grupo):
                return False
            actual = d
        return True

    def _avanzar_hasta(self, destino, motivo=None, vals_por_paso=None):
        """Acción compuesta ("cobrar y entregar" en un clic). NO es un atajo para tenants
        chicos: es el MISMO _avanzar N veces, con las mismas validaciones y la misma
        bitácora. El documento atraviesa TODOS los estados intermedios (la cola de despacho
        existe aunque dure milisegundos), así el reporte y el kardex no distinguen si lo
        hicieron tres personas en dos horas o una en 400 ms. Esa indistinguibilidad ES la
        escala libre.

        Semántica de fallo, deliberada:
          · 0 pasos ejecutados -> LANZA (el usuario pidió algo y no pasó nada).
          · >=1 paso y se corta -> devuelve completo=False (el documento quedó en la cola
            del que sí puede seguir). No es un error: es exactamente lo que pasa en una
            tienda de tres personas, con el mismo código.
        """
        self.ensure_one()
        ruta = self._ruta(self.estado, destino)
        if not ruta:
            raise UserError(_(
                "No hay camino de «%(o)s» a «%(d)s».",
                o=self._estado_label(self.estado), d=self._estado_label(destino)))
        hechos, corte = [], None
        for paso in ruta:
            try:
                # Savepoint por paso: un paso que falla a mitad no deja escritura parcial
                # ni ensucia la caché del ORM.
                with self.env.cr.savepoint():
                    self._avanzar(paso, motivo=motivo,
                                  vals=(vals_por_paso or {}).get(paso))
                hechos.append(paso)
            except (AccessError, UserError) as e:
                if not hechos:
                    raise
                corte = str(e)
                break
        return {
            'estado': self.estado,
            'pasos': hechos,
            'completo': self.estado == destino,
            'motivoCorte': corte,
        }

    # ─────────────────────────────────────────────── acciones (para la SPA)
    def _acciones(self):
        """Qué puede hacer ESTE usuario con ESTE documento AHORA: estado × grupo × guarda,
        resuelto en el addon. La SPA hace .map() y nada más — así la escala libre se vuelve
        visible sin una línea de TypeScript: al que tiene todos los roles le aparece el fold
        completo; al cajero puro, solo su tramo. Mismo endpoint, misma respuesta."""
        self.ensure_one()
        out = []
        for (o, d), t in self._transiciones().items():
            if o != self.estado:
                continue
            try:
                self._check_transicion(d, sonda=True)
            except (UserError, AccessError):
                continue
            out.append({
                'key': d,
                'label': t.get('label') or d,
                'pasos': [d],
                'requiereMotivo': bool(t.get('motivo')),
            })
        for destino, label in self._cadenas_sugeridas():
            ruta = self._ruta(self.estado, destino)
            if len(ruta) > 1 and self._puede_ruta(ruta):
                out.append({
                    'key': 'hasta:' + destino,
                    'label': label,
                    'pasos': ruta,
                    'requiereMotivo': False,
                })
        return out

    # ─────────────────────────────────────────────── cola (bandeja paginada)
    @api.model
    def _cola(self, dominio, offset=0, limit=10, order=None):
        """Cola de trabajo filtrada en el SERVIDOR (nunca en la SPA). Devuelve el envelope
        {items, total, offset, limit} que el controller serializa. Con 1 usuario la cola no
        se degrada: se colapsa a una bandeja de "Pendientes" — el mismo dominio sirve para
        1 y para N gracias al OR de las ir.rule por grupo."""
        total = self.search_count(dominio)
        registros = self.search(dominio, offset=offset, limit=limit,
                                order=order or self._order or 'id desc')
        return {
            'items': registros,
            'total': total,
            'offset': offset,
            'limit': limit,
        }

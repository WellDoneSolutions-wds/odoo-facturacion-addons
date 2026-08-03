# -*- coding: utf-8 -*-
"""Muro de la emisión masiva por rol.

La matriz de menú reserva la emisión masiva al supervisor/dueño (veMasivo = puedeSupervisar),
pero el ACL base de `l10n_pe_ne.lote` (l10n_pe_ne_biller) la concede a TODO emisor —y cajero,
vendedor, etc. implican `emisor`—, así que la SPA solo la OCULTABA del menú. Un cajero podía
crear/procesar un lote entrando a `/masivo` por URL o llamando el endpoint directo. Este es el
muro real, en línea con el patrón del resto de endpoints sensibles (has_group + AccessError).
"""
from odoo import _, models
from odoo.exceptions import AccessError

_GRUPO_SUPERVISOR = "l10n_pe_ne_roles.group_l10n_pe_ne_supervisor"


class L10nPeNeLote(models.Model):
    _inherit = "l10n_pe_ne.lote"

    def _l10n_pe_ne_masiva_gate(self):
        """Solo supervisor (el dueño lo implica vía implied_ids) o admin puede emitir masivamente.
        Los procesos automáticos (cron/worker SQS) corren en sudo y pasan por `env.su`, sin
        depender de que el disparador tenga el grupo."""
        if (self.env.su
                or self.env.user.has_group(_GRUPO_SUPERVISOR)
                or self.env.user.has_group("base.group_system")):
            return
        raise AccessError(_(
            "La emisión masiva está reservada al supervisor o al dueño del negocio."))

    def l10n_pe_ne_crear_lote(self, payload):
        self._l10n_pe_ne_masiva_gate()
        return super().l10n_pe_ne_crear_lote(payload)

    def l10n_pe_ne_procesar(self, max_filas=1):
        self._l10n_pe_ne_masiva_gate()
        return super().l10n_pe_ne_procesar(max_filas=max_filas)

    def l10n_pe_ne_reintentar(self):
        self._l10n_pe_ne_masiva_gate()
        return super().l10n_pe_ne_reintentar()

    def l10n_pe_ne_cancelar(self):
        self._l10n_pe_ne_masiva_gate()
        return super().l10n_pe_ne_cancelar()

    # --- Lecturas: también reservadas. `veMasivo` (supervisor/dueño) es TODO-o-nada: quien no
    # emite masivamente tampoco necesita ver la lista/el detalle/los resultados ni bajar la
    # plantilla. Sin esto, un cajero por Postman listaba los lotes del RUC (nombres de archivo,
    # conteos, resultados) aunque la SPA le oculte /masivo. El cron/worker corre en env.su y pasa.
    def l10n_pe_ne_list_lotes(self):
        self._l10n_pe_ne_masiva_gate()
        return super().l10n_pe_ne_list_lotes()

    def l10n_pe_ne_lote_detalle(self):
        self._l10n_pe_ne_masiva_gate()
        return super().l10n_pe_ne_lote_detalle()

    def l10n_pe_ne_resultados(self):
        self._l10n_pe_ne_masiva_gate()
        return super().l10n_pe_ne_resultados()

    def l10n_pe_ne_plantilla(self, tipo="comprobante"):
        self._l10n_pe_ne_masiva_gate()
        return super().l10n_pe_ne_plantilla(tipo=tipo)

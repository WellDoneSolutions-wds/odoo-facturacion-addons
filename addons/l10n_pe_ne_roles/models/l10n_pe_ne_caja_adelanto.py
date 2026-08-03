# -*- coding: utf-8 -*-
"""CN-02 · adelanto a cuenta en caja (Vía B, recibo interno).

Extiende la caja del biller para el prepago del cliente SIN emitir comprobante: un movimiento de
tipo 'adelanto' con su MEDIO (para cuadrar el arqueo por medio, no solo efectivo) y su CLIENTE, y
el enganche a la orden de trabajo. El adelanto NO es un ingreso genérico (que iría solo a Efectivo
y mezclaría un pasivo del cliente con el fondo propio): entra al esperado del arqueo por su medio
vía el seam _l10n_pe_ne_por_medio_arqueo del biller.
"""
from odoo import _, api, fields, models
from odoo.addons.l10n_pe_ne_biller.tools.caja_arqueo import normalizar_medio, sumar_medio


class L10nPeNeCajaMovimientoAdelanto(models.Model):
    _inherit = "l10n_pe_ne.caja.movimiento"

    # 'adelanto': prepago a cuenta ligado a una orden. cascade: si se desinstala roles, el concepto
    # desaparece con sus movimientos (no quedan 'adelanto' huérfanos en una caja solo-biller).
    tipo = fields.Selection(selection_add=[("adelanto", "Adelanto a cuenta")],
                            ondelete={"adelanto": "cascade"})
    partner_id = fields.Many2one("res.partner", string="Cliente", index=True)
    orden_trabajo_id = fields.Many2one("l10n_pe_ne.orden.trabajo", string="Orden de trabajo",
                                       index=True, ondelete="set null")

    # C3: `medio` (y su canonización en create/write) BAJÓ al biller. Lo estrenó el adelanto,
    # pero desde que el ingreso/retiro también sale por Yape es un dato de TODO movimiento de
    # caja, y la caja vive en el biller. Aquí no queda nada que redefinir: el adelanto sigue
    # entrando al arqueo por su medio, ahora con la misma normalización que el resto.


class L10nPeNeCajaSesionAdelanto(models.Model):
    _inherit = "l10n_pe_ne.caja.sesion"

    def _l10n_pe_ne_por_medio_arqueo(self, agr):
        """Suma los adelantos de la sesión al por-medio del arqueo (cada uno por SU medio). Así el
        prepago físico del cliente cuadra el esperado aunque venga por Yape/Tarjeta, sin inflar
        Efectivo como lo haría un ingreso genérico.

        Vía A (anticipo facturado): los adelantos cuya orden ya emitió su comprobante
        (orden_trabajo_id.anticipo_factura_id seteado) se SALTAN aquí — esa plata ya entra al arqueo
        por los MEDIOS del comprobante emitido (es una venta de la sesión, la cuenta el seam base);
        sumarla también aquí sería doble conteo. Corolario: si la emisión del anticipo quedó en
        'error', esa plata NO aparece en el arqueo hasta re-emitir — mismo contrato que cualquier venta
        con error (no se cuenta lo que SUNAT aún no aceptó)."""
        por_medio = super()._l10n_pe_ne_por_medio_arqueo(agr)
        for mv in self.movimiento_ids:
            if mv.tipo == "adelanto":
                # Vía A: el comprobante del anticipo ya aporta esta plata por sus medios (venta de la
                # sesión). Contar además el movimiento de caja duplicaría el ingreso.
                if mv.orden_trabajo_id.anticipo_factura_id:
                    continue
                # C1: se suma con `sumar_medio` (agrupación case/tilde-insensitive) y no con un
                # `dict[medio] +=`: un adelanto histórico escrito 'yape' tiene que caer en la
                # MISMA fila que la venta cobrada por 'Yape', o el cajero cuenta su bolsillo de
                # Yape dos veces y una de las dos filas cierra con diferencia.
                sumar_medio(por_medio, mv.medio, mv.monto or 0.0)
        return por_medio

    @api.model
    def _l10n_pe_ne_registrar_adelanto(self, monto, medio, partner, motivo):
        """Crea el movimiento de adelanto sobre la sesión abierta. Lo llama la orden de trabajo
        (que luego enlaza orden_trabajo_id). Exige caja abierta (el helper del biller lanza si no).

        Con varios locales, la sesión la resuelve el helper del biller: la del propio usuario
        primero. El prepago es dinero FÍSICO que el cliente deja en un mostrador concreto, así
        que colgarlo de la caja de otra sucursal descuadraría las dos —y si no hay forma de
        saber cuál, el helper prefiere lanzar antes que adivinar—."""
        sesion = self._l10n_pe_ne_sesion_abierta()
        return self.env["l10n_pe_ne.caja.movimiento"].create({
            "sesion_id": sesion.id,
            "tipo": "adelanto",
            "motivo": motivo or _("Adelanto a cuenta"),
            "monto": round(float(monto or 0.0), 2),
            # C1: nombre canónico ('Depósito', no 'DEPOSITO') — el create lo re-normaliza igual,
            # esto solo hace explícito el contrato de la llamada.
            "medio": normalizar_medio(medio),
            "partner_id": partner.id if partner else False,
        })

    def _l10n_pe_ne_movimientos_dicts(self):
        """Enriquece los adelantos con su cliente/orden para la vista de caja. El `medio` ya lo
        sirve el biller para TODO movimiento desde C3 (antes solo lo tenía el adelanto)."""
        dicts = super()._l10n_pe_ne_movimientos_dicts()
        by_id = {mv.id: mv for mv in self.movimiento_ids}
        for d in dicts:
            mv = by_id.get(d["id"])
            if mv and mv.tipo == "adelanto":
                d["cliente"] = mv.partner_id.name or ""
                d["orden"] = mv.orden_trabajo_id.name or ""
        return dicts

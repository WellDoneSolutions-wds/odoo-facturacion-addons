# -*- coding: utf-8 -*-
"""R10 · Agenda de citas / turnos (fase 2 del módulo de rubros).

Para los rubros de atención con horario: salud (consultorios), spa/estética,
veterinarias, educación (entrevistas). Es una AGENDA operativa, no un motor de
reservas: día + hora + cliente + servicio + estado, pensada para el mostrador
(«¿quién sigue?», «márcala atendida»). El cobro sigue siendo el POS/Emitir de
siempre — una cita atendida no factura sola (el precio real se decide al cobrar).

La hora se guarda como texto HH:MM en hora LOCAL del negocio: la agenda es un
dato de mostrador (como la pizarra de turnos), no un evento con husos horarios.
"""
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_HORA_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

ESTADOS = [
    ("pendiente", "Pendiente"),
    ("confirmada", "Confirmada"),
    ("atendida", "Atendida"),
    ("cancelada", "Cancelada"),
]


class Cita(models.Model):
    _name = "l10n_pe_ne.cita"
    _description = "Cita / turno de atención"
    _order = "fecha, hora, id"

    name = fields.Char(string="Servicio / motivo", required=True)   # «Consulta», «Corte + tinte»
    cliente = fields.Char(required=True)         # nombre libre: la mascota, el paciente, quien sea
    telefono = fields.Char()                     # para confirmar/reprogramar por teléfono
    fecha = fields.Date(required=True, index=True)
    hora = fields.Char(required=True)            # HH:MM local del negocio
    duracion_min = fields.Integer(default=30)
    estado = fields.Selection(ESTADOS, default="pendiente", required=True)
    notas = fields.Char()
    company_id = fields.Many2one(
        "res.company", required=True, index=True, default=lambda self: self.env.company)

    @api.constrains("hora")
    def _check_hora(self):
        for c in self:
            if not _HORA_RE.match(c.hora or ""):
                raise UserError(_("La hora debe ser HH:MM (ej. 09:30, 15:00)."))

    # ------------------------------------------------------------- API SPA
    def _dict(self):
        self.ensure_one()
        return {
            "id": self.id, "servicio": self.name, "cliente": self.cliente,
            "telefono": self.telefono or "", "fecha": str(self.fecha),
            "hora": self.hora, "duracionMin": self.duracion_min,
            "estado": self.estado, "notas": self.notas or "",
        }

    @api.model
    def l10n_pe_ne_list(self, fecha=None):
        """Agenda del día (o toda la futura si no se pasa fecha). Orden natural: la hora."""
        dom = [("company_id", "=", self.env.company.id)]
        if fecha:
            dom.append(("fecha", "=", fecha))
        else:
            dom.append(("fecha", ">=", fields.Date.context_today(self)))
        return [c._dict() for c in self.search(dom)]

    @api.model
    def l10n_pe_ne_save(self, payload):
        """Alta/edición. El muro R10 corta aquí: sin el módulo (rubro sin agenda) no hay citas
        ni por API directa."""
        self.env["account.move"]._l10n_pe_ne_check_modulo("R10", "Agenda de citas / turnos")
        payload = payload or {}
        vals = {
            "name": (payload.get("servicio") or "").strip(),
            "cliente": (payload.get("cliente") or "").strip(),
            "telefono": (payload.get("telefono") or "").strip() or False,
            "fecha": payload.get("fecha") or fields.Date.context_today(self),
            "hora": (payload.get("hora") or "").strip(),
            "duracion_min": int(payload.get("duracionMin") or 30),
            "estado": payload.get("estado") or "pendiente",
            "notas": (payload.get("notas") or "").strip() or False,
            "company_id": self.env.company.id,
        }
        if not vals["name"]:
            raise UserError(_("Escribe el servicio o motivo de la cita."))
        if not vals["cliente"]:
            raise UserError(_("Escribe el nombre del cliente."))
        if dict(ESTADOS).get(vals["estado"]) is None:
            raise UserError(_("Estado de cita no válido."))
        cita_id = int(payload.get("id") or 0)
        cita = self.browse(cita_id).exists() if cita_id else self.browse()
        if cita:
            cita.write(vals)
        else:
            cita = self.create(vals)
        return cita._dict()

    @api.model
    def l10n_pe_ne_delete(self, cita_id):
        """Una cita no tiene valor fiscal: se elimina de verdad (la pizarra se borra)."""
        cita = self.browse(int(cita_id or 0)).exists()
        if cita:
            cita.unlink()
        return {"ok": True}

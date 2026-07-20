"""Maestros de flota para la GRE: vehículos y conductores 'frecuentes' (el wizard de
SUNAT los guarda al usarlos; aquí igual — la SPA hace upsert al emitir una guía)."""
from odoo import api, fields, models


class L10nPeNeVehiculo(models.Model):
    _name = 'l10n_pe_ne.vehiculo'
    _description = 'Vehículo frecuente (GRE)'
    _order = 'placa'

    placa = fields.Char(required=True, index=True)
    ent_autorizacion = fields.Char(string='Entidad de la autorización (cat. D37)')
    num_autorizacion = fields.Char(string='N° de autorización')
    company_id = fields.Many2one('res.company', required=True, index=True,
                                 default=lambda s: s.env.company)

    _placa_company_uniq = models.Constraint(
        'unique(placa, company_id)',
        'Ya existe un vehículo con esa placa.',
    )

    @api.model
    def l10n_pe_ne_upsert(self, payload):
        placa = (payload.get('placa') or '').strip().upper()
        vals = {'placa': placa}
        if payload.get('entAutorizacion') is not None:
            vals['ent_autorizacion'] = payload.get('entAutorizacion') or False
        if payload.get('numAutorizacion') is not None:
            vals['num_autorizacion'] = payload.get('numAutorizacion') or False
        rec = self.search([('placa', '=', placa), ('company_id', '=', self.env.company.id)], limit=1)
        if rec:
            rec.write(vals)
        else:
            rec = self.create(dict(vals, company_id=self.env.company.id))
        return rec

    def _l10n_pe_ne_dict(self):
        self.ensure_one()
        return {'id': self.id, 'placa': self.placa,
                'entAutorizacion': self.ent_autorizacion or '',
                'numAutorizacion': self.num_autorizacion or ''}

    @api.model
    def l10n_pe_ne_list(self):
        return [v._l10n_pe_ne_dict()
                for v in self.search([('company_id', '=', self.env.company.id)])]

    @api.model
    def l10n_pe_ne_delete_vehiculo(self, rec_id):
        v = self.browse(int(rec_id or 0)).exists()
        if v:
            v.unlink()
        return {'ok': True, 'modo': 'eliminado'}


class L10nPeNeConductor(models.Model):
    _name = 'l10n_pe_ne.conductor'
    _description = 'Conductor frecuente (GRE)'
    _order = 'apellidos, nombres'

    tipo_doc = fields.Selection([('1', 'DNI'), ('4', 'Carné ext.'), ('7', 'Pasaporte')],
                                required=True, default='1')
    num_doc = fields.Char(required=True, index=True)
    nombres = fields.Char(required=True)
    apellidos = fields.Char(required=True)
    licencia = fields.Char(required=True)
    company_id = fields.Many2one('res.company', required=True, index=True,
                                 default=lambda s: s.env.company)

    _doc_company_uniq = models.Constraint(
        'unique(tipo_doc, num_doc, company_id)',
        'Ya existe un conductor con ese documento.',
    )

    @api.model
    def l10n_pe_ne_upsert(self, payload):
        tipo = payload.get('tipoDoc') or '1'
        num = (payload.get('numDoc') or '').strip()
        vals = {}
        for k, f in (('nombres', 'nombres'), ('apellidos', 'apellidos'), ('licencia', 'licencia')):
            if payload.get(k):
                vals[f] = payload[k]
        rec = self.search([('tipo_doc', '=', tipo), ('num_doc', '=', num),
                           ('company_id', '=', self.env.company.id)], limit=1)
        if rec:
            rec.write(vals)
        else:
            rec = self.create(dict(vals, tipo_doc=tipo, num_doc=num,
                                   company_id=self.env.company.id))
        return rec

    def _l10n_pe_ne_dict(self):
        self.ensure_one()
        return {'id': self.id, 'tipoDoc': self.tipo_doc, 'numDoc': self.num_doc,
                'nombres': self.nombres, 'apellidos': self.apellidos, 'licencia': self.licencia}

    @api.model
    def l10n_pe_ne_list(self):
        return [c._l10n_pe_ne_dict()
                for c in self.search([('company_id', '=', self.env.company.id)])]

    @api.model
    def l10n_pe_ne_delete_conductor(self, rec_id):
        c = self.browse(int(rec_id or 0)).exists()
        if c:
            c.unlink()
        return {'ok': True, 'modo': 'eliminado'}

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { computeM2OProps, Many2One } from "@web/views/fields/many2one/many2one";
import { buildM2OFieldDescription, Many2OneField } from "@web/views/fields/many2one/many2one_field";
import { Component } from "@odoo/owl";

/**
 * Many2one del campo Customer que, además de la búsqueda normal de Odoo,
 * inyecta sugerencias desde la fuente externa (DynamoDB/API) cuando lo que
 * escribes es un DNI (8) o RUC (11). Al elegir la sugerencia, crea el contacto
 * y lo selecciona. La búsqueda por nombre la sigue resolviendo Odoo.
 */
export class PartnerLookupMany2one extends Component {
    static template = "l10n_pe_partner_lookup.PartnerLookupMany2one";
    static components = { Many2One };
    static props = { ...Many2OneField.props };

    setup() {
        this.orm = useService("orm");
    }

    get m2oProps() {
        return {
            ...computeM2OProps(this.props),
            otherSources: this.sources,
        };
    }

    get sources() {
        if (!this.props.canCreate) {
            return [];
        }
        return [
            {
                options: (request) => this.loadExternalOptions(request),
                optionSlot: "externalOption",
                placeholder: _t("Buscando en la base externa…"),
            },
        ];
    }

    async loadExternalOptions(request) {
        const query = (request || "").trim();
        // El nombre lo busca Odoo; aquí solo documentos: DNI (8) / RUC (11).
        if (!/^(\d{8}|\d{11})$/.test(query)) {
            return [];
        }
        const suggestions = await this.orm.call(
            "res.partner",
            "l10n_pe_get_field_suggestions",
            [query]
        );
        return suggestions.map((suggestion) => ({
            cssClass: "o_m2o_dropdown_option",
            data: suggestion,
            label: suggestion.label,
            onSelect: () => this.onSelectExternal(suggestion),
        }));
    }

    async onSelectExternal(suggestion) {
        const result = await this.orm.call(
            "res.partner",
            "l10n_pe_create_partner_from_document",
            [suggestion.doc_number]
        );
        if (result) {
            await this.props.record.update({
                [this.props.name]: {
                    id: result.id,
                    display_name: result.display_name,
                },
            });
        }
    }
}

export const partnerLookupMany2one = {
    ...buildM2OFieldDescription(PartnerLookupMany2one),
};

registry.category("fields").add("l10n_pe_partner_lookup_m2o", partnerLookupMany2one);

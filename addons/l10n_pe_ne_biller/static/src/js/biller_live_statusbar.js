import { patch } from "@web/core/utils/patch";
import { FormController } from "@web/views/form/form_controller";
import { useService } from "@web/core/utils/hooks";
import { onWillUnmount } from "@odoo/owl";

/**
 * Statusbar del facturador en vivo: cuando el cron de emisión asíncrona aplica
 * un resultado (worker → DynamoDB → cron), el servidor emite una notificación
 * por el bus (websocket) y esta extensión recarga el registro abierto — el
 * statusbar y "Mensaje Facturador" cambian solos, sin F5.
 *
 * Canal único por BD; el payload trae move_id y solo recargamos si es el
 * registro visible y no está en edición (no pisar cambios sin guardar).
 */
const CHANNEL = "l10n_pe_biller_updates";

patch(FormController.prototype, {
    setup() {
        super.setup(...arguments);
        if (this.props.resModel !== "account.move") {
            return;
        }
        let bus;
        try {
            bus = useService("bus_service");
            bus.addChannel(CHANNEL);
        } catch {
            return; // sin bus (p.ej. tests) → sin live-update, sin romper nada
        }
        const onUpdate = (payload) => {
            try {
                const rec = this.model && this.model.root;
                if (!rec || !payload || payload.move_id !== rec.resId) {
                    return;
                }
                if (rec.dirty) {
                    return; // edición a medias: que gane el usuario
                }
                rec.load();
            } catch {
                /* nunca romper el form por el live-update */
            }
        };
        bus.subscribe("l10n_pe_biller_update", onUpdate);
        onWillUnmount(() => {
            bus.unsubscribe("l10n_pe_biller_update", onUpdate);
            bus.deleteChannel(CHANNEL);
        });
    },
});

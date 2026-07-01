FROM public.ecr.aws/docker/library/odoo:19.0

# Dependencia Python del addon l10n_pe_partner_lookup (modo DynamoDB directo).
# No viene en la imagen base de Odoo. La imagen es Ubuntu 24.04 (PEP 668, entorno
# "externally managed"), de ahí --break-system-packages. Va ANTES del COPY de
# addons para que esta capa quede cacheada aunque cambien los addons.
USER root
RUN pip3 install --no-cache-dir --break-system-packages boto3
USER odoo

# Config adaptada para Docker (reemplaza el odoo.conf del contenedor base)
COPY config/odoo-docker.conf /etc/odoo/odoo.conf

# Addon custom de facturación electrónica PE
COPY addons/ /mnt/extra-addons/

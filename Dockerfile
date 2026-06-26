FROM public.ecr.aws/docker/library/odoo:19.0

# Config adaptada para Docker (reemplaza el odoo.conf del contenedor base)
COPY config/odoo-docker.conf /etc/odoo/odoo.conf

# Addon custom de facturación electrónica PE
COPY addons/ /mnt/extra-addons/

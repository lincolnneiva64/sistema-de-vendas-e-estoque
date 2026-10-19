#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt
python -u manage.py publicar_recolhimento
python manage.py collectstatic --noinput

"""
Creates the database table that backs Django's cache. Rate limits are kept
there so every gunicorn worker shares the same counts. Doing it in a migration
means it can't be forgotten on a new deployment.
"""

from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    call_command("createcachetable", database=schema_editor.connection.alias, verbosity=0)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [migrations.RunPython(create_cache_table, migrations.RunPython.noop)]

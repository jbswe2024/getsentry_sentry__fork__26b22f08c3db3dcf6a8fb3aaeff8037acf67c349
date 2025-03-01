import django
from django.db import migrations

import sentry
from sentry.new_migrations.migrations import CheckedMigration
from sentry.new_migrations.monkey.fields import SafeRemoveField
from sentry.new_migrations.monkey.state import DeletionAction


class Migration(CheckedMigration):
    atomic = False

    dependencies = [
        ("good_flow_delete_field_pending_with_fk_constraint_app", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="TestTable",
            name="fk_table",
            field=sentry.db.models.fields.foreignkey.FlexibleForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                to="good_flow_delete_field_pending_with_fk_constraint_app.fktable",
                db_index=False,
                db_constraint=False,
                null=True,
            ),
        ),
        SafeRemoveField(
            model_name="testtable",
            name="fk_table",
            deletion_action=DeletionAction.MOVE_TO_PENDING,
        ),
    ]

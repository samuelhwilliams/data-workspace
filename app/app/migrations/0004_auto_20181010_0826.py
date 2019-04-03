# Generated by Django 2.1.2 on 2018-10-10 08:26

from django.conf import settings
import django.core.validators
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('app', '0003_auto_20181009_1028'),
    ]

    operations = [
        migrations.CreateModel(
            name='Privilage',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_date', models.DateTimeField(auto_now_add=True)),
                ('modified_date', models.DateTimeField(auto_now=True)),
                ('tables', models.CharField(help_text='Comma-separated list of tables that can be accessed on this database. "ALL TABLES" (without quotes) to allow access to all tables', max_length=1024, validators=[django.core.validators.RegexValidator(regex='(^([a-z][a-z0-9_]*,?)+(?<!,)$)|(^ALL TABLES$)')])),
            ],
        ),
        migrations.AddField(
            model_name='database',
            name='is_public',
            field=models.BooleanField(default=False, help_text='If public, the same credentials for the database will be shared with each user. If not public, each user must be explicilty given access, and temporary credentials will be created for each.'),
        ),
        migrations.AddField(
            model_name='privilage',
            name='database',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='app.Database'),
        ),
        migrations.AddField(
            model_name='privilage',
            name='user',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddIndex(
            model_name='privilage',
            index=models.Index(fields=['user'], name='app_privila_user_id_3e8176_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='privilage',
            unique_together={('user', 'database')},
        ),
    ]
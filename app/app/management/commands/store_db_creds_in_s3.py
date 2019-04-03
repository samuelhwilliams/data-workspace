import hashlib

import boto3

from django.conf import (
    settings,
)
from django.contrib.auth import (
    get_user_model,
)
from django.core.management.base import (
    BaseCommand,
)

from app.shared import (
    new_private_database_credentials,
)


class Command(BaseCommand):
    help = 'Creates and saves DB credentials for all SSO users'

    def handle(self, *args, **options):
        self.stdout.write('store_db_creds_in_s3 started')

        bucket = settings.NOTEBOOKS_BUCKET
        self.stdout.write('Will store credentials in bucket {}'.format(bucket))

        s3_client = boto3.client('s3')

        User = get_user_model()
        all_users = User.objects.order_by('last_name', 'first_name', 'id')
        for user in all_users:
            self.stdout.write(f'Creating credentials for {user.email}')
            creds = new_private_database_credentials(user)
            self.stdout.write(str(creds))

        self.stdout.write(self.style.SUCCESS('store_db_creds_in_s3 finished'))
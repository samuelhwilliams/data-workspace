import logging
import urllib.parse

from django.conf import settings

from zenpy import Zenpy
from zenpy.lib.api_objects import Ticket, User, Comment, CustomField

logger = logging.getLogger('app')

zendesk_service_field_id = settings.ZENDESK_SERVICE_FIELD_ID
zendesk_service_field_value = settings.ZENDESK_SERVICE_FIELD_VALUE


def get_username(user):
    return f'{user.first_name} {user.last_name}'


def get_people_url(name):
    return "https://people.trade.gov.uk/search?search_filters[]=people&query={}".format(
        urllib.parse.quote(name)
    )


def build_ticket_description_text(
    dataset_name, dataset_url, contact_email, user, goal_text
):
    username = get_username(user)
    people_url = get_people_url(username)
    ticket_description = f"""Access request for
{dataset_name}
{dataset_url}

Username:   {username}
Contact:    {contact_email}
SSO Login:  {user.email}
People search: {people_url}


Why do you need this data?

{goal_text}

"""

    logger.debug(ticket_description)
    return ticket_description


def build_private_comment_text(
    information_asset_owner, information_asset_manager, approval_url
):
    asset_owner_text = 'None'
    asset_manager_text = 'None'

    if information_asset_owner:
        asset_owner_text = f'{information_asset_owner.first_name} {information_asset_owner.last_name} <{information_asset_owner.email}>'

    if information_asset_manager:
        asset_owner_text = f'{information_asset_manager.first_name} {information_asset_manager.last_name} <{information_asset_manager.email}>'

    private_comment = f"""

Information Asset Owner: {asset_owner_text}
Information Asset Manager: {asset_manager_text}

You can approve this request here
{approval_url}
    """

    logger.debug(private_comment)

    return private_comment


def create_zendesk_ticket(
    contact_email,
    user,
    goal,
    approval_url,
    dataset_name,
    dataset_url,
    information_asset_owner,  # nb this can be null
    information_asset_manager,  # so can this
):
    client = Zenpy(
        subdomain=settings.ZENDESK_SUBDOMAIN,
        email=settings.ZENDESK_EMAIL,
        token=settings.ZENDESK_TOKEN,
    )

    ticket_description = build_ticket_description_text(
        dataset_name, dataset_url, contact_email, user, goal
    )

    private_comment = build_private_comment_text(
        information_asset_owner, information_asset_manager, approval_url
    )

    username = get_username(user)
    subject = f'Access Request for {dataset_name}'

    ticket_audit = client.tickets.create(
        Ticket(
            subject=subject,
            description=ticket_description,
            requester=User(email=contact_email, name=username),
            custom_fields=[
                CustomField(
                    id=zendesk_service_field_id, value=zendesk_service_field_value
                )
            ],
        )
    )

    ticket_audit.ticket.comment = Comment(body=private_comment, public=False)
    client.tickets.update(ticket_audit.ticket)

    return ticket_audit.ticket.id


def update_zendesk_ticket(ticket_id, comment=None, status=None):
    client = Zenpy(
        subdomain=settings.ZENDESK_SUBDOMAIN,
        email=settings.ZENDESK_EMAIL,
        token=settings.ZENDESK_TOKEN,
    )

    ticket = client.tickets(id=ticket_id)

    if comment:
        ticket.comment = Comment(body=comment, public=False)

    if status:
        ticket.status = status

    client.tickets.update(ticket)

    return ticket


def create_support_request(user, email, message, tag=None, subject=None):
    client = Zenpy(
        subdomain=settings.ZENDESK_SUBDOMAIN,
        email=settings.ZENDESK_EMAIL,
        token=settings.ZENDESK_TOKEN,
    )
    ticket_audit = client.tickets.create(
        Ticket(
            subject=subject or 'Data Workspace Support Request',
            description=message,
            requester=User(email=email, name=user.get_full_name()),
            tags=[tag] if tag else None,
            custom_fields=[
                CustomField(
                    id=zendesk_service_field_id, value=zendesk_service_field_value
                )
            ],
        )
    )
    return ticket_audit.ticket.id

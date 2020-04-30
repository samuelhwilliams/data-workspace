import asyncio
import base64
import hmac
import ipaddress
import json
import logging
import os
import random
import secrets
import sys
import string
import urllib

import aiohttp
from aiohttp import web

import aioredis
from hawkserver import authenticate_hawk_header
from multidict import CIMultiDict
from yarl import URL

from dataworkspace.utils import normalise_environment
from proxy_session import SESSION_KEY, redis_session_middleware


class UserException(Exception):
    pass


class ContextAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f'[{self.extra["context"]}] {msg}', kwargs


PROFILE_CACHE_PREFIX = 'data_workspace_profile'
CONTEXT_ALPHABET = string.ascii_letters + string.digits


async def async_main():
    stdout_handler = logging.StreamHandler(sys.stdout)
    for logger_name in ['aiohttp.server', 'aiohttp.web', 'aiohttp.access', 'proxy']:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        logger.addHandler(stdout_handler)

    env = normalise_environment(os.environ)
    port = int(env['PROXY_PORT'])
    admin_root = env['UPSTREAM_ROOT']
    hawk_senders = env['HAWK_SENDERS']
    sso_base_url = env['AUTHBROKER_URL']
    sso_client_id = env['AUTHBROKER_CLIENT_ID']
    sso_client_secret = env['AUTHBROKER_CLIENT_SECRET']
    redis_url = env['REDIS_URL']
    root_domain = env['APPLICATION_ROOT_DOMAIN']
    basic_auth_user = env['METRICS_SERVICE_DISCOVERY_BASIC_AUTH_USER']
    basic_auth_password = env['METRICS_SERVICE_DISCOVERY_BASIC_AUTH_PASSWORD']
    x_forwarded_for_trusted_hops = int(env['X_FORWARDED_FOR_TRUSTED_HOPS'])
    application_ip_whitelist = env['APPLICATION_IP_WHITELIST']
    mirror_remote_root = env['MIRROR_REMOTE_ROOT']
    mirror_local_root = '/__mirror/'

    root_domain_no_port, _, root_port_str = root_domain.partition(':')
    try:
        root_port = int(root_port_str)
    except ValueError:
        root_port = None

    csp_common = "object-src 'none';"
    if root_domain not in ['dataworkspace.test:8000']:
        csp_common += 'upgrade-insecure-requests;'

    # A spawning application on <my-application>.<root_domain> shows the admin-styled site,
    # fetching assets from <root_domain>, but also makes requests to the current domain
    csp_application_spawning = csp_common + (
        f'default-src {root_domain};'
        f'base-uri {root_domain};'
        f'font-src {root_domain} data:;'
        f'form-action {root_domain} *.{root_domain};'
        f'frame-ancestors {root_domain};'
        f'img-src {root_domain} data: https://www.googletagmanager.com https://www.google-analytics.com;'
        f"script-src 'unsafe-inline' {root_domain} https://www.googletagmanager.com https://www.google-analytics.com;"
        f"style-src 'unsafe-inline' {root_domain};"
        f"connect-src {root_domain} 'self';"
    )

    # A running application should only connect to self: this is where we have the most
    # concern because we run the least-trusted code
    def csp_application_running_direct(host):
        return csp_common + (
            "default-src 'self';"
            "base-uri 'self';"
            # Safari does not have a 'self' for WebSockets
            f"connect-src 'self' wss://{host};"
            "font-src 'self' data:;"
            "form-action 'self';"
            "frame-ancestors 'self';"
            "img-src 'self' data: blob:;"
            # Both JupyterLab and RStudio need `unsafe-eval`
            "script-src 'unsafe-inline' 'unsafe-eval' 'self';"
            "style-src 'unsafe-inline' 'self';"
        )

    redis_pool = await aioredis.create_redis_pool(redis_url)

    default_http_timeout = aiohttp.ClientTimeout()

    # When spawning and tring to detect if the app is running,
    # we fail quickly and often so a connection check is quick
    spawning_http_timeout = aiohttp.ClientTimeout(sock_read=5, sock_connect=2)

    def get_random_context_logger():
        return ContextAdapter(
            logger, {'context': ''.join(random.choices(CONTEXT_ALPHABET, k=8))}
        )

    def without_transfer_encoding(request_or_response):
        return tuple(
            (key, value)
            for key, value in request_or_response.headers.items()
            if key.lower() != 'transfer-encoding'
        )

    def admin_headers(downstream_request):
        return (
            without_transfer_encoding(downstream_request)
            + downstream_request['sso_profile_headers']
        )

    def mirror_headers(downstream_request):
        return tuple(
            (key, value)
            for key, value in downstream_request.headers.items()
            if key.lower() not in ['host', 'transfer-encoding']
        )

    def application_headers(downstream_request):
        return without_transfer_encoding(downstream_request) + (
            (('x-scheme', downstream_request.headers['x-forwarded-proto']),)
            if 'x-forwarded-proto' in downstream_request.headers
            else ()
        )

    def is_service_discovery(request):
        return (
            request.url.path == '/api/v1/application'
            and request.url.host == root_domain_no_port
            and request.method == 'GET'
        )

    def is_app_requested(request):
        return request.url.host.endswith(
            f'.{root_domain_no_port}'
        ) and not request.url.path.startswith(mirror_local_root)

    def is_mirror_requested(request):
        return request.url.host.endswith(
            f'.{root_domain_no_port}'
        ) and request.url.path.startswith(mirror_local_root)

    def is_requesting_credentials(request):
        return (
            request.url.host == root_domain_no_port
            and request.url.path == '/api/v1/aws_credentials'
        )

    def is_requesting_files(request):
        return request.url.host == root_domain_no_port and request.url.path == '/files'

    def is_dataset_requested(request):
        return (
            request.url.path.startswith('/api/v1/dataset/')
            or request.url.path.startswith('/api/v1/reference-dataset/')
            or request.url.path.startswith('/api/v1/eventlog/')
            or request.url.path.startswith('/api/v1/accounts/')
            or request.url.path.startswith('/api/v1/application-instance/')
            and request.url.host == root_domain_no_port
        )

    def is_hawk_auth_required(request):
        return is_dataset_requested(request)

    def is_healthcheck_requested(request):
        return (
            request.url.path == '/healthcheck'
            and request.method == 'GET'
            and not is_app_requested(request)
        )

    def is_table_requested(request):
        return (
            request.url.path.startswith('/api/v1/table/')
            and request.url.host == root_domain_no_port
            and request.method == 'POST'
        )

    def is_sso_auth_required(request):
        return (
            not is_healthcheck_requested(request)
            and not is_service_discovery(request)
            and not is_table_requested(request)
            and not is_dataset_requested(request)
        )

    def get_peer_ip(request):
        peer_ip = (
            request.headers['x-forwarded-for']
            .split(',')[-x_forwarded_for_trusted_hops]
            .strip()
        )

        is_private = True
        try:
            is_private = ipaddress.ip_address(peer_ip).is_private
        except ValueError:
            is_private = False

        return peer_ip, is_private

    def request_scheme(request):
        return request.headers.get('x-forwarded-proto', request.url.scheme)

    def request_url(request):
        return str(request.url.with_scheme(request_scheme(request)))

    async def handle(downstream_request):
        method = downstream_request.method
        path = downstream_request.url.path
        query = downstream_request.url.query
        app_requested = is_app_requested(downstream_request)
        mirror_requested = is_mirror_requested(downstream_request)

        # Websocket connections
        # - tend to close unexpectedly, both from the client and app
        # - don't need to show anything nice to the user on error
        is_websocket = (
            downstream_request.headers.get('connection', '').lower() == 'upgrade'
            and downstream_request.headers.get('upgrade', '').lower() == 'websocket'
        )

        try:
            if app_requested:
                return await handle_application(
                    is_websocket, downstream_request, method, path, query
                )
            if mirror_requested:
                return await handle_mirror(downstream_request, method, path)
            return await handle_admin(downstream_request, method, path, query)
        except Exception as exception:
            logger.exception(
                'Exception during %s %s %s',
                downstream_request.method,
                downstream_request.url,
                type(exception),
            )

            if is_websocket:
                raise

            params = (
                {'message': exception.args[0]}
                if isinstance(exception, UserException)
                else {}
            )

            status = exception.args[1] if isinstance(exception, UserException) else 500

            return await handle_http(
                downstream_request,
                'GET',
                CIMultiDict(admin_headers(downstream_request)),
                URL(admin_root).with_path(f'/error_{status}'),
                params,
                default_http_timeout,
            )

    async def handle_application(is_websocket, downstream_request, method, path, query):
        public_host, _, _ = downstream_request.url.host.partition(
            f'.{root_domain_no_port}'
        )
        possible_public_host, _, public_host_or_port_override = public_host.rpartition(
            '--'
        )
        try:
            port_override = int(public_host_or_port_override)
        except ValueError:
            port_override = None
        else:
            public_host = possible_public_host
        host_api_url = admin_root + '/api/v1/application/' + public_host
        host_html_path = '/tools/' + public_host

        async with client_session.request(
            'GET', host_api_url, headers=CIMultiDict(admin_headers(downstream_request))
        ) as response:
            host_exists = response.status == 200
            application = await response.json()

        if response.status != 200 and response.status != 404:
            raise UserException('Unable to start the application', response.status)

        if host_exists and application['state'] not in ['SPAWNING', 'RUNNING']:
            if (
                'x-data-workspace-no-modify-application-instance'
                not in downstream_request.headers
            ):
                async with client_session.request(
                    'DELETE',
                    host_api_url,
                    headers=CIMultiDict(admin_headers(downstream_request)),
                ) as delete_response:
                    await delete_response.read()
            raise UserException('Application ' + application['state'], 500)

        if not host_exists:
            if (
                'x-data-workspace-no-modify-application-instance'
                not in downstream_request.headers
            ):
                params = {
                    key: value
                    for key, value in downstream_request.query.items()
                    if key == '__memory_cpu'
                }
                async with client_session.request(
                    'PUT',
                    host_api_url,
                    params=params,
                    headers=CIMultiDict(admin_headers(downstream_request)),
                ) as response:
                    host_exists = response.status == 200
                    application = await response.json()
                if params:
                    return web.Response(status=302, headers={'location': '/'})
            else:
                raise UserException('Application stopped while starting', 500)

        if response.status != 200:
            raise UserException('Unable to start the application', response.status)

        if application['state'] not in ['SPAWNING', 'RUNNING']:
            raise UserException(
                'Attempted to start the application, but it ' + application['state'],
                500,
            )

        if not application['proxy_url']:
            return await handle_http(
                downstream_request,
                'GET',
                CIMultiDict(admin_headers(downstream_request)),
                admin_root + host_html_path + '/spawning',
                {},
                default_http_timeout,
                (('content-security-policy', csp_application_spawning),),
            )

        if is_websocket:
            return await handle_application_websocket(
                downstream_request, application['proxy_url'], path, query, port_override
            )

        if application['state'] == 'SPAWNING':
            return await handle_application_http_spawning(
                downstream_request,
                method,
                application_upstream(application['proxy_url'], path, port_override),
                query,
                host_html_path,
                host_api_url,
            )

        return await handle_application_http_running_direct(
            downstream_request,
            method,
            application_upstream(application['proxy_url'], path, port_override),
            query,
        )

    async def handle_application_websocket(
        downstream_request, proxy_url, path, query, port_override
    ):
        upstream_url = application_upstream(proxy_url, path, port_override).with_query(
            query
        )
        return await handle_websocket(
            downstream_request,
            CIMultiDict(application_headers(downstream_request)),
            upstream_url,
        )

    def application_upstream(proxy_url, path, port_override):
        return (
            URL(proxy_url).with_path(path)
            if port_override is None
            else URL(proxy_url).with_path(path).with_port(port_override)
        )

    async def handle_application_http_spawning(
        downstream_request, method, upstream_url, query, host_html_path, host_api_url
    ):
        host = downstream_request.headers['host']
        try:
            logger.info('Spawning: Attempting to connect to %s', upstream_url)
            response = await handle_http(
                downstream_request,
                method,
                CIMultiDict(application_headers(downstream_request)),
                upstream_url,
                query,
                spawning_http_timeout,
                # Although the application is spawning, if the response makes it back to the client,
                # we know the application is running, so we return the _running_ CSP headers
                (('content-security-policy', csp_application_running_direct(host)),),
            )

        except Exception:
            logger.info('Spawning: Failed to connect to %s', upstream_url)
            return await handle_http(
                downstream_request,
                'GET',
                CIMultiDict(admin_headers(downstream_request)),
                admin_root + host_html_path + '/spawning',
                {},
                default_http_timeout,
                (('content-security-policy', csp_application_spawning),),
            )

        else:
            # Once a streaming response is done, if we have not yet returned
            # from the handler, it looks like aiohttp can cancel the current
            # task. We set RUNNING in another task to avoid it being cancelled
            async def set_application_running():
                async with client_session.request(
                    'PATCH',
                    host_api_url,
                    json={'state': 'RUNNING'},
                    headers=CIMultiDict(admin_headers(downstream_request)),
                    timeout=default_http_timeout,
                ) as patch_response:
                    await patch_response.read()

            asyncio.ensure_future(set_application_running())

            return response

    async def handle_application_http_running_direct(
        downstream_request, method, upstream_url, query
    ):
        host = downstream_request.headers['host']
        return await handle_http(
            downstream_request,
            method,
            CIMultiDict(application_headers(downstream_request)),
            upstream_url,
            query,
            default_http_timeout,
            (('content-security-policy', csp_application_running_direct(host)),),
        )

    async def handle_mirror(downstream_request, method, path):
        mirror_path = path[len(mirror_local_root) :]
        upstream_url = URL(mirror_remote_root + mirror_path)
        return await handle_http(
            downstream_request,
            method,
            CIMultiDict(mirror_headers(downstream_request)),
            upstream_url,
            {},
            default_http_timeout,
        )

    async def handle_admin(downstream_request, method, path, query):
        upstream_url = URL(admin_root).with_path(path)
        return await handle_http(
            downstream_request,
            method,
            CIMultiDict(admin_headers(downstream_request)),
            upstream_url,
            query,
            default_http_timeout,
        )

    async def handle_websocket(downstream_request, upstream_headers, upstream_url):
        protocol = downstream_request.headers.get('Sec-WebSocket-Protocol')
        protocols = (protocol,) if protocol else ()

        async def proxy_msg(msg, to_ws):
            if msg.type == aiohttp.WSMsgType.TEXT:
                await to_ws.send_str(msg.data)

            elif msg.type == aiohttp.WSMsgType.BINARY:
                await to_ws.send_bytes(msg.data)

            elif msg.type == aiohttp.WSMsgType.CLOSE:
                await to_ws.close()

            elif msg.type == aiohttp.WSMsgType.ERROR:
                await to_ws.close()

        async def upstream():
            try:
                async with client_session.ws_connect(
                    str(upstream_url), headers=upstream_headers, protocols=protocols
                ) as upstream_ws:
                    upstream_connection.set_result(upstream_ws)
                    downstream_ws = await downstream_connection
                    async for msg in upstream_ws:
                        await proxy_msg(msg, downstream_ws)
            except BaseException as exception:
                if not upstream_connection.done():
                    upstream_connection.set_exception(exception)
                raise
            finally:
                await downstream_ws.close()

        # This is slightly convoluted, but aiohttp documents that reading
        # from websockets should be done in the same task as the websocket was
        # created, so we read from downstream in _this_ task, and create
        # another task to connect to and read from the upstream socket. We
        # also need to make sure we wait for each connection before sending
        # data to it
        downstream_connection = asyncio.Future()
        upstream_connection = asyncio.Future()
        upstream_task = asyncio.ensure_future(upstream())

        try:
            upstream_ws = await upstream_connection
            _, _, _, with_session_cookie = downstream_request[SESSION_KEY]
            downstream_ws = await with_session_cookie(
                web.WebSocketResponse(protocols=protocols)
            )

            await downstream_ws.prepare(downstream_request)
            downstream_connection.set_result(downstream_ws)

            async for msg in downstream_ws:
                await proxy_msg(msg, upstream_ws)
        finally:
            upstream_task.cancel()

        return downstream_ws

    async def handle_http(
        downstream_request,
        upstream_method,
        upstream_headers,
        upstream_url,
        upstream_query,
        timeout,
        response_headers=tuple(),
    ):
        # Avoid aiohttp treating request as chunked unnecessarily, which works
        # for some upstream servers, but not all. Specifically RStudio drops
        # GET responses half way through if the request specified a chunked
        # encoding. AFAIK RStudio uses a custom webserver, so this behaviour
        # is not documented anywhere.

        # fmt: off
        data = \
            b'' if (
                'content-length' not in upstream_headers
                and downstream_request.headers.get('transfer-encoding', '').lower() != 'chunked'
            ) else \
            await downstream_request.read() if downstream_request.content.at_eof() else \
            downstream_request.content
        # fmt: on

        async with client_session.request(
            upstream_method,
            str(upstream_url),
            params=upstream_query,
            headers=upstream_headers,
            data=data,
            allow_redirects=False,
            timeout=timeout,
        ) as upstream_response:

            _, _, _, with_session_cookie = downstream_request[SESSION_KEY]
            downstream_response = await with_session_cookie(
                web.StreamResponse(
                    status=upstream_response.status,
                    headers=CIMultiDict(
                        without_transfer_encoding(upstream_response) + response_headers
                    ),
                )
            )
            await downstream_response.prepare(downstream_request)
            async for chunk in upstream_response.content.iter_any():
                await downstream_response.write(chunk)

        return downstream_response

    def server_logger():
        @web.middleware
        async def _server_logger(request, handler):

            request_logger = get_random_context_logger()
            request['logger'] = request_logger
            url = request_url(request)

            request_logger.info(
                'Receiving (%s) (%s) (%s) (%s)',
                request.method,
                url,
                request.headers.get('User-Agent', '-'),
                request.headers.get('X-Forwarded-For', '-'),
            )

            response = await handler(request)

            request_logger.info(
                'Responding (%s) (%s) (%s) (%s) (%s) (%s)',
                request.method,
                url,
                request.headers.get('User-Agent', '-'),
                request.headers.get('X-Forwarded-For', '-'),
                response.status,
                response.content_length,
            )

            return response

        return _server_logger

    def authenticate_by_staff_sso_token():

        me_path = 'api/v1/user/me/'

        @web.middleware
        async def _authenticate_by_staff_sso_token(request, handler):
            staff_sso_token_required = is_table_requested(request)
            request.setdefault('sso_profile_headers', ())

            if not staff_sso_token_required:
                return await handler(request)

            if 'Authorization' not in request.headers:
                request['logger'].info(
                    'SSO-token unathenticated: missing authorization header'
                )
                return await handle_admin(request, 'GET', '/error_403', {})

            async with client_session.get(
                f'{sso_base_url}{me_path}',
                headers={'Authorization': request.headers['Authorization']},
            ) as me_response:
                me_profile = (
                    await me_response.json() if me_response.status == 200 else None
                )

            if not me_profile:
                request['logger'].info(
                    'SSO-token unathenticated: bad authorization header'
                )
                return await handle_admin(request, 'GET', '/error_403', {})

            request['sso_profile_headers'] = (
                ('sso-profile-email', me_profile['email']),
                (
                    'sso-profile-related-emails',
                    ','.join(me_profile.get('related_emails', [])),
                ),
                ('sso-profile-user-id', me_profile['user_id']),
                ('sso-profile-first-name', me_profile['first_name']),
                ('sso-profile-last-name', me_profile['last_name']),
            )

            request['logger'].info(
                'SSO-token authenticated: %s %s',
                me_profile['email'],
                me_profile['user_id'],
            )

            return await handler(request)

        return _authenticate_by_staff_sso_token

    def authenticate_by_staff_sso():

        auth_path = 'o/authorize/'
        token_path = 'o/token/'
        me_path = 'api/v1/user/me/'
        grant_type = 'authorization_code'
        scope = 'read write'
        response_type = 'code'

        redirect_from_sso_path = '/__redirect_from_sso'
        session_token_key = 'staff_sso_access_token'

        async def get_redirect_uri_authenticate(set_session_value, redirect_uri_final):
            scheme = URL(redirect_uri_final).scheme
            sso_state = await set_redirect_uri_final(
                set_session_value, redirect_uri_final
            )

            redirect_uri_callback = urllib.parse.quote(
                get_redirect_uri_callback(scheme), safe=''
            )
            return (
                f'{sso_base_url}{auth_path}?'
                f'scope={scope}&state={sso_state}&'
                f'redirect_uri={redirect_uri_callback}&'
                f'response_type={response_type}&'
                f'client_id={sso_client_id}'
            )

        def get_redirect_uri_callback(scheme):
            return str(
                URL.build(
                    host=root_domain_no_port,
                    port=root_port,
                    scheme=scheme,
                    path=redirect_from_sso_path,
                )
            )

        async def set_redirect_uri_final(set_session_value, redirect_uri_final):
            session_key = secrets.token_hex(32)
            sso_state = urllib.parse.quote(
                f'{session_key}_{redirect_uri_final}', safe=''
            )

            await set_session_value(session_key, redirect_uri_final)

            return sso_state

        async def get_redirect_uri_final(get_session_value, sso_state):
            session_key, _, state_redirect_url = urllib.parse.unquote(
                sso_state
            ).partition('_')
            return state_redirect_url, await get_session_value(session_key)

        async def redirection_to_sso(
            with_new_session_cookie, set_session_value, redirect_uri_final
        ):
            return await with_new_session_cookie(
                web.Response(
                    status=302,
                    headers={
                        'Location': await get_redirect_uri_authenticate(
                            set_session_value, redirect_uri_final
                        )
                    },
                )
            )

        @web.middleware
        async def _authenticate_by_sso(request, handler):
            sso_auth_required = is_sso_auth_required(request)

            if not sso_auth_required:
                request.setdefault('sso_profile_headers', ())
                return await handler(request)

            get_session_value, set_session_value, with_new_session_cookie, _ = request[
                SESSION_KEY
            ]

            token = await get_session_value(session_token_key)
            if request.path != redirect_from_sso_path and token is None:
                return await redirection_to_sso(
                    with_new_session_cookie, set_session_value, request_url(request)
                )

            if request.path == redirect_from_sso_path:
                code = request.query['code']
                sso_state = request.query['state']
                (
                    redirect_uri_final_from_url,
                    redirect_uri_final_from_session,
                ) = await get_redirect_uri_final(get_session_value, sso_state)

                if redirect_uri_final_from_url != redirect_uri_final_from_session:
                    # We might have been overtaken by a parallel request initiating another auth
                    # flow, and so another session. However, because we haven't retrieved the final
                    # URL from the session, we can't be sure that this is the same client that
                    # initiated this flow. However, we can redirect back to SSO
                    return await redirection_to_sso(
                        with_new_session_cookie,
                        set_session_value,
                        redirect_uri_final_from_url,
                    )

                async with client_session.post(
                    f'{sso_base_url}{token_path}',
                    data={
                        'grant_type': grant_type,
                        'code': code,
                        'client_id': sso_client_id,
                        'client_secret': sso_client_secret,
                        'redirect_uri': get_redirect_uri_callback(
                            request_scheme(request)
                        ),
                    },
                ) as sso_response:
                    sso_response_json = await sso_response.json()
                await set_session_value(
                    session_token_key, sso_response_json['access_token']
                )
                return await with_new_session_cookie(
                    web.Response(
                        status=302,
                        headers={'Location': redirect_uri_final_from_session},
                    )
                )

            # Get profile from Redis cache to avoid calling SSO on every request
            redis_profile_key = f'{PROFILE_CACHE_PREFIX}___{session_token_key}___{token}'.encode(
                'ascii'
            )
            with await redis_pool as conn:
                me_profile_raw = await conn.execute('GET', redis_profile_key)
            me_profile = json.loads(me_profile_raw) if me_profile_raw else None

            async def handler_with_sso_headers():
                request['sso_profile_headers'] = (
                    ('sso-profile-email', me_profile['email']),
                    (
                        'sso-profile-related-emails',
                        ','.join(me_profile.get('related_emails', [])),
                    ),
                    ('sso-profile-user-id', me_profile['user_id']),
                    ('sso-profile-first-name', me_profile['first_name']),
                    ('sso-profile-last-name', me_profile['last_name']),
                )

                request['logger'].info(
                    'SSO-authenticated: %s %s %s',
                    me_profile['email'],
                    me_profile['user_id'],
                    request_url(request),
                )

                return await handler(request)

            if me_profile:
                return await handler_with_sso_headers()

            async with client_session.get(
                f'{sso_base_url}{me_path}', headers={'Authorization': f'Bearer {token}'}
            ) as me_response:
                me_profile_full = (
                    await me_response.json() if me_response.status == 200 else None
                )

            if not me_profile_full:
                return await redirection_to_sso(
                    with_new_session_cookie, set_session_value, request_url(request)
                )

            me_profile = {
                'email': me_profile_full['email'],
                'related_emails': me_profile_full['related_emails'],
                'user_id': me_profile_full['user_id'],
                'first_name': me_profile_full['first_name'],
                'last_name': me_profile_full['last_name'],
            }
            with await redis_pool as conn:
                await conn.execute(
                    'SET',
                    redis_profile_key,
                    json.dumps(me_profile).encode('utf-8'),
                    'EX',
                    60,
                )

            return await handler_with_sso_headers()

        return _authenticate_by_sso

    def authenticate_by_basic_auth():
        @web.middleware
        async def _authenticate_by_basic_auth(request, handler):
            basic_auth_required = is_service_discovery(request)

            if not basic_auth_required:
                return await handler(request)

            if 'Authorization' not in request.headers:
                return web.Response(status=401)

            basic_auth_prefix = 'Basic '
            auth_value = (
                request.headers['Authorization'][len(basic_auth_prefix) :]
                .strip()
                .encode('ascii')
            )
            required_auth_value = base64.b64encode(
                f'{basic_auth_user}:{basic_auth_password}'.encode('ascii')
            )

            if len(auth_value) != len(required_auth_value) or not hmac.compare_digest(
                auth_value, required_auth_value
            ):
                return web.Response(status=401)

            request['logger'].info('Basic-authenticated: %s', basic_auth_user)

            return await handler(request)

        return _authenticate_by_basic_auth

    def authenticate_by_hawk_auth():
        async def lookup_credentials(sender_id):
            for hawk_sender in hawk_senders:
                if hawk_sender['id'] == sender_id:
                    return hawk_sender

        async def seen_nonce(nonce, sender_id):
            nonce_key = f'nonce-{sender_id}-{nonce}'
            with await redis_pool as conn:
                response = await conn.execute('SET', nonce_key, '1', 'EX', 60, 'NX')
                seen_nonce = response != b'OK'
                return seen_nonce

        @web.middleware
        async def _authenticate_by_hawk_auth(request, handler):
            hawk_auth_required = is_hawk_auth_required(request)

            if not hawk_auth_required:
                return await handler(request)

            try:
                authorization_header = request.headers['Authorization']
            except KeyError:
                request['logger'].info('Hawk missing header')
                return web.Response(status=401)

            content = await request.read()

            is_authenticated, error_message, creds = await authenticate_hawk_header(
                lookup_credentials,
                seen_nonce,
                15,
                authorization_header,
                request.method,
                request.url.host,
                request.url.port,
                request.url.path_qs,
                request.headers['Content-Type'],
                content,
            )
            if not is_authenticated:
                request['logger'].info('Hawk unauthenticated: %s', error_message)
                return web.Response(status=401)

            request['logger'].info('Hawk authenticated: %s', creds['id'])

            return await handler(request)

        return _authenticate_by_hawk_auth

    def authenticate_by_ip_whitelist():
        @web.middleware
        async def _authenticate_by_ip_whitelist(request, handler):
            ip_whitelist_required = (
                is_app_requested(request)
                or is_mirror_requested(request)
                or is_requesting_credentials(request)
                or is_requesting_files(request)
            )

            if not ip_whitelist_required:
                return await handler(request)

            peer_ip, _ = get_peer_ip(request)
            peer_ip_in_whitelist = any(
                ipaddress.IPv4Address(peer_ip)
                in ipaddress.IPv4Network(address_or_subnet)
                for address_or_subnet in application_ip_whitelist
            )

            if not peer_ip_in_whitelist:
                request['logger'].info('IP-whitelist unauthenticated: %s', peer_ip)
                return await handle_admin(request, 'GET', '/error_403', {})

            request['logger'].info('IP-whitelist authenticated: %s', peer_ip)
            return await handler(request)

        return _authenticate_by_ip_whitelist

    async with aiohttp.ClientSession(
        auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar()
    ) as client_session:
        app = web.Application(
            middlewares=[
                server_logger(),
                redis_session_middleware(redis_pool, root_domain_no_port),
                authenticate_by_staff_sso_token(),
                authenticate_by_staff_sso(),
                authenticate_by_basic_auth(),
                authenticate_by_hawk_auth(),
                authenticate_by_ip_whitelist(),
            ]
        )
        app.add_routes(
            [
                getattr(web, method)(r'/{path:.*}', handle)
                for method in [
                    'delete',
                    'get',
                    'head',
                    'options',
                    'patch',
                    'post',
                    'put',
                ]
            ]
        )

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        await asyncio.Future()


def main():
    loop = asyncio.get_event_loop()
    loop.run_until_complete(async_main())


if __name__ == '__main__':
    main()

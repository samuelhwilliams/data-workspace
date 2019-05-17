''' Addition-only temporary Redis session without race conditions

The Redis storage that comes with aiohttp-session has a race condition when
there are multiple requests, which can happen even when favicon.ico is
requested concurrently.

- Request 1 comes in, fetches the JSON encoded session dict from Redis
- Request 2 comes in, fetches the JSON encoded session dict from Redis
...
- Both requests change and save their sessions to their local dicts
...
- Request 1 completes, JSON encoding its dict and saves to Redis
- Request 2 completes, JSON encoding its dict and saves to Redis, overwriting
  the one from Request 1

Note, we don't need to

- remove keys (other than "eventually" to not keep them in Redis forever);
- save anything other than ascii strings;
- specfically, no need for a deep structure;
- get a list of all keys in session;
- read-back a value once set (in the same request).

So we use SET and GET to manually keep a "dict" in Redis that allows multiple
requests to add keys concurrently
'''

import secrets
import time

from aiohttp import (
    web,
)
from multidict import (
    CIMultiDict,
)


COOKIE_NAME = 'analysis_workspace_proxy_session'
COOKIE_MAX_AGE = 60 * 60 * 10

REDIS_KEY_PREFIX = 'cookie___analysis_workspace_proxy_session'
REDIS_MAX_AGE = 60 * 60 * 9

SESSION_KEY = 'SESSION'


def redis_session_middleware(redis_pool):

    @web.middleware
    async def _redis_session_middleware(request, handler):
        cookie_value = request.cookies.get(COOKIE_NAME)
        to_set = {}

        async def get_value(key):
            if not cookie_value:
                return None

            with await redis_pool as conn:
                redis_key = f'{REDIS_KEY_PREFIX}___{cookie_value}___{key}'.encode('ascii')
                raw = await conn.execute('GET', redis_key)
            return \
                raw.decode('ascii') if raw is not None else \
                None

        async def set_value(key, value):
            to_set[key] = value

        # Cookies have to be explicitly set on the response _before_ they are
        # prepared, which is done explicitly for streaming responses before
        # the handler returns

        async def with_new_cookie(response):
            nonlocal cookie_value
            cookie_value = secrets.token_urlsafe(64)
            return await with_cookie(response)

        async def with_cookie(response):
            nonlocal cookie_value

            if not cookie_value:
                cookie_value = secrets.token_urlsafe(64)

            if to_set:
                with await redis_pool as conn:
                    for key, value in to_set.items():
                        redis_key = f'{REDIS_KEY_PREFIX}___{cookie_value}___{key}'.encode('ascii')
                        redis_value = value.encode('ascii')
                        await conn.execute('SET', redis_key, redis_value, 'EX', REDIS_MAX_AGE)

            expires = time.strftime('%a, %d-%b-%Y %T GMT', time.gmtime(time.time() + COOKIE_MAX_AGE))
            secure = \
                '; Secure' if request.headers.get('x-forwarded-proto', request.url.scheme) == 'https' else \
                ''
            # aiohttp's set_cookie doesn't seem to support the SameSite attribute
            response.headers.add(
                'set-cookie', f'{COOKIE_NAME}={cookie_value}; expires={expires}; Max-Age={COOKIE_MAX_AGE}; HttpOnly; Path=/; SameSite=Lax{secure}'
            )
            return response

        request[SESSION_KEY] = get_value, set_value, with_new_cookie, with_cookie

        return await handler(request)

    return _redis_session_middleware
"""The only network path: exact HTTPS host allowlist, validated redirects, bounded bodies.

The UID nftables table is the OS boundary; these checks keep provider-supplied
addresses (manifests, redirects, subtitle and cover URLs) inside the allowlist.
"""
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36'
HOSTS = frozenset(('graphql.anilist.co', 's4.anilist.co', 'ani.pm', 'embed.settlar.io', 'media.settlar.io'))
MAX_REDIRECTS = 3
TIMEOUT = 30


class Failure(ValueError):
    """A safe, allowlisted error code. Upstream text and URLs never cross IPC."""


class RateLimited(Failure):
    def __init__(self, retry_after):
        super().__init__('RATE_LIMITED'); self.retry_after = retry_after


def check(url):
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except (ValueError, TypeError):
        raise Failure('SOURCE_CHANGED') from None
    if parts.scheme != 'https' or parts.hostname not in HOSTS or parts.username or parts.password or port not in (None, 443) or parts.fragment:
        raise Failure('SOURCE_CHANGED')
    return url


def public_address(host):
    """Defense in depth beside nftables: refuse names that resolve to non-public addresses."""
    try: infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError: raise Failure('SOURCE_UNAVAILABLE') from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split('%')[0])
        if not address.is_global: raise Failure('SOURCE_CHANGED')


class Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check(newurl); public_address(urllib.parse.urlsplit(newurl).hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_opener():
    # Built explicitly: HTTPS only, no environment proxies, no file:/ftp:/data:/http: handlers.
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.HTTPSHandler(), Redirects(), urllib.request.HTTPErrorProcessor(), urllib.request.HTTPDefaultErrorHandler(), urllib.request.UnknownHandler()):
        opener.add_handler(handler)
    return opener


_opener = build_opener()


def fetch(url, *, limit, data=None, headers=None, timeout=TIMEOUT, opener=None):
    """Return (status, headers, body). Raises Failure codes only."""
    check(url); public_address(urllib.parse.urlsplit(url).hostname)
    request = urllib.request.Request(url, data=data, headers={'User-Agent': USER_AGENT, **(headers or {})}, method='POST' if data is not None else 'GET')
    try:
        with (opener or _opener).open(request, timeout=timeout) as response:
            body = response.read(limit + 1)
            status, response_headers = response.status, response.headers
    except urllib.error.HTTPError as error:
        if error.code == 429:
            try: retry = max(1, min(3600, int(error.headers.get('Retry-After', '60'))))
            except ValueError: retry = 60
            raise RateLimited(retry) from None
        raise Failure('NOT_FOUND' if error.code in (404, 410) else 'SOURCE_UNAVAILABLE') from None
    except Failure: raise
    except (urllib.error.URLError, OSError, ValueError): raise Failure('SOURCE_UNAVAILABLE') from None
    if len(body) > limit: raise Failure('RESPONSE_TOO_LARGE')
    return status, response_headers, body

import http.client
import ipaddress
import re
import socket
import ssl
from urllib.parse import urlsplit

from django.conf import settings


class UnsafeURL(ValueError):
    pass


def parsed_public_url(url, *, live=True, allow_query=True):
    if not isinstance(url, str) or len(url) > 1000 or any(ord(char) < 32 for char in url) or "\\" in url:
        raise UnsafeURL("Invalid URL.")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURL("Invalid URL.") from exc
    if parsed.scheme not in ({"https"} if live else {"https", "http"}) or not host or "@" in parsed.netloc or parsed.fragment:
        raise UnsafeURL("URL must use an allowed scheme and public host.")
    if not allow_query and parsed.query:
        raise UnsafeURL("URL query parameters are not allowed.")
    if host != host.strip(".") or "%" in host or "@" in host:
        raise UnsafeURL("Ambiguous host.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address:
        if not address.is_global:
            raise UnsafeURL("Private destination.")
    else:
        try:
            ascii_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise UnsafeURL("Invalid host.") from exc
        labels = ascii_host.split(".")
        if (
            len(labels) < 2
            or len(ascii_host) > 253
            or ascii_host.endswith((".localhost", ".local", ".internal"))
            or ascii_host.replace(".", "").isdigit()
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
        ):
            raise UnsafeURL("Invalid public host.")
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeURL("Invalid port.")
    return parsed


def return_origin(url, *, live=None):
    if live is None:
        live = settings.FOXPAY_ENV == "live"
    parsed = parsed_public_url(url, live=live)
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if parsed.scheme == "https" else 80
    port = f":{parsed.port}" if parsed.port and parsed.port != default_port else ""
    origin = f"{parsed.scheme}://{host}{port}"
    if len(origin) > 255:
        raise UnsafeURL("Origin is too long.")
    return origin


def resolve_public_target(host, port):
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise UnsafeURL("Destination could not be resolved.") from exc
    if not addresses:
        raise UnsafeURL("Destination could not be resolved.")
    resolved = [item[4][0] for item in addresses]
    if any(not ipaddress.ip_address(address).is_global for address in resolved):
        raise UnsafeURL("Destination resolves to a private address.")
    return resolved[0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, target_ip, *, connect_timeout=3):
        super().__init__(host, port=port, timeout=connect_timeout, context=ssl.create_default_context())
        self.target_ip = target_ip

    def connect(self):
        sock = socket.create_connection((self.target_ip, self.port), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def safe_webhook_post(url, body, headers, *, response_limit=4096):
    parsed = parsed_public_url(url, live=True, allow_query=False)
    port = parsed.port or 443
    target_ip = resolve_public_target(parsed.hostname, port)
    connection = PinnedHTTPSConnection(parsed.hostname, port, target_ip)
    try:
        connection.request("POST", parsed.path or "/", body=body, headers={"Host": parsed.netloc, **headers})
        connection.sock.settimeout(5)
        response = connection.getresponse()
        response_body = response.read(response_limit + 1)
        if len(response_body) > response_limit:
            raise UnsafeURL("Destination response exceeded size limit.")
        return response.status
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise UnsafeURL("Destination request failed.") from exc
    finally:
        connection.close()

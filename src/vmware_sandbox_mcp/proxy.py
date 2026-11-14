"""Filtering web proxy for sandboxes created with network="web".

A "web" sandbox sits on a private VMware network with no route anywhere. This proxy,
listening on the host's address on that network, is its only way out. It forwards
HTTP and HTTPS (CONNECT) to public internet addresses and refuses everything else:

  * the destination name is resolved here, on the host, and the connection goes to
    exactly the address that was checked (no second lookup, so no DNS rebinding);
  * private, loopback, link-local, carrier-NAT, multicast and reserved addresses are
    refused, as are the host's own addresses and IPv6 forms that embed an IPv4 address;
  * only the configured destination ports are allowed (80 and 443 by default).

Every request reaching the proxy is untrusted input from inside a sandbox.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import threading
from collections.abc import Awaitable, Callable, Iterable, Iterator
from urllib.parse import urlsplit

from .audit import AuditLog

log = logging.getLogger("vmware_sandbox_mcp")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

MAX_HEADER_BYTES = 65536
MAX_CONNECTIONS = 64
HEADER_TIMEOUT_SECONDS = 15
CONNECT_TIMEOUT_SECONDS = 15
IDLE_TIMEOUT_SECONDS = 300

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_V4_COMPATIBLE = ipaddress.ip_network("::/96")
_HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,251}[A-Za-z0-9])?\Z")
_HOP_HEADERS = (b"proxy-connection", b"proxy-authorization", b"connection", b"keep-alive")


class Denied(Exception):
    """The request is refused by policy. The message is shown to the sandbox."""


class BadRequest(Exception):
    pass


def _with_embedded(ip: IPAddress) -> Iterator[IPAddress]:
    """The address itself plus every IPv4 address an IPv6 form can smuggle inside it."""
    yield ip
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            yield ip.ipv4_mapped
        if ip.sixtofour is not None:
            yield ip.sixtofour
        if ip.teredo is not None:
            yield from ip.teredo
        if ip in _NAT64 or ip in _V4_COMPATIBLE:
            yield ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)


def address_allowed(ip: IPAddress, local: frozenset[IPAddress] = frozenset()) -> bool:
    """True only for addresses on the public internet."""
    for candidate in _with_embedded(ip):
        if (
            not candidate.is_global
            or candidate.is_private
            or candidate.is_loopback
            or candidate.is_link_local
            or candidate.is_multicast
            or candidate.is_reserved
            or candidate.is_unspecified
            or candidate in local
        ):
            return False
    return True


def host_addresses() -> frozenset[IPAddress]:
    """This computer's own addresses, so a sandbox cannot reach the host by any of them."""
    found: set[IPAddress] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(ipaddress.ip_address(info[4][0].split("%")[0]))
    except (OSError, ValueError):
        pass
    return frozenset(found)


async def _system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _split_host_port(target: str, default_port: int | None) -> tuple[str, int]:
    host, port_text = target, ""
    if target.startswith("["):  # [v6]:port
        end = target.find("]")
        if end < 0:
            raise BadRequest("bad address")
        host, rest = target[1:end], target[end + 1 :]
        if rest:
            if not rest.startswith(":"):
                raise BadRequest("bad address")
            port_text = rest[1:]
    elif target.count(":") == 1:
        host, port_text = target.split(":")
    if not port_text:
        if default_port is None:
            raise BadRequest("missing port")
        return host, default_port
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise BadRequest("bad port")
    return host, int(port_text)


def parse_request(head: bytes) -> tuple[str, str, int, bytes | None]:
    """-> (method, host, port, rewritten head to send upstream, or None for CONNECT)."""
    lines = head.split(b"\r\n")
    try:
        method_b, target_b, version_b = lines[0].split(b" ")
        method, target, version = method_b.decode("ascii"), target_b.decode("ascii"), version_b.decode("ascii")
    except (ValueError, UnicodeDecodeError):
        raise BadRequest("malformed request line") from None
    if version not in ("HTTP/1.0", "HTTP/1.1") or not method.isalpha():
        raise BadRequest("malformed request line")

    if method.upper() == "CONNECT":
        host, port = _split_host_port(target, None)
        return "CONNECT", host, port, None

    try:
        url = urlsplit(target)
        scheme, host, port = url.scheme.lower(), url.hostname or "", url.port or 80
    except ValueError:
        raise BadRequest("malformed URL") from None
    if scheme != "http" or not host:
        raise BadRequest("only http:// URLs and CONNECT are supported")
    path = (url.path or "/") + (f"?{url.query}" if url.query else "")
    kept = [
        line
        for line in lines[1:]
        if line and line.split(b":", 1)[0].strip().lower() not in _HOP_HEADERS
    ]
    # One request per connection: everything after this head is piped through untouched.
    rewritten = b"\r\n".join([f"{method} {path} HTTP/1.1".encode("ascii"), *kept, b"Connection: close", b"", b""])
    return method.upper(), host, port, rewritten


class WebProxy:
    def __init__(
        self,
        host: str,
        port: int,
        allowed_ports: Iterable[int] = (80, 443),
        audit: AuditLog | None = None,
        *,
        allow: Callable[[IPAddress], bool] | None = None,
        resolver: Resolver = _system_resolver,
    ) -> None:
        self._host, self._port = host, port
        self._allowed_ports = frozenset(allowed_ports)
        self._audit = audit or AuditLog(None)
        if allow is None:
            local = host_addresses() | {ipaddress.ip_address(host)}
            allow = lambda ip: address_allowed(ip, local)  # noqa: E731
        self._allow = allow
        self._resolver = resolver
        self._active = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None  # actual port once listening

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Listen in a background thread. Raises OSError if the address cannot be bound."""
        ready: list[BaseException | None] = []
        signal = threading.Event()

        def run() -> None:
            async def serve() -> None:
                try:
                    server = await asyncio.start_server(
                        self._handle, self._host, self._port, limit=MAX_HEADER_BYTES
                    )
                except OSError as exc:
                    ready.append(exc)
                    signal.set()
                    return
                self.port = server.sockets[0].getsockname()[1]
                self._loop, self._stop = asyncio.get_running_loop(), asyncio.Event()
                ready.append(None)
                signal.set()
                async with server:
                    await self._stop.wait()

            asyncio.run(serve())

        self._thread = threading.Thread(target=run, daemon=True, name="sandbox-web-proxy")
        self._thread.start()
        signal.wait()
        if ready[0] is not None:
            raise ready[0]

    def start_with_retry(self, retry_seconds: float = 30) -> None:
        """Like start(), but if the port is taken (another server instance), keep trying."""
        try:
            self.start()
            log.info("Web proxy for sandboxes listening on %s:%s", self._host, self.port)
            return
        except OSError as exc:
            log.warning(
                "Web proxy could not listen on %s:%s (%s). Retrying every %ss; another "
                "vmware-sandbox-mcp instance may already be serving it.",
                self._host, self._port, exc, retry_seconds,
            )

        def retry() -> None:
            while True:
                threading.Event().wait(retry_seconds)
                try:
                    self.start()
                except OSError:
                    continue
                log.info("Web proxy for sandboxes listening on %s:%s", self._host, self.port)
                return

        threading.Thread(target=retry, daemon=True, name="sandbox-web-proxy-retry").start()

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ---- one connection --------------------------------------------------

    async def _choose_address(self, host: str, port: int) -> str:
        if port not in self._allowed_ports:
            raise Denied(f"port {port} is not allowed")
        try:
            literal: IPAddress | None = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is None:
            if not _HOSTNAME.match(host):
                raise BadRequest("bad host name")
            try:
                candidates = await asyncio.wait_for(self._resolver(host, port), CONNECT_TIMEOUT_SECONDS)
            except (OSError, asyncio.TimeoutError):
                raise Denied(f"could not resolve {host}") from None
        else:
            candidates = [str(literal)]
        for candidate in candidates:
            try:
                ip = ipaddress.ip_address(candidate.split("%")[0])
            except ValueError:
                continue
            if self._allow(ip):
                return str(ip)
        raise Denied("destination is not a public internet address")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        fields: dict[str, object] = {"client": peer}
        upstream_writer: asyncio.StreamWriter | None = None

        async def refuse(status: str, reason: str) -> None:
            self._audit.record("web", **fields, ok=False, error=reason)
            body = f"vmware-sandbox-mcp proxy: {reason}\n".encode()
            try:
                writer.write(
                    f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                    f"Connection: close\r\n\r\n".encode() + body
                )
                await writer.drain()
            except OSError:
                pass

        self._active += 1
        try:
            if self._active > MAX_CONNECTIONS:
                await refuse("503 Service Unavailable", "too many connections")
                return
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEADER_TIMEOUT_SECONDS)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, OSError):
                await refuse("400 Bad Request", "no valid request received")
                return
            try:
                method, host, port, rewritten = parse_request(head[:-4])
                fields.update(method=method, target=f"{host}:{port}")
                address = await self._choose_address(host, port)
            except BadRequest as exc:
                await refuse("400 Bad Request", str(exc))
                return
            except Denied as exc:
                await refuse("403 Forbidden", str(exc))
                return
            fields["address"] = address
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(address, port), CONNECT_TIMEOUT_SECONDS
                )
            except (OSError, asyncio.TimeoutError):
                await refuse("502 Bad Gateway", "could not connect to the destination")
                return

            self._audit.record("web", **fields, ok=True)
            if rewritten is None:
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            else:
                upstream_writer.write(rewritten)
            await self._relay(reader, writer, upstream_reader, upstream_writer)
        except Exception:  # never let one connection take the proxy down
            log.exception("web proxy connection failed")
        finally:
            self._active -= 1
            for w in (writer, upstream_writer):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass

    @staticmethod
    async def _relay(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        loop = asyncio.get_running_loop()
        last_activity = [loop.time()]

        async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while data := await reader.read(65536):
                    last_activity[0] = loop.time()
                    writer.write(data)
                    await writer.drain()
                if writer.can_write_eof():
                    writer.write_eof()
            except (OSError, asyncio.IncompleteReadError):
                pass

        tasks = [
            asyncio.ensure_future(pipe(client_reader, upstream_writer)),
            asyncio.ensure_future(pipe(upstream_reader, client_writer)),
        ]
        try:
            while True:
                done, pending = await asyncio.wait(tasks, timeout=5, return_when=asyncio.ALL_COMPLETED)
                if not pending or loop.time() - last_activity[0] > IDLE_TIMEOUT_SECONDS:
                    return
                # One side finished: give the other a short while to flush, then stop.
                if done and loop.time() - last_activity[0] > 30:
                    return
        finally:
            for task in tasks:
                task.cancel()

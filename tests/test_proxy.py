"""The filtering web proxy: destination policy, request parsing, and real forwarding on loopback."""

from __future__ import annotations

import ipaddress
import json
import socket
import socketserver
import threading

import pytest

from vmware_sandbox_mcp.audit import AuditLog
from vmware_sandbox_mcp.proxy import BadRequest, WebProxy, address_allowed, parse_request


# ---- which addresses count as "public internet" ----------------------------

@pytest.mark.parametrize("address", [
    "1.1.1.1", "8.8.8.8", "93.184.215.14", "2606:4700:4700::1111",
])
def test_public_addresses_are_allowed(address):
    assert address_allowed(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", [
    "127.0.0.1", "127.8.9.10", "0.0.0.0",
    "10.0.0.5", "172.16.0.1", "172.31.255.255", "192.168.0.1", "192.168.56.1",
    "169.254.169.254",          # link-local / cloud metadata
    "100.64.0.1",               # carrier-grade NAT
    "224.0.0.1", "239.255.255.250", "255.255.255.255", "240.0.0.1",
    "::1", "::", "fe80::1", "fc00::1", "fd15:4ba5::1", "ff02::1",
    "::ffff:192.168.0.1",       # IPv4-mapped
    "::ffff:127.0.0.1",
    "::192.168.0.1",            # IPv4-compatible
    "64:ff9b::c0a8:1",          # NAT64 of 192.168.0.1
    "2002:c0a8:0001::1",        # 6to4 of 192.168.0.1
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
])
def test_non_public_addresses_are_refused(address):
    assert not address_allowed(ipaddress.ip_address(address))


def test_the_hosts_own_public_address_is_refused():
    own = ipaddress.ip_address("8.8.4.4")
    assert address_allowed(own)
    assert not address_allowed(own, frozenset({own}))


# ---- request parsing -------------------------------------------------------

def test_connect_request():
    assert parse_request(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443") == (
        "CONNECT", "example.com", 443, None)


def test_connect_ipv6_literal():
    assert parse_request(b"CONNECT [2606:4700::1111]:443 HTTP/1.1")[1:3] == ("2606:4700::1111", 443)


def test_plain_request_is_rewritten_to_origin_form():
    method, host, port, head = parse_request(
        b"GET http://Example.com:8080/a/b?x=1 HTTP/1.1\r\nHost: example.com:8080\r\n"
        b"Proxy-Connection: keep-alive\r\nProxy-Authorization: Basic abc\r\nConnection: keep-alive\r\nAccept: */*"
    )
    assert (method, host, port) == ("GET", "example.com", 8080)
    assert head == b"GET /a/b?x=1 HTTP/1.1\r\nHost: example.com:8080\r\nAccept: */*\r\nConnection: close\r\n\r\n"


def test_plain_request_defaults_to_port_80_and_root_path():
    _, host, port, head = parse_request(b"GET http://example.com HTTP/1.1\r\nHost: example.com")
    assert (host, port) == ("example.com", 80) and head.startswith(b"GET / HTTP/1.1\r\n")


@pytest.mark.parametrize("head", [
    b"", b"GARBAGE", b"GET /local/path HTTP/1.1", b"GET https://example.com/ HTTP/1.1",
    b"GET ftp://example.com/ HTTP/1.1", b"CONNECT example.com HTTP/1.1", b"CONNECT example.com:0 HTTP/1.1",
    b"CONNECT example.com:99999 HTTP/1.1", b"CONNECT example.com:44x HTTP/1.1", b"GET http://example.com/ HTTP/9.9",
    b"G\xffT http://example.com/ HTTP/1.1", b"CONNECT [::1 HTTP/1.1", b"GET http://example.com:port/ HTTP/1.1",
])
def test_malformed_requests_are_rejected(head):
    with pytest.raises(BadRequest):
        parse_request(head)


# ---- live proxy on loopback ------------------------------------------------

class _Upstream(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _HttpHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(65536)
            if not chunk:
                return
            data += chunk
        body = b"SEEN:" + data
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        while chunk := self.request.recv(65536):
            self.request.sendall(chunk.upper())


@pytest.fixture
def upstream():
    servers = []

    def start(handler):
        server = _Upstream(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def make_proxy(tmp_path):
    proxies = []
    audit_path = tmp_path / "audit.jsonl"

    def make(**kwargs):
        proxy = WebProxy("127.0.0.1", 0, audit=AuditLog(audit_path), **kwargs)
        proxy.start()
        proxies.append(proxy)
        return proxy

    make.audit = lambda: [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    yield make
    for proxy in proxies:
        proxy.stop()


def exchange(port: int, payload: bytes, then: bytes | None = None) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(payload)
        if then is not None:
            first = sock.recv(65536)
            sock.sendall(then)
            second = sock.recv(65536)
            return first + second
        sock.shutdown(socket.SHUT_WR)
        received = b""
        while chunk := sock.recv(65536):
            received += chunk
        return received


async def _to_loopback(host, port):
    return ["127.0.0.1"]


def test_plain_http_is_forwarded(upstream, make_proxy):
    target = upstream(_HttpHandler)
    proxy = make_proxy(allow=lambda ip: True, allowed_ports=[target], resolver=_to_loopback)
    reply = exchange(proxy.port, f"GET http://files.example:{target}/pkg?v=2 HTTP/1.1\r\nHost: files.example\r\n"
                                 f"Proxy-Connection: keep-alive\r\n\r\n".encode())
    assert reply.startswith(b"HTTP/1.1 200 OK")
    assert b"SEEN:GET /pkg?v=2 HTTP/1.1\r\nHost: files.example\r\nConnection: close\r\n\r\n" in reply
    entry = make_proxy.audit()[-1]
    assert entry["event"] == "web" and entry["ok"] and entry["target"] == f"files.example:{target}"
    assert entry["address"] == "127.0.0.1" and entry["method"] == "GET"


def test_connect_tunnel_carries_bytes_both_ways(upstream, make_proxy):
    target = upstream(_EchoHandler)
    proxy = make_proxy(allow=lambda ip: True, allowed_ports=[target], resolver=_to_loopback)
    reply = exchange(proxy.port, f"CONNECT secure.example:{target} HTTP/1.1\r\n\r\n".encode(), then=b"hello tunnel")
    assert reply == b"HTTP/1.1 200 Connection established\r\n\r\nHELLO TUNNEL"


@pytest.mark.parametrize("target", [
    "127.0.0.1", "localhost", "192.168.0.1", "192.168.56.1", "10.0.0.1", "169.254.169.254", "[::1]",
    "[::ffff:192.168.0.1]",
])
def test_real_policy_refuses_local_destinations(upstream, make_proxy, target):
    port = upstream(_EchoHandler)
    proxy = make_proxy(allowed_ports=[port])  # default policy, real resolver
    reply = exchange(proxy.port, f"CONNECT {target}:{port} HTTP/1.1\r\n\r\n".encode())
    assert reply.startswith(b"HTTP/1.1 403 Forbidden")
    entry = make_proxy.audit()[-1]
    assert entry["ok"] is False and "public internet" in entry["error"]


def test_name_that_resolves_to_a_private_address_is_refused(make_proxy):
    async def rebind(host, port):
        return ["192.168.0.1", "10.0.0.1"]

    proxy = make_proxy(resolver=rebind)
    assert exchange(proxy.port, b"GET http://innocent.example/ HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")


def test_mixed_resolution_uses_only_the_checked_public_address(upstream, make_proxy):
    port = upstream(_HttpHandler)
    seen = []

    async def mixed(host, port_):
        return ["10.9.9.9", "127.0.0.1"]

    def allow(ip):
        seen.append(str(ip))
        return str(ip) == "127.0.0.1"  # stands in for "the public one"

    proxy = make_proxy(allow=allow, allowed_ports=[port], resolver=mixed)
    assert exchange(proxy.port, f"GET http://x.example:{port}/ HTTP/1.1\r\n\r\n".encode()).startswith(b"HTTP/1.1 200")
    assert seen == ["10.9.9.9", "127.0.0.1"]
    assert make_proxy.audit()[-1]["address"] == "127.0.0.1"


def test_ports_outside_the_allowed_list_are_refused(make_proxy):
    proxy = make_proxy(allow=lambda ip: True)  # default ports 80 and 443
    for port in (22, 25, 445, 3389, 8080):
        reply = exchange(proxy.port, f"CONNECT example.com:{port} HTTP/1.1\r\n\r\n".encode())
        assert reply.startswith(b"HTTP/1.1 403") and b"not allowed" in reply


def test_bad_host_names_never_reach_the_resolver(make_proxy):
    calls = []

    async def spy(host, port):
        calls.append(host)
        return ["1.1.1.1"]

    proxy = make_proxy(resolver=spy)
    for host in ("exa mple", "-bad.example", "a" * 300, "x;y", "%00", "example.com\n", "exa\nmple.com"):
        reply = exchange(proxy.port, f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode())
        assert reply.startswith(b"HTTP/1.1 400")
    assert calls == []


def test_garbage_and_oversized_requests_get_400(make_proxy):
    proxy = make_proxy()
    assert exchange(proxy.port, b"\x00\x01\x02 not http\r\n\r\n").startswith(b"HTTP/1.1 400")
    assert exchange(proxy.port, b"GET http://example.com/ HTTP/1.1\r\nX: " + b"a" * 100_000).startswith(b"HTTP/1.1 400")
    assert exchange(proxy.port, b"").startswith(b"HTTP/1.1 400")


def test_unreachable_destination_gives_502(make_proxy):
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        dead_port = closed.getsockname()[1]
    proxy = make_proxy(allow=lambda ip: True, allowed_ports=[dead_port], resolver=_to_loopback)
    assert exchange(proxy.port, f"CONNECT x.example:{dead_port} HTTP/1.1\r\n\r\n".encode()).startswith(b"HTTP/1.1 502")


def test_proxy_survives_bad_clients(upstream, make_proxy):
    target = upstream(_HttpHandler)
    proxy = make_proxy(allow=lambda ip: True, allowed_ports=[target], resolver=_to_loopback)
    for _ in range(20):
        with socket.create_connection(("127.0.0.1", proxy.port)) as sock:
            sock.sendall(b"CONNECT")  # then hang up mid-request
    reply = exchange(proxy.port, f"GET http://ok.example:{target}/ HTTP/1.1\r\n\r\n".encode())
    assert reply.startswith(b"HTTP/1.1 200")


def test_second_proxy_on_the_same_port_fails_to_start(make_proxy):
    first = make_proxy()
    second = WebProxy("127.0.0.1", first.port)
    with pytest.raises(OSError):
        second.start()

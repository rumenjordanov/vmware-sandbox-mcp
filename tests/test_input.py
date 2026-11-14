"""Keyboard and mouse: the RFB client against a fake remote display, key parsing, and the manager."""

from __future__ import annotations

import dataclasses
import os
import socket
import socketserver
import struct
import threading

import pytest

from vmware_sandbox_mcp import input as remote_input
from vmware_sandbox_mcp.config import ConfigError, load_config
from vmware_sandbox_mcp.input import (
    ALT, CONTROL, SHIFT, SUPER, InputError, RfbClient, des_encrypt_block, parse_keys, vnc_response,
)
from vmware_sandbox_mcp.manager import SandboxError
from vmware_sandbox_mcp.vmx import harden_vmx_text

from conftest import BASE_VMX_TEXT

NO_SLEEP = lambda seconds: None  # noqa: E731


# ---- DES and VNC authentication --------------------------------------------

@pytest.mark.parametrize("key,plain,cipher", [
    ("133457799BBCDFF1", "0123456789ABCDEF", "85E813540F0AB405"),
    ("0000000000000000", "0000000000000000", "8CA64DE9C1B123A7"),
    ("FFFFFFFFFFFFFFFF", "FFFFFFFFFFFFFFFF", "7359B2163E4EDC58"),
    ("0123456789ABCDEF", "4E6F772069732074", "3FA40E8A984D4815"),
])
def test_des_known_answers(key, plain, cipher):
    assert des_encrypt_block(bytes.fromhex(key), bytes.fromhex(plain)).hex().upper() == cipher


def test_vnc_response_reverses_key_bits_and_pads_the_password():
    challenge = bytes(range(16))
    # "a" = 0x61 -> bit-reversed 0x86, padded with zero bytes
    key = bytes([0x86]) + bytes(7)
    assert vnc_response("a", challenge) == des_encrypt_block(key, challenge[:8]) + des_encrypt_block(key, challenge[8:])
    assert vnc_response("abcdefgh-ignored", challenge) == vnc_response("abcdefgh", challenge)


# ---- a fake remote display --------------------------------------------------

class FakeDisplay(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, password="Passw0rd", security=(2,), size=(1280, 800)):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.password, self.security, self.size = password, security, size
        self.events: list[tuple] = []
        self.connections = 0
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]

    def keys(self):
        return [e[1:] for e in self.events if e[0] == "key"]

    def pointers(self):
        return [e[1:] for e in self.events if e[0] == "pointer"]


class _Handler(socketserver.BaseRequestHandler):
    def _read(self, count):
        data = b""
        while len(data) < count:
            chunk = self.request.recv(count - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def handle(self):
        display: FakeDisplay = self.server  # type: ignore[assignment]
        display.connections += 1
        try:
            self.request.sendall(b"RFB 003.008\n")
            self._read(12)
            self.request.sendall(bytes([len(display.security), *display.security]))
            chosen = self._read(1)[0]
            if chosen == 2:
                challenge = os.urandom(16)
                self.request.sendall(challenge)
                ok = self._read(16) == vnc_response(display.password, challenge)
                self.request.sendall(struct.pack(">I", 0 if ok else 1))
                if not ok:
                    return
            self._read(1)
            name = b"fake"
            self.request.sendall(struct.pack(">HH", *display.size) + bytes(16) + struct.pack(">I", len(name)) + name)
            while True:
                kind = self._read(1)[0]
                if kind == 4:
                    down, _, keysym = struct.unpack(">BHI", self._read(7))
                    display.events.append(("key", keysym, bool(down)))
                elif kind == 5:
                    mask, x, y = struct.unpack(">BHH", self._read(5))
                    display.events.append(("pointer", x, y, mask))
                else:
                    return
        except (ConnectionError, OSError):
            pass


@pytest.fixture
def display():
    server = FakeDisplay()
    yield server
    server.shutdown()
    server.server_close()


def settle(display, count, kind="key"):
    """Events arrive on another thread; wait until `count` of them are in."""
    import time

    deadline = time.time() + 5
    while time.time() < deadline:
        if len([e for e in display.events if e[0] == kind]) >= count:
            return
        time.sleep(0.01)


# ---- the client -------------------------------------------------------------

def test_connects_with_the_right_password_and_learns_the_screen_size(display):
    client = RfbClient(display.port, "Passw0rd")
    assert (client.width, client.height) == (1280, 800)
    client.close()


def test_wrong_password_is_rejected(display):
    with pytest.raises(InputError, match="rejected the password"):
        RfbClient(display.port, "wrongpwd")


def test_display_without_password_is_refused():
    server = FakeDisplay(security=(1,))
    try:
        with pytest.raises(InputError, match="does not ask for a password"):
            RfbClient(server.port, "Passw0rd")
    finally:
        server.shutdown()
        server.server_close()


def test_nothing_listening_is_a_clean_error():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(InputError, match="Could not reach"):
        RfbClient(port, "Passw0rd", timeout=2)


def test_garbage_server_is_a_clean_error():
    class Garbage(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(b"HTTP/1.1 200 OK\r\n\r\n")

    server = socketserver.TCPServer(("127.0.0.1", 0), Garbage)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(InputError):
            RfbClient(server.server_address[1], "Passw0rd", timeout=2)
    finally:
        server.shutdown()
        server.server_close()


# ---- typing -----------------------------------------------------------------

def test_typing_holds_shift_for_capitals_and_symbols(display):
    client = RfbClient(display.port, "Passw0rd")
    assert remote_input.type_text(client, "aB1!", NO_SLEEP) == 4
    settle(display, 12)
    client.close()
    assert display.keys() == [
        (ord("a"), True), (ord("a"), False),
        (SHIFT, True), (ord("B"), True), (ord("B"), False), (SHIFT, False),
        (ord("1"), True), (ord("1"), False),
        (SHIFT, True), (ord("!"), True), (ord("!"), False), (SHIFT, False),
    ]


def test_newline_and_tab_become_enter_and_tab(display):
    client = RfbClient(display.port, "Passw0rd")
    remote_input.type_text(client, "a\r\nb\tc\n", NO_SLEEP)
    settle(display, 12)
    client.close()
    assert [k for k, down in display.keys() if down] == [ord("a"), 0xFF0D, ord("b"), 0xFF09, ord("c"), 0xFF0D]


@pytest.mark.parametrize("text", ["héllo", "日本", "bell\x07", "esc\x1b[0m", "x" * 10_001, "", "emoji 😀"])
def test_untypable_text_is_refused_before_any_key_is_sent(display, text):
    client = RfbClient(display.port, "Passw0rd")
    with pytest.raises(InputError):
        remote_input.type_text(client, text, NO_SLEEP)
    client.close()
    assert display.events == []


def test_every_printable_ascii_character_can_be_typed(display):
    text = "".join(chr(c) for c in range(0x20, 0x7F))
    client = RfbClient(display.port, "Passw0rd")
    assert remote_input.type_text(client, text, NO_SLEEP) == 95
    client.close()


# ---- keys and shortcuts -----------------------------------------------------

@pytest.mark.parametrize("keys,expected", [
    ("Return", ([], 0xFF0D)), ("enter", ([], 0xFF0D)), ("ESC", ([], 0xFF1B)), ("F1", ([], 0xFFBE)), ("f12", ([], 0xFFC9)),
    ("ctrl+c", ([CONTROL], ord("c"))), ("Ctrl+Alt+T", ([CONTROL, ALT], ord("t"))),
    ("ctrl+alt+t", ([CONTROL, ALT], ord("t"))), ("alt+F4", ([ALT], 0xFFC1)), ("shift+Tab", ([SHIFT], 0xFF09)),
    ("super", ([SUPER], None)), ("ctrl+shift", ([CONTROL, SHIFT], None)), ("win+d", ([SUPER], ord("d"))),
    ("A", ([SHIFT], ord("A"))), ("?", ([SHIFT], ord("?"))), ("shift+a", ([SHIFT], ord("a"))),
    ("+", ([SHIFT], ord("+"))), ("ctrl++", ([CONTROL, SHIFT], ord("+"))), ("ctrl+plus", ([CONTROL, SHIFT], ord("+"))),
    ("ctrl+shift+T", ([CONTROL, SHIFT], ord("t"))), ("ctrl+minus", ([CONTROL], ord("-"))), ("a", ([], ord("a"))),
    (" ctrl + c ", ([CONTROL], ord("c"))), ("space", ([], 0x20)), ("Delete", ([], 0xFFFF)), ("PageDown", ([], 0xFF56)),
])
def test_key_names_and_shortcuts(keys, expected):
    assert parse_keys(keys) == expected


@pytest.mark.parametrize("keys", [
    "", "   ", "ctrl+", "c+ctrl+x", "hyper+x", "ctrl+doesnotexist", "Return Return", "é", "ctrl+é", None, 5,
    "a" * 65, "ctrl+alt+delete+extra",
])
def test_bad_key_descriptions_are_refused(keys):
    with pytest.raises(InputError):
        parse_keys(keys)


def test_shortcut_presses_modifiers_first_and_releases_them_last(display):
    client = RfbClient(display.port, "Passw0rd")
    remote_input.press_keys(client, "ctrl+alt+t", NO_SLEEP)
    settle(display, 6)
    client.close()
    assert display.keys() == [
        (CONTROL, True), (ALT, True), (ord("t"), True), (ord("t"), False), (ALT, False), (CONTROL, False),
    ]


def test_lone_modifier_is_pressed_and_released(display):
    client = RfbClient(display.port, "Passw0rd")
    remote_input.press_keys(client, "super", NO_SLEEP)
    settle(display, 2)
    client.close()
    assert display.keys() == [(SUPER, True), (SUPER, False)]


# ---- clicking ---------------------------------------------------------------

@pytest.mark.parametrize("button,mask", [("left", 1), ("middle", 2), ("right", 4)])
def test_click_moves_then_presses_and_releases(display, button, mask):
    client = RfbClient(display.port, "Passw0rd")
    remote_input.click(client, 640, 348, button, False, NO_SLEEP)
    settle(display, 3, "pointer")
    client.close()
    assert display.pointers() == [(640, 348, 0), (640, 348, mask), (640, 348, 0)]


def test_double_click(display):
    client = RfbClient(display.port, "Passw0rd")
    remote_input.click(client, 10, 20, "left", True, NO_SLEEP)
    settle(display, 5, "pointer")
    client.close()
    assert [mask for _, _, mask in display.pointers()] == [0, 1, 0, 1, 0]


@pytest.mark.parametrize("x,y", [(-1, 5), (5, -1), (1280, 5), (5, 800), (99999, 99999), (1.5, 2), ("3", 4), (True, 1)])
def test_clicks_outside_the_screen_or_with_bad_numbers_are_refused(display, x, y):
    client = RfbClient(display.port, "Passw0rd")
    with pytest.raises(InputError):
        remote_input.click(client, x, y, "left", False, NO_SLEEP)
    client.close()
    assert display.events == []


def test_unknown_button_is_refused(display):
    client = RfbClient(display.port, "Passw0rd")
    with pytest.raises(InputError, match="button"):
        remote_input.click(client, 1, 1, "side", False, NO_SLEEP)
    client.close()


def test_corner_pixels_are_clickable(display):
    client = RfbClient(display.port, "Passw0rd")
    remote_input.click(client, 0, 0, "left", False, NO_SLEEP)
    remote_input.click(client, 1279, 799, "left", False, NO_SLEEP)
    client.close()


# ---- the clone's settings ---------------------------------------------------

def _settings_of(text: str) -> dict[str, str]:
    return dict(line.split(" = ", 1) for line in text.splitlines())


def test_remote_display_is_off_unless_asked_for():
    result = _settings_of(harden_vmx_text(BASE_VMX_TEXT, "none"))
    assert result["RemoteDisplay.vnc.enabled"] == '"FALSE"'
    assert not any(key.lower().startswith("remotedisplay.vnc.p") for key in result)


def test_remote_display_is_bound_to_this_computer_with_a_password():
    result = _settings_of(harden_vmx_text(BASE_VMX_TEXT, "none", None, (15999, "Abc12345")))
    assert result["RemoteDisplay.vnc.enabled"] == '"TRUE"'
    assert result["RemoteDisplay.vnc.ip"] == '"127.0.0.1"'
    assert result["RemoteDisplay.vnc.port"] == '"15999"'
    assert result["RemoteDisplay.vnc.password"] == '"Abc12345"'


def test_a_remote_display_configured_in_the_base_vm_is_never_inherited():
    base = BASE_VMX_TEXT + (
        'RemoteDisplay.vnc.enabled = "TRUE"\nRemoteDisplay.vnc.port = "5900"\n'
        'RemoteDisplay.vnc.ip = "0.0.0.0"\nremotedisplay.vnc.webSocket.port = "6080"\nRemoteDisplay.skipVNCAuth = "TRUE"\n'
    )
    for display_setting in (None, (15999, "Abc12345")):
        text = harden_vmx_text(base, "none", None, display_setting)
        assert "0.0.0.0" not in text and "5900" not in text and "6080" not in text and "skipVNCAuth" not in text
    assert _settings_of(harden_vmx_text(base, "none"))["RemoteDisplay.vnc.enabled"] == '"FALSE"'


@pytest.mark.parametrize("display_setting", [
    (80, "Abc12345"), (70000, "Abc12345"), ("15999", "Abc12345"), (True, "Abc12345"),
    (15999, ""), (15999, "short"), (15999, "toolong123"), (15999, 'ab"cd\n12'), (15999, "abc1234\n"), (15999, None),
])
def test_bad_remote_display_settings_are_refused(display_setting):
    with pytest.raises(ValueError):
        harden_vmx_text(BASE_VMX_TEXT, "none", None, display_setting)


# ---- settings ---------------------------------------------------------------

@pytest.fixture
def settings(env, tmp_path):
    vmrun_exe = tmp_path / "vmrun.exe"
    vmrun_exe.write_text("", encoding="utf-8")
    return {
        "VMSANDBOX_BASE_VMX": str(env.base_vmx), "VMSANDBOX_SNAPSHOT": "Snapshot 1",
        "VMSANDBOX_CLONES_DIR": str(env.clones), "VMSANDBOX_TRANSFER_DIR": str(env.config.transfer_dir),
        "VMSANDBOX_GUEST_USER": "user", "VMSANDBOX_GUEST_PASSWORD": "s3cret-pw",
        "VMSANDBOX_VMRUN_PATH": str(vmrun_exe),
    }


@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("0", False), ("off", False), ("No", False), ("false", False),
    ("1", True), ("on", True), ("TRUE", True), ("yes", True),
])
def test_remote_input_switch(settings, value, expected):
    if value is not None:
        settings["VMSANDBOX_REMOTE_INPUT"] = value
    assert load_config(settings).remote_input is expected


def test_unclear_switch_value_is_an_error(settings):
    settings["VMSANDBOX_REMOTE_INPUT"] = "maybe"
    with pytest.raises(ConfigError, match="VMSANDBOX_REMOTE_INPUT"):
        load_config(settings)


# ---- through the manager ----------------------------------------------------

@pytest.fixture
def ready(env, display):
    """A sandbox with keyboard and mouse switched on, wired to the fake display."""
    env.display = display
    env.manager._config = dataclasses.replace(env.config, remote_input=True)
    env.manager._port_picker = lambda taken: display.port
    env.sid = env.manager.create()
    record = env.manager._store.all()[env.sid]
    display.password = record["input"]["password"]
    env.fake.calls.clear()
    return env


def test_sandbox_gets_its_own_local_display_with_a_random_password(ready):
    record = ready.manager._store.all()[ready.sid]
    password = record["input"]["password"]
    assert record["input"]["port"] == ready.display.port
    assert len(password) == 8 and password.isalnum()
    vmx = (ready.clones / ready.sid / f"{ready.sid}.vmx").read_text(encoding="utf-8")
    assert f'RemoteDisplay.vnc.port = "{ready.display.port}"' in vmx
    assert 'RemoteDisplay.vnc.ip = "127.0.0.1"' in vmx
    assert f'RemoteDisplay.vnc.password = "{password}"' in vmx


def test_each_sandbox_gets_a_different_password(env):
    env.manager._config = dataclasses.replace(env.config, remote_input=True)
    env.manager._port_picker = lambda taken: 16000 + len(taken)
    ids = [env.manager.create() for _ in range(3)]
    records = [env.manager._store.all()[sid]["input"] for sid in ids]
    assert len({r["password"] for r in records}) == 3
    assert len({r["port"] for r in records}) == 3


def test_default_port_picker_avoids_taken_and_busy_ports(env):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        for _ in range(20):
            port = env.manager._free_port({15900, 15901})
            assert 15900 <= port < 16900 and port not in (15900, 15901)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))  # really free


def test_click_type_and_key_reach_the_display(ready):
    ready.manager.click(ready.sid, 640, 348)
    assert ready.manager.type_text(ready.sid, "Hi!") == 3
    ready.manager.press_keys(ready.sid, "Return")
    settle(ready.display, 12)
    assert ready.display.pointers() == [(640, 348, 0), (640, 348, 1), (640, 348, 0)]
    assert [k for k, down in ready.display.keys() if down] == [SHIFT, ord("H"), ord("i"), SHIFT, ord("!"), 0xFF0D]
    assert ready.display.connections == 3  # one short connection per operation


def test_input_does_not_use_vmrun_at_all(ready):
    ready.manager.click(ready.sid, 1, 1)
    ready.manager.type_text(ready.sid, "x")
    assert ready.fake.calls == []


def test_input_is_refused_when_switched_off(env):
    sid = env.manager.create()
    for operation in (
        lambda: env.manager.click(sid, 1, 1),
        lambda: env.manager.type_text(sid, "x"),
        lambda: env.manager.press_keys(sid, "Return"),
    ):
        with pytest.raises(SandboxError, match="not available"):
            operation()
    vmx = (env.clones / sid / f"{sid}.vmx").read_text(encoding="utf-8")
    assert 'RemoteDisplay.vnc.enabled = "FALSE"' in vmx
    assert "input" not in env.manager._store.all()[sid]


def test_input_needs_a_known_sandbox(ready):
    with pytest.raises(SandboxError):
        ready.manager.click("sbx-deadbeef", 1, 1)
    with pytest.raises(SandboxError):
        ready.manager.type_text("not-an-id", "x")
    assert ready.display.connections == 0


def test_bad_input_becomes_a_tool_error_not_a_crash(ready):
    with pytest.raises(SandboxError, match="outside the sandbox's screen"):
        ready.manager.click(ready.sid, 5000, 5000)
    with pytest.raises(SandboxError, match="Unknown key"):
        ready.manager.press_keys(ready.sid, "ctrl+banana")
    with pytest.raises(SandboxError, match="Cannot type"):
        ready.manager.type_text(ready.sid, "naïve")


def test_display_gone_is_a_clean_error(ready):
    ready.display.shutdown()
    ready.display.server_close()
    with pytest.raises(SandboxError, match="Could not reach"):
        ready.manager.press_keys(ready.sid, "Return")


def test_password_is_never_listed_or_logged(ready):
    password = ready.manager._store.all()[ready.sid]["input"]["password"]
    ready.manager.click(ready.sid, 3, 4, "right", True)
    ready.manager.type_text(ready.sid, "hello")
    ready.manager.press_keys(ready.sid, "ctrl+s")
    with pytest.raises(SandboxError):
        ready.manager.click(ready.sid, -1, -1)
    assert password not in str(ready.manager.list())
    assert password not in ready.audit_path.read_text(encoding="utf-8")
    click, typed, key, refused = ready.audit()[-4:]
    assert (click["event"], click["x"], click["y"], click["button"], click["double"]) == ("click", 3, 4, "right", True)
    assert (typed["event"], typed["text"], typed["keys"]) == ("type", "hello", 5)
    assert (key["event"], key["keys"]) == ("key", "ctrl+s")
    assert refused["ok"] is False and "outside" in refused["error"]


def test_remote_display_port_lookup(ready, env):
    assert ready.manager.remote_display_port(ready.sid) == ready.display.port

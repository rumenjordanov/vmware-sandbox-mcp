"""Keyboard and mouse input for a sandbox, through VMware's built-in remote display (VNC).

When the feature is switched on, each sandbox gets its own remote display, bound to
127.0.0.1, on its own port, with its own random password. This module is a minimal RFB
client for it: connect, authenticate, send key and pointer events. It never reads the
screen; sandbox_screenshot does that.

VMware's server maps each key to a physical key and ignores whether it is a capital or
a symbol, so this module holds Shift itself. That assumes a US keyboard layout in the
sandbox.
"""

from __future__ import annotations

import socket
import struct
import time
from collections.abc import Callable

MAX_TEXT_LENGTH = 10_000
_KEY_DELAY = 0.012
_CLICK_DELAY = 0.06

# ---- DES, single block, encrypt only: all that VNC authentication needs --------------

_IP = [58,50,42,34,26,18,10,2,60,52,44,36,28,20,12,4,62,54,46,38,30,22,14,6,64,56,48,40,32,24,16,8,
       57,49,41,33,25,17,9,1,59,51,43,35,27,19,11,3,61,53,45,37,29,21,13,5,63,55,47,39,31,23,15,7]
_FP = [40,8,48,16,56,24,64,32,39,7,47,15,55,23,63,31,38,6,46,14,54,22,62,30,37,5,45,13,53,21,61,29,
       36,4,44,12,52,20,60,28,35,3,43,11,51,19,59,27,34,2,42,10,50,18,58,26,33,1,41,9,49,17,57,25]
_E = [32,1,2,3,4,5,4,5,6,7,8,9,8,9,10,11,12,13,12,13,14,15,16,17,16,17,18,19,20,21,20,21,22,23,24,25,
      24,25,26,27,28,29,28,29,30,31,32,1]
_P = [16,7,20,21,29,12,28,17,1,15,23,26,5,18,31,10,2,8,24,14,32,27,3,9,19,13,30,6,22,11,4,25]
_PC1 = [57,49,41,33,25,17,9,1,58,50,42,34,26,18,10,2,59,51,43,35,27,19,11,3,60,52,44,36,
        63,55,47,39,31,23,15,7,62,54,46,38,30,22,14,6,61,53,45,37,29,21,13,5,28,20,12,4]
_PC2 = [14,17,11,24,1,5,3,28,15,6,21,10,23,19,12,4,26,8,16,7,27,20,13,2,
        41,52,31,37,47,55,30,40,51,45,33,48,44,49,39,56,34,53,46,42,50,36,29,32]
_SHIFTS = [1,1,2,2,2,2,2,2,1,2,2,2,2,2,2,1]
_S = [
 [14,4,13,1,2,15,11,8,3,10,6,12,5,9,0,7, 0,15,7,4,14,2,13,1,10,6,12,11,9,5,3,8, 4,1,14,8,13,6,2,11,15,12,9,7,3,10,5,0, 15,12,8,2,4,9,1,7,5,11,3,14,10,0,6,13],
 [15,1,8,14,6,11,3,4,9,7,2,13,12,0,5,10, 3,13,4,7,15,2,8,14,12,0,1,10,6,9,11,5, 0,14,7,11,10,4,13,1,5,8,12,6,9,3,2,15, 13,8,10,1,3,15,4,2,11,6,7,12,0,5,14,9],
 [10,0,9,14,6,3,15,5,1,13,12,7,11,4,2,8, 13,7,0,9,3,4,6,10,2,8,5,14,12,11,15,1, 13,6,4,9,8,15,3,0,11,1,2,12,5,10,14,7, 1,10,13,0,6,9,8,7,4,15,14,3,11,5,2,12],
 [7,13,14,3,0,6,9,10,1,2,8,5,11,12,4,15, 13,8,11,5,6,15,0,3,4,7,2,12,1,10,14,9, 10,6,9,0,12,11,7,13,15,1,3,14,5,2,8,4, 3,15,0,6,10,1,13,8,9,4,5,11,12,7,2,14],
 [2,12,4,1,7,10,11,6,8,5,3,15,13,0,14,9, 14,11,2,12,4,7,13,1,5,0,15,10,3,9,8,6, 4,2,1,11,10,13,7,8,15,9,12,5,6,3,0,14, 11,8,12,7,1,14,2,13,6,15,0,9,10,4,5,3],
 [12,1,10,15,9,2,6,8,0,13,3,4,14,7,5,11, 10,15,4,2,7,12,9,5,6,1,13,14,0,11,3,8, 9,14,15,5,2,8,12,3,7,0,4,10,1,13,11,6, 4,3,2,12,9,5,15,10,11,14,1,7,6,0,8,13],
 [4,11,2,14,15,0,8,13,3,12,9,7,5,10,6,1, 13,0,11,7,4,9,1,10,14,3,5,12,2,15,8,6, 1,4,11,13,12,3,7,14,10,15,6,8,0,5,9,2, 6,11,13,8,1,4,10,7,9,5,0,15,14,2,3,12],
 [13,2,8,4,6,15,11,1,10,9,3,14,5,0,12,7, 1,15,13,8,10,3,7,4,12,5,6,11,0,14,9,2, 7,11,4,1,9,12,14,2,0,6,10,13,15,3,5,8, 2,1,14,7,4,10,8,13,15,12,9,0,3,5,6,11],
]


def _permute(value: int, table: list[int], width: int) -> int:
    out = 0
    for position in table:
        out = (out << 1) | ((value >> (width - position)) & 1)
    return out


def des_encrypt_block(key: bytes, block: bytes) -> bytes:
    k = _permute(int.from_bytes(key, "big"), _PC1, 64)
    c, d = k >> 28, k & 0xFFFFFFF
    subkeys = []
    for shift in _SHIFTS:
        c = ((c << shift) | (c >> (28 - shift))) & 0xFFFFFFF
        d = ((d << shift) | (d >> (28 - shift))) & 0xFFFFFFF
        subkeys.append(_permute((c << 28) | d, _PC2, 56))
    v = _permute(int.from_bytes(block, "big"), _IP, 64)
    left, right = v >> 32, v & 0xFFFFFFFF
    for subkey in subkeys:
        x = _permute(right, _E, 32) ^ subkey
        f = 0
        for i in range(8):
            six = (x >> (42 - 6 * i)) & 0x3F
            row, col = ((six >> 4) & 2) | (six & 1), (six >> 1) & 0xF
            f = (f << 4) | _S[i][row * 16 + col]
        left, right = right, left ^ _permute(f, _P, 32)
    return _permute((right << 32) | left, _FP, 64).to_bytes(8, "big")


def vnc_response(password: str, challenge: bytes) -> bytes:
    raw = password.encode("latin-1")[:8].ljust(8, b"\0")
    key = bytes(int(f"{byte:08b}"[::-1], 2) for byte in raw)  # VNC uses each key byte bit-reversed
    return des_encrypt_block(key, challenge[:8]) + des_encrypt_block(key, challenge[8:16])


# ---- RFB client ----------------------------------------------------------------------

class InputError(Exception):
    """A problem the agent can understand and act on."""


class RfbClient:
    def __init__(self, port: int, password: str, host: str = "127.0.0.1", timeout: float = 10) -> None:
        try:
            self._sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise InputError(f"Could not reach the sandbox's keyboard and mouse ({exc}).") from None
        try:
            if not self._read(12).startswith(b"RFB "):
                raise InputError("The sandbox's remote display gave an unexpected answer.")
            self._sock.sendall(b"RFB 003.008\n")
            count = self._read(1)[0]
            if count == 0:
                raise InputError("The sandbox's remote display refused the connection.")
            types = self._read(count)
            if 2 not in types:
                # Never fall back to "no authentication": a display without a password is not ours.
                raise InputError("The sandbox's remote display does not ask for a password; refusing to use it.")
            self._sock.sendall(b"\x02")
            self._sock.sendall(vnc_response(password, self._read(16)))
            if struct.unpack(">I", self._read(4))[0] != 0:
                raise InputError("The sandbox's remote display rejected the password.")
            self._sock.sendall(b"\x01")  # shared: do not disconnect anyone else
            self.width, self.height = struct.unpack(">HH", self._read(4))
            self._read(16)
            self._read(struct.unpack(">I", self._read(4))[0])
        except (OSError, InputError) as exc:
            self._sock.close()
            if isinstance(exc, InputError):
                raise
            raise InputError(f"Lost the connection to the sandbox's keyboard and mouse ({exc}).") from None

    def _read(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self._sock.recv(count - len(data))
            if not chunk:
                raise InputError("The sandbox's remote display closed the connection.")
            data += chunk
        return data

    def key(self, keysym: int, down: bool) -> None:
        self._sock.sendall(struct.pack(">BBHI", 4, 1 if down else 0, 0, keysym))

    def pointer(self, x: int, y: int, buttons: int) -> None:
        self._sock.sendall(struct.pack(">BBHH", 5, buttons, x, y))

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


# ---- keys ----------------------------------------------------------------------------

SHIFT, CONTROL, ALT, SUPER = 0xFFE1, 0xFFE3, 0xFFE9, 0xFFEB
_MODIFIERS = {
    "shift": SHIFT, "ctrl": CONTROL, "control": CONTROL, "alt": ALT,
    "super": SUPER, "meta": SUPER, "win": SUPER, "windows": SUPER, "cmd": SUPER,
}
_NAMED = {
    "return": 0xFF0D, "enter": 0xFF0D, "escape": 0xFF1B, "esc": 0xFF1B, "tab": 0xFF09,
    "backspace": 0xFF08, "delete": 0xFFFF, "del": 0xFFFF, "insert": 0xFF63,
    "home": 0xFF50, "end": 0xFF57, "pageup": 0xFF55, "pagedown": 0xFF56,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "space": 0x20, "menu": 0xFF67, "printscreen": 0xFF61,
    **{f"f{n}": 0xFFBD + n for n in range(1, 13)},
}
_SHIFTED = set('~!@#$%^&*()_+{}|:"<>?') | set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_BUTTONS = {"left": 1, "middle": 2, "right": 4}


def _char_key(char: str) -> tuple[int, bool]:
    """-> (keysym, needs Shift) for one printable ASCII character."""
    if len(char) != 1 or not 0x20 <= ord(char) <= 0x7E:
        raise InputError(
            f"Cannot type {char!r}: only plain English keyboard characters, Enter and Tab can be typed."
        )
    return ord(char), char in _SHIFTED


def parse_keys(keys: str) -> tuple[list[int], int | None]:
    """'ctrl+alt+t' -> ([Control, Alt], keysym of t). A lone modifier such as 'super' is allowed."""
    if not isinstance(keys, str) or not keys.strip() or len(keys) > 64:
        raise InputError("keys must be a key name or a shortcut such as 'Return' or 'ctrl+alt+t'.")
    text = keys.strip()
    parts = ["+"] if text == "+" else [part.strip() for part in text.replace("++", "+plus").split("+")]
    modifiers: list[int] = []
    for part in parts[:-1]:
        if part.lower() not in _MODIFIERS:
            raise InputError(f"{part!r} is not a modifier key. Use ctrl, alt, shift or super before the last key.")
        modifiers.append(_MODIFIERS[part.lower()])
    last = parts[-1]
    if last.lower() in _MODIFIERS:
        return modifiers + [_MODIFIERS[last.lower()]], None
    if last.lower() in _NAMED:
        return modifiers, _NAMED[last.lower()]
    last = {"plus": "+", "minus": "-"}.get(last.lower(), last)
    if len(last) == 1:
        if modifiers and last.isalpha():
            last = last.lower()  # "Ctrl+T" means ctrl+t; write shift explicitly to get ctrl+shift+t
        keysym, shifted = _char_key(last)
        if shifted and SHIFT not in modifiers:
            modifiers.append(SHIFT)
        return modifiers, keysym
    raise InputError(
        f"Unknown key {last!r}. Use a single character or a name such as Return, Escape, Tab, "
        "Backspace, Delete, Space, Up, Down, Left, Right, Home, End, PageUp, PageDown, F1 to F12."
    )


def press_keys(client: RfbClient, keys: str, sleep: Callable[[float], None] = time.sleep) -> None:
    modifiers, key = parse_keys(keys)
    for modifier in modifiers:
        client.key(modifier, True)
        sleep(_KEY_DELAY)
    if key is not None:
        client.key(key, True)
        sleep(_KEY_DELAY)
        client.key(key, False)
        sleep(_KEY_DELAY)
    for modifier in reversed(modifiers):
        client.key(modifier, False)
        sleep(_KEY_DELAY)


def type_text(client: RfbClient, text: str, sleep: Callable[[float], None] = time.sleep) -> int:
    if not isinstance(text, str) or not text:
        raise InputError("text is empty.")
    if len(text) > MAX_TEXT_LENGTH:
        raise InputError(f"text is too long (limit {MAX_TEXT_LENGTH} characters).")
    plan: list[tuple[int, bool]] = []
    for char in text.replace("\r\n", "\n"):
        plan.append((0xFF0D, False) if char == "\n" else (0xFF09, False) if char == "\t" else _char_key(char))
    for keysym, shifted in plan:  # validated in full before the first key is sent
        if shifted:
            client.key(SHIFT, True)
        client.key(keysym, True)
        sleep(_KEY_DELAY)
        client.key(keysym, False)
        if shifted:
            client.key(SHIFT, False)
        sleep(_KEY_DELAY)
    return len(plan)


def click(
    client: RfbClient,
    x: int,
    y: int,
    button: str = "left",
    double: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if button not in _BUTTONS:
        raise InputError("button must be 'left', 'right' or 'middle'.")
    for value in (x, y):
        if isinstance(value, bool) or not isinstance(value, int):
            raise InputError("x and y must be whole numbers (pixels in the screenshot).")
    if not (0 <= x < client.width and 0 <= y < client.height):
        raise InputError(
            f"({x}, {y}) is outside the sandbox's screen, which is {client.width} x {client.height} pixels."
        )
    mask = _BUTTONS[button]
    client.pointer(x, y, 0)
    sleep(_CLICK_DELAY)
    for _ in range(2 if double else 1):
        client.pointer(x, y, mask)
        sleep(_CLICK_DELAY)
        client.pointer(x, y, 0)
        sleep(_CLICK_DELAY)

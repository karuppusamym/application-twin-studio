"""Host-side TN3270 server (RFC 1576 style) and a small client.

The server negotiates TERMINAL-TYPE, END-OF-RECORD and BINARY over Telnet and
speaks the 3270 data stream: Erase/Write with SBA, SF and IC orders outbound;
AID + cursor + modified fields inbound. Text is EBCDIC code page 037.

Limits, stated plainly:
  * TN3270E (RFC 2355) is declined during negotiation; clients fall back to
    classic TN3270. LU binding, SNA responses and extended attributes are not
    implemented.
  * Structured fields (Query Reply), extended highlighting/colour, and printer
    sessions are not implemented.
  * Verified here with the bundled client only. Replay with the real emulator
    your automation uses (for example wc3270 or a UiPath Terminal session) is
    a separate acceptance step.
"""
from __future__ import annotations

import socket
import socketserver
import threading
from typing import Any, Callable

IAC, DONT, DO, WONT, WILL, SB, SE, EOR_CMD = 0xFF, 0xFE, 0xFD, 0xFC, 0xFB, 0xFA, 0xF0, 0xEF
OPT_BINARY, OPT_TTYPE, OPT_EOR, OPT_TN3270E = 0, 24, 25, 40
CMD_ERASE_WRITE, CMD_WRITE = 0xF5, 0xF1
ORD_SF, ORD_SBA, ORD_IC = 0x1D, 0x11, 0x13
CODEC = "cp037"

_CODE = bytes([
    0x40, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F,
    0x50, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0x5A, 0x5B, 0x5C, 0x5D, 0x5E, 0x5F,
    0x60, 0x61, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0x6A, 0x6B, 0x6C, 0x6D, 0x6E, 0x6F,
    0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0x7A, 0x7B, 0x7C, 0x7D, 0x7E, 0x7F,
])
AIDS = {
    "ENTER": 0x7D, "CLEAR": 0x6D, "PA1": 0x6C, "PA2": 0x6E, "PA3": 0x6B,
    **{f"PF{n}": 0xF0 + n for n in range(1, 10)}, "PF10": 0x7A, "PF11": 0x7B, "PF12": 0x7C,
    **{f"PF{n + 12}": 0xC0 + n for n in range(1, 10)}, "PF22": 0x4A, "PF23": 0x4B, "PF24": 0x4C,
}
AID_NAMES = {v: k for k, v in AIDS.items()}
SHORT_READ = {"CLEAR", "PA1", "PA2", "PA3"}


def enc_addr(addr: int) -> bytes:
    return bytes([_CODE[(addr >> 6) & 0x3F], _CODE[addr & 0x3F]])


def dec_addr(b1: int, b2: int) -> int:
    if b1 & 0xC0 == 0:  # 14-bit binary addressing
        return ((b1 & 0x3F) << 8) | b2
    return ((b1 & 0x3F) << 6) | (b2 & 0x3F)


def _attr(protected: bool, hidden: bool = False, intense: bool = False) -> int:
    bits = (0x20 if protected else 0) | (0x0C if hidden else 0x08 if intense else 0)
    return _CODE[bits]


def build_screen(layout: dict) -> bytes:
    """Layout from ScreenSession -> 3270 Erase/Write data stream."""
    cols = layout["cols"]
    size = layout["rows"] * cols
    out = bytearray([CMD_ERASE_WRITE, 0xC3])  # WCC: reset MDT + keyboard restore
    for f in sorted(layout["fields"], key=lambda f: (f["row"], f["col"])):
        addr = (f["row"] - 1) * cols + (f["col"] - 1)
        out += bytes([ORD_SBA]) + enc_addr((addr - 1) % size)
        out += bytes([ORD_SF, _attr(f["protected"], f.get("hidden", False), f.get("intense", False))])
        if f["protected"]:
            out += f["text"].encode(CODEC, "replace")
        else:
            # terminate the unprotected field with a protected attribute byte
            out += bytes([ORD_SBA]) + enc_addr((addr + f["len"]) % size) + bytes([ORD_SF, _attr(True)])
    cur = (layout["cursor"][0] - 1) * cols + (layout["cursor"][1] - 1)
    out += bytes([ORD_SBA]) + enc_addr(cur) + bytes([ORD_IC])
    return bytes(out)


def parse_inbound(record: bytes) -> tuple[str, int, dict[int, str]]:
    """Inbound read-modified record -> (aid name, cursor address, {field address: text})."""
    if not record:
        return "ENTER", 0, {}
    aid = AID_NAMES.get(record[0], f"AID{record[0]:02X}")
    if len(record) < 3:
        return aid, 0, {}
    cursor = dec_addr(record[1], record[2])
    fields: dict[int, str] = {}
    i = 3
    while i < len(record):
        if record[i] == ORD_SBA and i + 2 < len(record):
            addr = dec_addr(record[i + 1], record[i + 2])
            i += 3
            j = i
            while j < len(record) and record[j] != ORD_SBA:
                j += 1
            fields[addr] = record[i:j].decode(CODEC, "replace")
            i = j
        else:
            i += 1
    return aid, cursor, fields


class _Telnet:
    """Record framing shared by server and client."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()

    def send_record(self, data: bytes) -> None:
        self.sock.sendall(data.replace(b"\xff", b"\xff\xff") + bytes([IAC, EOR_CMD]))

    def send_raw(self, *b: int) -> None:
        self.sock.sendall(bytes(b))

    def _fill(self) -> bool:
        chunk = self.sock.recv(4096)
        if not chunk:
            return False
        self.buf += chunk
        return True

    def read_event(self) -> tuple[str, Any] | None:
        """Returns ('cmd', (verb, opt)), ('sb', bytes) or ('record', bytes); None at EOF."""
        data = bytearray()
        while True:
            while not self.buf:
                if not self._fill():
                    return None
            b = self.buf[0]
            if b != IAC:
                data.append(b)
                del self.buf[0]
                continue
            while len(self.buf) < 2:
                if not self._fill():
                    return None
            c = self.buf[1]
            if c == IAC:
                data.append(IAC)
                del self.buf[:2]
            elif c == EOR_CMD:
                del self.buf[:2]
                return "record", bytes(data)
            elif c in (DO, DONT, WILL, WONT):
                while len(self.buf) < 3:
                    if not self._fill():
                        return None
                opt = self.buf[2]
                del self.buf[:3]
                return "cmd", (c, opt)
            elif c == SB:
                while True:
                    end = self.buf.find(bytes([IAC, SE]))
                    if end >= 0:
                        break
                    if not self._fill():
                        return None
                sub = bytes(self.buf[2:end])
                del self.buf[: end + 2]
                return "sb", sub
            else:
                del self.buf[:2]


class Tn3270Server:
    """One listening port per (environment, application)."""

    def __init__(self, session_factory: Callable[[], Any], host: str = "127.0.0.1", port: int = 0):
        factory = session_factory

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                tn = _Telnet(self.request)
                self.request.settimeout(300)
                try:
                    tn.send_raw(IAC, DO, OPT_TTYPE)
                    state = {"ttype": False, "ready": False}
                    session = None
                    while True:
                        ev = tn.read_event()
                        if ev is None:
                            return
                        kind, val = ev
                        if kind == "cmd":
                            verb, opt = val
                            if opt == OPT_TN3270E:
                                tn.send_raw(IAC, DONT if verb == WILL else WONT, opt)
                            elif verb == WILL and opt == OPT_TTYPE:
                                tn.send_raw(IAC, SB, OPT_TTYPE, 1, IAC, SE)
                            elif verb == WONT and opt == OPT_TTYPE:
                                return  # not a 3270 client
                            elif verb in (DO, WILL) and opt not in (OPT_BINARY, OPT_EOR, OPT_TTYPE):
                                tn.send_raw(IAC, WONT if verb == DO else DONT, opt)
                        elif kind == "sb" and val[:2] == bytes([OPT_TTYPE, 0]) and not state["ready"]:
                            tn.send_raw(IAC, DO, OPT_EOR, IAC, WILL, OPT_EOR, IAC, DO, OPT_BINARY, IAC, WILL, OPT_BINARY)
                            state["ready"] = True
                            session = factory()
                            tn.send_record(build_screen(session.layout()))
                        elif kind == "record" and session is not None:
                            aid, _cursor, fields = parse_inbound(val)
                            session.send_by_address(aid, fields)
                            if session.closed:
                                return
                            tn.send_record(build_screen(session.layout()))
                except (OSError, socket.timeout):
                    return

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server((host, port), Handler)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name=f"tn3270-{self.port}")
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Tn3270Client:
    """Minimal TN3270 client used by the sample bots and the conformance tests."""

    def __init__(self, host: str, port: int, timeout: float = 10.0, rows: int = 24, cols: int = 80):
        self.rows, self.cols = rows, cols
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.tn = _Telnet(self.sock)
        self.buffer = [" "] * (rows * cols)
        self.fields: list[dict] = []  # {addr (first data position), protected, hidden}
        self.cursor = 0
        self.pending: dict[int, str] = {}
        self._negotiate()

    def _negotiate(self) -> None:
        while True:
            ev = self.tn.read_event()
            if ev is None:
                raise ConnectionError("host closed during negotiation")
            kind, val = ev
            if kind == "cmd":
                verb, opt = val
                if verb == DO and opt in (OPT_TTYPE, OPT_EOR, OPT_BINARY):
                    self.tn.send_raw(IAC, WILL, opt)
                elif verb == WILL and opt in (OPT_EOR, OPT_BINARY):
                    self.tn.send_raw(IAC, DO, opt)
                elif verb == DO:
                    self.tn.send_raw(IAC, WONT, opt)
                elif verb == WILL:
                    self.tn.send_raw(IAC, DONT, opt)
            elif kind == "sb" and val[:2] == bytes([OPT_TTYPE, 1]):
                self.sock.sendall(bytes([IAC, SB, OPT_TTYPE, 0]) + b"IBM-3278-2" + bytes([IAC, SE]))
            elif kind == "record":
                self._apply(val)
                return

    def _apply(self, rec: bytes) -> None:
        if not rec:
            return
        if rec[0] == CMD_ERASE_WRITE:
            self.buffer = [" "] * (self.rows * self.cols)
            self.fields = []
        self.pending = {}
        addr, i, size = 0, 2, self.rows * self.cols
        while i < len(rec):
            b = rec[i]
            if b == ORD_SBA:
                addr = dec_addr(rec[i + 1], rec[i + 2])
                i += 3
            elif b == ORD_SF:
                bits = _CODE.index(rec[i + 1]) if rec[i + 1] in _CODE else rec[i + 1] & 0x3F
                self.buffer[addr] = " "
                self.fields.append({"addr": (addr + 1) % size, "protected": bool(bits & 0x20), "hidden": bits & 0x0C == 0x0C})
                addr = (addr + 1) % size
                i += 2
            elif b == ORD_IC:
                self.cursor = addr
                i += 1
            else:
                self.buffer[addr] = bytes([b]).decode(CODEC, "replace")
                addr = (addr + 1) % size
                i += 1

    # -- public API -------------------------------------------------------
    def screen(self) -> list[str]:
        return ["".join(self.buffer[r * self.cols:(r + 1) * self.cols]) for r in range(self.rows)]

    def text(self) -> str:
        return "\n".join(self.screen())

    def get(self, row: int, col: int, length: int) -> str:
        start = (row - 1) * self.cols + (col - 1)
        return "".join(self.buffer[start:start + length]).strip()

    def set_field(self, row: int, col: int, value: str) -> None:
        addr = (row - 1) * self.cols + (col - 1)
        fld = next((f for f in self.fields if f["addr"] == addr), None)
        if fld is None or fld["protected"]:
            raise ValueError(f"no unprotected field starts at row {row} col {col}")
        self.pending[addr] = value

    def send(self, aid: str = "ENTER") -> list[str]:
        aid = aid.upper()
        out = bytearray([AIDS[aid]])
        if aid not in SHORT_READ:
            out += enc_addr(self.cursor)
            for addr, value in sorted(self.pending.items()):
                out += bytes([ORD_SBA]) + enc_addr(addr) + value.encode(CODEC, "replace")
        self.tn.send_record(bytes(out))
        while True:
            ev = self.tn.read_event()
            if ev is None:
                self.buffer = [" "] * (self.rows * self.cols)
                return self.screen()
            if ev[0] == "record":
                self._apply(ev[1])
                return self.screen()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

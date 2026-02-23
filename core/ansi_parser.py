"""
ANSI / VT100 escape sequence parser.

Converts a stream of raw bytes (including ANSI SGR colour/attribute codes)
into a list of AnsiSpan objects that the output widget can render.

Handles:
  - SGR colours: standard 8, bright 8, 256-colour (38;5;n), true-colour (38;2;r;g;b)
  - Attributes: bold, dim, italic, underline, blink, reverse, strikethrough, reset
  - Private/extended CSI sequences (?1000l, >4;1m, etc.) — consumed silently
  - Non-CSI escape sequences (ESC =, ESC >, ESC M, etc.) — consumed silently
  - OSC sequences (ESC ] ... BEL/ST) — consumed silently
  - Partial sequences buffered across chunk boundaries

The key correctness rule: ANY escape sequence that is fully present in the
buffer must be consumed (even if we don't act on it), so it never blocks
the plain text that follows it.
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# CSI: ESC [ <private?> <params> <final>
# Private sequences have a leading ? ! > = character after ESC [
_CSI_RE = re.compile(
    rb'\x1b\['          # ESC [
    rb'[?!>]?'          # optional private marker
    rb'[0-9;]*'         # numeric params
    rb'[a-zA-Z@`]'      # final byte
)

# OSC: ESC ] ... (BEL or ESC \)
_OSC_RE = re.compile(
    rb'\x1b\]'
    rb'[^\x07\x1b]*'    # content
    rb'(?:\x07|\x1b\\)' # BEL or ST
)

# Single-char escape sequences: ESC followed by one non-[ non-] byte
_ESC1_RE = re.compile(rb'\x1b[^[\]][^\x1b]?')

# Standard 16 ANSI colours as (R, G, B)
_ANSI16 = [
    (  0,   0,   0),  # 0  black
    (170,   0,   0),  # 1  red
    (  0, 170,   0),  # 2  green
    (170,  85,   0),  # 3  yellow/brown
    (  0,   0, 170),  # 4  blue
    (170,   0, 170),  # 5  magenta
    (  0, 170, 170),  # 6  cyan
    (170, 170, 170),  # 7  white
    ( 85,  85,  85),  # 8  bright black
    (255,  85,  85),  # 9  bright red
    ( 85, 255,  85),  # 10 bright green
    (255, 255,  85),  # 11 bright yellow
    ( 85,  85, 255),  # 12 bright blue
    (255,  85, 255),  # 13 bright magenta
    ( 85, 255, 255),  # 14 bright cyan
    (255, 255, 255),  # 15 bright white
]


def _build_256_palette():
    palette = list(_ANSI16)
    for r in range(6):
        for g in range(6):
            for b in range(6):
                palette.append((
                    0 if r == 0 else 55 + r * 40,
                    0 if g == 0 else 55 + g * 40,
                    0 if b == 0 else 55 + b * 40,
                ))
    for i in range(24):
        v = 8 + i * 10
        palette.append((v, v, v))
    return palette

_PALETTE_256 = _build_256_palette()

_FG_DEFAULT = None
_BG_DEFAULT = None


@dataclass
class TextStyle:
    fg: Optional[Tuple[int,int,int]] = None
    bg: Optional[Tuple[int,int,int]] = None
    _fg_base_idx: int = -1   # 0-7 if set via SGR 30-37, else -1
    bold:          bool = False
    dim:           bool = False
    italic:        bool = False
    underline:     bool = False
    blink:         bool = False
    reverse:       bool = False
    strikethrough: bool = False

    def reset(self):
        self.fg = self.bg = None
        self._fg_base_idx = -1
        self.bold = self.dim = self.italic = False
        self.underline = self.blink = self.reverse = False
        self.strikethrough = False

    def copy(self):
        s = TextStyle(
            fg=self.fg, bg=self.bg,
            bold=self.bold, dim=self.dim, italic=self.italic,
            underline=self.underline, blink=self.blink,
            reverse=self.reverse, strikethrough=self.strikethrough,
        )
        s._fg_base_idx = self._fg_base_idx
        return s


@dataclass
class AnsiSpan:
    text:  str
    style: TextStyle


class AnsiParser:
    """
    Stateful streaming ANSI parser.
    Feed raw byte chunks via feed().  Returns list of AnsiSpan objects.
    """

    def __init__(self):
        self._style  = TextStyle()
        self._buffer = b""

    def feed(self, data: bytes) -> List[AnsiSpan]:
        data = self._buffer + data
        self._buffer = b""
        spans: List[AnsiSpan] = []
        pos = 0
        length = len(data)

        while pos < length:
            # Find next ESC
            esc = data.find(b'\x1b', pos)

            if esc == -1:
                # No more escapes — emit everything remaining
                text = data[pos:].decode('utf-8', errors='replace')
                if text:
                    spans.append(AnsiSpan(text, self._style.copy()))
                pos = length
                break

            # Emit plain text before the escape
            if esc > pos:
                text = data[pos:esc].decode('utf-8', errors='replace')
                if text:
                    spans.append(AnsiSpan(text, self._style.copy()))
            pos = esc

            # Need at least ESC + 1 more byte to know sequence type
            if pos + 1 >= length:
                self._buffer = data[pos:]
                break

            next_byte = data[pos+1:pos+2]

            if next_byte == b'[':
                # CSI sequence — try to match complete sequence
                m = _CSI_RE.match(data, pos)
                if m:
                    # Got a complete CSI — only act on 'm' (SGR)
                    seq = data[m.start():m.end()]
                    if seq.endswith(b'm'):
                        # Extract params between ESC [ ... m
                        inner = seq[2:-1]  # strip ESC [ and m
                        inner = inner.lstrip(b'?!>')  # strip private marker
                        self._apply_sgr(inner)
                    pos = m.end()
                else:
                    # Incomplete CSI — check if it could still complete
                    # If we haven't seen a final byte yet, buffer it
                    remaining = data[pos:]
                    # If it's longer than 32 bytes and still no final byte,
                    # it's malformed — skip the ESC and move on
                    if len(remaining) > 32:
                        pos += 1  # skip stray ESC
                    else:
                        self._buffer = remaining
                        break

            elif next_byte == b']':
                # OSC sequence
                m = _OSC_RE.match(data, pos)
                if m:
                    pos = m.end()
                else:
                    remaining = data[pos:]
                    if len(remaining) > 256:
                        pos += 1  # malformed, skip
                    else:
                        self._buffer = remaining
                        break

            else:
                # Single-character or two-character escape (ESC = ESC > etc.)
                # These are always exactly 2 bytes — just consume them
                pos += 2

        return [s for s in spans if s.text]

    def _apply_sgr(self, params_bytes: bytes):
        raw = params_bytes.decode('ascii', errors='replace').strip(';')
        if not raw:
            self._style.reset()
            return
        params = [int(p) if p.isdigit() else 0 for p in raw.split(';')]
        i = 0
        while i < len(params):
            p = params[i]
            if   p == 0:  self._style.reset()
            elif p == 1:  self._style.bold = True
            elif p == 2:  self._style.dim = True
            elif p == 3:  self._style.italic = True
            elif p == 4:  self._style.underline = True
            elif p in (5,6): self._style.blink = True
            elif p == 7:  self._style.reverse = True
            elif p == 9:  self._style.strikethrough = True
            elif p == 22: self._style.bold = self._style.dim = False
            elif p == 23: self._style.italic = False
            elif p == 24: self._style.underline = False
            elif p == 25: self._style.blink = False
            elif p == 27: self._style.reverse = False
            elif p == 29: self._style.strikethrough = False
            elif p == 39: self._style.fg = None; self._style._fg_base_idx = -1
            elif p == 49: self._style.bg = None
            elif 30 <= p <= 37:
                # Store the base colour index (0-7); bold-as-bright is
                # applied at render time so it reacts to bold set in any order
                self._style.fg = _ANSI16[p - 30]
                self._style._fg_base_idx = p - 30   # remember base for bold-bright
            elif p == 38:
                col, n = self._parse_ext(params, i+1)
                if col: self._style.fg = col
                i += n
            elif 40 <= p <= 47:
                self._style.bg = _ANSI16[p - 40]
            elif p == 48:
                col, n = self._parse_ext(params, i+1)
                if col: self._style.bg = col
                i += n
            elif 90 <= p <= 97:
                self._style.fg = _ANSI16[p - 90 + 8]
            elif 100 <= p <= 107:
                self._style.bg = _ANSI16[p - 100 + 8]
            i += 1

    @staticmethod
    def _parse_ext(params, start):
        if start >= len(params):
            return None, 0
        mode = params[start]
        if mode == 5 and start + 1 < len(params):
            return _PALETTE_256[params[start+1] % 256], 2
        if mode == 2 and start + 3 < len(params):
            return (params[start+1]&0xFF, params[start+2]&0xFF, params[start+3]&0xFF), 4
        return None, 1

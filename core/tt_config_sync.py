"""
TinTin++ config file sync.

Two responsibilities:

1. READING from TinTin++
   Send `#write {tempfile}` to a running tt++ process, poll for the file,
   parse it into a config dict with keys:
     aliases, actions, timers, highlights

2. WRITING to disk
   Serialise a config dict back to a .tin file that tt++ can `#read`.

The config file for a session lives at:
   ~/.config/tintin-gui/<session_name>_config.tin

Key parsing notes
-----------------
- TinTin++ #write may use abbreviated command names (#ali, #act, #tick, #hig).
  We map every valid prefix to a canonical name.
- Arguments may contain nested braces:
      #action {hungry} {#if {%1>50} {eat bread}} {5}
  A simple [^}]* regex breaks; we use a brace-counting tokenizer instead.
- TinTin++ appends a class argument to most directives:
      #alias {name} {body} {classname}
  Extra trailing args are silently ignored.
"""

import logging
import os
import re
from pathlib import Path

from PyQt6.QtCore import QTimer, QObject, pyqtSignal

log = logging.getLogger(__name__)

_CONFIG_DIR = Path.home() / ".config" / "tintin-gui"
_WRITE_TMP  = _CONFIG_DIR / "_ttwrite_tmp.tin"


# ── Path helpers ──────────────────────────────────────────────────────

def config_file_path(session_name: str) -> Path:
    safe = re.sub(r"[^\w\-]", "_", session_name)
    return _CONFIG_DIR / f"{safe}_config.tin"


# ── Command prefix map ────────────────────────────────────────────────
# TinTin++ #write may abbreviate command names.  We accept any
# unambiguous prefix of length >= 3 and map it to a canonical name.

_CANONICAL: dict[str, str] = {}
for _full, _canon in [
    ("#alias",     "#alias"),
    ("#action",    "#action"),
    ("#ticker",    "#ticker"),
    ("#highlight", "#highlight"),
    ("#colour",    "#highlight"),    # British spelling variant
    ("#color",     "#highlight"),    # US alternative (older tt++ versions)
]:
    for _end in range(3, len(_full) + 1):
        _p = _full[:_end]
        if _p not in _CANONICAL:    # first (shortest) match wins
            _CANONICAL[_p] = _canon


# ── Brace-counting tokenizer ──────────────────────────────────────────

def _extract_brace_args(s: str) -> list[str]:
    """
    Extract all top-level brace-delimited arguments from a string.
    Handles arbitrary nesting:
        '{foo} {bar {baz}} {qux}' -> ['foo', 'bar {baz}', 'qux']
    """
    args: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == '{':
            depth = 1
            i += 1
            start = i
            while i < n and depth > 0:
                if   s[i] == '{': depth += 1
                elif s[i] == '}': depth -= 1
                i += 1
            args.append(s[start : i - 1])
        elif s[i] in ' \t':
            i += 1
        else:
            # Unbraced token — treat as one arg
            start = i
            while i < n and s[i] not in ' \t{':
                i += 1
            args.append(s[start:i])
    return args


def _parse_directive(line: str) -> tuple[str, list[str]] | tuple[None, None]:
    """
    Parse one line into (canonical_command, [args]).
    Returns (None, None) for blank/comment/unrecognised lines.
    Accepts abbreviated command names (e.g. #ali, #act, #hig, #tick).
    """
    line = line.strip()
    if not line or not line.startswith('#'):
        return None, None
    parts = line.split(None, 1)
    canon = _CANONICAL.get(parts[0].lower())
    if canon is None:
        return None, None
    rest = parts[1] if len(parts) > 1 else ""
    return canon, _extract_brace_args(rest)


# ── Multiline directive joiner ───────────────────────────────────────

def _join_directives(text: str) -> list[str]:
    """
    Collapse multiline TinTin++ directives into single strings.

    TinTin++ #write uses a pretty-printed format where the body brace
    block is on separate lines:

        #ACTION {pattern}
        {
            body
        }

    Strategy: a new directive begins on any line that starts with '#'
    AND the previously accumulated content has balanced braces (depth==0).
    Everything until the next such boundary is joined with spaces.

    This handles both single-line and multiline forms transparently.
    """
    directives: list[str] = []
    chunks: list[str] = []
    depth = 0

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        delta = stripped.count('{') - stripped.count('}')

        if stripped.startswith('#') and depth == 0 and chunks:
            # Balanced braces + new #command = flush previous directive
            directives.append(' '.join(chunks))
            chunks = []

        chunks.append(stripped)
        depth += delta

    if chunks:
        directives.append(' '.join(chunks))

    return directives


# ── Parser ────────────────────────────────────────────────────────────

def parse_tin_file(path: str | Path, debug: bool = False) -> dict:
    """
    Parse a TinTin++ .tin file (e.g. from #write) into a config dict:
        {
            "aliases":    [{"name": str, "body": str}, ...],
            "actions":    [{"pattern": str, "command": str,
                            "priority": int, "enabled": True}, ...],
            "timers":     [{"name": str, "command": str,
                            "interval": int, "enabled": True}, ...],
            "highlights": [{"pattern": str, "fg": str, "bg": str}, ...],
        }
    Handles both single-line and multiline TinTin++ #write formats.
    Handles arbitrary nested braces.
    """
    config: dict = {
        "aliases":    [],
        "actions":    [],
        "timers":     [],
        "highlights": [],
    }
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError) as exc:
        log.warning("parse_tin_file: cannot read %s: %s", path, exc)
        return config

    if debug:
        log.debug("parse_tin_file raw content (%d bytes):\n%s", len(text), text[:4000])

    for directive in _join_directives(text):
        canon, args = _parse_directive(directive)
        if canon is None:
            continue

        if canon == "#alias":
            # #alias {name} {body} [{class}]
            if len(args) >= 2 and args[0].strip():
                config["aliases"].append({
                    "name": args[0].strip(),
                    "body": args[1].strip(),
                })

        elif canon == "#action":
            # #action {pattern} {command} {priority} [{class}]
            if len(args) >= 2 and args[0].strip():
                priority = 5
                if len(args) >= 3:
                    try:
                        priority = int(args[2].strip())
                    except ValueError:
                        pass
                config["actions"].append({
                    "pattern":  args[0].strip(),
                    "command":  args[1].strip(),
                    "priority": priority,
                    "enabled":  True,
                })

        elif canon == "#ticker":
            # #ticker {name} {command} {interval} [{class}]
            if len(args) >= 3 and args[0].strip():
                try:
                    interval = int(args[2].strip())
                except ValueError:
                    interval = 30
                config["timers"].append({
                    "name":     args[0].strip(),
                    "command":  args[1].strip(),
                    "interval": interval,
                    "enabled":  True,
                })

        elif canon == "#highlight":
            # #highlight {pattern} {color} [{class}]
            if len(args) >= 2 and args[0].strip():
                fg, bg = _split_highlight_color(args[1].strip())
                config["highlights"].append({
                    "pattern": args[0].strip(),
                    "fg":      fg,
                    "bg":      bg,
                })

    log.debug(
        "parse_tin_file results: %d aliases, %d actions, %d timers, %d highlights",
        len(config["aliases"]), len(config["actions"]),
        len(config["timers"]),  len(config["highlights"]),
    )
    return config


def _split_highlight_color(color: str) -> tuple[str, str]:
    """
    Split "bold white on blue" -> ("bold white", "blue").
    Split "bold yellow"        -> ("bold yellow", "").
    """
    parts = color.split(" on ", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return color.strip(), ""


# ── Writer ────────────────────────────────────────────────────────────

def write_config_file(path: str | Path, config: dict):
    """
    Write a config dict to a .tin file that tt++ can #read.
    Clear-all guards make the file idempotent on re-read.
    """
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "#nop -- TinTin++ GUI session config",
        "#nop    Auto-generated -- edit via View -> Configuration in the GUI.",
        "",
    ]

    lines += ["#nop -- Aliases", "#alias {}"]
    for a in config.get("aliases", []):
        name = a.get("name", "").strip()
        body = a.get("body", "").strip()
        if name:
            lines.append(f"#alias {{{name}}} {{{body}}}")
    lines.append("")

    lines += ["#nop -- Actions", "#action {}"]
    for a in config.get("actions", []):
        pat = a.get("pattern", "").strip()
        cmd = a.get("command", "").strip()
        pri = a.get("priority", 5)
        if pat and a.get("enabled", True):
            lines.append(f"#action {{{pat}}} {{{cmd}}} {{{pri}}}")
    lines.append("")

    lines += ["#nop -- Timers", "#ticker {}"]
    for t in config.get("timers", []):
        name = t.get("name", "").strip()
        cmd  = t.get("command", "").strip()
        secs = t.get("interval", 30)
        if name and cmd and t.get("enabled", True):
            lines.append(f"#ticker {{{name}}} {{{cmd}}} {{{secs}}}")
    lines.append("")

    lines += ["#nop -- Highlights", "#highlight {}"]
    for h in config.get("highlights", []):
        pat = h.get("pattern", "").strip()
        fg  = h.get("fg",      "").strip()
        bg  = h.get("bg",      "").strip()
        if pat:
            if fg and bg:   color = f"{fg} on {bg}"
            elif fg:        color = fg
            elif bg:        color = bg
            else:           continue
            lines.append(f"#highlight {{{pat}}} {{{color}}}")
    lines.append("")

    Path(path).write_text("\n".join(lines), encoding="utf-8")


# ── Live sync from a running tt++ process ────────────────────────────

class TinTinConfigLoader(QObject):
    """
    Asks a running TinTin++ process to dump its config via `#write`,
    then asynchronously reads and parses the result.

    Usage:
        loader = TinTinConfigLoader(tt_process)
        loader.loaded.connect(my_callback)   # callback(config: dict)
        loader.error.connect(my_error_cb)    # callback(msg: str)
        loader.load()
    """

    loaded  = pyqtSignal(dict)
    error   = pyqtSignal(str)
    # Emits the raw file text so callers can show/log it for debugging
    raw_dump = pyqtSignal(str)

    _POLL_MS    = 150
    _TIMEOUT_MS = 8_000

    def __init__(self, tt_process, parent=None):
        super().__init__(parent)
        self._tt      = tt_process
        self._path    = str(_WRITE_TMP)
        self._timer   = None
        self._elapsed = 0

    def load(self):
        """Delete any stale dump, send #write, start polling."""
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass

        self._elapsed = 0
        self._tt.send(f"#write {{{self._path}}}")

        self._timer = QTimer(self)
        self._timer.setInterval(self._POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    def _poll(self):
        self._elapsed += self._POLL_MS

        if os.path.exists(self._path):
            try:
                size = os.path.getsize(self._path)
            except OSError:
                return
            if size == 0:
                return   # tt++ still writing

            self._timer.stop()

            try:
                raw_text = Path(self._path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                raw_text = ""

            self.raw_dump.emit(raw_text)
            log.debug("tt++ #write dump (%d bytes):\n%s", len(raw_text), raw_text[:4000])

            config = parse_tin_file(self._path, debug=True)

            try:
                os.unlink(self._path)
            except OSError:
                pass

            self.loaded.emit(config)
            return

        if self._elapsed >= self._TIMEOUT_MS:
            self._timer.stop()
            self.error.emit(
                "Timed out waiting for TinTin++ to write config.\n"
                "Is TinTin++ running and connected to a session?"
            )

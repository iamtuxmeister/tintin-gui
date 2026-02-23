# TinTin++ GUI

A graphical shell around [TinTin++](https://tintin.mudhalla.net/) — giving the best
terminal MUD client a proper GUI without touching a single line of its source code.

## Architecture

```
[MUD Server] <──TCP──> [TinTin++ process] <──PTY──> [Python/PyQt6 GUI]
```

TinTin++ runs as a child process connected via a Unix pseudo-terminal (PTY).
The GUI intercepts all output, parses ANSI codes, and renders them natively.
TinTin++ never knows it isn't talking to a real terminal — full script
compatibility is preserved for free.

### Module layout

```
tintin-gui/
├── main.py                   Entry point
├── requirements.txt
├── core/
│   ├── tintin_process.py     PTY-based subprocess manager + QThread reader
│   ├── ansi_parser.py        Streaming ANSI/SGR → AnsiSpan converter
│   └── map_parser.py         MapGraph data model + GMCP Room.Info parser
└── ui/
    ├── main_window.py        Top-level QMainWindow, wires everything together
    ├── output_widget.py      Split-scrollback ANSI display (QTextEdit based)
    ├── map_widget.py         QGraphicsScene node/edge map renderer
    └── button_bar.py         Configurable macro button strip
```

## Setup

### Windows

1. **Install WinTin++** — download and run the MSI installer from https://tintin.mudhalla.net/download.php
   Default install path: `C:\Program Files (x86)\WinTin++\bin\tt.exe`

2. **Install Python 3.10+** from https://python.org (check "Add to PATH" during install)

3. **Install dependencies**
   ```cmd
   pip install PyQt6 pywinpty
   ```

4. **Run**
   ```cmd
   python main.py
   ```

---

### Linux

### 1. Install TinTin++
```bash
sudo apt install tintin++          # Debian/Ubuntu
sudo dnf install tintin++          # Fedora
sudo pacman -S tintin++            # Arch
```

### 2. Install Python dependencies
```bash
pip install PyQt6
# or
pip install -r requirements.txt
```

### 3. Run
```bash
python main.py
# or with a startup script:
python main.py ~/myscripts/toril.tin
```

## Features

### Split Scrollback
- Scroll up in the live pane (or press `Page Up`) to open the scrollback split
- The live pane continues streaming new output while you read history
- Scroll back to the bottom (or press `Page Down` to the end) to close the split
- `Ctrl+Shift+S` toggles the split manually

### Graphical Map
- Rooms rendered as coloured nodes on a zoomable/pannable canvas
- Colour-coded by terrain type (city, forest, mountain, water, desert…)
- Current room highlighted in gold
- Up/Down exits shown as coloured dots
- Hover over a room to see its name, vnum, and area
- `Ctrl+M` toggles the map panel
- `Ctrl+Wheel` to zoom

Map data is populated from **GMCP Room.Info** packets automatically when the
MUD supports GMCP (Toril does).  You can also load a `#map write` XML export
via **File → Load map XML**.

### Button Bar
- Default buttons for common commands (movement, look, score, inv, map)
- Right-click any button to edit label, command, or colour
- Right-click to add/delete buttons
- Layout persists in `~/.config/tintin-gui/buttons.json`

### Input Bar
- `↑`/`↓` for command history
- `Enter` or click Send to dispatch
- Any keypress outside the input bar refocuses it automatically

### Font size
- `Ctrl+=` / `Ctrl+-` to resize the output font live

## Configuring TinTin++ for GMCP (Toril)

Add to your startup `.tin` file to relay GMCP room data:
```
#event {IAC WILL GMCP}  {#gmcp send {core 1}}
#event {GMCP Room.Info} {GMCP: Room.Info %1}
```

The second line echoes the JSON payload as a plain text line that the GUI's
map parser picks up automatically.

## Roadmap

- [ ] Health/mana/move gauge bar
- [ ] Multiple profiles (different MUD connections)
- [ ] TinTin++ script editor tab with syntax highlighting
- [ ] Map: click room to auto-walk (`#map goto`)
- [ ] Map: area filter / zoom to area
- [ ] Session logging
- [ ] Sound event hooks

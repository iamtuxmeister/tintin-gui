"""
Map data layer.

Two sources of map information are supported:

1.  **GMCP Room.Info** packets relayed by TinTin++ via #event.
    This is the preferred path when the MUD supports GMCP.
    Toril/TorilMUD does support GMCP.

2.  **#map write** XML export.  When TinTin++ writes its map to a temp
    file we parse the XML to reconstruct the room graph.  Useful for
    re-hydrating a session after reconnect.

The MapGraph class is the shared data model consumed by MapWidget.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


# Direction vectors for standard compass + up/down
# Used when laying out rooms in a grid for the graphical renderer
_DIR_DELTA: Dict[str, Tuple[int, int]] = {
    "n":  ( 0, -1),
    "ne": ( 1, -1),
    "e":  ( 1,  0),
    "se": ( 1,  1),
    "s":  ( 0,  1),
    "sw": (-1,  1),
    "w":  (-1,  0),
    "nw": (-1, -1),
    "u":  ( 0,  0),   # rendered as special icon, no grid offset
    "d":  ( 0,  0),
}


@dataclass
class Exit:
    direction: str
    to_vnum:   int        # -1 if unknown / not yet visited


@dataclass
class Room:
    vnum:    int
    name:    str          = ""
    area:    str          = ""
    terrain: str          = ""
    exits:   List[Exit]   = field(default_factory=list)
    x:       int          = 0    # grid position (computed by layout engine)
    y:       int          = 0
    visited: bool         = False


class MapGraph:
    """
    In-memory graph of rooms.

    Thread-safety note: MapWidget reads this from the Qt main thread;
    updates arrive on the same thread via Qt signals, so no locking needed
    as long as we never update from the reader thread directly.
    """

    def __init__(self):
        self.rooms:       Dict[int, Room] = {}
        self.current_vnum: int = -1

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def update_room(self, vnum: int, name: str = "", area: str = "",
                    terrain: str = "", exits: List[Exit] = None) -> Room:
        room = self.rooms.get(vnum)
        if room is None:
            room = Room(vnum=vnum)
            self.rooms[vnum] = room
        if name:
            room.name = name
        if area:
            room.area = area
        if terrain:
            room.terrain = terrain
        if exits is not None:
            room.exits = exits
        room.visited = True
        return room

    def set_current(self, vnum: int):
        self.current_vnum = vnum

    def clear(self):
        self.rooms.clear()
        self.current_vnum = -1

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compute_layout(self):
        """
        BFS from the current room to assign (x, y) grid coordinates.
        Only rooms reachable via cardinal directions get grid positions.
        """
        if self.current_vnum not in self.rooms:
            return

        visited: Set[int] = set()
        queue = [(self.current_vnum, 0, 0)]
        while queue:
            vnum, x, y = queue.pop(0)
            if vnum in visited:
                continue
            visited.add(vnum)
            room = self.rooms.get(vnum)
            if room is None:
                continue
            room.x, room.y = x, y
            for ex in room.exits:
                if ex.to_vnum > 0 and ex.to_vnum not in visited:
                    dx, dy = _DIR_DELTA.get(ex.direction.lower(), (0, 0))
                    if dx != 0 or dy != 0:
                        queue.append((ex.to_vnum, x + dx, y + dy))

    # ------------------------------------------------------------------
    # Importers
    # ------------------------------------------------------------------

    def ingest_gmcp_room_info(self, data: dict):
        """
        Parse a GMCP Room.Info payload dict and update the graph.

        Expected keys (all optional except num):
          num, name, area, terrain, exits (dict dir->vnum)
        """
        vnum = int(data.get("num", -1))
        if vnum < 0:
            return
        exits = [
            Exit(direction=d, to_vnum=int(v))
            for d, v in data.get("exits", {}).items()
        ]
        self.update_room(
            vnum    = vnum,
            name    = data.get("name", ""),
            area    = data.get("area", ""),
            terrain = data.get("terrain", ""),
            exits   = exits,
        )
        self.set_current(vnum)
        self.compute_layout()

    def load_from_xml(self, xml_path: str) -> int:
        """
        Load a map exported by `#map write <path>`.
        Returns number of rooms loaded.
        """
        try:
            tree = ET.parse(xml_path)
        except (ET.ParseError, FileNotFoundError):
            return 0

        root = tree.getroot()
        count = 0
        for room_el in root.iter("room"):
            try:
                vnum = int(room_el.get("id", -1))
                if vnum < 0:
                    continue
                exits = []
                for ex_el in room_el.findall("exit"):
                    d    = ex_el.get("dir", "?")
                    dest = int(ex_el.get("dest", -1))
                    exits.append(Exit(direction=d, to_vnum=dest))
                self.update_room(
                    vnum    = vnum,
                    name    = room_el.get("name", ""),
                    area    = room_el.get("area", ""),
                    terrain = room_el.get("terrain", ""),
                    exits   = exits,
                )
                count += 1
            except (ValueError, TypeError):
                continue

        self.compute_layout()
        return count


# ------------------------------------------------------------------
# GMCP line parser
# ------------------------------------------------------------------

# TinTin++ relays GMCP events as lines like:
#   GMCP: Room.Info { "num": 1234, "name": "The Town Square", ... }
_GMCP_RE = re.compile(
    r"^GMCP:\s+(?P<pkg>[\w.]+)\s+(?P<json>\{.*\})\s*$"
)


def try_parse_gmcp_line(line: str) -> Optional[Tuple[str, dict]]:
    """
    If line looks like a TinTin++ GMCP relay, return (package, data_dict).
    Returns None otherwise.
    """
    import json
    m = _GMCP_RE.match(line.strip())
    if not m:
        return None
    try:
        data = json.loads(m.group("json"))
        return m.group("pkg"), data
    except (json.JSONDecodeError, ValueError):
        return None

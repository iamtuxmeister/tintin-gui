"""
MapWidget — graphical room mapper.

Renders a MapGraph as an interactive node-edge diagram using QGraphicsScene /
QGraphicsView.  Rooms are drawn as coloured rectangles; exits as lines with
optional direction labels.  The current room is highlighted.

The widget auto-centers on the current room whenever it changes.

Terrain colours (loosely matching common MUD conventions):
  city/indoors → slate blue
  forest       → dark green
  mountain     → grey
  water        → blue
  desert       → tan
  default      → dark teal
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QGraphicsView, QGraphicsScene,
    QGraphicsRectItem, QGraphicsTextItem, QGraphicsLineItem,
    QGraphicsEllipseItem, QSizePolicy,
)
from PyQt6.QtCore  import Qt, QRectF, QPointF
from PyQt6.QtGui   import (
    QColor, QPen, QBrush, QFont, QPainter, QWheelEvent,
)

from core.map_parser import MapGraph, Room


# Grid cell size in scene units
_CELL = 60
_ROOM_W = 36
_ROOM_H = 24

_CURRENT_COLOR  = QColor(220, 180,  40)   # gold
_VISITED_COLOR  = QColor( 50,  80, 120)   # dark blue
_UNKNOWN_COLOR  = QColor( 35,  35,  45)   # very dark
_EXIT_COLOR     = QColor(100, 130, 160)
_TEXT_COLOR     = QColor(220, 220, 220)
_BG_COLOR       = QColor( 12,  12,  18)

_TERRAIN_COLORS = {
    "city":     QColor( 70,  70, 110),
    "indoors":  QColor( 70,  70, 110),
    "building": QColor( 70,  70, 110),
    "forest":   QColor( 30,  80,  30),
    "mountain": QColor( 90,  90,  90),
    "water":    QColor( 20,  50, 120),
    "ocean":    QColor( 10,  30, 100),
    "desert":   QColor(130, 100,  40),
    "field":    QColor( 50, 100,  40),
    "road":     QColor( 80,  70,  50),
}


def _room_color(room: Room, is_current: bool) -> QColor:
    if is_current:
        return _CURRENT_COLOR
    terrain = room.terrain.lower() if room.terrain else ""
    for key, col in _TERRAIN_COLORS.items():
        if key in terrain:
            return col
    return _VISITED_COLOR


def _grid_to_scene(x: int, y: int) -> QPointF:
    return QPointF(x * _CELL, y * _CELL)


class _ZoomView(QGraphicsView):
    """QGraphicsView with Ctrl+Wheel zoom."""

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setBackgroundBrush(QBrush(_BG_COLOR))
        self._zoom_level = 1.0

    def wheelEvent(self, event: QWheelEvent):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self._zoom_level *= factor
            self._zoom_level = max(0.2, min(self._zoom_level, 5.0))
            self.setTransform(
                self.transform().scale(factor, factor)
            )
        else:
            super().wheelEvent(event)


class MapWidget(QWidget):
    """
    Drop-in widget that renders a MapGraph.

    Call ``refresh(graph)`` whenever the graph is updated.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self._scene = QGraphicsScene(self)
        self._view  = _ZoomView(self._scene, self)
        self._view.setMinimumWidth(200)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)

        self._font = QFont("Monospace")
        self._font.setPointSize(7)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self, graph: MapGraph):
        """Re-draw the entire scene from graph data."""
        self._scene.clear()
        if not graph.rooms:
            return

        # Draw exits first (so they appear behind rooms)
        drawn_exits: set = set()
        for room in graph.rooms.values():
            sp = _grid_to_scene(room.x, room.y)
            for ex in room.exits:
                if ex.to_vnum <= 0:
                    continue
                dest = graph.rooms.get(ex.to_vnum)
                if dest is None:
                    continue
                key = (min(room.vnum, dest.vnum), max(room.vnum, dest.vnum))
                if key in drawn_exits:
                    continue
                drawn_exits.add(key)

                ep = _grid_to_scene(dest.x, dest.y)
                line = QGraphicsLineItem(
                    sp.x() + _ROOM_W / 2,
                    sp.y() + _ROOM_H / 2,
                    ep.x() + _ROOM_W / 2,
                    ep.y() + _ROOM_H / 2,
                )
                line.setPen(QPen(_EXIT_COLOR, 1.5))
                self._scene.addItem(line)

        # Draw rooms
        for room in graph.rooms.values():
            self._draw_room(room, room.vnum == graph.current_vnum)

        # Center view on current room
        if graph.current_vnum in graph.rooms:
            cur = graph.rooms[graph.current_vnum]
            cp  = _grid_to_scene(cur.x, cur.y)
            self._view.centerOn(
                cp.x() + _ROOM_W / 2,
                cp.y() + _ROOM_H / 2,
            )

    def clear(self):
        self._scene.clear()

    # ------------------------------------------------------------------

    def _draw_room(self, room: Room, is_current: bool):
        sp    = _grid_to_scene(room.x, room.y)
        color = _room_color(room, is_current)

        rect = QGraphicsRectItem(sp.x(), sp.y(), _ROOM_W, _ROOM_H)
        rect.setBrush(QBrush(color))
        border_color = QColor(200, 170, 60) if is_current else QColor(80, 100, 120)
        rect.setPen(QPen(border_color, 1.5 if is_current else 1.0))
        rect.setToolTip(f"[{room.vnum}] {room.name}\n{room.area}")
        self._scene.addItem(rect)

        # Room name label (truncated)
        if room.name:
            label = QGraphicsTextItem()
            label.setDefaultTextColor(_TEXT_COLOR)
            label.setFont(self._font)
            short = room.name[:12] + "…" if len(room.name) > 12 else room.name
            label.setPlainText(short)
            label.setPos(sp.x() + 2, sp.y() + 5)
            label.setTextWidth(_ROOM_W - 4)
            self._scene.addItem(label)

        # Up/Down indicators
        dirs = {ex.direction.lower() for ex in room.exits}
        if "u" in dirs:
            self._add_small_dot(sp.x() + _ROOM_W - 8, sp.y() + 2, QColor(150, 220, 150))
        if "d" in dirs:
            self._add_small_dot(sp.x() + _ROOM_W - 8, sp.y() + _ROOM_H - 8, QColor(220, 150, 150))

    def _add_small_dot(self, x: float, y: float, color: QColor):
        dot = QGraphicsEllipseItem(x, y, 5, 5)
        dot.setBrush(QBrush(color))
        dot.setPen(QPen(Qt.PenStyle.NoPen))
        self._scene.addItem(dot)

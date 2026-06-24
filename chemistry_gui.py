from __future__ import annotations

import json
import sys
import threading
from collections import deque
from queue import Empty, Queue
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QEvent, QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QAction, QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QSpinBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from basic_commands import (
    RunControl,
    add_log_comment,
    cleaning,
    emergency_stop,
    empty_reactor,
    fill_reactor,
    move_liquid,
    pause_data_logging,
    read_serial_cable_values,
    read_hotplate_values,
    resume_data_logging,
    run_peristaltic_pump,
    run_syringe_pump,
    set_valve_position,
    set_serial_rts,
    start_data_logging,
    heat_reactor,
    start_heating,
    stir_reactor,
    start_stirring,
    stop_reactor_heating,
    stop_heating,
    stop_peristaltic_pump,
    stop_data_logging,
    stop_reactor_stirring,
    stop_stirring,
    stop_syringe_pump,
)
import exp_topology


@dataclass
class GuiContext:
    topology: Any
    experiment_runners: dict[str, Any] = field(default_factory=dict)
    expert_mode: bool = False
    run_control: RunControl = field(default_factory=RunControl)

    def snapshot(self) -> dict[str, Any]:
        return self.topology.gui_snapshot(experiment_runners=self.experiment_runners)


class NoWheelComboBox(QComboBox):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)

    def event(self, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.Wheel and not self.hasFocus():
            event.ignore()
            return True
        return super().event(event)


class NoWheelSpinBox(QSpinBox):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)
        if self.lineEdit() is not None:
            self.lineEdit().setFocusPolicy(Qt.ClickFocus)

    def event(self, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.Wheel and not self.hasFocus():
            event.ignore()
            return True
        return super().event(event)


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)
        if self.lineEdit() is not None:
            self.lineEdit().setFocusPolicy(Qt.ClickFocus)

    def event(self, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.Wheel and not self.hasFocus():
            event.ignore()
            return True
        return super().event(event)


class TopologyGraphWidget(QWidget):
    """Small dependency-free renderer for the physical setup graph."""

    def __init__(self) -> None:
        super().__init__()
        self.graph: dict[str, Any] = {}
        self.animation_tick = 0
        self.setMinimumHeight(520)

    def update_graph(self, graph: dict[str, Any], animation_tick: int) -> None:
        self.graph = graph
        self.animation_tick = animation_tick
        self.update()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#fbfbfc"))

        nodes = self.graph.get("nodes", [])
        connections = self.graph.get("connections", [])
        active_transfers = self.graph.get("active_transfers", [])
        if not nodes:
            painter.setPen(QColor("#666666"))
            painter.drawText(self.rect(), Qt.AlignCenter, "No topology nodes")
            return

        positions = self._node_positions(nodes, connections)
        active_edges = {
            frozenset((a, b))
            for transfer in active_transfers
            for a, b in zip(transfer.get("nodes", []), transfer.get("nodes", [])[1:])
        }

        for connection in connections:
            a = str(connection["a"])
            b = str(connection["b"])
            if a not in positions or b not in positions:
                continue
            active = frozenset((a, b)) in active_edges
            pen = QPen(QColor("#0084ff" if active else "#b8bec6"))
            pen.setWidth(6 if active else 3)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.drawLine(positions[a], positions[b])

        for transfer in active_transfers:
            path = transfer.get("nodes", [])
            for index, (a, b) in enumerate(zip(path, path[1:])):
                if a not in positions or b not in positions:
                    continue
                progress = ((self.animation_tick + index) % 5 + 1) / 6.0
                start = positions[a]
                end = positions[b]
                dot = QPointF(
                    start.x() + ((end.x() - start.x()) * progress),
                    start.y() + ((end.y() - start.y()) * progress),
                )
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(QColor("#fff4e6")))
                painter.drawEllipse(dot, 7, 7)
                painter.setBrush(QBrush(QColor("#006fd7")))
                painter.drawEllipse(dot, 4, 4)

        for node in nodes:
            name = str(node["name"])
            kind = str(node.get("kind", "node"))
            position = positions[name]
            rect = QRectF(position.x() - 64, position.y() - 22, 128, 44)
            painter.setPen(QPen(QColor("#39424e"), 2))
            painter.setBrush(QBrush(self._node_color(kind, bool(node.get("is_device")))))
            painter.drawRoundedRect(rect, 10, 10)
            painter.setPen(QColor("#1f2933"))
            painter.drawText(rect, Qt.AlignCenter, f"{name}\n{kind}")

        painter.setPen(QColor("#58616d"))
        if active_transfers:
            descriptions = [
                f"{item['source']} -> {item['destination']} ({item['volume_ml']:g} mL)"
                for item in active_transfers
            ]
            status = "Active transfer: " + ", ".join(descriptions)
        else:
            status = "No liquid transfer currently running"
        painter.drawText(18, self.height() - 18, status)

    def _node_positions(
        self,
        nodes: list[dict[str, Any]],
        connections: list[dict[str, Any]],
    ) -> dict[str, QPointF]:
        rows = self._hierarchy_rows(nodes, connections)
        if not rows:
            return {}

        top = 34.0
        bottom = max(top, self.height() - 76.0)
        row_gap = 0.0 if len(rows) == 1 else (bottom - top) / (len(rows) - 1)
        positions: dict[str, QPointF] = {}
        for row_index, names in enumerate(rows):
            y = top + row_index * row_gap
            horizontal_gap = self.width() / (len(names) + 1)
            for column_index, name in enumerate(names):
                positions[name] = QPointF(
                    horizontal_gap * (column_index + 1),
                    y,
                )
        return positions

    @staticmethod
    def _hierarchy_rows(
        nodes: list[dict[str, Any]],
        connections: list[dict[str, Any]],
    ) -> list[list[str]]:
        node_names = [str(node["name"]) for node in nodes]
        node_kinds = {
            str(node["name"]): str(node.get("kind", ""))
            for node in nodes
        }
        neighbors = {name: [] for name in node_names}
        for connection in connections:
            a = str(connection["a"])
            b = str(connection["b"])
            if a in neighbors and b in neighbors:
                neighbors[a].append(b)
                neighbors[b].append(a)

        roots = [name for name in node_names if node_kinds[name] == "reactor"]
        if not roots and node_names:
            roots = [node_names[0]]

        levels: dict[str, int] = {}
        queue = deque()
        for root in roots:
            levels[root] = 0
            queue.append(root)

        while queue:
            current = queue.popleft()
            for neighbor in neighbors[current]:
                if neighbor in levels:
                    continue
                levels[neighbor] = levels[current] + 1
                queue.append(neighbor)

        next_level = max(levels.values(), default=-1) + 1
        for name in node_names:
            if name not in levels:
                levels[name] = next_level

        return [
            [name for name in node_names if levels[name] == level]
            for level in range(max(levels.values(), default=-1) + 1)
            if any(node_level == level for node_level in levels.values())
        ]

    @staticmethod
    def _node_color(kind: str, is_device: bool) -> QColor:
        if kind in {"source", "sink", "container", "endpoint"}:
            return QColor("#d9edf7")
        if kind == "reactor":
            return QColor("#f6d8a8")
        if kind == "valve":
            return QColor("#e4daf5")
        if "pump" in kind:
            return QColor("#d5ead8")
        return QColor("#e6e8eb" if is_device else "#d9edf7")


class ChemistryMainWindow(QMainWindow):
    def __init__(self, context: GuiContext) -> None:
        super().__init__()
        self.context = context
        self.setWindowTitle("Chemistry Setup Controller")

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(1000)
        self.refresh_timer.timeout.connect(self.refresh_live_state)

        self.snapshot_data: dict[str, Any] = {}
        self._ui_queue: Queue = Queue()
        self._animation_tick = 0
        self._busy_actions = 0
        self._action_buttons: list[QPushButton] = []
        self._button_styles = {
            "default": "",
            "active": "background-color: #3fa34d; color: white;",
            "stopped": "background-color: #c94c4c; color: white;",
        }
        self._emergency_step = 0
        self._emergency_sequence_token = 0

        self._build_ui()
        self._collect_action_buttons()
        self._apply_initial_geometry()
        self.refresh_snapshot()
        self.refresh_timer.start()

    def _build_ui(self) -> None:
        refresh_action = QAction("Refresh", self)
        refresh_action.triggered.connect(self.refresh_snapshot)
        self.toolbar = self.addToolBar("Main")
        self.toolbar.setMovable(False)
        self.toolbar.addAction(refresh_action)
        self.emergency_buttons = [
            QPushButton("STOP 1"),
            QPushButton("STOP 2"),
            QPushButton("STOP 3"),
        ]
        for index, button in enumerate(self.emergency_buttons):
            button.setMinimumWidth(110)
            button.clicked.connect(
                lambda _checked=False, step=index + 1: self._advance_emergency_stop(step)
            )
        self._reset_emergency_stop_sequence()

        central = QWidget()
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(12)

        emergency_row = QHBoxLayout()
        emergency_row.addStretch(1)
        for button in self.emergency_buttons:
            emergency_row.addWidget(button)
        emergency_row.addStretch(1)
        root_layout.addLayout(emergency_row)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_overview_tab(), "Overview")
        self.tabs.addTab(self._build_topology_tab(), "Topology")
        self.tabs.addTab(self._build_operations_tab(), "Operations")
        self.tabs.addTab(self._build_devices_tab(), "Devices")
        self.tabs.addTab(self._build_logging_tab(), "Logging")
        self.tabs.addTab(self._build_experiments_tab(), "Experiments")
        if self.context.expert_mode:
            self.tabs.addTab(self._build_snapshot_tab(), "Snapshot")

        root_layout.addWidget(self.tabs)
        self.setCentralWidget(central)

    def _collect_action_buttons(self) -> None:
        buttons: list[QPushButton] = []
        for name in (
            "move_button",
            "fill_button",
            "empty_button",
            "clean_button",
            "logging_start_button",
            "logging_pause_button",
            "logging_resume_button",
            "logging_stop_button",
            "logging_comment_button",
            "experiment_start_button",
            "experiment_pause_button",
            "experiment_resume_button",
            "experiment_stop_button",
        ):
            button = getattr(self, name, None)
            if isinstance(button, QPushButton):
                buttons.append(button)
        for panel in self.device_panels.values():
            for key in (
                "set_button",
                "start_button",
                "stop_button",
                "run_button",
                "stir_start_button",
                "stir_stop_button",
                "heat_start_button",
                "heat_stop_button",
                "read_button",
                "rts_toggle_button",
                "serial_read_button",
            ):
                button = panel.get(key)
                if isinstance(button, QPushButton):
                    buttons.append(button)
        for panel in getattr(self, "endpoint_maintenance_panels", {}).values():
            for key in ("refill_button", "empty_button"):
                button = panel.get(key)
                if isinstance(button, QPushButton):
                    buttons.append(button)

        deduped: list[QPushButton] = []
        seen: set[int] = set()
        for button in buttons:
            button_id = id(button)
            if button_id in seen:
                continue
            seen.add(button_id)
            deduped.append(button)
        self._action_buttons = deduped
        self._apply_action_button_lock()

    def _set_action_button_state(self, button: QPushButton, enabled: bool) -> None:
        allow_when_busy = bool(button.property("_allow_when_busy"))
        button.setProperty("_base_enabled", bool(enabled))
        button.setEnabled(bool(enabled) and (self._busy_actions == 0 or allow_when_busy))

    def _apply_action_button_lock(self) -> None:
        busy = self._busy_actions > 0
        for button in self._action_buttons:
            base_enabled = button.property("_base_enabled")
            if base_enabled is None:
                base_enabled = button.isEnabled()
                button.setProperty("_base_enabled", bool(base_enabled))
            allow_when_busy = bool(button.property("_allow_when_busy"))
            button.setEnabled(bool(base_enabled) and (not busy or allow_when_busy))

    def _apply_initial_geometry(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self.resize(1320, 860)
            return

        available = screen.availableGeometry()
        width = max(900, int(available.width() * 0.5))
        height = max(650, int(available.height() * 0.5))
        width = min(width, available.width())
        height = min(height, available.height())

        x = available.x() + (available.width() - width) // 2
        y = available.y() + (available.height() - height) // 2
        self.setGeometry(x, y, width, height)

    def _build_overview_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(10)

        self.overview_devices_group = QGroupBox("Devices")
        self.overview_devices_layout = QVBoxLayout(self.overview_devices_group)
        self.overview_devices_layout.setContentsMargins(10, 10, 10, 10)
        self.overview_devices_layout.setSpacing(8)
        top_row.addWidget(self.overview_devices_group, 1)

        self.overview_endpoints_group = QGroupBox("Endpoints")
        self.overview_endpoints_layout = QVBoxLayout(self.overview_endpoints_group)
        self.overview_endpoints_layout.setContentsMargins(10, 10, 10, 10)
        self.overview_endpoints_layout.setSpacing(8)
        top_row.addWidget(self.overview_endpoints_group, 1)

        layout.addLayout(top_row)

        self.overview_loggers_group = QGroupBox("Loggers")
        self.overview_loggers_layout = QVBoxLayout(self.overview_loggers_group)
        self.overview_loggers_layout.setContentsMargins(10, 10, 10, 10)
        self.overview_loggers_layout.setSpacing(8)
        layout.addWidget(self.overview_loggers_group)

        self.overview_experiments_group = QGroupBox("Experiments")
        self.overview_experiments_layout = QVBoxLayout(self.overview_experiments_group)
        self.overview_experiments_layout.setContentsMargins(10, 10, 10, 10)
        self.overview_experiments_layout.setSpacing(8)
        layout.addWidget(self.overview_experiments_group)

        layout.addStretch(1)

        return tab

    def _build_topology_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        self.topology_graph = TopologyGraphWidget()
        layout.addWidget(self.topology_graph)
        return tab

    def _build_devices_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)

        self.device_panels: dict[str, dict[str, Any]] = {}
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        self.devices_layout = QVBoxLayout(scroll_content)
        self.devices_layout.setContentsMargins(0, 0, 0, 0)
        self.devices_layout.setSpacing(12)
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll)

        self.endpoints_tree = self._make_tree()
        self.endpoints_box = self._wrap_widget("Endpoints", self.endpoints_tree)
        self.endpoints_box.setVisible(self.context.expert_mode)
        layout.addWidget(self.endpoints_box)

        self._build_device_panels()
        return tab

    def _build_operations_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.move_liquid_group = QGroupBox("Move Liquid")
        move_layout = QFormLayout(self.move_liquid_group)
        self.move_source_input = NoWheelComboBox()
        self.move_destination_input = NoWheelComboBox()
        self.move_volume_input = NoWheelDoubleSpinBox()
        self.move_volume_input.setRange(0.1, 1000.0)
        self.move_volume_input.setDecimals(2)
        self.move_volume_input.setSingleStep(0.5)
        self.move_volume_input.setValue(5.0)
        self.move_peristaltic_rpm_input = NoWheelSpinBox()
        self.move_peristaltic_rpm_input.setRange(0, 999)
        self.move_peristaltic_rpm_input.setSpecialValueText("auto")
        self.move_peristaltic_flow_input = NoWheelDoubleSpinBox()
        self.move_peristaltic_flow_input.setRange(0.0, 1000.0)
        self.move_peristaltic_flow_input.setDecimals(2)
        self.move_peristaltic_flow_input.setSingleStep(1.0)
        self.move_peristaltic_flow_input.setSpecialValueText("auto")
        self.move_button = QPushButton("Move Liquid")
        self.move_status_label = QLabel("-")
        self.move_status_label.setWordWrap(True)
        move_controls = QWidget()
        move_controls_layout = QGridLayout(move_controls)
        move_controls_layout.setContentsMargins(0, 0, 0, 0)
        move_controls_layout.setHorizontalSpacing(14)
        move_controls_layout.setVerticalSpacing(4)
        move_controls_layout.addWidget(_stacked_field("Source", self.move_source_input), 0, 0)
        move_controls_layout.addWidget(_stacked_field("Destination", self.move_destination_input), 0, 1)
        move_controls_layout.addWidget(_stacked_field("Volume (mL)", self.move_volume_input), 0, 2)
        move_controls_layout.addWidget(_stacked_field("RPM", self.move_peristaltic_rpm_input), 0, 3)
        move_controls_layout.addWidget(_stacked_field("Flow (mL/min)", self.move_peristaltic_flow_input), 0, 4)
        move_controls_layout.addWidget(self.move_button, 0, 5, 1, 1, Qt.AlignBottom)
        move_controls_layout.setColumnStretch(5, 1)
        move_layout.addRow(move_controls)
        move_layout.addRow("Status:", self.move_status_label)
        self.move_button.clicked.connect(self._run_move_liquid_operation)
        layout.addWidget(self.move_liquid_group)

        self.reactor_ops_group = QGroupBox("Reactor Operations")
        reactor_layout = QVBoxLayout(self.reactor_ops_group)

        fill_row = QGridLayout()
        self.fill_source_input = NoWheelComboBox()
        self.fill_volume_input = NoWheelDoubleSpinBox()
        self.fill_volume_input.setRange(0.1, 1000.0)
        self.fill_volume_input.setDecimals(2)
        self.fill_volume_input.setSingleStep(0.5)
        self.fill_volume_input.setValue(5.0)
        self.fill_peristaltic_rpm_input = NoWheelSpinBox()
        self.fill_peristaltic_rpm_input.setRange(0, 999)
        self.fill_peristaltic_rpm_input.setSpecialValueText("auto")
        self.fill_peristaltic_flow_input = NoWheelDoubleSpinBox()
        self.fill_peristaltic_flow_input.setRange(0.0, 1000.0)
        self.fill_peristaltic_flow_input.setDecimals(2)
        self.fill_peristaltic_flow_input.setSingleStep(1.0)
        self.fill_peristaltic_flow_input.setSpecialValueText("auto")
        self.fill_button = QPushButton("Fill Reactor")
        fill_row.setHorizontalSpacing(14)
        fill_row.setVerticalSpacing(4)
        fill_row.addWidget(_stacked_field("Source", self.fill_source_input), 0, 0)
        fill_row.addWidget(_stacked_field("Volume (mL)", self.fill_volume_input), 0, 1)
        fill_row.addWidget(_stacked_field("RPM", self.fill_peristaltic_rpm_input), 0, 2)
        fill_row.addWidget(_stacked_field("Flow", self.fill_peristaltic_flow_input), 0, 3)
        fill_row.addWidget(self.fill_button, 0, 4, 1, 1, Qt.AlignBottom)
        fill_row.setColumnStretch(4, 1)
        self.fill_button.clicked.connect(self._run_fill_reactor_operation)
        reactor_layout.addLayout(fill_row)

        empty_row = QGridLayout()
        self.empty_destination_input = NoWheelComboBox()
        self.empty_volume_input = NoWheelDoubleSpinBox()
        self.empty_volume_input.setRange(0.0, 1000.0)
        self.empty_volume_input.setDecimals(2)
        self.empty_volume_input.setSingleStep(0.5)
        self.empty_volume_input.setSpecialValueText("all known volume")
        self.empty_peristaltic_rpm_input = NoWheelSpinBox()
        self.empty_peristaltic_rpm_input.setRange(0, 999)
        self.empty_peristaltic_rpm_input.setSpecialValueText("auto")
        self.empty_peristaltic_flow_input = NoWheelDoubleSpinBox()
        self.empty_peristaltic_flow_input.setRange(0.0, 1000.0)
        self.empty_peristaltic_flow_input.setDecimals(2)
        self.empty_peristaltic_flow_input.setSingleStep(1.0)
        self.empty_peristaltic_flow_input.setSpecialValueText("auto")
        self.empty_button = QPushButton("Empty Reactor")
        empty_row.setHorizontalSpacing(14)
        empty_row.setVerticalSpacing(4)
        empty_row.addWidget(_stacked_field("Destination", self.empty_destination_input), 0, 0)
        empty_row.addWidget(_stacked_field("Volume (mL)", self.empty_volume_input), 0, 1)
        empty_row.addWidget(_stacked_field("RPM", self.empty_peristaltic_rpm_input), 0, 2)
        empty_row.addWidget(_stacked_field("Flow", self.empty_peristaltic_flow_input), 0, 3)
        empty_row.addWidget(self.empty_button, 0, 4, 1, 1, Qt.AlignBottom)
        empty_row.setColumnStretch(4, 1)
        self.empty_button.clicked.connect(self._run_empty_reactor_operation)
        reactor_layout.addLayout(empty_row)

        cleaning_row = QGridLayout()
        self.clean_cycles_input = NoWheelSpinBox()
        self.clean_cycles_input.setRange(1, 20)
        self.clean_cycles_input.setValue(1)
        self.clean_volume_input = NoWheelDoubleSpinBox()
        self.clean_volume_input.setRange(0.0, 1000.0)
        self.clean_volume_input.setDecimals(2)
        self.clean_volume_input.setSingleStep(0.5)
        self.clean_volume_input.setValue(20.0)
        self.clean_wait_input = NoWheelDoubleSpinBox()
        self.clean_wait_input.setRange(0.0, 3600.0)
        self.clean_wait_input.setDecimals(1)
        self.clean_wait_input.setValue(0.0)
        self.clean_stir_rpm_input = NoWheelSpinBox()
        self.clean_stir_rpm_input.setRange(0, 2000)
        self.clean_stir_rpm_input.setSpecialValueText("off")
        self.clean_stir_rpm_input.setValue(0)
        self.clean_peristaltic_rpm_input = NoWheelSpinBox()
        self.clean_peristaltic_rpm_input.setRange(0, 999)
        self.clean_peristaltic_rpm_input.setSpecialValueText("auto")
        self.clean_peristaltic_flow_input = NoWheelDoubleSpinBox()
        self.clean_peristaltic_flow_input.setRange(0.0, 1000.0)
        self.clean_peristaltic_flow_input.setDecimals(2)
        self.clean_peristaltic_flow_input.setSingleStep(1.0)
        self.clean_peristaltic_flow_input.setSpecialValueText("auto")
        self.clean_button = QPushButton("Clean Reactor")
        cleaning_row.setHorizontalSpacing(14)
        cleaning_row.setVerticalSpacing(4)
        cleaning_row.addWidget(_stacked_field("Cycles", self.clean_cycles_input), 0, 0)
        cleaning_row.addWidget(_stacked_field("Fill volume (mL)", self.clean_volume_input), 0, 1)
        cleaning_row.addWidget(_stacked_field("Wait (s)", self.clean_wait_input), 0, 2)
        cleaning_row.addWidget(_stacked_field("Stir RPM", self.clean_stir_rpm_input), 0, 3)
        cleaning_row.addWidget(_stacked_field("Pump RPM", self.clean_peristaltic_rpm_input), 0, 4)
        cleaning_row.addWidget(_stacked_field("Flow", self.clean_peristaltic_flow_input), 0, 5)
        cleaning_row.addWidget(self.clean_button, 0, 6, 1, 1, Qt.AlignBottom)
        cleaning_row.setColumnStretch(6, 1)
        self.clean_button.clicked.connect(self._run_cleaning_operation)
        reactor_layout.addLayout(cleaning_row)

        self.reactor_status_label = QLabel("-")
        self.reactor_status_label.setWordWrap(True)
        reactor_layout.addWidget(self.reactor_status_label)
        layout.addWidget(self.reactor_ops_group)

        self.endpoint_maintenance_group = QGroupBox("Endpoint Maintenance")
        self.endpoint_maintenance_layout = QVBoxLayout(self.endpoint_maintenance_group)
        self.endpoint_maintenance_layout.setContentsMargins(10, 10, 10, 10)
        self.endpoint_maintenance_layout.setSpacing(8)
        self.endpoint_maintenance_panels: dict[str, dict[str, Any]] = {}
        layout.addWidget(self.endpoint_maintenance_group)

        layout.addStretch(1)
        return tab

    def _build_logging_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(10)

        self.logging_status_group = QGroupBox("Logger Status")
        form = QFormLayout(self.logging_status_group)
        form.setContentsMargins(10, 10, 10, 10)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        self.logging_target_input = NoWheelComboBox()
        self.logging_state_value = QLabel("-")
        self.logging_file_value = QLabel("-")
        self.logging_actions_value = QLabel("-")
        form.addRow("Target:", self.logging_target_input)
        form.addRow("State:", self.logging_state_value)
        form.addRow("Log file:", self.logging_file_value)
        form.addRow("Actions:", self.logging_actions_value)
        top_row.addWidget(self.logging_status_group, 1)

        self.logging_controls_group = QGroupBox("Logger Control")
        controls_layout = QVBoxLayout(self.logging_controls_group)
        controls_layout.setContentsMargins(10, 10, 10, 10)
        controls_layout.setSpacing(8)
        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(8)
        self.logging_start_button = QPushButton("Start")
        self.logging_pause_button = QPushButton("Pause")
        self.logging_resume_button = QPushButton("Resume")
        self.logging_stop_button = QPushButton("Stop")
        self.logging_stop_button.setProperty("_allow_when_busy", True)
        buttons_row.addWidget(self.logging_start_button)
        buttons_row.addWidget(self.logging_pause_button)
        buttons_row.addWidget(self.logging_resume_button)
        buttons_row.addWidget(self.logging_stop_button)
        buttons_row.addStretch(1)
        controls_layout.addLayout(buttons_row)

        comment_row = QHBoxLayout()
        self.logging_comment_input = QLineEdit()
        self.logging_comment_input.setPlaceholderText("Add log comment")
        self.logging_comment_button = QPushButton("Add Comment")
        comment_row.addWidget(self.logging_comment_input)
        comment_row.addWidget(self.logging_comment_button)
        controls_layout.addLayout(comment_row)
        top_row.addWidget(self.logging_controls_group, 1)

        layout.addLayout(top_row)

        self.logging_last_entry_box = QPlainTextEdit()
        self.logging_last_entry_box.setReadOnly(True)
        self.logging_last_entry_box.setMaximumBlockCount(1)
        self.logging_last_entry_box.setFixedHeight(54)
        layout.addWidget(self._wrap_widget("Last Entry", self.logging_last_entry_box))

        self.loggers_tree = self._make_tree()
        self.loggers_box = self._wrap_widget("Data Loggers", self.loggers_tree)
        self.loggers_box.setVisible(self.context.expert_mode)
        layout.addWidget(self.loggers_box)
        layout.addStretch(1)

        self.logging_start_button.clicked.connect(lambda: self._trigger_logging_action("start"))
        self.logging_pause_button.clicked.connect(lambda: self._trigger_logging_action("pause"))
        self.logging_resume_button.clicked.connect(lambda: self._trigger_logging_action("resume"))
        self.logging_stop_button.clicked.connect(lambda: self._trigger_logging_action("stop"))
        self.logging_comment_button.clicked.connect(self._add_logging_comment)
        self.logging_target_input.currentTextChanged.connect(lambda _value: self._update_logging_panel())
        return tab

    def _build_experiments_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(10)

        self.experiment_status_group = QGroupBox("Experiment Status")
        form = QFormLayout(self.experiment_status_group)
        form.setContentsMargins(10, 10, 10, 10)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        self.experiment_runner_input = NoWheelComboBox()
        self.experiment_name_value = QLabel("-")
        self.experiment_state_value = QLabel("-")
        self.experiment_step_value = QLabel("-")
        self.experiment_error_value = QLabel("-")
        self.experiment_remaining_value = QLabel("-")
        form.addRow("Runner:", self.experiment_runner_input)
        form.addRow("Recipe:", self.experiment_name_value)
        form.addRow("Status:", self.experiment_state_value)
        form.addRow("Current step:", self.experiment_step_value)
        form.addRow("Remaining wait:", self.experiment_remaining_value)
        form.addRow("Last error:", self.experiment_error_value)
        top_row.addWidget(self.experiment_status_group, 2)

        self.experiment_actions_group = QGroupBox("Available Actions")
        actions_layout = QVBoxLayout(self.experiment_actions_group)
        actions_layout.setContentsMargins(10, 10, 10, 10)
        actions_layout.setSpacing(8)
        self.experiment_actions_label = QLabel("-")
        self.experiment_actions_label.setWordWrap(True)
        actions_layout.addWidget(self.experiment_actions_label)
        top_row.addWidget(self.experiment_actions_group, 1)

        layout.addLayout(top_row)

        self.experiment_controls_group = QGroupBox("Experiment Control")
        controls_layout = QHBoxLayout(self.experiment_controls_group)
        controls_layout.setContentsMargins(10, 10, 10, 10)
        controls_layout.setSpacing(8)
        self.experiment_start_button = QPushButton("Start")
        self.experiment_pause_button = QPushButton("Pause")
        self.experiment_resume_button = QPushButton("Resume")
        self.experiment_stop_button = QPushButton("Stop")
        self.experiment_stop_button.setProperty("_allow_when_busy", True)
        controls_layout.addWidget(self.experiment_start_button)
        controls_layout.addWidget(self.experiment_pause_button)
        controls_layout.addWidget(self.experiment_resume_button)
        controls_layout.addWidget(self.experiment_stop_button)
        controls_layout.addStretch(1)
        layout.addWidget(self.experiment_controls_group)

        self.experiment_start_button.clicked.connect(lambda: self._trigger_experiment_action("start"))
        self.experiment_pause_button.clicked.connect(lambda: self._trigger_experiment_action("pause"))
        self.experiment_resume_button.clicked.connect(lambda: self._trigger_experiment_action("resume"))
        self.experiment_stop_button.clicked.connect(lambda: self._trigger_experiment_action("stop"))
        self.experiment_runner_input.currentTextChanged.connect(lambda _value: self._update_experiment_panel())

        self.experiments_tree = self._make_tree()
        self.experiments_box = self._wrap_widget("All Experiment Runners", self.experiments_tree)
        self.experiments_box.setVisible(self.context.expert_mode)
        layout.addWidget(self.experiments_box)
        layout.addStretch(1)
        return tab

    def _build_snapshot_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)

        self.snapshot_text = QPlainTextEdit()
        self.snapshot_text.setReadOnly(True)
        self.snapshot_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(self.snapshot_text)
        return tab

    def refresh_snapshot(self) -> None:
        self._refresh_serial_cable_states()
        self.snapshot_data = self.context.snapshot()
        self._rebuild_static_views()
        self._update_live_panels()

    def refresh_live_state(self) -> None:
        self._drain_ui_queue()
        self._animation_tick += 1
        self._refresh_serial_cable_states()
        self.snapshot_data = self.context.snapshot()
        self._update_live_panels()

    def _refresh_serial_cable_states(self) -> None:
        for name, node in self.context.topology.nodes.items():
            if getattr(node, "kind", None) != "serial_cable":
                continue
            try:
                read_serial_cable_values(name, topology=self.context.topology)
            except Exception:
                continue

    def _rebuild_static_views(self) -> None:
        self._update_overview()
        self._sync_operation_options()
        self._populate_tree(self.endpoints_tree, self.snapshot_data.get("endpoints", {}))
        self._populate_tree(self.loggers_tree, self.snapshot_data.get("loggers", {}))
        self._populate_tree(self.experiments_tree, self.snapshot_data.get("experiments", {}))
        if hasattr(self, "snapshot_text"):
            self.snapshot_text.setPlainText(json.dumps(_display_payload(self.snapshot_data), indent=2))

    def _update_live_panels(self) -> None:
        self.topology_graph.update_graph(
            self.snapshot_data.get("topology", {}),
            self._animation_tick,
        )
        self._sync_control_selectors()
        self._update_overview()
        self._update_logging_panel()
        self._update_experiment_panel()
        self._update_device_panels()
        self._update_endpoint_maintenance_panels()

    def _update_overview(self) -> None:
        devices = self.snapshot_data.get("devices", {})
        endpoints = self.snapshot_data.get("endpoints", {})
        loggers = self.snapshot_data.get("loggers", {})
        experiments = self.snapshot_data.get("experiments", {})

        self._replace_overview_cards(
            self.overview_devices_layout,
            [
                self._overview_card(name, self._overview_device_buttons(snapshot))
                for name, snapshot in devices.items()
            ],
            empty_text="No devices",
        )
        self._replace_overview_cards(
            self.overview_endpoints_layout,
            [
                self._overview_card(name, [self._overview_volume_button(snapshot)])
                for name, snapshot in endpoints.items()
            ],
            empty_text="No endpoints",
        )
        self._replace_overview_cards(
            self.overview_loggers_layout,
            [
                self._overview_card(
                    name,
                    [self._overview_logger_button(snapshot)],
                    detail=_display_text(snapshot.get("last_entry", "-") or "-"),
                )
                for name, snapshot in loggers.items()
            ],
            empty_text="No loggers",
        )
        self._replace_overview_cards(
            self.overview_experiments_layout,
            [
                self._overview_card(
                    name,
                    [self._overview_experiment_button(snapshot)],
                    detail=_format_current_step_detail(snapshot.get("current_step")),
                )
                for name, snapshot in experiments.items()
            ],
            empty_text="No experiments",
        )

    def _update_experiment_panel(self) -> None:
        experiments = self.snapshot_data.get("experiments", {})
        if not experiments:
            self._set_combo_items(self.experiment_runner_input, [])
            self.experiment_name_value.setText("-")
            self.experiment_state_value.setText("No runner attached")
            self.experiment_step_value.setText("-")
            self.experiment_error_value.setText("-")
            self.experiment_remaining_value.setText("-")
            self.experiment_actions_label.setText("Attach a runner or queue to populate this tab.")
            self._set_experiment_buttons([])
            return

        runner_name = self._selected_runner_name()
        if runner_name is None:
            self.experiment_name_value.setText("-")
            self.experiment_state_value.setText("No runner selected")
            self.experiment_step_value.setText("-")
            self.experiment_error_value.setText("-")
            self.experiment_remaining_value.setText("-")
            self.experiment_actions_label.setText("Select a runner to control it.")
            self._set_experiment_buttons([])
            return

        runner_snapshot = experiments.get(runner_name, {})
        current_step = runner_snapshot.get("current_step")
        current_step_text = "-"
        if isinstance(current_step, dict):
            current_step_text = current_step.get("label") or current_step.get("action") or "-"

        self.experiment_name_value.setText(str(runner_snapshot.get("recipe_name", runner_name)))
        self.experiment_state_value.setText(str(runner_snapshot.get("status", "-")))
        self.experiment_step_value.setText(str(current_step_text))
        self.experiment_error_value.setText(str(runner_snapshot.get("last_error") or "-"))
        remaining = runner_snapshot.get("remaining_wait_s")
        self.experiment_remaining_value.setText("-" if remaining is None else f"{_format_float(remaining)} s")
        actions = runner_snapshot.get("interactive_actions", [])
        self.experiment_actions_label.setText(", ".join(actions) if actions else "No actions available")
        self._set_experiment_buttons(actions)

    def _update_logging_panel(self) -> None:
        target_name = self._selected_logging_target_name()
        if target_name is None:
            self.logging_state_value.setText("No logging-capable device found")
            self.logging_file_value.setText("-")
            self.logging_actions_value.setText("-")
            self.logging_last_entry_box.setPlainText("-")
            self._set_logging_buttons([])
            return

        devices = self.snapshot_data.get("devices", {})
        device_snapshot = devices.get(target_name, {})
        state = device_snapshot.get("state", {})
        loggers = self.snapshot_data.get("loggers", {})
        logger_snapshot = loggers.get(target_name)

        active = bool(state.get("logging_active", False))
        paused = bool(state.get("logging_paused", False))
        if active and paused:
            status_text = "paused"
        elif active:
            status_text = "active"
        else:
            status_text = "idle"

        actions = _logging_actions_for_state(active=active, paused=paused)
        self.logging_state_value.setText(status_text)
        self.logging_file_value.setText(_display_text((logger_snapshot or {}).get("log_file", "-")))
        self.logging_actions_value.setText(", ".join(actions) if actions else "No actions available")
        self.logging_last_entry_box.setPlainText(_display_text((logger_snapshot or {}).get("last_entry", "-") or "-"))
        self._set_logging_buttons(actions)

    def _set_experiment_buttons(self, actions: list[str]) -> None:
        enabled = set(actions)
        self._set_action_button_state(self.experiment_start_button, "start" in enabled)
        self._set_action_button_state(self.experiment_pause_button, "pause" in enabled)
        self._set_action_button_state(self.experiment_resume_button, "resume" in enabled)
        self._set_action_button_state(self.experiment_stop_button, "stop" in enabled)

    def _set_logging_buttons(self, actions: list[str]) -> None:
        enabled = set(actions)
        self._set_action_button_state(self.logging_start_button, "start" in enabled)
        self._set_action_button_state(self.logging_pause_button, "pause" in enabled)
        self._set_action_button_state(self.logging_resume_button, "resume" in enabled)
        self._set_action_button_state(self.logging_stop_button, "stop" in enabled)
        self._set_action_button_state(self.logging_comment_button, True)

    def _primary_runner(self) -> tuple[str | None, Any | None]:
        runner_name = self._selected_runner_name()
        if runner_name is None:
            return None, None
        return runner_name, self.context.experiment_runners[runner_name]

    def _logging_target_names(self) -> list[str]:
        devices = self.snapshot_data.get("devices", {})
        target_names: list[str] = []
        for name, snapshot in devices.items():
            interactive_actions = snapshot.get("interactive_actions", [])
            if "start_data_logging" in interactive_actions or "stop_data_logging" in interactive_actions:
                target_names.append(str(name))
        return target_names

    def _selected_logging_target_name(self) -> str | None:
        target_name = self.logging_target_input.currentText()
        return target_name or None

    def _selected_runner_name(self) -> str | None:
        runner_name = self.experiment_runner_input.currentText()
        if runner_name not in self.context.experiment_runners:
            return None
        return runner_name

    def _sync_control_selectors(self) -> None:
        self._set_combo_items(self.logging_target_input, self._logging_target_names())
        self._set_combo_items(self.experiment_runner_input, list(self.context.experiment_runners))

    def _sync_operation_options(self) -> None:
        operations = self.snapshot_data.get("operations", {})
        move_spec = operations.get("move_liquid", {})
        transfer_nodes = list(move_spec.get("sources", []))

        self._set_combo_items(self.move_source_input, transfer_nodes)
        self._set_combo_items(self.move_destination_input, list(move_spec.get("destinations", transfer_nodes)))
        self._set_combo_items(self.fill_source_input, transfer_nodes)
        self._set_combo_items(self.empty_destination_input, transfer_nodes)

    def _build_device_panels(self) -> None:
        devices = self.snapshot_data.get("devices", {})
        while self.devices_layout.count():
            item = self.devices_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.device_panels.clear()

        for device_name, device_snapshot in devices.items():
            if not device_snapshot.get("manual_actions"):
                continue
            panel = self._create_device_panel(device_name, device_snapshot)
            self.device_panels[device_name] = panel
            self.devices_layout.addWidget(panel["group"])

        self.devices_layout.addStretch(1)
        self._collect_action_buttons()

    def _create_device_panel(self, device_name: str, device_snapshot: dict[str, Any]) -> dict[str, Any]:
        group = QGroupBox(f"{device_name} ({device_snapshot.get('kind', '-')})")
        layout = QVBoxLayout(group)

        form = QFormLayout()
        status_value = QLabel("-")
        form.addRow("Current status:", status_value)
        layout.addLayout(form)

        helper_label = QLabel("Manual controls for this device type are not wired into the GUI yet.")
        helper_label.setWordWrap(True)
        layout.addWidget(helper_label)

        panel: dict[str, Any] = {
            "group": group,
            "status": status_value,
            "helper": helper_label,
            "kind": str(device_snapshot.get("kind", "-")),
        }

        if panel["kind"] == "valve":
            valve_group = QGroupBox("Valve Control")
            valve_layout = QHBoxLayout(valve_group)
            port_input = NoWheelSpinBox()
            port_input.setRange(1, 10)
            port_input.valueChanged.connect(lambda _value, name=device_name: self._mark_valve_input_dirty(name))
            set_button = QPushButton("Set Port")
            set_button.clicked.connect(lambda _checked=False, name=device_name: self._set_valve_port(name))
            valve_layout.addWidget(QLabel("Port"))
            valve_layout.addWidget(port_input)
            valve_layout.addWidget(set_button)
            valve_layout.addStretch(1)
            layout.addWidget(valve_group)
            helper_label.setText("Direct valve control is available here.")
            panel["valve_group"] = valve_group
            panel["port_input"] = port_input
            panel["set_button"] = set_button
            panel["dirty"] = False

        elif panel["kind"] == "peristaltic_pump":
            pump_group = QGroupBox("Pump Control")
            pump_layout = QHBoxLayout(pump_group)
            rpm_input = NoWheelSpinBox()
            rpm_input.setRange(1, 999)
            rpm_input.setValue(300)
            direction_input = NoWheelComboBox()
            direction_input.addItems(["CW", "CCW"])
            spinner_label = QLabel("-")
            spinner_label.setMinimumWidth(20)
            spinner_label.setAlignment(Qt.AlignCenter)
            start_button = QPushButton("Run")
            stop_button = QPushButton("Stop")
            stop_button.setProperty("_allow_when_busy", True)
            start_button.clicked.connect(lambda _checked=False, name=device_name: self._run_peristaltic_from_panel(name))
            stop_button.clicked.connect(lambda _checked=False, name=device_name: self._stop_peristaltic_from_panel(name))
            pump_layout.addWidget(QLabel("RPM"))
            pump_layout.addWidget(rpm_input)
            pump_layout.addWidget(QLabel("Direction"))
            pump_layout.addWidget(direction_input)
            pump_layout.addWidget(spinner_label)
            pump_layout.addWidget(start_button)
            pump_layout.addWidget(stop_button)
            pump_layout.addStretch(1)
            layout.addWidget(pump_group)
            helper_label.setText("Manual peristaltic pump control is available here.")
            panel["pump_group"] = pump_group
            panel["rpm_input"] = rpm_input
            panel["direction_input"] = direction_input
            panel["spinner_label"] = spinner_label
            panel["start_button"] = start_button
            panel["stop_button"] = stop_button

        elif panel["kind"] == "syringe_pump":
            syringe_group = QGroupBox("Pump Control")
            syringe_layout = QHBoxLayout(syringe_group)
            volume_input = NoWheelDoubleSpinBox()
            volume_input.setRange(0.1, 100.0)
            volume_input.setDecimals(2)
            volume_input.setSingleStep(0.1)
            volume_input.setValue(5.0)
            rate_input = NoWheelDoubleSpinBox()
            rate_input.setRange(0.1, 100.0)
            rate_input.setDecimals(2)
            rate_input.setSingleStep(0.5)
            rate_input.setValue(5.0)
            direction_input = NoWheelComboBox()
            direction_input.addItems(["WDR", "INF"])
            motion_label = QLabel("-")
            motion_label.setMinimumWidth(26)
            motion_label.setAlignment(Qt.AlignCenter)
            run_button = QPushButton("Run")
            stop_button = QPushButton("Stop")
            stop_button.setProperty("_allow_when_busy", True)
            run_button.clicked.connect(lambda _checked=False, name=device_name: self._run_syringe_from_panel(name))
            stop_button.clicked.connect(lambda _checked=False, name=device_name: self._stop_syringe_from_panel(name))
            syringe_layout.addWidget(QLabel("Volume (mL)"))
            syringe_layout.addWidget(volume_input)
            syringe_layout.addWidget(QLabel("Rate (mL/min)"))
            syringe_layout.addWidget(rate_input)
            syringe_layout.addWidget(QLabel("Direction"))
            syringe_layout.addWidget(direction_input)
            syringe_layout.addWidget(motion_label)
            syringe_layout.addWidget(run_button)
            syringe_layout.addWidget(stop_button)
            syringe_layout.addStretch(1)
            layout.addWidget(syringe_group)
            helper_label.setText("Manual syringe pump control is available here.")
            panel["syringe_group"] = syringe_group
            panel["volume_input"] = volume_input
            panel["rate_input"] = rate_input
            panel["direction_input"] = direction_input
            panel["motion_label"] = motion_label
            panel["run_button"] = run_button
            panel["stop_button"] = stop_button

        elif panel["kind"] == "reactor":
            reactor_group = QGroupBox("Attached Stirrer/Hotplate")
            reactor_layout = QVBoxLayout(reactor_group)

            stir_row = QHBoxLayout()
            stir_rpm_input = NoWheelSpinBox()
            stir_rpm_input.setRange(1, 2000)
            stir_rpm_input.setValue(500)
            stir_start_button = QPushButton("Stir Reactor")
            stir_stop_button = QPushButton("Stop Stirring")
            stir_stop_button.setProperty("_allow_when_busy", True)
            stir_start_button.clicked.connect(
                lambda _checked=False, name=device_name: self._stir_reactor_from_panel(name)
            )
            stir_stop_button.clicked.connect(
                lambda _checked=False, name=device_name: self._stop_reactor_stirring_from_panel(name)
            )
            stir_row.addWidget(QLabel("RPM"))
            stir_row.addWidget(stir_rpm_input)
            stir_row.addWidget(stir_start_button)
            stir_row.addWidget(stir_stop_button)
            stir_row.addStretch(1)
            reactor_layout.addLayout(stir_row)

            heat_row = QHBoxLayout()
            heat_temp_input = NoWheelDoubleSpinBox()
            heat_temp_input.setRange(0.0, 250.0)
            heat_temp_input.setDecimals(1)
            heat_temp_input.setSingleStep(1.0)
            heat_temp_input.setValue(50.0)
            heat_start_button = QPushButton("Heat Reactor")
            heat_stop_button = QPushButton("Stop Heating")
            heat_stop_button.setProperty("_allow_when_busy", True)
            heat_start_button.clicked.connect(
                lambda _checked=False, name=device_name: self._heat_reactor_from_panel(name)
            )
            heat_stop_button.clicked.connect(
                lambda _checked=False, name=device_name: self._stop_reactor_heating_from_panel(name)
            )
            heat_row.addWidget(QLabel("Temp (C)"))
            heat_row.addWidget(heat_temp_input)
            heat_row.addWidget(heat_start_button)
            heat_row.addWidget(heat_stop_button)
            heat_row.addStretch(1)
            reactor_layout.addLayout(heat_row)

            layout.addWidget(reactor_group)
            helper_label.setText("Reactor heat/stir commands use the attached stirrer/hotplate.")
            panel["reactor_group"] = reactor_group
            panel["stir_rpm_input"] = stir_rpm_input
            panel["stir_start_button"] = stir_start_button
            panel["stir_stop_button"] = stir_stop_button
            panel["heat_temp_input"] = heat_temp_input
            panel["heat_start_button"] = heat_start_button
            panel["heat_stop_button"] = heat_stop_button

        elif panel["kind"] in {"stirrer", "magnetic_stirrer", "hotplate"}:
            hotplate_group = QGroupBox("Hotplate Control")
            hotplate_layout = QVBoxLayout(hotplate_group)

            stir_row = QHBoxLayout()
            stir_rpm_input = NoWheelSpinBox()
            stir_rpm_input.setRange(1, 2000)
            stir_rpm_input.setValue(500)
            stir_start_button = QPushButton("Start Stirring")
            stir_stop_button = QPushButton("Stop Stirring")
            stir_stop_button.setProperty("_allow_when_busy", True)
            stir_start_button.clicked.connect(
                lambda _checked=False, name=device_name: self._start_stirring_from_panel(name)
            )
            stir_stop_button.clicked.connect(
                lambda _checked=False, name=device_name: self._stop_stirring_from_panel(name)
            )
            stir_row.addWidget(QLabel("RPM"))
            stir_row.addWidget(stir_rpm_input)
            stir_row.addWidget(stir_start_button)
            stir_row.addWidget(stir_stop_button)
            stir_row.addStretch(1)
            hotplate_layout.addLayout(stir_row)

            heat_row = QHBoxLayout()
            heat_temp_input = NoWheelDoubleSpinBox()
            heat_temp_input.setRange(0.0, 250.0)
            heat_temp_input.setDecimals(1)
            heat_temp_input.setSingleStep(1.0)
            heat_temp_input.setValue(50.0)
            heat_start_button = QPushButton("Start Heating")
            heat_stop_button = QPushButton("Stop Heating")
            heat_stop_button.setProperty("_allow_when_busy", True)
            heat_start_button.clicked.connect(
                lambda _checked=False, name=device_name: self._start_heating_from_panel(name)
            )
            heat_stop_button.clicked.connect(
                lambda _checked=False, name=device_name: self._stop_heating_from_panel(name)
            )
            heat_row.addWidget(QLabel("Temp (C)"))
            heat_row.addWidget(heat_temp_input)
            heat_row.addWidget(heat_start_button)
            heat_row.addWidget(heat_stop_button)
            heat_row.addStretch(1)
            hotplate_layout.addLayout(heat_row)

            read_row = QHBoxLayout()
            read_button = QPushButton("Read Values")
            read_button.clicked.connect(
                lambda _checked=False, name=device_name: self._read_hotplate_from_panel(name)
            )
            read_row.addWidget(read_button)
            read_row.addStretch(1)
            hotplate_layout.addLayout(read_row)

            layout.addWidget(hotplate_group)
            helper_label.setText("Manual hotplate control is available here.")
            panel["hotplate_group"] = hotplate_group
            panel["stir_rpm_input"] = stir_rpm_input
            panel["stir_start_button"] = stir_start_button
            panel["stir_stop_button"] = stir_stop_button
            panel["heat_temp_input"] = heat_temp_input
            panel["heat_start_button"] = heat_start_button
            panel["heat_stop_button"] = heat_stop_button
            panel["read_button"] = read_button

        elif panel["kind"] == "serial_cable":
            serial_group = QGroupBox("Serial Cable")
            serial_layout = QHBoxLayout(serial_group)
            rts_value = QLabel("RTS: -")
            cts_value = QLabel("CTS: -")
            rts_toggle_button = QPushButton("Set RTS ON")
            serial_read_button = QPushButton("Read")
            rts_toggle_button.clicked.connect(
                lambda _checked=False, name=device_name: self._toggle_serial_rts_from_panel(name)
            )
            serial_read_button.clicked.connect(
                lambda _checked=False, name=device_name: self._read_serial_cable_from_panel(name)
            )
            serial_layout.addWidget(rts_value)
            serial_layout.addWidget(cts_value)
            serial_layout.addWidget(rts_toggle_button)
            serial_layout.addWidget(serial_read_button)
            serial_layout.addStretch(1)
            layout.addWidget(serial_group)
            helper_label.setText("RTS can be changed here; CTS is monitored live.")
            panel["serial_group"] = serial_group
            panel["rts_value"] = rts_value
            panel["cts_value"] = cts_value
            panel["rts_toggle_button"] = rts_toggle_button
            panel["serial_read_button"] = serial_read_button

        return panel

    def _update_device_panels(self) -> None:
        devices = self.snapshot_data.get("devices", {})
        manual_device_names = {
            name for name, snapshot in devices.items() if snapshot.get("manual_actions")
        }
        if manual_device_names != set(self.device_panels):
            self._build_device_panels()
            devices = self.snapshot_data.get("devices", {})

        for device_name, device_snapshot in devices.items():
            panel = self.device_panels.get(device_name)
            if panel is None:
                continue
            device_state = device_snapshot.get("state", {})
            panel["status"].setText(_format_state_summary(device_state))

            if panel.get("kind") == "valve":
                current_port = device_state.get("current_port")
                port_input = panel["port_input"]
                if isinstance(current_port, int) and self._should_sync_device_input(device_name):
                    port_input.blockSignals(True)
                    port_input.setValue(current_port)
                    port_input.blockSignals(False)
            elif panel.get("kind") == "peristaltic_pump":
                is_running = bool(device_state.get("running", False))
                self._set_action_button_state(panel["start_button"], True)
                self._set_action_button_state(panel["stop_button"], True)
                panel["start_button"].setStyleSheet(
                    self._button_styles["active"] if is_running else self._button_styles["default"]
                )
                panel["stop_button"].setStyleSheet(
                    self._button_styles["default"] if is_running else self._button_styles["stopped"]
                )
                direction = device_state.get("direction")
                if direction in {"CW", "CCW"} and not panel["direction_input"].hasFocus():
                    panel["direction_input"].setCurrentText(str(direction))
                panel["spinner_label"].setText(_pump_spinner_frame(is_running, direction, self._animation_tick))
            elif panel.get("kind") == "syringe_pump":
                is_running = bool(device_state.get("running", False))
                self._set_action_button_state(panel["run_button"], True)
                self._set_action_button_state(panel["stop_button"], True)
                panel["run_button"].setStyleSheet(
                    self._button_styles["active"] if is_running else self._button_styles["default"]
                )
                panel["stop_button"].setStyleSheet(
                    self._button_styles["default"] if is_running else self._button_styles["stopped"]
                )
                direction = device_state.get("direction")
                if direction in {"WDR", "INF"} and not panel["direction_input"].hasFocus():
                    panel["direction_input"].setCurrentText(str(direction))
                panel["motion_label"].setText(_syringe_motion_frame(is_running, direction, self._animation_tick))
            elif panel.get("kind") == "reactor":
                attached_state = self._attached_hotplate_state_for_reactor(device_name)
                stirring = bool(attached_state.get("stirring", False))
                heating = bool(attached_state.get("heating", False))
                self._set_action_button_state(panel["stir_start_button"], True)
                self._set_action_button_state(panel["stir_stop_button"], True)
                self._set_action_button_state(panel["heat_start_button"], True)
                self._set_action_button_state(panel["heat_stop_button"], True)
                panel["stir_start_button"].setStyleSheet(
                    self._button_styles["active"] if stirring else self._button_styles["default"]
                )
                panel["stir_stop_button"].setStyleSheet(
                    self._button_styles["default"] if stirring else self._button_styles["stopped"]
                )
                panel["heat_start_button"].setStyleSheet(
                    self._button_styles["active"] if heating else self._button_styles["default"]
                )
                panel["heat_stop_button"].setStyleSheet(
                    self._button_styles["default"] if heating else self._button_styles["stopped"]
                )
            elif panel.get("kind") in {"stirrer", "magnetic_stirrer", "hotplate"}:
                stirring = bool(device_state.get("stirring", False))
                heating = bool(device_state.get("heating", False))
                self._set_action_button_state(panel["stir_start_button"], True)
                self._set_action_button_state(panel["stir_stop_button"], True)
                self._set_action_button_state(panel["heat_start_button"], True)
                self._set_action_button_state(panel["heat_stop_button"], True)
                self._set_action_button_state(panel["read_button"], True)
                panel["stir_start_button"].setStyleSheet(
                    self._button_styles["active"] if stirring else self._button_styles["default"]
                )
                panel["stir_stop_button"].setStyleSheet(
                    self._button_styles["default"] if stirring else self._button_styles["stopped"]
                )
                panel["heat_start_button"].setStyleSheet(
                    self._button_styles["active"] if heating else self._button_styles["default"]
                )
                panel["heat_stop_button"].setStyleSheet(
                    self._button_styles["default"] if heating else self._button_styles["stopped"]
                )
            elif panel.get("kind") == "serial_cable":
                rts = bool(device_state.get("rts", False))
                cts = bool(device_state.get("cts", False))
                panel["rts_value"].setText(f"RTS: {_on_off(rts)}")
                panel["cts_value"].setText(f"CTS: {_on_off(cts)}")
                panel["rts_toggle_button"].setText(
                    "Set RTS OFF" if rts else "Set RTS ON"
                )
                self._set_action_button_state(panel["rts_toggle_button"], True)
                self._set_action_button_state(panel["serial_read_button"], True)
                panel["rts_toggle_button"].setStyleSheet(
                    self._button_styles["active"] if rts else self._button_styles["stopped"]
                )
                panel["cts_value"].setStyleSheet(
                    self._button_styles["active"] if cts else self._button_styles["stopped"]
                )

    def _update_endpoint_maintenance_panels(self) -> None:
        endpoints = self.snapshot_data.get("endpoints", {})
        if set(endpoints) != set(self.endpoint_maintenance_panels):
            self._build_endpoint_maintenance_panels(endpoints)

        for endpoint_name, endpoint_snapshot in endpoints.items():
            panel = self.endpoint_maintenance_panels.get(endpoint_name)
            if panel is None:
                continue
            values = _merged_snapshot_values(endpoint_snapshot)
            volume = values.get("volume_ml")
            max_volume = values.get("max_volume_ml")
            if volume is None and max_volume is None:
                panel["status"].setText("volume unknown")
            elif max_volume is None:
                panel["status"].setText(f"{_display_text(volume or 0)} mL")
            else:
                panel["status"].setText(
                    f"{_display_text(volume or 0)}/{_display_text(max_volume)} mL"
                )

            target_input = panel["target_input"]
            if not target_input.hasFocus() and not bool(panel.get("dirty", False)):
                target = max_volume if max_volume is not None else volume
                if target is not None:
                    target_input.blockSignals(True)
                    target_input.setValue(float(target))
                    target_input.blockSignals(False)
            panel["empty_button"].setEnabled(volume is not None and float(volume) > 0)

    def _build_endpoint_maintenance_panels(self, endpoints: dict[str, Any]) -> None:
        self._clear_layout(self.endpoint_maintenance_layout)
        self.endpoint_maintenance_panels.clear()
        if not endpoints:
            self.endpoint_maintenance_layout.addWidget(QLabel("No endpoints"))
            return

        for endpoint_name, endpoint_snapshot in endpoints.items():
            panel = self._create_endpoint_maintenance_panel(endpoint_name, endpoint_snapshot)
            self.endpoint_maintenance_panels[endpoint_name] = panel
            self.endpoint_maintenance_layout.addWidget(panel["row"])
        self._collect_action_buttons()

    def _create_endpoint_maintenance_panel(
        self,
        endpoint_name: str,
        endpoint_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        values = _merged_snapshot_values(endpoint_snapshot)
        max_volume = values.get("max_volume_ml")
        volume = values.get("volume_ml", 0.0)

        row = QWidget()
        layout = QGridLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(4)

        name_label = QLabel(endpoint_name)
        name_label.setMinimumWidth(150)
        status_label = QLabel("-")
        target_input = NoWheelDoubleSpinBox()
        target_input.setRange(0.0, 999999.0)
        target_input.setDecimals(2)
        target_input.setSingleStep(10.0)
        target_input.setValue(float(max_volume if max_volume is not None else volume or 0.0))
        refill_button = QPushButton("Refill")
        empty_button = QPushButton("Empty")

        target_input.valueChanged.connect(
            lambda _value, name=endpoint_name: self._mark_endpoint_target_dirty(name)
        )
        refill_button.clicked.connect(
            lambda _checked=False, name=endpoint_name: self._refill_endpoint(name)
        )
        empty_button.clicked.connect(
            lambda _checked=False, name=endpoint_name: self._empty_endpoint(name)
        )

        layout.addWidget(name_label, 0, 0)
        layout.addWidget(status_label, 0, 1)
        layout.addWidget(_stacked_field("Target (mL)", target_input), 0, 2)
        layout.addWidget(refill_button, 0, 3)
        layout.addWidget(empty_button, 0, 4)
        layout.setColumnStretch(5, 1)

        return {
            "row": row,
            "status": status_label,
            "target_input": target_input,
            "refill_button": refill_button,
            "empty_button": empty_button,
            "dirty": False,
        }

    def _mark_endpoint_target_dirty(self, endpoint_name: str) -> None:
        panel = self.endpoint_maintenance_panels.get(endpoint_name)
        if panel is not None and panel["target_input"].hasFocus():
            panel["dirty"] = True

    def _refill_endpoint(self, endpoint_name: str) -> None:
        panel = self.endpoint_maintenance_panels.get(endpoint_name)
        if panel is None:
            return
        target_volume = float(panel["target_input"].value())
        self.context.topology.update_node_state(
            endpoint_name,
            {"volume_ml": target_volume, "volume_status": "manual"},
            mirror_to_metadata=True,
        )
        panel["dirty"] = False
        self.refresh_snapshot()

    def _empty_endpoint(self, endpoint_name: str) -> None:
        panel = self.endpoint_maintenance_panels.get(endpoint_name)
        if panel is None:
            return
        self.context.topology.update_node_state(
            endpoint_name,
            {"volume_ml": 0.0, "volume_status": "manual"},
            mirror_to_metadata=True,
        )
        panel["dirty"] = False
        self.refresh_snapshot()

    def _mark_valve_input_dirty(self, device_name: str) -> None:
        panel = self.device_panels.get(device_name)
        if panel is None:
            return
        port_input = panel.get("port_input")
        if port_input is not None and port_input.hasFocus():
            panel["dirty"] = True

    def _should_sync_device_input(self, device_name: str) -> bool:
        panel = self.device_panels.get(device_name)
        if panel is None:
            return True
        port_input = panel.get("port_input")
        return not bool(panel.get("dirty", False)) and (port_input is None or not port_input.hasFocus())

    def _attached_hotplate_state_for_reactor(self, reactor_name: str) -> dict[str, Any]:
        for neighbor, _connection in self.context.topology._neighbors.get(reactor_name, []):
            node = self.context.topology.nodes.get(neighbor)
            if node is not None and node.kind in {"stirrer", "magnetic_stirrer", "hotplate"}:
                return dict(node.state)
        return {}

    def _stir_reactor_from_panel(self, reactor_name: str) -> None:
        panel = self.device_panels.get(reactor_name)
        if panel is None:
            return
        rpm = panel["stir_rpm_input"].value()
        panel["helper"].setText(f"Starting stirring for {reactor_name} at {rpm} rpm...")

        self._start_background_task(
            lambda: stir_reactor(
                rpm,
                reactor=reactor_name,
                topology=self.context.topology,
            ),
            on_success=lambda _result, panel=panel: panel["helper"].setText(
                "Reactor heat/stir commands use the attached stirrer/hotplate."
            ),
            on_error=lambda exc, panel=panel: panel["helper"].setText(str(exc)),
        )

    def _stop_reactor_stirring_from_panel(self, reactor_name: str) -> None:
        panel = self.device_panels.get(reactor_name)
        if panel is None:
            return
        panel["helper"].setText(f"Stopping stirring for {reactor_name}...")

        self._start_background_task(
            lambda: stop_reactor_stirring(
                reactor=reactor_name,
                topology=self.context.topology,
            ),
            on_success=lambda _result, panel=panel: panel["helper"].setText(
                "Reactor heat/stir commands use the attached stirrer/hotplate."
            ),
            on_error=lambda exc, panel=panel: panel["helper"].setText(str(exc)),
            reset_abort=False,
        )

    def _heat_reactor_from_panel(self, reactor_name: str) -> None:
        panel = self.device_panels.get(reactor_name)
        if panel is None:
            return
        temp = panel["heat_temp_input"].value()
        panel["helper"].setText(f"Starting heating for {reactor_name} at {_display_text(temp)} C...")

        self._start_background_task(
            lambda: heat_reactor(
                temp,
                reactor=reactor_name,
                topology=self.context.topology,
            ),
            on_success=lambda _result, panel=panel: panel["helper"].setText(
                "Reactor heat/stir commands use the attached stirrer/hotplate."
            ),
            on_error=lambda exc, panel=panel: panel["helper"].setText(str(exc)),
        )

    def _stop_reactor_heating_from_panel(self, reactor_name: str) -> None:
        panel = self.device_panels.get(reactor_name)
        if panel is None:
            return
        panel["helper"].setText(f"Stopping heating for {reactor_name}...")

        self._start_background_task(
            lambda: stop_reactor_heating(
                reactor=reactor_name,
                topology=self.context.topology,
            ),
            on_success=lambda _result, panel=panel: panel["helper"].setText(
                "Reactor heat/stir commands use the attached stirrer/hotplate."
            ),
            on_error=lambda exc, panel=panel: panel["helper"].setText(str(exc)),
            reset_abort=False,
        )

    def _set_valve_port(self, device_name: str) -> None:
        panel = self.device_panels.get(device_name)
        if panel is None:
            return
        target_port = panel["port_input"].value()
        panel["helper"].setText(f"Setting {device_name} to port {target_port}...")

        self._start_background_task(
            lambda: set_valve_position(
                target_port,
                valve=device_name,
                topology=self.context.topology,
                execute=True,
            ),
            on_success=lambda _result, panel=panel: self._finish_valve_action(panel),
            on_error=lambda exc, panel=panel: panel["helper"].setText(str(exc)),
        )

    def _run_peristaltic_from_panel(self, device_name: str) -> None:
        panel = self.device_panels.get(device_name)
        if panel is None:
            return
        rpm = panel["rpm_input"].value()
        direction = panel["direction_input"].currentText()
        preview = run_peristaltic_pump(
            pump=device_name,
            rpm=rpm,
            direction=direction,
            duration="Keep",
            topology=self.context.topology,
            execute=False,
        )
        panel["helper"].setText(
            f"Running {device_name}: {direction} at {preview['rpm']} rpm"
            + (
                f" / {_display_text(preview['rate_ml_min'])} mL/min"
                if preview.get("rate_ml_min") is not None
                else ""
            )
            + "..."
        )

        self._start_background_task(
            lambda: run_peristaltic_pump(
                pump=device_name,
                rpm=rpm,
                direction=direction,
                duration="Keep",
                topology=self.context.topology,
                execute=True,
            ),
            on_success=lambda _result, panel=panel: panel["helper"].setText(
                "Manual peristaltic pump control is available here."
            ),
            on_error=lambda exc, panel=panel: panel["helper"].setText(str(exc)),
        )

    def _stop_peristaltic_from_panel(self, device_name: str) -> None:
        panel = self.device_panels.get(device_name)
        if panel is None:
            return
        self.context.run_control.request_abort()
        panel["helper"].setText(f"Stopping {device_name}...")

        self._start_background_task(
            lambda: stop_peristaltic_pump(
                pump=device_name,
                topology=self.context.topology,
                execute=True,
            ),
            on_success=lambda _result, panel=panel: panel["helper"].setText(
                "Manual peristaltic pump control is available here."
            ),
            on_error=lambda exc, panel=panel: panel["helper"].setText(str(exc)),
            reset_abort=False,
        )

    def _run_syringe_from_panel(self, device_name: str) -> None:
        panel = self.device_panels.get(device_name)
        if panel is None:
            return
        volume_ml = panel["volume_input"].value()
        rate_ml_min = panel["rate_input"].value()
        direction = panel["direction_input"].currentText()
        panel["helper"].setText(
            f"Running {device_name}: {direction} { _display_text(volume_ml) } mL at { _display_text(rate_ml_min) } mL/min..."
        )

        self._start_background_task(
            lambda: run_syringe_pump(
                pump=device_name,
                volume_ml=volume_ml,
                rate_ml_min=rate_ml_min,
                direction=direction,
                topology=self.context.topology,
                execute=True,
            ),
            on_success=lambda _result, panel=panel: panel["helper"].setText(
                "Manual syringe pump control is available here."
            ),
            on_error=lambda exc, panel=panel: panel["helper"].setText(str(exc)),
        )

    def _stop_syringe_from_panel(self, device_name: str) -> None:
        panel = self.device_panels.get(device_name)
        if panel is None:
            return
        self.context.run_control.request_abort()
        panel["helper"].setText(f"Stopping {device_name}...")

        self._start_background_task(
            lambda: stop_syringe_pump(
                pump=device_name,
                topology=self.context.topology,
                execute=True,
            ),
            on_success=lambda _result, panel=panel: panel["helper"].setText(
                "Manual syringe pump control is available here."
            ),
            on_error=lambda exc, panel=panel: panel["helper"].setText(str(exc)),
            reset_abort=False,
        )

    def _start_stirring_from_panel(self, device_name: str) -> None:
        panel = self.device_panels.get(device_name)
        if panel is None:
            return
        rpm = panel["stir_rpm_input"].value()
        panel["helper"].setText(f"Starting stirring on {device_name} at {rpm} rpm...")

        self._start_background_task(
            lambda: start_stirring(
                rpm,
                hotplate=device_name,
                topology=self.context.topology,
            ),
            on_success=lambda _result, panel=panel: panel["helper"].setText(
                "Manual hotplate control is available here."
            ),
            on_error=lambda exc, panel=panel: panel["helper"].setText(str(exc)),
        )

    def _stop_stirring_from_panel(self, device_name: str) -> None:
        panel = self.device_panels.get(device_name)
        if panel is None:
            return
        panel["helper"].setText(f"Stopping stirring on {device_name}...")

        self._start_background_task(
            lambda: stop_stirring(
                hotplate=device_name,
                topology=self.context.topology,
            ),
            on_success=lambda _result, panel=panel: panel["helper"].setText(
                "Manual hotplate control is available here."
            ),
            on_error=lambda exc, panel=panel: panel["helper"].setText(str(exc)),
            reset_abort=False,
        )

    def _start_heating_from_panel(self, device_name: str) -> None:
        panel = self.device_panels.get(device_name)
        if panel is None:
            return
        temp = panel["heat_temp_input"].value()
        panel["helper"].setText(f"Starting heating on {device_name} to { _display_text(temp) } C...")

        self._start_background_task(
            lambda: start_heating(
                temp,
                hotplate=device_name,
                topology=self.context.topology,
            ),
            on_success=lambda _result, panel=panel: panel["helper"].setText(
                "Manual hotplate control is available here."
            ),
            on_error=lambda exc, panel=panel: panel["helper"].setText(str(exc)),
        )

    def _stop_heating_from_panel(self, device_name: str) -> None:
        panel = self.device_panels.get(device_name)
        if panel is None:
            return
        panel["helper"].setText(f"Stopping heating on {device_name}...")

        self._start_background_task(
            lambda: stop_heating(
                hotplate=device_name,
                topology=self.context.topology,
            ),
            on_success=lambda _result, panel=panel: panel["helper"].setText(
                "Manual hotplate control is available here."
            ),
            on_error=lambda exc, panel=panel: panel["helper"].setText(str(exc)),
            reset_abort=False,
        )

    def _read_hotplate_from_panel(self, device_name: str) -> None:
        panel = self.device_panels.get(device_name)
        if panel is None:
            return
        panel["helper"].setText(f"Reading values from {device_name}...")

        self._start_background_task(
            lambda: read_hotplate_values(
                hotplate=device_name,
                topology=self.context.topology,
            ),
            on_success=lambda _result, panel=panel: panel["helper"].setText(
                "Manual hotplate control is available here."
            ),
            on_error=lambda exc, panel=panel: panel["helper"].setText(str(exc)),
        )

    def _toggle_serial_rts_from_panel(self, device_name: str) -> None:
        panel = self.device_panels.get(device_name)
        if panel is None:
            return
        current_rts = bool(self.context.topology.nodes[device_name].state.get("rts", False))
        target_rts = not current_rts
        panel["helper"].setText(f"Setting {device_name} RTS to {_on_off(target_rts)}...")

        self._start_background_task(
            lambda: set_serial_rts(
                target_rts,
                cable=device_name,
                topology=self.context.topology,
            ),
            on_success=lambda _result, panel=panel: panel["helper"].setText(
                "RTS can be changed here; CTS is monitored live."
            ),
            on_error=lambda exc, panel=panel: panel["helper"].setText(str(exc)),
        )

    def _read_serial_cable_from_panel(self, device_name: str) -> None:
        panel = self.device_panels.get(device_name)
        if panel is None:
            return
        panel["helper"].setText(f"Reading {device_name}...")

        self._start_background_task(
            lambda: read_serial_cable_values(
                device_name,
                topology=self.context.topology,
            ),
            on_success=lambda _result, panel=panel: panel["helper"].setText(
                "RTS can be changed here; CTS is monitored live."
            ),
            on_error=lambda exc, panel=panel: panel["helper"].setText(str(exc)),
        )

    def _trigger_experiment_action(self, action: str) -> None:
        _runner_name, runner = self._primary_runner()
        if runner is None:
            return

        method = getattr(runner, action, None)
        if method is None:
            return

        try:
            method()
        except Exception as exc:
            self.experiment_error_value.setText(str(exc))
        finally:
            self.refresh_snapshot()

    def _trigger_logging_action(self, action: str) -> None:
        target_name = self._selected_logging_target_name()
        if target_name is None:
            return

        def action_fn() -> None:
            if action == "start":
                start_data_logging(hotplate=target_name, topology=self.context.topology)
            elif action == "pause":
                pause_data_logging(hotplate=target_name, topology=self.context.topology)
            elif action == "resume":
                resume_data_logging(hotplate=target_name, topology=self.context.topology)
            elif action == "stop":
                stop_data_logging(hotplate=target_name, topology=self.context.topology)

        self._start_background_task(
            action_fn,
            on_error=lambda exc: self.logging_state_value.setText(str(exc)),
            reset_abort=action == "start",
        )

    def _add_logging_comment(self) -> None:
        target_name = self._selected_logging_target_name()
        if target_name is None:
            return

        comment = self.logging_comment_input.text().strip()
        if not comment:
            return

        self._start_background_task(
            lambda: add_log_comment(comment, hotplate=target_name, topology=self.context.topology),
            on_success=lambda _result: self.logging_comment_input.clear(),
            on_error=lambda exc: self.logging_state_value.setText(str(exc)),
        )

    def _run_move_liquid_operation(self) -> None:
        peristaltic_kwargs = _operation_peristaltic_kwargs(
            self.move_peristaltic_rpm_input,
            self.move_peristaltic_flow_input,
        )
        try:
            preview = move_liquid(
                self.move_source_input.currentText(),
                self.move_destination_input.currentText(),
                self.move_volume_input.value(),
                topology=self.context.topology,
                execute=False,
                update_volumes=False,
                **peristaltic_kwargs,
            )
            self.move_status_label.setText(f"Running: {_format_plan_status(preview)}")
        except Exception as exc:
            self.move_status_label.setText(str(exc))
            return

        self._start_background_task(
            lambda: move_liquid(
                self.move_source_input.currentText(),
                self.move_destination_input.currentText(),
                self.move_volume_input.value(),
                topology=self.context.topology,
                execute=True,
                abort_event=self.context.run_control.abort_event,
                **peristaltic_kwargs,
            ),
            on_success=lambda result: self.move_status_label.setText(f"Done: {_format_plan_status(result)}"),
            on_error=lambda exc: self.move_status_label.setText(str(exc)),
        )

    def _run_fill_reactor_operation(self) -> None:
        peristaltic_kwargs = _operation_peristaltic_kwargs(
            self.fill_peristaltic_rpm_input,
            self.fill_peristaltic_flow_input,
        )
        try:
            preview = fill_reactor(
                self.fill_source_input.currentText(),
                self.fill_volume_input.value(),
                topology=self.context.topology,
                execute=False,
                update_volumes=False,
                **peristaltic_kwargs,
            )
            self.reactor_status_label.setText(f"Running: {_format_plan_status(preview)}")
        except Exception as exc:
            self.reactor_status_label.setText(str(exc))
            return

        self._start_background_task(
            lambda: fill_reactor(
                self.fill_source_input.currentText(),
                self.fill_volume_input.value(),
                topology=self.context.topology,
                execute=True,
                abort_event=self.context.run_control.abort_event,
                **peristaltic_kwargs,
            ),
            on_success=lambda result: self.reactor_status_label.setText(f"Done: {_format_plan_status(result)}"),
            on_error=lambda exc: self.reactor_status_label.setText(str(exc)),
        )

    def _run_empty_reactor_operation(self) -> None:
        requested_volume = self.empty_volume_input.value()
        kwargs: dict[str, Any] = {
            "destination": self.empty_destination_input.currentText(),
            "topology": self.context.topology,
            "execute": True,
            "abort_event": self.context.run_control.abort_event,
        }
        kwargs.update(
            _operation_peristaltic_kwargs(
                self.empty_peristaltic_rpm_input,
                self.empty_peristaltic_flow_input,
            )
        )
        if requested_volume > 0:
            kwargs["ml"] = requested_volume

        preview_kwargs = dict(kwargs)
        preview_kwargs["execute"] = False
        preview_kwargs["update_volumes"] = False
        try:
            preview = empty_reactor(**preview_kwargs)
            self.reactor_status_label.setText(f"Running: {_format_plan_status(preview)}")
        except Exception as exc:
            self.reactor_status_label.setText(str(exc))
            return

        self._start_background_task(
            lambda: empty_reactor(**kwargs),
            on_success=lambda result: self.reactor_status_label.setText(f"Done: {_format_plan_status(result)}"),
            on_error=lambda exc: self.reactor_status_label.setText(str(exc)),
        )

    def _run_cleaning_operation(self) -> None:
        cycles = self.clean_cycles_input.value()
        fill_volume = self.clean_volume_input.value()
        wait_s = self.clean_wait_input.value()
        stir_rpm = self.clean_stir_rpm_input.value()
        peristaltic_kwargs = _operation_peristaltic_kwargs(
            self.clean_peristaltic_rpm_input,
            self.clean_peristaltic_flow_input,
        )
        stir_suffix = f", stir {stir_rpm} rpm" if stir_rpm > 0 else ""
        self.reactor_status_label.setText(
            f"Starting cleaning: {cycles} cycle(s), { _display_text(fill_volume) } mL, wait { _display_text(wait_s) } s"
            + stir_suffix
            + _format_peristaltic_settings_suffix(peristaltic_kwargs)
        )
        cleaning_kwargs: dict[str, Any] = {}
        if stir_rpm > 0:
            cleaning_kwargs["stir_rpm"] = stir_rpm
        self._start_background_task(
            lambda: cleaning(
                topology=self.context.topology,
                cycles=cycles,
                fill_volume_ml=fill_volume,
                wait_seconds=wait_s,
                execute=True,
                abort_event=self.context.run_control.abort_event,
                **cleaning_kwargs,
                **peristaltic_kwargs,
            ),
            on_success=lambda result: self.reactor_status_label.setText(
                f"Cleaning finished with {len(result)} planned moves"
            ),
            on_error=lambda exc: self.reactor_status_label.setText(str(exc)),
        )

    def _make_tree(self) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setColumnCount(2)
        tree.setHeaderLabels(["Field", "Value"])
        tree.setAlternatingRowColors(True)
        tree.header().setStretchLastSection(True)
        return tree

    def _populate_tree(
        self,
        tree: QTreeWidget,
        mapping: dict[str, Any],
        *,
        selected_name: str | None = None,
    ) -> None:
        tree.clear()
        selected_item: QTreeWidgetItem | None = None
        for name, payload in mapping.items():
            root = QTreeWidgetItem([str(name), ""])
            root.setData(0, Qt.UserRole, str(name))
            tree.addTopLevelItem(root)
            self._add_payload_items(root, payload)
            if selected_name is not None and str(name) == selected_name:
                selected_item = root
        tree.expandToDepth(1)
        if selected_item is not None:
            tree.setCurrentItem(selected_item)

    def _add_payload_items(self, parent: QTreeWidgetItem, payload: Any, *, key_name: str | None = None) -> None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                child = QTreeWidgetItem(
                    [str(key), "" if isinstance(value, (dict, list)) else _display_text(value)]
                )
                parent.addChild(child)
                if isinstance(value, (dict, list)):
                    self._add_payload_items(child, value, key_name=str(key))
            return

        if isinstance(payload, list):
            for index, value in enumerate(payload):
                label = f"[{index}]"
                child = QTreeWidgetItem(
                    [label, "" if isinstance(value, (dict, list)) else _display_text(value)]
                )
                parent.addChild(child)
                if isinstance(value, (dict, list)):
                    self._add_payload_items(child, value, key_name=label)
            return

        if key_name is not None:
            parent.setText(1, _display_text(payload))

    def _wrap_widget(self, title: str, widget: QWidget) -> QGroupBox:
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        layout.addWidget(widget)
        return box

    def _replace_overview_cards(
        self,
        layout: QVBoxLayout,
        cards: list[QWidget],
        *,
        empty_text: str,
    ) -> None:
        self._clear_layout(layout)
        if not cards:
            label = QLabel(empty_text)
            label.setAlignment(Qt.AlignCenter)
            layout.addWidget(label)
            return
        for card in cards:
            layout.addWidget(card)

    def _clear_layout(self, layout: QVBoxLayout | QHBoxLayout | QGridLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            child_layout = item.layout()
            widget = item.widget()
            if child_layout is not None:
                self._clear_layout(child_layout)
            if widget is not None:
                widget.deleteLater()

    def _overview_card(
        self,
        title: str,
        buttons: list[QPushButton],
        *,
        detail: str | None = None,
    ) -> QGroupBox:
        card = QGroupBox(title)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        button_row = QHBoxLayout()
        button_row.setSpacing(6)
        for button in buttons:
            button_row.addWidget(button)
        button_row.addStretch(1)
        layout.addLayout(button_row)
        if detail is not None:
            detail_label = QLabel(detail)
            detail_label.setWordWrap(True)
            detail_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(detail_label)
        return card

    def _overview_status_button(self, text: str, state: str = "neutral") -> QPushButton:
        button = QPushButton(text)
        button.setEnabled(False)
        button.setMinimumHeight(28)
        colors = {
            "active": ("#3fa34d", "white"),
            "inactive": ("#c94c4c", "white"),
            "neutral": ("#e7e7e7", "#222222"),
        }
        background, foreground = colors.get(state, colors["neutral"])
        button.setStyleSheet(
            "QPushButton:disabled {"
            f"background-color: {background}; color: {foreground};"
            "border: 1px solid #b8b8b8; border-radius: 4px; padding: 4px 8px;"
            "}"
        )
        return button

    def _overview_device_buttons(self, snapshot: dict[str, Any]) -> list[QPushButton]:
        kind = str(snapshot.get("kind", ""))
        state = snapshot.get("state", {})
        metadata = snapshot.get("metadata", {})
        values = dict(metadata) if isinstance(metadata, dict) else {}
        if isinstance(state, dict):
            values.update(state)

        if kind == "reactor":
            return [self._overview_volume_button(snapshot)]
        if kind in {"stirrer", "magnetic_stirrer", "hotplate"}:
            rpm = values.get("stir_rate", values.get("target_stir_rate", 0))
            temp = values.get("probe_temperature", values.get("target_temperature", 0))
            stirring = bool(values.get("stirring", False))
            heating = bool(values.get("heating", False))
            return [
                self._overview_status_button(f"{_display_text(rpm)} rpm", "active" if stirring else "inactive"),
                self._overview_status_button(f"{_display_text(temp)} C", "active" if heating else "inactive"),
            ]
        if kind == "peristaltic_pump":
            direction = values.get("direction") or "-"
            rpm = values.get("current_rpm", values.get("target_rpm", 0))
            running = bool(values.get("running", False))
            return [
                self._overview_status_button(
                    f"{direction}: {_display_text(rpm)} rpm",
                    "active" if running else "inactive",
                )
            ]
        if kind == "syringe_pump":
            direction = values.get("direction") or "-"
            volume = values.get("current_volume_ml", values.get("target_volume_ml", 0))
            running = bool(values.get("running", False))
            return [
                self._overview_status_button(
                    f"{direction}: {_display_text(volume)} mL",
                    "active" if running else "inactive",
                )
            ]
        if kind == "valve":
            port = values.get("current_port")
            port_name = values.get("current_port_name")
            port_text = "port -" if port is None else f"port {port}"
            if port_name:
                port_text += f" ({port_name})"
            return [self._overview_status_button(port_text)]
        if kind == "serial_cable":
            rts = bool(values.get("rts", False))
            cts = bool(values.get("cts", False))
            return [
                self._overview_status_button(f"RTS: {_on_off(rts)}", "active" if rts else "inactive"),
                self._overview_status_button(f"CTS: {_on_off(cts)}", "active" if cts else "inactive"),
            ]
        return [self._overview_status_button(_format_state_summary(state if isinstance(state, dict) else {}))]

    def _overview_volume_button(self, snapshot: dict[str, Any]) -> QPushButton:
        state = snapshot.get("state", {})
        metadata = snapshot.get("metadata", {})
        values = dict(metadata) if isinstance(metadata, dict) else {}
        if isinstance(state, dict):
            values.update(state)
        volume = values.get("volume_ml")
        max_volume = values.get("max_volume_ml")
        if volume is None and max_volume is None:
            return self._overview_status_button("-")
        if max_volume is None:
            text = f"{_display_text(volume)} mL"
        else:
            text = f"{_display_text(volume or 0)}/{_display_text(max_volume)} mL"
        return self._overview_status_button(text)

    def _overview_logger_button(self, snapshot: dict[str, Any]) -> QPushButton:
        active = bool(snapshot.get("active", False))
        paused = bool(snapshot.get("paused", False))
        if active and paused:
            text = "paused"
            state = "inactive"
        elif active:
            text = "active"
            state = "active"
        else:
            text = "idle"
            state = "inactive"
        return self._overview_status_button(text, state)

    def _overview_experiment_button(self, snapshot: dict[str, Any]) -> QPushButton:
        status = str(snapshot.get("status", "idle"))
        state = "active" if status == "running" else "inactive"
        if status == "paused":
            state = "neutral"
        return self._overview_status_button(status, state)

    def _set_combo_items(self, combo: QComboBox, values: list[str]) -> None:
        current = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(values)
        if current and current in values:
            combo.setCurrentText(current)
        combo.blockSignals(False)

    def _finish_valve_action(self, panel: dict[str, Any]) -> None:
        panel["dirty"] = False
        panel["helper"].setText("Direct valve control is available here.")

    def _start_background_task(
        self,
        action: Any,
        *,
        on_success: Any | None = None,
        on_error: Any | None = None,
        reset_abort: bool = True,
    ) -> None:
        if reset_abort:
            self.context.run_control.reset_abort()
        self._busy_actions += 1
        self._apply_action_button_lock()

        def worker() -> None:
            try:
                result = action()
            except Exception as exc:
                if on_error is not None:
                    self._ui_queue.put(lambda exc=exc: on_error(exc))
                self._ui_queue.put(self._finish_background_task)
                self._ui_queue.put(self.refresh_snapshot)
                return

            if on_success is not None:
                self._ui_queue.put(lambda result=result: on_success(result))
            self._ui_queue.put(self._finish_background_task)
            self._ui_queue.put(self.refresh_snapshot)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_background_task(self) -> None:
        self._busy_actions = max(0, self._busy_actions - 1)
        self._apply_action_button_lock()

    def _advance_emergency_stop(self, step: int) -> None:
        if step != self._emergency_step + 1:
            self._reset_emergency_stop_sequence()
            return

        self._emergency_step = step
        self._apply_emergency_stop_sequence()
        if self._emergency_step == 3:
            self._trigger_emergency_stop()
        else:
            self._emergency_sequence_token += 1
            sequence_token = self._emergency_sequence_token
            QTimer.singleShot(
                5000,
                lambda: self._reset_emergency_stop_sequence(sequence_token),
            )

    def _reset_emergency_stop_sequence(self, sequence_token: int | None = None) -> None:
        if sequence_token is not None and sequence_token != self._emergency_sequence_token:
            return
        self._emergency_sequence_token += 1
        self._emergency_step = 0
        self._apply_emergency_stop_sequence()

    def _apply_emergency_stop_sequence(self) -> None:
        labels = ["STOP 1", "STOP 2", "STOP 3"]
        for index, button in enumerate(self.emergency_buttons):
            step = index + 1
            if step <= self._emergency_step:
                button.setText(f"{labels[index]} OK")
                button.setStyleSheet(
                    "background-color: #8a2f2f; color: white; font-weight: 700;"
                )
                button.setEnabled(False)
            elif step == self._emergency_step + 1:
                button.setText(labels[index])
                button.setStyleSheet(
                    "background-color: #c94c4c; color: white; font-weight: 700;"
                )
                button.setEnabled(True)
            else:
                button.setText(labels[index])
                button.setStyleSheet(
                    "background-color: #d8d8d8; color: #666666; font-weight: 700;"
                )
                button.setEnabled(False)

    def _trigger_emergency_stop(self) -> None:
        self._reset_emergency_stop_sequence()
        self.context.run_control.request_abort()
        self.move_status_label.setText("Emergency stop requested")
        self.reactor_status_label.setText("Emergency stop requested")
        self.logging_state_value.setText("Emergency stop requested")
        self.experiment_error_value.setText("Emergency stop requested")
        self._start_background_task(
            lambda: emergency_stop(
                topology=self.context.topology,
                experiment_runners=self.context.experiment_runners,
                run_control=self.context.run_control,
            ),
            on_success=lambda result: self._finish_emergency_stop(result),
            on_error=lambda exc: self.experiment_error_value.setText(str(exc)),
            reset_abort=False,
        )

    def _finish_emergency_stop(self, result: dict[str, Any]) -> None:
        devices = ", ".join(result.get("stopped_devices", [])) or "no devices"
        self.move_status_label.setText(f"Emergency stop sent to {devices}")
        self.reactor_status_label.setText(f"Emergency stop sent to {devices}")
        self.logging_state_value.setText("Stopped")
        self.experiment_error_value.setText("Emergency stop completed")

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                callback = self._ui_queue.get_nowait()
            except Empty:
                break
            callback()

def build_demo_context(*, expert_mode: bool = False) -> GuiContext:
    topology = exp_topology.example_topology()
    return GuiContext(
        topology=topology,
        expert_mode=expert_mode,
    )

def launch_demo() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = ChemistryMainWindow(build_demo_context())
    window.show()
    return app.exec()

def _operation_peristaltic_kwargs(
    rpm_input: QSpinBox,
    flow_input: QDoubleSpinBox,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if rpm_input.value() > 0:
        kwargs["peristaltic_speed"] = rpm_input.value()
    if flow_input.value() > 0:
        kwargs["peristaltic_rate_ml_min"] = flow_input.value()
    return kwargs

def _inline_field(label_text: str, widget: QWidget) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    label = QLabel(label_text)
    layout.addWidget(label)
    layout.addWidget(widget)
    return container

def _stacked_field(label_text: str, widget: QWidget) -> QWidget:
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    label = QLabel(label_text)
    layout.addWidget(label)
    layout.addWidget(widget)
    return container

def _format_plan_status(plan: Any) -> str:
    base = plan.as_path() if hasattr(plan, "as_path") else str(plan)
    if not hasattr(plan, "actions"):
        return base

    for action in getattr(plan, "actions", []):
        if action.get("type") != "run_pump" or action.get("kind") != "peristaltic_pump":
            continue
        rpm = action.get("speed")
        rate = action.get("rate_ml_min")
        direction = action.get("direction")
        pump = action.get("pump")
        details: list[str] = []
        if pump:
            details.append(str(pump))
        if direction:
            details.append(str(direction))
        if rpm is not None:
            details.append(f"{rpm} rpm")
        if rate is not None:
            details.append(f"{_display_text(rate)} mL/min")
        if details:
            return f"{base} | {' '.join(details)}"
    return base

def _format_peristaltic_settings_suffix(kwargs: dict[str, Any]) -> str:
    details: list[str] = []
    if "peristaltic_speed" in kwargs:
        details.append(f"{kwargs['peristaltic_speed']} rpm")
    if "peristaltic_rate_ml_min" in kwargs:
        details.append(f"{_display_text(kwargs['peristaltic_rate_ml_min'])} mL/min")
    if not details:
        return ""
    return " | " + ", ".join(details)

def _format_current_step_detail(step: Any) -> str:
    if not isinstance(step, dict):
        return "-"
    label = step.get("label")
    action = step.get("action", "-")
    index = step.get("index")
    kwargs = step.get("kwargs", {})
    title = str(label or action)
    if index is not None:
        title = f"{index}: {title}"
    if not kwargs:
        return title
    if isinstance(kwargs, dict):
        detail = ", ".join(
            f"{key}={_display_text(value)}" for key, value in kwargs.items()
        )
        return f"{title}\n{detail}"
    return f"{title}\n{_display_text(kwargs)}"

def _logging_actions_for_state(*, active: bool, paused: bool) -> list[str]:
    if not active:
        return ["start"]
    if paused:
        return ["resume", "stop"]
    return ["pause", "stop"]

def _format_state_summary(state: dict[str, Any]) -> str:
    if not state:
        return "-"
    summary_parts: list[str] = []
    for key, value in state.items():
        if value in (None, False, 0, 0.0, "", []):
            continue
        summary_parts.append(f"{key}={_display_text(value)}")
    return ", ".join(summary_parts) if summary_parts else "idle"

def _format_float(value: float) -> str:
    text = f"{float(value):.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text

def _on_off(value: Any) -> str:
    return "ON" if bool(value) else "OFF"

def _display_text(value: Any) -> str:
    if isinstance(value, float):
        return _format_float(value)
    return str(value)

def _display_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _display_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_display_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_display_payload(item) for item in value]
    if isinstance(value, float):
        return _format_float(value)
    return value

def _merged_snapshot_values(snapshot: dict[str, Any]) -> dict[str, Any]:
    metadata = snapshot.get("metadata", {})
    state = snapshot.get("state", {})
    values = dict(metadata) if isinstance(metadata, dict) else {}
    if isinstance(state, dict):
        values.update(state)
    return values

def _pump_spinner_frame(is_running: bool, direction: Any, tick: int) -> str:
    if not is_running:
        return "-"
    cw_frames = ["|", "/", "-", "\\"]
    ccw_frames = list(reversed(cw_frames))
    frames = cw_frames if str(direction).upper() == "CW" else ccw_frames
    return frames[tick % len(frames)]

def _syringe_motion_frame(is_running: bool, direction: Any, tick: int) -> str:
    if not is_running:
        return "-"
    if str(direction).upper() == "WDR":
        frames = ["<", "<<", "<<<", "<<"]
    else:
        frames = [">", ">>", ">>>", ">>"]
    return frames[tick % len(frames)]


if __name__ == "__main__":
    raise SystemExit(launch_demo())

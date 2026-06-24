"""
Tools for describing the physical topology of a chemistry setup.

The topology intentionally stays separate from hardware initialization. A setup
can be described with plain dictionaries first, then the command layer can use
the returned route to decide which valve position, pump, or reactor to operate.
"""

import json
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable


def format_nested_dict(d: dict, prefix: str = "") -> str:
    """helper function to print items of nested dictionary topologies"""
    lines = []

    items = list(d.items())

    for index, (key, value) in enumerate(items):
        is_last = index == len(items) - 1

        connector = "└── " if is_last else "├── "
        child_prefix = "    " if is_last else "│   "

        if isinstance(value, dict):
            lines.append(f"{prefix}{connector}{key}")
            lines.append(format_nested_dict(value, prefix + child_prefix))
        elif isinstance(value, float):
            lines.append(f"{prefix}{connector}{key}: {value:.2f}")
        else:
            lines.append(f"{prefix}{connector}{key}: {value}")

    return "\n".join(lines)

@dataclass(frozen=True)
class Node:
    """A named thing in the setup graph."""

    name: str
    kind: str
    handle: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def is_device(self) -> bool:
        return self.kind not in {"source", "sink", "liquid", "container", "endpoint"}

@dataclass(frozen=True)
class Connection:
    """A physical connection (tubing) between two named nodes."""

    a: str
    b: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def other(self, node: str) -> str:
        if self.a == node:
            return self.b
        if self.b == node:
            return self.a
        raise TopologyError(f"Connection does not include node {node!r}")

@dataclass(frozen=True)
class Route:
    """A resolved path through the setup. Think: Flask, tube, valve, pump, reactor..."""

    source: str
    destination: str
    nodes: tuple[str, ...]
    connections: tuple[Connection, ...]

    def valve_positions(self) -> dict[str, int]:
        positions: dict[str, int] = {}
        for step in self.valve_steps():
            positions[step["valve"]] = step["port"]
        return positions

    def valve_steps(self) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        for connection in self.connections:
            for valve_name, port in self._connection_valve_ports(connection).items():
                steps.append(
                    {
                        "valve": str(valve_name),
                        "port": int(port),
                        "node": connection.other(str(valve_name)),
                    }
                )
        return steps

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "destination": self.destination,
            "nodes": list(self.nodes),
            "valve_steps": self.valve_steps(),
            "valve_positions": self.valve_positions(),
        }
    
    def as_path(self) -> str:
        path_string = " -> ".join(self.nodes)
        return path_string

    @staticmethod
    def _connection_valve_ports(connection: Connection) -> dict[str, int]:
        valve_ports = connection.metadata.get("valve_ports")
        if valve_ports is not None:
            return {str(valve): int(port) for valve, port in valve_ports.items()}

        valve_name = connection.metadata.get("valve")
        port = connection.metadata.get("port")
        if valve_name is not None and port is not None:
            return {str(valve_name): int(port)}

        return {}

class TopologyError(ValueError):
    """Raised when a topology cannot be assembled or queried."""

class SetupTopology:
    """Graph of devices, flasks, liquids, and ports in an experiment setup."""

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self._connections: list[Connection] = []
        self._neighbors: dict[str, list[tuple[str, Connection]]] = {}
        self._active_transfers: dict[int, dict[str, Any]] = {}
        self._active_transfer_counter = 0
        self._active_transfer_lock = threading.Lock()

    def add_device(
        self,
        name: str,
        *,
        kind: str = "device",
        handle: Any = None,
        state: dict[str, Any] | None = None,
        **metadata: Any,
    ) -> None:
        """create a device-node in the graph"""
        self._add_node(
            Node(
                name=name,
                kind=kind,
                handle=handle,
                metadata=metadata,
                state=_initial_node_state(kind, metadata, state),
            )
        )

    def add_endpoint(
        self,
        name: str,
        *,
        kind: str = "endpoint",
        contents: str | None = None,
        volume_ml: float | None = None,
        max_volume_ml: float | None = None,
        state: dict[str, Any] | None = None,
        **metadata: Any,
    ) -> None:
        """create a flask-node in the graph; e.g. flask, reactor, waste bucket..."""
        endpoint_metadata = dict(metadata)
        if contents is not None:
            endpoint_metadata["contents"] = contents
        if volume_ml is not None:
            endpoint_metadata["volume_ml"] = volume_ml
        if max_volume_ml is not None:
            endpoint_metadata["max_volume_ml"] = max_volume_ml
        self._add_node(
            Node(
                name=name,
                kind=kind,
                metadata=endpoint_metadata,
                state=_initial_node_state(kind, endpoint_metadata, state),
            )
        )

    def connect(self, a: str, b: str, **metadata: Any) -> None:
        """build 'digital' connection between nodes"""
        if a not in self.nodes:
            raise TopologyError(f"Unknown topology node: {a!r}")
        if b not in self.nodes:
            raise TopologyError(f"Unknown topology node: {b!r}")

        metadata = self._add_default_valve_role(a, b, dict(metadata))

        connection = Connection(a=a, b=b, metadata=metadata)
        self._connections.append(connection)
        self._neighbors.setdefault(a, []).append((b, connection))
        self._neighbors.setdefault(b, []).append((a, connection))

    def find_path(self, source: str, destination: str) -> Route:
        """Return the shortest physical route from source to destination."""

        if source not in self.nodes:
            raise TopologyError(f"Source {source!r} cannot be found")
        if destination not in self.nodes:
            raise TopologyError(f"Destination {destination!r} cannot be found")
        if source == destination:
            return Route(source, destination, (source,), ())

        queue: deque[tuple[str, list[str], list[Connection], Connection | None]] = deque(
            [(source, [source], [], None)]
        )
        visited = {(source, None)}
        while queue:
            current, path, connections, previous_connection = queue.popleft()
            for neighbor, connection in self._neighbors.get(current, []):
                if neighbor in path and not self._can_revisit_node(
                    current, neighbor, previous_connection, connection
                ):
                    continue
                if self._is_blocked_valve_transition(
                    current, previous_connection, connection
                ):
                    continue

                state = (neighbor, id(connection))
                if state in visited:
                    continue

                next_path = [*path, neighbor]
                next_connections = [*connections, connection]
                if neighbor == destination:
                    return Route(
                        source=source,
                        destination=destination,
                        nodes=tuple(next_path),
                        connections=tuple(next_connections),
                    )

                visited.add(state)
                queue.append((neighbor, next_path, next_connections, connection))

        raise TopologyError(f"No path found from {source!r} to {destination!r}")

    def validate(self) -> None:
        """check topology for missing connections and other errors"""
        if not self.nodes:
            raise TopologyError("Topology has no nodes")
        disconnected = [
            name
            for name, node in self.nodes.items()
            if (
                name not in self._neighbors
                and len(self.nodes) > 1
                and node.kind not in _STANDALONE_DEVICE_KINDS
            )
        ]
        if disconnected:
            raise TopologyError(f"Disconnected topology nodes: {', '.join(disconnected)}")

    def values(
        self,
        node_names: Iterable[str] | None = None,
        *,
        include_handles: bool = False,
        include_state: bool = True,
        strict: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """returns metadata of given nodes (devices/flasks)"""
        selected_names = list(self.nodes) if node_names is None else list(node_names)
        result: dict[str, dict[str, Any]] = {}

        for name in selected_names:
            if name not in self.nodes:
                if strict:
                    raise TopologyError(f"Unknown topology node: {name!r}")
                result[name] = {"error": "unknown node"}
                continue

            node = self.nodes[name]
            values = {"kind": node.kind, **node.metadata}
            if include_state:
                values["state"] = dict(node.state)
            if include_handles:
                values["handle"] = node.handle
            result[name] = values

        return result
    
    def print_values(
        self,
        node_names: Iterable[str] | None = None,
        *,
        include_handles: bool = False,
        include_state: bool = True,
        strict: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """prints metadata acquired from 'values' function and
        also returns the same data"""
        values = self.values(
            node_names,
            include_handles=include_handles,
            include_state=include_state,
            strict=strict,
        )
        print(format_nested_dict(values))
        return values

    def state_values(
        self,
        node_names: Iterable[str] | None = None,
        *,
        strict: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """finds metadata of given nodes and returns
        a dictionary of nodes and their values"""
        selected_names = list(self.nodes) if node_names is None else list(node_names)
        result: dict[str, dict[str, Any]] = {}
        for name in selected_names:
            if name not in self.nodes:
                if strict:
                    raise TopologyError(f"Unknown topology node: {name!r}")
                result[name] = {"error": "unknown node"}
                continue
            result[name] = dict(self.nodes[name].state)
        return result

    def node_snapshot(self, name: str, *, strict: bool = False) -> dict[str, Any]:
        """collects current metadata of a given node"""
        if name not in self.nodes:
            if strict:
                raise TopologyError(f"Unknown topology node: {name!r}")
            return {"error": "unknown node"}

        node = self.nodes[name]
        metadata = dict(node.metadata)
        logger = metadata.pop("data_logger", None)
        if logger is not None:
            metadata["data_logger_name"] = getattr(logger, "name", f"{name}_logger")
        return {
            "name": node.name,
            "kind": node.kind,
            "is_device": node.is_device,
            "has_handle": node.handle is not None,
            "metadata": _json_safe(metadata),
            "state": _json_safe(dict(node.state)),
            "capabilities": _node_capabilities(node),
            "manual_actions": _node_manual_actions(node),
            "interactive_actions": _node_interactive_actions(node),
        }

    def update_node_state(
        self,
        name: str,
        values: dict[str, Any] | None = None,
        *,
        mirror_to_metadata: bool = False,
        **extra_values: Any,
    ) -> dict[str, Any]:
        if name not in self.nodes:
            raise TopologyError(f"Unknown topology node: {name!r}")
        payload = {}
        if values is not None:
            payload.update(values)
        payload.update(extra_values)
        node = self.nodes[name]
        node.state.update(payload)
        if mirror_to_metadata or not node.is_device:
            node.metadata.update(payload)
        return dict(node.state)

    def gui_snapshot(
        self,
        *,
        experiment_runners: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """collects all currently available data displayed in the GUI"""
        devices: dict[str, dict[str, Any]] = {}
        endpoints: dict[str, dict[str, Any]] = {}
        loggers: dict[str, dict[str, Any]] = {}

        for name, node in self.nodes.items():
            snapshot = self.node_snapshot(name, strict=True)
            if node.is_device:
                devices[name] = snapshot
            else:
                endpoints[name] = snapshot

            logger = node.metadata.get("data_logger")
            if logger is not None:
                if hasattr(logger, "snapshot"):
                    logger_snapshot = dict(logger.snapshot())
                    logger_snapshot.pop("latest_values", None)
                else:
                    logger_snapshot = {
                        "logger_name": getattr(logger, "name", f"{name}_logger"),
                        "active": bool(node.state.get("logging_active", False)),
                        "paused": bool(node.state.get("logging_paused", False)),
                        "log_file": node.state.get("log_file"),
                        "log_interval_s": node.state.get("log_interval_s"),
                        "hardware_refresh_s": node.state.get("hardware_refresh_s"),
                    }
                logger_snapshot["attached_to"] = name
                logger_snapshot["kind"] = "data_logger"
                logger_snapshot["capabilities"] = [
                    "start",
                    "pause",
                    "resume",
                    "stop",
                    "add_comment",
                ]
                logger_snapshot["manual_actions"] = []
                logger_snapshot["interactive_actions"] = [
                    "start",
                    "pause",
                    "resume",
                    "stop",
                    "add_comment",
                ]
                loggers[name] = _json_safe(logger_snapshot)

        return {
            "devices": devices,
            "endpoints": endpoints,
            "loggers": loggers,
            "topology": self.graph_snapshot(),
            "operations": _global_interactive_actions(self),
            "experiments": _experiment_snapshots(experiment_runners),
        }

    def gui_snapshot_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.gui_snapshot(), indent=indent)

    def graph_snapshot(self) -> dict[str, Any]:
        """Return the physical graph and any transfers currently in progress."""

        with self._active_transfer_lock:
            active_transfers = [
                dict(transfer) for transfer in self._active_transfers.values()
            ]
        return {
            "nodes": [
                {
                    "name": node.name,
                    "kind": node.kind,
                    "is_device": node.is_device,
                }
                for node in self.nodes.values()
            ],
            "connections": [
                {
                    "a": connection.a,
                    "b": connection.b,
                    "metadata": _json_safe(connection.metadata),
                }
                for connection in self._connections
            ],
            "active_transfers": _json_safe(active_transfers),
        }

    def begin_liquid_transfer(self, route: Route, volume_ml: float) -> int:
        """Publish an executing liquid route for live user interfaces."""

        with self._active_transfer_lock:
            self._active_transfer_counter += 1
            transfer_id = self._active_transfer_counter
            self._active_transfers[transfer_id] = {
                "id": transfer_id,
                "source": route.source,
                "destination": route.destination,
                "volume_ml": float(volume_ml),
                "nodes": list(route.nodes),
            }
        return transfer_id

    def end_liquid_transfer(self, transfer_id: int) -> None:
        with self._active_transfer_lock:
            self._active_transfers.pop(transfer_id, None)

    def _add_node(self, node: Node) -> None:
        if node.name in self.nodes:
            raise TopologyError(f"Duplicate topology node: {node.name!r}")
        self.nodes[node.name] = node

    def _add_default_valve_role(
        self, a: str, b: str, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        valves = [
            name
            for name in (a, b)
            if self.nodes[name].kind == "valve"
        ]
        if metadata.get("valve_roles") is not None:
            return metadata
        if metadata.get("valve_port_role") is not None:
            valve_name = str(metadata.get("valve", valves[0] if valves else ""))
            if valve_name:
                metadata.setdefault("valve", valve_name)
                metadata["valve_roles"] = {valve_name: metadata["valve_port_role"]}
            return metadata

        if not valves:
            return metadata

        valve_name = str(metadata.get("valve", valves[0]))
        metadata.setdefault("valve", valve_name)
        metadata["valve_port_role"] = "leaf" if "port" in metadata else "common"
        metadata["valve_roles"] = {valve_name: metadata["valve_port_role"]}
        return metadata

    def _is_blocked_valve_transition(
        self,
        current: str,
        previous_connection: Connection | None,
        next_connection: Connection,
    ) -> bool:
        if self.nodes[current].kind != "valve" or previous_connection is None:
            return False

        previous_role = self._valve_role(previous_connection, current)
        next_role = self._valve_role(next_connection, current)

        return previous_role == "leaf" and next_role == "leaf"

    def _can_revisit_node(
        self,
        current: str,
        neighbor: str,
        previous_connection: Connection | None,
        next_connection: Connection,
    ) -> bool:
        if previous_connection is None:
            return False
        if self.nodes[current].kind != "syringe_pump":
            return False
        if self.nodes[neighbor].kind != "valve":
            return False
        if previous_connection != next_connection:
            return False

        return self._valve_role(next_connection, neighbor) == "common"

    @staticmethod
    def _valve_role(connection: Connection, valve_name: str) -> str | None:
        valve_roles = connection.metadata.get("valve_roles", {})
        if valve_name in valve_roles:
            return valve_roles[valve_name]
        if connection.metadata.get("valve") != valve_name:
            return None
        return connection.metadata.get("valve_port_role")


def build_topology(definition: dict[str, Any]) -> SetupTopology:
    """
    Build a topology from a human-editable dictionary.

    Expected keys:
        devices: mapping of device name to metadata
        endpoints: mapping of endpoint name to metadata
        connections: iterable of pairs, or dictionaries with "from"/"to" keys
    """

    topology = SetupTopology()

    for name, spec in definition.get("devices", {}).items():
        spec = _normalize_spec(spec)
        topology.add_device(
            name,
            kind=spec.pop("kind", "device"),
            handle=spec.pop("handle", None),
            state=spec.pop("state", None),
            **spec,
        )

    declared_endpoints = set(definition.get("endpoints", {}))
    for _valve_name, ports in definition.get("valve_ports", {}).items():
        for port_name, port_spec in ports.items():
            _port_number, target_name = _normalize_valve_port(port_name, port_spec)
            if target_name not in topology.nodes and target_name not in declared_endpoints:
                topology.add_endpoint(target_name)

    for name, spec in definition.get("endpoints", {}).items():
        spec = _normalize_spec(spec)
        topology.add_endpoint(
            name,
            kind=spec.pop("kind", "endpoint"),
            state=spec.pop("state", None),
            **spec,
        )

    for connection in definition.get("connections", []):
        a, b, metadata = _normalize_connection(connection)
        topology.connect(a, b, **metadata)

    for valve_name, ports in definition.get("valve_ports", {}).items():
        for port_name, port_spec in ports.items():
            port_number, target_name = _normalize_valve_port(port_name, port_spec)
            if target_name not in topology.nodes:
                topology.add_endpoint(target_name)

            target_role = (
                "common" if topology.nodes[target_name].kind == "valve" else None
            )
            valve_roles = {valve_name: "leaf"}
            if target_role is not None:
                valve_roles[target_name] = target_role

            topology.connect(
                valve_name,
                target_name,
                valve=valve_name,
                port=int(port_number),
                valve_ports={valve_name: int(port_number)},
                valve_port_role="leaf",
                valve_roles=valve_roles,
                port_name=port_name,
            )

    topology.validate()
    return topology

def _normalize_spec(spec: Any) -> dict[str, Any]:
    if spec is None:
        return {}
    if isinstance(spec, dict):
        return dict(spec)
    return {"handle": spec}

_STATE_METADATA_KEYS = {
    "contents",
    "volume_ml",
    "max_volume_ml",
    "volume_status",
    "target_temperature",
    "probe_temperature",
    "hotplate_sensor_temperature",
    "target_stir_rate",
    "stir_rate",
    "heating",
    "stirring",
    "viscosity_trend",
    "logging_active",
    "logging_paused",
    "log_interval_s",
    "hardware_refresh_s",
    "log_file",
}

_STANDALONE_DEVICE_KINDS = {
    "serial_cable",
}

def _initial_node_state(
    kind: str,
    metadata: dict[str, Any],
    state: dict[str, Any] | None,
) -> dict[str, Any]:
    initial_state = _default_node_state(kind, None)
    for key, value in metadata.items():
        if key in _STATE_METADATA_KEYS:
            initial_state[key] = value
    if state is not None:
        initial_state.update(dict(state))
    return initial_state

def _default_node_state(kind: str, state: dict[str, Any] | None) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    if kind == "valve":
        defaults = {
            "current_port": None,
            "current_port_name": None,
        }
    elif kind == "peristaltic_pump":
        defaults = {
            "running": False,
            "direction": None,
            "current_rpm": 0,
            "target_rpm": 0,
        }
    elif kind == "syringe_pump":
        defaults = {
            "running": False,
            "direction": None,
            "current_volume_ml": 0.0,
            "target_volume_ml": 0.0,
        }
    elif kind in {"stirrer", "magnetic_stirrer", "hotplate"}:
        defaults = {
            "heating": False,
            "stirring": False,
            "target_temperature": 0.0,
            "probe_temperature": None,
            "hotplate_sensor_temperature": None,
            "target_stir_rate": 0.0,
            "stir_rate": 0.0,
            "viscosity_trend": None,
            "logging_active": False,
            "logging_paused": False,
        }
    elif kind == "serial_cable":
        defaults = {
            "rts": False,
            "cts": False,
        }

    merged = dict(defaults)
    if state is not None:
        merged.update(dict(state))
    return merged

def _node_capabilities(node: Node) -> list[str]:
    capabilities: list[str] = []
    if node.kind == "valve":
        capabilities.extend(["read_state", "set_position"])
    elif node.kind == "peristaltic_pump":
        capabilities.extend(["read_state", "run", "stop", "circulate"])
    elif node.kind == "syringe_pump":
        capabilities.extend(["read_state", "withdraw", "infuse", "stop"])
    elif node.kind in {"stirrer", "magnetic_stirrer", "hotplate"}:
        capabilities.extend(
            [
                "read_state",
                "start_heating",
                "stop_heating",
                "start_stirring",
                "stop_stirring",
                "data_logging",
            ]
        )
    elif node.kind == "serial_cable":
        capabilities.extend(["read_state", "read_cts", "set_rts"])
    elif node.kind == "reactor":
        capabilities.extend(["read_state", "fill", "empty", "clean", "heat", "stir"])
    elif node.kind == "measurement_cell":
        capabilities.extend(["read_state"])
    elif node.is_device:
        capabilities.append("read_state")

    if node.metadata.get("data_logger") is not None and "data_logging" not in capabilities:
        capabilities.append("data_logging")

    return capabilities

def _node_manual_actions(node: Node) -> list[str]:
    if node.kind == "valve":
        return ["set_valve_position"]
    if node.kind == "peristaltic_pump":
        return ["run_peristaltic_pump", "stop_peristaltic_pump"]
    if node.kind == "syringe_pump":
        return ["run_syringe_pump", "stop_syringe_pump"]
    if node.kind in {"stirrer", "magnetic_stirrer", "hotplate"}:
        return [
            "start_heating",
            "stop_heating",
            "start_stirring",
            "stop_stirring",
            "read_hotplate_values",
        ]
    if node.kind == "serial_cable":
        return ["read_serial_cable_values", "set_serial_rts"]
    if node.kind == "reactor":
        return [
            "stir_reactor",
            "stop_reactor_stirring",
            "heat_reactor",
            "stop_reactor_heating",
        ]
    return []

def _node_interactive_actions(node: Node) -> list[str]:
    if node.kind == "reactor":
        return ["fill_reactor", "empty_reactor", "cleaning"]
    if node.kind == "measurement_cell":
        return []
    if node.kind in {"stirrer", "magnetic_stirrer", "hotplate"}:
        return ["start_data_logging", "pause_data_logging", "resume_data_logging", "stop_data_logging"]
    if node.kind == "peristaltic_pump":
        return ["start_circulation", "pause_circulation", "resume_circulation", "stop_circulation"]
    return []

def _global_interactive_actions(topology: SetupTopology) -> dict[str, Any]:
    transfer_nodes = _liquid_transfer_nodes(topology)
    return {
        "move_liquid": {
            "action": "move_liquid",
            "inputs": ["source", "destination", "ml"],
            "optional_inputs": ["peristaltic_speed", "peristaltic_rate_ml_min", "syringe_rate_ml_min"],
            "sources": transfer_nodes,
            "destinations": transfer_nodes,
        }
    }

def _liquid_transfer_nodes(topology: SetupTopology) -> list[str]:
    candidates: list[str] = []
    liquid_kinds = {
        "source",
        "sink",
        "container",
        "endpoint",
        "reactor",
        "measurement_cell",
    }
    for name, node in topology.nodes.items():
        if node.kind in liquid_kinds:
            candidates.append(name)
            continue
        if "volume_ml" in node.metadata or "max_volume_ml" in node.metadata:
            candidates.append(name)
    return candidates

def _experiment_snapshots(experiment_runners: dict[str, Any] | None) -> dict[str, Any]:
    if experiment_runners is None:
        return {}

    snapshots: dict[str, Any] = {}
    for name, runner in experiment_runners.items():
        if hasattr(runner, "snapshot"):
            runner_snapshot = runner.snapshot()
        else:
            runner_snapshot = {"error": f"Runner {name!r} has no snapshot()"}
        snapshots[str(name)] = _json_safe(runner_snapshot)
    return snapshots

def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "as_posix"):
        return str(value)
    return repr(value)

def _normalize_valve_port(port_name: str, spec: Any) -> tuple[int, str]:
    if isinstance(spec, dict):
        data = dict(spec)
        if "port" not in data:
            raise TopologyError(
                f"Valve port {port_name!r} needs a 'port' value"
            )
        target = data.get("connects_to", data.get("connected_to", port_name))
        return int(data["port"]), str(target)

    return int(spec), port_name

def _normalize_connection(connection: Any) -> tuple[str, str, dict[str, Any]]:
    if isinstance(connection, dict):
        data = dict(connection)
        try:
            a = data.pop("from")
            b = data.pop("to")
        except KeyError as exc:
            raise TopologyError(
                "Connection dictionaries need 'from' and 'to' keys"
            ) from exc
        return str(a), str(b), data

    if isinstance(connection, Iterable) and not isinstance(connection, (str, bytes)):
        values = list(connection)
        if len(values) == 2:
            return str(values[0]), str(values[1]), {}
        if len(values) >= 3 and all(isinstance(value, dict) for value in values[2:]):
            metadata: dict[str, Any] = {}
            for value in values[2:]:
                metadata.update(value)
            return str(values[0]), str(values[1]), metadata

    raise TopologyError(
        "Connections must be ('node_a', 'node_b') or "
        "('node_a', 'node_b', {'metadata': 'value'}) or "
        "{'from': 'node_a', 'to': 'node_b'}"
    )

EXAMPLE_SETUP = {
    "devices": {
        "pump1": {"kind": "syringe_pump"},
        "valveA": {"kind": "valve"},
        "valveB": {"kind": "valve"},
        "reactor": {"kind": "reactor", "volume_ml":10, "max_volume_ml": 500},
        "stirring_plate": {"kind": "stirrer"},
    },
    "endpoints": {
        "water": {"kind": "source", "contents": "water", "volume_ml":999.9999},
        "waste": {"kind": "container", "volume_ml": 0, "max_volume_ml": 1000},
        # "wasteB": {"kind": "container", "volume_ml": 0, "max_volume_ml": 1000},
        "solutionA": {"kind": "source", "contents": "reaction solutionA"},
        "solutionB": {"kind": "source", "contents": "reaction solutionB"},
        "extractionA": {"kind": "container"},
        "extractionB": {"kind": "container"},
    },
    "valve_ports": {
        "valveA": {
            "water": 1,
            "waste": 2,
            "solutionA": 3,
            "extractionA": 4,
            #"transfer_to_valveB": {"port": 5, "connects_to": "valveB"},
        },
        "valveB": {
            "water": 1,
            "waste": 2,
            "solutionB": 3,
            "extractionB": 4,
            "reactor": 5,
        }
    },
    "connections": [
        ("pump1", "valveA", {"valve_port_role": "common"}),# {"pump_direction": "CCW"}),
        ("pump1", "valveB", {"valve_port_role": "common"}),# {"pump_direction": "CW"}),
        ("reactor", "stirring_plate"),
    ],
}

def example_topology() -> SetupTopology:
    return build_topology(EXAMPLE_SETUP)

def find_path(source: str, destination: str) -> Route:
    """Compatibility helper for quick experiments with EXAMPLE_SETUP."""

    return example_topology().find_path(source, destination)


if __name__ == "__main__":
    setup = build_topology(EXAMPLE_SETUP)
    # try:
    #     print(setup.find_path("solutionA", "waste").as_path())
    # except TopologyError as exc:
    #     print(exc)
    #print()
    setup.print_values(["solutionA", "water", "waste"])

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import exp_topology
from data_logger import DataLogger

# TODO peristaltic_rate_ml_min is calculated through speed and tubing properties, might be implemented later

@dataclass(frozen=True)
class LiquidMovePlan:
    source: str
    destination: str
    volume_ml: float
    route: exp_topology.Route
    actions: tuple[dict[str, Any], ...]
    volume_changes: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "destination": self.destination,
            "volume_ml": self.volume_ml,
            "route": self.route.as_dict(),
            "actions": list(self.actions),
            "volume_changes": list(self.volume_changes),
        }

    def as_path(self) -> str:
        return self.route.as_path()
    
    def show_liquids(self) -> list:
        return list(self.volume_changes)

class CommandError(ValueError):
    """Raised when a liquid move cannot be planned or executed."""

class CommandAborted(RuntimeError):
    """Raised when a running command is interrupted on purpose."""

    def __init__(self, message: str, *, estimated_volume_ml: float | None = None) -> None:
        super().__init__(message)
        self.estimated_volume_ml = estimated_volume_ml

@dataclass
class RunControl:
    """
    enables functionality to interrupt or stop running threads
    """
    abort_event: threading.Event = field(default_factory=threading.Event)

    def request_abort(self) -> None:
        self.abort_event.set()

    def reset_abort(self) -> None:
        self.abort_event.clear()

##### factory functions #####
def _plan_liquid_move(
    *,
    setup: exp_topology.SetupTopology,
    route: exp_topology.Route,
    volume_ml: float,
    syringe_rate_ml_min: float,
    peristaltic_rate_ml_min: float | None,
    peristaltic_speed: int,
) -> LiquidMovePlan:
    pump_name = _single_pump_name(setup, route)
    pump = setup.nodes[pump_name]

    if pump.kind == "syringe_pump":
        actions = _plan_syringe_actions(
            route=route,
            pump_name=pump_name,
            volume_ml=volume_ml,
            rate_ml_min=syringe_rate_ml_min,
        )
    elif pump.kind == "peristaltic_pump":
        actions = _plan_peristaltic_actions(
            setup=setup,
            route=route,
            pump_name=pump_name,
            volume_ml=volume_ml,
            rate_ml_min=peristaltic_rate_ml_min,
            speed=peristaltic_speed,
        )
    else:
        raise CommandError(
            f"Pump {pump_name!r} has unsupported kind {pump.kind!r}. "
            "Use 'syringe_pump' or 'peristaltic_pump'."
        )

    return LiquidMovePlan(
        source=route.source,
        destination=route.destination,
        volume_ml=volume_ml,
        route=route,
        actions=tuple(actions),
        volume_changes=tuple(_volume_changes(route.source, route.destination, volume_ml)),
    )

def _plan_syringe_actions(
    *,
    route: exp_topology.Route,
    pump_name: str,
    volume_ml: float,
    rate_ml_min: float,
) -> list[dict[str, Any]]:
    valve_steps = route.valve_steps()
    if len(valve_steps) < 2:
        raise CommandError(
            "Syringe transfers need at least two valve steps: one leaf port to "
            f"load from and one leaf port to eject to. Route {route.as_path()!r} "
            f"only has valve steps: {valve_steps!r}. Put the destination on a "
            "valve port, or connect to a second valve through a valve leaf."
        )

    actions: list[dict[str, Any]] = []
    actions.extend(_valve_actions([valve_steps[0]]))
    actions.append(
        {
            "type": "run_pump",
            "pump": pump_name,
            "kind": "syringe_pump",
            "direction": "WDR",
            "volume_ml": volume_ml,
            "rate_ml_min": rate_ml_min,
            "phase": "load",
        }
    )
    actions.extend(_valve_actions(valve_steps[1:]))
    actions.append(
        {
            "type": "run_pump",
            "pump": pump_name,
            "kind": "syringe_pump",
            "direction": "INF",
            "volume_ml": volume_ml,
            "rate_ml_min": rate_ml_min,
            "phase": "eject",
        }
    )
    return actions

def _plan_peristaltic_actions(
    *,
    setup: exp_topology.SetupTopology,
    route: exp_topology.Route,
    pump_name: str,
    volume_ml: float,
    rate_ml_min: float | None,
    speed: int | None,
) -> list[dict[str, Any]]:
    actions = _valve_actions(route.valve_steps())
    direction = _peristaltic_direction(setup, route, pump_name)
    resolved_speed, resolved_rate = _resolve_peristaltic_flow(
        setup,
        pump_name,
        direction=direction,
        rpm=speed,
        rate_ml_min=rate_ml_min,
    )
    cw = direction == "CW"
    pump_action: dict[str, Any] = {
        "type": "run_pump",
        "pump": pump_name,
        "kind": "peristaltic_pump",
        "direction": direction,
        "cw": cw,
        "volume_ml": volume_ml,
        "speed": resolved_speed,
        "rate_ml_min": resolved_rate,
    }
    pump_action["duration_sec"] = (volume_ml / resolved_rate) * 60.0
    actions.append(pump_action)
    return actions

def _valve_actions(valve_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "set_valve",
            "valve": step["valve"],
            "port": step["port"],
            "node": step["node"],
        }
        for step in valve_steps
    ]

def _single_pump_name(setup: exp_topology.SetupTopology, route: exp_topology.Route) -> str:
    pump_names = [
        name
        for name in route.nodes
        if setup.nodes[name].kind in {"syringe_pump", "peristaltic_pump"}
    ]
    unique_pumps = list(dict.fromkeys(pump_names))
    if not unique_pumps:
        raise CommandError(f"Route has no pump: {route.as_path()}")
    if len(unique_pumps) > 1:
        raise CommandError(
            f"Routes with multiple pumps are not supported yet: {route.as_path()}"
        )
    return unique_pumps[0]

def _peristaltic_direction(
    setup: exp_topology.SetupTopology,
    route: exp_topology.Route,
    pump_name: str,
) -> str:
    pump_index = route.nodes.index(pump_name)
    if pump_index == 0 or pump_index == len(route.nodes) - 1:
        raise CommandError(f"Pump {pump_name!r} is not between two route nodes")

    incoming_node = route.nodes[pump_index - 1]
    outgoing_node = route.nodes[pump_index + 1]
    incoming_connection = route.connections[pump_index - 1]
    outgoing_connection = route.connections[pump_index]

    outgoing_direction = _connection_pump_direction(
        outgoing_connection,
        from_node=pump_name,
        to_node=outgoing_node,
    )
    if outgoing_direction is not None:
        return outgoing_direction

    incoming_direction = _connection_pump_direction(
        incoming_connection,
        from_node=incoming_node,
        to_node=pump_name,
    )
    if incoming_direction is not None:
        return _opposite_pump_direction(incoming_direction)

    pump_neighbors = [
        neighbor
        for neighbor, _connection in setup._neighbors[pump_name]
        if neighbor != pump_name
    ]
    if len(pump_neighbors) < 2:
        raise CommandError(
            f"Peristaltic pump {pump_name!r} needs two physical connections"
        )

    cw_from = setup.nodes[pump_name].metadata.get("cw_from", pump_neighbors[0])
    cw_to = setup.nodes[pump_name].metadata.get("cw_to", pump_neighbors[1])
    if incoming_node == cw_from and outgoing_node == cw_to:
        return "CW"
    if incoming_node == cw_to and outgoing_node == cw_from:
        return "CCW"

    raise CommandError(
        f"Route crosses {pump_name!r} through {incoming_node!r}->{outgoing_node!r}, "
        f"but its CW direction is configured as {cw_from!r}->{cw_to!r}"
    )

def _connection_pump_direction(
    connection: exp_topology.Connection,
    *,
    from_node: str,
    to_node: str,
) -> str | None:
    direction = connection.metadata.get("pump_direction")
    if direction is None:
        return None

    normalized_direction = _normalize_pump_direction(direction)
    if connection.a == from_node and connection.b == to_node:
        return normalized_direction
    if connection.a == to_node and connection.b == from_node:
        return _opposite_pump_direction(normalized_direction)

    raise CommandError(
        f"Connection {connection.a!r}->{connection.b!r} is not on "
        f"{from_node!r}->{to_node!r}"
    )

def _normalize_pump_direction(direction: Any) -> str:
    normalized = str(direction).upper()
    if normalized not in {"CW", "CCW"}:
        raise CommandError(f"Pump direction must be 'CW' or 'CCW', got {direction!r}")
    return normalized

def _opposite_pump_direction(direction: str) -> str:
    return "CCW" if direction == "CW" else "CW"

def _volume_changes(source: str, destination: str, volume_ml: float) -> list[dict[str, Any]]:
    return [
        {"node": source, "delta_ml": -volume_ml},
        {"node": destination, "delta_ml": volume_ml},
    ]

def _validate_volume(ml: float) -> float:
    volume_ml = float(ml)
    if volume_ml <= 0:
        raise CommandError("Volume must be greater than 0 mL")
    return volume_ml

def _validate_volume_changes(
    setup: exp_topology.SetupTopology,
    changes: tuple[dict[str, Any], ...],
) -> None:
    for change in changes:
        node = setup.nodes[change["node"]]
        current_volume = node.metadata.get("volume_ml")
        max_volume = node.metadata.get("max_volume_ml")
        new_volume = None

        if current_volume is not None:
            new_volume = float(current_volume) + float(change["delta_ml"])
            if new_volume < 0:
                raise CommandError(
                    f"Not enough volume in {node.name!r}: "
                    f"{current_volume} mL available, {-change['delta_ml']} mL requested"
                )
        elif change["delta_ml"] < 0 and node.kind not in {"source", "endpoint"}:
            raise CommandError(f"Cannot withdraw from {node.name!r}; no volume is known")

        if max_volume is not None:
            if new_volume is None:
                new_volume = float(change["delta_ml"])
            if new_volume > float(max_volume):
                raise CommandError(
                    f"Moving liquid would exceed {node.name!r} capacity: "
                    f"{new_volume} mL > {max_volume} mL"
                )

def _apply_volume_changes(
    setup: exp_topology.SetupTopology,
    changes: tuple[dict[str, Any], ...],
    *,
    volume_status: str = "confirmed",
) -> None:
    for change in changes:
        node = setup.nodes[change["node"]]
        new_volume: float | None = None
        if "volume_ml" in node.metadata:
            new_volume = (
                float(node.metadata["volume_ml"]) + float(change["delta_ml"])
            )
            node.metadata["volume_ml"] = new_volume
        elif change["delta_ml"] > 0:
            new_volume = float(change["delta_ml"])
            node.metadata["volume_ml"] = new_volume

        if new_volume is not None:
            _update_runtime_state(
                setup,
                change["node"],
                {
                    "volume_ml": float(new_volume),
                    "volume_status": volume_status,
                },
            )
            node.metadata["volume_status"] = volume_status

def _execute_actions(
    setup: exp_topology.SetupTopology,
    actions: tuple[dict[str, Any], ...],
    *,
    abort_event: threading.Event | None = None,
) -> None:
    for action in actions:
        _check_abort(abort_event)
        if action["type"] == "set_valve":
            valve = _device_handle(setup, action["valve"])
            valve.move_to_position(action["port"])
            _update_runtime_state(
                setup,
                action["valve"],
                {
                    "current_port": int(action["port"]),
                    "current_port_name": action.get("node"),
                },
            )
        elif action["type"] == "run_pump":
            pump = _device_handle(setup, action["pump"])
            if action["kind"] == "syringe_pump":
                _update_runtime_state(
                    setup,
                    action["pump"],
                    {
                        "running": True,
                        "direction": action["direction"],
                        "target_volume_ml": float(action["volume_ml"]),
                    },
                )
                pump.run(action["volume_ml"], action["rate_ml_min"], action["direction"])
                _check_abort(abort_event)
                _update_runtime_state(
                    setup,
                    action["pump"],
                    {
                        "running": False,
                        "current_volume_ml": 0.0,
                        "last_volume_ml": float(action["volume_ml"]),
                        "last_rate_ml_min": float(action["rate_ml_min"]),
                        "last_direction": action["direction"],
                    },
                )
            elif action["kind"] == "peristaltic_pump":
                _update_runtime_state(
                    setup,
                    action["pump"],
                    {
                        "running": True,
                        "direction": action["direction"],
                        "target_rpm": int(action["speed"]),
                        "current_rpm": int(action["speed"]),
                    },
                )
                try:
                    _execute_peristaltic_action(pump, action, abort_event=abort_event)
                    _check_abort(abort_event)
                    _update_runtime_state(
                        setup,
                        action["pump"],
                        {
                            "running": False,
                            "current_rpm": 0,
                            "last_speed": int(action["speed"]),
                            "last_volume_ml": float(action["volume_ml"]),
                            "last_direction": action["direction"],
                            "last_duration_sec": float(action["duration_sec"]),
                            "last_transfer_estimated": False,
                        },
                    )
                except CommandAborted as exc:
                    _update_runtime_state(
                        setup,
                        action["pump"],
                        {
                            "running": False,
                            "current_rpm": 0,
                            "last_speed": int(action["speed"]),
                            "last_volume_ml": float(exc.estimated_volume_ml or 0.0),
                            "last_direction": action["direction"],
                            "last_duration_sec": float(action["duration_sec"]),
                            "last_transfer_estimated": True,
                        },
                    )
                    raise
            else:
                raise CommandError(f"Unknown pump action kind {action['kind']!r}")

def _execute_peristaltic_action(
    pump: Any,
    action: dict[str, Any],
    *,
    abort_event: threading.Event | None = None,
) -> None:
    if "duration_sec" not in action:
        raise CommandError(
            "Peristaltic execution needs peristaltic_rate_ml_min to compute duration"
        )
    duration_sec = float(action["duration_sec"])
    pump.Set_speed(
        Duration="Keep",
        Speed=action["speed"],
        ON=True,
        CW=action["cw"],
    )
    started_at = time.time()
    try:
        _abortable_sleep(duration_sec, abort_event=abort_event)
    except CommandAborted:
        elapsed = max(0.0, time.time() - started_at)
        estimated_volume_ml = min(
            float(action["volume_ml"]),
            max(0.0, (float(action["rate_ml_min"]) / 60.0) * elapsed),
        )
        raise CommandAborted(
            "Command interrupted by stop request",
            estimated_volume_ml=estimated_volume_ml,
        )
    finally:
        if hasattr(pump, "Stop"):
            pump.Stop()

def _device_handle(setup: exp_topology.SetupTopology, name: str) -> Any:
    handle = setup.nodes[name].handle
    if handle is None:
        raise CommandError(f"Node {name!r} has no hardware handle")
    return handle

def _setup(
    topology: exp_topology.SetupTopology | None,
) -> exp_topology.SetupTopology:
    return topology or exp_topology.example_topology()

def _known_volume(setup: exp_topology.SetupTopology, node_name: str) -> float:
    return _validate_volume(_node_volume(setup, node_name))

def _max_volume(setup: exp_topology.SetupTopology, node_name: str) -> float:
    if node_name not in setup.nodes:
        raise CommandError(f"Unknown topology node: {node_name!r}")

    volume = setup.nodes[node_name].metadata.get("max_volume_ml")
    if volume is None:
        raise CommandError(
            f"Cannot infer max_volume for {node_name!r}; "
            "add 'max_volume_ml' to the topology node."
        )
    return _validate_volume(volume)

def _node_volume(setup: exp_topology.SetupTopology, node_name: str) -> float:
    if node_name not in setup.nodes:
        raise CommandError(f"Unknown topology node: {node_name!r}")

    volume = setup.nodes[node_name].metadata.get("volume_ml")
    if volume is None:
        raise CommandError(
            f"Cannot infer volume for {node_name!r}; pass ml explicitly or add "
            "'volume_ml' to the topology node."
        )
    return float(volume)

def _valve_port_name(
    setup: exp_topology.SetupTopology,
    valve_name: str,
    port: int,
) -> str | None:
    for _neighbor, connection in setup._neighbors.get(valve_name, []):
        if connection.metadata.get("valve") != valve_name:
            continue
        if connection.metadata.get("port") == port:
            port_name = connection.metadata.get("port_name")
            if port_name is not None:
                return str(port_name)
    return None

def _hotplate_handle(
    hotplate: str | Any,
    topology: exp_topology.SetupTopology,
) -> Any:
    if isinstance(hotplate, str):
        return _device_handle(topology, hotplate)
    return hotplate

def _reactor_hotplate_name(
    setup: exp_topology.SetupTopology,
    reactor: str,
) -> str:
    if reactor not in setup.nodes:
        raise CommandError(f"Unknown topology node: {reactor!r}")
    if setup.nodes[reactor].kind != "reactor":
        raise CommandError(
            f"Node {reactor!r} must be a 'reactor', not {setup.nodes[reactor].kind!r}"
        )

    hotplates = [
        neighbor
        for neighbor, _connection in setup._neighbors.get(reactor, [])
        if setup.nodes[neighbor].kind in {"stirrer", "magnetic_stirrer", "hotplate"}
    ]
    if not hotplates:
        raise CommandError(f"Reactor {reactor!r} has no attached stirrer/hotplate")
    if len(hotplates) > 1:
        raise CommandError(
            f"Reactor {reactor!r} has multiple attached stirrer/hotplates: "
            f"{', '.join(hotplates)}"
        )
    return hotplates[0]

def _circulation_pump_handle(
    pump: str | Any,
    topology: exp_topology.SetupTopology | None,
) -> Any:
    if isinstance(pump, str):
        setup = _setup(topology)
        if pump not in setup.nodes:
            raise CommandError(f"Unknown topology node: {pump!r}")
        if setup.nodes[pump].kind != "peristaltic_pump":
            raise CommandError(
                f"Circulation pump {pump!r} must be a 'peristaltic_pump', "
                f"not {setup.nodes[pump].kind!r}"
            )
        return _device_handle(setup, pump)
    return pump

def _circulation_direction(direction: str | bool) -> bool:
    if isinstance(direction, bool):
        return direction

    normalized = _normalize_pump_direction(direction)
    return normalized == "CW"

def _validate_serial_cable_node(
    setup: exp_topology.SetupTopology,
    cable: str,
) -> None:
    if cable not in setup.nodes:
        raise CommandError(f"Unknown topology node: {cable!r}")
    if setup.nodes[cable].kind != "serial_cable":
        raise CommandError(
            f"Node {cable!r} must be a 'serial_cable', not {setup.nodes[cable].kind!r}"
        )

def _sync_serial_cable_values(
    setup: exp_topology.SetupTopology,
    cable: str,
    handle: Any,
) -> dict[str, Any]:
    values = _collect_serial_cable_values(handle)
    _update_runtime_state(setup, cable, values)
    return values

def _collect_serial_cable_values(handle: Any) -> dict[str, Any]:
    values: dict[str, Any] = {}
    try:
        values["rts"] = bool(getattr(handle, "rts"))
    except Exception:
        try:
            values["rts"] = bool(handle.getRTS())
        except Exception:
            pass
    try:
        values["cts"] = bool(getattr(handle, "cts"))
    except Exception:
        try:
            values["cts"] = bool(handle.getCTS())
        except Exception:
            pass
    return values

def _validate_temperature(temp):
    temp_C = float(temp)
    if temp_C < 0:
        raise CommandError("Temperature must be equal or greater than 0.0 degC")
    return temp_C

def _sync_hotplate_values(
    setup: exp_topology.SetupTopology,
    hotplate: str | Any,
    plate: Any,
) -> dict[str, Any]:
    node_name = hotplate if isinstance(hotplate, str) else _find_handle_node_name(setup, plate)
    if node_name is None:
        return {}

    values = _collect_hotplate_values(plate)
    _update_runtime_state(setup, node_name, values)
    return values

def _collect_hotplate_values(plate: Any) -> dict[str, Any]:
    fields = {
        "target_temperature": "target_temperature",
        "probe_temperature": "probe_temperature",
        "hotplate_sensor_temperature": "hotplate_sensor_temperature",
        "target_stir_rate": "target_stir_rate",
        "stir_rate": "stir_rate",
        "heating": "heating",
        "stirring": "stirring",
    }
    values: dict[str, Any] = {}
    for metadata_key, attribute_name in fields.items():
        try:
            values[metadata_key] = getattr(plate, attribute_name)
        except Exception:
            continue
    return values

def _find_handle_node_name(
    setup: exp_topology.SetupTopology,
    handle: Any,
) -> str | None:
    for name, node in setup.nodes.items():
        if node.handle is handle:
            return name
    return None

def _hotplate_logger(
    hotplate: str | Any,
    setup: exp_topology.SetupTopology,
    *,
    create_if_missing: bool = False,
    log_interval_s: float = 1.0,
    hardware_refresh_s: float = 10.0,
    save_folder: str | None = None,
    verbose: bool = False,
) -> DataLogger:
    node_name: str | None
    if isinstance(hotplate, str):
        node_name = hotplate
        if node_name not in setup.nodes:
            raise CommandError(f"Unknown topology node: {node_name!r}")
        plate = _hotplate_handle(node_name, setup)
        node = setup.nodes[node_name]
    else:
        plate = hotplate
        node_name = _find_handle_node_name(setup, hotplate)
        if node_name is None:
            raise CommandError("Hotplate handle is not attached to a topology node")
        node = setup.nodes[node_name]

    logger = node.metadata.get("data_logger")
    if logger is None and create_if_missing:
        logger = DataLogger(
            plate_obj=plate,
            plate_name=node_name,
            timer=log_interval_s,
            hardware_refresh_s=hardware_refresh_s,
            save_folder=save_folder,
            on_update=lambda values: _update_runtime_state(setup, node_name, values),
            verbose=verbose,
        )
        node.metadata["data_logger"] = logger
        _update_runtime_state(
            setup,
            node_name,
            {
                "logging_active": False,
                "logging_paused": False,
                "log_interval_s": float(log_interval_s),
                "hardware_refresh_s": float(hardware_refresh_s),
                "log_file": str(logger.file_name),
                "logging_verbose": bool(verbose),
            },
        )
    if logger is None:
        raise CommandError(
            f"No data logger exists for {node_name!r}; call start_data_logging(...) first"
        )
    return logger

def _update_runtime_state(
    setup: exp_topology.SetupTopology,
    node_name: str,
    values: dict[str, Any],
) -> dict[str, Any]:
    return setup.update_node_state(node_name, values, mirror_to_metadata=True)

def _sync_circulation_state(
    setup: exp_topology.SetupTopology,
    pump: str | Any,
    values: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(pump, str):
        node_name = _find_handle_node_name(setup, pump)
        if node_name is None:
            return None
    else:
        node_name = pump
    return _update_runtime_state(setup, node_name, values)

def _check_abort(abort_event: threading.Event | None) -> None:
    if abort_event is not None and abort_event.is_set():
        raise CommandAborted("Command interrupted by stop request")

def _abortable_sleep(
    seconds: float,
    *,
    abort_event: threading.Event | None = None,
    poll_s: float = 0.1,
) -> None:
    remaining = float(seconds)
    while remaining > 0:
        _check_abort(abort_event)
        step = min(poll_s, remaining)
        time.sleep(step)
        remaining -= step
#TODO unused
def _pump_handle(
    pump: str,
    setup: exp_topology.SetupTopology,
    *,
    expected_kind: str | None = None,
) -> Any:
    _validate_pump_node(setup, pump, expected_kind=expected_kind)
    return _device_handle(setup, pump)

def _validate_pump_node(
    setup: exp_topology.SetupTopology,
    pump: str,
    *,
    expected_kind: str | None = None,
) -> None:
    if pump not in setup.nodes:
        raise CommandError(f"Unknown topology node: {pump!r}")
    if expected_kind is not None and setup.nodes[pump].kind != expected_kind:
        raise CommandError(
            f"Pump {pump!r} must be a {expected_kind!r}, not {setup.nodes[pump].kind!r}"
        )

def _normalize_manual_pump_direction(direction: str | bool) -> str:
    if isinstance(direction, bool):
        return "CW" if direction else "CCW"
    return _normalize_pump_direction(direction)

def _normalize_syringe_direction(direction: str) -> str:
    normalized = str(direction).upper()
    if normalized not in {"WDR", "INF"}:
        raise CommandError(
            f"Syringe pump direction must be 'WDR' or 'INF', got {direction!r}"
        )
    return normalized

def _validate_peristaltic_duration(duration: float | str) -> float | str:
    if isinstance(duration, str):
        if duration != "Keep":
            raise CommandError("Peristaltic pump duration must be a number or 'Keep'")
        return duration
    target_duration = float(duration)
    if target_duration <= 0:
        raise CommandError("Peristaltic pump duration must be greater than 0")
    return target_duration

def _validate_positive_int(value: int, *, label: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise CommandError(f"{label} must be a positive integer")
    return value

def _resolve_peristaltic_flow(
    setup: exp_topology.SetupTopology,
    pump_name: str,
    *,
    direction: str,
    rpm: int | None,
    rate_ml_min: float | None,
) -> tuple[int, float]:
    calibration = _peristaltic_calibration(setup, pump_name, direction)

    if rpm is not None and rate_ml_min is not None:
        return _validate_positive_int(rpm, label="Peristaltic pump rpm"), _validate_positive_float(
            rate_ml_min, label="Peristaltic pump rate_ml_min"
        )

    if calibration is not None:
        ref_rpm, ref_rate = calibration
        if rpm is not None:
            target_rpm = _validate_positive_int(rpm, label="Peristaltic pump rpm")
            target_rate = ref_rate * (target_rpm / ref_rpm)
            return target_rpm, float(target_rate)
        if rate_ml_min is not None:
            target_rate = _validate_positive_float(rate_ml_min, label="Peristaltic pump rate_ml_min")
            target_rpm = int(round(ref_rpm * (target_rate / ref_rate)))
            return _validate_positive_int(target_rpm, label="Peristaltic pump rpm"), target_rate
        return ref_rpm, ref_rate

    if rpm is None and rate_ml_min is None:
        return 999, 120.0
    if rpm is None:
        raise CommandError(
            f"Cannot infer rpm for {pump_name!r} in direction {direction!r}; "
            "pass rpm explicitly or add calibration like 'CW_flow': (rpm, ml_min)."
        )
    if rate_ml_min is None:
        raise CommandError(
            f"Cannot infer rate_ml_min for {pump_name!r} in direction {direction!r}; "
            "pass rate_ml_min explicitly or add calibration like 'CW_flow': (rpm, ml_min)."
        )
    raise CommandError("Could not resolve peristaltic flow settings")

def _peristaltic_calibration(
    setup: exp_topology.SetupTopology,
    pump_name: str,
    direction: str,
) -> tuple[int, float] | None:
    node = setup.nodes[pump_name]
    key = f"{direction}_flow"
    raw_value = node.metadata.get(key)
    if raw_value is None:
        return None
    if not isinstance(raw_value, (tuple, list)) or len(raw_value) != 2:
        raise CommandError(
            f"{pump_name!r} metadata {key!r} must be a 2-item tuple/list like (900, 100)"
        )

    ref_rpm = _validate_positive_int(int(raw_value[0]), label=f"{pump_name} {key} rpm")
    ref_rate = _validate_positive_float(float(raw_value[1]), label=f"{pump_name} {key} rate_ml_min")
    return ref_rpm, ref_rate

def _validate_positive_float(value: float, *, label: str) -> float:
    target_value = float(value)
    if target_value <= 0:
        raise CommandError(f"{label} must be greater than 0")
    return target_value


##### reactor interaction #####
def fill_reactor(
    source: str,
    ml: float,
    *,
    reactor: str = "reactor",
    topology: exp_topology.SetupTopology | None = None,
    execute: bool = False,
    update_volumes: bool = True,
    syringe_rate_ml_min: float = 100.0,
    peristaltic_rate_ml_min: float | None = None,
    peristaltic_speed: int | None = None,
    abort_event: threading.Event | None = None,
) -> LiquidMovePlan:
    """Move liquid from a source/container into the reactor."""

    setup = _setup(topology)
    return move_liquid(
        source,
        reactor,
        ml,
        topology=setup,
        execute=execute,
        update_volumes=update_volumes,
        syringe_rate_ml_min=syringe_rate_ml_min,
        peristaltic_rate_ml_min=peristaltic_rate_ml_min,
        peristaltic_speed=peristaltic_speed,
        abort_event=abort_event,
    )

def empty_reactor(
    *,
    reactor: str = "reactor",
    destination: str = "waste",
    ml: float | None = None,
    topology: exp_topology.SetupTopology | None = None,
    execute: bool = False,
    update_volumes: bool = True,
    syringe_rate_ml_min: float = 100.0,
    peristaltic_rate_ml_min: float | None = None,
    peristaltic_speed: int | None = None,
    abort_event: threading.Event | None = None,
) -> LiquidMovePlan:
    """Move either a requested volume or the full known reactor volume to waste or given destination."""

    setup = _setup(topology)
    sample_volume = _known_volume(setup, reactor) if ml is None else _validate_volume(ml)
    return move_liquid(
        reactor,
        destination,
        sample_volume,
        topology=setup,
        execute=execute,
        update_volumes=update_volumes,
        syringe_rate_ml_min=syringe_rate_ml_min,
        peristaltic_rate_ml_min=peristaltic_rate_ml_min,
        peristaltic_speed=peristaltic_speed,
        abort_event=abort_event,
    )

def cleaning(
    reactor: str = "reactor",
    cycles: int = 1,
    *,
    topology: exp_topology.SetupTopology | None = None,
    water_source: str = "water",
    waste_destination: str = "waste",
    fill_volume_ml: float | None = None,
    fill_fraction: float = 0.5,
    wait_seconds: float = 30,
    execute: bool = False,
    update_volumes: bool = True,
    syringe_rate_ml_min: float = 100.0,
    peristaltic_rate_ml_min: float | None = None,
    peristaltic_speed: int | None = None,
    stir_rpm: int =500,
    abort_event: threading.Event | None = None,
) -> list[LiquidMovePlan]:
    """Empty, rinse, wait, and empty the reactor for a number of cycles."""

    if not isinstance(cycles, int) or cycles < 1:
        raise CommandError("Cleaning cycles must be an integer greater than 0")
    if fill_fraction <= 0:
        raise CommandError("fill_fraction must be greater than 0")

    setup = _setup(topology)
    rinse_volume = (
        _validate_volume(fill_volume_ml)
        if fill_volume_ml is not None
        else _max_volume(setup, reactor) * float(fill_fraction)
    )
    _validate_volume(rinse_volume)
    if stir_rpm is not None:
        _validate_positive_int(stir_rpm, label="Cleaning stir_rpm")

    plans: list[LiquidMovePlan] = []
    stirring_started = False
    try:
        if execute and stir_rpm is not None:
            stir_reactor(stir_rpm, reactor=reactor, topology=setup)
            stirring_started = True

        for _cycle in range(cycles):
            _check_abort(abort_event)
            if _node_volume(setup, reactor) > 0:
                plans.append(
                    empty_reactor(
                        reactor=reactor,
                        destination=waste_destination,
                        topology=setup,
                        execute=execute,
                        update_volumes=update_volumes,
                        syringe_rate_ml_min=syringe_rate_ml_min,
                        peristaltic_rate_ml_min=peristaltic_rate_ml_min,
                        peristaltic_speed=peristaltic_speed,
                        abort_event=abort_event,
                    )
                )

            _check_abort(abort_event)
            plans.append(
                fill_reactor(
                    water_source,
                    rinse_volume,
                    reactor=reactor,
                    topology=setup,
                    execute=execute,
                    update_volumes=update_volumes,
                    syringe_rate_ml_min=syringe_rate_ml_min,
                    peristaltic_rate_ml_min=peristaltic_rate_ml_min,
                    peristaltic_speed=peristaltic_speed,
                    abort_event=abort_event,
                )
            )
            if wait_seconds > 0:
                _abortable_sleep(wait_seconds, abort_event=abort_event)

            _check_abort(abort_event)
            plans.append(
                empty_reactor(
                    reactor=reactor,
                    destination=waste_destination,
                    topology=setup,
                    execute=execute,
                    update_volumes=update_volumes,
                    syringe_rate_ml_min=syringe_rate_ml_min,
                    peristaltic_rate_ml_min=peristaltic_rate_ml_min,
                    peristaltic_speed=peristaltic_speed,
                    abort_event=abort_event,
                )
            )
    finally:
        if stirring_started:
            stop_reactor_stirring(reactor=reactor, topology=setup)

    return plans

def heat_reactor(
    temp: float,
    *,
    reactor: str = "reactor",
    topology: exp_topology.SetupTopology | None = None,
    wait_for_temp: bool = False,
    tolerance: float = 0,
    wait_until_stable: bool = False,
    stable_timeout: float = 60,
) -> Any:
    setup = _setup(topology)
    hotplate = _reactor_hotplate_name(setup, reactor)
    return start_heating(
        temp,
        hotplate=hotplate,
        topology=setup,
        wait_for_temp=wait_for_temp,
        tolerance=tolerance,
        wait_until_stable=wait_until_stable,
        stable_timeout=stable_timeout,
    )

def stop_reactor_heating(
    reactor: str = "reactor",
    *,
    topology: exp_topology.SetupTopology | None = None,
) -> Any:
    setup = _setup(topology)
    hotplate = _reactor_hotplate_name(setup, reactor)
    return stop_heating(hotplate=hotplate, topology=setup)

def stir_reactor(
    rpm: int,
    *,
    reactor: str = "reactor",
    topology: exp_topology.SetupTopology | None = None,
    wait_for_rpm: bool = False,
    tolerance: int = 0,
) -> Any:
    setup = _setup(topology)
    hotplate = _reactor_hotplate_name(setup, reactor)
    return start_stirring(
        rpm,
        hotplate=hotplate,
        topology=setup,
        wait_for_rpm=wait_for_rpm,
        tolerance=tolerance,
    )

def stop_reactor_stirring(
    reactor: str = "reactor",
    *,
    topology: exp_topology.SetupTopology | None = None,
) -> Any:
    setup = _setup(topology)
    hotplate = _reactor_hotplate_name(setup, reactor)
    return stop_stirring(hotplate=hotplate, topology=setup)


##### peristaltic pump interaction #####
def move_liquid(
    source: str,
    destination: str,
    ml: float,
    *,
    topology: exp_topology.SetupTopology | None = None,
    execute: bool = False,
    update_volumes: bool = True,
    syringe_rate_ml_min: float = 100.0,
    peristaltic_rate_ml_min: float | None = None,
    peristaltic_speed: int | None = None,
    abort_event: threading.Event | None = None,
) -> LiquidMovePlan:
    """
    Plan, and optionally execute, a liquid transfer through the setup topology.

    By default this returns a dry-run plan and updates known in-memory volumes.
    Set execute=True to call device handles stored in topology nodes.
    """

    setup = _setup(topology)
    volume_ml = _validate_volume(ml)
    route = setup.find_path(source, destination)
    plan = _plan_liquid_move(
        setup=setup,
        route=route,
        volume_ml=volume_ml,
        syringe_rate_ml_min=syringe_rate_ml_min,
        peristaltic_rate_ml_min=peristaltic_rate_ml_min,
        peristaltic_speed=peristaltic_speed,
    )

    _validate_volume_changes(setup, plan.volume_changes)
    active_transfer_id: int | None = None
    try:
        if execute:
            active_transfer_id = setup.begin_liquid_transfer(route, volume_ml)
            try:
                _execute_actions(setup, plan.actions, abort_event=abort_event)
            finally:
                setup.end_liquid_transfer(active_transfer_id)
        _check_abort(abort_event)
        if update_volumes:
            _apply_volume_changes(setup, plan.volume_changes, volume_status="confirmed")
    except CommandAborted as exc:
        estimated_volume_ml = getattr(exc, "estimated_volume_ml", None)
        if (
            update_volumes
            and estimated_volume_ml is not None
            and estimated_volume_ml > 0
        ):
            partial_changes = tuple(_volume_changes(route.source, route.destination, estimated_volume_ml))
            _apply_volume_changes(setup, partial_changes, volume_status="estimated")
        raise

    return plan

def start_circulation(
    *,
    pump: str | Any = "circulation_pump",
    topology: exp_topology.SetupTopology | None = None,
    direction: str | bool = "CW",
    duration_hours: float = 1 / 60,
    max_rpm: int = 700,
    min_rpm: int = 500,
    step_rpm: int = 200,
    plateau_time_s: float = 30,
    ramp_time_s: float = 10,
) -> Any:
    """
    start cyclic rpm changes for a peristaltic pump, typically used for flowing reaction solution
    through the measurement cell
    """
    setup = _setup(topology)
    circulation_pump = _circulation_pump_handle(pump, setup)
    direction_bool = _circulation_direction(direction)
    timer_hours = float(duration_hours)
    long_step = float(plateau_time_s)
    short_step = float(ramp_time_s)

    if timer_hours <= 0:
        raise CommandError("Circulation duration_hours must be greater than 0")
    if max_rpm <= 0 or min_rpm <= 0:
        raise CommandError("Circulation rpm values must be greater than 0")
    if min_rpm > max_rpm:
        raise CommandError("min_rpm cannot be greater than max_rpm")
    if step_rpm <= 0:
        raise CommandError("step_rpm must be greater than 0")
    if long_step <= 0 or short_step <= 0:
        raise CommandError("Circulation time steps must be greater than 0")

    circulation_pump.variable_rpm_thread(
        direction=direction_bool,
        timer=timer_hours,
        time_step_long=long_step,
        time_step_short=short_step,
        speed_max=max_rpm,
        speed_min=min_rpm,
        speed_step=step_rpm,
    )
    _sync_circulation_state(
        setup,
        pump,
        {
            "running": True,
            "paused": False,
            "direction": "CW" if direction_bool else "CCW",
            "min_rpm": int(min_rpm),
            "max_rpm": int(max_rpm),
            "target_rpm": int(max_rpm),
            "step_rpm": int(step_rpm),
            "plateau_time_s": long_step,
            "ramp_time_s": short_step,
            "duration_hours": timer_hours,
        },
    )
    return circulation_pump

def pause_circulation(
    pump: str | Any = "circulation_pump",
    *,
    topology: exp_topology.SetupTopology | None = None,
) -> Any:
    setup = _setup(topology)
    circulation_pump = _circulation_pump_handle(pump, setup)
    circulation_pump.pause_variable_rpm()
    _sync_circulation_state(setup, pump, {"running": True, "paused": True})
    return circulation_pump

def resume_circulation(
    pump: str | Any = "circulation_pump",
    *,
    topology: exp_topology.SetupTopology | None = None,
) -> Any:
    setup = _setup(topology)
    circulation_pump = _circulation_pump_handle(pump, setup)
    circulation_pump.resume_variable_rpm()
    _sync_circulation_state(setup, pump, {"running": True, "paused": False})
    return circulation_pump

def stop_circulation(
    pump: str | Any = "circulation_pump",
    *,
    topology: exp_topology.SetupTopology | None = None,
) -> Any:
    setup = _setup(topology)
    circulation_pump = _circulation_pump_handle(pump, setup)
    circulation_pump.stop_variable_rpm()
    _sync_circulation_state(
        setup,
        pump,
        {
            "running": False,
            "paused": False,
            "current_rpm": 0,
            "target_rpm": 0,
        },
    )
    return circulation_pump

def run_peristaltic_pump(
    *,
    pump: str = "pump1",
    rpm: int | None = None,
    rate_ml_min: float | None = None,
    direction: str | bool = "CW",
    duration: float | str = "Keep",
    topology: exp_topology.SetupTopology | None = None,
    execute: bool = True,
) -> dict[str, Any]:
    setup = _setup(topology)
    _validate_pump_node(setup, pump, expected_kind="peristaltic_pump")
    pump_handle = _device_handle(setup, pump) if execute else None
    target_duration = _validate_peristaltic_duration(duration)
    normalized_direction = _normalize_manual_pump_direction(direction)
    calibration = _peristaltic_calibration(setup, pump, normalized_direction)
    if rpm is not None:
        target_rpm = _validate_positive_int(rpm, label="Peristaltic pump rpm")
        if rate_ml_min is not None:
            target_rate = _validate_positive_float(rate_ml_min, label="Peristaltic pump rate_ml_min")
        elif calibration is not None:
            ref_rpm, ref_rate = calibration
            target_rate = float(ref_rate * (target_rpm / ref_rpm))
        else:
            target_rate = None
    elif rate_ml_min is not None:
        target_rpm, target_rate = _resolve_peristaltic_flow(
            setup,
            pump,
            direction=normalized_direction,
            rpm=None,
            rate_ml_min=rate_ml_min,
        )
    else:
        target_rpm, target_rate = _resolve_peristaltic_flow(
            setup,
            pump,
            direction=normalized_direction,
            rpm=None,
            rate_ml_min=None,
        )

    if execute:
        pump_handle.Set_speed(
            Duration=target_duration,
            Speed=target_rpm,
            ON=True,
            CW=normalized_direction == "CW",
        )

    state_payload = {
        "running": True,
        "direction": normalized_direction,
        "current_rpm": target_rpm,
        "target_rpm": target_rpm,
        "last_duration": target_duration,
    }
    if target_rate is not None:
        state_payload["current_rate_ml_min"] = target_rate
        state_payload["target_rate_ml_min"] = target_rate
    state = _update_runtime_state(setup, pump, state_payload)
    return {
        "pump": pump,
        "kind": "peristaltic_pump",
        "rpm": target_rpm,
        "rate_ml_min": target_rate,
        "direction": normalized_direction,
        "duration": target_duration,
        "executed": execute,
        "state": state,
    }

def stop_peristaltic_pump(
    *,
    pump: str = "pump1",
    topology: exp_topology.SetupTopology | None = None,
    execute: bool = True,
) -> dict[str, Any]:
    setup = _setup(topology)
    _validate_pump_node(setup, pump, expected_kind="peristaltic_pump")
    pump_handle = _device_handle(setup, pump) if execute else None
    if execute:
        pump_handle.Stop()

    state = _update_runtime_state(
        setup,
        pump,
        {
            "running": False,
            "current_rpm": 0,
            "target_rpm": 0,
            "current_rate_ml_min": 0.0,
            "target_rate_ml_min": 0.0,
        },
    )
    return {
        "pump": pump,
        "kind": "peristaltic_pump",
        "executed": execute,
        "state": state,
    }


##### syringe pump interaction #####
def run_syringe_pump(
    *,
    pump: str = "pump1",
    volume_ml: float,
    rate_ml_min: float,
    direction: str = "WDR",
    topology: exp_topology.SetupTopology | None = None,
    execute: bool = True,
) -> dict[str, Any]:
    setup = _setup(topology)
    _validate_pump_node(setup, pump, expected_kind="syringe_pump")
    pump_handle = _device_handle(setup, pump) if execute else None
    target_volume = _validate_volume(volume_ml)
    target_rate = float(rate_ml_min)
    if target_rate <= 0:
        raise CommandError("Syringe pump rate_ml_min must be greater than 0")
    normalized_direction = _normalize_syringe_direction(direction)

    if execute:
        pump_handle.run(target_volume, target_rate, normalized_direction)

    current_volume = float(setup.nodes[pump].state.get("current_volume_ml", 0.0))
    if normalized_direction == "WDR":
        current_volume += target_volume
    else:
        current_volume = max(0.0, current_volume - target_volume)

    state = _update_runtime_state(
        setup,
        pump,
        {
            "running": False,
            "direction": normalized_direction,
            "current_volume_ml": current_volume,
            "target_volume_ml": target_volume,
            "last_rate_ml_min": target_rate,
            "last_direction": normalized_direction,
        },
    )
    return {
        "pump": pump,
        "kind": "syringe_pump",
        "volume_ml": target_volume,
        "rate_ml_min": target_rate,
        "direction": normalized_direction,
        "executed": execute,
        "state": state,
    }

def stop_syringe_pump(
    *,
    pump: str = "pump1",
    topology: exp_topology.SetupTopology | None = None,
    execute: bool = True,
) -> dict[str, Any]:
    setup = _setup(topology)
    _validate_pump_node(setup, pump, expected_kind="syringe_pump")
    pump_handle = _device_handle(setup, pump) if execute else None
    if execute:
        pump_handle.stop()

    state = _update_runtime_state(
        setup,
        pump,
        {
            "running": False,
            "target_volume_ml": 0.0,
        },
    )
    return {
        "pump": pump,
        "kind": "syringe_pump",
        "executed": execute,
        "state": state,
    }


##### hotplate interaction #####
def start_heating(
    temp: float,
    *,
    hotplate: str | Any = "stirring_plate",
    topology: exp_topology.SetupTopology | None = None,
    wait_for_temp: bool = False,
    tolerance: float = 0,
    wait_until_stable: bool = False,
    stable_timeout: float = 60,
) -> Any:
    setup = _setup(topology)
    plate = _hotplate_handle(hotplate, setup)
    target_temp = _validate_temperature(temp)

    if wait_for_temp:
        plate.wait_for_temperature(target_temp, tolerance=tolerance)
    else:
        plate.start_heating(target_temp)

    if wait_until_stable:
        plate.wait_until_temperature_stable(time_out=stable_timeout)

    _sync_hotplate_values(setup, hotplate, plate)
    return plate

def heat_for_time(
    temp: float,
    duration: float,
    *,
    hotplate: str | Any = "stirring_plate",
    topology: exp_topology.SetupTopology | None = None,
    wait_for_temp: bool = False,
    tolerance: float = 0,
    wait_until_stable: bool = False,
    stable_timeout: float = 60,
) -> Any:
    """
    sets temperature for a hotplate and will keep it for a given timer
    """
    plate = start_heating(
        temp,
        hotplate=hotplate,
        topology=topology,
        wait_for_temp=wait_for_temp,
        tolerance=tolerance,
        wait_until_stable=wait_until_stable,
        stable_timeout=stable_timeout,
    )
    hold_time = float(duration)
    if hold_time < 0:
        raise CommandError("Heating duration must be 0 seconds or greater")

    try:
        time.sleep(hold_time)
    finally:
        plate.stop_heating()
        _sync_hotplate_values(_setup(topology), hotplate, plate)

    return plate

def stop_heating(
    hotplate: str | Any = "stirring_plate",
    *,
    topology: exp_topology.SetupTopology | None = None,
) -> Any:
    setup = _setup(topology)
    plate = _hotplate_handle(hotplate, setup)
    plate.stop_heating()
    _sync_hotplate_values(setup, hotplate, plate)
    return plate

def start_stirring(
    rpm: int,
    *,
    hotplate: str | Any = "stirring_plate",
    topology: exp_topology.SetupTopology | None = None,
    wait_for_rpm: bool = False,
    tolerance: int = 0,
) -> Any:
    setup = _setup(topology)
    plate = _hotplate_handle(hotplate, setup)
    if not isinstance(rpm, int) or rpm <= 0:
        raise CommandError("Stirring rpm must be a positive integer")

    if wait_for_rpm:
        plate.wait_for_stir(rpm, tolerance=tolerance)
    else:
        plate.start_stirring(rpm)

    _sync_hotplate_values(setup, hotplate, plate)
    return plate

def stop_stirring(
    hotplate: str | Any = "stirring_plate",
    *,
    topology: exp_topology.SetupTopology | None = None,
) -> Any:
    setup = _setup(topology)
    plate = _hotplate_handle(hotplate, setup)
    plate.stop_stirring()
    _sync_hotplate_values(setup, hotplate, plate)
    return plate

def read_hotplate_values(
    hotplate: str | Any = "stirring_plate",
    *,
    topology: exp_topology.SetupTopology | None = None,
) -> dict[str, Any]:
    setup = _setup(topology)
    plate = _hotplate_handle(hotplate, setup)
    return _sync_hotplate_values(setup, hotplate, plate)


##### data logger interaction #####
def start_data_logging(
    *,
    hotplate: str | Any = "stirring_plate",
    topology: exp_topology.SetupTopology | None = None,
    log_interval_s: float = 1.0,
    hardware_refresh_s: float = 10.0,
    save_folder: str | None = None,
    verbose: bool = False,
) -> DataLogger:
    setup = _setup(topology)
    node_name = hotplate if isinstance(hotplate, str) else _find_handle_node_name(setup, hotplate)
    logger = _hotplate_logger(
        hotplate,
        setup,
        create_if_missing=True,
        log_interval_s=log_interval_s,
        hardware_refresh_s=hardware_refresh_s,
        save_folder=save_folder,
        verbose=verbose,
    )
    logger.start()
    if node_name is not None:
        _update_runtime_state(
            setup,
            node_name,
            {"logging_active": True, "logging_paused": False},
        )
    return logger

def attach_data_logger(
    logger: DataLogger | None = None,
    *,
    hotplate: str | Any = "stirring_plate",
    topology: exp_topology.SetupTopology | None = None,
    log_interval_s: float = 1.0,
    hardware_refresh_s: float = 10.0,
    save_folder: str | None = None,
    verbose: bool = False,
) -> DataLogger:
    """
    Attach a logger instance to a hotplate node without starting it.

    If no logger is provided, one is created from the hotplate handle and stored
    on the topology node. GUI controls and command helpers will then reuse this
    attached logger instead of creating a separate one later.
    """

    setup = _setup(topology)
    if isinstance(hotplate, str):
        node_name = hotplate
        if node_name not in setup.nodes:
            raise CommandError(f"Unknown topology node: {node_name!r}")
        plate = _hotplate_handle(node_name, setup)
    else:
        plate = hotplate
        node_name = _find_handle_node_name(setup, hotplate)
        if node_name is None:
            raise CommandError("Hotplate handle is not attached to a topology node")

    node = setup.nodes[node_name]
    if logger is None:
        logger = DataLogger(
            plate_obj=plate,
            plate_name=node_name,
            timer=log_interval_s,
            hardware_refresh_s=hardware_refresh_s,
            save_folder=save_folder,
            on_update=lambda values: _update_runtime_state(setup, node_name, values),
            verbose=verbose,
        )
    else:
        logger.on_update = lambda values: _update_runtime_state(setup, node_name, values)

    node.metadata["data_logger"] = logger
    _update_runtime_state(
        setup,
        node_name,
        {
            "logging_active": bool(logger.active),
            "logging_paused": bool(logger.paused),
            "log_interval_s": float(logger.timer),
            "hardware_refresh_s": float(logger.hardware_refresh_s),
            "log_file": str(logger.file_name),
            "logging_verbose": bool(getattr(logger, "verbose", False)),
        },
    )
    return logger

def pause_data_logging(
    hotplate: str | Any = "stirring_plate",
    *,
    topology: exp_topology.SetupTopology | None = None,
) -> DataLogger:
    setup = _setup(topology)
    node_name = hotplate if isinstance(hotplate, str) else _find_handle_node_name(setup, hotplate)
    logger = _hotplate_logger(hotplate, setup)
    logger.pause()
    if node_name is not None:
        _update_runtime_state(
            setup,
            node_name,
            {"logging_active": True, "logging_paused": True},
        )
    return logger

def resume_data_logging(
    hotplate: str | Any = "stirring_plate",
    *,
    topology: exp_topology.SetupTopology | None = None,
) -> DataLogger:
    setup = _setup(topology)
    node_name = hotplate if isinstance(hotplate, str) else _find_handle_node_name(setup, hotplate)
    logger = _hotplate_logger(hotplate, setup)
    logger.resume()
    if node_name is not None:
        _update_runtime_state(
            setup,
            node_name,
            {"logging_active": True, "logging_paused": False},
        )
    return logger

def stop_data_logging(
    hotplate: str | Any = "stirring_plate",
    *,
    topology: exp_topology.SetupTopology | None = None,
) -> DataLogger:
    setup = _setup(topology)
    node_name = hotplate if isinstance(hotplate, str) else _find_handle_node_name(setup, hotplate)
    logger = _hotplate_logger(hotplate, setup)
    logger.stop()
    if node_name is not None:
        _update_runtime_state(
            setup,
            node_name,
            {"logging_active": False, "logging_paused": False},
        )
    return logger

def add_log_comment(
    comment: str,
    hotplate: str | Any = "stirring_plate",
    *,
    topology: exp_topology.SetupTopology | None = None,
) -> DataLogger:
    logger = _hotplate_logger(hotplate, _setup(topology))
    logger.add_comment(comment)
    return logger


##### valve interaction #####
def set_valve_position(
    port: int = 1,
    *,
    valve: str = "valveA",
    topology: exp_topology.SetupTopology | None = None,
    execute: bool = True,
) -> dict[str, Any]:
    setup = _setup(topology)
    if valve not in setup.nodes:
        raise CommandError(f"Unknown topology node: {valve!r}")
    if setup.nodes[valve].kind != "valve":
        raise CommandError(
            f"Node {valve!r} must be a 'valve', not {setup.nodes[valve].kind!r}"
        )

    valve_port = int(port)
    if not 1 <= valve_port <= 10:
        raise CommandError("Valve port must be between 1 and 10")

    port_name = _valve_port_name(setup, valve, valve_port)
    if execute:
        valve_handle = _device_handle(setup, valve)
        valve_handle.move_to_position(valve_port)

    state = _update_runtime_state(
        setup,
        valve,
        {
            "current_port": valve_port,
            "current_port_name": port_name,
        },
    )
    return {
        "valve": valve,
        "port": valve_port,
        "port_name": port_name,
        "executed": execute,
        "state": state,
    }


##### misc. interaction #####
def emergency_stop(
    *,
    topology: exp_topology.SetupTopology | None = None,
    experiment_runners: dict[str, Any] | None = None,
    run_control: RunControl | None = None,
    stop_loggers: bool = True,
) -> dict[str, Any]:
    setup = _setup(topology)
    stopped_devices: list[str] = []
    stopped_loggers: list[str] = []
    stopped_runners: list[str] = []

    if run_control is not None:
        run_control.request_abort()

    if experiment_runners is not None:
        for name, runner in experiment_runners.items():
            try:
                runner.stop()
                stopped_runners.append(str(name))
            except Exception:
                continue

    for node_name, node in setup.nodes.items():
        handle = node.handle
        try:
            if node.kind == "peristaltic_pump" and handle is not None:
                if hasattr(handle, "stop_variable_rpm"):
                    handle.stop_variable_rpm()
                elif hasattr(handle, "Stop"):
                    handle.Stop()
                _update_runtime_state(
                    setup,
                    node_name,
                    {"running": False, "paused": False, "current_rpm": 0, "target_rpm": 0},
                )
                stopped_devices.append(node_name)
            elif node.kind == "syringe_pump" and handle is not None:
                if hasattr(handle, "stop"):
                    handle.stop()
                _update_runtime_state(
                    setup,
                    node_name,
                    {"running": False, "target_volume_ml": 0.0},
                )
                stopped_devices.append(node_name)
            elif node.kind in {"stirrer", "magnetic_stirrer", "hotplate"} and handle is not None:
                if hasattr(handle, "stop_heating"):
                    handle.stop_heating()
                if hasattr(handle, "stop_stirring"):
                    handle.stop_stirring()
                _sync_hotplate_values(setup, node_name, handle)
                stopped_devices.append(node_name)
        except Exception:
            continue

        logger = node.metadata.get("data_logger")
        if stop_loggers and logger is not None:
            try:
                logger.stop(immediate=True)
                _update_runtime_state(
                    setup,
                    node_name,
                    {"logging_active": False, "logging_paused": False},
                )
                stopped_loggers.append(node_name)
            except Exception:
                continue

    return {
        "stopped_devices": stopped_devices,
        "stopped_loggers": stopped_loggers,
        "stopped_runners": stopped_runners,
        "abort_requested": bool(run_control is not None and run_control.abort_event.is_set()),
    }

def set_serial_rts(
    rts: bool,
    *,
    cable: str = "serial_cable",
    topology: exp_topology.SetupTopology | None = None,
) -> dict[str, Any]:
    setup = _setup(topology)
    _validate_serial_cable_node(setup, cable)
    handle = _device_handle(setup, cable)
    target_rts = bool(rts)

    if hasattr(handle, "rts"):
        setattr(handle, "rts", target_rts)
    elif hasattr(handle, "setRTS"):
        handle.setRTS(target_rts)
    else:
        raise CommandError(f"Serial cable {cable!r} handle cannot set RTS")

    return _sync_serial_cable_values(setup, cable, handle)

def read_serial_cable_values(
    cable: str = "serial_cable",
    *,
    topology: exp_topology.SetupTopology | None = None,
) -> dict[str, Any]:
    setup = _setup(topology)
    _validate_serial_cable_node(setup, cable)
    handle = _device_handle(setup, cable)
    return _sync_serial_cable_values(setup, cable, handle)


if __name__ == "__main__":
    topo = exp_topology.example_topology()
    plaen = move_liquid("water", "waste", 20, topology=topo)
    print(plaen.as_path())
    print(plaen.show_liquids())
    print(topo.nodes["water"].metadata)
    print(topo.nodes["reactor"].metadata["volume_ml"])

    cleaning(wait_seconds=5, cycles=3)

    stir_reactor(500, reactor="reactor", topology=topo)

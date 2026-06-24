from __future__ import annotations

import inspect
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import basic_commands
import exp_topology
from data_logger import DataLogger


Action = Callable[..., Any]


class RecipeError(ValueError):
    """Raised when a recipe definition or recipe execution is invalid."""

class _RecipeStopped(RuntimeError):
    """Internal control-flow signal for clean recipe stopping."""

@dataclass(frozen=True)
class RecipeStep:
    """single command/action of a bigger experiment defined in an experiment recipe dictionary.
    example: "action": "fill_reactor", "source": "reaction_solution", "ml": 50, "execute": True"""
    index: int
    action: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    label: str | None = None

    @property
    def name(self) -> str:
        return self.label or self.action

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action,
            "label": self.label,
            "kwargs": dict(self.kwargs),
        }

@dataclass(frozen=True)
class ExperimentRecipe:
    """object of a full experiment procedure consisting of individual steps."""
    name: str
    steps: tuple[RecipeStep, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "metadata": dict(self.metadata),
            "steps": [step.as_dict() for step in self.steps],
        }

# translation of human readable words into machine code
DEFAULT_ACTIONS: dict[str, Action] = {
    "move_liquid": basic_commands.move_liquid,
    "fill_reactor": basic_commands.fill_reactor,
    "empty_reactor": basic_commands.empty_reactor,
    "cleaning": basic_commands.cleaning,
    "set_valve_position": basic_commands.set_valve_position,
    "run_peristaltic_pump": basic_commands.run_peristaltic_pump,
    "stop_peristaltic_pump": basic_commands.stop_peristaltic_pump,
    "run_syringe_pump": basic_commands.run_syringe_pump,
    "stop_syringe_pump": basic_commands.stop_syringe_pump,
    "start_circulation": basic_commands.start_circulation,
    "pause_circulation": basic_commands.pause_circulation,
    "resume_circulation": basic_commands.resume_circulation,
    "stop_circulation": basic_commands.stop_circulation,
    "heat_reactor": basic_commands.heat_reactor,
    "stop_reactor_heating": basic_commands.stop_reactor_heating,
    "stir_reactor": basic_commands.stir_reactor,
    "stop_reactor_stirring": basic_commands.stop_reactor_stirring,
    "start_heating": basic_commands.start_heating,
    "stop_heating": basic_commands.stop_heating,
    "start_stirring": basic_commands.start_stirring,
    "stop_stirring": basic_commands.stop_stirring,
    "read_hotplate_values": basic_commands.read_hotplate_values,
    "start_data_logging": basic_commands.start_data_logging,
    "pause_data_logging": basic_commands.pause_data_logging,
    "resume_data_logging": basic_commands.resume_data_logging,
    "stop_data_logging": basic_commands.stop_data_logging,
    "add_log_comment": basic_commands.add_log_comment,
}

class ExperimentRunner:
    """
    Execute a readable recipe definition with pause/resume/stop control.

    Pause/resume is immediate for wait steps and takes effect between command
    steps. Long blocking hardware commands need their own pause/stop support if
    they should become interruptible mid-command.
    """

    def __init__(
        self,
        recipe: ExperimentRecipe,
        *,
        topology: exp_topology.SetupTopology,
        action_registry: dict[str, Action] | None = None,
        wait_poll_s: float = 0.2,
        run_control: basic_commands.RunControl | None = None,
    ) -> None:
        self.recipe = recipe
        self.topology = topology
        self.action_registry = dict(DEFAULT_ACTIONS)
        if action_registry is not None:
            self.action_registry.update(action_registry)

        self.wait_poll_s = float(wait_poll_s)
        if self.wait_poll_s <= 0:
            raise RecipeError("wait_poll_s must be greater than 0")
        self.run_control = run_control or basic_commands.RunControl()

        self.status = "idle"
        self.current_step_index = 0
        self.completed_steps: list[int] = []
        self.results: list[dict[str, Any]] = []
        self.last_error: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self._remaining_wait_s: float | None = None
        self._current_step_started_at: float | None = None

        self._thread: threading.Thread | None = None
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    @property
    def total_steps(self) -> int:
        return len(self.recipe.steps)

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.is_active:
                raise RecipeError("Experiment runner is already active")
            if self.status == "completed":
                raise RecipeError("Completed recipe cannot be started again; create a new runner")
            if self.status == "stopped":
                raise RecipeError("Stopped recipe cannot be restarted; create a new runner")
            if self.status == "failed":
                raise RecipeError("Failed recipe cannot be restarted; create a new runner")

            self._stop_event.clear()
            self._pause_event.set()
            self.run_control.reset_abort()
            self.status = "running"
            self.last_error = None
            if self.started_at is None:
                self.started_at = time.time()
            self._thread = threading.Thread(
                target=self._run_recipe,
                name=f"recipe_{self.recipe.name}",
                daemon=True,
            )
            self._thread.start()

    def pause(self) -> None:
        with self._lock:
            if self.status != "running":
                raise RecipeError(f"Cannot pause recipe while status is {self.status!r}")
            self.status = "paused"
            self._pause_event.clear()

    def resume(self) -> None:
        with self._lock:
            if self.status != "paused":
                raise RecipeError(f"Cannot resume recipe while status is {self.status!r}")
            self.status = "running"
            self._pause_event.set()

    def stop(self) -> None:
        with self._lock:
            if self.status in {"completed", "stopped", "failed", "idle"} and not self.is_active:
                self.status = "stopped"
                self.finished_at = time.time()
                return
            self.status = "stopped"
            self.finished_at = time.time()
            self._stop_event.set()
            self._pause_event.set()
            self.run_control.request_abort()

    def wait_until_finished(self, timeout_s: float | None = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout_s)
        return not thread.is_alive()

    def snapshot(self) -> dict[str, Any]:
        current_step = None
        if 0 <= self.current_step_index < self.total_steps:
            current_step = self.recipe.steps[self.current_step_index].as_dict()
        return {
            "recipe_name": self.recipe.name,
            "status": self.status,
            "current_step_index": self.current_step_index,
            "total_steps": self.total_steps,
            "current_step": current_step,
            "completed_steps": list(self.completed_steps),
            "last_error": self.last_error,
            "remaining_wait_s": self._remaining_wait_s,
            "capabilities": ["start", "pause", "resume", "stop"],
            "interactive_actions": _runner_interactive_actions(self.status),
        }

    def _run_recipe(self) -> None:
        try:
            while self.current_step_index < self.total_steps:
                if self._stop_event.is_set():
                    self.status = "stopped"
                    return

                self._pause_event.wait()
                if self._stop_event.is_set():
                    self.status = "stopped"
                    return

                step = self.recipe.steps[self.current_step_index]
                self._current_step_started_at = time.time()
                result = self._execute_step(step)
                self.results.append(
                    {
                        "step_index": step.index,
                        "action": step.action,
                        "label": step.label,
                        "result": result,
                    }
                )
                self.completed_steps.append(step.index)
                self.current_step_index += 1
                self._remaining_wait_s = None

            self.status = "completed"
            self.finished_at = time.time()
        except _RecipeStopped:
            self.status = "stopped"
            self.finished_at = time.time()
        except basic_commands.CommandAborted as exc:
            self.last_error = str(exc)
            self.status = "stopped"
            self.finished_at = time.time()
        except Exception as exc:
            self.last_error = str(exc)
            self.status = "failed"
            self.finished_at = time.time()
        finally:
            self._pause_event.set()

    def _execute_step(self, step: RecipeStep) -> Any:
        if step.action == "wait":
            return self._wait_step(step)

        action = self.action_registry.get(step.action)
        if action is None:
            raise RecipeError(f"Unknown recipe action: {step.action!r}")

        kwargs = dict(step.kwargs)
        signature = inspect.signature(action)
        if "topology" in signature.parameters and "topology" not in kwargs:
            kwargs["topology"] = self.topology
        if "abort_event" in signature.parameters and "abort_event" not in kwargs:
            kwargs["abort_event"] = self.run_control.abort_event
        return action(**kwargs)

    def _wait_step(self, step: RecipeStep) -> dict[str, Any]:
        duration_s = _wait_duration_seconds(step.kwargs)
        remaining = duration_s
        self._remaining_wait_s = remaining

        while remaining > 0:
            if self._stop_event.is_set() or self.run_control.abort_event.is_set():
                raise _RecipeStopped()

            self._pause_event.wait()
            if self._stop_event.is_set() or self.run_control.abort_event.is_set():
                raise _RecipeStopped()

            interval = min(self.wait_poll_s, remaining)
            time.sleep(interval)
            remaining = max(0.0, remaining - interval)
            self._remaining_wait_s = remaining

        return {"waited_s": duration_s}


def build_recipe(
    definition: dict[str, Any] | list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    name: str | None = None,
) -> ExperimentRecipe:
    """helper function to create an object from a dictionary containing the full experiment procedure"""
    if isinstance(definition, dict):
        recipe_name = str(definition.get("name", name or "experiment"))
        metadata = dict(definition.get("metadata", {}))
        steps_data = definition.get("steps", [])
    else:
        recipe_name = str(name or "experiment")
        metadata = {}
        steps_data = definition

    if not isinstance(steps_data, (list, tuple)):
        raise RecipeError("Recipe steps must be a list or tuple")

    steps: list[RecipeStep] = []
    for index, raw_step in enumerate(steps_data):
        if not isinstance(raw_step, dict):
            raise RecipeError(f"Recipe step {index} must be a dictionary")

        step_data = dict(raw_step)
        action = step_data.pop("action", None)
        if action is None:
            raise RecipeError(f"Recipe step {index} is missing an 'action'")

        label = step_data.pop("label", None)
        steps.append(
            RecipeStep(
                index=index,
                action=str(action),
                label=None if label is None else str(label),
                kwargs=step_data,
            )
        )

    return ExperimentRecipe(name=recipe_name, metadata=metadata, steps=tuple(steps))


class ExperimentQueueRunner:
    """
    Run several recipes sequentially through the same start/pause/resume/stop API.

    If logger_hotplate is provided, a fresh DataLogger is attached and started
    for each recipe, then stopped before the next recipe begins.
    """

    def __init__(
        self,
        recipes: list[ExperimentRecipe] | tuple[ExperimentRecipe, ...],
        *,
        topology: exp_topology.SetupTopology,
        name: str = "experiment_queue",
        action_registry: dict[str, Action] | None = None,
        wait_poll_s: float = 0.2,
        run_control: basic_commands.RunControl | None = None,
        logger_hotplate: str | None = None,
        log_interval_s: float = 1.0,
        hardware_refresh_s: float = 10.0,
        log_folder: str | None = None,
        logger_verbose: bool = False,
    ) -> None:
        if not recipes:
            raise RecipeError("Experiment queue needs at least one recipe")

        self.name = str(name)
        self.recipes = tuple(recipes)
        self.topology = topology
        self.action_registry = action_registry
        self.wait_poll_s = float(wait_poll_s)
        if self.wait_poll_s <= 0:
            raise RecipeError("wait_poll_s must be greater than 0")
        self.run_control = run_control or basic_commands.RunControl()
        self.logger_hotplate = logger_hotplate
        self.log_interval_s = float(log_interval_s)
        self.hardware_refresh_s = float(hardware_refresh_s)
        self.log_folder = log_folder
        self.logger_verbose = bool(logger_verbose)

        self.status = "idle"
        self.current_recipe_index = 0
        self.completed_recipes: list[str] = []
        self.results: list[dict[str, Any]] = []
        self.last_error: str | None = None
        self.current_runner: ExperimentRunner | None = None
        self.current_log_file: str | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None

        self._thread: threading.Thread | None = None
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    @property
    def total_recipes(self) -> int:
        return len(self.recipes)

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.is_active:
                raise RecipeError("Experiment queue is already active")
            if self.status in {"completed", "stopped", "failed"}:
                raise RecipeError(
                    f"Queue with status {self.status!r} cannot be restarted; create a new queue"
                )

            self._stop_event.clear()
            self._pause_event.set()
            self.run_control.reset_abort()
            self.status = "running"
            self.last_error = None
            self.started_at = time.time()
            self._thread = threading.Thread(
                target=self._run_queue,
                name=f"queue_{self.name}",
                daemon=True,
            )
            self._thread.start()

    def pause(self) -> None:
        with self._lock:
            if self.status != "running":
                raise RecipeError(f"Cannot pause queue while status is {self.status!r}")
            self.status = "paused"
            self._pause_event.clear()
            if self.current_runner is not None and self.current_runner.status == "running":
                self.current_runner.pause()

    def resume(self) -> None:
        with self._lock:
            if self.status != "paused":
                raise RecipeError(f"Cannot resume queue while status is {self.status!r}")
            self.status = "running"
            self._pause_event.set()
            if self.current_runner is not None and self.current_runner.status == "paused":
                self.current_runner.resume()

    def stop(self) -> None:
        with self._lock:
            if self.status in {"completed", "stopped", "failed", "idle"} and not self.is_active:
                self.status = "stopped"
                self.finished_at = time.time()
                return
            self.status = "stopped"
            self.finished_at = time.time()
            self._stop_event.set()
            self._pause_event.set()
            self.run_control.request_abort()
            if self.current_runner is not None:
                self.current_runner.stop()

    def wait_until_finished(self, timeout_s: float | None = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout_s)
        return not thread.is_alive()

    def snapshot(self) -> dict[str, Any]:
        current_recipe = None
        if 0 <= self.current_recipe_index < self.total_recipes:
            current_recipe = self.recipes[self.current_recipe_index].as_dict()
        runner_snapshot = (
            self.current_runner.snapshot()
            if self.current_runner is not None
            else {}
        )
        return {
            "recipe_name": self.name,
            "kind": "experiment_queue",
            "status": self.status,
            "current_recipe_index": self.current_recipe_index,
            "total_recipes": self.total_recipes,
            "current_recipe": current_recipe,
            "current_step_index": runner_snapshot.get("current_step_index", 0),
            "total_steps": runner_snapshot.get("total_steps", 0),
            "current_step": runner_snapshot.get("current_step"),
            "completed_steps": runner_snapshot.get("completed_steps", []),
            "completed_recipes": list(self.completed_recipes),
            "last_error": self.last_error,
            "remaining_wait_s": runner_snapshot.get("remaining_wait_s"),
            "current_log_file": self.current_log_file,
            "capabilities": ["start", "pause", "resume", "stop"],
            "interactive_actions": _runner_interactive_actions(self.status),
        }

    def _run_queue(self) -> None:
        try:
            for index, recipe in enumerate(self.recipes):
                if self._stop_event.is_set():
                    self.status = "stopped"
                    return

                self.current_recipe_index = index
                self._pause_event.wait()
                if self._stop_event.is_set():
                    self.status = "stopped"
                    return

                self._start_recipe_logger(recipe)
                runner = ExperimentRunner(
                    recipe,
                    topology=self.topology,
                    action_registry=self.action_registry,
                    wait_poll_s=self.wait_poll_s,
                    run_control=self.run_control,
                )
                self.current_runner = runner
                runner.start()

                while runner.is_active:
                    if self._stop_event.is_set():
                        runner.stop()
                    time.sleep(self.wait_poll_s)

                runner.wait_until_finished(1)
                self._stop_recipe_logger(recipe)
                self.results.append(
                    {
                        "recipe": recipe.name,
                        "status": runner.status,
                        "results": list(runner.results),
                        "last_error": runner.last_error,
                    }
                )

                if runner.status != "completed":
                    self.last_error = runner.last_error
                    self.status = runner.status
                    self.finished_at = time.time()
                    return

                self.completed_recipes.append(recipe.name)

            self.status = "completed"
            self.finished_at = time.time()
        except basic_commands.CommandAborted as exc:
            self.last_error = str(exc)
            self.status = "stopped"
            self.finished_at = time.time()
        except Exception as exc:
            self.last_error = str(exc)
            self.status = "failed"
            self.finished_at = time.time()
        finally:
            try:
                if self.logger_hotplate is not None:
                    basic_commands.stop_data_logging(
                        hotplate=self.logger_hotplate,
                        topology=self.topology,
                    )
            except Exception:
                pass
            self._pause_event.set()

    def _start_recipe_logger(self, recipe: ExperimentRecipe) -> None:
        if self.logger_hotplate is None:
            self.current_log_file = None
            return

        if self.logger_hotplate not in self.topology.nodes:
            raise RecipeError(f"Unknown logger hotplate node: {self.logger_hotplate!r}")
        plate = self.topology.nodes[self.logger_hotplate].handle
        if plate is None:
            raise RecipeError(f"Logger hotplate {self.logger_hotplate!r} has no handle")

        logger = DataLogger(
            plate_obj=plate,
            plate_name=f"{_safe_name(recipe.name)}_{_safe_name(self.logger_hotplate)}",
            timer=self.log_interval_s,
            hardware_refresh_s=self.hardware_refresh_s,
            save_folder=self.log_folder,
            verbose=self.logger_verbose,
        )
        basic_commands.attach_data_logger(
            logger=logger,
            hotplate=self.logger_hotplate,
            topology=self.topology,
        )
        basic_commands.start_data_logging(
            hotplate=self.logger_hotplate,
            topology=self.topology,
        )
        basic_commands.add_log_comment(
            f"{recipe.name} started",
            hotplate=self.logger_hotplate,
            topology=self.topology,
        )
        self.current_log_file = str(logger.file_name)

    def _stop_recipe_logger(self, recipe: ExperimentRecipe) -> None:
        if self.logger_hotplate is None:
            return

        try:
            basic_commands.add_log_comment(
                f"{recipe.name} finished",
                hotplate=self.logger_hotplate,
                topology=self.topology,
            )
        finally:
            basic_commands.stop_data_logging(
                hotplate=self.logger_hotplate,
                topology=self.topology,
            )


def _safe_name(value: str) -> str:
    safe = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in value)
    return safe.strip("_") or "experiment"

# example recipe 1
EXPERIMENT_1 = build_recipe(
    {
        "name": "experiment1",
        "steps": [
            {"action": "fill_reactor", "source": "liquid", "ml": 100},
            {"action": "start_circulation", "direction": "CW", "duration_hours": 4},
            {"action": "stir_reactor", "rpm": 500, "label": "stir reactor"},
            {"action": "heat_reactor", "temp": 80, "label": "heat reactor"},
            {"action": "wait", "duration_s": 2 * 60 * 60, "label": "react at 80 C"},
            {"action": "stop_reactor_heating"},
            {"action": "wait", "duration_s": 2 * 60 * 60, "label": "cool while stirring"},
            {"action": "stop_reactor_stirring"},
            {"action": "stop_circulation"},
            {"action": "cleaning"},
        ],
    }
)

# example recipe 2
EXPERIMENT_2 = build_recipe(
    {
        "name": "experiment2",
        "steps": [
            {"action": "fill_reactor", "source": "liquid", "ml": 50},
            {"action": "stir_reactor", "rpm": 500},
            {"action": "heat_reactor", "temp": 80},
            {"action": "wait", "duration_s": 3 * 60 * 60, "label": "react at 80 C"},
            {"action": "stop_reactor_heating"},
            {"action": "wait", "duration_s": 60 * 60, "label": "cool while stirring"},
            {"action": "stop_reactor_stirring"},
            {"action": "cleaning"},
        ],
    }
)


def _wait_duration_seconds(kwargs: dict[str, Any]) -> float:
    duration = kwargs.get("duration_s", kwargs.get("seconds"))
    if duration is None:
        raise RecipeError("Wait step needs 'duration_s' or 'seconds'")
    duration_s = float(duration)
    if duration_s < 0:
        raise RecipeError("Wait duration must be 0 or greater")
    return duration_s


def _runner_interactive_actions(status: str) -> list[str]:
    if status == "idle":
        return ["start"]
    if status == "running":
        return ["pause", "stop"]
    if status == "paused":
        return ["resume", "stop"]
    return []

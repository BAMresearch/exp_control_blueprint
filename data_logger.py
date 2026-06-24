from __future__ import annotations

import datetime
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable


class DataLogger:
    """
    Log magnetic stirrer state in a background thread.

    Logging cadence and hardware-read cadence are separated so the logger can
    write frequent rows while only reading the real device every
    ``hardware_refresh_s`` seconds.
    """

    def __init__(
        self,
        plate_obj: Any,
        plate_name: str = "logging_plate",
        timer: float = 1.0,
        hardware_refresh_s: float = 10.0,
        save_folder: str | os.PathLike[str] | None = None,
        cable: Any = None,
        on_update: Callable[[dict[str, Any]], None] | None = None,
        verbose: bool = False,
        debug_writer: Callable[[str], None] | None = None,
    ) -> None:
        self.plate_obj = plate_obj
        self.plate_name = plate_name
        self.name = f"{self.plate_name}_logger"
        self.timer = float(timer)
        self.hardware_refresh_s = float(hardware_refresh_s)
        self.cable = cable
        self.on_update = on_update
        self.verbose = bool(verbose)
        self.debug_writer = print if debug_writer is None else debug_writer

        if self.timer <= 0:
            raise ValueError("timer must be greater than 0")
        if self.hardware_refresh_s <= 0:
            raise ValueError("hardware_refresh_s must be greater than 0")

        self.active = False
        self.stop_event = threading.Event()
        self.resume_event = threading.Event()
        self.resume_event.set()
        self.thread: threading.Thread | None = None
        self.latest_values = self._empty_values()
        self.last_entry = ""
        self._start_time: float | None = None
        self._last_refresh_time: float | None = None
        self._write_lock = threading.Lock()

        save_root = (
            Path(save_folder)
            if save_folder is not None
            else Path(__file__).resolve().parent / "measurement_data"
        )
        save_root.mkdir(parents=True, exist_ok=True)
        self.save_folder = save_root
        self.file_name = self._next_log_file(save_root, f"{self.name}.txt")
        self.logfile: Path | None = None
        if self.cable is not None:
            self.logfile = save_root / f"triggers_{self.plate_name}.log"
            if not self.logfile.exists():
                self.logfile.write_text("date\ttime\tdigital_in\n", encoding="utf-8")

        header = (
            datetime.datetime.fromtimestamp(time.time()).strftime("%Y-%b-%d|%H:%M:%S")
            + "\n"
            + "YYYY-MM-DD|hh:mm:ss,elapsed_time(s),epoch_time,"
            + "ext_T_(C),int_T_(C),target_T_(C),ext_rpm,target_rpm,"
            + "visco_trend_(%),heating,stirring"
        )
        self.file_name.write_text(header + "\n", encoding="utf-8")

    def start(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.stop_event.clear()
            self.resume_event.set()
            self.active = True
            self.thread = threading.Thread(target=self._write_data, name=self.name, daemon=True)
            self.thread.start()

    def stop(self, *, immediate: bool = False) -> None:
        should_write_final_measurement = self.active and not immediate
        self.stop_event.set()
        self.resume_event.set()
        self.active = False
        if self.thread is not None:
            self.thread.join()
        self.thread = None
        if should_write_final_measurement:
            self._write_measurement(force_refresh=True)

    def pause(self) -> None:
        self.resume_event.clear()

    def resume(self) -> None:
        self.resume_event.set()

    @property
    def paused(self) -> bool:
        return not self.resume_event.is_set()

    def add_comment(self, comment: str = "") -> None:
        date_now = datetime.datetime.fromtimestamp(time.time()).strftime("%Y-%b-%d|%H:%M:%S")
        comment_line = f"{date_now}, COMMENT: {comment}"
        with self.file_name.open("a", encoding="utf-8") as handle:
            handle.write(comment_line + "\n")

    def _write_data(self) -> None:
        self._start_time = time.time()
        self._last_refresh_time = None
        cable_state = getattr(self.cable, "cts", None) if self.cable is not None else None

        self._refresh_values(force=True)
        while not self.stop_event.is_set():
            self.resume_event.wait()
            if self.stop_event.is_set():
                break

            now = time.time()
            self._write_measurement(now=now)

            if self.cable is not None and self.logfile is not None:
                current_state = getattr(self.cable, "cts", None)
                if current_state != cable_state:
                    digital_state = "ON" if current_state else "OFF"
                    with self.logfile.open("a", encoding="utf-8") as handle:
                        handle.write(
                            f"{datetime.datetime.fromtimestamp(now).strftime('%d/%m/%Y %H:%M:%S')}"
                            f"\t{now - self._start_time:.3f}\tDigital in change to {digital_state}\n"
                        )
                    cable_state = current_state

            self.stop_event.wait(self.timer)

    def _refresh_values(self, *, now: float | None = None, force: bool = False) -> None:
        current_time = time.time() if now is None else now
        if not force and self._last_refresh_time is not None:
            if current_time - self._last_refresh_time < self.hardware_refresh_s:
                return

        try:
            values = self._read_plate_values()
        except Exception as exc:
            self._debug(
                f"Read hiccup ({exc}). Using previous values, logging is not interrupted."
            )
            return

        self.latest_values = values
        self._last_refresh_time = current_time
        if self.on_update is not None:
            self.on_update(dict(values))

        date_now = datetime.datetime.fromtimestamp(current_time).strftime("%Y-%b-%d|%H:%M:%S")
        self._debug(
            f"{date_now}, probe_T: {values['probe_temperature']:.2f}, "
            f"int_T: {values['hotplate_sensor_temperature']:.2f}, "
            f"stir_rate: {values['stir_rate']:.2f}, "
            f"visco_trend: {values['viscosity_trend']:.2f}"
        )

    def _write_measurement(
        self,
        *,
        now: float | None = None,
        force_refresh: bool = False,
    ) -> None:
        current_time = time.time() if now is None else now
        with self._write_lock:
            self._refresh_values(now=current_time, force=force_refresh)
            line = self._format_line(current_time)
            with self.file_name.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self.last_entry = self._format_debug_entry(current_time)

    def _read_plate_values(self) -> dict[str, Any]:
        return {
            "probe_temperature": float(self.plate_obj.probe_temperature),
            "hotplate_sensor_temperature": float(self.plate_obj.hotplate_sensor_temperature),
            "target_temperature": float(self.plate_obj.target_temperature),
            "stir_rate": float(self.plate_obj.stir_rate),
            "target_stir_rate": float(self.plate_obj.target_stir_rate),
            "viscosity_trend": float(self.plate_obj.viscosity_trend),
            "heating": getattr(self.plate_obj, "heating", None),
            "stirring": getattr(self.plate_obj, "stirring", None),
        }

    def _format_line(self, now: float) -> str:
        date_now = datetime.datetime.fromtimestamp(now).strftime("%Y-%b-%d|%H:%M:%S")
        elapsed = 0.0 if self._start_time is None else now - self._start_time
        values = self.latest_values
        return (
            f"{date_now},{elapsed:.2f},{now},"
            f"{values['probe_temperature']:.2f},"
            f"{values['hotplate_sensor_temperature']:.2f},"
            f"{values['target_temperature']:.2f},"
            f"{values['stir_rate']:.2f},"
            f"{values['target_stir_rate']:.2f},"
            f"{values['viscosity_trend']:.2f},"
            f"{values['heating']},{values['stirring']}"
        ).replace(" ", "")

    def _format_debug_entry(self, now: float) -> str:
        date_now = datetime.datetime.fromtimestamp(now).strftime("%Y-%b-%d|%H:%M:%S")
        values = self.latest_values
        return (
            f"{date_now}, probe_T: {values['probe_temperature']:.2f}, "
            f"int_T: {values['hotplate_sensor_temperature']:.2f}, "
            f"stir_rate: {values['stir_rate']:.2f}, "
            f"visco_trend: {values['viscosity_trend']:.2f}"
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "logger_name": self.name,
            "plate_name": self.plate_name,
            "active": bool(self.active),
            "paused": bool(self.paused),
            "log_interval_s": float(self.timer),
            "hardware_refresh_s": float(self.hardware_refresh_s),
            "log_file": str(self.file_name),
            "verbose": bool(self.verbose),
            "last_entry": self.last_entry,
            "latest_values": dict(self.latest_values),
        }

    def _debug(self, message: str) -> None:
        if self.verbose:
            self.debug_writer(message)

    @staticmethod
    def _next_log_file(folder: Path, filename: str) -> Path:
        base_path = folder / filename
        if not base_path.exists():
            return base_path

        stem = base_path.stem
        suffix = base_path.suffix
        copy_number = 1
        while True:
            candidate = folder / f"{stem}_copy{copy_number}{suffix}"
            if not candidate.exists():
                return candidate
            copy_number += 1

    @staticmethod
    def _empty_values() -> dict[str, Any]:
        return {
            "probe_temperature": 0.0,
            "hotplate_sensor_temperature": 0.0,
            "target_temperature": 0.0,
            "stir_rate": 0.0,
            "target_stir_rate": 0.0,
            "viscosity_trend": 0.0,
            "heating": None,
            "stirring": None,
        }

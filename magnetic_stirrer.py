import math
import time

import serial

class IkaLabDevice:
    NAMUR = {
        'IN_PV': 'IN_PV_{x}',
        'IN_SP': 'IN_SP_{x}',
        'OUT_SP': 'OUT_SP_{x} {val}',
        'START': 'START_{x}',
        'STOP': 'STOP_{x}',
    }
    HEATER_CHANNEL = 1
    HOTPLATE_SENSOR_CHANNEL = 2
    VISCOSITY_TREND_CHANNEL = 5
    STIRRER_CHANNEL = 4
    READ_ERROR_VALUE = -999.0

    def __init__(self, port, name = "IKA_hotplate"):
        self.ser = serial.Serial(port, 9600, timeout=0.5, parity=serial.PARITY_EVEN, bytesize=7, stopbits=1)
        self.name = name
        self._heating = False
        self._stirring = False

    def get_value(self, channel):
        self.ser.write((self.NAMUR['IN_PV'].format(x=channel) + "\r\n").encode())
        try:
            resp = self.ser.read_until(b'\r').decode().strip()
            return float(''.join(c for c in resp if c.isdigit() or c == '.' or c == '-'))
        except: return -999.0

    def get_target(self, channel):
        self.ser.write((self.NAMUR['IN_SP'].format(x=channel) + "\r\n").encode())
        try:
            resp = self.ser.read_until(b'\r').decode().strip()
            return float(''.join(c for c in resp if c.isdigit() or c == '.' or c == '-'))
        except: return -999.0

    def set_target(self, channel, val):
        self.ser.write((self.NAMUR['OUT_SP'].format(x=channel, val=val) + "\r\n").encode())

    def set_state(self, func, state):
        cmd = self.NAMUR['START' if state else 'STOP'].format(x=func)
        self.ser.write((cmd + "\r\n").encode())

    def start_heating(self, temp):
        self.set_target(self.HEATER_CHANNEL, temp)
        self.set_state(self.HEATER_CHANNEL, True)
        self._heating = True

    def stop_heating(self):
        self.set_state(self.HEATER_CHANNEL, False)
        self._heating = False

    def start_stirring(self, rpm):
        self.set_target(self.STIRRER_CHANNEL, rpm)
        self.set_state(self.STIRRER_CHANNEL, True)
        self._stirring = True

    def stop_stirring(self):
        self.set_state(self.STIRRER_CHANNEL, False)
        self._stirring = False

    def _wait_for_value(
        self,
        value_reader,
        target,
        tolerance,
        timeout,
        poll_interval,
        value_name,
        start_action,
    ):
        """Poll a measured value until it is within the requested target range."""
        target = float(target)
        tolerance = float(tolerance)
        poll_interval = float(poll_interval)

        if not math.isfinite(target):
            raise ValueError(f"Target {value_name} must be a finite number")
        if not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError("Tolerance must be a finite number greater than or equal to 0")
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("Poll interval must be a finite number greater than 0")
        if timeout is not None:
            timeout = float(timeout)
            if not math.isfinite(timeout) or timeout < 0:
                raise ValueError("Timeout must be None or a finite number greater than or equal to 0")

        start_action()
        started_at = time.monotonic()
        last_value = self.READ_ERROR_VALUE

        while True:
            current_value = float(value_reader())
            if math.isfinite(current_value) and current_value != self.READ_ERROR_VALUE:
                last_value = current_value
                if abs(current_value - target) <= tolerance:
                    return current_value

            elapsed = time.monotonic() - started_at
            if timeout is not None and elapsed >= timeout:
                raise TimeoutError(
                    f"{self.name}: target {value_name} {target} was not reached "
                    f"within {timeout:g} s (last valid value: {last_value})"
                )

            sleep_time = poll_interval
            if timeout is not None:
                sleep_time = min(sleep_time, max(0.0, timeout - elapsed))
            time.sleep(sleep_time)

    def wait_for_temperature(
        self,
        target_temp=0,
        tolerance=0,
        timeout=1800,
        poll_interval=2,
    ):
        """Start heating and block until the probe reaches the target temperature."""
        return self._wait_for_value(
            lambda: self.probe_temperature,
            target_temp,
            tolerance,
            timeout,
            poll_interval,
            "temperature",
            lambda: self.start_heating(target_temp),
        )

    def wait_for_stir(
        self,
        target_rpm=None,
        tolerance=0,
        timeout=30,
        poll_interval=1,
    ):
        """Start stirring and block until the measured speed reaches the target RPM."""
        if target_rpm is None:
            raise ValueError("Target RPM must be specified")
        return self._wait_for_value(
            lambda: self.stir_rate,
            target_rpm,
            tolerance,
            timeout,
            poll_interval,
            "stir rate",
            lambda: self.start_stirring(target_rpm),
        )

    @property
    def probe_temperature(self):
        return self.get_value(self.HEATER_CHANNEL)

    @property
    def hotplate_sensor_temperature(self):
        return self.get_value(self.HOTPLATE_SENSOR_CHANNEL)

    @property
    def target_temperature(self):
        return self.get_target(self.HEATER_CHANNEL)

    @property
    def stir_rate(self):
        return self.get_value(self.STIRRER_CHANNEL)

    @property
    def target_stir_rate(self):
        return self.get_target(self.STIRRER_CHANNEL)

    @property
    def viscosity_trend(self):
        return self.get_value(self.VISCOSITY_TREND_CHANNEL)

    @property
    def heating(self):
        return self._heating

    @property
    def stirring(self):
        return self._stirring

    def close(self):
        self.ser.close()

    def disconnect(self):
        self.close()

class simIkaLabDevice:
    HEATER_CHANNEL = IkaLabDevice.HEATER_CHANNEL
    HOTPLATE_SENSOR_CHANNEL = IkaLabDevice.HOTPLATE_SENSOR_CHANNEL
    VISCOSITY_TREND_CHANNEL = IkaLabDevice.VISCOSITY_TREND_CHANNEL
    STIRRER_CHANNEL = IkaLabDevice.STIRRER_CHANNEL

    def __init__(
        self,
        name="simIKA_hotplate",
        initial_temperature=20.0,
        initial_stir_rate=0.0,
        viscosity_trend=0.0,
    ):
        self.name = name
        self._probe_temperature = float(initial_temperature)
        self._hotplate_sensor_temperature = float(initial_temperature)
        self._target_temperature = float(initial_temperature)
        self._stir_rate = float(initial_stir_rate)
        self._target_stir_rate = float(initial_stir_rate)
        self._viscosity_trend = float(viscosity_trend)
        self._heating = False
        self._stirring = initial_stir_rate > 0
        self.command_history = []

    def get_value(self, channel):
        if channel == self.HEATER_CHANNEL:
            return self._probe_temperature
        if channel == self.HOTPLATE_SENSOR_CHANNEL:
            return self._hotplate_sensor_temperature
        if channel == self.STIRRER_CHANNEL:
            return self._stir_rate
        if channel == self.VISCOSITY_TREND_CHANNEL:
            return self._viscosity_trend
        return -999.0

    def get_target(self, channel):
        if channel == self.HEATER_CHANNEL:
            return self._target_temperature
        if channel == self.STIRRER_CHANNEL:
            return self._target_stir_rate
        return -999.0

    def set_target(self, channel, val):
        target = float(val)
        if channel == self.HEATER_CHANNEL:
            self._target_temperature = target
            self._probe_temperature = target
            self._hotplate_sensor_temperature = target
        elif channel == self.STIRRER_CHANNEL:
            self._target_stir_rate = target
            if self._stirring:
                self._stir_rate = target
        else:
            raise ValueError(f"Unsupported IKA channel: {channel}")
        self.command_history.append(("OUT_SP", channel, target))

    def set_state(self, func, state):
        target_state = bool(state)
        if func == self.HEATER_CHANNEL:
            self._heating = target_state
        elif func == self.STIRRER_CHANNEL:
            self._stirring = target_state
            self._stir_rate = self._target_stir_rate if target_state else 0.0
        else:
            raise ValueError(f"Unsupported IKA function: {func}")
        self.command_history.append(("START" if target_state else "STOP", func))

    def start_heating(self, temp):
        self.set_target(self.HEATER_CHANNEL, temp)
        self.set_state(self.HEATER_CHANNEL, True)

    def stop_heating(self):
        self.set_state(self.HEATER_CHANNEL, False)

    def start_stirring(self, rpm):
        target_rpm = int(rpm)
        if target_rpm <= 0:
            raise ValueError("Stirring rpm must be greater than 0")
        self.set_target(self.STIRRER_CHANNEL, target_rpm)
        self.set_state(self.STIRRER_CHANNEL, True)

    def stop_stirring(self):
        self.set_state(self.STIRRER_CHANNEL, False)

    def wait_for_temperature(
        self,
        target_temp=0,
        tolerance=0,
        timeout=180,
        poll_interval=2,
    ):
        self.start_heating(target_temp)
        return self.probe_temperature

    def wait_for_stir(
        self,
        target_rpm=None,
        tolerance=0,
        timeout=30,
        poll_interval=1,
    ):
        if target_rpm is None:
            raise ValueError("Target RPM must be specified")
        self.start_stirring(int(target_rpm))
        return self.stir_rate

    def wait_until_temperature_stable(self, time_out=1800):
        return True

    @property
    def probe_temperature(self):
        return self.get_value(self.HEATER_CHANNEL)

    @property
    def hotplate_sensor_temperature(self):
        return self.get_value(self.HOTPLATE_SENSOR_CHANNEL)

    @property
    def target_temperature(self):
        return self.get_target(self.HEATER_CHANNEL)

    @property
    def stir_rate(self):
        return self.get_value(self.STIRRER_CHANNEL)

    @property
    def target_stir_rate(self):
        return self.get_target(self.STIRRER_CHANNEL)

    @property
    def viscosity_trend(self):
        return self.get_value(self.VISCOSITY_TREND_CHANNEL)

    @property
    def heating(self):
        return self._heating

    @property
    def stirring(self):
        return self._stirring

    def close(self):
        self.command_history.append(("CLOSE",))

    def disconnect(self):
        self.close()

def main():
    hotplate = IkaLabDevice("COM3", "testplate")
    input("start stirring")
    hotplate.start_stirring(220)
    input("stop stirring")
    hotplate.stop_stirring()
    input("start heaing")
    hotplate.start_heating(20)
    input("stop heating")
    hotplate.stop_heating()
    datenliste = [hotplate.probe_temperature, hotplate.hotplate_sensor_temperature,
                  hotplate.heating, hotplate.stirring,
                  hotplate.viscosity_trend]
    print(datenliste)

if __name__ == "__main__":
    main()

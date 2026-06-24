'''
    LONGER class by Bofeng
    reduced and optimized by T. W. Ryll
'''

import serial
import time
from binascii import unhexlify
import threading
import time
import sys
import glob
import codecs

"""
!!"Factory functions" shall only be called internally by the script itself and might break functionalities otherwise!!
"""

STRGLO=""
BOOL=True

leng_WJ = int('0x06',16)

W = int('0x57',16)
J = int('0x4a',16)

PH_0 = int('0x00',16) # Placeholder 0x00

CW = int('0x01',16)
CCW = int('0x00',16)
ON = int('0x01',16)
OFF = int('0x00',16)


def get_serial_ports():
    """ Lists serial port names
        :raises EnvironmentError:
            On unsupported or unknown platforms
        :returns:
            A list of the serial ports available on the system
    """
    if sys.platform.startswith('win'):
        ports = ['COM%s' % (i + 1) for i in range(256)]
    elif sys.platform.startswith('linux') or sys.platform.startswith('cygwin'):
        # this excludes your current terminal "/dev/tty"
        ports = glob.glob('/dev/tty[A-Za-z]*')
    elif sys.platform.startswith('darwin'):
        ports = glob.glob('/dev/tty.*')
    else:
        raise EnvironmentError('Unsupported platform')

    serial_ports = []
    for port in ports:
        try:
            s = serial.Serial(port)
            s.close()
            serial_ports.append(port)
        except (OSError, serial.SerialException):
            pass
    return serial_ports

def OpenPort(portx, bps=1200, timeout=0):
    """
    try to open local serial port with specific name
    
    Args:
        portx (str): port where a physical device is connected to
        bps (int): bits per second
        timeout (int): seconds until timeout
    """
    ret = False
    try:
        # open serial port and return
        ser = serial.Serial(portx, bps, timeout=timeout,
                            parity=serial.PARITY_EVEN,
                            stopbits=serial.STOPBITS_ONE,
                            bytesize=serial.EIGHTBITS,
                            xonxoff=True,
                            dsrdtr=False)
        # return boolean to show wether opened
        if (ser.is_open):
            ret = True
            reader = threading.Thread(target=ReadData, args=(ser,))
            #reader.start() # TODO readdata working now but this is blocking everything
            #ReadData(ser)
    except Exception as error:
        print("can't open the port", error)
    return ser, ret

def OpenPort_S():
    """loop local serial ports and open them, return a list contains all the opened ports"""
    Ports_list_opened = []
    ports_list = get_serial_ports()
    print(f'Find {len(ports_list)} ports, try to open these ports')
    i = 0
    for port in ports_list:
        ser, ret = OpenPort(port, bps=1200, timeout=1)
        Ports_list_opened.append(ser)
        print(f'Find Port: {ser.name} now is open: {ret}')
        i = i + 1
    return Ports_list_opened

def ReadData(ser, wait_time=5):
    """
    not so useful here. may have to run in threading.
    function is reading some bytes and tries to print them - failed every time as off 06. may 24
    """
    global STRGLO, BOOL
    t = 0  # set a time_var to read data after sending
    while BOOL:
        if ser.in_waiting:
            # TODO something about hex decoding is not working #
            raw_data = ser.read(ser.in_waiting)  # Read and decode the data into a string
            STRGLO = codecs.decode(raw_data.hex(), "hex")
            '''print('moin', STRGLO)

            if len(raw_data) % 2 != 0:  # Check if the length is odd
                raw_data = '0' + raw_data  # Add a leading zero to make it even-length

            STRGLO = codecs.decode(raw_data, "hex")  # Decode the hexadecimal string
            print('moin', STRGLO)
            #STRGLO = codecs.decode('0' + STRGLO, 'hex_codec')'''
            decode_hex = codecs.getdecoder("hex_codec")
            STRGLO = decode_hex(ser.read(ser.in_waiting))
            time.sleep(1)
            t = t + 1
            if t > (wait_time - 1):
                return STRGLO


class PUMP:
    """
    creation of a longer-pump object
    
    Args:
        serial (serial): a serial object of an opened port where the pump is connected to
        Addr (int): must be '1' but will be used for correct addressing of the device. check manufacturer details if necessary
        Speed (int): default operation rpm for this pump
        CW (boolean): spin the pump CW (True) or CCW (False)
        ON (boolean): indicator if device is in operation or not
        name (str): device name for references
    """
    def __init__(self, serial, Addr=1, Speed=0, CW=True, ON=False, name="Longer-Pump"):
        self.serial = serial  # serial class
        self.Speed = Speed  # int
        self.Addr = Addr  # int
        self.CW = CW
        self.ON = ON
        self.name = name
        # variables for timed execution of commands
        self.thread = None
        self.stop_event = threading.Event()
        self.resume_event = threading.Event()
        self.resume_event.set()  # Initially set to allow actions
        self.variable_rpm_worker = None
        self.variable_rpm_stop_event = threading.Event()
        self.variable_rpm_resume_event = threading.Event()
        self.variable_rpm_resume_event.set()
        self.variable_rpm_speed = None
        self.variable_rpm_direction = self.CW

    @property
    def variable_rpm_active(self):
        return self.variable_rpm_worker is not None and self.variable_rpm_worker.is_alive()

    @property
    def variable_rpm_paused(self):
        return self.variable_rpm_active and not self.variable_rpm_resume_event.is_set()

    def start_thread(self, timer="Keep"):
        """
        helper function for executing commands as threads.
        starts the pumping for a given timer

        Args:
            timer (int/str): seconds for how long the pump shall operate OR 'Keep' for infinite time
        """
        if self.thread is None or not self.thread.is_alive():
            self.stop_event.clear()
            self.resume_event.set()  # Ensure the resume event is set
            self.thread = threading.Thread(target=self.pump_thread, kwargs={"timer":timer})
            self.thread.start()
        elif self.thread.is_alive():
            self.stop_event.clear()
            self.resume_event.set()

    def stop_thread(self):
        """
        helper function for executing commands as threads.
        stops the pump indefintely
        """
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()

    def pause_thread(self):
        """
        helper function for executing commands as threads.
        pause pumping, can be continued anytime
        """
        self.resume_event.clear()

    def resume_thread(self):
        """
        helper function for executing commands as threads.
        resume pumping after a pause
        """
        self.resume_event.set()

    def pump_thread(self, timer="Keep"):
        """
        Factory function: actual activation of pumping, has to be run in a thread to fulfill its purpose

        Args:
            timer (int/str): seconds of operation OR 'Keep' for infinite operation
        """
        while not self.stop_event.is_set():
            self.resume_event.wait()  # Wait until execution is resumed
            ####actual code execution happens here####
            if type(timer) == int or type(timer) == float:
                self.Set_speed() # TODO hier fehlt alles
                remaining_time = timer  # Total timer duration in seconds
                runner = False
            elif timer == "Keep":
                self.Set_speed()
                remaining_time = 0
                runner = True # boolean to keep command active
            else:
                print("timer has wrong input. only numbers and 'Keep' are allowed")
                raise ValueError

            interval = 1  # Check pause/resume every 1 second
            
            while runner or remaining_time > 0:
                #print(f"into ze loop {remaining_time}")
                if self.stop_event.is_set():  # Stop if the stop_event is set
                    ####stop code execution entirely####cmd
                    self.Stop()
                    break # return here to end and exit whole function
                if not self.resume_event.is_set():  # Pause if the resume_event is cleared
                    ####pause code execution####
                    self.Stop()
                    self.resume_event.wait() # program will sit here until "resume.event.set()" is called
                    self.Set_speed()
                time.sleep(interval)  # Sleep for a short interval
                remaining_time -= interval  # Reduce the remaining time
            if not runner and remaining_time <= 0:
                self.Stop()
                break   # end execution if both variables are expired -> prevent loops

    def PDU_FCX(self, PDU, Speed):
        """
        Factory function: decodes commands into hex format for communication with a pump
        """
        fcx = 0
        if Speed == None:
            PDU_ = PDU[:]
            for byte in PDU_:
                fcx = fcx ^ byte
            return hex(fcx)

        if Speed < 256:
            PDU_ = PDU[:]
            PDU_.insert(5, Speed)
            for byte in PDU_:
                fcx = fcx ^ byte
            return hex(fcx)

        else:
            SPD_1 = '0' + hex(Speed)[2]
            SPD_2 = hex(Speed)[3:5]
            SPD_1 = int(SPD_1, 16)
            SPD_2 = int(SPD_2, 16)
            PDU.copy().pop(5)
            PDU.pop(4)
            for byte in PDU:
                fcx = fcx ^ byte
            fcx = fcx ^ SPD_1 ^ SPD_2
            return hex(fcx)

    def get_speed_hex(self, Speed):
        """
        Factory function: decodes speed/rpm into hex format for communication with a pump
        """
        if Speed > 255:
            SPD_1 = '0x0' + hex(Speed)[2]
            SPD_2 = '0x' + hex(Speed)[3:5]
            hex_spd = [SPD_1, SPD_2]

        elif Speed == 232:
            hex_spd = [hex(Speed), hex(PH_0).replace('x', 'x0')]
        elif Speed < 16:
            hex_spd = [hex(Speed).replace('x', 'x0')]
        else:
            hex_spd = hex(Speed)
        return hex_spd

    def get_full_command(self, *PDU, FCX, Speed, hex_spd):
        """
        Factroy function: reading the command and get CRC-Byte
        """
        bf_btye = ['0xe9']  # E9 as command_flag. No need to change but only works for LONGER PUMP
        if Speed == None and hex_spd == None:
            for byte in PDU:
                if len(hex(byte)) < 4:
                    hex_byte = hex(byte)
                    hex_byte = hex_byte.replace('0x', '0x0')
                    bf_btye.append(hex_byte)

                else:
                    hex_byte = hex(byte)
                    bf_btye.append(hex_byte)

            if len(FCX) < 4:  # special situation for CRC only in 1 Byte
                FCX = FCX.replace('0x', '0x0')

            bf_btye.append(FCX)

            command_str = str(bf_btye)
            command_str = command_str.replace('[', '').replace(']', '')
            command_str = command_str.replace('0x', '').replace(', ', '').replace("''", '').replace("'", '')
            return command_str

        if Speed > 255:  # speed_hex will be 3 byte after set more than 255, these 3 byte have to devided in to 2byte + 2byte
            for byte in PDU:
                if len(hex(byte)) < 4:
                    hex_byte = hex(byte)
                    hex_byte = hex_byte.replace('0x', '0x0')
                    bf_btye.append(hex_byte)

                else:
                    hex_byte = hex(byte)
                    bf_btye.append(hex_byte)

            if len(FCX) < 4:
                FCX = FCX.replace('0x', '0x0')

            bf_btye.append(FCX)
            bf_btye.insert(5, hex_spd)

            command_str = str(bf_btye)
            command_str = command_str.replace('[', '').replace(']', '')
            command_str = command_str.replace('0x', '').replace(', ', '').replace("''", '').replace("'", '')

        else:  # normal situation here
            for byte in PDU:
                if len(hex(byte)) < 4:
                    hex_byte = hex(byte)
                    hex_byte = hex_byte.replace('0x', '0x0')
                    bf_btye.append(hex_byte)

                else:
                    hex_byte = hex(byte)
                    bf_btye.append(hex_byte)

            if len(FCX) < 4:
                FCX = FCX.replace('0x', '0x0')

            bf_btye.append(FCX)
            bf_btye.insert(6, hex_spd)

            command_str = str(bf_btye)
            command_str = command_str.replace('[', '').replace(']', '')
            command_str = command_str.replace('0x', '').replace(', ', '').replace("''", '').replace("'", '')
        return command_str

    def disconnect(self):
        """properly disconnect a pump software-sided"""
        self.stop_variable_rpm()
        self.Stop()
        global BOOL
        BOOL = False
        self.serial.close()

    def Stop(self):
        """stops the pump's current operation"""
        Addr = self.Addr
        CW = self.CW
        Speed = 0 #int(self.Speed) * 10 # TODO can this be 0 instead of specific value?
        
        PDU = [Addr, leng_WJ, W, J, PH_0, OFF, CW]
        hex_spd = self.get_speed_hex(Speed)
        FCX = self.PDU_FCX(PDU, Speed)
        cmd = self.get_full_command(*PDU, FCX=FCX, Speed=Speed, hex_spd=hex_spd)
        self.serial.write(unhexlify(cmd))
        self.ON = False
        # print(f"pumpe ist stopped, ON: {self.ON}")
    

    def Set_speed(self, Duration="Keep", Speed=50, ON=True, CW=True):
        """
        transmit rpm, direction and duration to a pump and start operation

        Args:
            Duration (int/str): seconds for operation OR 'Keep' for infinite operation
            Speed (int): rpm from 1 to 999
            ON (boolean): reference flag for if a pump is in operation
            CW (boolean): spin the pump CW (True) or CCW (False)
        """
        if Speed is not None:
            self.Speed = Speed
        else:
            Speed = 0 #int(self.Speed * 10) # Speed = int(self.Speed * 10)
        if Speed > 999 or Speed < 1:
            raise Warning('invalid Speed, valid Speed from 1 -> 999 rpm')
        
        if ON is not None:
            self.ON = ON
        else:
            ON = self.ON
        if CW is not None:
            self.CW = CW
        else:
            CW = self.CW
        Addr = self.Addr
        OFF = int('0x00',16) # dont know why func cant read this hex_code from begnning so assign again here

        if ON == True and CW == True:
            PDU = [Addr, leng_WJ, W, J, PH_0, ON, CW]
        elif ON == False and CW == False:
            PDU = [Addr, leng_WJ, W, J, PH_0, OFF, CCW]
        elif ON == True and CW == False:
            PDU = [Addr, leng_WJ, W, J, PH_0, ON, CCW]
        else:
            PDU = [Addr, leng_WJ, W, J, PH_0, OFF, CW]
        
        hex_spd = self.get_speed_hex(Speed)
        FCX = self.PDU_FCX(PDU, Speed)
        cmd = self.get_full_command(*PDU, FCX=FCX, Speed=Speed, hex_spd=hex_spd)
        
        if str(Duration) == "Keep": # sends command once and goes along in code
            #print("Duration == 'Keep'")
            self.serial.write(unhexlify(cmd))
            #print(f'pump Nr. {Addr} is running through serial port {self.serial.name}')
            
        else: # PDU[-1] to control direction PDU[-2] to control switch.
              # setting different conditions manually to confirm that it will work
            if PDU[-1] == True and PDU[-2] == False: 
                self.serial.write(unhexlify(cmd))
                time.sleep(Duration)
                ON = int('0x01',16)
                PDU = [Addr, leng_WJ, W, J, PH_0, ON, CW]
                FCX = self.PDU_FCX(PDU, Speed)
                cmd_ = self.get_full_command(*PDU, FCX=FCX, Speed=Speed, hex_spd=hex_spd)
                self.serial.write(unhexlify(cmd_))
                self.ON = False
                
            elif PDU[-1] == True and PDU[-2] == True:
                self.serial.write(unhexlify(cmd))
                time.sleep(Duration)
                OFF = int('0x00',16)
                PDU = [Addr, leng_WJ, W, J, PH_0, OFF, CW]
                FCX = self.PDU_FCX(PDU, Speed)
                cmd_ = self.get_full_command(*PDU, FCX=FCX, Speed=Speed, hex_spd=hex_spd)
                self.serial.write(unhexlify(cmd_))
                self.ON = False
                
            elif PDU[-1] == False and PDU[-2] == False:
                self.serial.write(unhexlify(cmd))
                time.sleep(Duration)
                ON = int('0x01',16)
                PDU = [Addr, leng_WJ, W, J, PH_0, ON, CW]
                FCX = self.PDU_FCX(PDU, Speed)
                cmd_ = self.get_full_command(*PDU, FCX=FCX, Speed=Speed, hex_spd=hex_spd)
                self.serial.write(unhexlify(cmd_))
                self.ON = False
                
            elif PDU[-1] == False and PDU[-2] == True:
                self.serial.write(unhexlify(cmd))
                time.sleep(Duration)
                OFF = int('0x00',16)
                PDU = [Addr, leng_WJ, W, J, PH_0, OFF, CW]
                FCX = self.PDU_FCX(PDU, Speed)
                cmd_ = self.get_full_command(*PDU, FCX=FCX, Speed=Speed, hex_spd=hex_spd)
                self.serial.write(unhexlify(cmd_))
                self.ON = False
            else:
                return 0
    
    def Set_speed_thread(self, Duration="Keep", Speed=None, ON=None, CW=None):
        """
        same functionality as 'Set_speed' but ran as a thread to not interrupt/pause other commands in operation
        """
        threading.Thread(target=PUMP.Set_speed(Duration, Speed, ON, CW)).start()
    
    def variable_rpm_thread(self, direction=True, timer=1/60, time_step_long=60, time_step_short=10, speed_max=999,
                            speed_min=499, speed_step=100):
        if self.variable_rpm_worker is not None and self.variable_rpm_worker.is_alive():
            self.stop_variable_rpm()

        self.variable_rpm_stop_event.clear()
        self.variable_rpm_resume_event.set()
        self.variable_rpm_worker = threading.Thread(
            target=self.variable_rpm,
            kwargs={
                "direction": direction,
                "timer": timer,
                "time_step_long": time_step_long,
                "time_step_short": time_step_short,
                "speed_max": speed_max,
                "speed_min": speed_min,
                "speed_step": speed_step,
            },
        )
        self.variable_rpm_worker.start()

    def pause_variable_rpm(self):
        self.variable_rpm_resume_event.clear()
        self.Stop()

    def resume_variable_rpm(self):
        self.variable_rpm_resume_event.set()

    def stop_variable_rpm(self):
        self.variable_rpm_stop_event.set()
        self.variable_rpm_resume_event.set()
        self.Stop()
        if self.variable_rpm_worker is not None and self.variable_rpm_worker.is_alive():
            self.variable_rpm_worker.join()
        self.variable_rpm_worker = None
        self.variable_rpm_speed = None

    def _wait_with_variable_rpm_controls(self, seconds):
        end_time = time.time() + seconds
        while time.time() < end_time:
            if self.variable_rpm_stop_event.is_set():
                self.Stop()
                return False
            if not self.variable_rpm_resume_event.is_set():
                self.Stop()
                self.variable_rpm_resume_event.wait()
                if self.variable_rpm_stop_event.is_set():
                    self.Stop()
                    return False
                if self.variable_rpm_speed is not None:
                    self.Set_speed(Duration='Keep', Speed=self.variable_rpm_speed, CW=self.variable_rpm_direction)
            time.sleep(min(0.2, max(end_time - time.time(), 0)))
        return not self.variable_rpm_stop_event.is_set()

    def variable_rpm(self, direction=True, timer=1/60, time_step_long=60, time_step_short=10,
                    speed_max=999, speed_min=499, speed_step=100):
        """
        Makes the pump oscillate between given RPM values over a given time span.

        Args:
            direction (bool): Pump direction (True = CW, False = CCW)
            timer (float): Total duration in hours
            time_step (float): Time in seconds between speed adjustments
            speed_max (int): Maximum RPM
            speed_min (int): Minimum RPM
            speed_step (int): Step size in RPM
        """
        start_time = time.time()
        elapsed = 0
        action_speed = speed_max

        try:
            while elapsed < timer * 3600 and not self.variable_rpm_stop_event.is_set():
            # long duration of highest speed
                self.variable_rpm_speed = action_speed
                self.variable_rpm_direction = direction
                self.Set_speed(Duration='Keep', Speed=action_speed, CW=direction)
                elapsed = time.time() - start_time
                if not self._wait_with_variable_rpm_controls(time_step_long):
                    return
                elapsed = time.time() - start_time
                if elapsed >= timer * 3600:
                    return
                action_speed -= speed_step
                # decreasing speed in short time
                while action_speed > speed_min and not self.variable_rpm_stop_event.is_set():
                    self.variable_rpm_speed = action_speed
                    self.variable_rpm_direction = direction
                    self.Set_speed(Duration='Keep', Speed=action_speed, CW=direction)
                    if not self._wait_with_variable_rpm_controls(time_step_short):
                        return
                    elapsed = time.time() - start_time
                    if elapsed >= timer * 3600:
                        return
                    action_speed -= speed_step
                # increasing speed in short time
                while action_speed < speed_max and not self.variable_rpm_stop_event.is_set():
                    self.variable_rpm_speed = action_speed
                    self.variable_rpm_direction = direction
                    self.Set_speed(Duration='Keep', Speed=action_speed, CW=direction)
                    if not self._wait_with_variable_rpm_controls(time_step_short):
                        return
                    elapsed = time.time() - start_time
                    if elapsed >= timer * 3600:
                        return
                    action_speed += speed_step
                action_speed = speed_max
        finally:
            self.Stop()
            self.variable_rpm_worker = None
            self.variable_rpm_speed = None

    def back_and_forth(self, timer=1/60, time_step=60, Speed=999):
        """
        Makes the pump change direction every given time_step in s.
        set speed manually to 111 to stop operation

        Args:
            timer (float): Total duration in hours
            time_step (float): Time in seconds between speed adjustments
            Speed (int): RPM
        """
        start_time = time.time()
        elapsed = 0

        while elapsed < timer * 3600:
            self.Set_speed(Duration=time_step, Speed=Speed, CW=True)
            elapsed = time.time() - start_time
            while not self.ON:
                    time.sleep(5)
            if self.Speed == 111 or (elapsed >= timer * 3600):
                return
            
            self.Set_speed(Duration=time_step, Speed=Speed, CW=False)
            while not self.ON:
                    time.sleep(5)
            if self.Speed == 111 or (elapsed >= timer * 3600):
                return
            
    # TODO set_volume: exact measurement for CW and CCW needed
    # feed as values to function and calculate pumping parameters

class simPUMP:
    def __init__(self, name):
        self.name = name
        self.ON = False
        self.Speed = 0
        self.CW = True
        self.CCW = False
        self.variable_rpm_worker = None
        self.variable_rpm_stop_event = threading.Event()
        self.variable_rpm_resume_event = threading.Event()
        self.variable_rpm_resume_event.set()
        self.variable_rpm_speed = None
        self.variable_rpm_direction = self.CW

    @property
    def variable_rpm_active(self):
        return self.variable_rpm_worker is not None and self.variable_rpm_worker.is_alive()

    @property
    def variable_rpm_paused(self):
        return self.variable_rpm_active and not self.variable_rpm_resume_event.is_set()

    def disconnect(self):
        self.stop_variable_rpm()
        self.Stop()
        print(f"Pump {self.name} disconnected!")

    def Stop(self):
        self.Speed = 0
        self.ON = False
    
    def Set_speed(self, Duration="Keep", Speed=None, ON=True, CW=True):
        if Speed is not None:
            self.Speed = Speed
        else:
            Speed = 0 #int(self.Speed * 10) # Speed = int(self.Speed * 10)
        if Speed > 999 or Speed < 1:
            raise Warning('invalid Speed, valid Speed from 1 -> 999 rpm')
        
        if ON is not None:
            self.ON = ON
        else:
            ON = self.ON
        if CW is not None:
            self.CW = CW
        else:
            CW = self.CW
        print(f"pumping at {Speed} rpm for: {Duration}")
        if Duration != "Keep":
            #time.sleep(Duration) # Duration
            time.sleep(5)
            self.ON = False

    def variable_rpm_thread(self, direction, timer, time_step_long, time_step_short, speed_max, speed_min, speed_step):
        if self.variable_rpm_worker is not None and self.variable_rpm_worker.is_alive():
            self.stop_variable_rpm()

        self.variable_rpm_stop_event.clear()
        self.variable_rpm_resume_event.set()
        self.variable_rpm_worker = threading.Thread(target=self.variable_rpm, kwargs={
        "direction": direction, "timer": timer, "time_step_long": time_step_long, "time_step_short": time_step_short, "speed_max": speed_max,
        "speed_min": speed_min, "speed_step": speed_step})
        self.variable_rpm_worker.start()

    def pause_variable_rpm(self):
        self.variable_rpm_resume_event.clear()
        self.Stop()

    def resume_variable_rpm(self):
        self.variable_rpm_resume_event.set()

    def stop_variable_rpm(self):
        self.variable_rpm_stop_event.set()
        self.variable_rpm_resume_event.set()
        self.Stop()
        if self.variable_rpm_worker is not None and self.variable_rpm_worker.is_alive():
            self.variable_rpm_worker.join()
        self.variable_rpm_worker = None
        self.variable_rpm_speed = None

    def _wait_with_variable_rpm_controls(self, seconds):
        end_time = time.time() + seconds
        while time.time() < end_time:
            if self.variable_rpm_stop_event.is_set():
                self.Stop()
                return False
            if not self.variable_rpm_resume_event.is_set():
                self.Stop()
                self.variable_rpm_resume_event.wait()
                if self.variable_rpm_stop_event.is_set():
                    self.Stop()
                    return False
                if self.variable_rpm_speed is not None:
                    self.Set_speed(Duration='Keep', Speed=self.variable_rpm_speed, CW=self.variable_rpm_direction)
            time.sleep(min(0.2, max(end_time - time.time(), 0)))
        return not self.variable_rpm_stop_event.is_set()

    def variable_rpm(self, direction=True, timer=1/60, time_step_long=60,
                    time_step_short=10, speed_max=999, speed_min=499, speed_step=100):
        """
        Makes the pump oscillate between given RPM values over a given time span.

        Args:
            direction (bool): Pump direction (True = CW, False = CCW)
            timer (float): Total duration in hours
            time_step (float): Time in seconds between speed adjustments
            speed_max (int): Maximum RPM
            speed_min (int): Minimum RPM
            speed_step (int): Step size in RPM
        """
        start_time = time.time()
        elapsed = 0
        action_speed = speed_max

        try:
            while elapsed < timer * 3600 and not self.variable_rpm_stop_event.is_set():
            # long duration of highest speed
                self.variable_rpm_speed = action_speed
                self.variable_rpm_direction = direction
                self.Set_speed(Duration='Keep', Speed=action_speed, CW=direction)
                elapsed = time.time() - start_time
                if not self._wait_with_variable_rpm_controls(time_step_long):
                    return
                elapsed = time.time() - start_time
                if elapsed >= timer * 3600:
                    return
                action_speed -= speed_step
                # decreasing speed in short time
                while action_speed > speed_min and not self.variable_rpm_stop_event.is_set():
                    self.variable_rpm_speed = action_speed
                    self.variable_rpm_direction = direction
                    self.Set_speed(Duration='Keep', Speed=action_speed, CW=direction)
                    if not self._wait_with_variable_rpm_controls(time_step_short):
                        return
                    elapsed = time.time() - start_time
                    if elapsed >= timer * 3600:
                        return
                    action_speed -= speed_step
                # increasing speed in short time
                while action_speed < speed_max and not self.variable_rpm_stop_event.is_set():
                    self.variable_rpm_speed = action_speed
                    self.variable_rpm_direction = direction
                    self.Set_speed(Duration='Keep', Speed=action_speed, CW=direction)
                    if not self._wait_with_variable_rpm_controls(time_step_short):
                        return
                    elapsed = time.time() - start_time
                    if elapsed >= timer * 3600:
                        return
                    action_speed += speed_step
                action_speed = speed_max
        finally:
            self.Stop()
            self.variable_rpm_worker = None
            self.variable_rpm_speed = None

    def back_and_forth(self, timer=1/60, time_step=60, Speed=999):
        """
        Makes the pump change direction every given time step

        Args:
            timer (float): Total duration in hours
            time_step (float): Time in seconds between speed adjustments
            Speed (int): RPM
        """
        start_time = time.time()
        elapsed = 0

        while elapsed < timer * 3600:
            self.Set_speed(Duration=time_step, Speed=Speed, CW=True)
            elapsed = time.time() - start_time
            while not self.ON:
                    time.sleep(5)
            if self.Speed == 111 or (elapsed >= timer * 3600):
                return
            
            self.Set_speed(Duration=time_step, Speed=Speed, CW=False)
            while not self.ON:
                    time.sleep(5)
            if self.Speed == 111 or (elapsed >= timer * 3600):
                return


class DummyFunction:
    def __getattr__(self, name):
        def method(*args, **kwargs):
            print(f"Function '{name}' called with args: {args} and kwargs: {kwargs}")
        return method

if __name__ == "__main__":
    serialstuff = OpenPort(DummyFunction())
    pumpy = PUMP(serial=serialstuff[0], Addr=1, Speed=55, CW=True, ON=True)

    variable = 'y'
    while True:
        variable = input("Enter 'a' to start, 'r' to resume, 'p' to pause, 's' to stop:\n").strip().lower()

        if variable == 'r':
            pumpy.resume_thread()
            #pumpy.Set_speed()
        elif variable == 'p':
            pumpy.pause_thread()
            #pumpy.Stop()
        elif variable == 's':
            pumpy.stop_thread()
            #pumpy.Stop()
        elif variable == 'a':
            pumpy.start_thread(timer="Keep")
            #pumpy.Set_speed()
        else:
            print("end, wrong input")
            pumpy.disconnect()
            break

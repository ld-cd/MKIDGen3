from logging import getLogger
import numpy as np
import time
import pynq
from mkidgen3.fixedpoint import FP16_15


class DACTableAXIM(pynq.DefaultIP):
    bindto = ['mazinlab:mkidgen3:dac_table_axim:1.33']

    def __init__(self, description):
        super().__init__(description=description)
        self._buffer = None

    def replay_ramp(self, tlast_every=256):
        ramp = np.arange(2 ** 19, dtype=np.complex64)
        ramp.real %= (2 ** 16)
        ramp.imag = ramp.real
        self.stop()
        self.replay(ramp, tlast_every=tlast_every, fpgen=None)

    def replay(self, data, tlast=True, tlast_every=256, replay_len=None, start=True,
               fpgen=lambda x: [FP16_15(v).__index__() for v in x], stop_if_needed=False):
        """
        data - an array of complex numbers, nominally 2^19 samples
        tlast - Set to true to emit a tlast pulse every tlast_every transactions
        tlast_every - assert tlast every tlast_every*16 th sample.
        replay_len - Number of groups of 16 (DAC takes 16/clock) in the data to use.
            e.g. if data[N].reshape(N//16,16). Note that the user channel will count to length-1
        start - start the replay immediately
        fpgen - set to a function to convert floating point numbers to integers. must work on arrays of data
        if None data will be truncated.
        """
        if self.register_map.run.run and not stop_if_needed:
            raise RuntimeError('Replay in progress. Call .stop() or with stop_if_needed=True')
        if fpgen == 'simple':
            data = (data * 8192).round().clip(-8192, 8191) * 4
            fpgen = None
        # Data has right shape
        if data.size < 2 ** 19:
            getLogger(__name__).warning('Insufficient data, padding with zeros')
            x = np.zeros(2 ** 19, dtype=np.complex64)
            x[:data.size] = data
            data = x

        if data.size > 2 ** 19:
            getLogger(__name__).warning('Data will be truncated to 2^19 samples')
            data = data[:2 ** 19]

        # Process Length
        if replay_len is None:
            replay_len = data.size // 16
        replay_len = int(replay_len)

        if replay_len > 2 ** 15:  # NB 15 as length is in sets of 16
            getLogger(__name__).warning('Clipping replay length to 2^15 sets of 16')
            replay_len = 2 ** 15
        if replay_len % 2:
            raise ValueError('Replay length must be a multiple of 2')

        # Process tlast_every
        tlast_every = int(tlast_every)

        if tlast and not (1 <= tlast_every <= replay_len):
            raise ValueError('tlast_every must be in [1-length] when tlast is set')
        if not tlast:
            tlast_every = 256  # just assign some value to ensure we didn't get handed garbage

        if self._buffer is not None:
            self._buffer.freebuffer()
        self._buffer = pynq.allocate(2 ** 20, dtype=np.uint16)

        iload = fpgen(data.real) if fpgen is not None else data.real.astype(np.int16)
        qload = fpgen(data.imag) if fpgen is not None else data.imag.astype(np.int16)
        for i in range(16):
            self._buffer[i:data.size * 2:32] = iload[i::16]
            self._buffer[i + 16:data.size * 2:32] = qload[i::16]

        if self.register_map.run.run:
            self.stop()
            while not self.register_map.CTRL.AP_IDLE:
                time.sleep(.0001)
        self.register_map.a_1 = self._buffer.device_address
        self.register_map.length_r = replay_len - 1  # length counter
        self.register_map.tlast = bool(tlast)
        self.register_map.replay_length = tlast_every - 1
        self.register_map.run = True
        if start:
            self.register_map.CTRL.AP_START = 1

    def stop(self):
        # Note the dac still outputs stuff (harmonics of last bunnfer)
        # Need to load URAM with zeros if you want it to be quiet (see quiet)
        self.register_map.run = False

    def quiet(self):
        """Replay 0s"""
        self.replay(np.zeros(16, dtype=np.complex64), tlast=False, replay_len=16, stop_if_needed=True)

    def start(self):
        # TODO probably need to check for the case where not idle and run = False (axis stall to completion)
        if self._buffer is None:
            raise RuntimeError('Must call replay first to configure the core')
        self.register_map.CTRL.AP_START = 1

    def status(self):
        return {'running': self.register_map.run and self.register_map.CTRL.AP_START,
                'buffer': self._buffer.copy() if self._buffer is not None else None}

    def configure(self, waveform_values=None, fpgen=None):

        if waveform_values is None:
            raise ValueError('waveform_values is None')

        self.replay(waveform_values, tlast=True, tlast_every=256, replay_len=None, start=True,
                    fpgen=fpgen, stop_if_needed=True)

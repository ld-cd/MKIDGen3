import threading
import time

import numpy as np
import zmq
import blosc2

# from . import power_sweep_freqs, N_CHANNELS, SYSTEM_BANDWIDTH
from mkidgen3.funcs import *
from mkidgen3.funcs import SYSTEM_BANDWIDTH, compute_lo_steps
import logging
import binascii
import os
from logging import getLogger


def zpipe(ctx):
    """
    build an inproc pipe for talking to threads
    mimic pipe used in czmq zthread_fork.
    Returns a pair of PAIRs connected via inproc
    """
    a = ctx.socket(zmq.PAIR)
    b = ctx.socket(zmq.PAIR)
    a.linger = b.linger = 0
    a.hwm = b.hwm = 1
    iface = "inproc://%s" % binascii.hexlify(os.urandom(8))
    a.bind(iface)
    b.connect(iface)
    return a, b


class Waveform:
    def __init__(self, frequencies, n_samples=2 ** 19, sample_rate=4.096e9, amplitudes=None, phases=None,
                 iq_ratios=None,
                 phase_offsets=None, seed=2, maximize_dynamic_range=True, compute=False):
        """
        Args:
            frequencies (float): list/array of frequencies in the comb
            n_samples (int): number of complex samples in waveform
            sample_rate (float): waveform sample rate in Hz
            amplitudes (float): list/array of amplitudes, one per frequency in (0,1]. If None, all ones is assumed.
            phases (float): list/array of phases, one per frequency in [0, 2*np.pi). If None, generates random phases using input seed.
            iq_ratios (float): list of ratios for IQ values used to help minimize image tones in band.
                       Allowed values between 0 and 1. If None, 50:50 ratio (all ones) is assumed.
                      TODO: what does this actually do and how does it work
            phase_offsets (float): list/array of phase offsets in [0, 2*np.pi)
            seed (int): random seed to seed phase randomization process

        Attributes:
            values (float): Computed waveform values. Amplitude is unscaled and is the product of additions of unit waveforms.
            quant_vals (int): Computed waveform values quantized to DAC digital format with optimum precision
            max_quant_error (float): maximum difference between quant_vals and values scaled to the DAC max output.
        """
        self.freqs = frequencies
        self.points = n_samples
        self.fs = sample_rate
        self.amps = amplitudes if amplitudes is not None else np.ones_like(frequencies)
        self.phases = phases if phases is not None else np.random.default_rng(seed=seed).uniform(0., 2. * np.pi,
                                                                                                 len(frequencies))
        self.iq_ratios = iq_ratios if iq_ratios is not None else np.ones_like(frequencies)
        self.phase_offsets = phase_offsets if phase_offsets is not None else np.zeros_like(frequencies)
        self._seed = seed
        self.quant_freqs = quantize_frequencies(self.freqs, rate=sample_rate, n_samples=n_samples)
        self._values = None
        self.quant_vals = None
        self.quant_error = None
        self.maximize_dynamic_range = maximize_dynamic_range
        if compute:
            self.values

    @property
    def values(self):
        if self._values is None:
            self._values = self._compute_waveform()
            if self.maximize_dynamic_range:
                self._waveform.optimize_random_phase(
                    max_quant_err=3 * predict_quantization_error(resolution=DAC_RESOLUTION),
                    max_attempts=10)
        return self._values

    def _compute_waveform(self):
        iq = np.zeros(self.points, dtype=np.complex64)
        # generate each signal
        t = 2 * np.pi * np.arange(self.points) / self.fs
        logging.getLogger(__name__).debug(
            f'Computing net waveform with {self.freqs.size} tones. For 2048 tones this takes about 7 min.')
        for i in range(self.freqs.size):
            exp = self.amps[i] * np.exp(1j * (t * self.quant_freqs[i] + self.phases[i]))
            scaled = np.sqrt(2) / np.sqrt(1 + self.iq_ratios[i] ** 2)
            c1 = self.iq_ratios[i] * scaled * np.exp(1j * np.deg2rad(self.phase_offsets)[i])
            iq.real += c1.real * exp.real + c1.imag * exp.imag
            iq.imag += scaled * exp.imag
        return iq

    def _optimize_random_phase(self, max_quant_err=3 * predict_quantization_error(resolution=DAC_RESOLUTION),
                               max_attempts=10):
        """
        inputs:
        - max_quant_error: float
            maximum allowable quantization error for real or imaginary samples.
            see predict_quantization_error() for how to estimate this value.
        - max_attempts: int
            Max number of times to recompute the waveform and attempt to get a quantization error below the specified max
            before giving up.

        returns: floating point complex waveform with optimized random phases
        """
        if max_quant_err is None:
            max_quant_err = 3 * predict_quantization_error(resolution=DAC_RESOLUTION)

        self.quant_vals, self.quant_error = quantize_to_int(self._values, resolution=DAC_RESOLUTION, signed=True,
                                                            word_length=ADC_DAC_INTERFACE_WORD_LENGTH,
                                                            return_error=True)
        cnt = 0
        while self.quant_error > max_quant_err:
            logging.getLogger(__name__).warning(
                "Max quantization error exceeded. The freq comb's relative phases may have added up sub-optimally."
                "Calculating with new random phases")
            self._seed += 1
            self.phases = np.random.default_rng(seed=self._seed).uniform(0., 2. * np.pi, len(self.freqs))
            self._values = self._compute_waveform()
            self.quant_vals, self.quant_error = quantize_to_int(self._values, resolution=DAC_RESOLUTION, signed=True,
                                                                word_length=ADC_DAC_INTERFACE_WORD_LENGTH,
                                                                return_error=True)
            cnt += 1
            if cnt > max_attempts:
                raise Exception("Process reach maximum attempts: Could not find solution below max quantization error.")
        return


class FLConfigMixin:
    def __eq__(self, other):
        """ Feedline configs are equivalent if all of their settings are equivalent"""
        if not isinstance(other, (type(self), int)):
            return False
        v_hash = hash(self)
        o_hash = other if isinstance(other, int) else hash(other)
        return v_hash == o_hash

    def __hash__(self):
        def hasher(v):
            try:
                return hash(v)
            except TypeError:
                return hash(v.tobytes())

        hash_data = ((k, hasher(getattr(self, k))) for k in self._settings)
        return hash(sorted(hash_data, key=lambda x: x[0]))

    def __str__(self):
        name = self.__class__.split('.')[-1]
        return (f"{name}: {hash(self)}\n"
                "  {self.settings_dict()}")

    def settings_dict(self):
        return {k: getattr(self, k) for k in self._settings}


class FLMetaConfigMixin:
    def __eq__(self, other):
        """ Feedline configs are equivalent if all of their settings are equivalent"""
        for k, v in self.__dict__.items():
            other_v = getattr(other, k)
            assert isinstance(v, (FLMetaConfigMixin, type(None), int))
            assert isinstance(other_v, (FLMetaConfigMixin, type(None), int))
            # if either is None we match
            if v is None or other_v is None:
                continue

            # Compute hash if necessary
            v_hash = hash(v) if isinstance(v, FLMetaConfigMixin) else v
            o_hash = hash(other_v) if isinstance(other_v, FLMetaConfigMixin) else other_v
            if v_hash != o_hash:
                return False
        return True

    def __hash__(self):
        return hash(tuple(sorted(((k, hash(v)) for k, v in self.__dict__.items()), key=lambda x: x[0])))


class DACConfig(FLConfigMixin):
    def __init__(self, ntones, name: str, n_uniform_tones=None, waveform_spec: [np.array, dict, Waveform] = None,
                 qmc_settings=None):
        self.spec_type = name
        freqs = power_sweep_freqs(ntones, bandwidth=SYSTEM_BANDWIDTH)
        wf_spec = dict(n_samples=2 ** 19, sample_rate=4.096e9, amplitudes=None, phases=None,
                       iq_ratios=None, phase_offsets=None, seed=2)
        if isinstance(waveform_spec, (np.array, list)):
            wf_spec['freqs'] = np.asarray(waveform_spec)

        if isinstance(waveform_spec, (dict, np.array, list)):
            wf_spec.update(waveform_spec)
            self._waveform = Waveform(**wf_spec)
        elif isinstance(waveform_spec, Waveform):
            self._waveform = waveform_spec
        else:
            raise ValueError('doing it wrong')

        self.qmc_settings = qmc_settings
        self._settings = ('quant_vals', 'qmc_settings')

    @property
    def quant_vals(self):
        return self._waveform.quant_vals

    @property
    def waveform(self):
        return self._waveform.values


class IFConfig(FLConfigMixin):
    def __init__(self, lo, adc_attn, dac_attn):
        self.lo = lo
        self.adc_attn = adc_attn
        self.dac_attn = dac_attn
        self._settings = ('lo', 'adc_attn', 'dac_attn')


class TriggerConfig(FLConfigMixin):
    def __init__(self, holdoffs: np.ndarray, thresholds: np.ndarray):
        self.holdoffs = holdoffs
        self.thresholds = thresholds
        self._settings = ('holdoffs', 'thresholds')


class ChannelConfig(FLConfigMixin):
    def __init__(self, frequencies):
        self.frequencies = frequencies
        self._settings = ('frequencies',)


class DDCConfig(FLConfigMixin):
    def __init__(self, tones, loop_center, phase_offset):
        self.tones = tones
        self.loop_center = loop_center
        self.phase_offset = phase_offset
        self.center_relative = False
        self.quantize = True
        self._settings = ('tones', 'loop_center', 'phase_offset', 'center_relative', 'quantize')


class FilterConfig(FLConfigMixin):
    def __init__(self):
        self.coefficients = np.zeros(2048, 30)
        self._settings = ('coefficients',)


class PhotonPipeConfig(FLMetaConfigMixin):
    def __init__(self, chan: ChannelConfig = None, ddc: DDCConfig = None, filter: FilterConfig = None,
                 trig: TriggerConfig = None):
        self.chan_config = chan
        self.ddc_config = ddc
        self.trig_config = trig
        self.filter_config = filter

    def __str__(self):
        return (f"PhotonPipe {hash(self)}:\n"
                f"  Chan: {self.chan_config}\n"
                f"  DDC: {self.ddc_config}\n"
                f"  Filt: {self.filter_config}\n"
                f"  Trig: {self.trig_config}")



    @property
    def chan_config(self):
        return self._chan_config


class FeedlineStatus:
    def __init__(self):
        self.status = 'feedline status'


class DACStatus:
    def __init__(self, waveform: Waveform):
        self.waveform = waveform
        self._output_on = False


class DDCStatus:
    def __init__(self, tone_increments, phase_offsets, centers):
        self.tone_increments = tone_increments
        self.phase_offsets = phase_offsets
        self.centers = centers


class PowerSweepPipeCfg(FeedlineConfig):
    def __init__(self):
        super().__init__(dac_setup=DACConfig('regular_comb'),
                         channels=np.arange(0, 4096, 2, dtype=int))


class FLPhotonBuffer:
    """An nxm+1 sparse array full of photon events"""
    WATERMARK = 4500

    def __init__(self, _buf=None):
        self._buf = _buf

    @property
    def full(self):
        return (self._buf[0, :] > self.WATERMARK).any()


class CapDest:
    def __init__(self, data_dest: str, status_dest: str = ''):
        self._dest = data_dest
        self._socket = None
        self._status = None
        self._status_dest = status_dest

    def establish(self, context: zmq.Context):
        if self._status_dest:
            self._status = context.socket(zmq.REQ)
            self._status.connect(self._dest)
        if self._dest.startswith('file'):
            raise NotImplementedError
            f = os.path.open(self._dest, 'ab')
            f.close()
        else:
            self._socket = context.socket(zmq.PUB)
            self._socket.connect(self._dest)


class CaptureAbortedException(Exception):
    pass


class CaptureSink:
    # def __init__(self, request, server):
    #     pass

    def data(self):
        return None


class ADCCaptureSink(CaptureSink, threading.Thread):
    def __init__(self, id, source, term, context: zmq.Context = None, start=True):
        super(ADCCaptureSink, self).__init__(name=f'ADCCaptureSink_{id}')
        self.daemon = True
        self.cap_id = id
        self.data_source = source
        self.result = None
        self.term = term
        if start:
            self.start()

    def run(self):
        """

        Args:
            xymap: [nfeedline, npixel, 2] array
            feedline_source: zmq.PUB socket with photonbufers published by feedline
            term_source: a zmq socket of undecided type for detecting shutdown requests

        Returns: None

        """
        # term_source = None
        # context = zmq.Context.instance()
        # term = context.socket(zmq.SUB)
        # term.setsockopt(zmq.SUBSCRIBE, self.id)
        # term.connect(term_source)

        try:
            with zmq.Context.instance() as ctx:
                with ctx.socket(zmq.SUB) as data:
                    data.setsockopt(zmq.SUBSCRIBE, self.cap_id)
                    data.connect(self.data_source)

                    poller = zmq.Poller()
                    poller.register(self.term, flags=zmq.POLLIN)
                    poller.register(data, flags=zmq.POLLIN)

                    recieved = []
                    while True:
                        avail = dict(poller.poll())
                        if self.term in avail:
                            break
                        id, data = data.recv_multipart(copy=False)
                        if not data:
                            break
                        d = blosc2.decompress(data)
                        # raw adc data is i0q0 i1q1 int16

                        # TODO save the data or do something with it
                        recieved.append(d)

                    # self.term.close()
                    self.result = np.array(recieved)
        except zmq.ZMQError as e:
            getLogger(__name__).warning(f'Shutting down {self} due to {e}')
        # finally:
            # self.term.close()


class PhotonCaptureSink(CaptureSink):
    def __init__(self, source, context: zmq.Context = None):
        pass

    def terminate(self, context: zmq.Context = None):
        """terminate saving data"""
        context = context or zmq.Context.instance()
        _terminate = context.socket(zmq.PUB)
        _terminate.connect('inproc://PhotonCaptureSink.terminator.inproc')
        _terminate.send(b'')
        _terminate.close()

    def capture(self, hdf, xymap, feedline_source, fl_ids):
        t = threading.Thread(target=self._main, args=(hdf, xymap, feedline_source, fl_ids))
        t.start()

    @staticmethod
    def _main(hdf, xymap, feedline_source, fl_ids, term_source='inproc://PhotonCaptureSink.terminator.inproc'):
        """

        Args:
            xymap: [nfeedline, npixel, 2] array
            feedline_source: zmq.PUB socket with photonbufers published by feedline
            term_source: a zmq socket of undecided type for detecting shutdown requests

        Returns: None

        """

        fl_npix = 2048
        n_fl = 5
        MAX_NEW_PHOTONS = 5000
        DETECTOR_SHAPE = (128, 80)
        fl_id_to_index = np.arange(n_fl, dtype=int)

        context = zmq.Context.instance()
        term = context.socket(zmq.SUB)
        term.setsockopt(zmq.SUBSCRIBE, id)
        term.connect(term_source)

        data = context.socket(zmq.SUB)
        data.setsockopt(zmq.SUBSCRIBE, fl_ids)
        data.connect(feedline_source)

        poller = zmq.Poller()
        poller.register(term, flags=zmq.POLLIN)
        poller.register(data, flags=zmq.POLLIN)

        live_image = np.zeros(DETECTOR_SHAPE)
        live_image_socket = None
        live_image_by_fl = live_image.reshape(n_fl, fl_npix)
        photons_rabuf = np.recarray(MAX_NEW_PHOTONS,
                                    dtype=(('time', 'u32'), ('x', 'u32'), ('y', 'u32'),
                                           ('phase', 'u16')))

        while True:
            avail = poller.poll()
            if term in avail:
                break

            frame = data.recv_multipart(copy=False)
            fl_id = frame[0]
            time_offset = frame[1]
            d = blosc2.decompress(frame[1])
            frame_duration = None  # todo time coverage of data
            # buffer is nchan*nmax+1 32bit: 16bit time(base2) 16bit phase
            # make array of to [nnmax+1, nchan, 2] uint16
            # nmax will always be <<2^12 number valid will be at [0,:,0]
            # times need oring w offset
            # photon data is d[1:d[0,i,0], i, :]

            nnew = d[0, :, 0].sum()
            # if we wanted to save binary data then we could save this, the x,y list, and the time offset
            # mean pixel count rate in this packet is simply [0,:,0]/dt
            fl_ndx = fl_id_to_index[fl_id]
            live_image_by_fl[fl_ndx, :] += d[0, :, 0] / frame_duration

            # if live_image_ready
            live_image_socket.send_multipart([f'liveim', blosc2.compress(live_image)])

            cphot = np.cumsum(d[0, :, 0], dtype=int)
            for i in range(fl_npix):
                sl_out = slice(cphot[i], cphot[i] + d[0, i, 0])
                sl_in = slice(1, d[0, i, 0])
                photons_rabuf['time'][sl_out] = d[sl_in, :, 0]
                photons_rabuf['time'][sl_out] |= time_offset
                photons_rabuf['phase'][sl_out] = d[sl_in, :, 1]
                photons_rabuf['x'][sl_out] = xymap[fl_ndx, i, 0]
                photons_rabuf['y'][sl_out] = xymap[fl_ndx, i, 1]
            hdf.grow_by(photons_rabuf[:nnew])

        term.close()
        data.close()
        hdf.close()


class PostageCaptureSink(CaptureSink):
    def __init__(self, source, context: zmq.Context = None):
        pass


def CaptureSinkFactory(request, server, start=True) -> ((zmq.Socket, zmq.Socket), CaptureSink):
    a, pipe = zpipe(zmq.Context.instance())
    if request.tap == 'adc':
        saver = ADCCaptureSink(request.id, server, pipe, start=start)
    elif request.tap == 'photon':
        saver = PhotonCaptureSink(request.id, server, pipe)
    elif request.tap == 'postage':
        saver = PostageCaptureSink(request.id, server, pipe)
    elif request.tap == 'phase':
        saver = ADCCaptureSink(request.id, server, pipe, start=start)
    elif request.tap == 'iq':
        saver = ADCCaptureSink(request.id, server, pipe, start=start)

    return (a, pipe), saver


class StatusListner(threading.Thread):
    def __init__(self, id, source, initial_state='Created', start=True):
        super().__init__(name=f'StautsListner_{id}')
        self.daemon = True
        self.shutdown = False
        self.source = source
        self.id = id
        self._status_messages = [initial_state]
        if start:
            self.start()

    def run(self):
        with zmq.Context().instance() as ctx:
            with ctx.socket(zmq.SUB) as sock:
                sock.linger = 0
                sock.setsockopt(zmq.SUBSCRIBE, self.id)
                sock.connect(self.source)
                getLogger(__name__).debug(f'Listening for status updates to {self.id}')
                while not self.shutdown:
                    try:
                        id, _, update = sock.recv_multipart()
                        assert id == self.id
                    except zmq.ZMQError as e:
                        if e.errno == zmq.EAGAIN:
                            time.sleep(.1)  # play nice
                        elif e.errno == zmq.ETERM:
                            break
                    else:
                        update = update.decode()
                        self._status_messages.append(update)
                        getLogger(__name__).debug(f'Status update for {self.id}: {update}')
                        if (update.startswith('finished') or
                                update.startswith('aborted') or
                                update.startswith('failed')):
                            break
                sock.close()

    def latest(self):
        return self._status_messages[-1]


class CaptureRequest:
    def __init__(self, n, tap, feedline_config: FeedlineConfig, feedline_server):
        self.points = n
        self._last_status = None
        self.tap = tap  # maybe add some error handling here
        self.feedline_config = feedline_config
        self._feedline_server = feedline_server
        self._status_socket = None
        self._data_socket = None

    def __hash__(self):
        return hash((hash(self.feedline_config), self.tap, self.points, self._feedline_server))

    def __del__(self):
        self.destablish()

    @property
    def type(self):
        return 'engineering' if self.tap in ('adc', 'iq', 'phase') else self.tap

    @property
    def id(self):
        return str(hash(self)).encode()

    def establish(self, data_server='inproc://cap_data.xsub', status_server='inproc://cap_status.xsub',
                  context: zmq.Context = None):
        context = context or zmq.Context.instance()
        self._status_socket = context.socket(zmq.PUB)
        self._status_socket.connect(status_server)
        self._data_socket = context.socket(zmq.PUB)
        self._data_socket.connect(data_server)
        self._established = True
        self.set_status('established')

    def destablish(self):
        try:
            self._status_socket.close()  # TODO do we need to wait to make sure any previous sends get sent
        except AttributeError:
            pass
        try:
            self._data_socket.close()
        except AttributeError:
            pass
        self._established = False

    def fail(self, message, context: zmq.Context = None):
        self.set_status('failed', message, context=context)
        self.destablish()

    def finish(self):
        self.set_status('finished')
        self.destablish()

    def abort(self, message, context: zmq.Context = None):
        self.set_status('aborted', message, context=context)
        self.destablish()

    def add_data(self, data, status=''):
        if not self._established:
            raise RuntimeError('Establish must be called before add_data')
        # TODO ensure we are being smart about pointers and buffer acces vs copys
        self._data_socket.send_multipart([self.id, blosc2.compress(data)])
        self.set_status('capturing', message=status)

    def set_status(self, status, message='', context: zmq.Context = None,
                   status_server='inproc://capture_status'):
        """
        get appropriate context and send current status message after connecting socket.

        status_server is ignored once a status destination connection is established
        """
        self._last_status = status
        if not self._status_socket:
            context = context or zmq.Context().instance()
            self._status_socket = context.socket(zmq.PUB)
            self._status_socket.connect(status_server)
        self._status_socket.send_multipart([self.id, f'{status}:{message}'.encode()])

    @property
    def size(self):
        return self.points * 2048


class CaptureJob:  # feedline client end
    def __init__(self, request: CaptureRequest, feedline_server: str, data_server: str, status_server: str,
                 submit=True):
        self.request = request
        self._status_listner = StatusListner(request.id, status_server, initial_state='CREATED', start=False)
        self.feedline_server = feedline_server
        self._savepipes, self._datasaver = CaptureSinkFactory(request, data_server, start=False)
        if submit:
            self.submit()

    def status(self):
        """ Return the last known status of the request """
        return self._status_listner.latest()

    def cancel(self):
        self._savepipes[0].send(b'')
        ctx = zmq.Context().instance()
        with ctx.socket(zmq.REQ) as s:
            s.connect(self.feedline_server)
            s.send_pyobj(('abort', self.request.id))
            return s.recv_json()

    def data(self):
        return self._datasaver.data()

    def submit(self):
        self._status_listner.start()
        self._datasaver.start()
        self._submit()

    def _submit(self):
        ctx = zmq.Context().instance()
        # with zmq.Context().instance() as ctx:
        with ctx.socket(zmq.REQ) as s:
            s.connect(self.feedline_server)
            s.send_pyobj(('capture', self.request))
            return s.recv_json()

    def __del__(self):
        self._savepipes[0].send(b'')
        if self._datasaver is not None:
            self._datasaver.join()
        self._savepipes[0].close()
        self._savepipes[1].close()


class PowerSweepRequest:
    def __init__(self, ntones=2048, points=512, min_attn=0, max_attn=30, attn_step=0.25, lo_center=0, fres=7.14e3,
                 use_cached=True):
        """
        Args:
            ntones (int): Number of tones in power sweep comb. Default is 2048.
            points (int): Number of I and Q samples to capture for each IF setting.
            min_attn (float): Lowest global attenuation value in dB. 0-30 dB allowed.
            max_attn (float): Highest global attenuation value in dB. 0-30 dB allowed.
            attn_step (float): Difference in dB between subsequent global attenuation settings.
                               0.25 dB is default and finest resolution.
            lo_center (float): Starting LO position in Hz. Default is XXX XX-XX allowed.
            fres (float): Difference in Hz between subsequent LO settings.
                               7.14e3 Hz is default and finest resolution we can produce with a 4.096 GSPS DAC
                               and 2**19 complex samples in the waveform look-up-table.

        Returns:
            PowerSweepRequest: Object which computes the appropriate hardware settings and produces the necessary
            CaptureRequests to collect power sweep data.

        """
        self.freqs = np.linspace(0, ntones - 1, ntones)
        self.points = points
        self.total_attens = np.arange(min_attn, max_attn + attn_step, attn_step)
        self._sweep_bw = SYSTEM_BANDWIDTH / ntones
        self.lo_centers = compute_lo_steps(center=lo_center, resolution=fres, bandwidth=self._sweep_bw)
        self.use_cached = use_cached

    def capture_requests(self):
        dacsetup = DACOutputSpec('power_sweep_comb', n_uniform_tones=self.ntones)
        return [CaptureRequest(self.samples, dac_setup=dacsetup,
                               if_setup=IFSetup(lo=freq, adc_attn=adc_atten, dac_attn=dac_atten))
                for (adc_atten, dac_atten) in self.attens for freq in self.lo_centers]

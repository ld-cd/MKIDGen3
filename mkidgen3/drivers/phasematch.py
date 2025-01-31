import numpy as np
import pynq
from logging import getLogger
from mkidgen3.mkidpynq import check_description_for
from mkidgen3.fixedpoint import fp_factory
from mkidgen3.util import pack16_to_32
import time


class PhasematchDriver(pynq.DefaultHierarchy):
    N_TEMPLATE_TAPS = 30
    N_RES = 2048
    N_RES_P_LANE = 512
    N_LANES = 4
    N_SLOTS = 2
    MAX_COEFF_VALUE = 127  # 16 bits, 1 sign, 8 fractional
    COEFF_FORMAT = (1, 15)

    def __init__(self, description):
        super().__init__(description)
        self.fifo = self.reload.axi_fifo_mm_s_0
        self._pending = [0, 0, 0, 0]

    @staticmethod
    def checkhierarchy(description):
        if 'reload' not in description.get('hierarchies', {}):
            return False
        return bool(len(check_description_for(description['hierarchies']['reload'], 'xilinx.com:ip:axi_fifo_mm_s')))

    @staticmethod
    def vet_coeffs(coeffs):
        if coeffs.size != PhasematchDriver.N_TEMPLATE_TAPS:
            raise ValueError('Incorrect number of taps')
        if max(abs(coeffs)) > PhasematchDriver.MAX_COEFF_VALUE:
            raise ValueError(f'Coefficients must be <= {PhasematchDriver.MAX_COEFF_VALUE}')

    @staticmethod
    def vet_res_id(res_id):
        if 0 > res_id or res_id >= PhasematchDriver.N_RES:
            raise ValueError(f'resID must be in [0-{PhasematchDriver.N_RES}-1]')

    @staticmethod
    def reorder_coeffs(coeffs):
        """convert taps to order needed by a reload packet"""
        return coeffs[::-1]  # see coefficient reload tab for order in block design

    def load_coeff(self, res_id, coeffs, vet=True, force_commit=False, raw=False):
        """
        A reload packet consists of the coefficients and the coefficient set number

        If raw coeffs will be converted to np.uint16 via numpy casting/type coercion rules.

        See block diagram for layout. Resonators assigned to lanes 0-3 in consecutive sets of 512.

        FIRs have two reload slots and are in "on vector" update mode.

        See pg149 pg 18
        """
        self.vet_res_id(res_id)
        if vet:
            self.vet_coeffs(coeffs)

        if raw:
            fp_format = lambda x: x
        else:
            fp_format = fp_factory(*self.COEFF_FORMAT, True, include_index=True)

        lane = res_id % self.N_LANES
        reload_packet = np.zeros(self.N_TEMPLATE_TAPS + 1, dtype=np.uint16)
        reload_packet[0] = res_id // self.N_LANES
        reload_packet[1:] = [fp_format(c) for c in self.reorder_coeffs(coeffs)]

        cfg_packet = pack16_to_32(np.arange(self.N_RES_P_LANE, dtype=np.uint16))
        if max(self._pending) >= self.N_SLOTS:
            getLogger(__name__).warning('Forcing config before load as reload slots are full')
            self.fifo.tx(cfg_packet, destination=4)  # Send a config packet to trigger reload
            self._pending[:] = [0, 0, 0, 0]
        self.fifo.tx(pack16_to_32(reload_packet), destination=lane, last_bytes=2)  # reload channels are 0,2,4,6
        self._pending[lane] += 1
        if force_commit or max(self._pending) >= self.N_SLOTS:
            if not force_commit:
                getLogger(__name__).debug('Sending config packet')
            self.fifo.tx(cfg_packet, destination=4)  # Send a config packet to trigger reload
            self._pending[:] = [0, 0, 0, 0]

    def load_coeff_sets(self, coeff_sets):
        for res in range(self.N_RES):
            if self.fifo.tx_vacancy < 500:
                time.sleep(.1)
            self.load_coeff(res, coeff_sets[res], vet=True, force_commit=False)

    def configure(self, coefficients):
        getLogger(__name__).info(f'Configuring phasematch with {coefficients}')
        if isinstance(coefficients, str) and coefficients == 'unity':
            coefficients = np.zeros((2048, 30), dtype=np.int16)
            coefficients[:, 0] = 2 ** 15 - 1
        if coefficients.shape != (2048, 30) or coefficients.dtype != 'int16':
            raise ValueError('Please specify a (2048,30) array of int16s')

        channel = 0
        for coefs in coefficients:
            self.load_coeff(channel, coefs, vet=False, raw=True)
            channel += 1
        self.load_coeff(channel - 1, coefs, vet=False, raw=True, force_commit=True)
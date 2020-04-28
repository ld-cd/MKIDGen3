import numpy as np
from fpbinary import FpBinary, OverflowEnum, RoundingEnum

FP16_15 = lambda x: FpBinary(int_bits=1, frac_bits=15, signed=True, value=x)
FP26_26 = lambda x: FpBinary(int_bits=0, frac_bits=26, signed=True, value=x)
FP16_26 = lambda x: FpBinary(int_bits=-10, frac_bits=26, signed=True, value=x)
FP32_31 = lambda x: FpBinary(int_bits=1, frac_bits=31, signed=True, value=x)
FP18_17 = lambda x: FpBinary(int_bits=1, frac_bits=17, signed=True, value=x)


def do_fixed_point_pfb(fpcomb, fpcoeff, n_convert=None, truncate=True):
    """Set truncate to false to preserve the full output bitwidth. Truncation is done with FpBinary defaults."""
    n_total_packets = fpcomb.size // 2048 if n_convert is None else n_convert
    fft_block = np.zeros((n_total_packets + 1, 256, 16), dtype=np.complex64)
    for i in range(0, 128, 2):  # each packet of ADC samples, 128 new things to a lane 2 packets to feed all channels
        lane_out = np.zeros((2, 256, 16), dtype=np.complex64)
        for l in range(16):
            fresh = np.array([fpcoeff[l, :, 7 - c_i] * fpcomb[i + 2 * c_i:i + 2 * c_i + 2, l::16, :].reshape(256, 2).T
                              for c_i in range(8)]).sum(axis=0)
            delay = np.roll(np.array(
                [fpcoeff[l, :, 7 - c_i] * fpcomb[1 + i + 2 * c_i:1 + i + 2 * c_i + 2, l::16, :].reshape(256, 2).T
                 for c_i in range(8)]).sum(axis=0), 128, axis=1)
            # Sum the multiplies are roll the delayed samples
            if truncate:
                outformat = (-9, sum(fpcomb.flat[0].format) + 9)
                conv = lambda a: np.array(list(map(lambda x: float(x.resize(outformat)), a)))
                lane_out[0, :, l] = conv(fresh[0]) + conv(fresh[1]) * 1j
                lane_out[1, :, l] = conv(delay[0]) + conv(delay[1]) * 1j
            else:
                lane_out[0, :, l] = fresh[0].astype(float) + fresh[1].astype(float) * 1j
                lane_out[1, :, l] = delay[0].astype(float) + delay[1].astype(float) * 1j
        fft_block[i] = lane_out[0]
        fft_block[i + 1] = lane_out[1]
    return fft_block


def fparray(a, fpgen):
    """Convert a numpy array to an object array of FixedPoint numbers using the fp generator"""
    out = np.empty_like(a, dtype=object)
    if isinstance(type(a.dtype), (np.complex, np.complex128, np.complex64)):
        out.flat = [(fpgen(x.real), fpgen(x.imag)) for x in a.flat]
    else:
        out.flat = [fpgen(x) for x in a.flat]
    return out

import re
import pkg_resources
from logging import getLogger
import subprocess
def _parse_ticspro(file):
    with open(file, 'r') as f:
        lines = [l.rstrip("\n") for l in f]

        registers = []
        for i in lines:
            m = re.search('[\t]*(0x[0-9A-F]*)', i)
            registers.append(int(m.group(1), 16), )
    return registers


def _patch_xrfclk_lmk():
    # access with     xrfdc.set_ref_clks(lmk_freq='122.88_viaext10M')

    lmk04208_files = {
        '122.88_viaext10M': 'config/ZCU111_LMK04208_10MHz_Ref_J109SMA.txt'
    }

    lmk04828_files = {
        '256.0_MTS': 'config/LMK04828_256.0_MTS.txt',
        '500.0_MTS': 'config/LMK04828_500.0_MTS.txt'
    }

    lmx2594_files = {
        '500.0_MTS': 'config/LMX2594_500.0_MKTS.txt',
        '409.6_MTS': 'config/LMX2594_409.6_256FoscMTS.txt'
    }

    clock_config_dict = {
    'lmk04208': lmk04208_files,
    'lmk04828': lmk04828_files,
    'lmx2594': lmx2594_files
    }

    for clock_part in clock_config_dict:
        for programming_key, fname in clock_config_dict[clock_part].items():
            tpro_file = pkg_resources.resource_filename('mkidgen3',fname)
            xrfclk.xrfclk._Config[clock_part][programming_key] = _parse_ticspro(tpro_file)


def start_clocks(programming_key=False):
    """
    - 'external_10mhz' pull LMK clock source from 10 MHz Ref (ZCU111 Only for now)
    - '4.096GSPS_MTS' MTS compatible with 4.096 GSPS Sampling Fequency (RFSoC4x2 Only)
    - '5.000GSPS_MTS' MTS compatible with 5.000 GSPS Sampling Frequency (RFSoC4x2 Only)
    """
    try:
        import xrfclk, xrfdc
    except ImportError:
        getLogger(__name__).warning('xrfclk/xrfdc unavaiable, clock will not be started')
        return
    if programming_key is not False:
        _patch_xrfclk_lmk()

    board_name = subprocess.run(['cat', '/proc/device-tree/chosen/pynq_board'], capture_output=True, text=True).stdout

    if board_name == 'RFSoC4x2\x00':
        if programming_key == 'external_10mhz':
            raise ValueError('External 10 MHz is not supported on RFSoC4x2')
        if programming_key == '4.096GSPS_MTS':
            xrfclk.set_ref_clks(lmk_freq='256.0_MTS', lmx_freq='409.6_MTS')
        if programming_key == '5.000GSPS_MTS':
            xrfclk.set_ref_clks(lmk_freq='500.0_MTS', lmx_freq='500.0_MTS')
        else:
            xrfclk.set_ref_clks(lmk_freq=245.76, lmx_freq=409.6)

    elif board_name == 'ZCU111\x00':
        if programming_key == 'external_10mhz':
            _patch_xrfclk_lmk()
            xrfclk.set_ref_clks(lmk_freq='122.88_viaext10M', lmx_freq=409.6)
        else:
            xrfclk.set_ref_clks(lmk_freq=122.88, lmx_freq=409.6)

    else:
        raise ValueError('Unknown board name. Cannot proceed with clock programming.')
# SPDX-FileCopyrightText: © 2023 Tenstorrent Inc.
# SPDX-License-Identifier: Apache-2.0

"""
This file contains functions used to do a pcie level reset for Wormhole chip.
"""

import os
import sys
import time
import fcntl
import struct
from typing import List
from pyluwen import PciChip
from tt_tools_common.ui_common.themes import CMD_LINE_COLOR
from tt_tools_common.utils_common.tools_utils import read_refclk_counter


class WHChipReset:
    """Class to perform a chip level reset on WH pcie boards"""

    # WH magic numbers for reset
    TENSTORRENT_IOCTL_MAGIC = 0xFA
    TENSTORRENT_IOCTL_RESET_DEVICE = (TENSTORRENT_IOCTL_MAGIC << 8) | 6
    TENSTORRENT_RESET_DEVICE_RESTORE_STATE = 0
    TENSTORRENT_RESET_DEVICE_RESET_PCIE_LINK = 1
    A3_STATE_PROP_TIME = 0.03
    POST_RESET_MSG_WAIT_TIME = 2
    MSG_TRIGGER_SPI_COPY_LtoR = 0x50
    MSG_TYPE_ARC_STATE3 = 0xA3
    MSG_TYPE_TRIGGER_RESET = 0x56

    def reset_device_ioctl(self, interface_id: int, flags: int) -> bool:
        dev_path = f"/dev/tenstorrent/{interface_id}"
        dev_fd = os.open(
            dev_path, os.O_RDWR | os.O_CLOEXEC
        )  # Raises FileNotFoundError and other appropriate exceptions.
        try:
            reset_device_in_struct = "II"
            reset_device_out_struct = "II"
            reset_device_struct = reset_device_in_struct + reset_device_out_struct

            input_size_bytes = struct.calcsize(reset_device_in_struct)
            output_size_bytes = struct.calcsize(reset_device_out_struct)
            reset_device_buf = bytearray(
                struct.pack(reset_device_struct, output_size_bytes, flags, 0, 0)
            )
            fcntl.ioctl(
                dev_fd, self.TENSTORRENT_IOCTL_RESET_DEVICE, reset_device_buf
            )  # Raises OSError

            output_buf = reset_device_buf[input_size_bytes:]
            _, result = struct.unpack(reset_device_out_struct, output_buf)

            return result == 0
        finally:
            os.close(dev_fd)

    def full_lds_reset(
        self, pci_interfaces: List[int], reset_m3: bool = False
    ) -> List[PciChip]:
        """Performs a full LDS reset of a list of chips"""

        for pci_interface in pci_interfaces:
            self.reset_device_ioctl(
                pci_interface, self.TENSTORRENT_RESET_DEVICE_RESET_PCIE_LINK
            )
        pci_chips = [PciChip(pci_interface=interface) for interface in pci_interfaces]
        refclk_list = []
        fail = False
        # Trigger resets for all chips in order
        for chip in pci_chips:
            # Collect the arc refclk for the chip before sending reset arc messages
            refclk_list.append(read_refclk_counter(chip))
            # Trigger A3 safe state. A3 is a safe state where there are no more pending regulator requests.
            chip.arc_msg(self.MSG_TYPE_ARC_STATE3, wait_for_done=True)
            time.sleep(self.A3_STATE_PROP_TIME)
            # Triggers M3 board level reset by sending arc msg.
            if reset_m3:
                chip.arc_msg(self.MSG_TYPE_TRIGGER_RESET, wait_for_done=False, arg0=3)
            else:
                chip.arc_msg(self.MSG_TYPE_TRIGGER_RESET, wait_for_done=False)

        time.sleep(self.POST_RESET_MSG_WAIT_TIME)

        for i, (chip, pci_interface) in enumerate(zip(pci_chips, pci_interfaces)):
            self.reset_device_ioctl(
                pci_interface, self.TENSTORRENT_RESET_DEVICE_RESTORE_STATE
            )
            current_refclk = read_refclk_counter(chip)
            if refclk_list[i] < current_refclk:
                print(
                    CMD_LINE_COLOR.RED,
                    f"Reset for pci {pci_interface} didn't go through! Refclk didn't reset. Value before: {refclk_list[i]}, value after: {current_refclk}",
                    CMD_LINE_COLOR.ENDC,
                )
                fail = True

        if fail:
            print(
                CMD_LINE_COLOR.BLUE,
                "Reset failed for one or more boards, returning with non-zero exit code",
                CMD_LINE_COLOR.ENDC,
            )
            sys.exit(1)

        return pci_chips
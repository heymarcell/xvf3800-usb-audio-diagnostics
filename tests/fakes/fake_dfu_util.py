#!/usr/bin/env python3
"""Stand-in for dfu-util. Flashing `-D <file>` loads the firmware label stored in the fake
image into the XVFDIAG_FAKE_STATE device and resets runtime registers, like a reboot.
The DFU interface is listed unless the state sets "dfu_visible": false (button fallback).
"reset_exit" reproduces macOS dfu-util 0.11, which exits 251 when the device vanishes during
the final -R reset after a complete download; "fail_download" aborts mid-transfer."""
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["XVFDIAG_FAKE_STATE"])
args = sys.argv[1:]
with state_path.with_suffix(".dfu.log").open("a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")

if "-V" in args:
    print("dfu-util 0.11 (fake)")
elif "-l" in args:
    if not json.loads(state_path.read_text(encoding="utf-8")).get("dfu_visible", True):
        sys.exit(0)
    print('Found DFU: [2886:001a] ver=0202, devnum=9, cfg=1, intf=0, path="1-1", alt=1, name="DFU Upgrade", serial="101991441000000000"')
    print('Found DFU: [2886:001a] ver=0202, devnum=9, cfg=1, intf=0, path="1-1", alt=0, name="DFU Factory", serial="101991441000000000"')
elif "-D" in args:
    image = Path(args[args.index("-D") + 1])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    print("Copying data from PC to DFU device")
    if state.get("fail_download"):
        print("Download\t[=====                    ]  20%       186880 bytes")
        print("dfu-util: Error during download get_status")
        sys.exit(74)
    state.update(firmware=image.read_text(encoding="utf-8").strip(), regs={})
    state_path.write_text(json.dumps(state), encoding="utf-8")
    print("Download done.\nDFU state(7) = dfuMANIFEST, status(0) = No error condition is present")
    print("DFU state(2) = dfuIDLE, status(0) = No error condition is present\nDone!")
    print("Resetting USB to switch back to Run-Time mode")
    sys.exit(state.get("reset_exit", 0))
else:
    sys.exit(74)

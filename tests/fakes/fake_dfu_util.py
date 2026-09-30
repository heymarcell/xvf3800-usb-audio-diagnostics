#!/usr/bin/env python3
"""Stand-in for dfu-util. Flashing `-D <file>` loads the firmware label stored in the fake
image into the XVFDIAG_FAKE_STATE device and resets runtime registers, like a reboot."""
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
    print('Found DFU: [2886:001a] ver=0202, devnum=9, cfg=1, intf=0, path="1-1", alt=1, name="DFU Upgrade", serial="101991441000000000"')
    print('Found DFU: [2886:001a] ver=0202, devnum=9, cfg=1, intf=0, path="1-1", alt=0, name="DFU Factory", serial="101991441000000000"')
elif "-D" in args:
    image = Path(args[args.index("-D") + 1])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(firmware=image.read_text(encoding="utf-8").strip(), regs={})
    state_path.write_text(json.dumps(state), encoding="utf-8")
    print("Download done.\nResetting USB to switch back to Run-Time mode")
else:
    sys.exit(74)

#!/usr/bin/env python3
"""Stand-in for Seeed's python_control/xvf_host.py used by the hardware-free tests.

Mirrors the upstream CLI contract at the pinned commit: `COMMAND` reads and prints
`COMMAND: [v, ...]` then `Done!`; `COMMAND --values V ...` writes. Positional values are
rejected by argparse exactly like upstream. Device state lives in the JSON file named by
XVFDIAG_FAKE_STATE, and every invocation is appended to <state>.host.log.
"""
import argparse
import json
import os
import sys
from pathlib import Path

# name: (value count, type, access) — must match upstream PARAMETERS (see tests/test_upstream.py).
PARAMETERS = {
    "VERSION": (3, "uint8", "ro"),
    "BLD_MSG": (50, "char", "ro"),
    "BLD_REPO_HASH": (40, "char", "ro"),
    "USB_BIT_DEPTH": (2, "uint8", "rw"),
    "SAVE_CONFIGURATION": (1, "uint8", "wo"),
    "CLEAR_CONFIGURATION": (1, "uint8", "wo"),
    "AEC_AZIMUTH_VALUES": (4, "radians", "ro"),
    "AEC_SPENERGY_VALUES": (4, "float", "ro"),
    "AEC_HPFONOFF": (1, "int32", "rw"),
    "AEC_RT60": (1, "float", "ro"),
    "AEC_ASROUTONOFF": (1, "int32", "rw"),
    "AEC_ASROUTGAIN": (1, "float", "rw"),
    "AUDIO_MGR_MIC_GAIN": (1, "float", "rw"),
    "AUDIO_MGR_REF_GAIN": (1, "float", "rw"),
    "AUDIO_MGR_OP_PACKED": (2, "uint8", "rw"),
    "AUDIO_MGR_OP_UPSAMPLE": (2, "uint8", "rw"),
    "AUDIO_MGR_OP_L": (2, "uint8", "rw"),
    "AUDIO_MGR_OP_R": (2, "uint8", "rw"),
    "AUDIO_MGR_SYS_DELAY": (1, "int32", "rw"),
    "DOA_VALUE": (2, "uint16", "ro"),
    "PP_AGCMAXGAIN": (1, "float", "rw"),
    "PP_AGCDESIREDLEVEL": (1, "float", "rw"),
    "PP_LIMITONOFF": (1, "int32", "rw"),
}

DEFAULTS = {
    "USB_BIT_DEPTH": [0, 0], "AEC_AZIMUTH_VALUES": [0.0, 1.571, 3.142, 0.0], "AEC_SPENERGY_VALUES": [0.0] * 4,
    "AEC_HPFONOFF": [1], "AEC_RT60": [0.3], "AEC_ASROUTONOFF": [1], "AEC_ASROUTGAIN": [1.0],
    "AUDIO_MGR_MIC_GAIN": [90.0], "AUDIO_MGR_REF_GAIN": [8.0], "AUDIO_MGR_OP_PACKED": [0, 0],
    "AUDIO_MGR_OP_UPSAMPLE": [1, 1], "AUDIO_MGR_OP_L": [8, 0], "AUDIO_MGR_OP_R": [7, 3],
    "AUDIO_MGR_SYS_DELAY": [12], "DOA_VALUE": [0, 0], "PP_AGCMAXGAIN": [64.0],
    "PP_AGCDESIREDLEVEL": [0.0045], "PP_LIMITONOFF": [0],
}


def parse_value(value_str):
    if value_str.startswith(("0x", "0X")):
        return int(value_str, 16)
    try:
        return float(value_str)
    except ValueError:
        return int(value_str)


def command(value):
    upper = value.upper()
    if upper not in PARAMETERS:
        raise argparse.ArgumentTypeError(f"Invalid command '{value}'. ")
    return upper


def fmt(v):
    if isinstance(v, float):
        return f"{v:.3f}"
    if isinstance(v, str):
        return f"'{v}'"
    return str(v)


def main():
    state_path = Path(os.environ["XVFDIAG_FAKE_STATE"])
    with state_path.with_suffix(".host.log").open("a", encoding="utf-8") as log:
        log.write(json.dumps(sys.argv[1:]) + "\n")
    parser = argparse.ArgumentParser(description="ReSpeaker Host Control Script")
    parser.add_argument("-l", "--list", action="store_true")
    parser.add_argument("COMMAND", nargs="?", type=command)
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=0x2886)
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=0x001A)
    parser.add_argument("--values", nargs="+", type=parse_value)
    args = parser.parse_args()

    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not state.get("present", True):
        print("No device found")
        sys.exit(1)
    count, typ, access = PARAMETERS[args.COMMAND]
    regs = state.setdefault("regs", {})
    if args.values:
        if access == "ro":
            print(f"Error: {args.COMMAND} is read-only and cannot be written to")
            sys.exit(1)
        values = [float(v) for v in args.values] if typ in ("float", "radians") else [int(v) for v in args.values]
        if len(values) != count:
            print(f"Error: {args.COMMAND} value count is {count}, but {len(values)} values provided")
            sys.exit(1)
        print(f"WriteCMD: cmdid: 0, resid: 0, payload: {values}")
        regs[args.COMMAND] = values
        state_path.write_text(json.dumps(state), encoding="utf-8")
    else:
        if access == "wo":
            print(f"Error: {args.COMMAND} is write-only and cannot be read")
            sys.exit(1)
        fw = state["firmware"]
        if args.COMMAND == "VERSION":
            result = [2, 1, 0] if fw.startswith("v2.1.0") else [2, 1, 1]
        elif args.COMMAND == "BLD_MSG":
            result = fw  # upstream iterates char strings, printing ['v', '2', ...]
        elif args.COMMAND == "BLD_REPO_HASH":
            result = "0" * 40
        else:
            result = regs.get(args.COMMAND, DEFAULTS[args.COMMAND])
        print(f"ReadCMD: cmdid: 0, resid: 0, payload: {result}")
        print(f"{args.COMMAND}: [{', '.join(fmt(v) for v in result)}]")
    print("Done!")


if __name__ == "__main__":
    main()

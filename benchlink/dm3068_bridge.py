#!/usr/bin/env python3
"""BenchLink <-> Rigol DM3068 bridge.

Install:  pip install pyvisa pyvisa-py pyusb websockets
          (USB also needs a libusb/WinUSB driver — on Windows install the
           bundled driver package, or use Zadig, or install NI-VISA and set
           RM_BACKEND = '')
Run:      python dm3068_bridge.py
Then in BenchLink: Meter -> Live bridge -> Connect.

If you have NI-VISA or Keysight VISA installed, change RM_BACKEND to ''.

WebSocket protocol (JSON messages, each with an "id" echoed back):
    {op:"list"}                 -> {ok, data:{instruments:[...], all:[...]}}
    {op:"open", resource:"auto"} -> {ok, data:"<*IDN? response>"}
    {op:"query", scpi:"..."}    -> {ok, data:"<response>"}
    {op:"write", scpi:"..."}    -> {ok}
    {op:"close"}                -> {ok}
"""
import asyncio
import json

import pyvisa
import websockets

PORT = 9977
RM_BACKEND = "@py"  # '@py' = pure-python pyvisa-py; '' = installed NI/Keysight VISA

# Rigol Technologies' USB vendor ID and the DM3000-series product ID, taken
# from the bundled Windows driver (DM3000_SERIES.inf: DeviceID =
# "VID_1AB1&PID_0C94"). VISA resource strings look like
#   USB0::0x1AB1::0x0C94::DM3O240800123::INSTR
# so we can pick the DM3068 out of a bench with several USB instruments.
RIGOL_VID = "0X1AB1"
DM3068_PID = "0X0C94"

rm = pyvisa.ResourceManager(RM_BACKEND)
inst = None


def _rank(resource):
    """Sort key so the DM3068 wins: exact VID+PID, then any Rigol, then USB,
    then LAN, then everything else (which we discard)."""
    r = resource.upper()
    if RIGOL_VID in r and DM3068_PID in r:
        return 0
    if RIGOL_VID in r:
        return 1
    if r.startswith("USB"):
        return 2
    if "TCPIP" in r:
        return 3
    return 9


def find_instruments():
    """Return (candidates_best_first, all_resources)."""
    try:
        allres = list(rm.list_resources())
    except Exception as e:  # noqa: BLE001 - list_resources can throw on some backends
        print("list_resources failed:", e)
        allres = []
    cand = sorted((r for r in allres if _rank(r) < 9), key=_rank)
    return cand, allres


def configure(dev):
    """Apply the settings the DM3068 needs to be reliable over pyvisa-py."""
    dev.timeout = 5000
    # USBTMC/TCPIP reads want a newline terminator; without it pyvisa-py can
    # block until timeout (VI_ERROR_TMO). Harmless under NI-VISA.
    try:
        dev.read_termination = "\n"
        dev.write_termination = "\n"
    except Exception:  # noqa: BLE001
        pass
    # The DM3068's lock handling trips pyvisa-py's default lock timeout;
    # 0 disables it (documented DM3068 pyvisa-py fix).
    try:
        dev.lock_timeout = 0
    except Exception:  # noqa: BLE001
        pass
    # DM3068 supports RIGOL / AGILENT / FLUKE command sets; pin RIGOL so
    # BenchLink's native SCPI is interpreted the same every session.
    try:
        dev.write("CMDSET RIGOL")
    except Exception:  # noqa: BLE001
        pass


async def handle(ws):
    global inst
    print("client connected")
    async for raw in ws:
        msg = json.loads(raw)
        rid = msg.get("id")
        try:
            op = msg.get("op")
            if op == "list":
                cand, allres = find_instruments()
                await ws.send(json.dumps({"id": rid, "ok": True,
                                          "data": {"instruments": cand, "all": allres}}))
            elif op == "open":
                if inst is not None:
                    try:
                        inst.close()
                    except Exception:  # noqa: BLE001
                        pass
                    inst = None
                resource = msg.get("resource", "").strip()
                if resource.lower() in ("", "auto"):
                    cand, allres = find_instruments()
                    print("detected:", cand or "nothing", "| all:", allres)
                    if not cand:
                        raise RuntimeError(
                            "no instrument found - is the DM3068 on and the USB driver "
                            "installed? Looking for a Rigol device (USB VID 1AB1, PID 0C94)."
                        )
                    resource = cand[0]
                inst = rm.open_resource(resource)
                configure(inst)
                idn = inst.query("*IDN?").strip()
                if "DM3068" not in idn.upper() and "RIGOL" not in idn.upper():
                    print("warning: opened device does not look like a Rigol DM3068:", idn)
                print("opened:", resource, "->", idn)
                await ws.send(json.dumps({"id": rid, "ok": True, "data": idn}))
            elif op == "query":
                try:
                    data = inst.query(msg["scpi"]).strip()
                except Exception:  # noqa: BLE001
                    # pyvisa-py does not send the USBTMC abort after a read
                    # timeout, so the session otherwise wedges and every later
                    # point times out too. Clear it so the run can recover.
                    try:
                        inst.clear()
                    except Exception:  # noqa: BLE001
                        pass
                    raise
                await ws.send(json.dumps({"id": rid, "ok": True, "data": data}))
            elif op == "write":
                inst.write(msg["scpi"])
                await ws.send(json.dumps({"id": rid, "ok": True}))
            elif op == "close":
                if inst is not None:
                    try:
                        inst.close()
                    except Exception:  # noqa: BLE001
                        pass
                    inst = None
                await ws.send(json.dumps({"id": rid, "ok": True}))
            else:
                await ws.send(json.dumps({"id": rid, "ok": False, "error": "unknown op"}))
        except Exception as e:  # noqa: BLE001
            print("error:", e)
            await ws.send(json.dumps({"id": rid, "ok": False, "error": str(e)}))
    print("client disconnected")


async def main():
    print(f"BenchLink bridge listening on ws://localhost:{PORT}")
    cand, allres = find_instruments()
    if cand:
        print("instruments detected:")
        for r in cand:
            tag = "  <- DM3068" if (RIGOL_VID in r.upper() and DM3068_PID in r.upper()) else ""
            print("   ", r, tag)
    else:
        print("no instruments detected yet — connect the DM3068 and hit Connect in BenchLink.")
    print("Waiting for BenchLink to connect...")
    async with websockets.serve(handle, "localhost", PORT):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())

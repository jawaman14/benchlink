#!/usr/bin/env python3
"""BenchLink <-> Rigol DM3068 bridge.

Install:  pip install pyvisa pyvisa-py pyusb websockets
          (USB also needs a libusb/WinUSB driver — on Windows install the
           bundled driver package in driver/, or use Zadig, or install
           NI-VISA and set RM_BACKEND = '')
Run:      python dm3068_bridge.py
Then in BenchLink: Meter -> Live bridge -> Connect.

If you have NI-VISA or Keysight VISA installed, change RM_BACKEND to ''.

WebSocket protocol (JSON messages, each with an "id" echoed back):
    {op:"list"}                  -> {ok, data:{instruments, all, usb, hint}}
    {op:"open", resource:"auto"} -> {ok, data:"<*IDN? response>"}
    {op:"query", scpi:"..."}     -> {ok, data:"<response>"}
    {op:"write", scpi:"..."}     -> {ok}
    {op:"errors"}                -> {ok, data:["0,\"No error\""]}
    {op:"null", scpi:"...", enable:true} -> {ok, data:{offset, applied}}
    {op:"selftest"}              -> {ok, data:"<*TST? result>"}
    {op:"close"}                 -> {ok}
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
RIGOL_VID_INT = 0x1AB1
DM3068_PID_INT = 0x0C94
RIGOL_VID = "0X1AB1"
DM3068_PID = "0X0C94"

rm = pyvisa.ResourceManager(RM_BACKEND)
inst = None


def parse_usb(resource):
    """Pull (vid, pid, serial) out of a USB VISA resource name, or None.

    VISA writes the ids either in hex ("USB0::0x1AB1::0x0C94::SER::INSTR") or
    in decimal ("USB0::6833::3220::SER::0::INSTR") depending on the backend, so
    both spellings have to normalise to the same device.
    """
    parts = resource.split("::")
    if len(parts) < 4 or not parts[0].upper().startswith("USB"):
        return None
    try:
        vid = int(parts[1], 16) if parts[1].lower().startswith("0x") else int(parts[1])
        pid = int(parts[2], 16) if parts[2].lower().startswith("0x") else int(parts[2])
    except ValueError:
        return None
    return vid, pid, parts[3]


def _rank(resource):
    """Sort key so the DM3068 wins: exact VID+PID, then any Rigol, then USB,
    then LAN, then everything else (which we discard)."""
    ids = parse_usb(resource)
    if ids:
        vid, pid, _ = ids
        if vid == RIGOL_VID_INT and pid == DM3068_PID_INT:
            return 0
        if vid == RIGOL_VID_INT:
            return 1
        return 2
    if "TCPIP" in resource.upper():
        return 3
    return 9


def usb_instruments():
    """Find Rigol USBTMC devices directly with pyusb and build VISA resource
    names from their descriptors.

    Why this exists: pyvisa-py's own USB enumeration silently drops a device
    whose string descriptors it cannot read, which is exactly what happens to
    the DM3068 behind the WinUSB driver on Windows — list_resources() comes
    back empty even though opening the very same resource name works fine.
    Returns (resources, diagnostics).
    """
    out, diag = [], []
    try:
        import usb.core
    except Exception as e:  # noqa: BLE001
        return out, [f"pyusb unavailable ({e}) - install it for USB fallback discovery"]
    try:
        devs = list(usb.core.find(find_all=True, idVendor=RIGOL_VID_INT))
    except Exception as e:  # noqa: BLE001
        return out, [f"pyusb scan failed ({e}) - is a libusb/WinUSB driver installed?"]
    for d in devs:
        pid = d.idProduct
        # Only devices exposing the USBTMC interface (class 0xFE / subclass 3)
        # can speak SCPI over USB.
        tmc = False
        try:
            for cfg in d:
                for intf in cfg:
                    if intf.bInterfaceClass == 0xFE and intf.bInterfaceSubClass == 0x03:
                        tmc = True
        except Exception:  # noqa: BLE001
            pass
        serial = None
        try:
            serial = d.serial_number
        except Exception:  # noqa: BLE001
            serial = None
        if not tmc:
            diag.append(f"Rigol {RIGOL_VID_INT:04X}:{pid:04X} present but exposes no USBTMC "
                        f"interface - wrong driver bound?")
            continue
        if not serial:
            diag.append(f"Rigol {RIGOL_VID_INT:04X}:{pid:04X} found but its serial number could "
                        f"not be read - cannot build a VISA resource name")
            continue
        out.append(f"USB0::0x{RIGOL_VID_INT:04X}::0x{pid:04X}::{serial}::INSTR")
    return out, diag


def find_instruments():
    """Return (candidates_best_first, all_resources, diagnostics).

    Merges pyvisa's own enumeration with the pyusb fallback above.
    """
    diag = []
    try:
        allres = list(rm.list_resources())
    except Exception as e:  # noqa: BLE001
        diag.append(f"list_resources failed: {e}")
        allres = []
    cand = [r for r in allres if _rank(r) < 9]
    usb_found, usb_diag = usb_instruments()
    diag += usb_diag
    for r in usb_found:
        if r not in cand:
            cand.append(r)
            if r not in allres:
                diag.append(f"found via pyusb fallback (pyvisa did not enumerate it): {r}")
    cand.sort(key=_rank)
    # One physical meter can show up twice (pyvisa's decimal spelling plus our
    # hex one). Keep the best-ranked entry per (vid, pid, serial).
    seen, deduped = set(), []
    for r in cand:
        ids = parse_usb(r)
        key = ids if ids else ("raw", r)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped, allres, diag


def configure(dev):
    """Apply the settings the DM3068 needs to be reliable over pyvisa-py."""
    dev.timeout = 8000
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


def drain_errors(dev, limit=12):
    """Read the instrument error queue until it reports no error.

    The DM3068 answers SYST:ERR? with '0,"No error"' when the queue is empty,
    so anything else is a real complaint about a command we sent.
    """
    found = []
    for _ in range(limit):
        try:
            e = dev.query("SYST:ERR?").strip()
        except Exception as ex:  # noqa: BLE001
            found.append(f"error-queue read failed: {ex}")
            break
        if not e or e.startswith("0,"):
            break
        found.append(e)
    return found


async def handle(ws):
    global inst
    print("client connected")
    async for raw in ws:
        msg = json.loads(raw)
        rid = msg.get("id")
        try:
            op = msg.get("op")
            if op == "list":
                cand, allres, diag = find_instruments()
                usb_found, _ = usb_instruments()
                hint = None
                if not cand:
                    hint = ("No instrument found. Check the DM3068 is powered on and its USB "
                            "driver is installed (Rigol VID 1AB1, PID 0C94).")
                elif not allres and usb_found:
                    hint = ("pyvisa could not enumerate the meter, but it was found over raw USB "
                            "and will be opened directly.")
                await ws.send(json.dumps({"id": rid, "ok": True, "data": {
                    "instruments": cand, "all": allres, "usb": usb_found,
                    "diagnostics": diag, "hint": hint}}))
            elif op == "open":
                if inst is not None:
                    try:
                        inst.close()
                    except Exception:  # noqa: BLE001
                        pass
                    inst = None
                resource = msg.get("resource", "").strip()
                if resource.lower() in ("", "auto"):
                    cand, allres, diag = find_instruments()
                    for d in diag:
                        print("  ", d)
                    print("detected:", cand or "nothing", "| pyvisa saw:", allres)
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
                errs = drain_errors(inst)
                if errs:
                    print("instrument errors at open:", errs)
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
            elif op == "errors":
                await ws.send(json.dumps({"id": rid, "ok": True, "data": drain_errors(inst)}))
            elif op == "null":
                # Lead / offset compensation via the meter's REL function.
                # Enabling: take a reading of whatever is currently connected
                # (probes shorted, for lead resistance) and subtract it from
                # every later reading of this function.
                if msg.get("enable", True):
                    reading = float(inst.query(msg.get("scpi", ":MEAS:VOLT:DC?")).strip())
                    inst.write(f":CALCulate:REL:OFFSet {reading:.8E}")
                    inst.write(":CALCulate:REL:STATe ON")
                    applied = inst.query(":CALCulate:REL:OFFSet?").strip()
                    state = inst.query(":CALCulate:REL:STATe?").strip()
                    payload = {"offset": reading, "applied": applied, "state": state,
                               "errors": drain_errors(inst)}
                else:
                    inst.write(":CALCulate:REL:STATe OFF")
                    payload = {"offset": 0, "applied": "0", "state": "OFF",
                               "errors": drain_errors(inst)}
                await ws.send(json.dumps({"id": rid, "ok": True, "data": payload}))
            elif op == "selftest":
                # *TST? runs the meter's internal self-test, which measured
                # ~19 s on a DM3068 — far past the normal timeout. Raise it for
                # this one call, then put it back. Returns 0 on pass.
                prev = inst.timeout
                inst.timeout = 60000
                try:
                    result = inst.query("*TST?").strip()
                finally:
                    inst.timeout = prev
                await ws.send(json.dumps({"id": rid, "ok": True, "data": result,
                                          "passed": result.lstrip("+") == "0"}))
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
    cand, allres, diag = find_instruments()
    for d in diag:
        print("  note:", d)
    if cand:
        print("instruments detected:")
        for r in cand:
            tag = "  <- DM3068" if (RIGOL_VID in r.upper() and DM3068_PID in r.upper()) else ""
            print("   ", r, tag)
    else:
        print("no instruments detected yet — connect the DM3068 and hit Connect in BenchLink.")
    print("Waiting for BenchLink to connect...")
    # ping_interval/ping_timeout drop a dead browser tab instead of leaving the
    # meter session held open by a client that is never coming back.
    async with websockets.serve(handle, "localhost", PORT,
                                ping_interval=20, ping_timeout=20):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())

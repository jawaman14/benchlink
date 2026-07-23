#!/usr/bin/env python3
"""BenchLink <-> Rigol DM3068 bridge.

Install:  pip install pyvisa pyvisa-py pyusb websockets
          (USB also needs a libusb driver — on Windows install it with Zadig
           for the DM3068, or install NI-VISA and set RM_BACKEND = '')
Run:      python dm3068_bridge.py
Then in BenchLink: Devices -> Live bridge -> Connect.

If you have NI-VISA or Keysight VISA installed, change RM_BACKEND to ''.
"""
import asyncio
import json

import pyvisa
import websockets

PORT = 9977
RM_BACKEND = "@py"  # '@py' = pure-python pyvisa-py; '' = installed NI/Keysight VISA

rm = pyvisa.ResourceManager(RM_BACKEND)
inst = None


async def handle(ws):
    global inst
    print("client connected")
    async for raw in ws:
        msg = json.loads(raw)
        rid = msg.get("id")
        try:
            op = msg.get("op")
            if op == "open":
                if inst is not None:
                    inst.close()
                resource = msg["resource"].strip()
                if resource.lower() in ("", "auto"):
                    found = [r for r in rm.list_resources() if "USB" in r or "TCPIP" in r]
                    print("detected:", found or "nothing")
                    if not found:
                        raise RuntimeError("no instrument found - is the DM3068 on and the USB driver installed?")
                    resource = found[0]
                inst = rm.open_resource(resource)
                inst.timeout = 5000
                # DM3068 supports RIGOL / AGILENT / FLUKE command sets; pin RIGOL
                # so BenchLink's native SCPI is interpreted the same every session.
                try:
                    inst.write("CMDSET RIGOL")
                except Exception:  # noqa: BLE001
                    pass
                idn = inst.query("*IDN?").strip()
                print("opened:", idn)
                await ws.send(json.dumps({"id": rid, "ok": True, "data": idn}))
            elif op == "query":
                await ws.send(json.dumps({"id": rid, "ok": True, "data": inst.query(msg["scpi"]).strip()}))
            elif op == "write":
                inst.write(msg["scpi"])
                await ws.send(json.dumps({"id": rid, "ok": True}))
            elif op == "close":
                if inst is not None:
                    inst.close()
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
    print("Waiting for BenchLink to connect...")
    async with websockets.serve(handle, "localhost", PORT):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())

# BenchLink

A multi-device test station for the **Rigol DM3068** bench multimeter. Define a
board's test points once, then run a batch of boards through them — pass/fail
against per-point limits, live readout, yield and process-capability (Cp/Cpk)
analytics, a live strip-chart recorder, and CSV export throughout.

BenchLink runs entirely in the browser. It talks to a real DM3068 through a
small local Python **bridge**, or in **simulated mode** with realistic fake
readings for training and trying flows without hardware.

```
┌─────────────┐   WebSocket    ┌──────────────────┐   USB (VISA/SCPI)   ┌─────────┐
│ BenchLink   │ ◄───────────►  │ dm3068_bridge.py │ ◄────────────────►  │ DM3068  │
│ (browser)   │  ws://:9977    │  (pyvisa)        │                     │ meter   │
└─────────────┘                └──────────────────┘                     └─────────┘
```

## Repository layout

| Path | What it is |
|------|------------|
| [`BenchLink.html`](BenchLink.html) | **Standalone single-file app.** Double-click it, or open in any browser — no server needed. This is the one to use day to day. |
| [`benchlink/BenchLink.dc.html`](benchlink/BenchLink.dc.html) | Dev source for the UI (Claude Design component). |
| [`benchlink/support.js`](benchlink/support.js) | Runtime the dev source depends on. |
| [`benchlink/dm3068_bridge.py`](benchlink/dm3068_bridge.py) | The WebSocket ↔ VISA bridge for live hardware. |
| [`driver/`](driver/) | The DM3068 USB driver package (WinUSB/libusb): installers, `.inf`/`.cat`, and `amd64`/`arm64`/`x86` binaries. |

> **Standalone vs. dev source:** `BenchLink.html` is a built copy with
> `support.js` inlined, so it works from a bare `file://` open. The two-file
> version in `benchlink/` is for editing the design; rebuild the standalone
> after changes (see below).

## Quick start (simulated — no hardware)

Just open **[`BenchLink.html`](BenchLink.html)** in a browser. On the **Meter**
screen leave the mode on **Simulated** and hit **Connect**. Add serial numbers
on the **Batch** screen, then **Start run**.

## Running against a real DM3068

1. **Install the USB driver** (once) so the meter enumerates as a WinUSB
   device. Run the matching installer in [`driver/`](driver/)
   (`installer_x64.exe` on 64-bit Windows), or point [Zadig](https://zadig.akeo.ie/)
   at the DM3068 and install WinUSB. If you already have NI-VISA / Keysight
   VISA, you can use that instead (see the bridge notes below).

2. **Install the Python dependencies:**
   ```bash
   pip install pyvisa pyvisa-py pyusb websockets
   ```

3. **Start the bridge:**
   ```bash
   python benchlink/dm3068_bridge.py
   ```
   It prints any instruments it can see and then waits for BenchLink.

4. **Connect in BenchLink:** open the app, go to the **Meter** screen, switch
   to **Live bridge**, leave the VISA resource as `auto`, and hit **Connect**.

The device's USB identity is **VID `1AB1` (Rigol), PID `0C94`** — the bridge
uses this to pick the DM3068 out of a bench that may have several USB
instruments attached.

## The bridge

`dm3068_bridge.py` is a ~150-line WebSocket server that relays SCPI between the
browser and the meter over VISA. It listens on `ws://localhost:9977`.

**Backend:** defaults to pure-Python `pyvisa-py` (`RM_BACKEND = "@py"`). If you
have NI-VISA or Keysight VISA installed, set `RM_BACKEND = ""` to use it.

**Reliability details baked in** (from the DM3068 driver + known pyvisa-py
issues):

- **Targeted auto-detection** by Rigol VID/PID rather than "first USB device."
- **`read_termination = "\n"`** — avoids `VI_ERROR_TMO` read hangs.
- **`lock_timeout = 0`** — works around the DM3068's lock handling under
  pyvisa-py.
- **Timeout recovery** — clears the USBTMC session after a read timeout so a
  run recovers instead of every later point timing out (pyvisa-py doesn't send
  the abort automatically).
- **`CMDSET RIGOL`** — pins the command set so SCPI is interpreted the same
  every session.
- **`*IDN?` verification** — warns if the opened device isn't a Rigol DM3068.

**Protocol** (JSON per message, `id` echoed back):

| Send | Reply |
|------|-------|
| `{op:"list"}` | `{ok, data:{instruments:[…], all:[…]}}` |
| `{op:"open", resource:"auto"}` | `{ok, data:"<*IDN? response>"}` |
| `{op:"query", scpi:"…"}` | `{ok, data:"<response>"}` |
| `{op:"write", scpi:"…"}` | `{ok}` |
| `{op:"close"}` | `{ok}` |

## Rebuilding the standalone file

`BenchLink.html` is generated from the `benchlink/` sources by inlining
`support.js`. After editing the dev source, rebuild it:

```bash
python - <<'PY'
html = open('benchlink/BenchLink.dc.html', encoding='utf-8').read()
js   = open('benchlink/support.js', encoding='utf-8').read()
marker = '<script src="./support.js"></script>'
# Set __resources so the inlined runtime skips re-fetching its own page
# (that self-fetch mis-parses when the script is inlined ahead of <x-dc>).
inline = '<script>window.__resources = {};</script>\n<script>\n' + js + '\n</script>'
open('BenchLink.html', 'w', encoding='utf-8', newline='\n').write(html.replace(marker, inline))
print('rebuilt BenchLink.html')
PY
```

> On first load the app fetches React/Babel from a CDN, so the standalone file
> needs internet access the first time it runs.

## Board templates & CSV

- **Board** screen: define test points (pin, measurement, min/max, unit).
  Export/import templates as CSV. A criteria sheet with
  `Component, Resistance min/max, unit, Voltage min/max` columns is also
  accepted.
- **Results** screen: one row per serial number, one column per point, with the
  criteria appended — export the whole batch as CSV.
- **Plot** screen: record readings to a live strip chart with triggering, hold,
  and CSV export of the recording.

## Credits

Driver package: libusb/WinUSB via [libwdi](https://github.com/pbatard/libwdi)
(© Pete Batard, GNU LGPL). BenchLink UI and bridge built with
[Claude Code](https://claude.com/claude-code).

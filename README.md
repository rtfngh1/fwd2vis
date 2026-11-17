# fwd2vis

Export a [Forward Networks](https://www.forwardnetworks.com/) **Network Map** to:

- **Visio** (`.vsdx`): editable, with no Visio needed to create it
- **PNG**
- **PDF**: vector, zoomable

The map is drawn the way the Forward UI draws it:

- saved device positions
- device-card and port-label settings
- link appearance rules (color, dash, width)
- the BGP layer
- annotations and hidden links
- red outlines on devices that failed collection

It uses a light theme and conventional network-diagram icons.

## Requirements

- Python 3.8 or newer, on macOS, Linux or Windows.
- `matplotlib` and `pillow` (see `requirements.txt`). Without matplotlib, the script still writes the `.vsdx`.
- A Forward API token (Settings → API Tokens) that can read the network.

## Setup

### Option A: pyenv-virtualenv (macOS / Linux)

```bash
cd fwd2vis
pyenv virtualenv 3.13.9 fwd2vis
pyenv local fwd2vis
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### Option B: plain venv

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

## Credentials

`fwd_credentials.py` resolves each value in this order, and the first hit wins:

1. **macOS Keychain**: service `forward-api`, accounts `access-key`, `secret-key`, `network-id`, and optionally `base-url`.
2. **Environment**: `FWD_ACCESS_KEY`, `FWD_SECRET_KEY`, `FWD_NETWORK_ID`, `FWD_BASE_URL` (default `https://fwd.app`).
3. **The CREDENTIALS block** at the top of `fwd2vis.py`. It ships blank; don't commit a key.
4. **An interactive prompt.** The secret isn't echoed.

Run `python fwd_credentials.py --setup` for the exact Keychain and environment commands, or `python fwd_credentials.py` to see what currently resolves. The key is never printed.

On-prem Forward: set the base URL (Keychain account `base-url` or `FWD_BASE_URL`). For a TLS-intercepting proxy, point `FWD_CA_BUNDLE` at a PEM bundle.

## Run

```bash
python fwd2vis.py                                  # list the maps in the network
python fwd2vis.py --map "Core DC"                  # export to ~/Downloads
python fwd2vis.py -m "Core DC" -o ~/Desktop/maps   # export to another folder
python fwd2vis.py --allmaps -o ~/Desktop/maps      # export every map in the network
```

`--allmaps` exports every map from the same snapshot. If two maps share a name, the map ID is added to the file name. Maps with no devices are skipped. If one map fails, the rest still export, and the run ends with a summary (exit code 1 if anything failed).

The files are named `<map name>_<snapshotId>.vsdx`, `.png` and `.pdf`. The map name isn't case-sensitive, and the output folder is created if it's missing.

Command-line options override the CONFIG block, so you never have to edit the file. For the output folder, the order is: `--output`, then the `FWD2VIS_OUTPUT` environment variable, then `OUTPUT_DIR` in CONFIG (default `~/Downloads`). Run `python fwd2vis.py --help` to see the options.

## CONFIG reference

| Setting | Default | Meaning |
|---|---|---|
| `MAP_NAME` | `""` | Map to export (`--map` overrides it). Leave blank to list the maps. |
| `SNAPSHOT_ID` | `""` | Blank means the latest processed snapshot. |
| `OUTPUT_DIR` | `~/Downloads` | Output folder. It's created if missing. `FWD2VIS_OUTPUT` and `--output` override it. |
| `OUTPUT_FORMATS` | `["vsdx","png","pdf"]` | Any subset of these. |
| `PNG_DPI` | `120` | PNG resolution, capped at 30,000 px per side. |
| `MAP_PX_PER_INCH` | `500` | Drawing scale. Lower values give a bigger page with bigger labels. |
| `FORCE_BGP_LAYER` | `None` | `True` or `False` overrides the map's own BGP-layer setting. |
| `DRAW_INTERFACE_LABELS` / `DRAW_SUBNET_LABELS` | `True` | Port tags at the link ends, and subnet tags mid-link. |
| `OFFLINE_HAR` | `""` | Render from a browser HAR or saved map JSON instead of calling the API. |

## Notes

- **Uses the UI's map endpoints.** `/api/networks/{id}/maps` and `/api/networks/{id}/maps/{mapId}?view=snapshot` are what the Forward web UI calls. They aren't part of the published Forward API and may change between releases. This was tested against Forward SaaS 26.10.
- **Visio output is plain shapes.** Devices, labels and lines are individual shapes, and links aren't glued to devices.
- **The script prints a `NOTE:`** when a map uses a setting whose exact placement hasn't been checked against a real map yet (annotations, moved cards, hidden links). Compare those parts with the UI.
- **This is not an official Forward Networks product.**

## Icons

Device icons are rendered from [Tabler Icons](https://tabler.io/icons) (MIT License; see `icons/LICENSE-tabler.txt`). `tools/build_icons.py` regenerates them and is only needed to change the icon set; it needs `cairosvg`.

## License

No license has been chosen yet. All rights reserved by the author until one is added.

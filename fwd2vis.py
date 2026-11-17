#!/usr/bin/env python3
"""
fwd2vis.py -- export a Forward Networks Network Map to Visio, PNG and PDF.

Draws the map the way the Forward UI does: saved device positions, the map's
device-card and interface-label settings, link appearance rules, the BGP
layer, annotations and hidden links. Light theme. Device icons are from
Tabler Icons (MIT, see icons/LICENSE-tabler.txt).

    .vsdx  standard library + Pillow     -- editable in Visio
    .png   matplotlib (brings Pillow)
    .pdf   matplotlib, vector, zoomable

Works on macOS, Linux and Windows. Python 3.8+. See README.md for setup.


CREDENTIALS
-----------
Resolved by fwd_credentials.py (in this folder), first hit wins per value:
  1. macOS Keychain, service "forward-api", accounts access-key / secret-key
     / network-id / base-url
  2. Environment: FWD_ACCESS_KEY FWD_SECRET_KEY FWD_NETWORK_ID FWD_BASE_URL
  3. The CREDENTIALS block below (ship blank)
  4. Interactive prompt
`python fwd_credentials.py --setup` prints the setup commands.
FWD_CA_BUNDLE (optional) points at a PEM bundle for TLS-intercepting proxies.


ENDPOINTS
---------
The map endpoints are the ones the Forward web UI itself calls. They are not
in the published Forward API spec and may change between releases.

  GET /api/networks/{networkId}/maps                       map list
  GET /api/networks/{networkId}/snapshots/latestProcessed   (public API)
  GET /api/networks/{networkId}/maps/{mapId}?view=snapshot&snapshotId={id}
  GET /api/snapshots/{snapshotId}/topo?locations             location names


OUTPUT
------
  <OUTPUT_DIR>/<map name>_<snapshotId>.vsdx / .png / .pdf
  OUTPUT_DIR defaults to ~/Downloads; the FWD2VIS_OUTPUT environment
  variable overrides it, and --output overrides both.


COMMAND LINE (optional -- overrides the CONFIG block)
------------
  python fwd2vis.py                          list the maps (if MAP_NAME is blank)
  python fwd2vis.py --map "Core DC"          export that map
  python fwd2vis.py -m "Core DC" -o ~/Desktop/maps
  python fwd2vis.py --allmaps -o ~/Desktop/maps   every map in the network
"""

# =============================================================================
# CONFIG -- edit these
# =============================================================================
MAP_NAME = ""            # exact map name as shown in the UI (case-insensitive).
                         # Blank: list the network's maps and exit.
SNAPSHOT_ID = ""         # blank = latest processed snapshot

OUTPUT_DIR = "~/Downloads"     # any folder; created if missing
OUTPUT_FORMATS = ["vsdx", "png", "pdf"]

PNG_DPI = 120            # PNG resolution. The whole map is drawn at map scale,
                         # so large maps make large PNGs; lower this if needed.
MAP_PX_PER_INCH = 500    # drawing scale: Forward map pixels per inch of page.
                         # Smaller = bigger page and bigger labels relative
                         # to the page.

FONT_FAMILY = ["Roboto", "Arial", "Helvetica", "DejaVu Sans"]   # first installed wins
VISIO_FONT = "Arial"

# Override the map's own settings (None = do what the map says)
FORCE_BGP_LAYER = None          # True / False
DRAW_INTERFACE_LABELS = True    # port name / speed / IP tags at link ends
DRAW_SUBNET_LABELS = True       # subnet tag at the middle of a link

# Offline: read the map from a browser HAR (or a saved map JSON) instead of
# the API. Useful for testing; leave blank for normal use.
OFFLINE_HAR = ""

# CREDENTIALS (tier 3, used only if Keychain and environment are empty).
# Ship blank. A file with a key pasted in must not be committed.
ACCESS_KEY = ""
SECRET_KEY = ""
NETWORK_ID = ""
BASE_URL = "https://fwd.app"
# =============================================================================

import io
import json
import math
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

HERE = os.path.dirname(os.path.abspath(__file__))
ICONS_DIR = os.path.join(HERE, "icons")

# ---------------------------------------------------------------------------
# Layout constants matching the Forward map UI (26.10.x). Units are "map
# pixels": device x/y from the API multiplied by MAP_UNIT_PX.
# ---------------------------------------------------------------------------
MAP_UNIT_PX = 500.0           # 1 API coordinate unit = 500 map px (measured)
NODE_R = 93.3335              # device circle radius (Nb)
CARD_GAP = 200                # icon edge -> device card (X9t)
END_LABEL_START = NODE_R * 2 + 20   # first port tag distance from centre (OQ)
IF_CARD_ALONG = 200           # interface card offset along link (Swe)
IF_CARD_ACROSS = 50           # interface card offset across link (Cwe)
NAME_FONT = 50                # device-card name size (LWs)
CARD_FONT = 40
TAG_FONT = 30
ANNOTATION_FONT = 16          # default annotation fontSize (eUs)
STANDARD_SPEEDS = {100, 1000, 2500, 5000, 10000, 25000, 40000, 50000, 100000,
                   200000, 400000, 800000, 1600000}

DEVICE_DEFAULTS = dict(hidden=False, showName=True, showLocation=True,
                       showPlatform=True, showModel=False, showOs=False,
                       showOsVersion=True, showTags=False,
                       showCollectionIp=False, showManagementIps=False,
                       showStatus=False, showType=False)
INTERFACE_DEFAULTS = dict(hidden=False, showName=True, showSpeed=True,
                          showIp=True, showType=False, showDescription=False,
                          showStatus=False, showMtu=False,
                          showMacAddress=False, showDuplexMode=False,
                          showMembers=False, showNativeVlan=False,
                          showAccessVlan=False, showInboundAcl=False,
                          showOutboundAcl=False, showVrf=False)

# Light theme (Forward light palette where the UI defines one)
C = dict(
    background="#ffffff",
    link="#363636",            # --color-topo-default-icon-link (light)
    ebgp="#1e7b78",            # --color-topo-ebgp (light)
    ibgp="#982250",            # --color-topo-ibgp (light)
    error="#b5372a",           # --color-topo-down-problem-inferred (light)
    node_fill="#ffffff",
    node_ring="#4a4a4a",
    glyph="#2b2b2b",
    card_fill="#f6f8fa",
    card_stroke="#9aa4ae",
    card_name="#1a5fb4",
    card_location="#5f6b76",
    card_text="#2b2b2b",
    tag_fill="#ffffff",
    tag_stroke="#8a949e",
    tag_text="#2b2b2b",
    leader="#9aa4ae",
    annotation_fill="#eef3f8",
    annotation_text="#363636",
)

# Link dash patterns per Forward link type (on/off, in line widths)
TYPE_DASH = {"GRE": (3, 1, 1, 1, 1, 1), "VXLAN": (5, 1, 1, 1),
             "IPSEC": (1, 1, 1, 5)}
DASHED = (5, 5)

warnings = []


def warn(msg):
    if msg not in warnings:
        warnings.append(msg)


# =============================================================================
# Credentials / HTTP
# =============================================================================
def resolve_credentials():
    """Same resolution as the other Forward scripts: fwd_credentials.py."""
    sys.path.insert(0, HERE)
    try:
        import fwd_credentials
    except ImportError:
        raise SystemExit("ERROR: fwd_credentials.py must be in the same folder as fwd2vis.py.")
    finally:
        sys.path.pop(0)
    creds = fwd_credentials.resolve(fallback={
        "access_key": ACCESS_KEY, "secret_key": SECRET_KEY,
        "network_id": NETWORK_ID, "base_url": BASE_URL})
    print(creds.describe())
    return creds.base_url, creds.network_id, creds.basic_auth_header()


OUTPUT_OVERRIDE = None     # set from --output


def output_dir():
    d = os.path.expanduser(os.path.expandvars(
        OUTPUT_OVERRIDE or os.environ.get("FWD2VIS_OUTPUT") or OUTPUT_DIR or "~/Downloads"))
    os.makedirs(d, exist_ok=True)
    return d


class Api:
    def __init__(self, base, auth_header):
        self.base = base
        self.auth = auth_header
        ca = os.environ.get("FWD_CA_BUNDLE")
        self.ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()

    def get(self, path):
        req = urllib.request.Request(self.base + path, headers={
            "Authorization": self.auth, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=300) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code in (401, 403):
                raise SystemExit(f"ERROR: HTTP {e.code} on {path}. Check the API "
                                 f"key and that it can read this network.\n{body}")
            raise SystemExit(f"ERROR: HTTP {e.code} on {path}\n{body}")
        except urllib.error.URLError as e:
            raise SystemExit(f"ERROR: cannot reach {self.base}: {e.reason}")


def load_from_api(all_maps=False):
    """Returns (list of map JSONs, topo, snapshot id). Fetches lazily: the
    list holds callables so --allmaps can carry on past one bad map."""
    base, nid, hdr = resolve_credentials()
    api = Api(base, hdr)
    maps = api.get(f"/api/networks/{nid}/maps").get("maps", [])
    if not all_maps and not MAP_NAME.strip():
        print(f"\nMaps in network {nid}:")
        for m in sorted(maps, key=lambda m: m.get("name", "").lower()):
            print(f"  {m.get('id'):>6}  {m.get('name')}")
        raise SystemExit('\nRun again with --map "<name>" or --allmaps.')
    if all_maps:
        hits = sorted(maps, key=lambda m: m.get("name", "").lower())
        if not hits:
            raise SystemExit(f"Network {nid} has no maps.")
    else:
        hits = [m for m in maps if m.get("name", "").strip().lower() == MAP_NAME.strip().lower()]
        if not hits:
            raise SystemExit(f"ERROR: no map named {MAP_NAME!r}. Run without --map "
                             f"to list the maps.")
        if len(hits) > 1:
            warn(f"{len(hits)} maps are named {MAP_NAME!r}; using id {hits[0]['id']}. "
                 f"--allmaps exports all of them.")
        hits = hits[:1]
    snap = SNAPSHOT_ID.strip() or str(
        api.get(f"/api/networks/{nid}/snapshots/latestProcessed")["id"])
    print(f"network {nid}, snapshot {snap}, {len(hits)} map(s)")
    topo = api.get(f"/api/snapshots/{snap}/topo?locations")
    jobs = [(h, (lambda mid=h["id"]: api.get(
        f"/api/networks/{nid}/maps/{mid}?view=snapshot&snapshotId={snap}"))) for h in hits]
    return jobs, topo, snap


def load_offline(path, all_maps=False):
    doc = json.load(open(path, encoding="utf-8"))
    if "log" not in doc:                      # a saved map JSON
        return [(doc, lambda: doc)], {}, "offline"
    maps, topo, snap = {}, {}, "offline"
    for e in doc["log"]["entries"]:
        url = e["request"]["url"]
        text = e["response"].get("content", {}).get("text")
        if not text:
            continue
        if re.search(r"/maps/[^/?]+\?view=snapshot", url):
            m = json.loads(text)
            maps[m.get("id")] = m             # last response per map wins
            mm = re.search(r"snapshotId=(\d+)", url)
            snap = mm.group(1) if mm else snap
        elif "/topo?locations" in url:
            topo = json.loads(text)
    if not maps:
        raise SystemExit("ERROR: no maps/{id}?view=snapshot response in the HAR.")
    maps = list(maps.values())
    if not all_maps:
        if MAP_NAME.strip():
            maps = [m for m in maps if m.get("name", "").lower() == MAP_NAME.strip().lower()] or maps
        maps = maps[-1:]
    return [(m, (lambda m=m: m)) for m in maps], topo, snap


# =============================================================================
# Scene model -- everything in map pixels, y pointing DOWN (as in the UI)
# =============================================================================
class Box:
    """Rectangle (optionally rounded / rotated) with optional text lines."""
    def __init__(self, cx, cy, w, h, z, fill=None, stroke=None, lw=0.5,
                 dash=None, radius=0, angle=0.0, lines=None, shape="rect", center=False):
        self.cx, self.cy, self.w, self.h, self.z = cx, cy, w, h, z
        self.center = center              # centre multi-line text (annotations)
        self.fill, self.stroke, self.lw, self.dash = fill, stroke, lw, dash
        self.radius, self.angle, self.shape = radius, angle, shape
        self.lines = lines or []          # [(text, size_px, color, bold)]

    def extent(self):
        r = math.hypot(self.w, self.h) / 2 if self.angle else 0
        hw, hh = (r, r) if self.angle else (self.w / 2, self.h / 2)
        return self.cx - hw, self.cy - hh, self.cx + hw, self.cy + hh


class Line:
    def __init__(self, x1, y1, x2, y2, z, color, lw=0.75, dash=None,
                 start_arrow=False, end_arrow=False):
        self.x1, self.y1, self.x2, self.y2, self.z = x1, y1, x2, y2, z
        self.color, self.lw, self.dash = color, lw, dash
        self.start_arrow, self.end_arrow = start_arrow, end_arrow

    def extent(self):
        return (min(self.x1, self.x2), min(self.y1, self.y2),
                max(self.x1, self.x2), max(self.y1, self.y2))


class Icon:
    def __init__(self, cx, cy, half, file, z):
        self.cx, self.cy, self.half, self.file, self.z = cx, cy, half, file, z  # half = glyph half-size

    def extent(self):
        return (self.cx - self.half, self.cy - self.half,
                self.cx + self.half, self.cy + self.half)


Z_ANNOT, Z_LINK, Z_LEADER, Z_TAG, Z_NODE, Z_ICON, Z_CARD, Z_ANNOT_TOP = range(8)


def text_width(text, size):
    """Rough rendered width in map px (Roboto/Arial-ish metrics)."""
    w = 0.0
    for ch in text:
        if ch in "il.,:;|!'1 ":
            w += 0.30
        elif ch in "mwMW@":
            w += 0.85
        elif ch.isupper() or ch.isdigit():
            w += 0.62
        else:
            w += 0.54
    return w * size * 1.1      # headroom: DejaVu (Linux fallback) runs wider than Arial


def screen_angle(dx, dy):
    """Readable text angle (deg, counter-clockwise on the page) for a link
    going (dx, dy) in y-down map space."""
    a = -math.degrees(math.atan2(dy, dx))
    if a > 90:
        a -= 180
    elif a <= -90:
        a += 180
    return a


def hex_color(v, default):
    if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v):
        return v.lower()
    if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{8}", v):
        return v[:7].lower()
    return default


# =============================================================================
# Build the scene from the map JSON
# =============================================================================
# ---------------------------------------------------------------------------
# Device type -> (Tabler icon, caption). Conventional network-diagram symbols.
# ---------------------------------------------------------------------------
ICON_EXACT = {
    "ROUTER": ("arrows-move", None), "SD_WAN": ("arrows-move", "SD-WAN"),
    "BUNDLED_ROUTER": ("arrows-move", None), "K8S_CNI_NODE": ("arrows-move", None),
    "SWITCH": ("arrows-shuffle", None), "OPENFLOW_SWITCH": ("arrows-shuffle", None),
    "TAPI_OPTICAL": ("arrows-shuffle", None), "VSWITCH": ("switch-horizontal", None),
    "FIREWALL": ("wall", None),
    "LOADBALANCER": ("load-balancer", None), "RIVERBED_INTERCEPTOR": ("load-balancer", None),
    "WAN_OPTIMIZER": ("gauge", None), "WIFI_AP": ("wifi", None),
    "CONTROLLER": ("server-cog", None), "HYPERVISOR": ("stack-2", None),
    "ENCRYPTOR": ("shield-lock", None), "SYNTHETIC_ENCRYPTOR": ("shield-lock", None),
    "INTERNET_SERVICE": ("cloud", "INET"), "INTRANET_SERVICE": ("building", None),
    "ADJACENT_NETWORK": ("building", None),
    "MPLS_VPN_SERVICE": ("cloud", "L3"), "L2_VPN_SERVICE": ("cloud", "L2"),
    "CIRCUIT": ("cloud", "WAN"), "MISSING_PEER": ("circle-dashed", None),
    "NIC": ("plug-connected", None), "DDI": ("world-www", None),
    "EDGE_VM": ("cpu", None), "EDGE_NODE": ("cpu", None),
    "RESOURCES": ("stack-3", None), "BUNDLE_NODE": ("stack-2", None),
    "CLOUD_HOST": ("device-desktop", None), "INFERRED_HOST": ("device-desktop", None),
    "L3_HOST": ("device-desktop", None), "MULTICAST_SOURCE": ("device-desktop", None),
    "MULTICAST_RECEIVER": ("broadcast", None),
    "ZSCALER_ZPA": ("cloud-lock", None), "ZSCALER_APP_CONNECTOR": ("cloud-lock", None),
    "ALKIRA_CLOUD": ("cloud", "ALKIRA"), "AZURE_EXPRESS_ROUTE_CIRCUIT": ("plug-connected", None),
}
CLOUD_PREFIX = {"AWS_": "AWS", "AZURE_": "AZURE", "GCP_": "GCP", "IBM_": "IBM"}
ICON_SUFFIX = [            # first match wins, checked on cloud-vendor types
    (("_FIREWALL", "_VIRTUAL_APPLIANCE", "_INTERCEPT_DEPLOYMENT"), "wall"),
    (("_GATEWAY_LB", "_LB", "_LOAD_BALANCER", "_APP_GW", "_FRONTDOOR"), "load-balancer"),
    (("_VPN_GW", "_VPN_GATEWAY", "_P2S_VPN_GATEWAY"), "lock"),
    (("_NAT_GW", "_FLOATING_IP_GATEWAY"), "transfer"),
    (("_INTERNET_GW", "_PUBLIC_GATEWAY"), "world"),
    (("_TRANSIT_GW", "_TRANSIT_GATEWAY", "_VWAN_HUB", "_CLOUD_WAN", "_LOCAL_GW"), "topology-star-3"),
    (("_DIRECT_CONNECT_GW", "_EXPRESS_ROUTE_GATEWAY", "_DIRECT_LINK_GATEWAY", "_VNET_GW"), "plug-connected"),
    (("_RT", "_ROUTE_TABLE", "_CLOUD_ROUTER", "_VNET_RT"), "arrows-move"),
    (("_ROUTE_SERVER",), "route"),
    (("_SERVICE_ENDPOINT", "_SERVICE_ATTACHMENT", "_ENDPOINT_GATEWAY",
      "_PRIVATE_PATH_SERVICE_GATEWAY"), "link"),
    (("_GLOBAL_ACCELERATOR", "_TRAFFIC_DIRECTOR"), "rocket"),
    (("_SUBNET",), "network"),
    (("_INSTANCE",), "server"),
]


def icon_for(dtype):
    """(icon file, caption) for a Forward device type."""
    t = (dtype or "UNKNOWN").upper()
    name, cap = ICON_EXACT.get(t, (None, None))
    if not name:
        for pre, vendor in CLOUD_PREFIX.items():
            if t.startswith(pre):
                for suffixes, icon in ICON_SUFFIX:
                    if t.endswith(suffixes):
                        name = icon
                        break
                else:           # *_CLOUD, *_VPC, *_VNET and anything new
                    name, cap = "cloud", vendor
                break
    if not name:
        if t != "UNKNOWN":
            warn(f"Device type {t} has no icon mapping; drawn with '?'. Add it to ICON_EXACT.")
        name = "question-mark"
    path = os.path.join(ICONS_DIR, name + ".png")
    if not os.path.exists(path):
        raise SystemExit(f"ERROR: icon file missing: {path}. Keep the icons/ folder "
                         f"next to fwd2vis.py.")
    return path, cap


ACRONYMS = {"AWS", "GCP", "IBM", "VPN", "LB", "NAT", "GW", "RT", "VPC", "AP", "MPLS",
            "WAN", "DDI", "NIC", "SD", "VM", "L2", "L3", "IP", "F5", "A10", "HP", "HPE",
            "EOS", "IOS", "XE", "XR", "ASA", "ACI", "OS", "SRX", "NSX", "ZPA", "CXP",
            "WLC", "SSG", "TMOS", "FTD", "FMC", "SDWAN", "ESXI", "ACOS", "128T"}
SPECIAL = {"NXOS": "NX-OS", "PANOS": "PAN-OS", "FORTIOS": "FortiOS", "JUNOS": "Junos",
           "VMWARE": "VMware", "NETSCALER": "NetScaler", "CHECKPOINT": "Check Point",
           "PALO": "Palo", "SD_WAN": "SD-WAN"}


def pretty(enum_value):
    """ARISTA -> Arista, arista_eos -> Arista EOS, SD_WAN -> SD-WAN."""
    if not enum_value:
        return ""
    v = str(enum_value)
    if v.upper() in SPECIAL:
        return SPECIAL[v.upper()]
    words = []
    for w in re.split(r"[_\s]+", v):
        u = w.upper()
        if u in SPECIAL:
            words.append(SPECIAL[u])
        elif u in ACRONYMS or (len(u) <= 2 and u.isalpha()):
            words.append(u)
        else:
            words.append(w[:1].upper() + w[1:].lower())
    return " ".join(words)


def subnet_of(cidr):
    import ipaddress
    try:
        if "/" not in cidr:
            cidr += "/128" if ":" in cidr else "/32"
        return str(ipaddress.ip_interface(cidr).network)
    except ValueError:
        return None


def host_part(cidr):
    ip = cidr.split("/")[0]
    sep = ":" if ":" in ip else "."
    i = ip.rfind(sep)
    return ip[i:] if i >= 0 else None


def speed_label(mbps):
    return (f"{mbps / 1000:g}G" if mbps >= 1000 else f"{mbps}M")


def build_scene(m, topo):
    S = MAP_UNIT_PX
    items = []

    loc_names = {str(l["id"]): l.get("name") for l in (topo or {}).get("locations", [])}
    topo_dev_loc = {k: str(v) for k, v in (topo or {}).get("devices", {}).items()}
    for k, v in (topo or {}).get("flexibleDevices", {}).items():
        if v:
            topo_dev_loc.setdefault(k, str(v[0]))
    if not loc_names:
        warn("No location names available (topo?locations missing); device cards "
             "show no location.")

    devices = {d["name"]: d for d in m.get("devices", [])}
    pos = {n: (d.get("x", 0) * S, d.get("y", 0) * S) for n, d in devices.items()}
    names_by_len = sorted(devices, key=len, reverse=True)

    ifaces = {}
    for blk in m.get("interfaces", []):
        for i in blk.get("interfaces", []):
            ifaces[f"{blk['device']} {i['name']}"] = i

    def port_device(port):
        for n in names_by_len:
            if port == n or port.startswith(n + " "):
                return n
        return port.split(" ")[0]

    # ---------------- settings ----------------
    dev_defaults = {**DEVICE_DEFAULTS, **(m.get("deviceDefaults") or {})}
    if_defaults = {**INTERFACE_DEFAULTS, **(m.get("interfaceDefaults") or {})}
    dev_settings = {s["name"]: s for s in (m.get("deviceSettings") or []) if "name" in s}
    if_settings = {s["name"]: s for s in (m.get("interfaceSettings") or []) if "name" in s}

    def dev_opt(name):
        s = dev_settings.get(name, {})
        return {k: s.get(k, v) for k, v in dev_defaults.items()}

    def if_opt(port):
        s = if_settings.get(port, {})
        return {k: s.get(k, v) for k, v in if_defaults.items()}

    bgp_cfg = m.get("bgpSettings") or {}
    bgp_on = bool(bgp_cfg.get("enabled")) if FORCE_BGP_LAYER is None else FORCE_BGP_LAYER

    rules = [r for r in (m.get("linkAppearanceRules") or []) if not r.get("disabled")]
    for r in rules:
        extra = set(r.get("selector", {})) - {"type", "linkType", "linkSpeedMbps", "peeringType"}
        if extra:
            warn(f"Link rule {r.get('id')} has selector fields this script does not "
                 f"evaluate: {sorted(extra)}. Those conditions are ignored.")
    have_bgp_rule = {r["selector"].get("peeringType") for r in rules
                     if r.get("selector", {}).get("type") == "BGP"}
    for pt in ("INTERNAL", "EXTERNAL"):
        if pt not in have_bgp_rule:
            rules.append({"id": f"default_{pt}", "selector": {"type": "BGP", "peeringType": pt},
                          "strokeStyle": "SOLID", "strokeWidth": 1,
                          "strokeColor": C["ibgp"] if pt == "INTERNAL" else C["ebgp"]})

    def link_speed(lk):
        a = ifaces.get(lk["sourcePort"], {}).get("speedMbps")
        b = ifaces.get(lk["targetPort"], {}).get("speedMbps")
        if a is None and b is None:
            return None
        v = min(a if a is not None else float("inf"), b if b is not None else float("inf"))
        return int(v) if v in STANDARD_SPEEDS else None

    def match_rule(lk):
        """Forward UI semantics: first matching enabled rule wins."""
        sp = None
        for r in rules:
            sel = r.get("selector", {})
            if sel.get("type") == "BGP":
                if lk.get("bgp") and lk["bgp"].get("peeringType") == sel.get("peeringType"):
                    return r
                continue
            if lk.get("bgp"):
                continue
            if "linkType" in sel and sel["linkType"] != lk.get("linkType"):
                continue
            if "linkSpeedMbps" in sel:
                if sp is None:
                    sp = link_speed(lk) or -1
                if sel["linkSpeedMbps"] != sp:
                    continue
            return r
        return None

    # ---------------- hidden links ----------------
    hidden_raw = m.get("hiddenLinks") or []
    hidden_keys = []
    for h in hidden_raw:
        if isinstance(h, dict):
            vals = [v for k, v in h.items() if isinstance(v, str)
                    and k in ("sourcePort", "targetPort", "source", "target", "port1", "port2")]
            if len(vals) >= 2:
                hidden_keys.append(frozenset(vals[:2]))
        elif isinstance(h, str):
            hidden_keys.append(h)
        elif isinstance(h, (list, tuple)) and len(h) == 2:
            hidden_keys.append(frozenset(h))

    def is_hidden(lk):
        k = frozenset((lk["sourcePort"], lk["targetPort"]))
        for h in hidden_keys:
            if isinstance(h, frozenset) and h == k:
                return True
            if isinstance(h, str) and lk["sourcePort"] in h and lk["targetPort"] in h:
                return True
        return False

    # ---------------- links ----------------
    links, seen = [], set()
    n_hidden = 0
    for lk in m.get("links", []):
        k = frozenset((lk["sourcePort"], lk["targetPort"]))
        if k in seen:
            continue
        seen.add(k)
        a, b = port_device(lk["sourcePort"]), port_device(lk["targetPort"])
        if a not in pos or b not in pos or a == b:
            continue
        if hidden_keys and is_hidden(lk):
            n_hidden += 1
            continue
        links.append({**lk, "a": a, "b": b})
    if hidden_raw:
        if n_hidden != len(hidden_raw):
            warn(f"hiddenLinks has {len(hidden_raw)} entries but {n_hidden} matched a "
                 f"link. Format not verified against a real sample; first entry: "
                 f"{json.dumps(hidden_raw[0])[:200]}")
    if bgp_on:
        for bl in m.get("bgpLinks") or []:
            if bl.get("peeringType") == "UNKNOWN_LINK":
                continue
            a, b = bl["node1"]["deviceName"], bl["node2"]["deviceName"]
            if a in pos and b in pos and a != b:
                links.append({"a": a, "b": b, "bgp": bl, "linkType": "BGP",
                              "sourcePort": None, "targetPort": None})

    # parallel-link offsets
    groups = {}
    for lk in links:
        groups.setdefault(frozenset((lk["a"], lk["b"])), []).append(lk)
    PAR_GAP = 45
    for g in groups.values():
        n = len(g)
        for i, lk in enumerate(g):
            lk["offset"] = (i - (n - 1) / 2) * PAR_GAP

    def tag(cx, cy, text, angle, z=Z_TAG, size=TAG_FONT, color=None):
        w = text_width(text, size) + size * 0.7
        h = size * 1.45
        items.append(Box(cx, cy, w, h, z, fill=C["tag_fill"], stroke=C["tag_stroke"],
                         lw=0.35, radius=h * 0.25, angle=angle,
                         lines=[(text, size, color or C["tag_text"], False)]))
        return w

    def tag_row(texts, ox, oy, ux, uy, start):
        """Tags laid end to end along direction (ux,uy) from (ox,oy)."""
        d = start
        ang = screen_angle(ux, uy)
        for t in texts:
            w = text_width(t, TAG_FONT) + TAG_FONT * 0.7
            tag(ox + ux * (d + w / 2), oy + uy * (d + w / 2), t, ang)
            d += w + 10

    def card_lines_for_iface(i, o):
        out = []
        if o["showType"] and i.get("type"):
            out.append("Type: " + i["type"])
        if o["showDescription"] and i.get("description"):
            out.append("Description: " + i["description"])
        if o["showStatus"]:
            out.append("Status: " + ("down" if i.get("isDown") else "up"))
        if o["showMtu"] and i.get("mtu") is not None:
            out.append(f"MTU: {i['mtu']}")
        if o["showMacAddress"] and i.get("macAddress"):
            out.append("MAC address: " + i["macAddress"])
        if o["showDuplexMode"]:
            out.append("Duplex: " + ("full" if i.get("fullDuplex") else "half"))
        if o["showMembers"] and i.get("memberPorts"):
            out.append("Members: " + ", ".join(i["memberPorts"]))
        if o["showNativeVlan"] and i.get("nativeVlan"):
            out.append(f"Native VLAN: {i['nativeVlan']}")
        if o["showAccessVlan"] and i.get("accessVlan"):
            out.append(f"Access VLAN: {i['accessVlan']}")
        if o["showInboundAcl"] and i.get("inAclNames"):
            out.append("Inbound ACL: " + ", ".join(i["inAclNames"]))
        if o["showOutboundAcl"] and i.get("outAclNames"):
            out.append("Outbound ACL: " + ", ".join(i["outAclNames"]))
        if o["showVrf"] and i.get("vrfName"):
            out.append("VRF: " + i["vrfName"])
        return out

    for lk in links:
        (x1, y1), (x2, y2) = pos[lk["a"]], pos[lk["b"]]
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy) or 1.0
        ux, uy = dx / L, dy / L
        px, py = -uy, ux
        off = lk["offset"]
        x1o, y1o, x2o, y2o = x1 + px * off, y1 + py * off, x2 + px * off, y2 + py * off

        rule = match_rule(lk)
        color = hex_color(rule.get("strokeColor"), C["link"]) if rule else C["link"]
        if lk.get("bgp") and rule and not rule.get("strokeColor"):
            color = C["ibgp"] if lk["bgp"].get("peeringType") == "INTERNAL" else C["ebgp"]
        dash = TYPE_DASH.get((lk.get("linkType") or "").upper())
        if rule and rule.get("strokeStyle") == "DASHED":
            dash = DASHED
        elif rule and rule.get("strokeStyle") == "SOLID":
            dash = None
        lw = 0.75 * float(rule.get("strokeWidth") or 1) if rule else 0.75

        if L > 2 * NODE_R:
            items.append(Line(x1o + ux * NODE_R, y1o + uy * NODE_R,
                              x2o - ux * NODE_R, y2o - uy * NODE_R, Z_LINK, color, lw, dash))

        if lk.get("bgp"):
            if not (bgp_cfg.get("showAsNumber", True) or bgp_cfg.get("showRouterId", True)):
                continue
            bl = lk["bgp"]
            for node in (bl["node1"], bl["node2"]):
                if node["deviceName"] == lk["a"]:
                    ox, oy, sgn = x1o, y1o, 1
                else:
                    ox, oy, sgn = x2o, y2o, -1
                row = []
                if bgp_cfg.get("showAsNumber", True) and node.get("asNumber") is not None:
                    row.append(f"AS {node['asNumber']}")
                if bgp_cfg.get("showRouterId", True) and node.get("routerId"):
                    row.append(node["routerId"])
                tag_row(row, ox, oy, ux * sgn, uy * sgn, END_LABEL_START)
            continue

        if not DRAW_INTERFACE_LABELS:
            continue
        subnet = None
        for port, (ox, oy), sgn in ((lk["sourcePort"], (x1o, y1o), 1),
                                    (lk["targetPort"], (x2o, y2o), -1)):
            i = ifaces.get(port)
            if not i:
                continue
            o = if_opt(port)
            if o["hidden"]:
                continue
            row = []
            if o["showName"]:
                row.append(i["name"])
            if o["showSpeed"] and (i.get("speedMbps") or 0) > 0:
                row.append(speed_label(i["speedMbps"]))
            if o["showIp"] and i.get("ipAddresses"):
                hp = host_part(i["ipAddresses"][0])
                if hp:
                    row.append(hp)
                subnet = subnet or subnet_of(i["ipAddresses"][0])
            ex, ey = ux * sgn, uy * sgn
            tag_row(row, ox, oy, ex, ey, END_LABEL_START)

            extra = card_lines_for_iface(i, o)
            if extra:
                tx, ty = ox + ex * END_LABEL_START, oy + ey * END_LABEL_START
                w = max(text_width(t, TAG_FONT) for t in extra) + 40
                h = len(extra) * TAG_FONT * 1.5 + 24
                saved = if_settings.get(port, {})
                if saved.get("x") is not None and saved.get("y") is not None:
                    ofx, ofy = saved["x"], saved["y"]
                else:
                    qx, qy = -ey, ex
                    ofx = ex * (IF_CARD_ALONG + w / 2) + qx * (IF_CARD_ACROSS + h / 2)
                    ofy = ey * (IF_CARD_ALONG + w / 2) + qy * (IF_CARD_ACROSS + h / 2)
                items.append(Line(tx, ty, tx + ofx, ty + ofy, Z_LEADER, C["leader"], 0.4))
                items.append(Box(tx + ofx, ty + ofy, w, h, Z_CARD, fill=C["card_fill"],
                                 stroke=C["card_stroke"], lw=0.4, radius=10,
                                 lines=[(t, TAG_FONT, C["card_text"], False) for t in extra]))
        if DRAW_SUBNET_LABELS and subnet:
            tag((x1o + x2o) / 2, (y1o + y2o) / 2, subnet, screen_angle(dx, dy))

    # ---------------- devices ----------------
    for name, d in devices.items():
        cx, cy = pos[name]
        failed = bool(d.get("collectionError") or d.get("processingError"))
        items.append(Box(cx, cy, NODE_R * 2, NODE_R * 2, Z_NODE, shape="ellipse",
                         fill=C["node_fill"], stroke=C["error"] if failed else C["node_ring"],
                         lw=max(0.6, 12 * 72 / MAP_PX_PER_INCH)))
        icon, caption = icon_for(d.get("type"))
        if caption:
            # glyph shifted up, caption underneath, both inside the circle
            items.append(Icon(cx, cy - NODE_R * 0.14, NODE_R * 0.46, icon, Z_ICON))
            cap_size = NODE_R * (0.36 if len(caption) <= 4 else 0.26)
            items.append(Box(cx, cy + NODE_R * 0.50, NODE_R * 1.3, cap_size * 1.2, Z_ICON,
                             lines=[(caption, cap_size, C["glyph"], True)]))
        else:
            items.append(Icon(cx, cy, NODE_R * 0.6, icon, Z_ICON))

        o = dev_opt(name)
        if o["hidden"]:
            continue
        lines = []
        if o["showName"]:
            lines.append((d.get("displayName") or name, NAME_FONT, C["card_name"], False))
        loc = loc_names.get(str(d.get("locationId") or topo_dev_loc.get(name, "")))
        if o["showLocation"] and loc:
            lines.append((loc, CARD_FONT, C["card_location"], False))
        if o["showPlatform"]:
            v = pretty(d.get("vendor"))
            lines.append(("Vendor: " + (f"{v} {d['model']}" if d.get("model") else v),
                          CARD_FONT, C["card_text"], False))
        if o["showModel"] and d.get("model"):
            lines.append(("Model: " + d["model"], CARD_FONT, C["card_text"], False))
        if o["showOs"] and d.get("platform"):
            lines.append(("OS: " + pretty(d["platform"]),
                          CARD_FONT, C["card_text"], False))
        if o["showOsVersion"] and d.get("osVersion"):
            lines.append(("OS version: " + d["osVersion"], CARD_FONT, C["card_text"], False))
        if o["showTags"] and d.get("tags"):
            lines.append(("Tags: " + ", ".join(d["tags"]), CARD_FONT, C["card_text"], False))
        if o["showCollectionIp"] and d.get("collectionIp"):
            lines.append(("Collection IP: " + d["collectionIp"], CARD_FONT, C["card_text"], False))
        if o["showManagementIps"] and d.get("managementIps"):
            lines.append(("Management IP: " + ", ".join(d["managementIps"]),
                          CARD_FONT, C["card_text"], False))
        if o["showStatus"]:
            st = d.get("collectionError") or d.get("processingError")
            lines.append(("Status: " + (st.replace("_", " ").lower() if st else "succeeded"),
                          CARD_FONT, C["error"] if st else C["card_text"], False))
        if o["showType"] and d.get("type"):
            lines.append(("Type: " + pretty(d["type"]), CARD_FONT, C["card_text"], False))
        if not lines:
            continue
        w = max(text_width(t, s) for t, s, _, _ in lines) + 48
        h = sum(s * 1.55 for _, s, _, _ in lines) + 30
        saved = dev_settings.get(name, {})
        if saved.get("x") is not None and saved.get("y") is not None:
            ccx, ccy = cx + saved["x"], cy + saved["y"]
        else:
            ccx, ccy = cx, cy - NODE_R - CARD_GAP - h / 2
        items.append(Line(cx, cy, ccx, ccy, Z_LEADER, C["leader"], 0.5))
        items.append(Box(ccx, ccy, w, h, Z_CARD, fill=C["card_fill"], stroke=C["card_stroke"],
                         lw=0.5, radius=8, lines=lines))

    # ---------------- annotations ----------------
    for a in m.get("annotations") or []:
        t = (a.get("type") or "RECTANGLE").upper()
        x, y = a.get("x", 0), a.get("y", 0)
        dxa, dya = a.get("deltaX", 0), a.get("deltaY", 0)
        stroke = hex_color(a.get("strokeColor"), None) if a.get("strokeColor") else None
        fill = hex_color(a.get("fillColor"), None) if a.get("fillColor") else None
        lw = max(0.3, float(a.get("strokeWidth") or 1) * 72 / MAP_PX_PER_INCH)
        dash = DASHED if a.get("strokeStyle") == "DASHED" else None
        if a.get("strokeStyle") not in (None, "SOLID", "DASHED"):
            stroke = None
        size = a.get("fontSize") or ANNOTATION_FONT
        txt_color = hex_color(a.get("textColor"), C["annotation_text"])
        if t == "LINE":
            items.append(Line(x, y, x + dxa, y + dya, Z_ANNOT_TOP, stroke or C["link"], lw, dash,
                              bool(a.get("startArrow")), bool(a.get("endArrow"))))
            continue
        w, h = abs(dxa), abs(dya)
        cx, cy = x + dxa / 2, y + dya / 2
        lines = [(ln, size, txt_color, False) for ln in wrap(a.get("text") or "", size, w)]
        if t == "TEXT":
            items.append(Box(cx, cy, w, h, Z_ANNOT_TOP, lines=lines, center=True))
        else:
            items.append(Box(cx, cy, w, h, Z_ANNOT, fill=fill, stroke=stroke, lw=lw, dash=dash,
                             shape="ellipse" if t == "ELLIPSE" else "rect", lines=lines,
                             center=True))
        if t not in ("RECTANGLE", "ELLIPSE", "TEXT"):
            warn(f"Annotation type {t} is not known; drawn as a rectangle.")
    if m.get("annotations"):
        warn(f"{len(m['annotations'])} annotation(s) drawn. Annotation placement follows "
             f"the UI code but has not been checked against a real map yet; compare "
             f"with the UI.")
    if dev_settings or if_settings:
        warn("Map has per-device / per-interface card settings. Moved-card positions "
             "follow the UI code but have not been checked against a real map yet; "
             "compare with the UI.")

    items.sort(key=lambda it: it.z)
    return items


def wrap(text, size, width):
    out = []
    for para in str(text).split("\n"):
        cur = ""
        for word in para.split(" "):
            cand = (cur + " " + word).strip()
            if cur and width and text_width(cand, size) > width * 0.95:
                out.append(cur)
                cur = word
            else:
                cur = cand
        out.append(cur)
    return [l for l in out if l] if any(out) else []


def scene_bounds(items, margin=400):
    xs0, ys0, xs1, ys1 = zip(*(it.extent() for it in items))
    return min(xs0) - margin, min(ys0) - margin, max(xs1) + margin, max(ys1) + margin


# =============================================================================
# Icon recolouring and cropping
# =============================================================================
_icon_cache = {}


def icon_png(path, color, opaque_bg=None):
    """Recolour the glyph (any colour, alpha kept) and crop it to a square
    around its drawn area, so Icon.half is the glyph's own half-size.
    With opaque_bg the result is flattened onto that colour: some Visio
    renderers paint a foreign image's transparent pixels with the default
    fill, so the Visio icon carries its own background. The glyph square
    fits inside the node circle, so the background never shows."""
    key = (path, color, opaque_bg)
    if key in _icon_cache:
        return _icon_cache[key]
    from PIL import Image
    im = Image.open(path).convert("RGBA")
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    solid = Image.new("RGBA", im.size, (r, g, b, 255))
    alpha = im.getchannel("A")
    solid.putalpha(alpha)
    x0, y0, x1, y1 = alpha.getbbox() or (0, 0) + im.size
    side = max(x1 - x0, y1 - y0)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    box = (cx - side // 2, cy - side // 2, cx - side // 2 + side, cy - side // 2 + side)
    solid = solid.crop(box)
    if opaque_bg:
        bg = Image.new("RGBA", solid.size,
                       tuple(int(opaque_bg[i:i + 2], 16) for i in (1, 3, 5)) + (255,))
        bg.alpha_composite(solid)
        solid = bg.convert("RGB")
    buf = io.BytesIO()
    solid.save(buf, "PNG")
    _icon_cache[key] = (solid, buf.getvalue())
    return _icon_cache[key]


# =============================================================================
# PNG / PDF via matplotlib
# =============================================================================
def render_matplotlib(items, bounds, paths):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import patches, transforms
    import numpy as np

    matplotlib.rcParams["font.family"] = "sans-serif"
    matplotlib.rcParams["font.sans-serif"] = FONT_FAMILY + matplotlib.rcParams["font.sans-serif"]
    matplotlib.rcParams["pdf.fonttype"] = 42
    import logging
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

    x0, y0, x1, y1 = bounds
    W, H = (x1 - x0) / MAP_PX_PER_INCH, (y1 - y0) / MAP_PX_PER_INCH
    fig = plt.figure(figsize=(W, H), facecolor=C["background"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(x0, x1)
    ax.set_ylim(y1, y0)              # y down, like the UI
    ax.set_aspect("equal")
    ax.axis("off")
    pt = 72.0 / MAP_PX_PER_INCH      # map px -> points

    def ls(dash):
        return (0, dash) if dash else "solid"

    for it in items:
        if isinstance(it, Line):
            ax.plot([it.x1, it.x2], [it.y1, it.y2], color=it.color, lw=it.lw,
                    linestyle=ls(it.dash), solid_capstyle="butt", zorder=it.z)
            for flag, (ax_, ay_), (bx_, by_) in ((it.end_arrow, (it.x1, it.y1), (it.x2, it.y2)),
                                                (it.start_arrow, (it.x2, it.y2), (it.x1, it.y1))):
                if flag:
                    ax.annotate("", xy=(bx_, by_), xytext=(ax_, ay_), zorder=it.z,
                                arrowprops=dict(arrowstyle="-|>", color=it.color, lw=it.lw,
                                                shrinkA=0, shrinkB=0))
        elif isinstance(it, Icon):
            solid, _ = icon_png(it.file, C["glyph"])
            ax.imshow(np.asarray(solid), extent=(it.cx - it.half, it.cx + it.half,
                                                 it.cy + it.half, it.cy - it.half),
                      zorder=it.z, interpolation="antialiased")
        elif isinstance(it, Box):
            if it.fill or it.stroke:
                kw = dict(facecolor=it.fill or "none", edgecolor=it.stroke or "none",
                          lw=it.lw if it.stroke else 0, linestyle=ls(it.dash), zorder=it.z)
                if it.shape == "ellipse":
                    p = patches.Ellipse((it.cx, it.cy), it.w, it.h, **kw)
                else:
                    p = patches.FancyBboxPatch(
                        (it.cx - it.w / 2, it.cy - it.h / 2), it.w, it.h,
                        boxstyle=f"round,pad=0,rounding_size={it.radius}" if it.radius
                        else "square,pad=0", **kw)
                if it.angle:
                    p.set_transform(transforms.Affine2D().rotate_deg_around(
                        it.cx, it.cy, -it.angle) + ax.transData)
                ax.add_patch(p)
            if it.lines:
                if it.angle or len(it.lines) == 1:
                    t, s, col, bold = it.lines[0]
                    ax.text(it.cx, it.cy, t, fontsize=s * pt, color=col, ha="center",
                            va="center_baseline", rotation=it.angle, rotation_mode="anchor",
                            fontweight="bold" if bold else "normal", zorder=it.z + 0.5)
                else:
                    total = sum(s * 1.55 for _, s, _, _ in it.lines)
                    yy = it.cy - total / 2
                    left = it.cx if it.center else it.cx - it.w / 2 + 24
                    for t, s, col, bold in it.lines:
                        lh = s * 1.55
                        ax.text(left, yy + lh / 2, t, fontsize=s * pt, color=col,
                                ha="center" if it.center else "left",
                                va="center_baseline", zorder=it.z + 0.5,
                                fontweight="bold" if bold else "normal")
                        yy += lh

    for fmt, path in paths.items():
        if fmt == "png":
            dpi = PNG_DPI
            longest = max(W, H) * dpi
            if longest > 30000:
                dpi = int(30000 / max(W, H))
                warn(f"PNG would exceed 30000 px; DPI lowered to {dpi}.")
            fig.savefig(path, dpi=dpi, facecolor=C["background"])
        else:
            fig.savefig(path, facecolor=C["background"])
        print(f"wrote {path}")
    plt.close(fig)


# =============================================================================
# VSDX writer (hand-built Open Packaging file, no Visio needed)
# =============================================================================
VNS = "http://schemas.microsoft.com/office/visio/2012/main"
RNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _cell(n, v, u=None):
    if isinstance(v, float):
        v = f"{v:.6f}"
    return f"<Cell N='{n}' V='{xml_escape(str(v), {chr(39): '&apos;'})}'" + (f" U='{u}'" if u else "") + "/>"


def render_vsdx(items, bounds, path, title):
    x0, y0, x1, y1 = bounds
    S = MAP_PX_PER_INCH
    page_w, page_h = (x1 - x0) / S, (y1 - y0) / S

    def X(x):
        return (x - x0) / S

    def Y(y):
        return (y1 - y) / S             # Visio y points up

    have_pil = True
    try:
        import PIL  # noqa
    except ImportError:
        have_pil = False
        warn("Pillow not installed: .vsdx has no device icons. pip install pillow")

    media = {}                           # icon path -> (rid, filename)
    shapes = []
    sid = [0]

    def nid():
        sid[0] += 1
        return sid[0]

    def line_cells(color, lw_pt, dash):
        if color is None:
            return _cell("LinePattern", 0)
        pat = 1
        if dash == DASHED:
            pat = 2
        elif dash == TYPE_DASH["VXLAN"]:
            pat = 4
        elif dash == TYPE_DASH["GRE"]:
            pat = 5
        elif dash == TYPE_DASH["IPSEC"]:
            pat = 3
        return (_cell("LineColor", color) + _cell("LineWeight", lw_pt / 72.0, "PT")
                + _cell("LinePattern", pat))

    def fill_cells(color):
        if color is None:
            return _cell("FillPattern", 0)
        return _cell("FillForegnd", color) + _cell("FillPattern", 1)

    def text_section(lines, align_center):
        if not lines:
            return ""
        chars, txt = [], []
        styles = []
        for t, s, col, bold in lines:
            key = (round(s * 72.0 / S, 3), col, bold)
            if key not in styles:
                styles.append(key)
        for ix, (sz, col, bold) in enumerate(styles):
            chars.append(f"<Row IX='{ix}'>" + _cell("Font", VISIO_FONT) + _cell("Color", col)
                         + _cell("Size", sz / 72.0, "PT") + _cell("Style", 1 if bold else 0) + "</Row>")
        for k, (t, s, col, bold) in enumerate(lines):
            ix = styles.index((round(s * 72.0 / S, 3), col, bold))
            txt.append(f"<cp IX='{ix}'/>" + xml_escape(t) + ("\n" if k < len(lines) - 1 else ""))
        return ("<Section N='Character'>" + "".join(chars) + "</Section>"
                + "<Section N='Paragraph'><Row IX='0'>" + _cell("HorzAlign", 1 if align_center else 0)
                + _cell("SpLine", -1.2) + "</Row></Section>"
                + "<Text>" + "".join(txt) + "</Text>")

    for it in items:
        if isinstance(it, Line):
            bx, by, ex, ey = X(it.x1), Y(it.y1), X(it.x2), Y(it.y2)
            L = math.hypot(ex - bx, ey - by)
            if L < 1e-6:
                continue
            ang = math.atan2(ey - by, ex - bx)
            arrows = (_cell("BeginArrow", 4 if it.start_arrow else 0)
                      + _cell("EndArrow", 4 if it.end_arrow else 0))
            shapes.append(
                f"<Shape ID='{nid()}' Type='Shape' LineStyle='0' FillStyle='0' TextStyle='0'>"
                + _cell("PinX", (bx + ex) / 2) + _cell("PinY", (by + ey) / 2)
                + _cell("Width", L) + _cell("Height", 0.0) + _cell("LocPinX", L / 2)
                + _cell("LocPinY", 0.0) + _cell("Angle", ang)
                + _cell("BeginX", bx) + _cell("BeginY", by) + _cell("EndX", ex) + _cell("EndY", ey)
                + _cell("ObjType", 2) + _cell("OneD", 1)
                + line_cells(it.color, it.lw, it.dash) + arrows + _cell("FillPattern", 0)
                + "<Section N='Geometry' IX='0'>" + _cell("NoFill", 1)
                + "<Row T='MoveTo' IX='1'>" + _cell("X", 0.0) + _cell("Y", 0.0) + "</Row>"
                + "<Row T='LineTo' IX='2'>" + _cell("X", L) + _cell("Y", 0.0) + "</Row>"
                + "</Section></Shape>")
        elif isinstance(it, Icon):
            if not have_pil:
                continue
            if it.file not in media:
                media[it.file] = (f"rId{len(media) + 1}", f"image{len(media) + 1}.png")
            rid = media[it.file][0]
            sz = 2 * it.half / S
            shapes.append(
                f"<Shape ID='{nid()}' Type='Foreign' LineStyle='0' FillStyle='0' TextStyle='0'>"
                + _cell("PinX", X(it.cx)) + _cell("PinY", Y(it.cy)) + _cell("Width", sz)
                + _cell("Height", sz) + _cell("LocPinX", sz / 2) + _cell("LocPinY", sz / 2)
                + _cell("ImgOffsetX", 0.0) + _cell("ImgOffsetY", 0.0)
                + _cell("ImgWidth", sz) + _cell("ImgHeight", sz)
                + _cell("LinePattern", 0) + _cell("LineColor", C["node_fill"])
                + _cell("LineWeight", 0.0) + _cell("FillPattern", 0)
                + f"<ForeignData ForeignType='Bitmap' CompressionType='PNG'><Rel r:id='{rid}'/></ForeignData>"
                + "</Shape>")
        elif isinstance(it, Box):
            w, h = it.w / S, it.h / S
            geom = "<Section N='Geometry' IX='0'>"
            if it.fill is None:
                geom += _cell("NoFill", 1)
            if it.stroke is None:
                geom += _cell("NoLine", 1)
            if it.shape == "ellipse":
                geom += ("<Row T='Ellipse' IX='1'>" + _cell("X", w / 2) + _cell("Y", h / 2)
                         + _cell("A", w) + _cell("B", h / 2) + _cell("C", w / 2) + _cell("D", h)
                         + "</Row>")
            else:
                pts = [(0, 0), (w, 0), (w, h), (0, h), (0, 0)]
                geom += "<Row T='MoveTo' IX='1'>" + _cell("X", 0.0) + _cell("Y", 0.0) + "</Row>"
                for k, (px_, py_) in enumerate(pts[1:], 2):
                    geom += f"<Row T='LineTo' IX='{k}'>" + _cell("X", float(px_)) + _cell("Y", float(py_)) + "</Row>"
            geom += "</Section>"
            centered = it.center or bool(it.angle) or len(it.lines) <= 1
            shapes.append(
                f"<Shape ID='{nid()}' Type='Shape' LineStyle='0' FillStyle='0' TextStyle='0'>"
                + _cell("PinX", X(it.cx)) + _cell("PinY", Y(it.cy)) + _cell("Width", w)
                + _cell("Height", h) + _cell("LocPinX", w / 2) + _cell("LocPinY", h / 2)
                + _cell("Angle", math.radians(it.angle))
                + fill_cells(it.fill) + line_cells(it.stroke, it.lw, it.dash)
                + _cell("Rounding", it.radius / S if it.radius else 0.0)
                + _cell("LeftMargin", 0.0 if centered else 24 / S)
                + _cell("RightMargin", 0.0) + _cell("TopMargin", 0.0) + _cell("BottomMargin", 0.0)
                + _cell("VerticalAlign", 1)
                + geom + text_section(it.lines, centered) + "</Shape>")

    page_xml = (f"<?xml version='1.0' encoding='utf-8' ?><PageContents xmlns='{VNS}' "
                f"xmlns:r='{RNS}' xml:space='preserve'><Shapes>" + "".join(shapes)
                + "</Shapes></PageContents>")
    page_rels = ("<?xml version='1.0' encoding='utf-8' standalone='yes'?>"
                 "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
                 + "".join(f"<Relationship Id='{rid}' Type='http://schemas.openxmlformats.org/"
                           f"officeDocument/2006/relationships/image' Target='../media/{fn}'/>"
                           for rid, fn in media.values())
                 + "</Relationships>")
    pages_xml = (f"<?xml version='1.0' encoding='utf-8' ?><Pages xmlns='{VNS}' xmlns:r='{RNS}' "
                 f"xml:space='preserve'><Page ID='0' NameU='{xml_escape(title)}' "
                 f"Name='{xml_escape(title)}' ViewScale='-1' ViewCenterX='{page_w / 2:.4f}' "
                 f"ViewCenterY='{page_h / 2:.4f}'><PageSheet LineStyle='0' FillStyle='0' TextStyle='0'>"
                 + _cell("PageWidth", page_w, "IN") + _cell("PageHeight", page_h, "IN")
                 + _cell("PageScale", 1.0, "IN") + _cell("DrawingScale", 1.0, "IN")
                 + _cell("DrawingSizeType", 0) + _cell("DrawingScaleType", 0)
                 + _cell("PrintPageOrientation", 2 if page_w > page_h else 1)
                 + "</PageSheet><Rel r:id='rId1'/></Page></Pages>")
    pages_rels = ("<?xml version='1.0' encoding='utf-8' standalone='yes'?>"
                  "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
                  "<Relationship Id='rId1' Type='http://schemas.microsoft.com/visio/2010/relationships/page' "
                  "Target='page1.xml'/></Relationships>")
    style0 = ("<StyleSheet ID='0' NameU='No Style' Name='No Style'>"
              + _cell("EnableLineProps", 1) + _cell("EnableFillProps", 1) + _cell("EnableTextProps", 1)
              + _cell("HideForApply", 0)
              + _cell("LineWeight", 0.01041666666666667) + _cell("LineColor", "#000000")
              + _cell("LinePattern", 1) + _cell("Rounding", 0.0) + _cell("EndArrowSize", 2)
              + _cell("BeginArrow", 0) + _cell("EndArrow", 0) + _cell("LineCap", 0)
              + _cell("BeginArrowSize", 2) + _cell("LineColorTrans", 0.0)
              + _cell("FillForegnd", "#ffffff") + _cell("FillBkgnd", "#ffffff")
              + _cell("FillPattern", 0) + _cell("FillForegndTrans", 0.0) + _cell("FillBkgndTrans", 0.0)
              + _cell("ShdwPattern", 0)
              + _cell("LeftMargin", 0.0) + _cell("RightMargin", 0.0) + _cell("TopMargin", 0.0)
              + _cell("BottomMargin", 0.0) + _cell("VerticalAlign", 1) + _cell("TextBkgnd", 0)
              + _cell("TextDirection", 0)
              + "<Section N='Character'><Row IX='0'>" + _cell("Font", VISIO_FONT)
              + _cell("Color", "#000000") + _cell("Style", 0) + _cell("Size", 0.1111111111111111)
              + "</Row></Section>"
              + "<Section N='Paragraph'><Row IX='0'>" + _cell("HorzAlign", 1) + "</Row></Section>"
              + "</StyleSheet>")
    doc_xml = (f"<?xml version='1.0' encoding='utf-8' ?><VisioDocument xmlns='{VNS}' xmlns:r='{RNS}' "
               "xml:space='preserve'><DocumentSettings TopPage='0' DefaultTextStyle='0' "
               "DefaultLineStyle='0' DefaultFillStyle='0' DefaultGuideStyle='0'>"
               "<GlueSettings>9</GlueSettings><SnapSettings>65847</SnapSettings>"
               "<SnapExtensions>34</SnapExtensions><DynamicGridEnabled>0</DynamicGridEnabled>"
               "<ProtectStyles>0</ProtectStyles><ProtectShapes>0</ProtectShapes>"
               "<ProtectMasters>0</ProtectMasters><ProtectBkgnds>0</ProtectBkgnds>"
               "</DocumentSettings><Colors><ColorEntry IX='0' RGB='#000000'/>"
               "<ColorEntry IX='1' RGB='#FFFFFF'/></Colors>"
               f"<FaceNames><FaceName NameU='{VISIO_FONT}' Name='{VISIO_FONT}'/></FaceNames>"
               "<StyleSheets>" + style0 + "</StyleSheets></VisioDocument>")
    doc_rels = ("<?xml version='1.0' encoding='utf-8' standalone='yes'?>"
                "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
                "<Relationship Id='rId1' Type='http://schemas.microsoft.com/visio/2010/relationships/pages' "
                "Target='pages/pages.xml'/>"
                "<Relationship Id='rId2' Type='http://schemas.microsoft.com/visio/2010/relationships/windows' "
                "Target='windows.xml'/></Relationships>")
    windows_xml = (f"<?xml version='1.0' encoding='utf-8' ?><Windows ClientWidth='1600' "
                   f"ClientHeight='1000' xmlns='{VNS}' xmlns:r='{RNS}' xml:space='preserve'>"
                   "<Window ID='0' WindowType='Drawing' WindowState='1073741824' "
                   "WindowLeft='-8' WindowTop='-31' WindowWidth='1616' WindowHeight='1039' "
                   "ContainerType='Page' Page='0' ViewScale='-1' "
                   f"ViewCenterX='{page_w / 2:.4f}' ViewCenterY='{page_h / 2:.4f}'>"
                   "<ShowRulers>1</ShowRulers><ShowGrid>0</ShowGrid><ShowPageBreaks>0</ShowPageBreaks>"
                   "<ShowGuides>1</ShowGuides><ShowConnectionPoints>1</ShowConnectionPoints>"
                   "<GlueSettings>9</GlueSettings><SnapSettings>65847</SnapSettings>"
                   "<SnapExtensions>34</SnapExtensions><DynamicGridEnabled>0</DynamicGridEnabled>"
                   "<TabSplitterPos>0.5</TabSplitterPos></Window></Windows>")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    core = ("<?xml version='1.0' encoding='utf-8' standalone='yes'?>"
            "<cp:coreProperties xmlns:cp='http://schemas.openxmlformats.org/package/2006/metadata/core-properties' "
            "xmlns:dc='http://purl.org/dc/elements/1.1/' xmlns:dcterms='http://purl.org/dc/terms/' "
            "xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance'>"
            f"<dc:title>{xml_escape(title)}</dc:title><dc:creator>fwd2vis</dc:creator>"
            f"<dcterms:created xsi:type='dcterms:W3CDTF'>{now}</dcterms:created>"
            f"<dcterms:modified xsi:type='dcterms:W3CDTF'>{now}</dcterms:modified>"
            "</cp:coreProperties>")
    app = ("<?xml version='1.0' encoding='utf-8' standalone='yes'?>"
           "<Properties xmlns='http://schemas.openxmlformats.org/officeDocument/2006/extended-properties' "
           "xmlns:vt='http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes'>"
           "<Application>Microsoft Visio</Application><Template></Template>"
           "<AppVersion>16.0000</AppVersion></Properties>")
    ctypes = ("<?xml version='1.0' encoding='utf-8' standalone='yes'?>"
              "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'>"
              "<Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/>"
              "<Default Extension='xml' ContentType='application/xml'/>"
              "<Default Extension='png' ContentType='image/png'/>"
              "<Override PartName='/visio/document.xml' ContentType='application/vnd.ms-visio.drawing.main+xml'/>"
              "<Override PartName='/visio/pages/pages.xml' ContentType='application/vnd.ms-visio.pages+xml'/>"
              "<Override PartName='/visio/pages/page1.xml' ContentType='application/vnd.ms-visio.page+xml'/>"
              "<Override PartName='/visio/windows.xml' ContentType='application/vnd.ms-visio.windows+xml'/>"
              "<Override PartName='/docProps/core.xml' ContentType='application/vnd.openxmlformats-package.core-properties+xml'/>"
              "<Override PartName='/docProps/app.xml' ContentType='application/vnd.openxmlformats-officedocument.extended-properties+xml'/>"
              "</Types>")
    root_rels = ("<?xml version='1.0' encoding='utf-8' standalone='yes'?>"
                 "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
                 "<Relationship Id='rId1' Type='http://schemas.microsoft.com/visio/2010/relationships/document' Target='visio/document.xml'/>"
                 "<Relationship Id='rId2' Type='http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties' Target='docProps/core.xml'/>"
                 "<Relationship Id='rId3' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties' Target='docProps/app.xml'/>"
                 "</Relationships>")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
        z.writestr("visio/document.xml", doc_xml)
        z.writestr("visio/_rels/document.xml.rels", doc_rels)
        z.writestr("visio/windows.xml", windows_xml)
        z.writestr("visio/pages/pages.xml", pages_xml)
        z.writestr("visio/pages/_rels/pages.xml.rels", pages_rels)
        z.writestr("visio/pages/page1.xml", page_xml)
        z.writestr("visio/pages/_rels/page1.xml.rels", page_rels)
        for src, (rid, fn) in media.items():
            z.writestr(f"visio/media/{fn}", icon_png(src, C["glyph"], C["node_fill"])[1])
    print(f"wrote {path}")


# =============================================================================
def parse_args():
    import argparse
    ap = argparse.ArgumentParser(
        description="Export a Forward Networks Network Map to .vsdx / .png / .pdf. "
                    "Options override the CONFIG block at the top of this file.")
    pick = ap.add_mutually_exclusive_group()
    pick.add_argument("-m", "--map", metavar="NAME",
                      help="map name as shown in the UI (case-insensitive); "
                           "omit both --map and --allmaps to list the maps")
    pick.add_argument("--allmaps", action="store_true",
                      help="export every map in the network")
    ap.add_argument("-o", "--output", metavar="DIR",
                    help="output folder (default: OUTPUT_DIR, ~/Downloads); "
                         "created if missing")
    return ap.parse_args()


def export_map(m, topo, snap, out_dir, base_name):
    items = build_scene(m, topo)
    if not items:
        print("  map has no devices; skipped")
        return False
    bounds = scene_bounds(items)
    base = os.path.join(out_dir, base_name)
    fmts = [f.lower().lstrip(".") for f in OUTPUT_FORMATS]
    print(f"  {len(m.get('devices', []))} devices, page "
          f"{(bounds[2] - bounds[0]) / MAP_PX_PER_INCH:.1f} x "
          f"{(bounds[3] - bounds[1]) / MAP_PX_PER_INCH:.1f} in")
    if "vsdx" in fmts:
        render_vsdx(items, bounds, base + ".vsdx", m.get("name") or "Map")
    mpl = {f: base + "." + f for f in fmts if f in ("png", "pdf")}
    if mpl:
        try:
            import matplotlib  # noqa
        except ImportError:
            warn(f"matplotlib not installed: skipped {', '.join(mpl)}. "
                 f"pip install matplotlib")
        else:
            render_matplotlib(items, bounds, mpl)
    return True


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "map"


def main():
    global MAP_NAME, OUTPUT_OVERRIDE
    args = parse_args()
    if args.map is not None:
        MAP_NAME = args.map
    if args.output:
        OUTPUT_OVERRIDE = args.output
    if OFFLINE_HAR.strip():
        jobs, topo, snap = load_offline(OFFLINE_HAR.strip(), args.allmaps)
        print(f"offline: snapshot {snap}, {len(jobs)} map(s)")
    else:
        jobs, topo, snap = load_from_api(args.allmaps)

    for w in warnings:                         # from loading (e.g. duplicate names)
        print("NOTE: " + w)
    out_dir = output_dir()
    names = [safe_name(h.get("name") or f"map_{h.get('id')}") for h, _ in jobs]
    done, skipped, failed = [], [], []
    for (head, fetch), name in zip(jobs, names):
        label = f"{head.get('name')!r} (id {head.get('id')})"
        print(f"\nmap {label}")
        if names.count(name) > 1:              # two maps with the same name
            name = f"{name}_id{head.get('id')}"
        warnings.clear()
        try:
            ok = export_map(fetch(), topo, snap, out_dir, f"{name}_{snap}")
        except SystemExit as e:                # HTTP error etc.
            if not args.allmaps:
                raise
            print(f"  FAILED: {e}")
            failed.append(label)
            continue
        except Exception as e:                 # keep going with --allmaps
            if not args.allmaps:
                raise
            print(f"  FAILED: {type(e).__name__}: {e}")
            failed.append(label)
            continue
        (done if ok else skipped).append(label)
        for w in warnings:
            print("  NOTE: " + w)

    if args.allmaps:
        print(f"\n{len(done)} exported, {len(skipped)} skipped (empty), "
              f"{len(failed)} failed -> {out_dir}")
        for f in failed:
            print(f"  failed: {f}")
        if failed:
            sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
build_icons.py -- maintainer tool, not needed to run fwd2vis.

Downloads Tabler Icons (MIT, https://tabler.io/icons) from the npm registry,
rasterises the outline icons fwd2vis uses to 256x256 black-on-transparent
PNGs in ../icons/, and writes ../icons/LICENSE-tabler.txt.

Needs:  python -m pip install cairosvg   (and the Cairo library: brew install cairo)

Run from the repo root:
    python tools/build_icons.py
"""

import io
import json
import os
import tarfile
import urllib.request

TABLER_VERSION = "3.52.0"
ICONS = [
    "arrows-move", "arrows-shuffle", "switch-horizontal", "wall", "load-balancer",
    "gauge", "wifi", "server-cog", "stack-2", "stack-3", "shield-lock", "cloud",
    "building", "circle-dashed", "question-mark", "device-desktop", "server",
    "plug-connected", "cpu", "world-www", "network", "route", "world",
    "transfer", "lock", "topology-star-3", "link", "rocket", "cloud-lock",
    "broadcast",
]

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "icons")


def main():
    import cairosvg
    url = f"https://registry.npmjs.org/@tabler/icons/-/icons-{TABLER_VERSION}.tgz"
    print(f"downloading {url}")
    data = urllib.request.urlopen(url, timeout=120).read()
    tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    os.makedirs(OUT, exist_ok=True)
    for name in ICONS:
        member = tar.extractfile(f"package/icons/outline/{name}.svg")
        if member is None:
            raise SystemExit(f"icon not found in Tabler {TABLER_VERSION}: {name}")
        svg = member.read()
        cairosvg.svg2png(bytestring=svg, write_to=os.path.join(OUT, f"{name}.png"),
                         output_width=256, output_height=256)
    lic = tar.extractfile("package/LICENSE").read().decode("utf-8")
    with open(os.path.join(OUT, "LICENSE-tabler.txt"), "w", encoding="utf-8") as f:
        f.write(f"Icons in this folder are rendered from Tabler Icons {TABLER_VERSION}\n"
                f"(https://github.com/tabler/tabler-icons), used under the MIT License:\n\n")
        f.write(lic)
    with open(os.path.join(OUT, "SOURCE.json"), "w", encoding="utf-8") as f:
        json.dump({"source": "@tabler/icons", "version": TABLER_VERSION,
                   "style": "outline", "icons": ICONS}, f, indent=1)
    print(f"wrote {len(ICONS)} icons to {OUT}")


if __name__ == "__main__":
    main()

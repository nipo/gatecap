"""Ensure Surfer's prebuilt web (WASM) assets are available for embedding.

Prefers assets already on disk (this package, a $GATECAP_SURFER_ASSETS
override, or the Phase 0 spike's download); only if none are found does it
download the official Surfer VS Code extension (a .vsix, i.e. a zip) from the
VS Marketplace and extract its bundled `surfer/` web build. No Rust build.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parent / "surfer_assets"
# host/ = .../gui/../../../.. ; the Phase 0 spike fetches assets here too.
PHASE0_DIR = Path(__file__).resolve().parents[3] / "gui_phase0" / "surfer_assets"

EXTENSION = "surfer-project.surfer"
QUERY_URL = "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
VSIX_ASSET = "Microsoft.VisualStudio.Services.VSIXPackage"


def _valid(d):
    return d and Path(d).is_dir() and (Path(d) / "index.html").is_file() \
        and list(Path(d).glob("*.wasm"))


def _candidates(dest):
    yield dest
    env = os.environ.get("GATECAP_SURFER_ASSETS")
    if env:
        yield Path(env)
    yield PHASE0_DIR


def _latest_vsix_url():
    payload = {"filters": [{"criteria": [{"filterType": 7, "value": EXTENSION}]}],
               "flags": 914}
    req = urllib.request.Request(
        QUERY_URL, data=json.dumps(payload).encode(),
        headers={"Accept": "application/json;api-version=3.0-preview.1",
                 "Content-Type": "application/json", "User-Agent": "gatecap"},
        method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    version = data["results"][0]["extensions"][0]["versions"][0]
    for f in version["files"]:
        if f["assetType"] == VSIX_ASSET:
            return version["version"], f["source"]
    raise RuntimeError("VSIX asset not found in marketplace response")


def _download(dest):
    _, url = _latest_vsix_url()
    with urllib.request.urlopen(url, timeout=120) as r:
        blob = r.read()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            if name.startswith("extension/surfer/") and not name.endswith("/"):
                out = dest / name[len("extension/surfer/"):]
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(z.read(name))


def ensure(dest=ASSETS_DIR):
    """Return a directory holding the Surfer web build, reusing existing
    assets or downloading them. Raises RuntimeError with guidance if it can
    neither find nor fetch them."""
    dest = Path(dest)
    for cand in _candidates(dest):
        if _valid(cand):
            return Path(cand)
    try:
        _download(dest)
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(
            f"Surfer web assets are missing and could not be downloaded ({e}).\n"
            f"Provide them without a network: copy a Surfer web build (index.html "
            f"+ *.wasm + *.js) into {dest}, or point $GATECAP_SURFER_ASSETS at an "
            f"existing one (e.g. the Phase 0 spike's {PHASE0_DIR}).") from e
    if not _valid(dest):
        raise RuntimeError(f"downloaded assets look incomplete in {dest}")
    return dest


if __name__ == "__main__":
    print("Surfer assets in", ensure())

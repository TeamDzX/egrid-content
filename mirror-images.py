"""Mirror every circuit photo into the content repo.

Hotlinking upload.wikimedia.org worked but is not something to build on: their
infrastructure rate-limited this very sourcing run (HTTP 429), and a URL we do
not control can move or disappear. Copies live in the content repo instead,
served from the same base as the JSON, so images update without an App Store
submission exactly as the rest of the content does.

The licences all permit redistribution; the attribution fields travel with the
image unchanged, which is what actually satisfies them.
"""
import json, os, time, urllib.request
from io import BytesIO
from PIL import Image

UA = "EGrid/1.0 (alextemplet@yahoo.com) content-mirror"
HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(HERE, "images", "circuits")
LONG_EDGE = 1280          # comfortably over a 3x 250pt header
QUALITY = 82

SRC = os.path.join(HERE, "..", "EGrid", "Resources", "circuits.json")
circuits = json.load(open(SRC))["circuits"]
os.makedirs(DEST, exist_ok=True)

manifest, failed = {}, []
for i, c in enumerate(circuits, 1):
    img = c.get("image")
    if not img:
        continue
    url = img["url"]
    if not url.startswith("http"):
        print(f"[{i}] {c['id']}: already local, skipped")
        continue
    out = os.path.join(DEST, f"{c['id']}.jpg")
    if os.path.exists(out):
        manifest[c["id"]] = os.path.getsize(out)
        print(f"[{i}] {c['id']}: cached")
        continue

    data = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            data = urllib.request.urlopen(req, timeout=90).read()
            break
        except Exception as e:
            wait = 4 * (attempt + 1)
            print(f"    retry {c['id']} in {wait}s ({e})", flush=True)
            time.sleep(wait)
    if data is None:
        failed.append(c["id"])
        print(f"[{i}] {c['id']}: FAILED", flush=True)
        continue

    im = Image.open(BytesIO(data))
    # Flatten to RGB: a few sources are PNG/paletted, and the header never
    # needs transparency.
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.thumbnail((LONG_EDGE, LONG_EDGE), Image.LANCZOS)
    im.save(out, "JPEG", quality=QUALITY, optimize=True, progressive=True)
    manifest[c["id"]] = os.path.getsize(out)
    print(f"[{i}] {c['id']}: {im.width}x{im.height}  {os.path.getsize(out)//1024} KB", flush=True)
    time.sleep(1.3)          # stay well under Wikimedia's rate limit

total = sum(manifest.values())
print(f"\nmirrored {len(manifest)} images, {total/1024/1024:.1f} MB total, "
      f"{total//max(1,len(manifest))//1024} KB average")
if failed:
    print("FAILED:", failed)

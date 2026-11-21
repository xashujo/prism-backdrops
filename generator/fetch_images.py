"""Copy images listed in images/sources.txt into the repo.

Each line is   <path inside images/> = <link>
for example    streaming/netflix-cover.webp = https://example.com/some-image.webp

Files that are already in the repo are left alone, so each image is downloaded
once. To refresh one, delete its file from the repo and it comes back on the
next run. Lines starting with # are ignored.
"""
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
IMAGES = ROOT / "images"
LIST = IMAGES / "sources.txt"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
ACCEPT = {".gif": "image/gif,image/*;q=0.8", ".png": "image/png,image/*;q=0.8",
          ".jpg": "image/jpeg,image/*;q=0.8", ".jpeg": "image/jpeg,image/*;q=0.8",
          ".webp": "image/webp,image/*;q=0.8", ".svg": "image/svg+xml,image/*;q=0.8"}


def kind(data):
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return ".avif"
    head = data[:500].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return ".svg"
    return None


def fetch(url, ext):
    host = urllib.parse.urlsplit(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": ACCEPT.get(ext, "image/*"),
        "Referer": f"{host.scheme}://{host.netloc}/"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main():
    if not LIST.is_file():
        print("No images/sources.txt - nothing to copy")
        return
    got = failed = 0
    for n, line in enumerate(LIST.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            print(f"::warning::sources.txt line {n} has no '=': {line}")
            continue
        rel, url = (s.strip() for s in line.split("=", 1))
        dest = (IMAGES / rel).resolve()
        if IMAGES.resolve() not in dest.parents:
            print(f"::warning::sources.txt line {n}: '{rel}' points outside images/")
            continue
        if dest.exists():
            continue
        data, err = None, None
        for attempt in range(3):
            try:
                data = fetch(url, dest.suffix.lower())
                break
            except Exception as e:  # noqa: BLE001
                err = e
                time.sleep(3 * (attempt + 1))
        found = kind(data) if data else None
        if not found:
            failed += 1
            why = err or "the link didn't return an image"
            print(f"::warning::Couldn't copy {rel} from {url} ({why})")
            continue
        if found != dest.suffix.lower() and not (found == ".jpg" and dest.suffix.lower() == ".jpeg"):
            print(f"::notice::{rel} is really a {found[1:].upper()} file (still works, just named {dest.suffix})")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        got += 1
        print(f"Copied {rel} ({len(data) // 1024} KB)")
    print(f"Images: {got} copied, {failed} failed")


if __name__ == "__main__":
    main()

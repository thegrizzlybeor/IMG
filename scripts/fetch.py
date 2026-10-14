#!/usr/bin/env python3
"""
fetch.py <url> <index>

1. downloads the link with gallery-dl (nothing else is used to download)
   - videos / audio / archives are skipped up front (gallery-dl --filter)
   - anything that still slips through is checked by its real content and thrown away
     unless it really is an image
2. converts every image to WebP with the least quality loss that still compresses
     * PNG / BMP / TIFF / GIF ... (lossless sources) -> lossless WebP (zero loss)
     * JPEG / HEIC / AVIF ... (lossy sources)        -> the HIGHEST WebP quality that
       is not bigger than the source (ladder 95, 90, 85, 80, 75, 70), method 6
     * animated GIF / APNG -> animated lossless WebP
     * files that are already WebP are kept as they are (re-encoding only loses quality)
     * if a file cannot be converted for any reason the ORIGINAL is kept (never lost)
3. puts everything in ONE zip (never split) + out/title.txt + out/notes.md
   The zip must stay below GitHub's 2 GiB release-asset limit, otherwise the job fails
   loudly (nothing is uploaded, nothing is split).

Env (all optional):
  MAX_QUALITY=95        highest WebP quality tried for lossy sources (95 / 90 / 85)
  LOSSLESS_SOURCES=keep-lossless | lossy   what to do with PNG / BMP / TIFF / GIF
  COOKIES               contents of a Netscape cookies.txt
  WORKERS               conversion processes (default: number of CPUs)

A local file or folder can be passed instead of a URL (handy for testing the converter).
"""
import collections
import os
import re
import shutil
import subprocess
import sys
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

MB = 1024 * 1024
GIB = 1024 ** 3
ZIP_LIMIT = 2 * GIB - 1024          # GitHub: every release asset must be < 2 GiB
LADDER = (95, 90, 85, 80, 75, 70)   # lossy WebP qualities, best first
WEBP_MAX_SIDE = 16383               # hard limit of the WebP format

IMG_EXT = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".apng", ".gif", ".bmp", ".webp",
           ".tif", ".tiff", ".avif", ".heic", ".heif", ".jxl", ".svg", ".ico"}

# Not images -> never even downloaded. Unknown extensions are NOT listed here on purpose:
# they are downloaded and judged by their real content (e.g. "image.php?id=3" can be a jpeg).
SKIP_EXT = ("mp4", "m4v", "mkv", "webm", "mov", "avi", "flv", "wmv", "3gp", "3g2", "ts", "mts",
            "m2ts", "mpg", "mpeg", "ogv", "m3u8", "mpd",
            "mp3", "m4a", "aac", "wav", "flac", "ogg", "oga", "opus", "wma",
            "zip", "rar", "7z", "tar", "gz", "pdf", "swf")
GDL_FILTER = "(extension or '').lower() not in (" + ", ".join(repr(e) for e in SKIP_EXT) + ")"

# formats that count as "really an image" (checked on content, not on file name)
IMAGE_FORMATS = {"JPEG", "MPO", "PNG", "GIF", "BMP", "DIB", "TIFF", "WEBP", "ICO", "HEIF", "AVIF",
                 "JPEG2000", "PPM", "TGA", "PCX", "SGI", "PSD", "ICNS"}
LOSSY_FORMATS = {"JPEG", "MPO", "HEIF", "AVIF", "JPEG2000"}
FORMAT_EXT = {"JPEG": "jpg", "MPO": "jpg", "PNG": "png", "GIF": "gif", "BMP": "bmp", "DIB": "bmp",
              "TIFF": "tif", "WEBP": "webp", "ICO": "ico", "HEIF": "heic", "AVIF": "avif",
              "JPEG2000": "jp2", "PPM": "ppm", "TGA": "tga", "PCX": "pcx", "SGI": "sgi",
              "PSD": "psd", "ICNS": "icns", "SVG": "svg", "JXL": "jxl"}

TITLE_KEYS = ("album_name", "album_title", "title", "album", "gallery", "folder", "name",
              "subcategory_name")


def log(msg=""):
    print(msg, flush=True)


def mb(n):
    return f"{n / MB:.1f} MB"


def slugify(s, n=60):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("._-")[:n]


def natural(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


# --------------------------------------------------------------------------
# 1. DOWNLOAD (gallery-dl only)
# --------------------------------------------------------------------------
def run_gallery_dl(cmd, tail):
    """Runs gallery-dl, streams its output into the job log, returns the exit code."""
    p = subprocess.Popen([str(c) for c in cmd], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    for line in p.stdout:
        line = line.rstrip()
        tail.append(line)
        log(line)
    return p.wait()


def download(url, dl: Path, tail):
    """Returns True when gallery-dl finished without any error."""
    dl.mkdir(parents=True, exist_ok=True)
    local = Path(url)
    if local.exists():  # local file / folder (testing)
        if local.is_dir():
            shutil.copytree(local, dl, dirs_exist_ok=True)
        else:
            shutil.copy(local, dl)
        return True

    ext = Path(urlparse(url).path).suffix.lower()
    target = "direct://" + url if ext in IMG_EXT else url
    cmd = ["gallery-dl", "-d", dl, "--write-metadata", "--filter", GDL_FILTER,
           "--retries", "6", "--sleep-request", "0.5-1.5"]
    if Path("cookies.txt").exists():
        cmd += ["--cookies", "cookies.txt"]
    rc = 1
    for attempt in range(1, 4):
        # gallery-dl skips files that are already on disk, so a retry only fetches what is missing
        log(f">> gallery-dl (attempt {attempt}/3)")
        rc = run_gallery_dl(cmd + [target], tail)
        if rc == 0:
            return True
        log(f"!! gallery-dl exit code {rc}")
    return False


# --------------------------------------------------------------------------
# 2. IDENTIFY + ORDER + TITLE
# --------------------------------------------------------------------------
def identify(p: Path):
    """Returns an image format name (e.g. 'JPEG') or None when the file is not an image."""
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        register_openers()
        with Image.open(p) as im:
            fmt = (im.format or "").upper()
        if fmt in IMAGE_FORMATS:
            return fmt
    except Exception:
        pass
    try:
        with open(p, "rb") as f:
            head = f.read(4096)
    except OSError:
        return None
    low = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if (low.startswith(b"<svg") or (low.startswith(b"<?xml") and b"<svg" in low[:2048])) \
            and b"<html" not in low[:512]:
        return "SVG"
    if head[:2] == b"\xff\x0a" or head[:12] == b"\x00\x00\x00\x0cJXL \r\n\x87\n":
        return "JXL"
    return None


_REGISTERED = False


def register_openers():
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True
    try:
        import pillow_heif
        for name in ("register_heif_opener", "register_avif_opener"):
            try:
                getattr(pillow_heif, name)()
            except Exception:
                pass
    except Exception:
        pass


def read_meta(p: Path):
    import json
    m = Path(str(p) + ".json")
    try:
        d = json.loads(m.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def title_from(meta: dict) -> str:
    for key in TITLE_KEYS:
        v = meta.get(key)
        if isinstance(v, dict):
            v = v.get("title") or v.get("name")
        if isinstance(v, str) and v.strip():
            return re.sub(r"\s+", " ", v).strip()
    return ""


def sort_key(item):
    p, rel, meta = item
    num = meta.get("num")
    num = num if isinstance(num, int) else 0
    return (natural(str(rel.parent)), num, natural(rel.name))


# --------------------------------------------------------------------------
# 3. CONVERT (runs in worker processes)
# --------------------------------------------------------------------------
def convert(src, dst, fmt, max_q, lossless_mode):
    """
    Returns (status, note):  status 'webp' -> dst was written
                             status 'kept' -> use the original file (note says why)
    Never raises.
    """
    try:
        return _convert(Path(src), Path(dst), fmt, max_q, lossless_mode)
    except BaseException as e:  # noqa: BLE001  (a bug must never lose an image)
        Path(dst).unlink(missing_ok=True)
        return "kept", f"conversion failed ({e.__class__.__name__}: {str(e)[:120]}) -> original kept"


def _convert(src: Path, dst: Path, fmt, max_q, lossless_mode):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = None
    register_openers()
    src_size = src.stat().st_size
    ladder = [q for q in LADDER if q <= max_q] or [max_q]

    if fmt in ("SVG", "JXL"):
        return "kept", f"{fmt} cannot be converted -> original kept"

    with Image.open(src) as im:
        if (im.format or "").upper() == "WEBP":
            return "kept", "already WebP -> kept as is (re-encoding would only lose quality)"

        frames = getattr(im, "n_frames", 1)
        icc = im.info.get("icc_profile")

        # ---- animated GIF / APNG -> animated lossless WebP
        if fmt in ("GIF", "PNG") and frames > 1:
            durations = []
            for i in range(frames):
                im.seek(i)
                d = im.info.get("duration")
                durations.append(int(d) if isinstance(d, (int, float)) and d >= 20 else 100)
            im.seek(0)
            lc = im.info.get("loop")
            if fmt == "GIF":   # no loop block = plays once; N = N extra repeats
                loop = 1 if lc is None else (0 if lc == 0 else int(lc) + 1)
            else:
                loop = int(lc or 0)
            im.save(dst, "WEBP", save_all=True, lossless=True, quality=100, method=6,
                    exact=True, duration=durations, loop=loop)
            with Image.open(dst) as chk:
                if getattr(chk, "n_frames", 1) != frames or chk.size != im.size:
                    dst.unlink(missing_ok=True)
                    return "kept", "animation check failed -> original kept"
            return "webp", f"animated lossless ({frames} frames)"

        # ---- still image
        img = ImageOps.exif_transpose(im)
        img.load()
        mode = img.mode
        if mode in ("RGB", "RGBA"):
            pass
        elif mode in ("P", "PA"):
            alpha = mode == "PA" or "transparency" in img.info
            img = img.convert("RGBA" if alpha else "RGB")
        elif mode in ("L", "LA", "1"):
            img = img.convert("RGBA" if mode == "LA" else "RGB")
            icc = None   # a grey profile does not fit an RGB image
        elif mode == "CMYK":
            rgb = None
            if icc:
                try:
                    import io
                    from PIL import ImageCms
                    rgb = ImageCms.profileToProfile(
                        img, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                        ImageCms.createProfile("sRGB"), outputMode="RGB")
                except Exception:
                    rgb = None
            img = rgb if rgb is not None else img.convert("RGB")
            icc = None
        elif mode.startswith("I") or mode == "F":   # 16 / 32 bit grey
            img = img.convert("I").point(lambda i: i * (1 / 256)).convert("L").convert("RGB")
            icc = None
        else:
            img = img.convert("RGBA" if "A" in mode else "RGB")
            icc = None
        img.info.pop("icc_profile", None)   # Pillow would re-attach a stale profile on its own
        if icc and img.mode in ("RGB", "RGBA"):
            img.info["icc_profile"] = icc

        note = ""
        if max(img.size) > WEBP_MAX_SIDE:   # the WebP format cannot be larger than this
            img.thumbnail((WEBP_MAX_SIDE, WEBP_MAX_SIDE), Image.LANCZOS)
            note = f" (downscaled to fit WebP's {WEBP_MAX_SIDE}px limit)"
        size = img.size

        lossy_source = fmt in LOSSY_FORMATS
        if lossy_source or lossless_mode == "lossy":
            used = None
            for q in ladder:
                img.save(dst, "WEBP", quality=q, method=6, icc_profile=icc)
                used = q
                if dst.stat().st_size <= src_size:
                    break
            how = f"lossy q{used}"
        else:
            img.save(dst, "WEBP", lossless=True, quality=100, method=6, exact=True, icc_profile=icc)
            how = "lossless"

    with Image.open(dst) as chk:
        chk.load()
        if chk.size != size:
            dst.unlink(missing_ok=True)
            return "kept", "size check failed -> original kept"
    if dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        return "kept", "empty output -> original kept"
    return "webp", how + note


# --------------------------------------------------------------------------
def write_summary(row):
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("| Link | Images | WebP | Kept | Before | After |\n|---|---|---|---|---|---|\n"
                    + row + "\n")


def fail(out: Path, url, why, tail):
    out.mkdir(parents=True, exist_ok=True)
    (out / "FAILED.txt").write_text(f"{why}\n{url}\n\n" + "\n".join(tail[-40:]), encoding="utf-8")
    log(f"::error::{why}")
    sys.exit(1)


def main():
    url = sys.argv[1].strip()
    idx = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    max_q = int(os.getenv("MAX_QUALITY", "95") or "95")
    lossless_mode = (os.getenv("LOSSLESS_SOURCES", "keep-lossless") or "keep-lossless").strip()
    workers = int(os.getenv("WORKERS", "0") or 0) or (os.cpu_count() or 1)

    u = urlparse(url)
    name = f"{idx:02d}-{slugify(u.netloc + u.path, 50) or 'link'}"
    work = Path("work") / name
    out = Path("out")
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    (work / "dl").mkdir(parents=True)
    if os.getenv("COOKIES"):
        Path("cookies.txt").write_text(os.environ["COOKIES"])

    log(f"=== [{name}] {url}")
    tail = collections.deque(maxlen=60)
    complete = download(url, work / "dl", tail)
    tail = list(tail)

    # ---- keep only real images (checked on content, not on name)
    dl = work / "dl"
    items, dropped, partial = [], [], 0
    for p in sorted(dl.rglob("*")):
        if not p.is_file():
            continue
        if p.name.endswith(".json") and Path(str(p)[:-5]).exists():
            continue                                  # gallery-dl metadata sidecar
        if p.suffix == ".part":
            partial += 1
            continue
        fmt = identify(p)
        if fmt is None:
            dropped.append(p)
            continue
        items.append((p, fmt, p.relative_to(dl), read_meta(p)))
    if dropped:
        log(f"-- skipped {len(dropped)} non-image file(s) (videos / audio / pages / other): "
            + ", ".join(d.name for d in dropped[:10]) + (" ..." if len(dropped) > 10 else ""))
    if partial:
        log(f"::warning::{partial} unfinished download(s) (.part) were ignored")
        complete = False
    if not items:
        fail(out, url, "No images could be downloaded from this link.", tail)

    items.sort(key=lambda it: sort_key((it[0], it[2], it[3])))

    # ---- title
    title = ""
    for _p, _f, _r, meta in items:
        title = title_from(meta)
        if title:
            break
    if not title:
        title = Path(u.path).name or u.netloc or f"link {idx}"
    title = title.replace("\r", " ").replace("\n", " ")[:100].strip() or f"link {idx}"

    # ---- convert + zip (one zip, never split)
    zname = (slugify(title, 80) if re.search(r"[A-Za-z0-9]", slugify(title, 80)) else f"images_{idx}") + ".zip"
    zpath = out / zname
    tmp = work / "webp"
    tmp.mkdir(parents=True, exist_ok=True)
    width = max(4, len(str(len(items))))
    n_webp = n_kept = orig_total = 0
    log(f">> converting {len(items)} image(s) with {workers} worker(s)")

    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED, allowZip64=True) as z, \
            ProcessPoolExecutor(max_workers=workers) as ex:
        futures = []
        for n, (p, fmt, rel, _m) in enumerate(items, 1):
            futures.append(ex.submit(convert, str(p), str(tmp / f"{n:0{width}d}.webp"),
                                     fmt, max_q, lossless_mode))
        for n, ((p, fmt, rel, _m), fut) in enumerate(zip(items, futures), 1):
            orig = p.stat().st_size
            orig_total += orig
            try:
                status, note = fut.result()
            except BaseException as e:  # noqa: BLE001  (worker died, e.g. out of memory)
                status, note = "kept", f"worker failed ({e.__class__.__name__}) -> original kept"
            stem = slugify(p.stem, 40) or "image"
            webp = tmp / f"{n:0{width}d}.webp"
            if status == "webp" and webp.exists():
                arc = f"{n:0{width}d}_{stem}.webp"
                z.write(webp, arc)
                new = webp.stat().st_size
                webp.unlink()
                n_webp += 1
            else:
                webp.unlink(missing_ok=True)
                ext = FORMAT_EXT.get(fmt) or p.suffix.lstrip(".").lower() or "img"
                arc = f"{n:0{width}d}_{stem}.{ext}"
                z.write(p, arc)
                new = orig
                n_kept += 1
            p.unlink(missing_ok=True)
            Path(str(p) + ".json").unlink(missing_ok=True)
            log(f"[{n}/{len(items)}] {p.name}: {mb(orig)} -> {mb(new)}  ({note})")

    zsize = zpath.stat().st_size
    if zsize > ZIP_LIMIT:
        zpath.unlink()
        fail(out, url, f"The zip would be {zsize / GIB:.2f} GiB. GitHub releases accept less than 2 GiB per "
                       "file and the zip is never split, so nothing was uploaded.", tail)

    note_lines = [f"Source: {url}", "",
                  f"- Images: {len(items)} ({n_webp} converted to WebP"
                  + (f", {n_kept} kept in original format" if n_kept else "") + ")",
                  f"- Size: {mb(orig_total)} -> {mb(zsize)} zipped"]
    if not complete:
        note_lines += ["", "WARNING: gallery-dl reported errors, some images may be missing."]
        log("::warning::gallery-dl reported errors; the archive may be incomplete")
    (out / "notes.md").write_text("\n".join(note_lines) + "\n", encoding="utf-8")
    (out / "title.txt").write_text(title, encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)

    log(f"=== done: {len(items)} image(s), {mb(orig_total)} -> {mb(zsize)}  [{zname}]  \"{title}\"")
    write_summary(f"| {url} | {len(items)} | {n_webp} | {n_kept} | {mb(orig_total)} | {mb(zsize)} |")


if __name__ == "__main__":
    main()

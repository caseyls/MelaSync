#!/usr/bin/env python3
"""MelaSync: turn a *copy* of Mela's local database into a static cooking website.

    python3 build_site.py inspect          # snapshot the DB and print its structure
    python3 build_site.py build --out DIR  # snapshot the DB and generate the site

Safety: Mela's own files are only ever opened with open(..., "rb") to copy them.
Every SQLite call goes to the private snapshot under ~/Library/Caches/MelaSync.

Compatible with the Python 3.9 that ships with Apple's Command Line Tools.
Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import plistlib
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import zlib
from pathlib import Path

CORE_DATA_EPOCH = 978307200  # 2001-01-01 UTC
HOME = Path.home()
CACHE = Path(os.environ.get("MELASYNC_CACHE", HOME / "Library/Caches/MelaSync"))
SITE_TITLE = os.environ.get("MELASYNC_TITLE", "Recipes")
IMG_FULL, IMG_THUMB = 1400, 480


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- locate + snapshot

def find_db(override: str | None) -> Path:
    override = override or os.environ.get("MELA_DB_PATH", "").strip()
    if override:
        p = Path(override).expanduser()
        if not p.is_file():
            die(f"MELA_DB_PATH points to {p}, which does not exist.")
        return p
    root = HOME / "Library/Group Containers"
    try:
        hits = sorted(d / "Data/Curcuma.sqlite" for d in root.iterdir()
                      if "recipes.mela" in d.name and (d / "Data/Curcuma.sqlite").is_file())
    except PermissionError:
        die("macOS blocked access to ~/Library/Group Containers. Allow the 'access data from other apps' "
            "prompt, or add the app running this script (Shortcuts / Terminal) to "
            "System Settings > Privacy & Security > Full Disk Access.")
    except FileNotFoundError:
        hits = []
    if not hits:
        die("Mela's database was not found under ~/Library/Group Containers/*recipes.mela*/Data/Curcuma.sqlite. "
            "Open Mela once on this Mac so it syncs from iCloud, or set MELA_DB_PATH.")
    if len(hits) > 1:
        die("Several Mela databases found; set MELA_DB_PATH to one of:\n  " + "\n  ".join(map(str, hits)))
    return hits[0]


def _stat(p: Path):
    try:
        s = p.stat()
        return (s.st_size, s.st_mtime_ns)
    except FileNotFoundError:
        return None


def _copy(src: Path, dst: Path) -> None:
    # Plain byte copy; the source is opened read-only and nothing else is done to it.
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        shutil.copyfileobj(fi, fo, 1024 * 1024)


def snapshot(src: Path) -> tuple[Path, Path]:
    """Copy Curcuma.sqlite (+ -wal, never -shm) and the external image folder into CACHE/snapshot.

    Retries if Mela writes while we copy. Returns (db_copy, external_data_copy_dir).
    """
    snap = CACHE / "snapshot"
    snap.mkdir(parents=True, exist_ok=True)
    wal, journal = Path(str(src) + "-wal"), Path(str(src) + "-journal")
    dst = snap / "Curcuma.sqlite"
    for attempt in range(5):
        before = [_stat(p) for p in (src, wal, journal)]
        if before[2] and before[2][0] > 0:
            log("Mela is mid-write (rollback journal present); waiting…")
            time.sleep(2)
            continue
        for f in snap.glob("Curcuma.sqlite*"):
            f.unlink()
        _copy(src, dst)
        if before[1] is not None:
            try:
                _copy(wal, Path(str(dst) + "-wal"))
            except FileNotFoundError:
                continue
        if [_stat(p) for p in (src, wal, journal)] == before:
            break
        time.sleep(1 + attempt)
    else:
        die("Mela kept changing its database while it was being copied. Quit Mela or try again in a minute.")

    # External (large) image blobs: mirror changed files only.
    ext_src = src.parent / ".Curcuma_SUPPORT/_EXTERNAL_DATA"
    ext_dst = snap / "_EXTERNAL_DATA"
    ext_dst.mkdir(exist_ok=True)
    if ext_src.is_dir():
        wanted = set()
        for f in ext_src.iterdir():
            if not f.is_file() or f.is_symlink():
                continue
            wanted.add(f.name)
            t = ext_dst / f.name
            if _stat(t) is None or _stat(t)[0] != f.stat().st_size:
                _copy(f, t)
        for t in ext_dst.iterdir():
            if t.name not in wanted:
                t.unlink()
    return dst, ext_dst


def open_copy(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))  # our private copy (SQLite may fold the WAL into it; that's fine)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


# --------------------------------------------------------------------------- schema

class Schema:
    def __init__(self, conn: sqlite3.Connection):
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        self.tables = {n: [r[1] for r in conn.execute(f'PRAGMA table_info("{n}")')] for n in names}
        rc = set(self.tables.get("ZRECIPEOBJECT", []))
        if not {"Z_PK", "ZTITLE"} <= rc:
            die("This doesn't look like a Mela database (no ZRECIPEOBJECT.ZTITLE). Run `inspect` and check the output.")
        self.recipe_cols = rc
        self.join = None
        if {"Z_PK", "ZTITLE"} <= set(self.tables.get("ZRECIPETAG", [])):
            for t, cols in self.tables.items():
                if re.fullmatch(r"Z_\d+TAGS", t):
                    r = [c for c in cols if re.fullmatch(r"Z_\d+RECIPES", c)]
                    g = [c for c in cols if re.fullmatch(r"Z_\d+TAGS", c)]
                    if len(r) == 1 and len(g) == 1:
                        self.join = (t, r[0], g[0])
        self.images = {"ZRECIPE", "ZDATA"} <= set(self.tables.get("ZRECIPEIMAGEOBJECT", []))
        self.image_order = "ZINDEX" if "ZINDEX" in self.tables.get("ZRECIPEIMAGEOBJECT", []) else "Z_PK"

    def col(self, row, name):
        return row[name] if name in self.recipe_cols else None


def inspect(conn: sqlite3.Connection, src: Path, snap: Path) -> None:
    s = Schema(conn)
    print(f"Source (never opened by SQLite): {src}")
    print(f"Snapshot read by this script:    {snap}\n")
    for t, cols in s.tables.items():
        n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        print(f"{t}  ({n} rows)\n    " + ", ".join(cols))
    print("\nCategory join:", s.join or "NOT FOUND")
    print("Images table:", "yes" if s.images else "NOT FOUND")
    row = conn.execute("SELECT * FROM ZRECIPEOBJECT ORDER BY Z_PK LIMIT 1").fetchone()
    if row:
        print("\nFirst recipe, column types (values hidden):")
        for k in row.keys():
            v = row[k]
            print(f"    {k:<20} {type(v).__name__:<8} {'' if v is None else (str(len(v)) + ' chars' if isinstance(v, str) else '')}")


# --------------------------------------------------------------------------- images

def _is_image(d: bytes) -> bool:
    return (d.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF8"))
            or (d.startswith(b"RIFF") and d[8:12] == b"WEBP")
            or d[4:12] in (b"ftypheic", b"ftypheix", b"ftyphevc", b"ftypmif1"))


def _from_keyed_archive(d: bytes) -> bytes | None:
    try:
        arc = plistlib.loads(d)
    except Exception:
        return None
    objs = arc.get("$objects", []) if isinstance(arc, dict) else []
    res = lambda v: objs[v.data] if isinstance(v, plistlib.UID) else v  # noqa: E731
    for o in objs:
        if isinstance(o, dict) and isinstance(o.get("NS.keys"), list):
            for k, v in zip(o["NS.keys"], o.get("NS.objects", [])):
                if res(k) == "CD_data":
                    img = res(v)
                    if isinstance(img, bytes) and _is_image(img):
                        return img
    for o in objs:  # fallback: any embedded image bytes
        if isinstance(o, bytes) and _is_image(o):
            return o
    return None


def decode_blob(blob, ext_dir: Path) -> bytes | None:
    """ZRECIPEIMAGEOBJECT.ZDATA: 0x01 + bytes inline, or 0x02 + filename in _EXTERNAL_DATA."""
    if not blob:
        return None
    d = bytes(blob)
    if d[0] == 1:
        d = d[1:]
    elif d[0] == 2:
        ref = d[1:].split(b"\x00", 1)[0].decode("ascii", "replace")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", ref) or not (ext_dir / ref).is_file():
            return None
        d = (ext_dir / ref).read_bytes()
    if _is_image(d):
        return d
    if d.startswith(b"bvx"):  # LZFSE-compressed archive
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td, "a"), Path(td, "b")
            a.write_bytes(d)
            r = subprocess.run(["compression_tool", "-decode", "-a", "lzfse", "-i", str(a), "-o", str(b)],
                               capture_output=True)
            if r.returncode != 0:
                return None
            d = b.read_bytes()
        if _is_image(d):
            return d
    return _from_keyed_archive(d)


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def make_jpeg(data: bytes, out: Path, max_px: int) -> bool:
    """Resize + convert to JPEG (HEIC/PNG/WebP in -> JPEG out) with macOS `sips`; fall back to a raw copy."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        src = Path(td, "in")
        src.write_bytes(data)
        if _has("sips"):
            r = subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "72",
                                "-Z", str(max_px), str(src), "--out", str(out)], capture_output=True)
            if r.returncode == 0 and out.exists():
                return True
        try:  # non-mac fallback (used for testing only)
            from PIL import Image  # type: ignore
            im = Image.open(src).convert("RGB")
            im.thumbnail((max_px, max_px))
            im.save(out, "JPEG", quality=72)
            return True
        except Exception:
            pass
    if data.startswith(b"\xff\xd8\xff"):
        out.write_bytes(data)
        return True
    return False


# --------------------------------------------------------------------------- text helpers

def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:60] or "recipe"


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


E = html.escape
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_URL = re.compile(r"(?<![\"'=>])\bhttps?://[^\s<]+[^\s<.,;:!?)\]]")


def inline(text: str) -> str:
    """Escape, then a small Markdown subset: links, **bold**, *italic*, bare URLs."""
    keep: list[str] = []

    def stash(h: str) -> str:
        keep.append(h)
        return f"\x00{len(keep) - 1}\x00"

    t = _LINK.sub(lambda m: stash(f'<a href="{E(m[2])}" rel="noreferrer" target="_blank">{E(m[1])}</a>'), text)
    t = _URL.sub(lambda m: stash(f'<a href="{E(m[0])}" rel="noreferrer" target="_blank">{E(m[0])}</a>'), t)
    t = E(t, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", t)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m[1])], t)


def lines_of(block: str | None) -> list[tuple[str, str]]:
    """Mela list fields: one item per line; '#' lines are group headers."""
    out = []
    for raw in (block or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            out.append(("h", line.lstrip("#").strip()))
        else:
            line = re.sub(r"^(?:[-*•]\s+|\d+[.)]\s+)", "", line)
            line = re.sub(r"^\[[ xX]\]\s*", "", line)
            out.append(("i", line))
    return out


def paragraphs(text: str | None) -> str:
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return ""
    parts = re.split(r"\n\s*\n", text)
    return "".join("<p>" + "<br>".join(inline(l) for l in p.split("\n")) + "</p>" for p in parts)


# --------------------------------------------------------------------------- read recipes

def read_recipes(conn: sqlite3.Connection, ext_dir: Path, out: Path) -> list[dict]:
    s = Schema(conn)
    tags: dict[int, list[str]] = {}
    if s.join:
        t, rc, gc = s.join
        for r in conn.execute(f'SELECT j."{rc}" pk, g.ZTITLE title FROM "{t}" j '
                              f'JOIN ZRECIPETAG g ON g.Z_PK = j."{gc}" WHERE g.ZTITLE IS NOT NULL'):
            tags.setdefault(r["pk"], []).append(r["title"])
    else:
        log("warning: no category join table found; categories will be empty")

    img_cache = CACHE / "images"
    img_out = out / "img"
    img_out.mkdir(parents=True, exist_ok=True)
    images: dict[int, list[str]] = {}
    if s.images:
        for r in conn.execute(f"SELECT ZRECIPE pk, ZDATA data FROM ZRECIPEIMAGEOBJECT "
                              f"WHERE ZRECIPE IS NOT NULL ORDER BY ZRECIPE, {s.image_order}"):
            try:
                data = decode_blob(r["data"], ext_dir)
            except Exception as e:  # never let one bad image stop the sync
                log(f"warning: image for recipe {r['pk']} skipped ({e})")
                data = None
            if not data:
                continue
            h = hashlib.sha1(data).hexdigest()[:16]
            ok = True
            for name, px in ((f"{h}.jpg", IMG_FULL), (f"{h}-t.jpg", IMG_THUMB)):
                cached = img_cache / name
                if not cached.exists():
                    ok = make_jpeg(data, cached, px) and ok
                if cached.exists():
                    shutil.copyfile(cached, img_out / name)
            if ok:
                images.setdefault(r["pk"], []).append(h)

    recipes, used = [], set()
    for r in conn.execute("SELECT * FROM ZRECIPEOBJECT"):
        title = (s.col(r, "ZTITLE") or "").strip()
        if not title:
            continue
        rid = str(s.col(r, "ZID") or f"pk{r['Z_PK']}")
        slug = slugify(title)
        if slug in used:
            base = slug
            slug = f"{base}-{hashlib.sha1(rid.encode()).hexdigest()[:6]}"
            if slug in used:  # Mela can hold duplicate copies that share one ZID
                key = rid + "#" + str(r["Z_PK"])
                slug = base + "-" + hashlib.sha1(key.encode()).hexdigest()[:6]
        used.add(slug)
        date = s.col(r, "ZDATE")
        recipes.append({
            "pk": r["Z_PK"], "slug": slug, "title": title,
            "text": s.col(r, "ZTEXT"), "ingredients": s.col(r, "ZINGREDIENTS"),
            "instructions": s.col(r, "ZINSTRUCTIONS"), "notes": s.col(r, "ZNOTES"),
            "nutrition": s.col(r, "ZNUTRITION"), "link": s.col(r, "ZLINK"),
            "yield": s.col(r, "ZYIELD"), "prep": s.col(r, "ZPREPTIME"),
            "cook": s.col(r, "ZCOOKTIME"), "total": s.col(r, "ZTOTALTIME"),
            "fav": bool(s.col(r, "ZFAVORITE")), "want": bool(s.col(r, "ZWANTTOCOOK")),
            "date": (float(date) + CORE_DATA_EPOCH) if isinstance(date, (int, float)) else 0,
            "tags": sorted(set(tags.get(r["Z_PK"], [])), key=str.lower),
            "images": images.get(r["Z_PK"], []),
        })
    recipes.sort(key=lambda x: fold(x["title"]))
    return recipes


# --------------------------------------------------------------------------- render

ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" '
            'fill="#c2410c"/><path d="M12 30h40a20 20 0 0 1-40 0z" fill="#fff"/><path d="M26 22c0-4 4-4 4-8m6 8c0-4 '
            '4-4 4-8" stroke="#fff" stroke-width="3" fill="none" stroke-linecap="round"/></svg>')

ICONS = {
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "tag": '<path d="M3 12V4a1 1 0 0 1 1-1h8l9 9-9 9z"/><circle cx="7.5" cy="7.5" r="1.4"/>',
    "book": '<path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z"/><path d="M5 17a3 3 0 0 1 3-3h11"/>',
    "people": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/>'
              '<path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14a6 6 0 0 1 3.5 6"/>',
    "play": '<circle cx="12" cy="12" r="9"/><path d="M10 8.5l5 3.5-5 3.5z" fill="currentColor"/>',
    "sliders": '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
    "tray": '<path d="M3 13l3-8h12l3 8v6H3z"/><path d="M3 13h5l1 2h6l1-2h5"/>',
    "recent": '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1L3.5 8.5"/><path d="M3.5 3.5v5h5"/><path d="M12 8v4l3 2"/>',
    "want": '<circle cx="12" cy="12" r="9"/><path d="M8 12h8M12.5 8.5L16 12l-3.5 3.5"/>',
    "heart": '<path d="M12 20s-7.5-4.6-7.5-10A4.3 4.3 0 0 1 12 7.3 4.3 4.3 0 0 1 19.5 10c0 5.4-7.5 10-7.5 10z"/>',
    "folder": '<path d="M3 6h6l2 2h10v11H3z"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
    "sidebar": '<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="M9 4.5v15"/>',
    "back": '<path d="M15 5l-7 7 7 7"/>',
    "x": '<path d="M6 6l12 12M18 6L6 18"/>',
    "list": '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r=".6"/><circle cx="4.5" cy="12" r=".6"/>'
            '<circle cx="4.5" cy="18" r=".6"/>',
}
SPRITE = ('<svg width="0" height="0" style="position:absolute" aria-hidden="true">'
          + "".join(f'<symbol id="i-{k}" viewBox="0 0 24 24">{v}</symbol>' for k, v in ICONS.items()) + "</svg>")


def ic(name: str) -> str:
    return f'<svg class=ic aria-hidden=true><use href="#i-{name}"/></svg>'


def head(title: str, extra: str = "") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#1c1c1e" media="(prefers-color-scheme: dark)">
<title>{E(title)}</title><link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg"><link rel="apple-touch-icon" href="/icon-192.png">
<link rel="stylesheet" href="/style.css?v={{V}}">{extra}
</head>"""


def host_of(link: str | None) -> str:
    link = (link or "").strip()
    if not link.startswith(("http://", "https://")):
        return ""
    return re.sub(r"^www\.", "", re.sub(r"^https?://([^/]+).*", r"\1", link))


def snippet(text: str | None) -> str:
    t = _LINK.sub(r"\1", text or "")
    t = re.sub(r"[*_#>`]", "", t)
    return re.sub(r"\s+", " ", t).strip()[:160]


# Leading amount + unit of an ingredient line, e.g. "1 1/2 cups", "500g", "2-3 tbsp", "½".
_FR = {"½": .5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": .25, "¾": .75, "⅛": .125}
_NUM = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?(?:\s*[½⅓⅔¼¾⅛])?|[½⅓⅔¼¾⅛])"
_UNITS = ("g|gr|grams?|kg|kilos?|kilograms?|mg|ml|millilit(?:er|re)s?|cl|dl|l|lit(?:er|re)s?|oz|ounces?|lbs?|"
          "pounds?|cups?|c|tbsp|tbs|tablespoons?|tsp|teaspoons?|quarts?|qt|pints?|pt|cans?|tins?|jars?|cloves?|"
          "pinch(?:es)?|dash(?:es)?|handfuls?|slices?|sprigs?|bunch(?:es)?|sticks?|heads?|stalks?|packages?|pkg|"
          "packets?|sheets?|knobs?")
_QTY = re.compile(rf"^({_NUM})(?:\s*(?:-|–|to)\s*({_NUM}))?(\s*(?:{_UNITS})\.?(?!\w))?", re.I)


def _num(s: str) -> float:
    s = s.strip().replace(",", ".")
    v = 0.0
    if s and s[-1] in _FR:
        v += _FR[s[-1]]
        s = s[:-1].strip()
    for part in s.split():
        if "/" in part:
            a, b = part.split("/")
            v += float(a) / float(b) if float(b) else 0
        else:
            v += float(part)
    return v


def amount(text: str) -> tuple[str, str]:
    """Split an ingredient/yield line into (amount HTML, rest of the line, unescaped)."""
    m = _QTY.match(text)
    if not m:
        return "", text
    try:
        q1, q2 = _num(m[1]), (_num(m[2]) if m[2] else None)
    except (ValueError, ZeroDivisionError):
        return "", text
    end = m.end(2) if m[2] else m.end(1)
    d2 = f' data-q2="{q2:g}"' if q2 is not None else ""
    return (f'<span class=n data-q="{q1:g}"{d2}>{E(text[:end])}</span>{E(m[3] or "")}', text[m.end():])


def ing_item(n: int, t: str) -> str:
    q, rest = amount(t)
    return f'<li data-i="{n}">' + (f"<b class=q>{q}</b>" if q else "") + f"{inline(rest)}</li>"


def render_article(r: dict) -> str:
    def grouped(block, tag, cls, item):
        """'#' headers split the list into groups; returns HTML or '' if there are no items."""
        out, cur, n = [], [], 0
        for kind, t in lines_of(block):
            if kind == "h":
                if cur:
                    out.append(f"<{tag} class={cls}>{''.join(cur)}</{tag}>")
                    cur = []
                out.append(f"<h3>{inline(t)}</h3>")
            else:
                n += 1
                cur.append(item(n, t))
        if cur:
            out.append(f"<{tag} class={cls}>{''.join(cur)}</{tag}>")
        return "".join(out) if n else ""

    ings = grouped(r["ingredients"], "ul", "ing", ing_item)
    steps = grouped(r["instructions"], "ol", "steps",
                    lambda n, t: f'<li data-s="{n}"><span class=num>{n}</span> {inline(t)}</li>')
    imgs = r["images"]
    hero = f'<img class=hero src="/img/{imgs[0]}.jpg" alt="" decoding="async">' if imgs else ""
    more = "".join(f'<img src="/img/{h}.jpg" alt="" loading="lazy" decoding="async">' for h in imgs[1:])

    facts = []
    link, host = (r["link"] or "").strip(), host_of(r["link"])
    if host:
        facts.append(f'<a class=fact href="{E(link)}" rel="noreferrer" target="_blank" '
                     f'title="Open the original recipe on {E(host)}">{ic("book")}<span>{E(host)}</span></a>')
    elif link:
        facts.append(f'<span class=fact>{ic("book")}<span>{E(link)}</span></span>')
    if r["yield"] and str(r["yield"]).strip():
        q, rest = amount(str(r["yield"]).strip())
        facts.append(f'<span class=fact>{ic("people")}<span>{q}{E(rest)}</span></span>')
    times = [(lbl, str(r[f]).strip()) for lbl, f in (("Prep", "prep"), ("Cook", "cook"), ("Total", "total"))
             if r[f] and str(r[f]).strip()]
    if times:
        facts.append(f'<span class=fact>{ic("clock")}'
                     + "".join(f"<span class=t><b>{E(v)}</b><small>{k}</small></span>" for k, v in times) + "</span>")

    acts = []
    if steps:
        acts.append(f'<button class=act data-act=cook title="Start cooking mode: one step at a time, screen stays on">'
                    f'{ic("play")}<span>Cook</span></button>')
    if ings:
        acts.append(f'<button class=act data-act=adjust aria-expanded=false title="Scale the ingredient amounts">'
                    f'{ic("sliders")}<span>Adjust</span></button>')
    adjust = ('<div class=adjust hidden><input type=range min=0.5 max=4 step=0.5 value=1 aria-label="Scale" '
              'title="Drag to scale the ingredient amounts"><output>×1</output></div>') if ings else ""

    sec = lambda cls, title, body: f'<section class={cls}><h2>{title}</h2>{body}</section>' if body else ""  # noqa
    nutrition = "<br>".join(inline(l) for l in (r["nutrition"] or "").strip().split("\n")) if r["nutrition"] else ""
    return f"""<article class="rx{'' if hero else ' nohero'}" data-slug="{E(r['slug'])}">
<div class=main>
<header class=head>
<h1 class=title>{E(r['title'])}</h1>
{'<div class=facts>' + ''.join(facts) + '</div>' if facts else ''}
{'<div class=acts>' + ''.join(acts) + '</div>' if acts else ''}{adjust}
{('<div class=intro>' + paragraphs(r['text']) + '</div>') if r['text'] else ''}
</header>
<div class=method>{'<hr>' + steps if steps else ''}
{sec('notes', 'Notes', paragraphs(r['notes']))}
{sec('nutrition', 'Nutrition', ('<p>' + nutrition + '</p>') if nutrition else '')}
{('<div class=gallery>' + more + '</div>') if more else ''}</div>
</div>
<aside class=side2>{hero}{'<section class=ingredients>' + ings + '</section>' if ings else ''}</aside>
</article>"""


def render_recipe(r: dict) -> str:
    to_app = f"<script>if(matchMedia('(min-width:900px)').matches)location.replace('/#r={r['slug']}')</script>"
    return head(r["title"], to_app) + f"""
<body class=recipe>{SPRITE}
<a class=fab id=back href="/" title="Back to your recipes">{ic('back')}</a>
<main class=detail>{render_article(r)}</main>
<script src="/recipe.js?v={{V}}" defer></script><script src="/register.js?v={{V}}" defer></script>
</body></html>"""


def render_index(recipes: list[dict]) -> str:
    cats: dict[str, int] = {}
    for r in recipes:
        for t in r["tags"]:
            cats[t] = cats.get(t, 0) + 1
    data = [{"s": r["slug"], "t": r["title"], "c": r["tags"], "f": int(r["fav"]), "w": int(r["want"]),
             "d": int(r["date"]), "m": r["total"] or r["cook"] or "", "x": snippet(r["text"]), "h": host_of(r["link"]),
             "i": r["images"][0] if r["images"] else "",
             "q": fold(" ".join([r["title"], " ".join(r["tags"]), r["ingredients"] or ""]))} for r in recipes]

    def cat(c, icon, label, n, tip, sub=False):
        return (f'<li{" class=sub" if sub else ""}><button data-c="{E(c)}" title="{E(tip)}">{ic(icon)}'
                f'<span>{E(label)}</span><span class=n>{n}</span></button></li>')
    side = [cat("", "tray", "All", len(recipes), "Show every recipe")]
    side += [cat(c, "tag", c, n, f"Show recipes in {c}", True) for c, n in sorted(cats.items(), key=lambda kv: kv[0].lower())]
    side.append(cat("~recent", "recent", "Most Recent", len(recipes), "Show recipes, newest first"))
    for key, icon, label, tip, n in (("~want", "want", "Want to Cook", "Show recipes marked Want to Cook",
                                      sum(r["want"] for r in recipes)),
                                     ("~fav", "heart", "Favorites", "Show your favorite recipes",
                                      sum(r["fav"] for r in recipes)),
                                     ("~none", "folder", "Uncategorized", "Show recipes without a category",
                                      sum(not r["tags"] for r in recipes))):
        if n:
            side.append(cat(key, icon, label, n, tip))
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return head(SITE_TITLE) + f"""
<body class=app data-view=list>{SPRITE}
<div class=shell>
<nav class=side aria-label="Collections"><h1 class=serif>{E(SITE_TITLE)}</h1>
<ul class=cats>{''.join(side)}</ul>
<p class=status><span id=offline>Saving for offline…</span><br>Updated {time.strftime('%b %-d, %H:%M')}</p>
</nav>
<section class=list>
<header class=lh>
<button id=to-cats class=back title="Show collections and categories">{ic('back')}<span>{E(SITE_TITLE)}</span></button>
<button id=side-tog class=ib title="Hide sidebar">{ic('sidebar')}</button>
<div class=lt><h2 id=ltitle>All</h2><small id=lcount></small></div>
<label class=search>{ic('search')}<input id=q type=search placeholder="Search" autocomplete=off enterkeyhint=search aria-label="Search recipes" title="Search titles, categories and ingredients (/)"></label>
</header>
<ul id=list class=rows></ul><p id=empty class=gempty hidden>{ic('search')}</p>
</section>
<section class=dpane id=dpane></section>
</div>
<script>window.RECIPES={js};</script>
<script src="/recipe.js?v={{V}}" defer></script><script src="/index.js?v={{V}}" defer></script><script src="/register.js?v={{V}}" defer></script>
</body></html>"""


CSS = r"""
:root{--bg:#fff;--side:#f4f3f1;--soft:#f7f6f4;--fg:#1d1b1a;--fg2:#4a4643;--mute:#8b857f;--line:#e9e6e2;
--accent:#d4614a;--accent-fg:#fff;--amber:#f2ac2b;--sel:color-mix(in srgb,var(--fg) 8%,transparent);
--serif:"New York",ui-serif,"Iowan Old Style",Georgia,"Noto Serif",serif;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--side:#232325;--soft:#242426;--fg:#f2f1ef;--fg2:#c9c5c1;
--mute:#9a958f;--line:#38383a;--accent:#ea7a62}}
*{box-sizing:border-box}html{font-size:17px;-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:1rem/1.45 -apple-system,system-ui,"Segoe UI",Roboto,sans-serif;
-webkit-tap-highlight-color:transparent}
a{color:var(--accent);text-decoration:none}button,input{font:inherit;color:inherit}
button{background:none;border:0;padding:0;cursor:pointer}
.ic{width:1.15em;height:1.15em;flex:none;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;
stroke-linejoin:round;vertical-align:-.2em}
.serif,.title,.rt b{font-family:var(--serif);font-weight:700;letter-spacing:-.01em}
.ib{display:inline-grid;place-items:center;min-width:40px;height:40px;border-radius:10px;color:var(--accent)}
@media (hover:hover){.ib:hover{background:var(--sel)}}
.gempty{color:var(--mute);text-align:center;padding:56px 0;font-size:2.2rem;opacity:.5}

/* ---------- app shell: phone */
.app .side,.app .list{display:none;padding:calc(env(safe-area-inset-top) + 8px) 16px calc(24px + env(safe-area-inset-bottom))}
body[data-view=cats] .side,body[data-view=list] .list{display:block}
.dpane{display:none}
.side h1,#ltitle{font-family:var(--serif);font-weight:700;font-size:2.1rem;line-height:1.1;margin:6px 0 12px}
.cats{list-style:none;margin:0 0 20px;padding:0}
.cats button{display:flex;align-items:center;gap:12px;width:100%;padding:12px 2px;border-bottom:1px solid var(--line);text-align:left}
.cats .ic{color:var(--accent)}.cats .n{margin-left:auto;color:var(--mute);font-size:.85rem}
.cats .sub button{padding-left:30px}
.status{color:var(--mute);font-size:.75rem;line-height:1.5}
.lh{display:flex;flex-wrap:wrap;align-items:center}
.back{display:flex;align-items:center;gap:2px;color:var(--accent);padding:8px 8px 8px 0;margin-left:-6px}
.back .ic{width:1.4em;height:1.4em}
#side-tog,#lcount{display:none}.lt{flex-basis:100%}
.search{flex-basis:100%;display:flex;align-items:center;gap:6px;background:var(--side);border-radius:10px;padding:0 10px;
margin:0 0 6px;color:var(--mute);cursor:text}
.search input{flex:1;min-width:0;border:0;background:none;padding:8px 0;outline:none;color:var(--fg)}
.rows{list-style:none;margin:0;padding:0}
.row{display:flex;gap:12px;padding:10px 0;color:inherit;border-bottom:1px solid var(--line)}
.row img,.row .ph{width:78px;height:78px;border-radius:8px;object-fit:cover;flex:none;background:var(--side)}
.rt{min-width:0;display:flex;flex-direction:column;gap:3px;padding-top:2px}
.rt b{font-size:1rem;line-height:1.2}
.rm{display:flex;align-items:center;font-size:.72rem;white-space:nowrap;overflow:hidden;
-webkit-mask-image:linear-gradient(90deg,#000 85%,transparent);mask-image:linear-gradient(90deg,#000 85%,transparent)}
.rm span{display:inline-flex;align-items:center;gap:3px}
.rm span+span{margin-left:6px;padding-left:6px;border-left:1px solid var(--line)}
.rd{font-size:.78rem;color:var(--mute);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;line-height:1.35}

/* ---------- app shell: Mac-style three panes */
@media (min-width:900px){
html{font-size:15px}
body.app{height:100vh;overflow:hidden}
.app .shell{display:flex;height:100vh}
.app .side,.app .list,.app .dpane{display:block;height:100vh;overflow-y:auto;padding:0}
.app .side{width:230px;flex:none;background:var(--side);border-right:1px solid var(--line);padding:22px 10px 16px}
body.noside .app .side,body.noside .side{display:none}
.side h1{font-size:2rem;margin:4px 10px 14px}
.cats{margin-bottom:18px}.cats button{border:0;border-radius:7px;padding:6px 10px;gap:9px;font-size:.93rem}
.cats .sub button{padding-left:32px}.cats button[aria-current=true]{background:var(--sel)}
.status{padding:0 10px}
.app .list{width:350px;flex:none;border-right:1px solid var(--line)}
.lh{position:sticky;top:0;z-index:2;flex-wrap:nowrap;gap:6px;background:color-mix(in srgb,var(--bg) 88%,transparent);
-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);padding:10px 10px 10px 8px;border-bottom:1px solid var(--line)}
#to-cats{display:none}#side-tog{display:inline-grid;color:var(--mute)}
.lt{flex:1;min-width:0;flex-basis:auto}
#ltitle{font:600 .95rem/1.2 -apple-system,system-ui,sans-serif;margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#lcount{display:block;font-size:.75rem;color:var(--mute)}
.search{flex:0 1 150px;margin:0}.search input{padding:6px 0}
.rows{padding:6px 8px 20px}
.row{padding:8px;border-radius:9px;border:0}.row img,.row .ph{width:66px;height:66px}
.rows li+li .row{box-shadow:0 -1px 0 var(--line)}
.row[aria-current=true]{background:var(--accent);color:var(--accent-fg);box-shadow:none}
.rows li:has(.row[aria-current=true])+li .row{box-shadow:none}
.row[aria-current=true] .rd{color:rgba(255,255,255,.85)}.row[aria-current=true] .rm span+span{border-color:rgba(255,255,255,.4)}
.app .dpane{flex:1;min-width:0}
}

/* ---------- recipe (standalone phone page and the desktop detail pane) */
.detail,.dpane{container-type:inline-size}
.rx{display:flex;flex-direction:column}
.rx .main,.rx .side2{display:contents}
.hero{order:0;display:block;width:100%;aspect-ratio:4/3;max-height:62vh;object-fit:cover}
.head{order:1;padding:20px 20px 0}.ingredients{order:2;padding:0 20px}.method{order:3;padding:0 20px 48px}
.nohero .head{padding-top:calc(env(safe-area-inset-top) + 64px)}
.title{font-size:2rem;line-height:1.12;margin:0 0 14px}
.facts{display:flex;flex-wrap:wrap;align-items:center;row-gap:8px;font-size:.8rem;margin-bottom:12px;color:var(--fg)}
.fact{display:inline-flex;align-items:center;gap:6px;padding:0 12px;border-left:1px solid var(--line);color:inherit}
.fact:first-child{padding-left:0;border-left:0}
.t{display:inline-flex;flex-direction:column;line-height:1.1}.t+.t{margin-left:10px;padding-left:10px;border-left:1px solid var(--line)}
.t b{font-weight:600}.t small{font-size:.58rem;color:var(--mute);text-transform:uppercase;letter-spacing:.05em}
.acts{display:flex;flex-wrap:wrap;margin:4px 0 16px;font-size:.82rem}
.act{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border-left:1px solid var(--line);font-weight:500}
.act:first-child{padding-left:0;border-left:0}.act .ic{color:var(--accent);width:1.25em;height:1.25em}
.act[aria-expanded=true]{color:var(--accent)}
.adjust{display:flex;align-items:center;gap:12px;margin:-6px 0 16px;padding:8px 14px;border-radius:12px;background:var(--side);max-width:340px}
.adjust[hidden]{display:none}.adjust input{flex:1;accent-color:var(--accent)}
.adjust output{font-weight:600;min-width:2.4em;text-align:right}
.intro{color:var(--fg2)}.intro p{margin:0 0 .8em}
.method hr{border:0;border-top:1px solid var(--line);width:45%;margin:22px auto 8px}
.ingredients h3,.method h3{font-size:1rem;font-weight:700;margin:24px 0 6px}
.ing{list-style:none;margin:10px 0;padding:0}
.ing li{padding:5px 0;cursor:pointer;color:var(--fg2);line-height:1.4}
.ing .q{color:var(--fg);font-weight:700}.ing li.done{opacity:.3}
.steps{list-style:none;margin:0;padding:0}
.steps li{padding:8px 10px;margin:0 -10px 8px;border-radius:10px;cursor:pointer;line-height:1.65;color:var(--fg2)}
.num{color:var(--mute);font-size:.8em;margin-right:2px}
.steps li.cur{background:color-mix(in srgb,var(--accent) 13%,transparent);color:var(--fg)}
.notes h2,.nutrition h2{font-size:1rem;margin:28px 0 6px}.notes p,.nutrition p{margin:.4em 0;color:var(--fg2)}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;margin-top:24px}
.gallery img{width:100%;border-radius:10px}
.fab{position:fixed;top:calc(env(safe-area-inset-top) + 10px);left:12px;z-index:20;width:38px;height:38px;border-radius:50%;
display:grid;place-items:center;color:var(--fg);background:color-mix(in srgb,var(--bg) 72%,transparent);
-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);box-shadow:0 1px 4px rgba(0,0,0,.18)}
.fab .ic{width:1.3em;height:1.3em}
@container (min-width:720px){
.rx{display:grid;grid-template-columns:minmax(0,1fr) minmax(270px,35%);min-height:100vh}
.rx .main{display:block;padding:46px 52px 64px;max-width:780px}
.rx .side2{display:block;background:var(--soft);border-left:1px solid var(--line)}
.head,.method,.nohero .head{padding:0}.ingredients{padding:18px 30px 40px}
.hero{aspect-ratio:auto;max-height:none}.title{font-size:2.35rem}
}

/* ---------- cooking mode */
.cook{position:fixed;inset:0;z-index:50;display:flex;background:var(--bg)}
.cs{flex:1;overflow-y:auto;scroll-snap-type:y mandatory;padding:45vh 28px}
.cs h3{max-width:680px;margin:0 auto 28px;color:var(--mute);font-size:.95rem}
.cstep{max-width:680px;margin:0 auto 48px;font-size:1.5rem;line-height:1.5;font-weight:600;opacity:.2;cursor:pointer;scroll-snap-align:center}
.cstep.on{opacity:1}.cstep .num{font-weight:400}
.ci{width:38%;max-width:440px;overflow-y:auto;background:#2c2c2e;color:#f2f1ef;padding:44px 36px}
.ci h3{color:#f2f1ef}.ci .ing li{color:#f2f1ef;font-size:1.15rem;padding:7px 0}.ci .q{color:var(--amber)}
.cbtn{position:absolute;z-index:2;width:40px;height:40px;border-radius:50%;display:grid;place-items:center;
background:var(--side);color:var(--fg)}
.cbtn.close{top:calc(env(safe-area-inset-top) + 12px);left:12px}
.cbtn.ingt{bottom:calc(env(safe-area-inset-bottom) + 18px);right:18px;width:52px;height:52px;background:var(--accent);color:#fff}
.cbtn.ingt .ic{width:1.4em;height:1.4em}
@media (max-width:719px){
.ci{position:absolute;left:0;right:0;bottom:0;width:auto;max-width:none;max-height:72vh;border-radius:18px 18px 0 0;
padding:28px 24px calc(84px + env(safe-area-inset-bottom));transform:translateY(100%);transition:transform .25s ease}
.cstep{font-size:1.3rem}.cs{padding:45vh 22px}
.cook.sheet .ci{transform:none}
}
@media (min-width:720px){.cbtn.ingt{display:none}}
"""

INDEX_JS = r"""
(()=>{
const R=window.RECIPES,$=id=>document.getElementById(id),body=document.body,wide=matchMedia('(min-width:900px)');
const ic=n=>'<svg class=ic aria-hidden=true><use href="#i-'+n+'"/></svg>';
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fold=s=>s.normalize('NFKD').replace(/[̀-ͯ]/g,'').toLowerCase();
const NAMES={'':'All','~recent':'Most Recent','~want':'Want to Cook','~fav':'Favorites','~none':'Uncategorized'};
const q=$('q'),list=$('list'),pane=$('dpane'),cache={};
let st={c:'',q:'',v:'list',y:0,sel:''},shown=[];
try{Object.assign(st,JSON.parse(sessionStorage.getItem('idx')||'{}'))}catch(e){}
const h=(location.hash.match(/r=([^&]+)/)||[])[1];if(h)st.sel=decodeURIComponent(h);
q.value=st.q;
function save(){try{sessionStorage.setItem('idx',JSON.stringify(st))}catch(e){}}
function pick(){
 const t=fold(st.q).split(/\s+/).filter(Boolean);
 let rs=R.filter(r=>{
  if(st.c==='~want'&&!r.w||st.c==='~fav'&&!r.f||st.c==='~none'&&r.c.length)return false;
  if(st.c&&st.c[0]!=='~'&&r.c.indexOf(st.c)<0)return false;
  return t.every(x=>r.q.includes(x))});
 if(st.c==='~recent')rs=rs.slice().sort((a,b)=>b.d-a.d);
 else if(t.length){const t0=t.join(' ');rs=rs.slice().sort((a,b)=>fold(b.t).includes(t0)-fold(a.t).includes(t0))}
 return rs}
function row(r){
 const m=[];if(r.m)m.push(ic('clock')+esc(r.m));if(r.c[0])m.push(ic('tag')+esc(r.c[0]));if(r.h)m.push(ic('book')+esc(r.h));
 return '<li><a class=row href="/r/'+r.s+'/" data-s="'+r.s+'"'+(wide.matches&&r.s===st.sel?' aria-current=true':'')+'>'+
  (r.i?'<img src="/img/'+r.i+'-t.jpg" alt="" loading=lazy decoding=async>':'<span class=ph></span>')+
  '<span class=rt><b>'+esc(r.t)+'</b>'+(m.length?'<span class=rm><span>'+m.join('</span><span>')+'</span></span>':'')+
  (r.x?'<span class=rd>'+esc(r.x)+'</span>':'')+'</span></a></li>'}
function render(){
 shown=pick();list.innerHTML=shown.map(row).join('');$('empty').hidden=shown.length>0;
 $('ltitle').textContent=st.c in NAMES?NAMES[st.c]:st.c;
 $('lcount').textContent=shown.length+(shown.length===1?' recipe':' recipes');
 marks();save()}
function marks(){
 document.querySelectorAll('.side [data-c]').forEach(b=>b.setAttribute('aria-current',b.dataset.c===st.c))}
function view(v){
 if(wide.matches||v!=='cats')v='list';
 const changed=st.v!==v;st.v=v;body.dataset.view=v;save();
 if(changed&&!wide.matches)scrollTo(0,0)}
async function open(s){
 st.sel=s;save();history.replaceState(null,'','#r='+s);
 list.querySelectorAll('.row[aria-current]').forEach(x=>x.removeAttribute('aria-current'));
 const el=list.querySelector('.row[data-s="'+s+'"]');if(el){el.setAttribute('aria-current','true');el.scrollIntoView({block:'nearest'})}
 let html=cache[s];
 if(html==null){pane.style.opacity='.4';try{const d=new DOMParser().parseFromString(await (await fetch('/r/'+s+'/')).text(),'text/html');
  const a=d.querySelector('.rx');html=cache[s]=a?a.outerHTML:''}catch(e){html=''}}
 if(st.sel!==s)return;
 pane.style.opacity='';pane.innerHTML=html||'<p class=gempty>'+ic('x')+'</p>';pane.scrollTop=0;
 if(window.MelaRecipe)MelaRecipe(pane.querySelector('.rx'))}
function firstOpen(){if(!wide.matches||pane.firstChild)return;
 const s=R.some(r=>r.s===st.sel)?st.sel:shown[0]&&shown[0].s;if(s)open(s)}
try{localStorage.removeItem('groc')}catch(e){}  // drop the old on-device grocery list
// wiring
document.querySelector('.side').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;
 st.c=b.dataset.c;view('list');render();if(wide.matches)document.querySelector('.list').scrollTop=0;firstOpen()});
$('to-cats').addEventListener('click',()=>view('cats'));
const tog=$('side-tog');
function sideTitle(){tog.title=body.classList.contains('noside')?'Show sidebar':'Hide sidebar'}
try{if(localStorage.getItem('noside'))body.classList.add('noside')}catch(e){}sideTitle();
tog.addEventListener('click',()=>{const n=body.classList.toggle('noside');sideTitle();
 try{n?localStorage.setItem('noside','1'):localStorage.removeItem('noside')}catch(e){}});
q.addEventListener('input',()=>{st.q=q.value;render()});
list.addEventListener('click',e=>{const a=e.target.closest('.row');
 if(!a||!wide.matches||e.metaKey||e.ctrlKey||e.shiftKey)return;e.preventDefault();open(a.dataset.s)});
document.addEventListener('keydown',e=>{
 if(!wide.matches||document.querySelector('.cook'))return;
 if(e.key==='/'&&document.activeElement!==q){e.preventDefault();q.focus();return}
 if(e.key!=='ArrowDown'&&e.key!=='ArrowUp')return;
 if(e.target.tagName==='INPUT'&&e.target!==q)return;
 const i=shown.findIndex(r=>r.s===st.sel),j=Math.max(0,Math.min(shown.length-1,i+(e.key==='ArrowDown'?1:-1)));
 if(shown[j]){e.preventDefault();open(shown[j].s)}});
addEventListener('pagehide',()=>{if(!wide.matches){st.y=scrollY;save()}});
wide.addEventListener('change',()=>{view(st.v);render();firstOpen()});
render();view(st.v);
if(!wide.matches&&st.v==='list'&&st.y)scrollTo(0,st.y);
firstOpen();
})();
"""

RECIPE_JS = r"""
(()=>{
const ic=n=>'<svg class=ic aria-hidden=true><use href="#i-'+n+'"/></svg>';
const FR=[[0,''],[.125,'⅛'],[.25,'¼'],[1/3,'⅓'],[.5,'½'],[2/3,'⅔'],[.75,'¾'],[1,'']];
function fmt(v){
 if(v>=10)return String(Math.round(v));
 let w=Math.floor(v),f=v-w,b=FR[0];FR.forEach(x=>{if(Math.abs(f-x[0])<Math.abs(f-b[0]))b=x});
 if(Math.abs(f-b[0])>.06)return String(Math.round(v*10)/10);
 if(b[0]===1){w++;b=FR[0]}return ((w?String(w):'')+b[1])||'0'}
let cook=null;
function init(a){
 if(!a||a.dataset.ready)return;a.dataset.ready='1';
 const key='ck:'+a.dataset.slug;let st={i:{},s:0,t:Date.now()},f=1;
 try{const s=JSON.parse(localStorage.getItem(key)||'null');if(s&&Date.now()-s.t<12*36e5){st=s;st.i=st.i||{};if(typeof st.s!=='number')st.s=0}}catch(e){}
 const save=()=>{st.t=Date.now();try{localStorage.setItem(key,JSON.stringify(st))}catch(e){}};
 const ings=()=>document.querySelectorAll('.rx[data-ready] .ingredients .ing li, .cook .ing li');
 function paint(){ings().forEach(li=>li.classList.toggle('done',!!st.i[li.dataset.i]));
  a.querySelectorAll('.method .steps li').forEach(li=>li.classList.toggle('cur',+li.dataset.s===st.s))}
 function toggle(i){if(st.i[i])delete st.i[i];else st.i[i]=1;save();paint()}
 a.querySelectorAll('.n[data-q]').forEach(n=>n.dataset.o=n.textContent);
 const range=a.querySelector('.adjust input'),out=a.querySelector('.adjust output');
 if(range)range.addEventListener('input',()=>{f=+range.value;out.textContent='×'+fmt(f);
  a.querySelectorAll('.n[data-q]').forEach(n=>{n.textContent=f===1?n.dataset.o:
   fmt(+n.dataset.q*f)+(n.dataset.q2?'–'+fmt(+n.dataset.q2*f):'')})});
 a.addEventListener('click',e=>{
  if(e.target.closest('a'))return;
  const li=e.target.closest('.ing li');if(li){toggle(li.dataset.i);return}
  const sl=e.target.closest('.steps li');if(sl){st.s=st.s===+sl.dataset.s?0:+sl.dataset.s;save();paint();return}
  const b=e.target.closest('[data-act]');if(!b)return;
  if(b.dataset.act==='cook')openCook();
  else if(b.dataset.act==='adjust'){const p=a.querySelector('.adjust');p.hidden=!p.hidden;b.setAttribute('aria-expanded',String(!p.hidden));
   b.title=p.hidden?'Scale the ingredient amounts':'Hide the scaling slider'}});
 // ---- cooking mode
 let lock=null,raf=0;
 const wake=()=>{if(!cook||lock||document.visibilityState!=='visible'||!navigator.wakeLock)return;
  navigator.wakeLock.request('screen').then(l=>{lock=l;l.addEventListener('release',()=>{lock=null})}).catch(()=>{})};
 function openCook(){
  if(cook)return;
  cook=document.createElement('div');cook.className='cook';cook.setAttribute('role','dialog');cook.setAttribute('aria-label','Cooking mode');
  let s='';a.querySelectorAll('.method > h3, .method .steps li').forEach(el=>{
   s+=el.tagName==='H3'?'<h3>'+el.innerHTML+'</h3>':'<div class=cstep data-s="'+el.dataset.s+'">'+el.innerHTML+'</div>'});
  const ing=a.querySelector('.ingredients');
  cook.innerHTML='<button class="cbtn close" title="Leave cooking mode (Esc)">'+ic('x')+'</button><div class=cs>'+s+'</div>'+
   (ing?'<div class=ci>'+ing.innerHTML+'</div><button class="cbtn ingt" title="Show ingredients">'+ic('list')+'</button>':'');
  document.body.appendChild(cook);document.documentElement.style.overflow='hidden';paint();
  const cs=cook.querySelector('.cs'),steps=[...cook.querySelectorAll('.cstep')];
  const on=()=>{raf=0;const mid=cs.getBoundingClientRect().top+cs.clientHeight/2;let best=steps[0],d=1e9;
   steps.forEach(x=>{const r=x.getBoundingClientRect(),dd=Math.abs(r.top+r.height/2-mid);if(dd<d){d=dd;best=x}});
   steps.forEach(x=>x.classList.toggle('on',x===best));if(best&&+best.dataset.s!==st.s){st.s=+best.dataset.s;save();paint()}};
  cs.addEventListener('scroll',()=>{if(!raf)raf=requestAnimationFrame(on)},{passive:true});
  const start=steps.find(x=>+x.dataset.s===st.s)||steps[0];if(start)start.scrollIntoView({block:'center'});on();
  cook.addEventListener('click',e=>{
   const li=e.target.closest('.ing li');if(li){toggle(li.dataset.i);return}
   const x=e.target.closest('.cstep');if(x){x.scrollIntoView({block:'center',behavior:'smooth'});return}
   const t=e.target.closest('.ingt');if(t){const o=cook.classList.toggle('sheet');t.title=o?'Hide ingredients':'Show ingredients';return}
   if(e.target.closest('.close'))history.back()});
  cook._key=e=>{if(e.key==='Escape'){history.back();return}
   const i=steps.findIndex(x=>x.classList.contains('on'));let j=-1;
   if(e.key==='ArrowDown'||e.key==='ArrowRight'||e.key===' ')j=i+1;else if(e.key==='ArrowUp'||e.key==='ArrowLeft')j=i-1;
   if(j>=0&&j<steps.length){e.preventDefault();steps[j].scrollIntoView({block:'center',behavior:'smooth'})}};
  document.addEventListener('keydown',cook._key);
  history.pushState({cook:1},'');wake()}
 function closeCook(){if(!cook)return;document.removeEventListener('keydown',cook._key);cook.remove();cook=null;
  document.documentElement.style.overflow='';if(lock)lock.release();paint()}
 addEventListener('popstate',closeCook);
 document.addEventListener('visibilitychange',wake);
 paint();
}
window.MelaRecipe=init;
const solo=document.querySelector('body.recipe .rx');
if(solo){init(solo);
 const back=document.getElementById('back');
 back.addEventListener('click',e=>{try{if(document.referrer&&new URL(document.referrer).origin===location.origin&&history.length>1){
  e.preventDefault();history.back()}}catch(x){}})}
})();
"""

REGISTER_JS = r"""
if('serviceWorker' in navigator){
 navigator.serviceWorker.register('/sw.js').then(function(){return navigator.serviceWorker.ready}).then(function(reg){
  if(reg.active)reg.active.postMessage('warm')}).catch(function(){});
 navigator.serviceWorker.addEventListener('message',function(e){var el=document.getElementById('offline');if(!el||!e.data)return;
  el.textContent=e.data.done?'Available offline':'Saving for offline… '+e.data.n+'/'+e.data.of});
}else{var el=document.getElementById('offline');if(el)el.textContent='Offline mode unsupported'}
"""

SW_JS = r"""
const V='__VERSION__',PAGES='pages-'+V,MEDIA='media';
const CORE=__CORE__,MEDIA_LIST=__MEDIA__;
self.addEventListener('install',e=>{e.waitUntil(caches.open(PAGES).then(c=>c.addAll(CORE)).then(()=>self.skipWaiting()))});
self.addEventListener('activate',e=>{e.waitUntil((async()=>{
 for(const k of await caches.keys())if(k.startsWith('pages-')&&k!==PAGES)await caches.delete(k);
 const m=await caches.open(MEDIA),keep=new Set(MEDIA_LIST.map(u=>new URL(u,location).href));
 for(const r of await m.keys())if(!keep.has(r.url))await m.delete(r);
 await self.clients.claim()})())});
let warming=null;
async function warm(){const m=await caches.open(MEDIA);let n=0;
 const have=new Set((await m.keys()).map(r=>r.url));
 const todo=MEDIA_LIST.filter(u=>!have.has(new URL(u,location).href));
 const tell=async d=>{for(const c of await self.clients.matchAll())c.postMessage(d)};
 for(let i=0;i<todo.length;i+=6){await Promise.all(todo.slice(i,i+6).map(u=>m.add(u).catch(()=>{})));
  n=Math.min(i+6,todo.length);await tell({n:MEDIA_LIST.length-todo.length+n,of:MEDIA_LIST.length})}
 await tell({done:true})}
self.addEventListener('message',e=>{if(e.data==='warm'){warming=warming||warm().finally(()=>warming=null);e.waitUntil(warming)}});
self.addEventListener('fetch',e=>{
 const u=new URL(e.request.url);if(e.request.method!=='GET'||u.origin!==location.origin)return;
 if(u.pathname.startsWith('/img/')){e.respondWith(caches.open(MEDIA).then(async c=>{
   const hit=await c.match(e.request);if(hit)return hit;const r=await fetch(e.request);if(r.ok)c.put(e.request,r.clone());return r}));return}
 e.respondWith((async()=>{
  try{const r=await fetch(e.request);
   if(r.ok&&!r.redirected){const c=await caches.open(PAGES);c.put(e.request,r.clone())}return r}
  catch(err){const hit=await caches.match(e.request,{ignoreSearch:true})||
   (u.pathname.endsWith('/')?null:await caches.match(u.pathname+'/'));
   if(hit)return hit;if(e.request.mode==='navigate'){const idx=await caches.match('/');if(idx)return idx}
   return new Response('Offline',{status:503})}})())});
"""

MIDDLEWARE_JS = r"""
// Cloudflare Pages Function: password gate for the whole site.
// Password lives in the Pages secret SITE_PASSWORD (never in this repo / iCloud).
// A correct password sets a 1-year HttpOnly cookie, so the phone stays signed in.
const COOKIE = "mela_auth";
const enc = new TextEncoder();
async function token(pw) {
  const key = await crypto.subtle.importKey("raw", enc.encode(pw), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode("mela-sync-v1"));
  return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, "0")).join("");
}
function same(a, b) {
  if (a.length !== b.length) return false;
  let d = 0; for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i); return d === 0;
}
function page(msg, next) {
  const esc = s => s.replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  return new Response(`<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta name=robots content=noindex><title>Recipes</title><style>*{box-sizing:border-box}body{font:17px/1.4 -apple-system,system-ui,sans-serif;display:grid;place-items:center;
min-height:90vh;margin:0 16px;background:#fff;color:#1d1b1a}@media(prefers-color-scheme:dark){body{background:#1c1c1e;color:#f2f1ef}input{background:#2c2c2e;color:inherit}}
form{width:100%;max-width:340px}h1{font:700 2.1rem/1.1 "New York",ui-serif,Georgia,"Noto Serif",serif;margin:0 0 12px}
input,button{width:100%;font:inherit;padding:.65em .85em;border-radius:10px;margin-top:10px}
input{border:1px solid #d6d2ce}button{background:#d4614a;color:#fff;border:0;font-weight:600}p{color:#d4614a}</style>
<form method=post action="/__login"><h1>Recipes</h1><input type=hidden name=next value="${esc(next)}">
<input type=password name=password placeholder=Password autocomplete=current-password autofocus required>
<button>Open</button>${msg ? `<p>${msg}</p>` : ""}</form></html>`,
    { status: 401, headers: { "content-type": "text/html;charset=utf-8", "cache-control": "no-store", "x-robots-tag": "noindex" } });
}
export async function onRequest({ request, env, next }) {
  if (!env.SITE_PASSWORD) return new Response("SITE_PASSWORD secret is not set on this Pages project.", { status: 500 });
  const url = new URL(request.url);
  const good = await token(env.SITE_PASSWORD);
  if (url.pathname === "/__login" && request.method === "POST") {
    const form = await request.formData();
    let dest = String(form.get("next") || "/");
    if (!dest.startsWith("/") || dest.startsWith("//")) dest = "/";
    const ok = same(await token(String(form.get("password") || "")), good);
    if (!ok) { await new Promise(r => setTimeout(r, 800)); return page("Wrong password.", dest); }
    return new Response(null, { status: 303, headers: { location: dest,
      "set-cookie": `${COOKIE}=${good}; Max-Age=31536000; Path=/; Secure; HttpOnly; SameSite=Lax` } });
  }
  const m = (request.headers.get("cookie") || "").match(/(?:^|;\s*)mela_auth=([0-9a-f]+)/);
  if (m && same(m[1], good)) {
    const res = await next();
    const out = new Response(res.body, res);
    out.headers.set("x-robots-tag", "noindex, nofollow");
    return out;
  }
  if (request.method === "GET" && request.headers.get("accept")?.includes("text/html"))
    return page("", url.pathname + url.search);
  return new Response("Unauthorized", { status: 401 });
}
"""

HEADERS = """/sw.js
  Cache-Control: no-cache
/img/*
  Cache-Control: public, max-age=31536000, immutable
/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: no-referrer
"""


def png_icon(size: int) -> bytes:
    """Tiny pure-Python PNG: orange rounded square with a white bowl."""
    rows = []
    r_corner, cx = size * 0.22, size / 2
    for y in range(size):
        row = bytearray(b"\x00")
        for x in range(size):
            dx = max(r_corner - x, 0, x - (size - 1 - r_corner))
            dy = max(r_corner - y, 0, y - (size - 1 - r_corner))
            if dx * dx + dy * dy > r_corner * r_corner:
                row += b"\x00\x00\x00\x00"
                continue
            top = size * 0.47
            bowl = y >= top and (x - cx) ** 2 + (y - top) ** 2 <= (size * 0.31) ** 2
            row += b"\xff\xff\xff\xff" if bowl else b"\xc2\x41\x0c\xff"
        rows.append(bytes(row))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def build(conn: sqlite3.Connection, ext_dir: Path, root: Path) -> int:
    pub = root / "public"
    tmp = root / "public.new"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    recipes = read_recipes(conn, ext_dir, tmp)
    log(f"read {len(recipes)} recipes, {sum(len(r['images']) for r in recipes)} images")

    files: dict[str, str] = {}
    for r in recipes:
        files[f"r/{r['slug']}/index.html"] = render_recipe(r)
    files["index.html"] = render_index(recipes)
    statics = {"style.css": CSS, "index.js": INDEX_JS, "recipe.js": RECIPE_JS, "register.js": REGISTER_JS,
               "icon.svg": ICON_SVG, "_headers": HEADERS, "robots.txt": "User-agent: *\nDisallow: /\n"}
    v = hashlib.sha1(("".join(statics.values()) + "".join(files.values())).encode()).hexdigest()[:10]
    for p, body in {**statics, **files}.items():
        (tmp / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp / p).write_text(body.replace("{V}", v), encoding="utf-8")
    (tmp / "manifest.webmanifest").write_text(json.dumps({
        "name": SITE_TITLE, "short_name": SITE_TITLE, "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#ffffff", "theme_color": "#ffffff",
        "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
                  {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]}), encoding="utf-8")
    for n in (192, 512):
        cached = CACHE / f"icon-{n}.png"
        if not cached.exists():
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(png_icon(n))
        shutil.copyfile(cached, tmp / f"icon-{n}.png")

    core = ["/", f"/style.css?v={v}", f"/index.js?v={v}", f"/recipe.js?v={v}", f"/register.js?v={v}",
            "/manifest.webmanifest", "/icon.svg", "/icon-192.png"] + [f"/r/{r['slug']}/" for r in recipes]
    media = sorted({f"/img/{h}-t.jpg" for r in recipes for h in r["images"][:1]}
                   | {f"/img/{h}.jpg" for r in recipes for h in r["images"]})
    (tmp / "sw.js").write_text(SW_JS.replace("__VERSION__", v).replace("__CORE__", json.dumps(core))
                               .replace("__MEDIA__", json.dumps(media)), encoding="utf-8")

    fn = root / "functions"
    fn.mkdir(parents=True, exist_ok=True)
    (fn / "_middleware.js").write_text(MIDDLEWARE_JS, encoding="utf-8")

    shutil.rmtree(pub, ignore_errors=True)
    tmp.rename(pub)
    return len(recipes)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["inspect", "build"])
    ap.add_argument("--db", help="Path to Curcuma.sqlite (default: auto-detect; or env MELA_DB_PATH)")
    ap.add_argument("--out", default=str(CACHE / "site"), help="Site root (gets public/ and functions/)")
    a = ap.parse_args()
    src = find_db(a.db)
    db, ext = snapshot(src)
    conn = open_copy(db)
    try:
        if a.command == "inspect":
            inspect(conn, src, db)
        else:
            n = build(conn, ext, Path(a.out).expanduser())
            print(n)  # stdout carries only the count; sync.sh reads it
    finally:
        conn.close()


if __name__ == "__main__":
    main()

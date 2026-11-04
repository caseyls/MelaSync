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
            slug = f"{slug}-{hashlib.sha1(rid.encode()).hexdigest()[:6]}"
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


def head(title: str, depth: str, extra: str = "") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow"><meta name="theme-color" content="#c2410c">
<title>{E(title)}</title><link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.svg"><link rel="apple-touch-icon" href="/icon-192.png">
<link rel="stylesheet" href="/style.css?v={{V}}">{extra}
<script>try{{var f=localStorage.getItem('fs');if(f)document.documentElement.style.fontSize=f}}catch(e){{}}</script>
</head>"""


def meta_bits(r: dict) -> list[tuple[str, str]]:
    return [(k, str(r[f]).strip()) for k, f in (("Serves", "yield"), ("Prep", "prep"), ("Cook", "cook"),
                                                ("Total", "total")) if r[f] and str(r[f]).strip()]


def render_recipe(r: dict) -> str:
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

    ings = grouped(r["ingredients"], "ul", "ing",
                   lambda n, t: f'<li data-i="{n}"><span class=box></span><span>{inline(t)}</span></li>')
    steps = grouped(r["instructions"], "ol", "steps",
                    lambda n, t: f'<li data-s="{n}"><span class=num>{n}</span><div>{inline(t)}</div></li>')
    imgs = r["images"]
    hero = (f'<img class=hero src="/img/{imgs[0]}.jpg" alt="" decoding="async">' if imgs else "")
    more = "".join(f'<img src="/img/{h}.jpg" alt="" loading="lazy" decoding="async">' for h in imgs[1:])
    meta = "".join(f"<div><dt>{k}</dt><dd>{E(v)}</dd></div>" for k, v in meta_bits(r))
    tags = "".join(f'<a class=tag href="/#c={E(t)}">{E(t)}</a>' for t in r["tags"])
    link = (r["link"] or "").strip()
    if link.startswith(("http://", "https://")):
        host = re.sub(r"^www\.", "", re.sub(r"^https?://([^/]+).*", r"\1", link))
        src = f'<p class=src>Source: <a href="{E(link)}" rel="noreferrer" target="_blank">{E(host)}</a></p>'
    else:
        src = f"<p class=src>Source: {E(link)}</p>" if link else ""
    sec = lambda cls, title, body: f'<section class={cls}><h2>{title}</h2>{body}</section>' if body else ""  # noqa
    nutrition = "<br>".join(inline(l) for l in (r["nutrition"] or "").strip().split("\n")) if r["nutrition"] else ""
    return head(r["title"], "../../") + f"""
<body class=recipe data-slug="{E(r['slug'])}">
<header class=bar><a class=back href="/" aria-label="All recipes">‹ Recipes</a>
<span class=tools><button id=fsm aria-label="Smaller text">A−</button><button id=fsp aria-label="Larger text">A+</button>
<button id=wake hidden aria-pressed=false title="Keep screen on">☀︎ <span>Screen on</span></button></span></header>
<main>
<article>
{hero}
<h1>{E(r['title'])}</h1>
{'<dl class=meta>' + meta + '</dl>' if meta else ''}
{'<div class=tags>' + tags + '</div>' if tags else ''}
{('<div class=intro>' + paragraphs(r['text']) + '</div>') if r['text'] else ''}
<div class=cols>
{sec('ingredients', 'Ingredients', ings)}
{sec('method', 'Steps', steps)}
</div>
<button id=reset class=reset hidden>Clear checkmarks</button>
{sec('notes', 'Notes', paragraphs(r['notes']))}
{sec('nutrition', 'Nutrition', ('<p>' + nutrition + '</p>') if nutrition else '')}
{('<div class=gallery>' + more + '</div>') if more else ''}
{src}
</article></main>
<script src="/recipe.js?v={{V}}" defer></script><script src="/register.js?v={{V}}" defer></script>
</body></html>"""


def render_index(recipes: list[dict]) -> str:
    cats: dict[str, int] = {}
    for r in recipes:
        for t in r["tags"]:
            cats[t] = cats.get(t, 0) + 1
    data = [{"s": r["slug"], "t": r["title"], "c": r["tags"], "f": int(r["fav"]), "w": int(r["want"]),
             "d": int(r["date"]), "m": r["total"] or r["cook"] or "",
             "i": r["images"][0] if r["images"] else "",
             "q": fold(" ".join([r["title"], " ".join(r["tags"]), r["ingredients"] or ""]))} for r in recipes]
    chips = ['<button class=chip data-c="" aria-pressed=true>All</button>']
    if any(r["fav"] for r in recipes):
        chips.append('<button class=chip data-c="★">★ Favorites</button>')
    if any(r["want"] for r in recipes):
        chips.append('<button class=chip data-c="⚑">Want to cook</button>')
    chips += [f'<button class=chip data-c="{E(c)}">{E(c)} <small>{n}</small></button>'
              for c, n in sorted(cats.items(), key=lambda kv: kv[0].lower())]
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return head(SITE_TITLE, "") + f"""
<body class=index>
<header class=top><h1>{E(SITE_TITLE)}</h1>
<input id=q type=search placeholder="Search {len(recipes)} recipes or ingredients" autocomplete=off enterkeyhint=search aria-label="Search">
<nav class=chips id=chips>{''.join(chips)}</nav>
<div class=sortrow><span id=count></span><label>Sort <select id=sort><option value=a>A–Z</option><option value=n>Newest</option></select></label></div>
</header>
<main><ul id=list class=cards></ul><p id=empty hidden>No recipes match.</p></main>
<footer><span id=offline>Saving for offline…</span> · updated <time>{time.strftime('%b %-d, %H:%M')}</time></footer>
<script>window.RECIPES={js};</script>
<script src="/index.js?v={{V}}" defer></script><script src="/register.js?v={{V}}" defer></script>
</body></html>"""


CSS = r"""
:root{--bg:#fbf8f4;--fg:#1f1a17;--mute:#6b625b;--line:#e7e0d8;--card:#fff;--accent:#c2410c;--accent-fg:#fff;
--done:#a39a92;--hi:#fff1e6;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#161311;--fg:#f3ede7;--mute:#a59b92;--line:#2e2824;--card:#201b18;
--accent:#fb923c;--accent-fg:#1a1006;--done:#6d645d;--hi:#33241a}}
*{box-sizing:border-box}html{font-size:20px;-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:1rem/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)}
a{color:var(--accent)}button,input,select{font:inherit;color:inherit}
main{max-width:1100px;margin:0 auto;padding:0 16px 40px}
/* index */
.top{position:sticky;top:0;z-index:5;background:var(--bg);padding:12px 16px 8px;border-bottom:1px solid var(--line);max-width:1100px;margin:0 auto}
.top h1{font-size:1.4rem;margin:4px 0 10px}
#q{width:100%;padding:.6em .8em;border:1.5px solid var(--line);border-radius:12px;background:var(--card);font-size:1.05rem}
#q:focus{outline:none;border-color:var(--accent)}
.chips{display:flex;gap:8px;overflow-x:auto;padding:10px 0 4px;scrollbar-width:none}.chips::-webkit-scrollbar{display:none}
.chip{flex:none;border:1.5px solid var(--line);background:var(--card);border-radius:999px;padding:.3em .85em;font-size:.85rem;white-space:nowrap}
.chip small{color:var(--mute)}.chip[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}
.chip[aria-pressed=true] small{color:inherit;opacity:.8}
.sortrow{display:flex;justify-content:space-between;align-items:center;color:var(--mute);font-size:.8rem;padding-top:6px}
.sortrow select{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:2px 6px;font-size:.8rem}
.cards{list-style:none;margin:12px 0 0;padding:0;display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.cards a{display:flex;gap:12px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:8px;text-decoration:none;color:inherit;min-height:84px}
.cards img,.cards .ph{width:76px;height:76px;flex:none;border-radius:10px;object-fit:cover;background:var(--line)}
.cards .ph{display:grid;place-items:center;color:var(--mute);font-size:1.6rem}
.cards b{display:block;font-weight:600;line-height:1.25}.cards small{color:var(--mute);font-size:.75rem}
#empty{color:var(--mute);text-align:center;padding:40px 0}
footer{color:var(--mute);font-size:.75rem;text-align:center;padding:0 16px 24px}
/* recipe */
.bar{position:sticky;top:0;z-index:5;display:flex;justify-content:space-between;align-items:center;gap:8px;
background:var(--bg);border-bottom:1px solid var(--line);padding:6px 12px}
.back{text-decoration:none;font-weight:600;padding:8px 4px;font-size:1rem}
.tools{display:flex;gap:6px}.tools button{border:1px solid var(--line);background:var(--card);border-radius:10px;padding:6px 10px;font-size:.8rem}
#wake[aria-pressed=true]{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
@media (max-width:420px){#wake span{display:none}}
.hero{display:block;width:calc(100% + 32px);margin:0 -16px;max-height:52vh;object-fit:cover}
article h1{font-size:1.75rem;line-height:1.15;margin:18px 0 10px}
.meta{display:flex;flex-wrap:wrap;gap:6px 20px;margin:0 0 10px}.meta div{display:flex;gap:6px}
.meta dt{color:var(--mute)}.meta dd{margin:0;font-weight:600}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
.tag{font-size:.75rem;text-decoration:none;border:1px solid var(--line);border-radius:999px;padding:2px 10px;color:var(--mute)}
.intro{color:var(--mute)}
h2{font-size:1.25rem;margin:26px 0 8px;padding-bottom:4px;border-bottom:2px solid var(--accent);display:inline-block}
h3{font-size:1rem;margin:16px 0 4px;color:var(--accent)}
.ing,.steps{list-style:none;margin:0;padding:0}
.ing li{display:flex;gap:12px;align-items:flex-start;padding:10px 4px;border-bottom:1px solid var(--line);cursor:pointer;font-size:1.05rem}
.box{flex:none;width:1.1em;height:1.1em;margin-top:.2em;border:2px solid var(--mute);border-radius:6px}
.ing li.done{color:var(--done);text-decoration:line-through}.ing li.done .box{background:var(--done);border-color:var(--done)}
.steps li{display:flex;gap:14px;padding:12px 8px;border-radius:12px;cursor:pointer;line-height:1.55;font-size:1.05rem}
.num{flex:none;width:1.8em;height:1.8em;border-radius:50%;background:var(--accent);color:var(--accent-fg);
display:grid;place-items:center;font-weight:700;font-size:.85rem;margin-top:.05em}
.steps li.done{color:var(--done)}.steps li.done .num{background:var(--done)}
.steps li.cur{background:var(--hi)}
.reset{margin:16px 0 0;border:1px solid var(--line);background:var(--card);border-radius:10px;padding:8px 14px;font-size:.85rem}
.notes p,.nutrition p{margin:.5em 0}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px;margin-top:24px}
.gallery img{width:100%;border-radius:12px}.src{color:var(--mute);font-size:.85rem;margin-top:28px;word-break:break-word}
@media (min-width:860px){.cols{display:grid;grid-template-columns:minmax(280px,2fr) 3fr;gap:40px;align-items:start}
.ingredients{position:sticky;top:64px;max-height:calc(100vh - 80px);overflow:auto}
.hero{width:100%;margin:16px 0 0;border-radius:16px;max-height:420px}}
"""

INDEX_JS = r"""
(function(){
var R=window.RECIPES,list=document.getElementById('list'),q=document.getElementById('q'),
chips=document.getElementById('chips'),sortSel=document.getElementById('sort'),cnt=document.getElementById('count'),
empty=document.getElementById('empty'),cat='',st={};
function fold(s){return s.normalize('NFKD').replace(/[̀-ͯ]/g,'').toLowerCase()}
function esc(s){return s.replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
try{st=JSON.parse(sessionStorage.getItem('idx')||'{}')}catch(e){}
var h=decodeURIComponent((location.hash.match(/c=([^&]*)/)||[])[1]||'');
cat=h||st.c||'';q.value=st.q||'';sortSel.value=st.s||'a';
function render(){
 var terms=fold(q.value).split(/\s+/).filter(Boolean),out=[];
 var rs=R.filter(function(r){
  if(cat==='★'&&!r.f)return false;if(cat==='⚑'&&!r.w)return false;
  if(cat&&cat!=='★'&&cat!=='⚑'&&r.c.indexOf(cat)<0)return false;
  return terms.every(function(t){return r.q.indexOf(t)>=0})});
 if(sortSel.value==='n')rs=rs.slice().sort(function(a,b){return b.d-a.d});
 else if(terms.length){var t0=terms.join(' ');rs=rs.slice().sort(function(a,b){
   return (fold(b.t).indexOf(t0)>=0)-(fold(a.t).indexOf(t0)>=0)})}
 rs.forEach(function(r){out.push('<li><a href="/r/'+r.s+'/">'+(r.i?'<img src="/img/'+r.i+'-t.jpg" alt="" loading="lazy" decoding="async">':'<span class=ph>🍽</span>')+
  '<span><b>'+esc(r.t)+'</b><small>'+esc([r.m].concat(r.c.slice(0,3)).filter(Boolean).join(' · '))+'</small></span></a></li>')});
 list.innerHTML=out.join('');empty.hidden=rs.length>0;
 cnt.textContent=rs.length+' of '+R.length;
 [].forEach.call(chips.children,function(b){b.setAttribute('aria-pressed',b.dataset.c===cat)});
 try{sessionStorage.setItem('idx',JSON.stringify({c:cat,q:q.value,s:sortSel.value}))}catch(e){}
}
q.addEventListener('input',render);sortSel.addEventListener('change',render);
chips.addEventListener('click',function(e){var b=e.target.closest('.chip');if(!b)return;cat=b.dataset.c===cat?'':b.dataset.c;
 history.replaceState(null,'',cat?'#c='+encodeURIComponent(cat):location.pathname);render();scrollTo(0,0)});
render();
var act=chips.querySelector('[aria-pressed=true]');if(act&&act.scrollIntoView)act.scrollIntoView({inline:'center',block:'nearest'});
})();
"""

RECIPE_JS = r"""
(function(){
var slug=document.body.dataset.slug,key='ck:'+slug,state={i:{},s:{},t:Date.now()};
try{var s=JSON.parse(localStorage.getItem(key)||'null');if(s&&Date.now()-s.t<12*36e5)state=s}catch(e){}
var reset=document.getElementById('reset');
function save(){state.t=Date.now();try{localStorage.setItem(key,JSON.stringify(state))}catch(e){}paint()}
function paint(){
 var any=false,cur=null;
 document.querySelectorAll('.ing li').forEach(function(li){var d=!!state.i[li.dataset.i];li.classList.toggle('done',d);any=any||d});
 document.querySelectorAll('.steps li').forEach(function(li){var d=!!state.s[li.dataset.s];li.classList.toggle('done',d);any=any||d;
  li.classList.remove('cur');if(!d&&!cur)cur=li});
 if(cur&&Object.keys(state.s).length)cur.classList.add('cur');
 reset.hidden=!any}
document.addEventListener('click',function(e){
 if(e.target.closest('a'))return;
 var li=e.target.closest('.ing li,.steps li');if(!li)return;
 var m=li.dataset.i?state.i:state.s,k=li.dataset.i||li.dataset.s;if(m[k])delete m[k];else m[k]=1;save()});
reset.addEventListener('click',function(){state={i:{},s:{},t:Date.now()};save()});
paint();
// text size
var sizes=['17px','20px','23px','26px','30px'];
function fs(d){var cur=getComputedStyle(document.documentElement).fontSize,i=sizes.indexOf(cur);if(i<0)i=1;
 i=Math.max(0,Math.min(sizes.length-1,i+d));document.documentElement.style.fontSize=sizes[i];try{localStorage.setItem('fs',sizes[i])}catch(e){}}
document.getElementById('fsm').onclick=function(){fs(-1)};document.getElementById('fsp').onclick=function(){fs(1)};
// keep the screen awake while this page is visible
var btn=document.getElementById('wake'),lock=null,want=true;
if('wakeLock' in navigator){
 btn.hidden=false;
 var on=function(){if(!want||document.visibilityState!=='visible'||lock)return;
  navigator.wakeLock.request('screen').then(function(l){lock=l;btn.setAttribute('aria-pressed','true');
   l.addEventListener('release',function(){lock=null;btn.setAttribute('aria-pressed','false')})}).catch(function(){})};
 btn.onclick=function(){want=!want;if(want)on();else if(lock)lock.release()};
 document.addEventListener('visibilitychange',on);
 // some browsers only grant the lock after a user gesture
 document.addEventListener('pointerdown',on,{passive:true});
 on();
}
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
<meta name=robots content=noindex><title>Recipes</title><style>*{box-sizing:border-box}body{font:20px/1.4 system-ui,sans-serif;display:grid;place-items:center;
min-height:90vh;margin:0 16px;background:#fbf8f4;color:#1f1a17}@media(prefers-color-scheme:dark){body{background:#161311;color:#f3ede7}}
form{width:100%;max-width:340px}input,button{width:100%;font:inherit;padding:.6em .8em;border-radius:12px;margin-top:10px}
input{border:1.5px solid #bbb}button{background:#c2410c;color:#fff;border:0;font-weight:600}p{color:#b91c1c}</style>
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
        "background_color": "#fbf8f4", "theme_color": "#c2410c",
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

#!/usr/bin/env python3
"""build_archive_index.py — inject an "Archive" grid into The Daily Cryptograph's
index.html listing every past edition with a thumbnail and its lead headline.

The daily cron run OVERWRITES index.html, so the archive section cannot be
hand-written: this script is what the run calls after writing the new edition,
and it is idempotent — re-running replaces the section it previously injected
(between the ARCHIVE-INDEX markers) rather than appending a second copy.

Thumbnails: each edition's lead Cointelegraph article is fetched once, its
og:image cover downloaded, downscaled with ffmpeg and stored at
archive/<date>/thumb.jpg, then referenced with a repo-relative path. Local
copies (not CDN hotlinks) so the page renders on restrictive networks and
offline. Editions whose image cannot be resolved fall back to a CSS "front
page" miniature built from the masthead + lead headline.

Usage
-----
    python scripts/build_archive_index.py                 # all editions, fetch missing
    python scripts/build_archive_index.py --no-fetch      # offline; reuse cached thumbs
    python scripts/build_archive_index.py --limit 10      # only the 10 newest editions
    python scripts/build_archive_index.py --only 2026-09-28   # one edition

Requires: ffmpeg on PATH (or --ffmpeg-path). Stdlib only otherwise.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import subprocess
import sys

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

START = "<!-- ARCHIVE-INDEX:START -->"
END = "<!-- ARCHIVE-INDEX:END -->"
ANCHORS = ('<div class="bottom-rule">', "</body>")

TITLE_RE = re.compile(r"<title>\s*The Daily Cryptograph\s*[—\-–]\s*([^<]+?)\s*</title>", re.I)
H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.I | re.S)
CT_LINK_RE = re.compile(r"""https://cointelegraph\.com/news/[a-z0-9\-]+""", re.I)
OG_RE = re.compile(
    r"""<meta[^>]+(?:property|name)=["'](?:og:image|twitter:image)["'][^>]+content=["']([^"']+)["']""",
    re.I)
OG_RE_ALT = re.compile(
    r"""<meta[^>]+content=["']([^"']+)["'][^>]+(?:property|name)=["'](?:og:image|twitter:image)["']""",
    re.I)

MONTHS = ("January February March April May June July August September "
          "October November December").split()


def strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def nice_date(iso: str, raw: str | None) -> str:
    """'2026-09-28' -> 'Mon, Sep 28, 2026' (falls back to the page's own text)."""
    try:
        y, m, d = (int(p) for p in iso.split("-"))
        import datetime as _dt
        return _dt.date(y, m, d).strftime("%a, %b %-d, %Y") if os.name != "nt" \
            else _dt.date(y, m, d).strftime("%a, %b %d, %Y").replace(" 0", " ")
    except Exception:
        return raw or iso


def edition_meta(path: str, date: str) -> dict:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        doc = fh.read()
    m = TITLE_RE.search(doc)
    raw_date = m.group(1) if m else None

    lead = None
    m2 = H2_RE.search(doc)
    if m2:
        lead = strip_tags(m2.group(1))
    if not lead:
        m3 = re.search(r"<h3[^>]*>(.*?)</h3>", doc, re.I | re.S)
        if m3:
            lead = strip_tags(m3.group(1))
    if not lead:
        lead = "The Daily Cryptograph"

    links = CT_LINK_RE.findall(doc)
    return {
        "date": date,
        "date_label": nice_date(date, raw_date),
        "lead": lead,
        "stories": len(re.findall(r"<h3[^>]*>", doc, re.I)),
        "lead_url": links[0] if links else None,
    }


def curl(url: str, timeout: int = 25) -> str | None:
    try:
        r = subprocess.run(["curl", "-sL", "--max-time", str(timeout), "-A", UA, url],
                           capture_output=True, timeout=timeout + 10)
        if r.returncode == 0 and r.stdout:
            return r.stdout.decode("utf-8", "replace")
    except Exception:
        pass
    return None


def fetch_og_image(article_url: str) -> str | None:
    doc = curl(article_url)
    if not doc:
        return None
    for rx in (OG_RE, OG_RE_ALT):
        m = rx.search(doc)
        if m:
            return m.group(1).strip()
    return None


def download(url: str, dest: str, timeout: int = 30) -> bool:
    try:
        r = subprocess.run(["curl", "-sL", "--max-time", str(timeout), "-A", UA,
                            "-o", dest, url], capture_output=True, timeout=timeout + 10)
        return r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 2048
    except Exception:
        return False


def downscale(src: str, dest: str, width: int, ffmpeg: str) -> bool:
    """Resize to `width` px wide, preserving aspect (even height), JPEG q~6."""
    try:
        r = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", src,
             "-vf", f"scale={width}:-2", "-q:v", "6", "-f", "mjpeg", dest],
            capture_output=True, timeout=60)
        if r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 1024:
            os.remove(src)
            return True
    except Exception:
        pass
    return False


def ensure_thumb(repo: str, date: str, article_url: str | None, fetch: bool,
                 width: int, ffmpeg: str | None) -> str | None:
    """Return a repo-relative thumbnail path, or None for the CSS fallback."""
    adir = os.path.join(repo, "archive", date)
    thumb = os.path.join(adir, "thumb.jpg")
    if os.path.exists(thumb):
        return f"archive/{date}/thumb.jpg"
    if not (fetch and article_url and ffmpeg):
        return None

    img = fetch_og_image(article_url)
    if not img:
        return None
    tmp = os.path.join(adir, "_thumb_src")
    if not download(img, tmp):
        return None
    if downscale(tmp, thumb, width, ffmpeg):
        return f"archive/{date}/thumb.jpg"
    if os.path.exists(tmp):
        os.remove(tmp)
    return None


def card_html(meta: dict, thumb: str | None) -> str:
    href = f"archive/{meta['date']}/"
    lead = html.escape(meta["lead"])
    if len(lead) > 150:
        lead = lead[:147].rstrip() + "…"
    if thumb:
        thumb_html = (f'<img src="{thumb}" alt="{html.escape(meta["date_label"])} '
                      f'front page" loading="lazy" width="400" height="266">')
    else:
        thumb_html = ('<span class="tc-ph">'
                      f'<b>The Daily<br>Cryptograph</b><i>{html.escape(meta["date_label"])}</i>'
                      "</span>")
    return (
        f'<a class="tc-card" href="{href}">'
        f'<span class="tc-thumb">{thumb_html}</span>'
        f'<span class="tc-meta">'
        f'<span class="tc-date">{html.escape(meta["date_label"])}</span>'
        f'<span class="tc-lead">{lead}</span>'
        f'<span class="tc-count">{meta["stories"]} stories</span>'
        f"</span></a>"
    )


SECTION_CSS = """
<style>
  .tc-archive-wrap { margin: 34px 0 10px; }
  .tc-archive-wrap .tc-arch-head { text-align:center; border-top:3px double #241f1a;
    border-bottom:1px solid #241f1a; padding:10px 0 8px; margin-bottom:6px; }
  .tc-archive-wrap .tc-arch-head h2 { font-family:'UnifrakturMaguntia',serif;
    font-size:30px; margin:0; letter-spacing:1px; }
  .tc-archive-wrap .tc-arch-sub { text-align:center; font-size:10px; letter-spacing:3px;
    text-transform:uppercase; color:#6b6257; margin:8px 0 22px;
    font-family:'Courier Prime',monospace; overflow-wrap:anywhere; }
  .tc-archive-wrap .tc-grid { display:grid;
    grid-template-columns:repeat(auto-fill,minmax(min(158px,100%),1fr)); gap:18px 16px; }
  .tc-archive-wrap .tc-card { display:block; text-decoration:none; color:inherit;
    border:1px solid #cfc5b3; background:#f7f1e3; padding:6px;
    box-shadow:0 1px 0 #e6dcc8 inset; transition:transform .12s ease, box-shadow .12s ease; }
  .tc-archive-wrap .tc-card:hover { transform:translateY(-2px);
    box-shadow:0 4px 12px rgba(36,31,26,.22); border-color:#8b2010; }
  .tc-archive-wrap .tc-thumb { display:block; aspect-ratio:3/2; overflow:hidden;
    background:#241f1a; border:1px solid #241f1a; margin-bottom:7px; }
  .tc-archive-wrap .tc-thumb img { width:100%; height:100%; object-fit:cover;
    display:block; filter:sepia(.18) contrast(1.03); }
  .tc-archive-wrap .tc-ph { display:flex; flex-direction:column; align-items:center;
    justify-content:center; height:100%; background:#efe6d2; color:#241f1a; gap:4px;
    padding:6px; text-align:center; }
  .tc-archive-wrap .tc-ph b { font-family:'UnifrakturMaguntia',serif; font-size:13px;
    line-height:1.05; }
  .tc-archive-wrap .tc-ph i { font-size:8px; letter-spacing:1px; text-transform:uppercase;
    font-style:normal; color:#6b6257; font-family:'Courier Prime',monospace; }
  .tc-archive-wrap .tc-meta { display:block; }
  .tc-archive-wrap .tc-date { display:block; font-size:9px; letter-spacing:2px;
    text-transform:uppercase; color:#8b2010; font-family:'Courier Prime',monospace;
    margin-bottom:3px; }
  .tc-archive-wrap .tc-lead { display:block; font-family:Georgia,'Times New Roman',serif;
    font-size:11.5px; line-height:1.28; color:#241f1a; }
  .tc-archive-wrap .tc-count { display:block; font-size:8px; letter-spacing:1.5px;
    text-transform:uppercase; color:#8a8073; margin-top:5px;
    font-family:'Courier Prime',monospace; }
  @media (max-width:520px){ .tc-archive-wrap .tc-grid {
    grid-template-columns:repeat(auto-fill,minmax(min(132px,100%),1fr)); gap:12px 10px; }
    .tc-archive-wrap .tc-arch-sub { letter-spacing:1.5px; font-size:9px; }
    .tc-archive-wrap .tc-arch-head h2 { font-size:25px; } }
</style>
"""


def build_section(cards: list[str]) -> str:
    return (
        f"{START}\n{SECTION_CSS}"
        '<div class="tc-archive-wrap">\n'
        '  <div class="tc-arch-head"><h2>The Archive</h2></div>\n'
        '  <div class="tc-arch-sub">Every past edition &nbsp;✦&nbsp; previously published broadsheets</div>\n'
        '  <div class="tc-grid">\n'
        + "\n".join("    " + c for c in cards)
        + "\n  </div>\n</div>\n"
        f"{END}"
    )


def inject(repo: str, section: str) -> str:
    idx = os.path.join(repo, "index.html")
    with open(idx, "r", encoding="utf-8", errors="replace") as fh:
        doc = fh.read()

    if START in doc and END in doc:
        pre = doc[:doc.index(START)]
        post = doc[doc.index(END) + len(END):]
        doc = pre + section + post
        how = "replaced existing section"
    else:
        anchor, at = None, -1
        for a in ANCHORS:          # priority order: keep the colophon last
            p = doc.rfind(a)
            if p >= 0:
                anchor, at = a, p
                break
        if at < 0:
            raise SystemExit("could not find an insertion anchor in index.html")
        # keep the section off the very last line of the document
        doc = doc[:at] + section + "\n\n" + doc[at:]
        how = f"inserted before {anchor!r}"

    with open(idx, "w", encoding="utf-8", newline="") as fh:
        fh.write(doc)
    return how


def find_ffmpeg(explicit: str | None) -> str | None:
    if explicit:
        return explicit if os.path.exists(explicit) else None
    w = shutil.which("ffmpeg")
    if w:
        return w
    for c in (os.path.expanduser("~/bin/ffmpeg.exe"),
              r"C:\Program Files\Jellyfin\Server\ffmpeg.exe"):
        if os.path.exists(c):
            return c
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.dirname(os.path.abspath(__file__)) + os.sep + "..")
    ap.add_argument("--no-fetch", action="store_true", help="never hit the network")
    ap.add_argument("--limit", type=int, default=0, help="only the N newest editions")
    ap.add_argument("--only", action="append", default=None, help="one date (repeatable)")
    ap.add_argument("--width", type=int, default=400)
    ap.add_argument("--ffmpeg-path", default=None)
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    arch = os.path.join(repo, "archive")
    if not os.path.isdir(arch):
        print(f"no archive/ under {repo}", file=sys.stderr)
        return 2

    dates = sorted(d for d in os.listdir(arch)
                   if os.path.isfile(os.path.join(arch, d, "index.html")))
    if args.only:
        dates = [d for d in dates if d in set(args.only)]
    if args.limit:
        dates = dates[-args.limit:]
    if not dates:
        print("no editions found", file=sys.stderr)
        return 2

    ffmpeg = None if args.no_fetch else find_ffmpeg(args.ffmpeg_path)
    if not args.no_fetch and not ffmpeg:
        print("warning: ffmpeg not found — thumbnails will use the CSS fallback",
              file=sys.stderr)

    metas, got, missing = [], 0, 0
    for i, d in enumerate(dates, 1):
        meta = edition_meta(os.path.join(arch, d, "index.html"), d)
        thumb = ensure_thumb(repo, d, meta["lead_url"], not args.no_fetch,
                             args.width, ffmpeg)
        if thumb:
            got += 1
        else:
            missing += 1
        metas.append((meta, thumb))
        print(f"[{i}/{len(dates)}] {d}  thumb={'yes' if thumb else 'no (fallback)'}"
              f"  {meta['lead'][:58]!r}", flush=True)

    cards = [card_html(m, t) for m, t in sorted(metas, key=lambda x: x[0]["date"],
                                                reverse=True)]
    how = inject(repo, build_section(cards))

    print(f"\n{len(dates)} editions -> {how}")
    print(f"thumbnails: {got} local / {missing} CSS fallback")
    print(f"index.html: {os.path.getsize(os.path.join(repo, 'index.html')):,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())

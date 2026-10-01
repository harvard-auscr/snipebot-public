"""Local review queue: snipe photos whose face read is ambiguous, for an owner's yes/no.

Read-only towards Slack and the data branch. `scan` reads channel history for one
semester, runs the face detector on every tagged photo post and caches the boxes; the
owner labels each queued post yes (a fine snipe) or no (should not have counted) in a
local gallery or from the command line. Labels are calibration data and never change a
score: removing a snipe stays the explicit `veto`.

Everything lives in one local folder (default `review/`, gitignored): `faces.json` (ts,
file id, rendition hash prefix, integer boxes) and `labels.jsonl` (ts, label, unix
seconds). No names, URLs, tokens or image bytes are ever written; images are fetched
from Slack on demand and served from memory to the local gallery only.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import secrets
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from snipebot.parse import Candidate, ParseAnomaly, parse, rendition_url
from snipebot.slack_io import AuthError, MissingScope
from snipebot.ts import parse_ts

# 2: scores are floored to thousandths (1 rounded, so 0.8996 read as a clear 900).
CACHE_VERSION = 2
FAINT_MILLI = 600  # a box scoring at least this is drawn and counted as a faint face

LABELS = ("yes", "no", "clear")

# Queue reasons, most interesting first.
REPEAT = "repeat_photo"          # the same rendition bytes appear in another post
MANY_TAGS = "many_tags"          # review.min_targets or more people tagged
NO_FACE = "no_clear_face"        # no face at the live threshold in any image
FEWER = "fewer_faces_than_tags"  # some faces, fewer than the people tagged
EXTRA = "extra_faces"            # more faces than the people tagged
UNREAD = "photo_unreadable"      # a live photo the scan could not fetch or decode
REASONS = (REPEAT, MANY_TAGS, UNREAD, NO_FACE, FEWER, EXTRA)
REASON_TEXT = {
    REPEAT: "photo also posted elsewhere",
    MANY_TAGS: "many people tagged",
    NO_FACE: "no clear face",
    FEWER: "fewer faces than tags",
    EXTRA: "more faces than tags",
    UNREAD: "photo could not be read",
}


class ReviewError(Exception):
    """A malformed review folder or argument; the CLI maps it to a config-invalid exit."""


def threshold_milli(score_threshold: str) -> int:
    """The config's decimal score threshold ("0.9") as integer thousandths (900)."""
    try:
        value = Decimal(score_threshold)
    except InvalidOperation as exc:
        raise ReviewError(f"faces.score_threshold is not a decimal: {score_threshold!r}") from exc
    return int(value * 1000)


# --- the folder --------------------------------------------------------------

def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def load_cache(folder: Path) -> dict:
    path = folder / "faces.json"
    if not path.exists():
        return {"version": CACHE_VERSION, "posts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReviewError(f"{path} is unreadable: {exc}") from exc
    version = data.get("version") if isinstance(data, dict) else None
    if isinstance(version, int) and 0 < version < CACHE_VERSION:
        # An older detector wrote it: start over (the next scan rereads every photo).
        return {"version": CACHE_VERSION, "posts": {}}
    if not isinstance(data, dict) or version != CACHE_VERSION \
            or not isinstance(data.get("posts"), dict):
        raise ReviewError(f"{path} is not a version-{CACHE_VERSION} review cache")
    return data


def save_cache(folder: Path, cache: dict) -> None:
    _write_atomic(folder / "faces.json", json.dumps(cache, sort_keys=True, indent=1) + "\n")


def load_labels(folder: Path) -> dict[str, str]:
    """ts -> the latest label ("yes" or "no"); a later "clear" removes it."""
    path = folder / "labels.jsonl"
    labels: dict[str, str] = {}
    if not path.exists():
        return labels
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            ts, label = rec["ts"], rec["label"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ReviewError(f"{path}:{n} is not a label record") from exc
        if label not in LABELS:
            raise ReviewError(f"{path}:{n} has an unknown label {label!r}")
        if label == "clear":
            labels.pop(ts, None)
        else:
            labels[ts] = label
    return labels


_label_lock = threading.Lock()


def append_label(folder: Path, ts: str, label: str, now_s: int | None = None) -> None:
    parse_ts(ts)  # raises TsFormatError on anything that is not a Slack ts
    if label not in LABELS:
        raise ReviewError(f"label must be one of {', '.join(LABELS)}")
    rec = {"ts": ts, "label": label, "at": int(time.time()) if now_s is None else now_s}
    folder.mkdir(parents=True, exist_ok=True)
    with _label_lock, open(folder / "labels.jsonl", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")


# --- scan ----------------------------------------------------------------------

@dataclass
class ScanResult:
    posts: int            # tagged photo posts in the window
    detected: int         # images newly run through the detector
    failed: int           # images that could not be fetched or decoded this time
    urls: dict            # (ts, file_id) -> rendition URL, in memory only


def scan(slack, channel: str, oldest: str, latest: str, detector, cache: dict,
         bot_user_id: str, progress=None) -> ScanResult:
    """Detect faces on every not-yet-cached image of every tagged photo post in
    [oldest, latest]. A post is cached with its tag count (self-tags excluded) and one
    entry per live image; an image that fails is left out and retried next scan. Once the
    whole history is read, a cached post in the window that is no longer a tagged photo
    post (deleted, every photo gone, every tag removed) is dropped. Parse anomalies are
    silenced: review audits nothing, and their text carries user IDs."""
    posts = cache["posts"]
    urls: dict = {}
    current: set[str] = set()
    seen = detected = failed = 0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ParseAnomaly)
        for message in slack.history(channel, oldest, latest):
            row = parse(message, channel, bot_user_id)
            if not isinstance(row, Candidate) or not row.live_image_ids:
                continue
            targets = sum(1 for t in row.targets if t != row.sender)
            if targets == 0:
                continue
            seen += 1
            current.add(row.ts)
            entry = posts.setdefault(row.ts, {"targets": targets, "images": {}})
            entry["targets"] = targets
            live = set(row.live_image_ids)
            entry["images"] = {k: v for k, v in entry["images"].items() if k in live}
            for f in message.get("files", []):
                file_id = f.get("id")
                if file_id not in live:
                    continue
                url = rendition_url(f)
                if url is None:
                    failed += 1
                    continue
                urls[(row.ts, file_id)] = url
                if file_id in entry["images"]:
                    continue
                try:
                    data = slack.fetch_file_bytes(url)
                    boxes, w, h = detector.detect_boxes(data)
                except (MissingScope, AuthError):
                    raise  # the token cannot read files: no photo would be read, so stop
                except Exception:  # a fetch or decode fault: retried on the next scan
                    failed += 1
                    continue
                entry["images"][file_id] = {
                    "sha": hashlib.sha256(data).hexdigest()[:16],
                    "w": w, "h": h, "boxes": [list(b) for b in boxes],
                }
                detected += 1
            entry["unread"] = sum(1 for i in live if i not in entry["images"])
            if progress is not None and seen % 25 == 0:
                progress(seen, detected)
    # Only after a complete history read: a fault above raises past this, so the
    # caller's save keeps every entry it had.
    low, high = parse_ts(oldest), parse_ts(latest)
    for ts in [t for t in posts if t not in current and low <= parse_ts(t) <= high]:
        del posts[ts]
    return ScanResult(posts=seen, detected=detected, failed=failed, urls=urls)


# --- queue -----------------------------------------------------------------------

def reasons_for(ts: str, post: dict, clear_milli: int, min_targets: int | None,
                sha_posts: dict[str, set[str]]) -> list[str]:
    images = post["images"].values()
    unread = post.get("unread", 0)
    if not images:
        return [UNREAD] if unread else []
    clear = _best(images, lambda b: b[4] >= clear_milli)
    targets = post["targets"]
    out = []
    if any(len(sha_posts.get(img["sha"], ())) > 1 for img in images):
        out.append(REPEAT)
    if min_targets is not None and targets >= min_targets:
        out.append(MANY_TAGS)
    if unread:
        out.append(UNREAD)
    if clear == 0:
        out.append(NO_FACE)
    elif clear < targets:
        out.append(FEWER)
    elif clear > targets:
        out.append(EXTRA)
    return out


def _best(images, keep) -> int:
    """The highest per-image count of boxes `keep` accepts (0 with no images)."""
    return max((sum(1 for b in img["boxes"] if keep(b)) for img in images), default=0)


def sha_index(cache: dict) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for ts, post in cache["posts"].items():
        for img in post["images"].values():
            index.setdefault(img["sha"], set()).add(ts)
    return index


@dataclass(frozen=True)
class QueueItem:
    ts: str
    reasons: tuple[str, ...]
    targets: int
    clear_faces: int
    faint_faces: int
    label: str | None


def build_queue(cache: dict, labels: dict[str, str], clear_milli: int,
                min_targets: int | None, start_us: int, end_us: int,
                everything: bool = False) -> list[QueueItem]:
    """Queued posts in [start_us, end_us] (inclusive, as Semester): unlabeled first, then
    by reason priority, then newest first. `everything` also lists posts with no reason
    (for a full pass)."""
    index = sha_index(cache)
    items = []
    for ts, post in cache["posts"].items():
        if not (start_us <= parse_ts(ts) <= end_us) \
                or not (post["images"] or post.get("unread", 0)):
            continue
        why = reasons_for(ts, post, clear_milli, min_targets, index)
        if not why and not everything:
            continue
        items.append(QueueItem(
            ts=ts, reasons=tuple(why), targets=post["targets"],
            clear_faces=_best(post["images"].values(), lambda b: b[4] >= clear_milli),
            faint_faces=_best(post["images"].values(),
                              lambda b: FAINT_MILLI <= b[4] < clear_milli),
            label=labels.get(ts),
        ))
    rank = {r: i for i, r in enumerate(REASONS)}
    items.sort(key=lambda it: (
        it.label is not None,
        min((rank[r] for r in it.reasons), default=len(REASONS)),
        -parse_ts(it.ts),
    ))
    return items


def permalink(workspace_url: str, channel: str, ts: str) -> str:
    return f"{workspace_url.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}"


# --- stats ---------------------------------------------------------------------

def stats_lines(cache: dict, labels: dict[str, str], clear_milli: int,
                min_targets: int | None, start_us: int, end_us: int) -> list[str]:
    """Per reason: how the owner's labels fell. Then, for each candidate threshold, how
    the labels fell on posts with no face at that threshold: where 'no' piles up is
    where a face rule would have caught it."""
    items = build_queue(cache, labels, clear_milli, min_targets, start_us, end_us,
                        everything=True)
    lines = [f"posts {len(items)}  labeled {sum(1 for i in items if i.label)}"
             f"  yes {sum(1 for i in items if i.label == 'yes')}"
             f"  no {sum(1 for i in items if i.label == 'no')}", "",
             f"{'reason':<24}{'posts':>6}{'yes':>6}{'no':>6}{'open':>6}"]
    for reason in REASONS + ("(none)",):
        group = [i for i in items if (reason in i.reasons) or (reason == "(none)" and not i.reasons)]
        lines.append(
            f"{reason:<24}{len(group):>6}{sum(1 for i in group if i.label == 'yes'):>6}"
            f"{sum(1 for i in group if i.label == 'no'):>6}"
            f"{sum(1 for i in group if i.label is None):>6}")
    lines += ["", "no face at threshold  posts   yes    no"]
    read = {i.ts for i in items if cache["posts"][i.ts]["images"]}
    for milli in sorted(set(range(500, 1000, 50)) | {clear_milli}):
        group = [
            ts for ts in read
            if _best(cache["posts"][ts]["images"].values(), lambda b: b[4] >= milli) == 0
        ]
        mark = "  <- live" if milli == clear_milli else ""
        lines.append(
            f"  {milli // 1000}.{milli % 1000:03d}{'':<14}{len(group):>6}"
            f"{sum(1 for ts in group if labels.get(ts) == 'yes'):>6}"
            f"{sum(1 for ts in group if labels.get(ts) == 'no'):>6}{mark}")
    no = sorted((i.ts for i in items if i.label == "no"), key=parse_ts)
    if no:
        lines += ["", "labeled no (scores unchanged; veto to remove one):"]
        lines += [f"  {ts}" for ts in no]
    return lines


# --- gallery -------------------------------------------------------------------

def _sniff_type(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def gallery_payload(items: list[QueueItem], cache: dict, clear_milli: int,
                    workspace_url: str, channel: str, local_time) -> list[dict]:
    out = []
    for it in items:
        post = cache["posts"][it.ts]
        out.append({
            "ts": it.ts, "when": local_time(it.ts), "reasons": list(it.reasons),
            "targets": it.targets, "clear": it.clear_faces, "faint": it.faint_faces,
            "label": it.label, "link": permalink(workspace_url, channel, it.ts),
            "images": [
                {"id": fid, "w": img["w"], "h": img["h"],
                 "boxes": [[b[0], b[1], b[2], b[3], b[4] >= clear_milli]
                           for b in img["boxes"] if b[4] >= FAINT_MILLI]}
                for fid, img in sorted(post["images"].items())
            ],
        })
    return out


def serve(folder: Path, items_payload: list[dict], fetch, urls: dict, title: str,
          open_browser: bool = True, port: int = 0):
    """Serve the gallery on 127.0.0.1 behind a random path secret until Ctrl+C.
    `fetch(url) -> bytes` reads a rendition from Slack; bytes live only in memory."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    secret = secrets.token_urlsafe(16)
    known_ts = {p["ts"] for p in items_payload}
    labels_now = {p["ts"]: p["label"] for p in items_payload}
    page = _PAGE.replace("__TITLE__", html.escape(title)).replace(
        "__DATA__", json.dumps(items_payload).replace("</", "<\\/")).replace(
        "__REASONS__", json.dumps(REASON_TEXT))
    image_cache: dict = {}
    image_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # nothing is logged: paths carry message ts
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _route(self):
            parts = self.path.split("?", 1)[0].strip("/").split("/")
            if not parts or not secrets.compare_digest(parts[0], secret):
                return None
            return parts[1:]

        def do_GET(self):
            route = self._route()
            if route is None:
                return self._send(404, b"not found", "text/plain")
            if route == [] or route == [""]:
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if len(route) == 3 and route[0] == "img" and (route[1], route[2]) in urls:
                key = (route[1], route[2])
                with image_lock:
                    data = image_cache.get(key)
                if data is None:
                    try:
                        data = fetch(urls[key])
                    except Exception:
                        return self._send(502, b"could not fetch", "text/plain")
                    with image_lock:
                        if len(image_cache) > 64:
                            image_cache.clear()
                        image_cache[key] = data
                return self._send(200, data, _sniff_type(data))
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            route = self._route()
            if route != ["label"]:
                return self._send(404, b"not found", "text/plain")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("size")
                body = json.loads(self.rfile.read(length))
                ts, label = body["ts"], body["label"]
                if ts not in known_ts or label not in LABELS:
                    raise ValueError("unknown")
                append_label(folder, ts, label)
            except Exception:
                return self._send(400, b"bad label", "text/plain")
            labels_now[ts] = None if label == "clear" else label
            return self._send(200, b"ok", "text/plain")

    class Server(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            # A browser that drops a request mid-reply (a cancelled lazy image load, a
            # filter click, a closed tab) is normal use: no traceback for it, and only a
            # one-line note naming the error type for anything else.
            exc = sys.exc_info()[1]
            if not isinstance(exc, OSError):
                print(f"review gallery: a request failed ({type(exc).__name__})",
                      file=sys.stderr)

    server = Server(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/{secret}/"
    print(f"review gallery: {url}")
    print("Ctrl+C to stop; labels are saved as you click.")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return labels_now


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root { --bg:#f6f5f2; --card:#fff; --ink:#1d1d1b; --muted:#6b6a66; --line:#e3e1dc;
  --clear:#1f9d55; --faint:#d99a00; --yes:#1f9d55; --no:#c8372d; --chip:#efede8; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --card:#21211f; --ink:#ecebe7;
  --muted:#a3a19b; --line:#34332f; --chip:#2c2b28; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { position:sticky; top:0; z-index:2; background:var(--bg); border-bottom:1px solid var(--line);
  padding:12px 16px; display:flex; flex-wrap:wrap; gap:8px 16px; align-items:center; }
h1 { font-size:17px; margin:0; }
.filters { display:flex; flex-wrap:wrap; gap:6px; }
.filters button { border:1px solid var(--line); background:var(--card); color:var(--ink);
  border-radius:999px; padding:4px 10px; font:inherit; font-size:13px; cursor:pointer; }
.filters button[aria-pressed=true] { background:var(--ink); color:var(--bg); }
.progress { color:var(--muted); font-size:13px; margin-left:auto; }
main { padding:16px; display:grid; gap:16px; grid-template-columns:repeat(auto-fill, minmax(min(300px, 100%), 1fr)); }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden;
  display:flex; flex-direction:column; outline:none; }
.card:focus-visible { box-shadow:0 0 0 3px #4a7cf0; }
.card[data-label=yes] { border-color:var(--yes); }
.card[data-label=no] { border-color:var(--no); }
.shots { display:flex; gap:2px; overflow-x:auto; background:#000; }
.shot { position:relative; flex:0 0 100%; }
.shot img { display:block; width:100%; height:auto; }
.box { position:absolute; border:2px solid var(--clear); border-radius:3px; pointer-events:none; }
.box.faint { border-color:var(--faint); border-style:dashed; }
.body { padding:10px 12px 12px; display:flex; flex-direction:column; gap:8px; }
.chips { display:flex; flex-wrap:wrap; gap:4px; }
.chip { background:var(--chip); border-radius:6px; padding:2px 7px; font-size:12px; }
.meta { color:var(--muted); font-size:13px; display:flex; justify-content:space-between; gap:8px; }
.meta a { color:inherit; }
.actions { display:flex; gap:8px; }
.actions button { flex:1; border:1px solid var(--line); background:transparent; color:var(--ink);
  border-radius:8px; padding:8px; font:inherit; cursor:pointer; }
.actions button.yes[aria-pressed=true] { background:var(--yes); border-color:var(--yes); color:#fff; }
.actions button.no[aria-pressed=true] { background:var(--no); border-color:var(--no); color:#fff; }
.empty { color:var(--muted); padding:40px 16px; text-align:center; grid-column:1/-1; }
.legend { color:var(--muted); font-size:12px; flex-basis:100%; }
.card, .body, .meta span { min-width:0; }
</style></head><body>
<header>
  <h1>__TITLE__</h1>
  <div class="filters" id="filters"></div>
  <span class="progress" id="progress"></span>
  <span class="legend">green box = clear face, dashed = faint. Keys: y / n, u clears, arrows move.</span>
</header>
<main id="grid"></main>
<script>
const DATA = __DATA__;
const REASONS = __REASONS__;
let filter = "open";
const grid = document.getElementById("grid");

function counts() {
  const c = { open: 0, all: DATA.length, yes: 0, no: 0 };
  for (const r of Object.keys(REASONS)) c[r] = 0;
  for (const p of DATA) {
    if (!p.label) c.open++; else c[p.label]++;
    for (const r of p.reasons) c[r]++;
  }
  return c;
}
function renderFilters() {
  const c = counts();
  const opts = [["open", "To review"], ["all", "All"], ["yes", "Yes"], ["no", "No"]]
    .concat(Object.entries(REASONS));
  const el = document.getElementById("filters");
  el.innerHTML = "";
  for (const [key, text] of opts) {
    if (c[key] === 0 && !["open", "all"].includes(key)) continue;
    const b = document.createElement("button");
    b.textContent = `${text} ${c[key]}`;
    b.setAttribute("aria-pressed", String(filter === key));
    b.onclick = () => { filter = key; render(); };
    el.appendChild(b);
  }
  document.getElementById("progress").textContent =
    `${c.yes + c.no} of ${c.all} labeled`;
}
function visible(p) {
  if (filter === "open") return !p.label;
  if (filter === "all") return true;
  if (filter === "yes" || filter === "no") return p.label === filter;
  return p.reasons.includes(filter);
}
async function setLabel(p, label, card) {
  const res = await fetch("label", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ts: p.ts, label }) });
  if (!res.ok) { alert("Could not save that label."); return; }
  p.label = label === "clear" ? null : label;
  card.dataset.label = p.label || "";
  for (const b of card.querySelectorAll(".actions button"))
    b.setAttribute("aria-pressed", String(b.dataset.label === p.label));
  renderFilters();
}
function card(p) {
  const c = document.createElement("article");
  c.className = "card"; c.tabIndex = 0; c.dataset.label = p.label || "";
  const shots = document.createElement("div"); shots.className = "shots";
  for (const img of p.images) {
    const s = document.createElement("div"); s.className = "shot";
    const i = document.createElement("img");
    i.loading = "lazy"; i.alt = "snipe photo";
    i.src = `img/${encodeURIComponent(p.ts)}/${encodeURIComponent(img.id)}`;
    s.appendChild(i);
    for (const [x, y, w, h, clear] of img.boxes) {
      const b = document.createElement("div");
      b.className = "box" + (clear ? "" : " faint");
      b.style.left = `${100 * x / img.w}%`; b.style.top = `${100 * y / img.h}%`;
      b.style.width = `${100 * w / img.w}%`; b.style.height = `${100 * h / img.h}%`;
      s.appendChild(b);
    }
    shots.appendChild(s);
  }
  const body = document.createElement("div"); body.className = "body";
  const chips = document.createElement("div"); chips.className = "chips";
  for (const r of p.reasons) {
    const s = document.createElement("span"); s.className = "chip"; s.textContent = REASONS[r];
    chips.appendChild(s);
  }
  const meta = document.createElement("div"); meta.className = "meta";
  const left = document.createElement("span");
  left.textContent = `${p.when} · ${p.targets} tagged · ${p.clear} clear face${p.clear === 1 ? "" : "s"}`
    + (p.faint ? ` · ${p.faint} faint` : "") + (p.images.length > 1 ? ` · ${p.images.length} photos` : "");
  const link = document.createElement("a");
  link.href = p.link; link.textContent = "Slack"; link.target = "_blank"; link.rel = "noreferrer";
  meta.append(left, link);
  const actions = document.createElement("div"); actions.className = "actions";
  for (const [label, text] of [["yes", "Yes, fine"], ["no", "No"]]) {
    const b = document.createElement("button");
    b.className = label; b.dataset.label = label; b.textContent = text;
    b.setAttribute("aria-pressed", String(p.label === label));
    b.onclick = () => setLabel(p, p.label === label ? "clear" : label, c);
    actions.appendChild(b);
  }
  body.append(chips, meta, actions);
  c.append(shots, body);
  c.addEventListener("keydown", (e) => {
    if (e.key === "y") setLabel(p, "yes", c);
    else if (e.key === "n") setLabel(p, "no", c);
    else if (e.key === "u") setLabel(p, "clear", c);
    else if (e.key === "ArrowRight" || e.key === "ArrowDown") { (c.nextElementSibling || c).focus(); e.preventDefault(); }
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") { (c.previousElementSibling || c).focus(); e.preventDefault(); }
  });
  return c;
}
function render() {
  renderFilters();
  grid.innerHTML = "";
  const shown = DATA.filter(visible);
  if (!shown.length) {
    const e = document.createElement("p"); e.className = "empty";
    e.textContent = filter === "open" ? "Nothing left to review." : "Nothing here.";
    grid.appendChild(e); return;
  }
  for (const p of shown) grid.appendChild(card(p));
}
render();
</script></body></html>
"""

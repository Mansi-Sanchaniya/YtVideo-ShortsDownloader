"""YT Downloader - one Flet code base for a Windows .exe and an Android .apk."""
import asyncio
import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile

import flet as ft

APP_NAME = "YT Downloader"
IS_WINDOWS = sys.platform == "win32"
IS_ANDROID = sys.platform == "android" or bool(os.environ.get("ANDROID_ROOT"))


# ----------------------------------------------------------------------
# Folders
# ----------------------------------------------------------------------
def _data_dir():
    base = os.environ.get("FLET_APP_STORAGE_DATA") or os.path.join(
        os.path.expanduser("~"), ".yt_downloader"
    )
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = tempfile.gettempdir()
    return base


DATA_DIR = _data_dir()
UPDATE_ROOT = os.path.join(DATA_DIR, "ytdlp_update")
UPDATE_CURRENT = os.path.join(UPDATE_ROOT, "current")


def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v)))


def _use_updated_ytdlp():
    """If the user ran the in-app updater, load that yt-dlp instead of the bundled one
    (unless the bundled one is newer, e.g. after an app update)."""
    version_file = os.path.join(UPDATE_CURRENT, "yt_dlp", "version.py")
    if not os.path.exists(version_file):
        return
    try:
        from importlib.metadata import version

        bundled = version("yt-dlp")
        with open(version_file, encoding="utf-8") as f:
            updated = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", f.read()).group(1)
        if _ver_tuple(updated) <= _ver_tuple(bundled):
            return
    except Exception:
        pass  # not sure -> prefer the updated copy
    sys.path.insert(0, UPDATE_CURRENT)


_use_updated_ytdlp()

from yt_dlp import YoutubeDL  # noqa: E402
from yt_dlp.version import __version__ as YTDLP_VERSION  # noqa: E402


def _pick_download_dir():
    if IS_ANDROID:
        candidates = [
            "/storage/emulated/0/Download/YT Downloader",
            os.path.join(DATA_DIR, "downloads"),
        ]
    else:
        candidates = [os.path.join(os.path.expanduser("~"), "Downloads", "YT Downloader")]
    candidates.append(os.path.join(DATA_DIR, "downloads"))
    for c in candidates:
        try:
            os.makedirs(c, exist_ok=True)
            probe = os.path.join(c, ".write_test")
            with open(probe, "w") as f:
                f.write("x")
            os.remove(probe)
            return c
        except Exception:
            continue
    return tempfile.gettempdir()


DOWNLOAD_DIR = _pick_download_dir()


# ----------------------------------------------------------------------
# Bundled tools: ffmpeg + Deno (Windows). Android has neither.
# ----------------------------------------------------------------------
def _find_deno():
    try:
        import deno

        try:
            p = deno.find_deno_bin()
            if p and os.path.exists(p):
                return p
        except Exception:
            pass
        base = os.path.dirname(deno.__file__)
        for root, _dirs, files in os.walk(base):
            for name in files:
                if name.lower() in ("deno", "deno.exe"):
                    return os.path.join(root, name)
    except Exception:
        pass
    return shutil.which("deno")


def _setup_tools():
    ffmpeg_dir = None
    exe = shutil.which("ffmpeg")
    if exe:
        ffmpeg_dir = os.path.dirname(exe)
    else:
        try:
            import imageio_ffmpeg

            src = imageio_ffmpeg.get_ffmpeg_exe()
            bindir = os.path.join(DATA_DIR, "bin")
            os.makedirs(bindir, exist_ok=True)
            dst = os.path.join(bindir, "ffmpeg.exe" if IS_WINDOWS else "ffmpeg")
            if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
                shutil.copy2(src, dst)
                os.chmod(dst, 0o755)
            ffmpeg_dir = bindir
        except Exception:
            ffmpeg_dir = None

    deno = _find_deno()
    if deno:
        os.environ["PATH"] = os.path.dirname(deno) + os.pathsep + os.environ.get("PATH", "")
    return ffmpeg_dir, deno


FFMPEG_DIR, DENO_PATH = _setup_tools()
HAS_FFMPEG = FFMPEG_DIR is not None


# ----------------------------------------------------------------------
# Download logic
# ----------------------------------------------------------------------
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def clean(text):
    return _ANSI.sub("", str(text)).strip()


class _QuietLogger:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def build_format(kind, quality):
    if kind == "audio":
        return "bestaudio[ext=m4a]/bestaudio/best"
    if not HAS_FFMPEG:
        # no ffmpeg -> only single-file formats (video + audio already together)
        if quality == "best":
            return "b[ext=mp4]/b"
        h = int(quality)
        return f"b[height<={h}][ext=mp4]/b[ext=mp4]/b"
    if quality == "best":
        return "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b"
    h = int(quality)
    return (
        f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
        f"bv*[height<={h}]+ba/"
        f"b[height<={h}]/b"
    )


# Different ways of talking to YouTube. Tried in order until one works.
ATTEMPTS = [
    {},
    {"extractor_args": {"youtube": {"player_client": ["tv", "web_safari"]}}},
    {"extractor_args": {"youtube": {"player_client": ["android_vr"]}}},
]


def _base_opts(url):
    opts = {
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title).80s_%(id)s.%(ext)s"),
        "windowsfilenames": True,
        "noplaylist": "/playlist?" not in url,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _QuietLogger(),
    }
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    return opts


def _collect_paths(info):
    paths = []
    for entry in info.get("entries") or [info]:
        if not entry:
            continue
        for rd in entry.get("requested_downloads") or []:
            p = rd.get("filepath")
            if p and os.path.exists(p):
                paths.append(p)
    return paths


def _download_captions(info, lang, extra):
    """Separate pass so missing captions never break the video download."""
    import glob

    found = []
    for entry in info.get("entries") or [info]:
        if not entry:
            continue
        vurl = entry.get("webpage_url") or entry.get("original_url")
        vid = entry.get("id")
        if not vurl or not vid:
            continue
        opts = _base_opts(vurl)
        opts.update(
            {
                "skip_download": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": [f"{lang}.*"],
                "subtitlesformat": "srt/vtt/best",
                "ignore_no_formats_error": True,
            }
        )
        if HAS_FFMPEG:
            opts["postprocessors"] = [{"key": "FFmpegSubtitlesConvertor", "format": "srt"}]
        opts.update(extra)
        try:
            with YoutubeDL(opts) as ydl:
                ydl.extract_info(vurl, download=True)
        except Exception:
            continue
        found += glob.glob(os.path.join(DOWNLOAD_DIR, f"*{vid}*.srt"))
        found += glob.glob(os.path.join(DOWNLOAD_DIR, f"*{vid}*.vtt"))
    return list(dict.fromkeys(found))


def run_download(url, kind, quality, want_subs, lang, state):
    """Blocking. Runs in a worker thread. Never raises - returns a result dict."""
    result = {"title": url, "files": [], "subs": [], "error": None, "want_subs": want_subs}

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                state["pct"] = min(d.get("downloaded_bytes", 0) / total, 1.0)
            state["msg"] = (
                f"Downloading... {clean(d.get('_speed_str', ''))}  ETA {clean(d.get('_eta_str', ''))}"
            )
        elif d["status"] == "finished":
            state["pct"] = 1.0
            state["msg"] = "Processing file..."

    last_error = None
    for n, extra in enumerate(ATTEMPTS):
        if n:
            state["msg"] = f"Trying another method ({n + 1}/{len(ATTEMPTS)})..."
            state["pct"] = 0.0
        opts = _base_opts(url)
        opts["format"] = build_format(kind, quality)
        opts["progress_hooks"] = [hook]
        if HAS_FFMPEG:
            if kind == "audio":
                opts["postprocessors"] = [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
                ]
            else:
                opts["merge_output_format"] = "mp4"
        opts.update(extra)

        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            files = _collect_paths(info)
            if not files:
                raise RuntimeError("Download finished but no file was created.")
            result["title"] = info.get("title") or url
            result["files"] = files
            if want_subs:
                state["msg"] = "Fetching captions..."
                result["subs"] = _download_captions(info, lang, extra)
            return result
        except Exception as e:
            last_error = e

    result["error"] = clean(last_error)
    return result


# ----------------------------------------------------------------------
# In-app yt-dlp updater (downloads the newest yt-dlp + its matching yt-dlp-ejs from PyPI)
# ----------------------------------------------------------------------
def _http_json(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def _http_bytes(url):
    with urllib.request.urlopen(url, timeout=180) as r:
        return r.read()


def _wheel(pkg, version=None):
    api = f"https://pypi.org/pypi/{pkg}/{version}/json" if version else f"https://pypi.org/pypi/{pkg}/json"
    data = _http_json(api)
    for f in data["urls"]:
        if f["filename"].endswith("-none-any.whl"):
            return data["info"]["version"], f["url"]
    raise RuntimeError(f"No universal wheel found for {pkg}")


def update_ytdlp():
    latest, url = _wheel("yt-dlp")
    if _ver_tuple(latest) <= _ver_tuple(YTDLP_VERSION):
        return f"Already up to date (yt-dlp {YTDLP_VERSION})."

    ytdlp_whl = _http_bytes(url)
    with zipfile.ZipFile(io.BytesIO(ytdlp_whl)) as z:
        meta_name = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
        meta = z.read(meta_name).decode("utf-8", "replace")
    wheels = [ytdlp_whl]

    # yt-dlp rejects any yt-dlp-ejs version except the one it pins, so fetch that exact one.
    m = re.search(r"Requires-Dist:\s*yt-dlp-ejs\s*==\s*([0-9][0-9A-Za-z.]*)", meta)
    if m:
        _, ejs_url = _wheel("yt-dlp-ejs", m.group(1))
        wheels.append(_http_bytes(ejs_url))

    staging = os.path.join(UPDATE_ROOT, "staging")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    for data in wheels:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(staging)
    shutil.rmtree(UPDATE_CURRENT, ignore_errors=True)
    os.replace(staging, UPDATE_CURRENT)
    return f"Updated to yt-dlp {latest}. Close and reopen the app to use it."


# ----------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------
async def main(page: ft.Page):
    page.title = APP_NAME
    page.padding = 16
    if IS_WINDOWS:
        try:
            page.window.width = 560
            page.window.height = 880
        except Exception:
            pass

    urls = ft.TextField(
        label="YouTube link(s), one per line",
        hint_text="https://www.youtube.com/watch?v=...\nhttps://www.youtube.com/shorts/...",
        multiline=True,
        min_lines=3,
        max_lines=6,
    )
    audio_label = "Audio only (MP3)" if HAS_FFMPEG else "Audio only (M4A)"
    kind = ft.RadioGroup(
        value="video",
        content=ft.Row(
            [ft.Radio(value="video", label="Video (MP4)"), ft.Radio(value="audio", label=audio_label)],
            wrap=True,
        ),
    )
    quality = ft.RadioGroup(
        value="best",
        content=ft.Row(
            [
                ft.Radio(value="best", label="Best"),
                ft.Radio(value="1080", label="1080p"),
                ft.Radio(value="720", label="720p"),
                ft.Radio(value="480", label="480p"),
                ft.Radio(value="360", label="360p"),
            ],
            wrap=True,
        ),
    )
    subs_cb = ft.Checkbox(label="Also download captions (if available)", value=False)
    lang = ft.TextField(label="Caption language", value="en", width=170)

    download_btn = ft.FilledButton("Download", icon=ft.Icons.DOWNLOAD)
    update_btn = ft.OutlinedButton("Update yt-dlp", icon=ft.Icons.SYSTEM_UPDATE_ALT)
    bar = ft.ProgressBar(value=0)
    status = ft.Text("", selectable=True)
    results = ft.Column(spacing=8)

    def result_card(res):
        items = [ft.Text(res["title"], weight=ft.FontWeight.BOLD, selectable=True)]
        if res["error"]:
            items.append(ft.Text(res["error"], color=ft.Colors.RED, size=12, selectable=True))
            items.append(
                ft.Text(
                    "Tip: tap 'Update yt-dlp', restart the app, and try again.",
                    size=12,
                )
            )
        else:
            for f in res["files"]:
                items.append(ft.Text(f"Saved: {f}", size=12, selectable=True))
            if res["want_subs"]:
                if res["subs"]:
                    for s in res["subs"]:
                        items.append(ft.Text(f"Captions: {s}", size=12, selectable=True))
                else:
                    items.append(ft.Text("No captions available for this video.", size=12))
        return ft.Card(content=ft.Container(padding=12, content=ft.Column(items, spacing=4)))

    def set_busy(busy):
        download_btn.disabled = busy
        update_btn.disabled = busy

    async def on_download(e):
        lines = [u.strip() for u in (urls.value or "").splitlines() if u.strip()]
        if not lines:
            status.value = "Paste at least one link first."
            page.update()
            return

        set_busy(True)
        results.controls.clear()
        for i, url in enumerate(lines, start=1):
            state = {"pct": 0.0, "msg": "Connecting..."}
            task = asyncio.ensure_future(
                asyncio.to_thread(
                    run_download,
                    url,
                    kind.value,
                    quality.value,
                    bool(subs_cb.value),
                    (lang.value or "en").strip() or "en",
                    state,
                )
            )
            while not task.done():
                bar.value = state["pct"]
                status.value = f"[{i}/{len(lines)}] {state['msg']}"
                page.update()
                await asyncio.sleep(0.4)
            results.controls.append(result_card(task.result()))
            page.update()

        bar.value = 0
        status.value = f"Finished. Files are in: {DOWNLOAD_DIR}"
        set_busy(False)
        page.update()

    async def on_update(e):
        set_busy(True)
        status.value = "Checking for a newer yt-dlp..."
        page.update()
        try:
            msg = await asyncio.to_thread(update_ytdlp)
        except Exception as ex:
            msg = f"Update failed: {clean(ex)}"
        status.value = msg
        set_busy(False)
        page.update()

    async def on_open_folder(e):
        try:
            os.startfile(DOWNLOAD_DIR)  # Windows only
        except Exception:
            status.value = f"Files are in: {DOWNLOAD_DIR}"
            page.update()

    download_btn.on_click = on_download
    update_btn.on_click = on_update

    buttons = [download_btn, update_btn]
    if IS_WINDOWS:
        buttons.append(ft.OutlinedButton("Open folder", icon=ft.Icons.FOLDER_OPEN, on_click=on_open_folder))

    tools = (
        f"yt-dlp {YTDLP_VERSION}  |  ffmpeg: {'yes' if HAS_FFMPEG else 'no'}  |  "
        f"JS runtime: {'yes' if DENO_PATH else 'no'}"
    )
    notes = []
    if IS_ANDROID and not HAS_FFMPEG:
        notes.append(
            ft.Text(
                "Android build: videos download as single-file MP4 (quality may be limited) "
                "and audio as M4A.",
                size=12,
            )
        )

    page.add(
        ft.SafeArea(
            expand=True,
            content=ft.Column(
                scroll=ft.ScrollMode.AUTO,
                expand=True,
                spacing=12,
                controls=[
                    ft.Text(APP_NAME, size=26, weight=ft.FontWeight.BOLD),
                    ft.Text("Videos, Shorts and playlists, with or without captions.", size=13),
                    urls,
                    ft.Text("Download as"),
                    kind,
                    ft.Text("Max quality (video)"),
                    quality,
                    subs_cb,
                    lang,
                    ft.Row(buttons, wrap=True),
                    bar,
                    status,
                    results,
                    *notes,
                    ft.Text(tools, size=11),
                ],
            ),
        )
    )


ft.run(main)

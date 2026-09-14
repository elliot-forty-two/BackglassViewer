import json
import os
import time
import hashlib
import threading
import socket
import configparser
from pathlib import Path
from datetime import datetime
from PIL import Image

from image_compositor import cached_composite_path, make_composite_and_show

# -------------------- CONFIG --------------------
UDP_BIND = ("127.0.0.1", 49731)  # legacy; file handoff mode does not use UDP

BACKGLASS_PIPE_NAME = r"\\.\pipe\retrobat_backglass"
DMD_PIPE_NAME = r"\\.\pipe\retrobat_dmd"

RETROBAT_ROOT = Path(r"C:\RetroBat")
IMAGE_EXTS = [".png", ".jpg", ".jpeg", ".webp"]
VIDEO_EXTS = [".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"]

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "fanart_debug.log"
FALLBACK_IMAGE = BASE_DIR / "fallback.png"
VIEWER_CONFIG_FILE = BASE_DIR / "fanart_server.ini"
SELECTION_FILE = BASE_DIR / "current_selection.json"
QUIT_FILE = BASE_DIR / "quit.signal"

DMD_CACHE_VERSION = "dmd_center_v2"


def _load_viewer_setting(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = default
    config = configparser.ConfigParser()
    if VIEWER_CONFIG_FILE.exists():
        try:
            config.read(VIEWER_CONFIG_FILE, encoding="utf-8")
            section = config["BackglassViewer"] if "BackglassViewer" in config else {}
            value = str(section.get(name, default)).strip().lower()
        except Exception:
            value = default

    if value not in allowed:
        return default
    return value


def _load_int_setting(name: str, default: int, min_value: int, max_value: int) -> int:
    value = default
    config = configparser.ConfigParser()
    if VIEWER_CONFIG_FILE.exists():
        try:
            config.read(VIEWER_CONFIG_FILE, encoding="utf-8")
            section = config["BackglassViewer"] if "BackglassViewer" in config else {}
            raw = section.get(name, default)
            value = int(str(raw).strip())
        except Exception:
            value = default
    return max(min_value, min(max_value, value))


def _load_float_setting(name: str, default: float, min_value: float, max_value: float) -> float:
    value = default
    config = configparser.ConfigParser()
    if VIEWER_CONFIG_FILE.exists():
        try:
            config.read(VIEWER_CONFIG_FILE, encoding="utf-8")
            section = config["BackglassViewer"] if "BackglassViewer" in config else {}
            raw = section.get(name, default)
            value = float(str(raw).strip())
        except Exception:
            value = default
    return max(min_value, min(max_value, value))


BACKGLASS_SCREEN_MODE = _load_viewer_setting(
    "BackglassScreenMode",
    "auto",
    ("auto", "fanart", "backglass"),
)

DMD_SCREEN_MODE = _load_viewer_setting(
    "DmdScreenMode",
    "logo",
    ("logo", "marquee"),
)

DMD_TARGET_W = _load_int_setting("DmdWidth", 1920, 320, 8192)
DMD_TARGET_H = _load_int_setting("DmdHeight", 480, 120, 4096)
DMD_LOGO_SCALE = _load_float_setting("DmdLogoScale", 1.0, 0.1, 1.0)

# Performance/debug knobs
DEBUG_TIMINGS = True          # write detailed event/timing logs to fanart_debug.log
SLOW_MS = 75.0                # flag individual operations slower than this
POLL_MS = _load_int_setting("SelectionFilePollMs", 10, 1, 250)
# ------------------------------------------------


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


def _elapsed_ms(start_ms: float) -> float:
    return _now_ms() - start_ms


def _parse_client_ts(ts_value) -> datetime | None:
    """Parse client datetime.now().isoformat(timespec="milliseconds").

    Client and server are expected to be on the same Windows machine, so this
    compares local wall-clock timestamps. If the sender ever runs on another
    machine, clock skew will make this number misleading.
    """
    if not ts_value:
        return None
    try:
        return datetime.fromisoformat(str(ts_value))
    except Exception:
        return None


def _client_to_wall_ms(client_ts_value, wall_dt: datetime) -> float | None:
    client_dt = _parse_client_ts(client_ts_value)
    if client_dt is None:
        return None
    try:
        return (wall_dt - client_dt).total_seconds() * 1000.0
    except Exception:
        return None


def _fmt_ms(value) -> str:
    return "n/a" if value is None else f"{value:.1f}ms"


def _short_path(p) -> str:
    if not p:
        return ""
    try:
        return str(p)
    except Exception:
        return repr(p)


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def debug(msg: str):
    if DEBUG_TIMINGS:
        log(msg)


def send_mpv(cmd_obj: dict, pipe_name: str = BACKGLASS_PIPE_NAME, timeout_s: float = 1.5, event_id: str = "") -> bool:
    start_ms = _now_ms()
    deadline = time.time() + timeout_s
    last_err = None
    attempts = 0
    payload = json.dumps(cmd_obj)
    cmd = cmd_obj.get("command", ["?"])[0] if isinstance(cmd_obj, dict) else "?"

    while time.time() < deadline:
        attempts += 1
        try:
            with open(pipe_name, "r+", encoding="utf-8") as pipe:
                pipe.write(payload + "\n")
                pipe.flush()
                elapsed = _elapsed_ms(start_ms)
                if DEBUG_TIMINGS or elapsed >= SLOW_MS or attempts > 1:
                    debug(f"{event_id} mpv cmd={cmd!r} pipe={pipe_name} ok elapsed={elapsed:.1f}ms attempts={attempts}")
                return True
        except OSError as e:
            last_err = e
            time.sleep(0.02)

    elapsed = _elapsed_ms(start_ms)
    log(f"{event_id} ERROR: could not connect to mpv IPC pipe {pipe_name}: {last_err}; elapsed={elapsed:.1f}ms attempts={attempts} cmd={cmd!r}")
    return False


def show_in_mpv(path: str, pipe_name: str = BACKGLASS_PIPE_NAME, event_id: str = ""):
    global _last_loaded_by_pipe
    start_ms = _now_ms()
    path = (path or "").strip().strip('"')
    if not path or not os.path.isfile(path):
        debug(f"{event_id} show_in_mpv missing path={path!r}; sending stop to {pipe_name}")
        blank_mpv(pipe_name, event_id=event_id)
        return

    is_video = Path(path).suffix.lower() in VIDEO_EXTS
    last_path = _last_loaded_by_pipe.get(pipe_name)
    if last_path == path:
        debug(f"{event_id} show_in_mpv skip unchanged pipe={pipe_name} path={path}")
        return

    debug(f"{event_id} show_in_mpv start pipe={pipe_name} video={is_video} path={path}")
    keepaspect = not is_video
    if _last_keepaspect_by_pipe.get(pipe_name) != keepaspect:
        if send_mpv({"command": ["set_property", "keepaspect", keepaspect]}, pipe_name=pipe_name, event_id=event_id):
            _last_keepaspect_by_pipe[pipe_name] = keepaspect
    else:
        debug(f"{event_id} mpv keepaspect skip unchanged pipe={pipe_name} value={keepaspect}")

    ok = send_mpv({"command": ["loadfile", path, "replace"]}, pipe_name=pipe_name, event_id=event_id)
    if ok:
        _last_loaded_by_pipe[pipe_name] = path
        if not _fullscreen_set_by_pipe.get(pipe_name):
            try:
                if send_mpv({"command": ["set_property", "fullscreen", True]}, pipe_name=pipe_name, event_id=event_id):
                    _fullscreen_set_by_pipe[pipe_name] = True
            except Exception as e:
                log(f"{event_id} mpv fullscreen set failed: {type(e).__name__}: {e}")
        else:
            debug(f"{event_id} mpv fullscreen skip already set pipe={pipe_name}")
    debug(f"{event_id} show_in_mpv done elapsed={_elapsed_ms(start_ms):.1f}ms pipe={pipe_name}")


def blank_mpv(pipe_name: str, event_id: str = ""):
    global _last_loaded_by_pipe
    if _last_loaded_by_pipe.get(pipe_name) == "":
        debug(f"{event_id} blank_mpv skip already blank pipe={pipe_name}")
        return
    debug(f"{event_id} blank_mpv pipe={pipe_name}")
    if send_mpv({"command": ["stop"]}, pipe_name=pipe_name, event_id=event_id):
        _last_loaded_by_pipe[pipe_name] = ""


def show_media_or_blank(path: Path | None, pipe_name: str, event_id: str = "", blank_missing: bool = True) -> bool:
    if path and os.path.isfile(path):
        show_in_mpv(str(path), pipe_name=pipe_name, event_id=event_id)
        return True
    if blank_missing:
        blank_mpv(pipe_name, event_id=event_id)
    else:
        debug(f"{event_id} no media for pipe={pipe_name}; keeping current display until debounce")
    return False


def is_video_path(path: Path | None) -> bool:
    return bool(path and path.suffix.lower() in VIDEO_EXTS)


def file_sig(path: str) -> str:
    try:
        st = os.stat(path)
        m = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
        return f"{st.st_size}:{m}"
    except Exception:
        return "missing"


def centered_dmd_asset_path(src_path: Path, event_id: str = "") -> Path | None:
    if not src_path or not src_path.exists():
        return None

    cache_dir = BASE_DIR / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    key_src = f"{src_path}|{file_sig(str(src_path))}|{DMD_TARGET_W}x{DMD_TARGET_H}|scale={DMD_LOGO_SCALE:.3f}|{DMD_CACHE_VERSION}"
    key = hashlib.sha1(key_src.encode("utf-8", errors="ignore")).hexdigest()
    out_path = cache_dir / f"dmd_center_{key}.png"
    if out_path.exists() and out_path.stat().st_size > 0:
        debug(f"{event_id} dmd center cache_hit path={out_path}")
        return out_path

    try:
        with Image.open(src_path) as src:
            src_rgba = src.convert("RGBA")
            iw, ih = src_rgba.size
            if iw <= 0 or ih <= 0:
                return None

            fit_w = max(1, int(DMD_TARGET_W * DMD_LOGO_SCALE))
            fit_h = max(1, int(DMD_TARGET_H * DMD_LOGO_SCALE))
            scale = min(fit_w / float(iw), fit_h / float(ih))
            nw = max(1, int(iw * scale))
            nh = max(1, int(ih * scale))
            logo = src_rgba.resize((nw, nh), Image.LANCZOS)

            canvas = Image.new("RGB", (DMD_TARGET_W, DMD_TARGET_H), (0, 0, 0))
            x = (DMD_TARGET_W - nw) // 2
            y = (DMD_TARGET_H - nh) // 2
            canvas.paste(logo, (x, y), logo)
            canvas.save(out_path, format="PNG")
            debug(f"{event_id} dmd center rendered path={out_path}")
            return out_path
    except Exception as e:
        log(f"DMD centering failed: {type(e).__name__}: {e}")
        return None


def show_dmd_media_or_blank(path: Path | None, event_id: str = "", blank_missing: bool = True) -> bool:
    if DMD_SCREEN_MODE == "logo" and path and os.path.isfile(path) and not is_video_path(path):
        centered = centered_dmd_asset_path(path, event_id=event_id)
        if centered and centered.exists() and centered.stat().st_size > 0:
            show_in_mpv(str(centered), pipe_name=DMD_PIPE_NAME, event_id=event_id)
            return True
    return show_media_or_blank(path, DMD_PIPE_NAME, event_id=event_id, blank_missing=blank_missing)


def looks_like_path(s: str) -> bool:
    if not s:
        return False
    s = s.strip().strip('"')
    if ":\\" in s or ":/" in s or s.startswith("\\\\") or s.startswith(("/", "\\")):
        return True
    # file-ish
    lower = s.lower()
    return lower.endswith((".zip", ".7z", ".rar", ".iso", ".chd", ".cue", ".bin", ".nes", ".sfc", ".smc", ".gb", ".gba", ".nds"))

def infer_system_from_rom_path(p: Path) -> str | None:
    """
    Try to extract system name from ...\\RetroBat\\roms\\<system>\\...
    """
    parts = [x.lower() for x in p.parts]
    try:
        i = parts.index("roms")
        if i + 1 < len(p.parts):
            return p.parts[i + 1]
    except ValueError:
        pass
    return None

def normalize_message(msg: dict) -> dict:
    """
    Fix up messages where fields are swapped/missing.
    Returns dict with 'system' and 'rom' normalized.
    """
    system = (msg.get("system") or "").strip().strip('"')
    rom = (msg.get("rom") or "").strip().strip('"')

    # Case: system accidentally contains full rom path
    if looks_like_path(system) and not looks_like_path(rom):
        # If 'system' is a path and 'rom' is a short name, swap meaning
        rom_path = system
        system_name = infer_system_from_rom_path(normalize_rom_path(rom_path)) or ""
        return {**msg, "system": system_name, "rom": rom_path}

    # Case: rom contains full path but system missing/garbage
    if looks_like_path(rom):
        rom_path = rom
        system_name = system if system and not looks_like_path(system) else (infer_system_from_rom_path(normalize_rom_path(rom_path)) or "")
        return {**msg, "system": system_name, "rom": rom_path}

    # Case: both look like paths (rare) — pick the one that exists
    if looks_like_path(system) and looks_like_path(rom):
        sp = normalize_rom_path(system)
        rp = normalize_rom_path(rom)
        if rp.exists() and not sp.exists():
            rom_path = rom
        else:
            rom_path = system
        system_name = infer_system_from_rom_path(normalize_rom_path(rom_path)) or ""
        return {**msg, "system": system_name, "rom": str(rom_path)}

    # Otherwise assume normal format
    return msg


def normalize_rom_path(p: str) -> Path:
    p = (p or "").strip().strip('"').replace("/", "\\")
    return Path(p)


def find_media_file(media_dir: Path, rom_stem: str, suffix: str, exts: list[str] | tuple[str, ...] = IMAGE_EXTS) -> Path | None:
    for ext in exts:
        p = media_dir / f"{rom_stem}-{suffix}{ext}"
        if p.exists():
            return p
    return None


def find_media_file_stem(media_dir: Path, rom_stem: str, exts: list[str] | tuple[str, ...]) -> Path | None:
    for ext in exts:
        p = media_dir / f"{rom_stem}{ext}"
        if p.exists():
            return p
    return None


def find_media_file_any(media_dir: Path, rom_stem: str, suffixes: tuple[str, ...], exts: list[str] | tuple[str, ...] = IMAGE_EXTS) -> Path | None:
    for suffix in suffixes:
        media_file = find_media_file(media_dir, rom_stem, suffix, exts=exts)
        if media_file:
            return media_file
    return None


def find_media_across_dirs(media_dirs: list[Path], rom_stem: str, suffix: str, exts: list[str] | tuple[str, ...]) -> Path | None:
    for media_dir in media_dirs:
        media_file = find_media_file(media_dir, rom_stem, suffix, exts=exts)
        if media_file:
            return media_file
    return None


def find_media_any_across_dirs(media_dirs: list[Path], rom_stem: str, suffixes: tuple[str, ...], exts: list[str] | tuple[str, ...]) -> Path | None:
    for media_dir in media_dirs:
        media_file = find_media_file_any(media_dir, rom_stem, suffixes, exts=exts)
        if media_file:
            return media_file
    return None


def find_media_files(system: str, rom_path: Path, event_id: str = ""):
    start_ms = _now_ms()
    rom_name = rom_path.stem
    system = (system or "").strip().strip('"')
    images_dir = RETROBAT_ROOT / "roms" / system / "images"
    backglass_video_dirs = [
        RETROBAT_ROOT / "roms" / system / "video" / "Backglass",
        RETROBAT_ROOT / "roms" / system / "videos" / "Backglass",
    ]
    dmd_video_dirs = [
        RETROBAT_ROOT / "roms" / system / "video" / "DMD",
        RETROBAT_ROOT / "roms" / system / "videos" / "DMD",
    ]

    fanart = find_media_file(images_dir, rom_name, "fanart")
    marquee = find_media_file(images_dir, rom_name, "marquee", exts=IMAGE_EXTS)
    backglass = find_media_file(images_dir, rom_name, "backglass", exts=IMAGE_EXTS)
    logo = find_media_file_any(images_dir, rom_name, ("logo", "marquee-topper", "topper"), exts=IMAGE_EXTS)

    backglass_video = None
    for media_dir in backglass_video_dirs:
        backglass_video = find_media_file_stem(media_dir, rom_name, VIDEO_EXTS)
        if backglass_video:
            break

    dmd_video = None
    for media_dir in dmd_video_dirs:
        dmd_video = find_media_file_stem(media_dir, rom_name, VIDEO_EXTS)
        if dmd_video:
            break

    if backglass_video:
        backglass = backglass_video

    elapsed = _elapsed_ms(start_ms)
    debug(f"{event_id} media_lookup elapsed={elapsed:.1f}ms system={system!r} rom={rom_name!r} fanart={_short_path(fanart)} marquee={_short_path(marquee)} backglass={_short_path(backglass)} logo={_short_path(logo)} dmd_video={_short_path(dmd_video)}")
    return fanart, marquee, backglass, logo, dmd_video


def show_selected_media(fanart: Path | None, marquee: Path | None, backglass: Path | None, logo: Path | None, dmd_video: Path | None, event_id: str = "", should_continue=None) -> bool:
    """Pick media and display it quickly.

    Backglass handling is intentionally simple:
    - real backglass media wins in auto/backglass mode
    - otherwise fanart is shown through the compositor so it is smart-fit
    - if a composite is cached, it is loaded immediately
    - if not cached, it is rendered synchronously once, but the final mpv load is
      guarded so an older job cannot overwrite a newer selection
    """
    start_ms = _now_ms()

    def alive() -> bool:
        return True if should_continue is None else bool(should_continue())

    def show_smart_fanart(use_marquee: bool) -> bool:
        if not fanart or not os.path.isfile(fanart):
            return False
        overlay = str(marquee) if use_marquee and marquee and os.path.isfile(marquee) else None
        try:
            cached = cached_composite_path(str(fanart), overlay)
            if cached.exists() and cached.stat().st_size > 0:
                if alive():
                    debug(f"{event_id} fanart composite cache_hit path={cached}")
                    show_in_mpv(str(cached), pipe_name=BACKGLASS_PIPE_NAME, event_id=event_id)
                    return True
                debug(f"{event_id} fanart composite cache_hit ignored; superseded")
                return False

            if not alive():
                debug(f"{event_id} fanart composite render skipped; superseded")
                return False

            render_start_ms = _now_ms()
            debug(f"{event_id} fanart composite render start fanart={fanart} marquee={overlay}")

            shown = {"value": False}

            def guarded_show(out_path: str):
                if not alive():
                    debug(f"{event_id} fanart composite render produced stale output; not displaying path={out_path}")
                    return
                show_in_mpv(out_path, pipe_name=BACKGLASS_PIPE_NAME, event_id=event_id)
                shown["value"] = True

            make_composite_and_show(str(fanart), overlay, guarded_show)
            debug(f"{event_id} fanart composite render done displayed={shown['value']} elapsed={_elapsed_ms(render_start_ms):.1f}ms")
            return shown["value"]
        except Exception as e:
            log(f"{event_id} fanart composite failed: {type(e).__name__}: {e}")
            if alive():
                return show_media_or_blank(fanart, BACKGLASS_PIPE_NAME, event_id=event_id, blank_missing=True)
            return False

    dmd_media = dmd_video or (logo if DMD_SCREEN_MODE == "logo" else marquee) or (marquee if DMD_SCREEN_MODE == "logo" else logo)
    dmd_handled = False
    if alive():
        dmd_handled = show_dmd_media_or_blank(dmd_media, event_id=event_id, blank_missing=True)
    else:
        debug(f"{event_id} display abort before DMD; superseded")
        return False

    if not alive():
        debug(f"{event_id} display abort after DMD; superseded")
        return dmd_handled

    bg_handled = False

    if BACKGLASS_SCREEN_MODE == "fanart":
        bg_handled = show_smart_fanart(use_marquee=False)
    elif BACKGLASS_SCREEN_MODE == "backglass":
        if backglass and os.path.isfile(backglass):
            bg_handled = show_media_or_blank(backglass, BACKGLASS_PIPE_NAME, event_id=event_id, blank_missing=True)
        else:
            bg_handled = show_smart_fanart(use_marquee=False)
    else:
        if backglass and os.path.isfile(backglass):
            bg_handled = show_media_or_blank(backglass, BACKGLASS_PIPE_NAME, event_id=event_id, blank_missing=True)
        else:
            bg_handled = show_smart_fanart(use_marquee=True)

    if not bg_handled and alive():
        blank_mpv(BACKGLASS_PIPE_NAME, event_id=event_id)

    handled = dmd_handled or bg_handled
    debug(f"{event_id} show_selected_media done handled={handled} dmd={dmd_handled} backglass={bg_handled} elapsed={_elapsed_ms(start_ms):.1f}ms")
    return handled


# -------------------- Simple latest-only display handling --------------------
_lock = threading.Lock()
_job_id = 0
_last_loaded_by_pipe: dict[str, str] = {}
_last_keepaspect_by_pipe: dict[str, bool] = {}
_fullscreen_set_by_pipe: dict[str, bool] = {}

_pending_cond = threading.Condition()
_pending_msg = None
_pending_job_id = 0
_worker_started = False


def is_job_stale(job_id: int) -> bool:
    with _lock:
        return job_id != _job_id


def _ensure_worker_started():
    global _worker_started
    with _pending_cond:
        if _worker_started:
            return
        t = threading.Thread(target=_latest_only_worker, name="backglass_fast_latest_worker", daemon=True)
        t.start()
        _worker_started = True
        debug("[worker] fast latest-only worker started")


def handle_message(msg: dict):
    global _job_id, _pending_msg, _pending_job_id

    handle_start_ms = _now_ms()
    raw_msg = msg
    msg = normalize_message(msg)

    recv_perf_ms = msg.get("__recv_perf_ms")
    recv_to_handle_ms = None
    if isinstance(recv_perf_ms, (int, float)):
        recv_to_handle_ms = handle_start_ms - float(recv_perf_ms)

    with _lock:
        _job_id += 1
        job_id = _job_id
        event_id = f"[job {job_id}]"

    debug(
        f"{event_id} received raw={raw_msg} normalized={msg} "
        f"client_to_recv={_fmt_ms(msg.get('__client_to_recv_ms'))} "
        f"recv_to_handle={_fmt_ms(recv_to_handle_ms)}"
    )
    _ensure_worker_started()

    with _pending_cond:
        replaced_job = _pending_job_id if _pending_msg is not None else 0
        _pending_msg = msg
        _pending_job_id = job_id
        _pending_cond.notify()

    if replaced_job:
        debug(f"{event_id} queued latest; replaced pending job {replaced_job}; handle_elapsed={_elapsed_ms(handle_start_ms):.1f}ms")
    else:
        debug(f"{event_id} queued latest; handle_elapsed={_elapsed_ms(handle_start_ms):.1f}ms")


def _latest_only_worker():
    global _pending_msg, _pending_job_id

    while True:
        with _pending_cond:
            while _pending_msg is None:
                _pending_cond.wait()
            msg = _pending_msg
            job_id = _pending_job_id
            _pending_msg = None
            _pending_job_id = 0

        event_id = f"[job {job_id}]"
        if is_job_stale(job_id):
            debug(f"{event_id} worker drop before display; newer job already queued")
            continue

        client_to_display_ms = None
        recv_to_display_ms = None
        now_wall = datetime.now()
        client_to_display_ms = _client_to_wall_ms(msg.get("ts"), now_wall)
        recv_perf_ms = msg.get("__recv_perf_ms")
        if isinstance(recv_perf_ms, (int, float)):
            recv_to_display_ms = _now_ms() - float(recv_perf_ms)
        debug(
            f"{event_id} worker display start "
            f"client_to_display={_fmt_ms(client_to_display_ms)} "
            f"recv_to_display={_fmt_ms(recv_to_display_ms)}"
        )
        _process_message_for_display(msg, job_id)


def _process_message_for_display(msg: dict, job_id: int):
    event_id = f"[job {job_id}]"
    start_ms = _now_ms()

    try:
        system = (msg.get("system") or "").strip().strip('"')
        rom = (msg.get("rom") or "").strip().strip('"')
        debug(f"{event_id} normalized system={system!r} rom={rom!r}")

        if system and not rom:
            log(f"{event_id} system-only message received, show fallback system={system!r}")
            if not is_job_stale(job_id):
                show_in_mpv(str(FALLBACK_IMAGE), pipe_name=BACKGLASS_PIPE_NAME, event_id=event_id)
            if not is_job_stale(job_id):
                show_in_mpv(str(FALLBACK_IMAGE), pipe_name=DMD_PIPE_NAME, event_id=event_id)
            debug(f"{event_id} worker display done system-only elapsed={_elapsed_ms(start_ms):.1f}ms")
            return

        if not system or not rom:
            debug(f"{event_id} worker skipped missing system/rom system={system!r} rom={rom!r}")
            return

        rom_path = normalize_rom_path(rom)
        fanart, marquee, backglass, logo, dmd_video = find_media_files(system, rom_path, event_id=event_id)

        if is_job_stale(job_id):
            debug(f"{event_id} worker abort after media_lookup; superseded")
            return

        handled = show_selected_media(
            fanart, marquee, backglass, logo, dmd_video,
            event_id=event_id,
            should_continue=lambda: not is_job_stale(job_id),
        )
        debug(f"{event_id} worker display done handled={handled} elapsed={_elapsed_ms(start_ms):.1f}ms")
    except Exception as e:
        log(f"{event_id} worker display error: {type(e).__name__}: {e}; elapsed={_elapsed_ms(start_ms):.1f}ms")

def _read_selection_file() -> dict | None:
    """Read the latest selection JSON written by the ES event script.

    The writer should write to a temporary file and then replace/rename it over
    current_selection.json, so the viewer normally sees complete JSON. A small
    retry still protects against antivirus/indexing or a non-atomic writer.
    """
    for attempt in range(3):
        try:
            text = SELECTION_FILE.read_text(encoding="utf-8-sig")
            if not text.strip():
                return None
            msg = json.loads(text)
            if isinstance(msg, dict):
                return msg
            log(f"[file] ignored non-object JSON in {SELECTION_FILE}: {type(msg).__name__}")
            return None
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as e:
            if attempt < 2:
                time.sleep(0.005)
                continue
            log(f"[file] bad JSON in {SELECTION_FILE}: {e}")
            return None
        except OSError as e:
            if attempt < 2:
                time.sleep(0.005)
                continue
            log(f"[file] read failed {SELECTION_FILE}: {type(e).__name__}: {e}")
            return None
        except Exception as e:
            log(f"[file] read error {SELECTION_FILE}: {type(e).__name__}: {e}")
            return None
    return None


def _selection_file_signature() -> tuple[int, int] | None:
    try:
        st = SELECTION_FILE.stat()
        return (st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _quit_file_signature() -> tuple[int, int] | None:
    try:
        st = QUIT_FILE.stat()
        return (st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _shutdown_viewer(reason: str):
    log(f"[quit] {reason}; stopping server")
    try:
        send_mpv({"command": ["quit"]}, pipe_name=BACKGLASS_PIPE_NAME, event_id="[quit]")
        send_mpv({"command": ["quit"]}, pipe_name=DMD_PIPE_NAME, event_id="[quit]")
    except Exception as e:
        log(f"[quit] mpv quit failed: {type(e).__name__}: {e}")


def serve():
    log("fanart_server starting (file handoff listener)...")
    log(
        f"config BackglassScreenMode={BACKGLASS_SCREEN_MODE} "
        f"DmdScreenMode={DMD_SCREEN_MODE} SimpleFastMode=True Compositing=True "
        f"InputMode=file SelectionFile={SELECTION_FILE} QuitFile={QUIT_FILE} PollMs={POLL_MS} "
        f"ClientTsLatency=True DebugTimings={DEBUG_TIMINGS} SlowMs={SLOW_MS}"
    )
    log(f"Watching selection file: {SELECTION_FILE}")
    log(f"Watching quit file: {QUIT_FILE}")

    last_sig = None
    last_quit_sig = _quit_file_signature()
    last_payload_key = None
    missing_logged = False

    while True:
        quit_sig = _quit_file_signature()
        if quit_sig is not None and quit_sig != last_quit_sig:
            _shutdown_viewer(f"quit signal file changed sig={quit_sig} path={QUIT_FILE}")
            return

        sig = _selection_file_signature()
        if sig is None:
            if not missing_logged:
                debug(f"[file] waiting for selection file {SELECTION_FILE}")
                missing_logged = True
            time.sleep(POLL_MS / 1000.0)
            continue

        missing_logged = False
        if sig == last_sig:
            time.sleep(POLL_MS / 1000.0)
            continue

        read_start_ms = _now_ms()
        recv_perf_ms = read_start_ms
        recv_wall = datetime.now()
        msg = _read_selection_file()
        last_sig = sig
        if not msg:
            time.sleep(POLL_MS / 1000.0)
            continue

        payload_key = json.dumps(msg, sort_keys=True, ensure_ascii=False)
        if payload_key == last_payload_key:
            debug(f"[file] unchanged payload ignored sig={sig}")
            time.sleep(POLL_MS / 1000.0)
            continue
        last_payload_key = payload_key

        try:
            client_to_recv_ms = _client_to_wall_ms(msg.get("ts"), recv_wall)
            msg["__recv_perf_ms"] = recv_perf_ms
            msg["__recv_wall"] = recv_wall.isoformat(timespec="milliseconds")
            msg["__client_to_recv_ms"] = client_to_recv_ms
            debug(
                f"[file] changed sig={sig} path={SELECTION_FILE} "
                f"client_ts={msg.get('ts')!r} recv_wall={msg.get('__recv_wall')} "
                f"client_to_recv={_fmt_ms(client_to_recv_ms)} "
                f"read_elapsed={_elapsed_ms(read_start_ms):.1f}ms msg={msg}"
            )

            cmd = (msg.get("cmd") or "").lower().strip()
            if cmd == "quit":
                _shutdown_viewer("received quit command from selection file")
                return

            handle_message(msg)
        except Exception as e:
            log(f"[file] bad message: {type(e).__name__}: {e}")

        time.sleep(POLL_MS / 1000.0)


if __name__ == "__main__":
    try:
        serve()
    except Exception as e:
        log(f"FATAL ERROR: {type(e).__name__}: {e}")

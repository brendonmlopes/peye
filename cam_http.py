#!/usr/bin/env python3
from collections import deque
import json
from pathlib import Path
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = "0.0.0.0"
PORT = 8000

DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480
DEFAULT_FRAMERATE = 15
QUALITY = 80
DEFAULT_AWB = "auto"
DEFAULT_SATURATION = 1.0
DEFAULT_CONTRAST = 1.0

RESOLUTION_PRESETS = {
    "640x480": (640, 480),
    "1280x720": (1280, 720),
    "1920x1080": (1920, 1080),
}

FRAMERATE_PRESETS = [10, 15, 24, 30]
AWB_PRESETS = ["auto", "incandescent", "tungsten", "fluorescent", "indoor", "daylight", "cloudy"]
SATURATION_PRESETS = [0.7, 1.0, 1.3, 1.6]
CONTRAST_PRESETS = [0.8, 1.0, 1.2, 1.5]
CPU_TEMP_PATH = Path("/sys/class/thermal/thermal_zone0/temp")

latest_frame = None
frame_id = 0
frame_cond = threading.Condition()
settings_lock = threading.Lock()
camera_generation = 0
camera_settings = {
    "width": DEFAULT_WIDTH,
    "height": DEFAULT_HEIGHT,
    "framerate": DEFAULT_FRAMERATE,
    "awb": DEFAULT_AWB,
    "saturation": DEFAULT_SATURATION,
    "contrast": DEFAULT_CONTRAST,
}


def drain_stderr(pipe, tail):
    try:
        for line in iter(pipe.readline, b""):
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                tail.append(text)
    finally:
        pipe.close()


def get_camera_settings():
    with settings_lock:
        return dict(camera_settings), camera_generation


def update_camera_settings(changes):
    global camera_generation
    with settings_lock:
        changed = False
        for key, value in changes.items():
            if camera_settings.get(key) != value:
                camera_settings[key] = value
                changed = True
        if changed:
            camera_generation += 1
    return changed


def build_camera_command(settings):
    return [
        "rpicam-vid",
        "-t", "0",
        "-n",
        "--codec", "mjpeg",
        "--width", str(settings["width"]),
        "--height", str(settings["height"]),
        "--framerate", str(settings["framerate"]),
        "--quality", str(QUALITY),
        "--awb", settings["awb"],
        "--saturation", str(settings["saturation"]),
        "--contrast", str(settings["contrast"]),
        "-o", "-",
    ]


def format_number(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:.1f}" if isinstance(value, float) else str(value)


def render_option_buttons(name, options, current_value, formatter=str):
    buttons = []
    for option in options:
        selected = option == current_value
        label = formatter(option)
        class_name = "chip chip-active" if selected else "chip"
        buttons.append(
            f'<a class="{class_name}" data-control="{name}" data-value="{option}" href="/control?{name}={option}">{label}</a>'
        )
    return "".join(buttons)


def get_cpu_temperature():
    try:
        raw_value = CPU_TEMP_PATH.read_text(encoding="utf-8").strip()
        return int(raw_value) / 1000.0
    except (FileNotFoundError, PermissionError, ValueError, OSError):
        return None


def camera_worker():
    global latest_frame, frame_id

    while True:
        proc = None
        stderr_tail = deque(maxlen=10)
        stderr_thread = None
        restart_requested = False
        try:
            settings, generation = get_camera_settings()
            cmd = build_camera_command(settings)
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            stderr_thread = threading.Thread(
                target=drain_stderr,
                args=(proc.stderr, stderr_tail),
                daemon=True,
            )
            stderr_thread.start()

            buffer = bytearray()

            while True:
                _, latest_generation = get_camera_settings()
                if latest_generation != generation:
                    restart_requested = True
                    raise RuntimeError("camera settings changed")
                chunk = proc.stdout.read(4096)
                if not chunk:
                    err = " | ".join(stderr_tail)
                    detail = f": {err}" if err else ""
                    raise RuntimeError(f"camera process stopped{detail}")

                buffer.extend(chunk)

                while True:
                    start = buffer.find(b"\xff\xd8")
                    if start < 0:
                        if len(buffer) > 1024 * 1024:
                            buffer.clear()
                        break

                    end = buffer.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        if start > 0:
                            del buffer[:start]
                        break

                    jpg = bytes(buffer[start:end + 2])
                    del buffer[:end + 2]

                    with frame_cond:
                        latest_frame = jpg
                        frame_id += 1
                        frame_cond.notify_all()

        except Exception as e:
            print(f"[camera] {e}", flush=True)
            if restart_requested:
                restart_requested = False
            else:
                time.sleep(1)
        finally:
            if proc is not None:
                try:
                    if proc.poll() is None:
                        proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
            if stderr_thread is not None:
                stderr_thread.join(timeout=1)


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/control":
            changes = {}

            resolution = params.get("resolution", [None])[0]
            if resolution in RESOLUTION_PRESETS:
                width, height = RESOLUTION_PRESETS[resolution]
                changes["width"] = width
                changes["height"] = height

            framerate = params.get("framerate", [None])[0]
            if framerate is not None:
                try:
                    fps = int(framerate)
                except ValueError:
                    fps = None
                if fps in FRAMERATE_PRESETS:
                    changes["framerate"] = fps

            awb = params.get("awb", [None])[0]
            if awb in AWB_PRESETS:
                changes["awb"] = awb

            saturation = params.get("saturation", [None])[0]
            if saturation is not None:
                try:
                    sat = float(saturation)
                except ValueError:
                    sat = None
                if sat in SATURATION_PRESETS:
                    changes["saturation"] = sat

            contrast = params.get("contrast", [None])[0]
            if contrast is not None:
                try:
                    con = float(contrast)
                except ValueError:
                    con = None
                if con in CONTRAST_PRESETS:
                    changes["contrast"] = con

            update_camera_settings(changes)
            settings, _ = get_camera_settings()
            if params.get("ajax", ["0"])[0] == "1":
                self.send_json(
                    {
                        "ok": True,
                        "settings": settings,
                        "resolutionLabel": f'{settings["width"]}x{settings["height"]}',
                    }
                )
            else:
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
            return

        if path == "/status":
            cpu_temp = get_cpu_temperature()
            self.send_json(
                {
                    "ok": True,
                    "cpuTempC": cpu_temp,
                    "cpuTempLabel": f"{cpu_temp:.1f} C" if cpu_temp is not None else "Unavailable",
                }
            )
            return

        if path in ("/", "/index.html"):
            settings, _ = get_camera_settings()
            flash_message = params.get("message", [""])[0]
            cpu_temp = get_cpu_temperature()
            resolution_label = f'{settings["width"]}x{settings["height"]}'
            resolution_buttons = render_option_buttons(
                "resolution",
                list(RESOLUTION_PRESETS.keys()),
                resolution_label,
            )
            framerate_buttons = render_option_buttons(
                "framerate",
                FRAMERATE_PRESETS,
                settings["framerate"],
                formatter=lambda value: f"{value} FPS",
            )
            awb_buttons = render_option_buttons(
                "awb",
                AWB_PRESETS,
                settings["awb"],
                formatter=lambda value: value.title(),
            )
            saturation_buttons = render_option_buttons(
                "saturation",
                SATURATION_PRESETS,
                settings["saturation"],
                formatter=lambda value: f"{format_number(value)}x",
            )
            contrast_buttons = render_option_buttons(
                "contrast",
                CONTRAST_PRESETS,
                settings["contrast"],
                formatter=lambda value: f"{format_number(value)}x",
            )
            html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pi Camera Stream</title>
  <style>
    :root {{
      --bg: #f4efe7;
      --panel: rgba(255, 252, 247, 0.78);
      --panel-strong: rgba(255, 252, 247, 0.92);
      --text: #1f2421;
      --muted: #5f6b63;
      --accent: #0f766e;
      --accent-strong: #115e59;
      --accent-soft: rgba(15, 118, 110, 0.14);
      --border: rgba(31, 36, 33, 0.12);
      --shadow: 0 24px 80px rgba(37, 46, 39, 0.16);
      --radius: 24px;
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(15, 118, 110, 0.22), transparent 32%),
        radial-gradient(circle at top right, rgba(190, 24, 93, 0.14), transparent 26%),
        linear-gradient(160deg, #f8f4ec 0%, #efe8dc 48%, #e8ecdf 100%);
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(255,255,255,0.16) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.16) 1px, transparent 1px);
      background-size: 24px 24px;
      mask-image: linear-gradient(to bottom, rgba(0,0,0,0.38), transparent 72%);
    }}
    .shell {{
      width: min(1180px, calc(100% - 32px));
      margin: 0 auto;
      padding: 32px 0 48px;
    }}
    .hero {{
      display: grid;
      grid-template-columns: 1.15fr 0.85fr;
      gap: 24px;
      align-items: stretch;
    }}
    .panel {{
      position: relative;
      overflow: hidden;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--panel);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }}
    .hero-copy {{
      padding: 32px;
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255,255,255,0.55);
      color: var(--accent-strong);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 18px 0 14px;
      max-width: 12ch;
      font-size: clamp(2.5rem, 5vw, 4.8rem);
      line-height: 0.95;
      letter-spacing: -0.05em;
    }}
    .lede {{
      margin: 0;
      max-width: 42rem;
      color: var(--muted);
      font-size: 1.02rem;
      line-height: 1.7;
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 26px;
    }}
    .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 48px;
      padding: 0 18px;
      border-radius: 999px;
      border: 1px solid transparent;
      text-decoration: none;
      font-weight: 700;
      transition: transform 140ms ease, box-shadow 140ms ease, background 140ms ease;
    }}
    .button:hover {{
      transform: translateY(-1px);
    }}
    .button-primary {{
      background: var(--accent);
      color: #f7fffd;
      box-shadow: 0 14px 30px rgba(15, 118, 110, 0.22);
    }}
    .button-primary:hover {{
      background: var(--accent-strong);
    }}
    .button-danger {{
      background: #b91c1c;
      color: #fff8f8;
      box-shadow: 0 14px 30px rgba(185, 28, 28, 0.2);
    }}
    .button-danger:hover {{
      background: #991b1b;
    }}
    .button-secondary {{
      border-color: rgba(17, 94, 89, 0.18);
      background: rgba(255,255,255,0.64);
      color: var(--text);
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin-top: 24px;
    }}
    .stat {{
      padding: 16px;
      border-radius: 18px;
      background: rgba(255,255,255,0.55);
      border: 1px solid rgba(31, 36, 33, 0.08);
    }}
    .stat strong {{
      display: block;
      font-size: 1.05rem;
      letter-spacing: -0.02em;
    }}
    .stat span {{
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 0.92rem;
    }}
    .hero-side {{
      display: grid;
      grid-template-rows: auto auto;
      gap: 16px;
      padding: 20px;
    }}
    .mini-card {{
      padding: 18px;
      border-radius: 20px;
      background: var(--panel-strong);
      border: 1px solid rgba(31, 36, 33, 0.08);
    }}
    .mini-card h2 {{
      margin: 0 0 10px;
      font-size: 1rem;
      letter-spacing: -0.02em;
    }}
    .mini-card p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.6;
    }}
    .endpoint {{
      display: inline-flex;
      margin-top: 12px;
      padding: 8px 10px;
      border-radius: 12px;
      background: var(--accent-soft);
      color: var(--accent-strong);
      font-family: "SFMono-Regular", "Consolas", monospace;
      font-size: 0.9rem;
      text-decoration: none;
    }}
    .stream-panel {{
      margin-top: 24px;
      padding: 18px;
    }}
    .control-panel {{
      margin-top: 24px;
      padding: 20px;
    }}
    .control-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
      margin-top: 10px;
    }}
    .control-card {{
      padding: 18px;
      border-radius: 20px;
      background: rgba(255,255,255,0.58);
      border: 1px solid rgba(31, 36, 33, 0.08);
    }}
    .control-card h3 {{
      margin: 0;
      font-size: 1rem;
      letter-spacing: -0.02em;
    }}
    .control-card p {{
      margin: 8px 0 14px;
      color: var(--muted);
      font-size: 0.93rem;
      line-height: 1.55;
    }}
    .chip-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 40px;
      padding: 0 14px;
      border-radius: 999px;
      border: 1px solid rgba(17, 94, 89, 0.14);
      background: rgba(255,255,255,0.78);
      color: var(--text);
      text-decoration: none;
      font-size: 0.92rem;
      font-weight: 700;
      transition: transform 140ms ease, border-color 140ms ease, background 140ms ease;
    }}
    .chip:hover {{
      transform: translateY(-1px);
      border-color: rgba(17, 94, 89, 0.28);
    }}
    .chip-active {{
      background: linear-gradient(135deg, var(--accent), var(--accent-strong));
      border-color: transparent;
      color: #f7fffd;
      box-shadow: 0 12px 24px rgba(15, 118, 110, 0.18);
    }}
    .chip-pending {{
      opacity: 0.6;
      pointer-events: none;
    }}
    .current-stack {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 12px;
      margin-top: 18px;
    }}
    .current-pill {{
      padding: 14px;
      border-radius: 18px;
      background: rgba(255,255,255,0.68);
      border: 1px solid rgba(31, 36, 33, 0.08);
    }}
    .current-pill strong {{
      display: block;
      font-size: 0.96rem;
      letter-spacing: -0.02em;
    }}
    .current-pill span {{
      display: block;
      margin-top: 5px;
      color: var(--muted);
      font-size: 0.86rem;
    }}
    .stream-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 8px 8px 18px;
    }}
    .stream-header h2 {{
      margin: 0;
      font-size: 1.2rem;
      letter-spacing: -0.03em;
    }}
    .status {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 10px 14px;
      border-radius: 999px;
      background: rgba(255,255,255,0.72);
      color: var(--muted);
      font-size: 0.92rem;
      font-weight: 600;
    }}
    .status::before {{
      content: "";
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: #34d399;
      box-shadow: 0 0 0 6px rgba(52, 211, 153, 0.16);
    }}
    .stream-status {{
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .temp-badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 12px;
      border-radius: 999px;
      background: rgba(255,255,255,0.8);
      border: 1px solid rgba(31, 36, 33, 0.08);
      color: var(--text);
      font-size: 0.9rem;
      font-weight: 700;
      line-height: 1;
    }}
    .temp-badge svg {{
      width: 14px;
      height: 14px;
      color: #b45309;
      flex: 0 0 auto;
    }}
    .temp-badge-hot svg {{
      color: #b91c1c;
    }}
    .temp-badge-warm svg {{
      color: #d97706;
    }}
    .temp-badge-cool svg {{
      color: #0f766e;
    }}
    .frame {{
      position: relative;
      overflow: hidden;
      border-radius: 22px;
      background: #111827;
      border: 1px solid rgba(17, 24, 39, 0.08);
    }}
    .frame::after {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background: linear-gradient(to top, rgba(17,24,39,0.24), transparent 36%);
    }}
    .viewer-shell {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 280px;
      gap: 18px;
    }}
    .viewer-actions {{
      display: flex;
      flex-direction: column;
      gap: 14px;
    }}
    .viewer-card {{
      padding: 18px;
      border-radius: 20px;
      background: rgba(255,255,255,0.62);
      border: 1px solid rgba(31, 36, 33, 0.08);
    }}
    .viewer-card h3 {{
      margin: 0 0 10px;
      font-size: 1rem;
      letter-spacing: -0.02em;
    }}
    .viewer-card p {{
      margin: 0 0 14px;
      color: var(--muted);
      font-size: 0.93rem;
      line-height: 1.6;
    }}
    .viewer-card .button {{
      width: 100%;
    }}
    .button-busy {{
      opacity: 0.7;
      pointer-events: none;
    }}
    .viewer-meta {{
      margin-top: 12px;
      color: var(--muted);
      font-size: 0.88rem;
      line-height: 1.55;
    }}
    .record-indicator {{
      color: #991b1b;
      font-weight: 700;
    }}
    .flash {{
      margin-top: 18px;
      padding: 14px 16px;
      border-radius: 18px;
      border: 1px solid rgba(185, 28, 28, 0.12);
      background: rgba(255, 241, 242, 0.9);
      color: #991b1b;
      font-weight: 700;
    }}
    img {{
      display: block;
      width: 100%;
      max-width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: cover;
    }}
    .footer-note {{
      margin-top: 14px;
      padding: 0 8px 8px;
      color: var(--muted);
      font-size: 0.94rem;
      line-height: 1.6;
    }}
    @media (max-width: 900px) {{
      .hero {{
        grid-template-columns: 1fr;
      }}
      .stats {{
        grid-template-columns: 1fr;
      }}
      .control-grid,
      .current-stack {{
        grid-template-columns: 1fr;
      }}
      .viewer-shell {{
        grid-template-columns: 1fr;
      }}
      .stream-header {{
        align-items: flex-start;
        flex-direction: column;
      }}
    }}
    @media (max-width: 640px) {{
      .shell {{
        width: min(100% - 20px, 1180px);
        padding-top: 18px;
      }}
      .hero-copy,
      .hero-side,
      .stream-panel {{
        padding: 18px;
      }}
      h1 {{
        max-width: none;
      }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <article class="panel hero-copy">
        <span class="eyebrow">Raspberry Pi Camera</span>
        <h1>Live view without the friction.</h1>
        <p class="lede">
          A lightweight camera dashboard for checking the feed quickly, opening the raw MJPEG stream directly,
          or grabbing a still frame for scripts and monitoring tools.
        </p>
        <div class="actions">
          <a class="button button-primary" href="/stream.mjpg">Open live stream</a>
          <a class="button button-secondary" href="/snapshot.jpg">Capture snapshot</a>
        </div>
        <div class="stats">
          <div class="stat">
            <strong>{resolution_label}</strong>
            <span>Balanced for browser viewing and LAN access.</span>
          </div>
          <div class="stat">
            <strong>{settings["framerate"]} FPS</strong>
            <span>Configured stream cadence for smooth previews.</span>
          </div>
          <div class="stat">
            <strong>{settings["awb"].title()}</strong>
            <span>White balance mode currently driving the image tone.</span>
          </div>
        </div>
        {f'<div class="flash">{flash_message}</div>' if flash_message else ""}
      </article>

      <aside class="panel hero-side">
        <section class="mini-card">
          <h2>Endpoints</h2>
          <p>Use the browser UI for quick monitoring, or hit the direct routes from another device on your network.</p>
          <a class="endpoint" href="/stream.mjpg">GET /stream.mjpg</a>
          <a class="endpoint" href="/snapshot.jpg">GET /snapshot.jpg</a>
        </section>
        <section class="mini-card">
          <h2>Operational notes</h2>
          <p>
            This page is tuned for glanceable status, mobile readability, and direct actions first.
            If the camera restarts, the stream worker now respawns the capture process cleanly.
          </p>
        </section>
      </aside>
    </section>

    <section class="panel control-panel">
      <div class="stream-header">
        <h2>Camera controls</h2>
        <div class="status">Each change restarts capture with the new profile</div>
      </div>
      <div class="current-stack">
        <div class="current-pill">
          <strong>{resolution_label}</strong>
          <span>Resolution</span>
        </div>
        <div class="current-pill">
          <strong>{settings["framerate"]} FPS</strong>
          <span>Framerate</span>
        </div>
        <div class="current-pill">
          <strong>{settings["awb"].title()}</strong>
          <span>White balance</span>
        </div>
        <div class="current-pill">
          <strong>{format_number(settings["saturation"])}x</strong>
          <span>Saturation</span>
        </div>
        <div class="current-pill">
          <strong>{format_number(settings["contrast"])}x</strong>
          <span>Contrast</span>
        </div>
      </div>
      <div class="control-grid">
        <section class="control-card">
          <h3>Resolution</h3>
          <p>Pick a stream size based on detail versus bandwidth and browser smoothness.</p>
          <div class="chip-row">{resolution_buttons}</div>
        </section>
        <section class="control-card">
          <h3>Framerate</h3>
          <p>Lower rates cut CPU and network load. Higher rates feel more immediate.</p>
          <div class="chip-row">{framerate_buttons}</div>
        </section>
        <section class="control-card">
          <h3>White balance</h3>
          <p>Choose the lighting preset that best matches the room or daylight conditions.</p>
          <div class="chip-row">{awb_buttons}</div>
        </section>
        <section class="control-card">
          <h3>Saturation</h3>
          <p>Adjust overall color intensity from flatter neutral tones to more vivid output.</p>
          <div class="chip-row">{saturation_buttons}</div>
        </section>
        <section class="control-card">
          <h3>Contrast</h3>
          <p>Control edge separation and punch, especially useful in low-texture scenes.</p>
          <div class="chip-row">{contrast_buttons}</div>
        </section>
      </div>
    </section>

    <section class="panel stream-panel">
      <div class="stream-header">
        <h2>Live preview</h2>
        <div class="stream-status">
          <div class="status">Streaming over HTTP on port {PORT}</div>
          <div id="cpuTempBadge" class="temp-badge" aria-live="polite">
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M10 4a2 2 0 1 1 4 0v8.3a4.5 4.5 0 1 1-4 0V4Z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M12 14V7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
            </svg>
            <span id="cpuTempLabel">{f"{cpu_temp:.1f} C" if cpu_temp is not None else "Unavailable"}</span>
          </div>
        </div>
      </div>
      <div class="viewer-shell">
        <div class="frame">
          <img src="/stream.mjpg" alt="Live Raspberry Pi camera preview">
        </div>
        <aside class="viewer-actions">
          <section class="viewer-card">
            <h3>Recording</h3>
            <p>Record the live preview in your browser and download it on this device without saving anything on the Pi.</p>
            <button id="recordButton" class="button button-primary" type="button">Record video</button>
            <div id="recordMeta" class="viewer-meta">The recording downloads on stop. MP4 is preferred when this browser supports it.</div>
          </section>
          <section class="viewer-card">
            <h3>Screenshot</h3>
            <p>Download the current frame to this device without interrupting the stream or moving the page.</p>
            <button id="snapshotButton" class="button button-secondary" type="button">Save screenshot</button>
            <div id="snapshotMeta" class="viewer-meta">Screenshots download directly in the browser as JPEG files.</div>
          </section>
        </aside>
      </div>
      <p class="footer-note">
        For embedding in another app or dashboard, use the direct MJPEG endpoint. For single-frame polling, use the snapshot route.
      </p>
    </section>
  </main>
  <script>
    (() => {{
      const streamImage = document.querySelector('img[src="/stream.mjpg"]');
      const flash = document.querySelector('.flash');
      const currentCards = {{
        resolution: document.querySelector('.current-pill:nth-child(1) strong'),
        framerate: document.querySelector('.current-pill:nth-child(2) strong'),
        awb: document.querySelector('.current-pill:nth-child(3) strong'),
        saturation: document.querySelector('.current-pill:nth-child(4) strong'),
        contrast: document.querySelector('.current-pill:nth-child(5) strong'),
      }};
      const settings = {{
        width: {settings["width"]},
        height: {settings["height"]},
        framerate: {settings["framerate"]},
        awb: {json.dumps(settings["awb"])},
        saturation: {settings["saturation"]},
        contrast: {settings["contrast"]},
      }};
      const recordButton = document.getElementById('recordButton');
      const recordMeta = document.getElementById('recordMeta');
      const snapshotButton = document.getElementById('snapshotButton');
      const snapshotMeta = document.getElementById('snapshotMeta');
      const cpuTempBadge = document.getElementById('cpuTempBadge');
      const cpuTempLabel = document.getElementById('cpuTempLabel');
      const offscreenCanvas = document.createElement('canvas');
      const offscreenContext = offscreenCanvas.getContext('2d');
      let recorder = null;
      let recorderChunks = [];
      let recordMimeType = '';
      let recordExtension = 'webm';
      let drawTimer = null;

      function showFlash(message, isError = false) {{
        if (!flash) {{
          return;
        }}
        flash.textContent = message;
        flash.style.display = message ? 'block' : 'none';
        flash.style.color = isError ? '#991b1b' : '#115e59';
        flash.style.borderColor = isError ? 'rgba(185, 28, 28, 0.12)' : 'rgba(17, 94, 89, 0.12)';
        flash.style.background = isError ? 'rgba(255, 241, 242, 0.9)' : 'rgba(240, 253, 250, 0.9)';
      }}

      function formatNumber(value) {{
        return Number.isInteger(value) ? String(value) : value.toFixed(1);
      }}

      function updateCpuTemp(label, value) {{
        cpuTempLabel.textContent = label;
        cpuTempBadge.classList.remove('temp-badge-hot', 'temp-badge-warm', 'temp-badge-cool');
        if (typeof value !== 'number') {{
          return;
        }}
        if (value >= 70) {{
          cpuTempBadge.classList.add('temp-badge-hot');
        }} else if (value >= 55) {{
          cpuTempBadge.classList.add('temp-badge-warm');
        }} else {{
          cpuTempBadge.classList.add('temp-badge-cool');
        }}
      }}

      function updateCurrentSettings(next) {{
        settings.width = next.width;
        settings.height = next.height;
        settings.framerate = next.framerate;
        settings.awb = next.awb;
        settings.saturation = next.saturation;
        settings.contrast = next.contrast;
        currentCards.resolution.textContent = `${{next.width}}x${{next.height}}`;
        currentCards.framerate.textContent = `${{next.framerate}} FPS`;
        currentCards.awb.textContent = next.awb.charAt(0).toUpperCase() + next.awb.slice(1);
        currentCards.saturation.textContent = `${{formatNumber(next.saturation)}}x`;
        currentCards.contrast.textContent = `${{formatNumber(next.contrast)}}x`;
      }}

      async function applyControl(link) {{
        const group = link.dataset.control;
        const value = link.dataset.value;
        const siblings = document.querySelectorAll(`[data-control="${{group}}"]`);
        siblings.forEach((item) => item.classList.add('chip-pending'));
        try {{
          const response = await fetch(`/control?ajax=1&${{group}}=${{encodeURIComponent(value)}}`, {{
            headers: {{ 'X-Requested-With': 'fetch' }},
            cache: 'no-store',
          }});
          if (!response.ok) {{
            throw new Error('Control update failed');
          }}
          const payload = await response.json();
          updateCurrentSettings(payload.settings);
          siblings.forEach((item) => {{
            item.classList.remove('chip-active');
            if (item === link) {{
              item.classList.add('chip-active');
            }}
          }});
        }} catch (error) {{
          showFlash(error.message || 'Control update failed.', true);
        }} finally {{
          siblings.forEach((item) => item.classList.remove('chip-pending'));
        }}
      }}

      document.querySelectorAll('[data-control]').forEach((link) => {{
        link.addEventListener('click', (event) => {{
          event.preventDefault();
          applyControl(link);
        }});
      }});

      function filename(prefix, extension) {{
        const stamp = new Date().toISOString().replace(/[:.]/g, '-');
        return `${{prefix}}-${{stamp}}.${{extension}}`;
      }}

      async function downloadBlob(blob, name) {{
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = name;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }}

      snapshotButton.addEventListener('click', async () => {{
        snapshotButton.classList.add('button-busy');
        snapshotMeta.textContent = 'Capturing JPEG on this device...';
        try {{
          const response = await fetch('/snapshot.jpg', {{ cache: 'no-store' }});
          if (!response.ok) {{
            throw new Error('Snapshot is not available yet.');
          }}
          const blob = await response.blob();
          await downloadBlob(blob, filename('snapshot', 'jpg'));
          snapshotMeta.textContent = 'Screenshot downloaded to this device.';
        }} catch (error) {{
          snapshotMeta.textContent = error.message || 'Screenshot failed.';
        }} finally {{
          snapshotButton.classList.remove('button-busy');
        }}
      }});

      function pickRecordingFormat() {{
        const options = [
          ['video/mp4;codecs=h264', 'mp4'],
          ['video/mp4', 'mp4'],
          ['video/webm;codecs=vp9', 'webm'],
          ['video/webm;codecs=vp8', 'webm'],
          ['video/webm', 'webm'],
        ];
        for (const [mime, extension] of options) {{
          if (window.MediaRecorder && MediaRecorder.isTypeSupported(mime)) {{
            return {{ mime, extension }};
          }}
        }}
        return null;
      }}

      function syncCanvasSize() {{
        const width = streamImage.naturalWidth || settings.width;
        const height = streamImage.naturalHeight || settings.height;
        if (offscreenCanvas.width !== width || offscreenCanvas.height !== height) {{
          offscreenCanvas.width = width;
          offscreenCanvas.height = height;
        }}
      }}

      function startCanvasPump() {{
        const interval = Math.max(33, Math.round(1000 / Math.max(settings.framerate, 1)));
        drawTimer = window.setInterval(() => {{
          if (!streamImage.complete || offscreenCanvas.width === 0 || offscreenCanvas.height === 0) {{
            return;
          }}
          try {{
            offscreenContext.drawImage(streamImage, 0, 0, offscreenCanvas.width, offscreenCanvas.height);
          }} catch (_error) {{
          }}
        }}, interval);
      }}

      async function stopRecording() {{
        const activeRecorder = recorder;
        if (!activeRecorder) {{
          return;
        }}
        const blob = await new Promise((resolve, reject) => {{
          activeRecorder.addEventListener('stop', () => resolve(new Blob(recorderChunks, {{ type: recordMimeType }})), {{ once: true }});
          activeRecorder.addEventListener('error', () => reject(new Error('Recording failed.')), {{ once: true }});
          activeRecorder.stop();
        }});
        if (drawTimer !== null) {{
          window.clearInterval(drawTimer);
          drawTimer = null;
        }}
        activeRecorder.stream.getTracks().forEach((track) => track.stop());
        recorder = null;
        recorderChunks = [];
        recordButton.textContent = 'Record video';
        recordButton.classList.remove('button-danger', 'button-busy');
        recordButton.classList.add('button-primary');
        await downloadBlob(blob, filename('recording', recordExtension));
        recordMeta.innerHTML = `Download complete on this device. <span class="record-indicator">${{recordExtension.toUpperCase()}}</span> saved from the browser.`;
      }}

      recordButton.addEventListener('click', async () => {{
        if (recorder) {{
          recordButton.classList.add('button-busy');
          recordMeta.textContent = 'Finishing recording and downloading file...';
          try {{
            await stopRecording();
          }} catch (error) {{
            recordMeta.textContent = error.message || 'Recording failed.';
          }}
          return;
        }}

        const selected = pickRecordingFormat();
        if (!selected) {{
          recordMeta.textContent = 'This browser does not support in-browser recording for this stream.';
          return;
        }}

        syncCanvasSize();
        startCanvasPump();
        const stream = offscreenCanvas.captureStream(settings.framerate);
        recorderChunks = [];
        recordMimeType = selected.mime;
        recordExtension = selected.extension;
        recorder = new MediaRecorder(stream, {{ mimeType: recordMimeType }});
        recorder.addEventListener('dataavailable', (event) => {{
          if (event.data && event.data.size > 0) {{
            recorderChunks.push(event.data);
          }}
        }});
        recorder.start();
        recordButton.textContent = 'Stop recording';
        recordButton.classList.remove('button-primary');
        recordButton.classList.add('button-danger');
        recordMeta.innerHTML = `Recording on this device. Output format: <span class="record-indicator">${{recordExtension.toUpperCase()}}</span>.`;
      }});

      streamImage.addEventListener('load', syncCanvasSize);
      syncCanvasSize();
      updateCpuTemp({json.dumps(f"{cpu_temp:.1f} C" if cpu_temp is not None else "Unavailable")}, {json.dumps(cpu_temp)});
      window.setInterval(async () => {{
        try {{
          const response = await fetch('/status', {{ cache: 'no-store' }});
          if (!response.ok) {{
            return;
          }}
          const payload = await response.json();
          updateCpuTemp(payload.cpuTempLabel, payload.cpuTempC);
        }} catch (_error) {{
        }}
      }}, 5000);
      if (flash && !flash.textContent.trim()) {{
        flash.style.display = 'none';
      }}
    }})();
  </script>
</body>
</html>"""
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/snapshot.jpg":
            with frame_cond:
                ok = frame_cond.wait_for(lambda: latest_frame is not None, timeout=10)
                frame = latest_frame if ok else None

            if frame is None:
                self.send_error(503, "No frame available yet")
                return

            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(frame)
            return

        if path == "/stream.mjpg":
            self.send_response(200)
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()

            last_seen = -1
            try:
                while True:
                    with frame_cond:
                        ok = frame_cond.wait_for(
                            lambda: latest_frame is not None and frame_id != last_seen,
                            timeout=15,
                        )
                        if not ok:
                            continue
                        frame = latest_frame
                        last_seen = frame_id

                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        self.send_error(404)

    def log_message(self, fmt, *args):
        pass


def main():
    t = threading.Thread(target=camera_worker, daemon=True)
    t.start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"HTTP stream running on http://{HOST}:{PORT}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

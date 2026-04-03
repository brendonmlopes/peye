#!/usr/bin/env python3
from collections import deque
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = 8000

WIDTH = 640
HEIGHT = 480
FRAMERATE = 15
QUALITY = 80

latest_frame = None
frame_id = 0
frame_cond = threading.Condition()


def drain_stderr(pipe, tail):
    try:
        for line in iter(pipe.readline, b""):
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                tail.append(text)
    finally:
        pipe.close()


def camera_worker():
    global latest_frame, frame_id

    cmd = [
        "rpicam-vid",
        "-t", "0",
        "-n",
        "--codec", "mjpeg",
        "--width", str(WIDTH),
        "--height", str(HEIGHT),
        "--framerate", str(FRAMERATE),
        "--quality", str(QUALITY),
        "-o", "-",
    ]

    while True:
        proc = None
        stderr_tail = deque(maxlen=10)
        stderr_thread = None
        try:
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
    def do_GET(self):
        if self.path in ("/", "/index.html"):
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
            <strong>{WIDTH} x {HEIGHT}</strong>
            <span>Balanced for browser viewing and LAN access.</span>
          </div>
          <div class="stat">
            <strong>{FRAMERATE} FPS</strong>
            <span>Configured stream cadence for smooth previews.</span>
          </div>
          <div class="stat">
            <strong>MJPEG output</strong>
            <span>Simple to consume from browsers, tools, and embeds.</span>
          </div>
        </div>
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

    <section class="panel stream-panel">
      <div class="stream-header">
        <h2>Live preview</h2>
        <div class="status">Streaming over HTTP on port {PORT}</div>
      </div>
      <div class="frame">
        <img src="/stream.mjpg" alt="Live Raspberry Pi camera preview">
      </div>
      <p class="footer-note">
        For embedding in another app or dashboard, use the direct MJPEG endpoint. For single-frame polling, use the snapshot route.
      </p>
    </section>
  </main>
</body>
</html>"""
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/snapshot.jpg":
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

        if self.path == "/stream.mjpg":
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

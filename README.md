# webcam_rpi

Small Raspberry Pi camera server that exposes:

- a live MJPEG stream over HTTP
- a single-frame JPEG snapshot endpoint
- a browser UI for camera controls
- client-side screenshot download
- client-side recording download
- browser-side face recognition mode

The application is implemented in [cam_http.py](/home/mobius/Projects/webcam_rpi/cam_http.py).

## What the code does

The server starts a background camera worker that launches `rpicam-vid` in MJPEG mode and reads frames from stdout. Each decoded JPEG frame is stored in memory and made available to HTTP clients.

The HTTP server exposes:

- `/`
  The dashboard UI with the live viewer and control buttons.
- `/stream.mjpg`
  A multipart MJPEG stream suitable for browsers and simple embeds.
- `/snapshot.jpg`
  The latest available frame as a single JPEG.
- `/control?...`
  Runtime camera setting updates for resolution, framerate, white balance, saturation, and contrast.

The UI uses async browser requests for control changes, so the page updates in place and keeps the current scroll position. Screenshots and recordings are saved on the client side in the browser instead of being written to the Pi filesystem.

Face recognition mode also runs in the browser. It uses the browser `FaceDetector` API when available, draws boxes over detected faces, and stores registered face profiles in that browser's `localStorage`.

## Dependencies

Required:

- Python 3
- `rpicam-vid`
- a working Raspberry Pi camera setup

Optional but useful:

- a modern browser with `MediaRecorder` support for client-side recording

## Install dependencies

Update package lists:

```bash
sudo apt-get update
```

Install Python 3 if needed:

```bash
sudo apt-get install -y python3
```

Install Raspberry Pi camera apps if `rpicam-vid` is missing:

```bash
sudo apt-get install -y rpicam-apps
```

On some Raspberry Pi OS images, camera support may already be present. You can check with:

```bash
command -v rpicam-vid
```

If the camera is not detected, make sure the hardware is connected correctly and camera support is enabled for your Pi OS image.

## How to run

From the project directory:

```bash
python3 cam_http.py
```

By default the server listens on:

```text
http://0.0.0.0:8000/
```

From the same machine, open:

```text
http://127.0.0.1:8000/
```

From another machine on the same network, replace the host with the Pi IP address:

```text
http://<raspberry-pi-ip>:8000/
```

## Browser features

The dashboard includes:

- resolution presets
- framerate presets
- white balance presets
- saturation presets
- contrast presets
- live MJPEG preview
- screenshot download to the client device
- recording download to the client device
- face recognition toggle
- register-new-person button

Recording is handled in the browser from the live viewer. When supported by the browser, the app prefers MP4 output. If MP4 recording is not supported by the browser’s `MediaRecorder` implementation, it falls back to WebM.

Face recognition mode is browser-side and depends on `FaceDetector` support. Registration stores a lightweight local face profile in the current browser only. It is useful for simple local matching, but it is not a security-grade identity system.

## Notes about recording

The current recording flow is browser-side, not server-side:

- no recording files are written to the Pi
- no `ffmpeg` process is needed for normal recording
- the final file format depends on browser support

That means:

- Chrome/Chromium-based browsers may support MP4 or may fall back to WebM depending on platform and codec support
- some browsers only support WebM through `MediaRecorder`

If strict MP4 output is mandatory across all clients, that would need a different approach, typically server-side transcoding or a post-processing step.

## Troubleshooting

No image:

- confirm the camera is connected and recognized
- confirm `rpicam-vid` works directly from the shell
- check that no other process is already using the camera

Test the camera manually:

```bash
rpicam-vid -t 5000 -n --codec mjpeg -o /tmp/test.mjpg
```

Server starts but browser controls do nothing:

- open the browser dev tools and check for failed requests to `/control`
- confirm JavaScript is enabled

Recording button downloads WebM instead of MP4:

- that is expected when the browser does not support MP4 recording through `MediaRecorder`

Face recognition button says it is unsupported:

- use a browser that exposes the `FaceDetector` API
- confirm JavaScript is enabled
- face profiles are local to the browser where they were registered

## Files

- [cam_http.py](/home/mobius/Projects/webcam_rpi/cam_http.py)
  Main server, camera worker, and UI.

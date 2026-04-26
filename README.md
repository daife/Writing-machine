# Writing Machine

This project now includes a GUI workflow for preparing handwriting images, converting them into stroke paths, generating GRBL/G-code, and sending the job to a writing machine.

## GUI Usage

Install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Start the app:

```powershell
.\.venv\Scripts\python.exe main.py
```

Recommended workflow:

1. Use `1 底图设置` to import a homework-page photo, click the four paper corners, enter real size in mm, and generate `artifacts/reference_canvas.png`.
   - You can drag an image file from the desktop directly onto the reference canvas.
   - Use `保存当前底图配置` / `加载底图配置` to manage multiple reference canvases under `artifacts/references/`.
2. Use `2 字迹定位` to import answer images, crop/rotate/scale/move them over the reference background, then export `artifacts/handwriting_merged.png`.
   - You can drag one or more image files from the desktop directly onto the handwriting-positioning canvas.
   - Click an image on the canvas to select its layer.
   - Right-drag inside the selected frame to move the layer.
   - Left-drag inside the selected frame to erase with the current `橡皮擦半径 px`.
   - Left-drag the round handle above the frame to rotate.
   - Left-drag the blue square handle at the lower-right to scale; scaling is anchored at the upper-left.
   - Left-drag any red frame corner to crop.
   - Eraser output is saved as an already-extracted transparent layer, so later previews do not repeatedly thicken the same handwriting.
   - During mouse dragging, the operation frame updates immediately; after mouse release, the real image preview refreshes and left-side values stay synchronized. The left control panel scrolls vertically when controls overflow.
3. Use `3 轨迹生成` to skeletonize the transparent handwriting image and save `artifacts/strokes.json`. The skeleton parameters are persisted in `artifacts/project.json`. Enable `执行时使用对角原点/XY反向` if the physical paper is rotated 180 degrees; the preview rotates the background and handwriting together, while generated G-code moves to the opposite corner and writes with mirrored X/Y coordinates.
4. Optional: use `4 文本转字迹` to generate a Handright-style handwriting image, then add it back into module 2 for positioning.
   - Each generated image uses a timestamped filename like `handright_output_YYYYMMDD_HHMMSS_xxx.png`, so previous outputs are not overwritten.
   - Handright perturb parameters are exposed as `横向随机偏移`, `纵向随机偏移`, and `旋转随机扰动`; they are also persisted in `artifacts/project.json`.
5. Use `5 执行器` to generate `artifacts/job.gcode`; keep `dry-run 理论验证` enabled when no machine is connected. The executor shows the same `对角原点/XY反向` option used by module 3.

Generated artifacts are stored in `artifacts/`. The app automatically reloads `artifacts/project.json` on startup when it exists, including the last reference canvas, executor settings, and the trajectory rotation option.

## Serial Capture

This project captures traffic between the official writing-machine software and the real device by using a Windows virtual null-modem COM pair plus a Python serial proxy.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The virtual COM pair cannot be created by Python alone. Create it first with a Windows virtual serial tool such as com0com, VSPE, or a commercial Virtual Serial Port Driver.

Example wiring:

- Create a virtual pair: `COM1 <-> COM2`
- Configure the official software to connect to `COM1`
- Run this proxy on `COM2`
- Connect the real writing machine on `COM13`

## Usage

List detected ports:

```powershell
.\.venv\Scripts\python.exe serial_proxy.py --list-ports
```

Start capture:

```powershell
.\.venv\Scripts\python.exe serial_proxy.py --virtual-port COM2 --real-port COM13 --baud 115200 --log serial_log.txt
```

Captured packets are printed to the console and appended to `serial_log.txt` in hex plus ASCII preview.

## Troubleshooting

- If the official software opens `COM10`, this script must open the paired endpoint, for example `COM11`.
- Close serial monitor tools before running the proxy; a COM port usually cannot be opened by two programs at the same time.
- If `COM3` changes after reconnecting USB, run `--list-ports` again.

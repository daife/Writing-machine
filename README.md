# Writing Machine

这是一个写字机 GUI 工具，用于准备字迹图片、生成书写轨迹、导出 GRBL/G-code，并把任务发送到写字机。

## GUI 使用

安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

启动程序：

```powershell
.\.venv\Scripts\python.exe main.py
```

推荐流程：

1. 在 `1 底图设置` 中导入作业纸照片，点击四个纸张角点，填写真实宽高，然后生成 `artifacts/reference_canvas.png`。
   - 可以把图片文件直接拖到画布上。
   - 可以用 `Ctrl+V` 粘贴剪切板图片。
   - 可以用 `保存当前底图配置` / `加载底图配置` 管理 `artifacts/references/` 下的多个底图配置。
   - `清理缓存文件` 会删除未被当前项目引用的临时文件，但不会删除 `project.json`、底图配置、当前底图和当前图层图片。

2. 在 `2 字迹定位` 中导入答案图片图层，并在底图上移动、缩放、旋转、裁切或擦除，然后导出 `artifacts/handwriting_merged.png`。
   - 可以拖入一个或多个图片文件。
   - 可以用 `Ctrl+V` 或 `Win+V` 粘贴剪切板图片。
   - 点击画布中的图片可选择图层。
   - 右键拖动图框内部可平移图层。
   - 左键拖动图框内部可使用橡皮擦。
   - 左键拖动顶部圆形标识可旋转。
   - 左键拖动右下蓝色方块可缩放，缩放锚点在左上角。
   - 左键拖动红色边框角点可裁切。
   - 手动导入的答案图会二值化、白底透明化，并按“最小连通域”清理小噪点。
   - 模块 4 生成后加入模块 2 的字迹图会作为已处理透明图层使用，不会重复执行模块 2 的去噪处理。

3. 在 `3 轨迹生成` 中把透明字迹图转换为轨迹，并保存 `artifacts/strokes.json`。
   - 处理顺序是：Alpha 阈值 -> 膨胀 -> 腐蚀 -> 骨架化 -> 毛刺剪枝 -> 路径简化/过滤。
   - 这些参数会保存到 `artifacts/project.json`，下次启动自动读取。
   - 如果实际纸张旋转 180 度或机器使用对角原点，可启用 `执行时使用对角原点/XY反向`；预览和 G-code 坐标会同步调整。

4. 可选：在 `4 文本转字迹` 中把文本生成透明字迹图片，再加入模块 2。
   - 可以选择字体，也可以点击 `使用系统默认字体` 切回默认字体。
   - 用户只需要设置字号、字距、行距、行宽和模块 4 独立的最小连通域。
   - 图片宽高会根据文本内容自动计算。
   - 输出图片是二值透明 PNG，字迹为纯黑，背景透明。

5. 在 `5 执行器` 中生成 `artifacts/job.gcode` 并执行。
   - 没有连接机器时建议保持 `dry-run 理论验证` 开启。
   - 串口号、波特率、dry-run、X/Y 反向等执行器配置会自动保存并在下次启动时读取。

运行产生的文件默认保存在 `artifacts/`。程序启动时会自动读取 `artifacts/project.json`，恢复上次的底图、图层、轨迹参数、文本生成参数和执行器配置。

## 打包 Windows EXE

项目可以用 PyInstaller 打包成 Windows 桌面程序。先安装 PyInstaller：

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
```

使用仓库中的 `WritingMachine.spec` 打包：

```powershell
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean WritingMachine.spec
```

生成结果是：

```text
dist\WritingMachine.exe
```

这个 spec 是单文件模式，最终只需要分发 `dist\WritingMachine.exe`，不需要额外发送 `_internal` 文件夹。第一次启动可能稍慢，因为 PyInstaller 会把内置的 Python、Tcl/Tk、tkinterdnd2、OpenCV、NumPy、SciPy、scikit-image 等运行文件释放到系统临时目录。

### 为什么 spec 会被 PyInstaller 自动修改

如果使用下面这类命令：

```powershell
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean --onedir --windowed --name WritingMachine main.py
```

PyInstaller 会根据命令行参数重新生成 `WritingMachine.spec`。因为 `--name WritingMachine` 对应的 spec 文件名就是 `WritingMachine.spec`，所以它会覆盖仓库里原本手写的单文件配置。覆盖后通常会重新变成 onedir 结构，也就会出现 `dist\WritingMachine\_internal\`。

因此，想保留单文件打包配置时，请只使用：

```powershell
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean WritingMachine.spec
```

不要再额外加 `--onedir`、`--onefile`、`--name WritingMachine main.py` 这类会重新生成 spec 的参数。

如果已经不小心覆盖了 spec，可以从 Git 恢复：

```powershell
git restore WritingMachine.spec
```

然后重新运行：

```powershell
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean WritingMachine.spec
```

## 串口抓包

本项目还提供 `serial_proxy.py`，可用于抓取官方写字机软件和真实设备之间的串口通信。通常需要 Windows 虚拟串口对工具，例如 com0com、VSPE 或其它虚拟串口驱动。

示例连接方式：

- 创建虚拟串口对：`COM1 <-> COM2`
- 官方软件连接 `COM1`
- 本代理程序监听 `COM2`
- 真实写字机连接 `COM13`

列出串口：

```powershell
.\.venv\Scripts\python.exe serial_proxy.py --list-ports
```

开始抓包：

```powershell
.\.venv\Scripts\python.exe serial_proxy.py --virtual-port COM2 --real-port COM13 --baud 115200 --log serial_log.txt
```

抓到的数据会输出到控制台，并追加写入 `serial_log.txt`，包含十六进制和 ASCII 预览。

## 常见问题

- 如果官方软件打开的是 `COM10`，代理程序需要打开它配对的另一个端口，例如 `COM11`。
- 运行代理前请关闭其它串口监视工具；同一个 COM 口通常不能被多个程序同时打开。
- 如果重新插拔 USB 后 `COM3` 变化了，请重新运行 `--list-ports` 查看当前端口。

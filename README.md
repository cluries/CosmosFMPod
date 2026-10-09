# Pod 播客任务 TUI

使用 Python 3.10 或更新版本，并安装 `curl`、`ffmpeg`（包含 `ffprobe`）。

```sh
uv pip install --python .venv/bin/python -r requirements.txt
./.venv/bin/python pod.py
```

如果使用自己的 Python 环境，先执行 `python -m pip install -r requirements.txt`，再执行 `python pod.py`。

底部输入一条命令，按 Enter 提交一个任务：

```text
https://example.com/episode
https://example.com/episode 10
https://example.com/episode nomove
https://example.com/episode 10 nomove
https://example.com/episode nomove 10
```

- 数字表示裁掉开头的秒数，支持小数。
- 默认同时处理 3 个任务；每个任务内部依次获取页面、下载、转换或裁剪。
- 默认复制当前任务的 MP3 和标题 TXT 到设备，保留本地文件；显式指定 `nomove` 时只生成本地文件，不传输到设备。
- 设备任务按音频处理完成、进入队列的顺序执行，一次一个；每次复制后更新设备的 `LIST.md`。
- 设备未挂载时等待设备，其他下载和转换继续执行。
- 顶部固定的「设备传输」区显示正在复制／等待设备的任务，以及实际顺序的排队列表；复制时显示进度、速度和预计剩余时间。排队列表超过三行可独立滚动。
- 任务表随窗口宽度分配标题和详情列，窄窗口支持横向滚动，编号和状态固定在左侧；选中任务的标题、URL、状态、裁剪秒数、耗时、传输方式、输出文件、目标设备及复制进度显示在表格下方，可独立滚动查看；切换任务时回到详情顶部。
- 界面标签、状态、输入提示和应用日志使用英文；播客标题保留原文。
- 输入框与下方的一行总体状态固定在底部；状态显示处理、等待、完成、失败、复制排队和设备连接情况。
- ↑ / ↓ 回看输入历史；任务表和日志可滚动，Tab 切换焦点。
- `/exit` 停止接收任务，完成所有已提交任务后退出。设备未连接时会继续等待。
- Ctrl+C 停止下载和转换，等待当前复制操作清理临时文件后退出。已生成的本地结果保留。
- 单个任务失败不影响其他任务，错误显示在任务表和日志。

配置位于 `pod.py` 顶部：`DEST` 是目标设备，`MAX_CONCURRENT` 是处理并发数。
`dist/`、`tmp/` 和 `.move_history.jsonl` 均位于脚本目录，不受启动位置影响。
文件名使用 `月日_时分秒_毫秒`；同一毫秒提交多个任务时递增毫秒，避免覆盖。
程序不扫描或搬走 `dist/` 内其他任务或以前留下的文件。

验证：

```sh
./.venv/bin/python -m unittest -v test_pod.py
```

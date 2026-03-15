# WSL Agent Monitor

[![Tests](https://github.com/spaxfiz/wsl-agent-monitor/actions/workflows/test.yml/badge.svg)](https://github.com/spaxfiz/wsl-agent-monitor/actions/workflows/test.yml)
[![Build](https://github.com/spaxfiz/wsl-agent-monitor/actions/workflows/build.yml/badge.svg)](https://github.com/spaxfiz/wsl-agent-monitor/actions/workflows/build.yml)
[![Release](https://img.shields.io/github/v/release/spaxfiz/wsl-agent-monitor)](https://github.com/spaxfiz/wsl-agent-monitor/releases)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![English README](https://img.shields.io/badge/README-English-blue.svg)](README.md)

Did my agents dead?

大概率没有，只是它们在 WSL 里默默干活，而你在 Windows 上一脸茫然。

`WSL Agent Monitor` 是一个 Windows 桌面浮窗工具，用来实时观察已经在
WSL 中运行的 `Claude` 和 `Codex` 会话。它会读取这些 agent 已经写出的
会话文件，把最近的活动流式显示出来，但不会额外写日志，也不会替你启动新进程。

## 它能做什么

- 监控已经运行中的 `Claude` 和 `Codex`
- 每个 agent 支持同时跟踪多个活跃会话
- 在同一个输出区域中合并显示多会话事件
- 在 `Sessions` 中展示 `session id + name`
- 在输出行里只显示短 `session id`，减少视觉噪音
- 支持单个 agent 的 `Watch / Pause`
- 支持顶部总开关 `Watch all / Pause all`
- 最小化到托盘并从托盘恢复
- 吸附右侧屏幕边缘，鼠标靠近时展开

## 工作原理

程序运行在 Windows 上，但会通过 `wsl.exe` 进入 WSL 执行探测脚本。

- `Claude` 通过 `~/.claude/sessions/*.json` 和对应项目日志定位
- `Codex` 通过 `~/.codex/sessions/**/rollout-*.jsonl` 定位
- Codex 会话在最近 10 分钟内有新事件时，会继续保留在当前活动列表中
- 新会话可以动态发现，变 idle 的会话也会动态移出当前活动区域

UI 只维护最近一段滚动内容，不会生成本地持久化日志文件。

## 项目结构

```text
app.py                 薄入口
wsl_agent_monitor/     UI、探测脚本、常量、数据模型
tests/                 轻量级单元测试
launch_monitor.bat     本地运行脚本
build_exe.bat          单文件 Windows 打包脚本
```

## 环境要求

- Windows
- 可用的 Python
- 已正确安装并可使用的 WSL
- WSL 中已经运行过 `claude` 和/或 `codex`
- 如果需要托盘支持和打包功能，需要安装 `requirements-build.txt` 中的依赖

## 本地初始化

在项目目录中创建虚拟环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
```

## 运行

推荐方式：

```powershell
launch_monitor.bat
```

或者直接运行：

```powershell
python app.py
```

也可以使用本地虚拟环境：

```powershell
.\.venv\Scripts\python.exe app.py
```

## 使用说明

1. 可以在顶部填写 WSL 发行版名称；留空则使用默认发行版。
2. 用顶部按钮统一启动或暂停全部 watcher。
3. 也可以分别控制 `Claude` 和 `Codex` 两个卡片。
4. `Current activity` 会显示最近一次归纳出来的活动摘要。
5. `Sessions` 默认折叠，展开后可以看到完整的 `session id + 名称`。
6. `Watched files` 默认折叠，只有需要定位底层文件时再展开。
7. 点击 `_` 可以隐藏到系统托盘，再从托盘重新打开。
8. 开启 `Edge dock` 后，窗口会吸附到右侧边缘，并在鼠标靠近时展开。

## 测试

运行测试：

```powershell
python -m unittest discover -s tests -v
```

当前测试覆盖：

- 入口模块导入
- 关键 UI 折叠/展开逻辑
- probe 的错误处理分支

GitHub Actions 也会在 `push` 和 `pull request` 时自动执行测试。

## 构建

打包单文件 Windows 可执行程序：

```powershell
build_exe.bat
```

输出文件：

```text
dist\WSLAgentMonitor.exe
```

GitHub Actions 的构建流程会上传 `.exe` 产物，打 tag 后也可以自动用于发布 release。

## 更新记录

版本变更记录见 [CHANGELOG.md](CHANGELOG.md)。

## 局限

- 项目依赖 Claude 和 Codex 当前使用的本地会话文件格式
- 如果上游工具更改存储格式，探测逻辑需要同步更新
- Codex 当前活动判断依赖 rollout 文件的更新时间，而不是额外的状态存储
- 托盘功能依赖 `pystray` 和 `Pillow`

## 许可证

MIT，见 [LICENSE](LICENSE)。

# 测试

项目测试使用合成备份和生成的数据，不需要 iPhone、真实备份或真实密码。测试运行中会使用系统临时目录；不要把真实备份放进测试夹具。

## 运行

需要 Python 3.12 或更高版本，以及 `pyproject.toml` 中 `dev` 扩展的依赖。使用 uv 安装锁定的依赖：

```bash
uv sync --locked --extra dev
```

在 Linux 图形环境下运行全部测试和静态检查：

```bash
uv run --locked ruff check src tests
uv run --locked pytest tests -q
```

无桌面的 Linux 环境可使用 Qt 离屏插件：

```bash
QT_QPA_PLATFORM=offscreen uv run --locked pytest tests -q
```

若依赖已经安装在项目的 `.venv` 中，也可直接使用 `.venv/bin/ruff` 和 `.venv/bin/pytest`。运行单个模块或用例时，把 `tests` 替换为对应路径或 `路径::测试名`。

## 覆盖范围

测试覆盖明文与加密的合成备份、文件浏览和导出、暂挂与保存、跨窗口操作、plist 编辑、SQLite 和十六进制查看、图片加载及缓存、语言切换和主要界面行为。故障注入与取消测试检查部分错误和资源清理路径。

离屏测试不能替代真实桌面环境中的窗口、物理触摸板、原生拖放或跨平台测试，也不能证明所有设备的备份都能恢复。发布前的人工检查见 [发布检查清单](release-checklist.md)。

历史运行日志、截图、生成的 XML 和覆盖率报告不提交；CI 运行结果以对应的工作流记录为准。

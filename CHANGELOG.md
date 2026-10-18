# 更新日志

版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.2.1] - 2026-10-18

### 修复

- 目录页 `index.html` 与 `index.csv` 的标题列会原样显示富文本标记：
  `<text indent="1">…</text>`、行内图片引用 `☺ <40位hash><0/></>`。
  现在导出前会剥掉标记、折叠空白；无标题笔记不再把整段正文（最长 8282 字符）
  塞进目录页，改为截断到 120 字符并加省略号。

### 其它

- `tests\test_export.py` 增加不变量：目录页标题不含 `<` / `☺`，长度不超过 121 字符。

## [1.2.0] - 2026-10-18

### 新增

- `--diagnose` 诊断模式：只输出备份包的结构统计（头部、tar 成员、记录条数、字段号分布、
  附件类型分布、可疑点），便于换设备 / 换系统版本后定位问题。报告不含笔记正文。
- `--version`：打印版本号。
- GitHub Actions CI：Ubuntu 与 Windows 双平台、Python 3.9 / 3.11 / 3.13 跑四套测试，
  并做三种格式的冒烟导出与结构校验。
- `pyproject.toml`：支持 `pip install .` 安装成命令行工具（`miui-notes-export`）。
- `LICENSE`（MIT）、`SECURITY.md`、`CHANGELOG.md`。
- 图形界面在导出失败时会弹出错误框，并提示用 `--diagnose` 收集诊断信息。

### 修复

- GUI 的后台线程遇到 `SystemExit`（加密备份、没选中笔记备份等「人话错误」）时会静默结束，
  界面一直停在「导出中…」；现在会正常显示错误。
- Markdown 里下划线文本会输出成 `<u>…</u>` 内联 HTML（Markdown 没有下划线语法），
  在不支持内联 HTML 的笔记软件里会显示成代码。现在 Markdown 只保留文字，
  HTML / Word 仍然保留下划线。

### 其它

- `--diagnose` 与 `--rebuild-index`、正常导出互斥，优先执行诊断。
- 测试补齐兼容性用例（字段号变体、多应用备份包、加密备份、缺 python-docx 降级）。

## [1.1.0] - 2026-10-18

### 新增

- 合并去重：按「内容指纹」判断重复，内容完全相同的笔记不再重复导出；
  `--no-merge` 可关闭。
- `--rebuild-index`：给已有导出目录重建清单与目录页。
- 图形界面 `main.py` 与自启动 `启动.bat`。
- HTML 输出格式（`-f html`），配 `index.html` 目录页（可搜索、离线可开）。

### 修复

- 无标题笔记的目录名会带出正文标记（`☺ <hash>_text indent=_1__…`）。
- 多页手写笔记的附件互相串台（三个同名的 `multi_page_ent.ent`）。

## [1.0.0] - 2026-10-16

### 新增

- 解析 MIUI / 澎湃OS 备份容器（文本头 + tar），兼容不同的 tar 起点。
- 解析 `_tmp_bak` 里的 protobuf 记录，还原笔记、目录、时间戳。
- 解析自研正文标记：缩进、待办、粗体 / 斜体 / 下划线、行内图片。
- 附件按 SHA1 命名映射，按魔数识别类型（PNG / JPEG / JSON / 手写笔迹 / 多页手写）。
- 导出 Markdown 与 Word（.docx），图片相对引用并落到 `note.assets/`。
- `index.csv` 索引、`--keep-raw` 原样释放附件。

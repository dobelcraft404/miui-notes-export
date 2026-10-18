# MIUI 笔记备份导出工具

[![CI](https://github.com/dobelcraft404/miui-notes-export/actions/workflows/ci.yml/badge.svg)](https://github.com/dobelcraft404/miui-notes-export/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

把**小米澎湃OS / MIUI 的「本地备份」里的笔记包（`.bak`）离线解析**，导出成 Markdown、Word（`.docx`）和 HTML，图片等附件一并保留，输出目录结构与备份包保持一致。

全程本地离线解析，不需要登录小米账号、不联网、不上传任何数据。

> **AI 辅助说明**：本项目的代码、测试与文档主要由 AI（DeepSeek 编程助手）在人工提出需求与验收下生成；备份格式的逆向结论全部基于对真实备份包的逐字节实测。详见文末「关于 AI 辅助」。

## 背景：备份包长什么样

小米「备份」功能生成的压缩包（文件名形如 `<日期>_<时间>.zip`）内含：

```
小米备份.zip
├── descript.xml                 备份描述（设备型号、系统版本、包名等）
└── 笔记(com.miui.notes).bak     该应用的备份数据
```

`.bak` 本体是一个纯文本头 + **标准 POSIX tar 归档**：

```
MIUI BACKUP\n
2\n                            <- 容器版本
com.miui.notes 笔记\n           <- 包名 + 应用名
-1\n0\n
ANDROID BACKUP\n5\n0\nnone\n   <- 子系统版本、加密标志、算法
<tar 归档从这里开始>
```

tar 内部与本工具相关的条目：

| 条目 | 内容 |
| --- | --- |
| `apps/com.miui.notes/miui_bak/_tmp_bak` | 笔记正文（protobuf 编码） |
| `apps/com.miui.notes/miui_att/*` | 图片、手写笔迹、识别结果等附件 |
| `apps/com.miui.notes/_meta` | 桌面小组件偏好（与正文无关） |
`_tmp_bak` 内部是 protobuf：顶层 `#6` 里逐条存放「目录」和「笔记」记录，每条笔记含 ID、创建/修改时间、标题、所属目录、正文项（`#9`）以及手写笔记的额外字段（`#16`~`#21`）。

正文不是标准 HTML，而是小米自定义的行内标记：

```
<text indent="1">正文<b>加粗</b><br>第二行</text>
<input type="checkbox" indent="1" level="3" checked="true" />
☺ <40位十六进制hash><0/></>          <- 行内图片，hash 即附件文件名
```

## 安装

要求 **Python 3.9+**。完整功能在 Python 3.11 上开发并验证；Python 3.13.1 上实测 Markdown 导出正常（该解释器下没装 `python-docx`，所以 Word 导出未在 3.13 上验证）。

```powershell
python -m pip install -r requirements.txt
```

- 只导出 Markdown 时**无需任何第三方库**（仅用标准库）。
- 导出 Word 需要 `python-docx`（`requirements.txt` 里已列出）。
- HTML 只需要标准库。
- 装了 `python-docx` 与否都不影响启动：同时勾了 Markdown / Word 而机器上没装 `python-docx` 时，会打印一行提示后**跳过 Word**、其余格式照常导出；只勾 Word 才会报错让你装依赖。

也可以装成一个命令（可选；装了之后在任何目录都能用 `miui-notes-export`）：

```powershell
python -m pip install .
miui-notes-export "小米备份.zip" -o .\dist -f all
```

## 使用

### 图形界面（推荐）

双击本目录下的 **`启动.bat`**（或执行 `python main.py`）：

- 在界面里**手动选择备份包**（`.zip` 或 `.bak`）和输出目录；
- 勾选要导出的格式（Markdown / Word / HTML）；
- 选「**与已有文档合并**」或「不合并」（见下面的「重复内容怎么处理」）；
- 点「开始导出」，导出在后台线程里跑，窗口不会卡住；
- 完成后可以一键打开输出目录或 `index.html` 目录页。

`启动.bat` 会自动找 Python（优先 `%LOCALAPPDATA%\Programs\Python\Python311`，其次 `Python312/313`、`python`、`py`）。

> `启动.bat` 是 **GBK(cp936) + CRLF** 编码的批处理文件，这是为了让中文 Windows 的 `cmd` 能正确解析路径与提示语。由于 GitHub 按 UTF-8 显示文本，**在网页上直接点开这个文件会看到乱码，属于正常现象**；下载到本地双击运行不受影响。

### 命令行

在 **本目录下**执行（未安装成包，`python -m miui_notes_bak` 需要能 import 到这个目录）。最简单的情况下，直接把压缩包或 `.bak` 丢给它：

```powershell
# 直接吃小米备份 zip（自动在临时目录解出 .bak）
python -m miui_notes_bak "小米备份.zip" -o .\dist

# 也可以用已经解出来的 .bak
python -m miui_notes_bak ".\备份\笔记(com.miui.notes).bak" -o .\dist

# 三种格式都要（Markdown + Word + HTML，并生成 index.html 目录页）
python -m miui_notes_bak "小米备份.zip" -o .\dist -f all

# 只要 Markdown（零第三方依赖）
python -m miui_notes_bak "小米备份.zip" -o .\dist -f md

# 只要能在浏览器里翻阅的 HTML
python -m miui_notes_bak "小米备份.zip" -o .\dist -f html

# 只要 Markdown + HTML（逗号分隔，跳过 Word 就不需要 python-docx）
python -m miui_notes_bak "小米备份.zip" -o .\dist -f md,html

# 关闭合并：即使内容一模一样，也在输出目录里再写一份
python -m miui_notes_bak "小米备份.zip" -o .\dist -f all --no-merge

# 给一个「没有清单的老导出目录」补清单与目录页（不重新导出笔记）
python -m miui_notes_bak "小米备份.zip" -o .\dist --rebuild-index

# 额外把附件原样再存一份，并列出没被任何笔记引用的附件
python -m miui_notes_bak "小米备份.zip" -o .\dist --keep-raw --copy-unreferenced

# 换设备后导出不对 / 干脆报错：先体检备份包结构（报告不含笔记正文，可以直接贴出去求助）
python -m miui_notes_bak "小米备份.zip" --diagnose

# 看版本
python -m miui_notes_bak --version
```

参数一览：

| 参数 | 说明 |
| --- | --- |
| `source` | 备份 zip、`.bak` 文件，或包含它们的目录 |
| `-o, --out` | 输出目录，默认 `notes-export` |
| `-f, --format` | `md` / `docx` / `html` / `both`(=md+docx，默认) / `all`(三种都要)；也可用逗号组合，如 `md,html` |
| `--merge` / `--no-merge` | 是否与输出目录里已有的笔记合并（默认 `--merge`） |
| `--rebuild-index` | 只重建清单与目录页，不重新导出笔记 |
| `--diagnose` | 只体检备份包结构并输出诊断报告（不含笔记正文），换设备后排查用 |
| `--version` | 打印版本号 |
| `--keep-raw` | 另存一份原始附件到 `raw_attachments/` |
| `--copy-unreferenced` | 把没被引用到的附件放进 `raw_attachments/unreferenced/` |
| `--workdir` | 解压 zip 的临时目录 |
| `--no-index` | 不生成 `index.csv` / `index.html` |

### 重复内容怎么处理

同一个备份包反复导出、或者两次备份里有同一篇笔记时，默认**不会**产生第二份文件：

- 每篇笔记会算一枚**内容指纹**（`ID + 正文 + 全部关联附件的内容哈希`，刻意不含时间、标题和目录），落盘在输出目录的 `export-manifest.json` 里；
- 导出时先查清单：**内容指纹一致且 `note.md`/`note.docx`/`note.html` 都在磁盘上**，就直接跳过（终端会打印 `合并：N 篇内容与已有文件完全相同`），目录页仍然收录这篇笔记；
- 只改了标题或时间（内容没变）也算同一篇，沿用原来的目录，不会多出「标题 (2)」；
- 内容真的变了（正文或附件改了），会在**原地覆盖**那一篇，不新增目录；
- 清单可以随时删掉：删了只是让下次重新导出，不会丢笔记；老目录（清单丢失、或备份里的标题改过）在合并模式与 `--rebuild-index` 下会**按 `note.md` 正文认领**原目录。

## 兼容性

备份格式没有公开文档，不同系统版本、不同机型之间会有些细微差别。工具在这些地方**不写死**，尽量按结构特征去认：

| 可能变化的点 | 工具怎么处理 |
| --- | --- |
| 备份包里同时有很多应用 | 在 zip / 目录里找出全部 `.bak`，读文本头里的包名，只挑 `com.miui.notes`；找不到时明确报错并列出候选，不会拿别的应用的数据瞎解 |
| 文本头行数、字段略有不同 | 不按固定字节数切，而是逐行读、读到 `none` / `password` / `raw` 这类收尾标记为止；tar 起点靠扫 `ustar` 魔数 + 校验和定位 |
| 记录的 protobuf 字段号变了 | 容器字段不假设一定是 `#6`，而是找「装着目录/笔记记录」的那个字段；正文项也不假设一定是 `#9`，先按内部 `#1` 是不是 MIME 字符串来识别，认不出才回退 `#9` |
| 某条记录缺 ID 或时间戳 | 时间戳回退用正文项的字段并且只接受毫秒级（`>10**12`）；ID 缺失时用正文里的短数字串、再不行用记录偏移生成一个，保证不串篇 |
| 出现不认识的字段 | 不认识但是字符串/内嵌消息的字段收进 `note.extra`，不会让整条解析失败 |
| 备份被加密了 | 文本头里的加密标志非 `0` 时直接给出中文提示——本工具只做离线解析，不支持密码解密 |
| 机器上没装 `python-docx` | 跳过 Word、其余格式照常导出（只勾 Word 时才报错） |
| 正文里的下划线 | Markdown 没有下划线语法，所以 **Markdown 里只保留文字、不加任何 HTML 标签**；HTML 和 Word 里下划线样式照旧保留 |

需要说明的是：以上兼容性是在**一份**澎湃OS3 真实备份（笔记 `2.4.0.9`）上验证的，其余变体是用合成备份模拟的。如果你的机型和系统版本导出后有问题，把终端里的报错信息提出来即可定位——解析层对不认识的字段是宽容的，通常是某个字段的位置需要补一条规则。

## 输出结构

结构和备份包一致：**目录→笔记**两级，每篇笔记一个文件夹，自己的附件放在自己的 `note.assets/` 里。

```
dist/
├── index.html                            目录页：按目录分组列全部笔记，带搜索框，双击即可浏览
├── index.csv                             全部笔记的清单（utf-8-sig，Excel 可直接打开）
├── export-manifest.json                  「合并去重」用的清单：每篇笔记的内容指纹与落点
├── 日记（暂定）/                          备份里的目录名，原名保留
│   ├── 2026-09-03/                        笔记名（标题→摘要→未命名笔记）
│   │   ├── note.md                       正文（图片按相对路径引用 note.assets/）
│   │   ├── note.html                     单篇笔记的网页版（图片同样是相对引用）
│   │   ├── note.docx                     Word 版，图片已内嵌
│   │   └── note.assets/
│   │       ├── 32cbc4587467a266977f5eeeb291bb99132aff16.png
│   │       └── 1e42b214....jpg
│   └── ...
├── 游戏相关/
│   └── ...
└── 未分类/                               没有归入任何目录的笔记
    └── ...
```

用 `-f html` 或 `-f all` 时会生成 HTML；`index.html` 是纯静态页面（内联 CSS 和一个原生 JS 搜索框），**离线可开、可以整个目录打包带走**，也能直接扔进任何静态站点或网盘。图片是相对路径引用 `note.assets/`，所以移动整个 `dist/` 不会断链。

说明：

- 附件原文件名是**内容的 SHA1**，没有扩展名。工具按魔数识别真实类型并补上扩展名（图片 `.png` / `.jpg`、识别结果 `.json`、手写笔迹 `.hwp`、多页手写 `.entpage`），文件内容一个字节都不改。
- `note.assets/` 里除了图片，还会有笔记引用的非图片文件（手写笔迹 `.ent`、识别结果 `.json`）。它们没法在正文里显示，但都完整保留，`note.md` 末尾会逐条列出。
- 手写笔记：正文为空，工具会把笔迹缩略图插到正文位置；原始笔迹文件 `HWPENKITDATA` / 多页 `ENTPAGEINFO` 属于小米私有格式，**无法解码成通用图片**，按原样保留在 `note.assets/` 里。
- 文件名中的非法字符（`< > : " / \ | ? *`）会被替换，过长会截断。

## 作为库使用

```python
from miui_notes_bak import MiuiBackup, load_notes
from miui_notes_bak import content, attachments

with MiuiBackup(r'.\备份\笔记(com.miui.notes).bak') as bak:
    notes, raw = load_notes(bak)          # 目录记录 + 笔记记录
    atts = bak.attachments()              # {相对路径: bytes}，全部附件
    for note in notes:
        if note.is_folder:
            print('目录:', note.title)
            continue
        doc = content.parse_content(note.content, list(atts))
        refs = attachments.collect_refs(note, doc, attachments.Resolver(sorted(atts)), raw)
        print(note.note_id, note.display_title, len(doc.blocks), len(refs))
```

`export.py` 里的三个渲染函数都以同一份块列表（`content.NoteDocument`）为输入：
`note_markdown()` / `note_html()` / `note_docx()`，另外 `write_index_html()` 生成目录页。

## 项目结构

分层很清楚，每一层只依赖下一层，换格式或换输出都只动一处：

| 文件 | 职责 |
| --- | --- |
| `miui_notes_bak/bakfile.py` | 容器层：`.bak` 文本头、tar 定位与读取、`_tmp_bak` 的 protobuf 解析成 `Note` |
| `miui_notes_bak/content.py` | 正文层：`<text>` / `<input>` / `<b>` / 图片标记等自定义标记 → 块模型 |
| `miui_notes_bak/attachments.py` | 附件层：把正文里的附件引用映射到真实附件（SHA1 命名 + 相对路径） |
| `miui_notes_bak/media.py` | 附件类型识别（按魔数补扩展名）、文件名清洗 |
| `miui_notes_bak/export.py` | 输出层：Markdown / HTML / Word 三种渲染 + 目录页 |
| `miui_notes_bak/manifest.py` | 去重层：内容指纹、导出清单、目录分配、按正文认领旧目录 |
| `miui_notes_bak/diagnose.py` | 诊断层：只输出结构统计、不碰正文的体检报告 |
| `miui_notes_bak/cli.py` | 入口层：命令行参数、输入准备（zip / 目录 / 单个 `.bak`）、导出编排 |
| `main.py` | tkinter 图形界面（导出跑在后台线程，失败弹错误框而不是静默卡住） |
| `启动.bat` | Windows 双击启动（GBK + CRLF 编码，原因见「使用」一节） |
| `tools/` | 针对导出结果的自检脚本（纯标准库，不需要浏览器） |
| `tests/` | 测试与合成备份生成器（不含任何真实数据） |
| `技术细节.md` | 格式逆向的完整记录：容器布局、protobuf 字段表、附件关联、踩过的坑 |

## 自检

```powershell
# 纯合成数据的回归测试（秒级，不依赖备份文件）：正文解析边界 + 合成备份端到端导出
python tests\test_parse_content.py

# 合并去重语义（纯合成数据，秒级）：重复导出不产生第二份、改标题沿用原目录、重建清单
python tests\test_manifest.py

# 兼容性（纯合成数据，秒级）：字段号变体 / 头部变体 / 多应用备份包 / 加密备份 / 缺 python-docx
python tests\test_compat.py

# 诊断报告（纯合成数据，秒级）：确认报告不含正文、对各种变体都能出结果
python tests\test_diagnose.py

# 真实备份上的端到端断言（只读备份，输出到临时目录，跑完删除；不打印任何笔记正文）
python tests\test_export.py "小米备份.zip"
```

`test_export.py` 覆盖：容器与 protobuf 解析、正文标记剥离、附件类型识别、全量导出篇数、Markdown 图片链接有效性、HTML 结构与引用完整性、Word 可打开且内嵌图片数与 Markdown 一致、`index.html` 链接有效。`test_manifest.py` 覆盖合并去重的全部语义，`test_compat.py` 覆盖格式变体与容错，`test_diagnose.py` 覆盖诊断报告（后三个全部用合成数据，不需要真实备份）。五个脚本退出码 0 表示全部通过。

> `test_export.py` 只断言「结构不变量」（各种格式的篇数互相对得上、链接都有效、图片数三处一致），**不写死任何一份备份的具体数字**，所以换一台手机、换一份备份也能用。没有提供备份时它会打印「跳过」并返回 0，不会让整个测试套件变红。

另外两个**针对导出结果**的自检脚本（纯标准库，不依赖浏览器）：

```powershell
# HTML：doctype / utf-8 声明 / 标签闭合 / 每个 src、href 目标是否存在
python tools\html_check.py .\dist

# Markdown：note.assets 里的文件是否都被 md 提到；md 与 docx 是否配对
python tools\verify_export.py .\dist
```

两个脚本默认只打印汇总（导出目录里会有私人笔记标题，逐页输出不适合贴给别人），加 `-v` 才逐页列出。

> `test_export.py` 可以直接吃备份 zip：内部会先解出 `.bak`，并且**必须等全部步骤跑完才清理临时目录**（早期版本在第 1 步就把它删了，导致第 4 步 `FileNotFoundError: ...\backup.bak`，只在传 zip 时复现）。

`tests\make_synthetic_bak.py` 可以单独运行，在当前目录生成一份**不含任何真实数据**的最小备份，用来快速验证整条链路：

```powershell
python tests\make_synthetic_bak.py
python -m miui_notes_bak tests\synthetic.bak -o .\dist-synthetic
```

仓库里的 `demo-html/` 就是用上面这份合成数据生成的示例（完全不含真实笔记），双击 `demo-html\index.html` 可以直接看到导出效果。

## 隐私与安全

这个工具处理的是一份**完整的个人笔记备份**，值得把边界说清楚。详见 [`SECURITY.md`](SECURITY.md)。

**它不会做的事**

- 不联网：解析与导出全程只有本地文件读写，没有任何网络请求。
- 不读账号：不需要登录小米账号，也不访问 `i.mi.com`。
- 不改原始备份：对 `.bak` / `.zip` 只读；唯一会写的地方是你的输出目录，以及解 zip 时用的系统临时目录（`%TEMP%\miui-notes-xxxx`，跑完即删）。

**它会写出这些文件**

- `note.md` / `note.html` / `note.docx` 和 `note.assets/` 里的附件——**全都是明文**，包括图片、手写识别结果、通话笔记里的内容。导到哪，哪里就有一份完整副本。
- `export-manifest.json` 记录了每篇笔记的落点，并且带**本机绝对路径**。
- `--keep-raw` 会再把原始附件整个释放一份。

**几条建议**

1. 导出目录别放在共享盘或同步盘的公共位置；敏感笔记建议放在加密盘（如 VeraCrypt）里。
2. 分享截图或打包给别人之前，先清掉 `note.assets/`、`export-manifest.json` 和正文里不打算公开的部分。
3. **不要**把导出结果提交进 Git。仓库的 `.gitignore` 已经排除了 `dist/`、`导出结果/`、`_work/`、`*.bak`、`*.zip`。
4. 遇到问题要发出来求助时，优先用 `--diagnose`：它的输出只有结构统计（包名、记录数、字段号、附件类型分布），**不含任何正文、标题和附件内容**，可以直接贴。

## 常见问题

**导出的 Word 里没有图片？**
检查 `note.assets/` 是否有文件；`note.md` 的图片链接与 `note.docx` 的内嵌图片在测试里是逐一比对的，二者数量应当一致。

**手写笔记只有一张图？**
这是正常的。手写笔迹保存在小米私有的 `.ent`（`HWPENKITDATA`）里，只有 app 自己能渲染，导出的是备份里同时存下的缩略图。

**能导出其他应用的备份吗？**
不能。`_tmp_bak` 的 protobuf 结构是「笔记」这个应用自己的，本工具只针对 `com.miui.notes`。

**数据安全？**
解析与导出全在本机完成，不发起任何网络请求，不修改原始备份文件。会写出哪些明文、怎么处理更稳妥，见上面的「隐私与安全」。

**换了一台手机 / 升了系统，导出不对或直接报错？**
先跑一遍体检：

```powershell
python -m miui_notes_bak "小米备份.zip" --diagnose
```

报告里有文本头行数、tar 起点、记录数、顶层字段号、记录字段号、附件类型分布和解析错误，**不含任何笔记正文**，可以直接贴出来。最常见的情况是记录的字段号变了——报告里「未识别的字段号」那行会直接显示出来；GUI 报错时也会提示你先跑这个。

## 关于 AI 辅助

这一节说明代码是怎么来的，避免误解。

- **需求与验收**：由仓库主人在对话中逐项提出（读取本地备份包、导出 Markdown / Word / HTML、图片保留、输出结构一致、重复内容合并、图形界面、上传 GitHub），并逐项验收。
- **代码与文档**：`miui_notes_bak/`、`main.py`、`tools/`、`tests/`、`README.md`、`技术细节.md` 主要由 AI 编程助手（DeepSeek 模型）生成与迭代。
- **格式逆向**：`.bak` 的文本头与 tar 布局、`_tmp_bak` 的 protobuf 字段、附件按 SHA1 命名、手写与多页手写的关联方式，全部是从一份澎湃OS3（`OS3.0.305.0.WNMCNXM`，笔记 `2.4.0.9`）的真实备份包里逐字节实测出来的。这个格式没有公开文档。
- **验证方式**：每个结论都在真实备份或合成数据上跑过。开发中发现的死循环（行内图片紧跟块级标签）、三个同名 `multi_page_ent.ent` 被误匹配到同一份、测试提前删除 zip 临时目录等问题，都有可复现的用例，不是推测。
- **局限**：没有经过第三方代码审计；CI（GitHub Actions，Linux / Windows × Python 3.9 / 3.11 / 3.13）跑的只有合成数据和格式变体，真实备份上的结论仍然只来自一份设备、一个系统版本。换机型或系统版本可能需要微调。

如果你要把它用在重要数据上，建议先用 `--keep-raw` 导一份原始附件，再和 `note.assets/` 对一遍。

## 许可证

MIT License，见 [`LICENSE`](LICENSE)。

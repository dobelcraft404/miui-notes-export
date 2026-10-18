# -*- coding: utf-8 -*-
"""MIUI / 澎湃OS 笔记备份导出工具。

    python -m miui_notes_bak <备份.zip 或 .bak> -o 输出目录

图形界面：直接运行目录下的 `启动.bat`，或 `python main.py`。
"""

from .bakfile import MiuiBackup, Note, load_notes          # noqa: F401
from .manifest import Manifest, content_fingerprint        # noqa: F401

__version__ = '1.2.1'

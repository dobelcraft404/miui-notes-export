# -*- coding: utf-8 -*-
"""附件（miui_att）的识别与落盘。

附件在备份里没有扩展名，文件名通常是内容的 SHA1，只能按魔数判断真实类型。
"""

from __future__ import annotations

import json
import os
import re
import zipfile
import io
from typing import Dict, Optional, Tuple

# (魔数前缀, 扩展名)
_MAGIC: Tuple[Tuple[bytes, str], ...] = (
    (b'\x89PNG\r\n\x1a\n', '.png'),
    (b'\xff\xd8\xff', '.jpg'),
    (b'GIF87a', '.gif'),
    (b'GIF89a', '.gif'),
    (b'BM', '.bmp'),
    (b'%PDF', '.pdf'),
    (b'PK\x03\x04', '.zip'),
    (b'\x1f\x8b', '.gz'),
    (b'HWPENKITDATA', '.hwp'),
    (b'ENTPAGEINFO', '.entpage'),
)

_TEXT_EXT = {'.json', '.txt', '.xml', '.html', '.md'}


def detect(data: bytes) -> Tuple[str, str]:
    """返回 (类型名, 建议扩展名)。"""
    if not data:
        return 'empty', '.bin'
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            name = {
                '.png': 'image/png', '.jpg': 'image/jpeg', '.gif': 'image/gif',
                '.bmp': 'image/bmp', '.pdf': 'application/pdf', '.zip': 'application/zip',
                '.gz': 'application/gzip', '.hwp': 'handwriting/penkit',
                '.entpage': 'handwriting/multipage',
            }[ext]
            return name, ext
    if data[:1] in (b'{', b'['):
        try:
            json.loads(data.decode('utf-8'))
            return 'application/json', '.json'
        except Exception:
            pass
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp', '.webp'
    return 'application/octet-stream', '.bin'


def is_image(data: bytes) -> bool:
    return detect(data)[0].startswith('image/')


def safe_name(name: str, fallback: str = 'file') -> str:
    """把任意名字净化成 Windows 上合法的单层文件名（不含目录分隔）。"""
    name = name.replace('\\', '/').rsplit('/', 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')
    return name or fallback


class PenKitImages:
    """手写笔迹（HWPENKITDATA / .ent）里内嵌的位图。

    实测笔迹文件是 zip 容器，内部包含若干条目；只要能解出 PNG/JPEG 就能
    在导出结果里还原用户看到的手写画面。无法解析时返回空，由调用方回退到
    记录里的 thumbnail（缩略图）。
    """

    def __init__(self, data: bytes):
        self.data = data
        self._images: Optional[list] = None

    def _load(self) -> list:
        if self._images is not None:
            return self._images
        self._images = []
        try:
            with zipfile.ZipFile(io.BytesIO(self.data)) as zf:
                for info in sorted(zf.infolist(), key=lambda i: i.filename):
                    if info.is_dir():
                        continue
                    blob = zf.read(info)
                    if is_image(blob):
                        self._images.append((info.filename, blob))
        except Exception:
            self._images = []
        return self._images

    def images(self) -> list:
        return self._load()

    def first(self) -> Optional[bytes]:
        imgs = self._load()
        return imgs[0][1] if imgs else None


def looks_like_penkit(data: bytes) -> bool:
    return data.startswith(b'HWPENKITDATA') or data[:1] == b'P'


def write_asset(directory: str, filename: str, data: bytes) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, filename)
    with open(path, 'wb') as fh:
        fh.write(data)
    return path


def human_size(n: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return '%.0f %s' % (n, unit) if unit == 'B' else '%.1f %s' % (n, unit)
        n /= 1024.0
    return '%.1f GB' % n

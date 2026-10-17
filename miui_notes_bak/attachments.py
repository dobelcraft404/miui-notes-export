# -*- coding: utf-8 -*-
"""附件名解析：把笔记记录里的各种引用形式对应到 miui_att 里的实际条目。

笔记记录里出现附件的方式有四种：
  1. 正文里的行内图片：`☺ <40 位 hash><0/></>`
  2. 手写笔记 #16 的 JSON：entName / recognizedFile / thumbnail / images
  3. 多页手写 #18/#19/#20/#21：`ent_multi_engine_and_page_<时间戳>/xxx`
  4. 记录原始字节里散落的 40 位 hash（兜底，覆盖面最广）
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Sequence

from .bakfile import Note

HEX40 = re.compile(r'[0-9a-f]{40}')


def basename(path: str) -> str:
    return (path or '').replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]


class Resolver:
    """把记录里的引用串解析成 miui_att 下的实际条目名。"""

    def __init__(self, available: Sequence[str]):
        self.available = list(available)
        self._by_base: Dict[str, str] = {}
        for name in self.available:
            self._by_base.setdefault(basename(name), name)

    def __call__(self, token: str) -> Optional[str]:
        token = (token or '').strip().replace('\\', '/')
        if not token:
            return None
        if token in self.available:
            return token
        base = basename(token)
        # 路径型引用（多页手写：`ent_multi_engine_and_page_<ts>/xxx`）优先按路径片段
        # 匹配。备份里同名文件可能出现在不同目录下（如三个不同时间戳的
        # multi_page_ent.ent），只按文件名匹配会把它们全部指向同一个文件。
        parts = [p for p in token.split('/') if p and p not in ('.', '..')]
        if len(parts) > 1:
            best: Optional[str] = None
            for name in self.available:
                segs = name.split('/')
                window = len(parts)
                if any(segs[i:i + window] == parts
                       for i in range(len(segs) - window + 1)):
                    if best is None or len(name) > len(best):
                        best = name
            if best is not None:
                return best
        if base in self._by_base:
            return self._by_base[base]
        for suffix in ('.ent', '.jpg', '.png', '.json'):
            if base + suffix in self._by_base:
                return self._by_base[base + suffix]
        stem = base.rsplit('.', 1)[0]
        if stem != base and stem in self._by_base:
            return self._by_base[stem]
        return None

    def from_bytes(self, blob: bytes) -> List[str]:
        """整条记录里出现的 40 位 hash（要求至少含一个 a-f，避免误判纯数字）。"""
        out: List[str] = []
        for m in re.finditer(rb'(?=[0-9a-f]{40})(?=[0-9a-f]*[a-f])[0-9a-f]{40}', blob):
            name = self(m.group(0).decode('ascii'))
            if name and name not in out:
                out.append(name)
        return out

    def from_extra(self, note: Note) -> List[str]:
        """从 #16（手写 JSON）与 #18/#19/#20/#21（多页手写）里取附件。"""
        out: List[str] = []
        seen = set()

        def add(token: str) -> None:
            name = self(token)
            if name and name not in seen:
                seen.add(name)
                out.append(name)

        for key in ('16', '18', '19', '20', '21'):
            raw = (note.extra.get(key) or '').strip()
            if not raw:
                continue
            if key == '16':
                brace = raw.find('{')
                obj = None
                if brace >= 0:
                    try:
                        obj = json.loads(raw[brace:])
                    except Exception:
                        obj = None
                if isinstance(obj, dict):
                    for field in ('entName', 'recognizedFile', 'thumbnail'):
                        if obj.get(field):
                            add(str(obj[field]))
                    for item in obj.get('images') or []:
                        if isinstance(item, str):
                            add(item)
                else:
                    for m in HEX40.finditer(raw):
                        add(m.group(0))
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                obj = None
            if isinstance(obj, list):
                for item in obj:
                    if isinstance(item, str):
                        add(item)
            else:
                add(raw)
        return out


def _item_hashes(note: Note, resolver: 'Resolver') -> List[str]:
    """条目（#9）的 #4 里有时直接放的就是附件名（手写笔记的笔迹/缩略图/识别结果）。"""
    out: List[str] = []
    seen = set()
    for item in note.items:
        blob = item.content
        if not blob or len(blob) > 96:
            continue
        text = blob.decode('ascii', 'ignore').strip()
        if not text:
            continue
        name = resolver(text)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def collect_refs(note: Note, doc, resolver: 'Resolver', raw: bytes) -> List[str]:
    """汇总一篇笔记的附件：正文引用 -> 条目附件 -> 手写/多页字段 -> 记录内 hash。"""
    out: List[str] = []
    seen = set()

    def add(name: Optional[str]) -> None:
        if name and name not in seen:
            seen.add(name)
            out.append(name)

    for ref in doc.references:
        add(ref)
    for name in _item_hashes(note, resolver):
        add(name)
    for name in resolver.from_extra(note):
        add(name)
    for name in resolver.from_bytes(raw[note.record_offset:note.record_end]):
        add(name)
    return out


def handwrite_preview(note: Note, resolver: Resolver) -> Dict[str, str]:
    """手写笔记：entName（笔迹私有格式）-> thumbnail（可直接查看的图片）。

    笔迹文件 HWPENKITDATA 是小米自有的二进制格式，无法通用解码；记录里同时
    保存了缩略图，导出时用缩略图代替笔迹原文件展示。
    """
    mapping: Dict[str, str] = {}
    raw = note.extra.get('16') or ''
    brace = raw.find('{')
    if brace < 0:
        return mapping
    try:
        obj = json.loads(raw[brace:])
    except Exception:
        return mapping
    if not isinstance(obj, dict):
        return mapping
    ent = resolver(str(obj.get('entName', '')))
    thumb = resolver(str(obj.get('thumbnail', '')))
    if ent and thumb:
        mapping[ent] = thumb
    return mapping

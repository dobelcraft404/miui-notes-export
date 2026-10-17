# -*- coding: utf-8 -*-
"""MIUI / 澎湃OS 笔记备份（.bak）解析器。

格式说明（基于小米澎湃OS3，Notes 2.4.0.9 的备份实测）：

    MIUI BACKUP\n          <- magic
    2\n                    <- 容器版本
    com.miui.notes 笔记\n   <- 包名 + 应用名
    -1\n0\n                <- feature / bakType
    ANDROID BACKUP\n5\n0\nnone\n   <- 子系统、版本、加密标志、加密算法

magic 行（65 字节）之后紧跟一个标准 POSIX tar 归档（未压缩）。归档内与本
工具相关的条目：

    apps/com.miui.notes/miui_bak/_tmp_bak     笔记正文（protobuf 编码）
    apps/com.miui.notes/miui_att/<...>        附件（文件名多为内容的 SHA1）
    apps/com.miui.notes/_meta                 桌面小组件偏好（与正文无关）

_tmp_bak 的 protobuf 结构：

    message Root {
      repeated Group group = 6;      // 每项是一个目录或笔记
      ...
    }
    message Group {
      optional string id = 2;        // 笔记 ID（目录为目录 ID）
      optional int64  created = 5;   // 创建时间（毫秒）
      optional int64  modified = 6;  // 修改时间（毫秒）
      optional string summary = 8;   // 首行摘要
      repeated Item item = 9;        // 正文 / 附件描述项
      optional string folder = 10;   // 所属目录名
      optional string title = 14;    // 标题
      optional string kind = 15;     // common | handwrite | multi_handwrite
      optional string extra16 = 16;  // 手写笔迹 JSON
      optional string extra18 = 18;  // 多页手写主文件相对路径
      optional string extra20 = 20;  // 多页手写导出图相对路径
    }
    message Item {
      optional string mime = 1;      // 条目类型
      optional string content = 4;   // 正文（自定义富文本标记）
      optional string kind = 10;     // multi_handwrite 等
    }
"""

from __future__ import annotations

import io
import re
import tarfile
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

MAGIC = b'MIUI BACKUP\n'
PACKAGE = 'com.miui.notes'
NOTE_ROOT = 'apps/com.miui.notes/'
BAK_ENTRY = NOTE_ROOT + 'miui_bak/_tmp_bak'
ATT_PREFIX = NOTE_ROOT + 'miui_att/'

# 40 位 hex（SHA1）：必须至少包含一个 a-f，避免把正文里的纯数字串误判成附件名
HEX40_RE = re.compile(rb'(?=[0-9a-f]{40})(?=[0-9a-f]*[a-f])[0-9a-f]{40}')

MIME_TEXT_NOTE = 'vnd.android.cursor.item/text_note'
MIME_CALL_NOTE = 'vnd.android.cursor.item/call_note'


class BackupFormatError(Exception):
    """备份文件不符合预期格式。"""


def read_header(data: bytes) -> Tuple[List[str], int]:
    """解析 MIUI BACKUP 文本头，返回（各字段, tar 起始偏移）。"""
    if not data.startswith(MAGIC):
        raise BackupFormatError('不是 MIUI BACKUP 文件（缺少 magic）')
    end = data.find(b'\n', len(MAGIC))
    if end < 0:
        raise BackupFormatError('头部不完整')
    lines: List[str] = []
    pos = 0
    while True:
        nxt = data.find(b'\n', pos)
        if nxt < 0:
            raise BackupFormatError('头部缺少结尾换行')
        lines.append(data[pos:nxt].decode('utf-8'))
        pos = nxt + 1
        if len(lines) >= 4 and lines[-1] in ('none', 'password', 'raw'):
            break
    return lines, pos


class MiuiBackup:
    """按需读取 .bak 内 tar 条目的封装。"""

    def __init__(self, path: str):
        with open(path, 'rb') as fh:
            self._data = fh.read()
        self.header_lines, self.tar_offset = read_header(self._data)
        self._tar = tarfile.open(fileobj=io.BytesIO(self._data[self.tar_offset:]), mode='r:')
        self._members = {m.name: m for m in self._tar.getmembers()}

    # -- 元信息 ---------------------------------------------------------
    @property
    def package(self) -> str:
        return self.header_lines[2].split(' ', 1)[0] if len(self.header_lines) > 2 else ''

    def names(self) -> List[str]:
        return list(self._members)

    def attachments(self) -> Dict[str, bytes]:
        """返回 {miui_att 之后的相对路径: 文件内容}。"""
        out: Dict[str, bytes] = {}
        for name, m in self._members.items():
            if m.isfile() and name.startswith(ATT_PREFIX):
                out[name[len(ATT_PREFIX):]] = self.read(name)
        return out

    def read(self, name: str) -> bytes:
        m = self._members.get(name)
        if m is None or not m.isfile():
            raise KeyError('备份中没有条目: %s' % name)
        fh = self._tar.extractfile(m)
        if fh is None:
            raise KeyError('无法读取条目: %s' % name)
        return fh.read()

    def close(self) -> None:
        self._tar.close()

    def __enter__(self) -> 'MiuiBackup':
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 最小 protobuf 解析（只实现 wire type 0/1/2/5）
# ---------------------------------------------------------------------------

@dataclass
class Field:
    number: int
    wire: int
    value: object          # varint -> int；length-delimited -> bytes
    start: int = 0         # 值在缓冲区内的偏移
    end: int = 0


def _read_varint(buf: bytes, i: int) -> Tuple[int, int]:
    shift = 0
    val = 0
    while True:
        if i >= len(buf):
            raise ValueError('varint 越界')
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7
        if shift > 63:
            raise ValueError('varint 过长')


def parse_message(buf: bytes, start: int = 0, end: Optional[int] = None) -> List[Field]:
    """把 [start, end) 解析成字段列表。"""
    if end is None:
        end = len(buf)
    out: List[Field] = []
    i = start
    while i < end:
        key, i = _read_varint(buf, i)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise ValueError('字段号为 0')
        if wire == 0:
            val, i = _read_varint(buf, i)
            out.append(Field(number, wire, val))
        elif wire == 2:
            ln, i = _read_varint(buf, i)
            if i + ln > end:
                raise ValueError('length-delimited 越界')
            out.append(Field(number, wire, buf[i:i + ln], i, i + ln))
            i += ln
        elif wire == 5:
            if i + 4 > end:
                raise ValueError('fixed32 越界')
            out.append(Field(number, wire, buf[i:i + 4], i, i + 4))
            i += 4
        elif wire == 1:
            if i + 8 > end:
                raise ValueError('fixed64 越界')
            out.append(Field(number, wire, buf[i:i + 8], i, i + 8))
            i += 8
        else:
            raise ValueError('不支持的 wire type %d' % wire)
    return out


# ---------------------------------------------------------------------------
# 笔记模型
# ---------------------------------------------------------------------------

@dataclass
class Item:
    mime: str = ''
    content: bytes = b''
    kind: str = ''
    values: Dict[int, object] = field(default_factory=dict)


@dataclass
class Note:
    note_id: str
    created: int = 0
    modified: int = 0
    summary: str = ''
    folder: str = ''
    title: str = ''
    kind: str = 'common'
    items: List[Item] = field(default_factory=list)
    extra: Dict[int, str] = field(default_factory=dict)
    field_number: int = 2          # 2=笔记，1=目录
    record_offset: int = 0
    record_end: int = 0

    @property
    def is_folder(self) -> bool:
        return self.field_number == 1

    @property
    def mime(self) -> str:
        return self.items[0].mime if self.items else ''

    @property
    def content(self) -> bytes:
        for it in self.items:
            if it.mime in (MIME_TEXT_NOTE, MIME_CALL_NOTE) and it.content:
                return it.content
        return self.items[0].content if self.items else b''

    @property
    def images(self) -> List[Item]:
        return [it for it in self.items if it.mime.startswith('image/')]

    @property
    def display_title(self) -> str:
        for cand in (self.title, self.summary):
            cand = (cand or '').strip()
            if cand:
                return cand
        return '笔记-%s' % self.note_id


def _decode(b: object) -> str:
    if isinstance(b, bytes):
        return b.decode('utf-8', 'replace')
    if b is None:
        return ''
    return str(b)


def load_notes(backup: MiuiBackup) -> Tuple[List[Note], bytes]:
    """从 .bak 中读出全部目录与笔记，返回（笔记列表, _tmp_bak 原始字节）。"""
    raw = backup.read(BAK_ENTRY)
    root = parse_message(raw)
    groups: List[Field] = []
    for f in root:
        if f.number == 6 and f.wire == 2:
            groups.extend(g for g in parse_message(raw, f.start, f.end)
                          if g.number in (1, 2) and g.wire == 2)

    notes: List[Note] = []
    for g in groups:
        fields = parse_message(raw, g.start, g.end)
        note = Note(note_id='', field_number=g.number,
                    record_offset=g.start, record_end=g.end)
        for f in fields:
            if f.number == 2 and f.wire == 2:
                note.note_id = _decode(f.value)
            elif f.number == 5 and f.wire == 0:
                note.created = int(f.value)
            elif f.number == 6 and f.wire == 0:
                note.modified = int(f.value)
            elif f.number == 8 and f.wire == 2:
                note.summary = _decode(f.value)
            elif f.number == 10 and f.wire == 2:
                note.folder = _decode(f.value)
            elif f.number == 14 and f.wire == 2:
                note.title = _decode(f.value)
            elif f.number == 15 and f.wire == 2:
                note.kind = _decode(f.value)
            elif f.number == 9 and f.wire == 2:
                if note.field_number == 1:
                    # 目录记录的 #9 是目录名，不是正文条目
                    note.title = _decode(f.value)
                    continue
                item = Item()
                for f2 in parse_message(raw, f.start, f.end):
                    item.values[f2.number] = f2.value
                    if f2.number == 1 and f2.wire == 2:
                        item.mime = _decode(f2.value)
                    elif f2.number == 4 and f2.wire == 2:
                        item.content = f2.value
                    elif f2.number == 10 and f2.wire == 2:
                        item.kind = _decode(f2.value)
                note.items.append(item)
            elif f.wire == 2:
                note.extra[f.number] = _decode(f.value)
        notes.append(note)
    return notes, raw

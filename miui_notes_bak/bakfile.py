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


def _valid_tar_header(block: bytes) -> bool:
    """校验 512 字节块是否像合法的 tar 头（大小与校验和）。"""
    if len(block) < 512:
        return False
    try:
        chksum = int(block[148:156].split(b'\x00')[0].strip() or b'0', 8)
    except ValueError:
        return False
    if not chksum:
        return False
    unsigned = sum(block[:148]) + 8 * 0x20 + sum(block[156:])
    signed = (sum(b if b < 128 else b - 256 for b in block[:148]) + 8 * 0x20 +
              sum(b if b < 128 else b - 256 for b in block[156:]))
    return chksum in (unsigned, signed)


def _find_tar_offset(data: bytes) -> int:
    """定位 tar 归档起点，不依赖文本头的字段个数。

    tar 头的 257 处是 'ustar'，但正文里也可能出现这个字符串，所以再用校验和
    验证一次，避免在错误偏移上打开归档。找不到返回 -1。
    """
    pos = data.find(b'ustar', 100)
    while pos > 0:
        start = pos - 257
        if start > 0 and _valid_tar_header(data[start:start + 512]):
            return start
        pos = data.find(b'ustar', pos + 1)
    return -1


def read_header(data: bytes) -> Tuple[List[str], int]:
    """解析 MIUI BACKUP 文本头，返回（各字段, tar 起始偏移）。

    兼容不同版本：不假设头部正好几个字段，而是读到无法再按 UTF-8 解码、或读到
    加密算法行（none / password / raw）为止；tar 起点则用 `_find_tar_offset`
    独立定位。
    """
    if not data.startswith(MAGIC):
        raise BackupFormatError('不是 MIUI BACKUP 文件（缺少 magic）')
    lines: List[str] = []
    pos = 0
    limit = min(len(data), 65536)
    while pos < limit:
        nxt = data.find(b'\n', pos)
        if nxt < 0 or nxt > limit:
            break
        try:
            line = data[pos:nxt].decode('utf-8')
        except UnicodeDecodeError:
            break
        lines.append(line)
        pos = nxt + 1
        if line in ('none', 'password', 'raw') and len(lines) >= 3:
            break
    if len(lines) < 3:
        raise BackupFormatError('头部不完整（只有 %d 行）' % len(lines))
    tar_offset = _find_tar_offset(data)
    if tar_offset < 0:
        raise BackupFormatError('头部之后没有找到 tar 归档')
    return lines, tar_offset


class MiuiBackup:
    """按需读取 .bak 内 tar 条目的封装。"""

    def __init__(self, path: str):
        with open(path, 'rb') as fh:
            self._data = fh.read()
        self.header_lines, self.tar_offset = read_header(self._data)
        self._tar = tarfile.open(fileobj=io.BytesIO(self._data[self.tar_offset:]), mode='r:')
        self._members = {m.name: m for m in self._tar.getmembers() if m.isfile()}
        self._root: Optional[str] = None
        self._att_prefix_cache: Optional[str] = None

    # -- 元信息 ---------------------------------------------------------
    @property
    def package(self) -> str:
        """备份的应用包名（从头部第 3 行等位置推断，兼容头部字段数变化）。"""
        for line in self.header_lines:
            tok = line.split(' ', 1)[0].strip()
            low = tok.lower()
            if tok.count('.') >= 1 and low.startswith(('com.', 'org.', 'net.', 'cn.', 'io.')):
                return tok
        return PACKAGE

    @property
    def encrypted(self) -> bool:
        """头部里的加密标志是否为 1（加密备份无法离线解析）。"""
        try:
            idx = self.header_lines.index('ANDROID BACKUP')
        except ValueError:
            return False
        rest = self.header_lines[idx + 1:]
        if len(rest) < 2:
            return False
        return rest[1].strip() not in ('0', '')

    def _app_root(self) -> str:
        if self._root is None:
            self._root = 'apps/%s/' % self.package
        return self._root

    def names(self) -> List[str]:
        return list(self._members)

    def _find_entry(self, wanted: str) -> str:
        """定位 _tmp_bak 之类的关键条目，允许包名/目录名变化。"""
        for cand in (self._app_root() + wanted, NOTE_ROOT + wanted):
            if cand in self._members:
                return cand
        tail = '/' + wanted
        for name in self._members:
            if name.endswith(tail):
                return name
        raise BackupFormatError('备份里没有找到条目：%s' % wanted)

    def _att_prefix(self) -> str:
        """附件目录前缀；找不到 miui_att 时返回空串。"""
        if self._att_prefix_cache is not None:
            return self._att_prefix_cache
        cands = [self._app_root() + 'miui_att/', NOTE_ROOT + 'miui_att/']
        for name in self._members:
            idx = name.find('/miui_att/')
            if idx >= 0:
                cands.append(name[:idx + len('/miui_att/')])
        for prefix in cands:
            if any(n.startswith(prefix) for n in self._members):
                self._att_prefix_cache = prefix
                return prefix
        self._att_prefix_cache = ''
        return ''

    def _is_meta(self, name: str) -> bool:
        return (name.endswith('_manifest') or name.endswith('_meta')
                or name.endswith('_tmp_bak') or '/miui_meta/' in name)

    def attachments(self) -> Dict[str, bytes]:
        """返回 {附件相对路径: 文件内容}。

        正常情况取 `.../miui_att/` 下的条目；若该目录不存在（不同版本），退化为
        「应用目录下除元数据外的全部条目」。
        """
        prefix = self._att_prefix()
        out: Dict[str, bytes] = {}
        for name in self._members:
            if prefix:
                if not name.startswith(prefix):
                    continue
                rel = name[len(prefix):]
            else:
                if self._is_meta(name):
                    continue
                root = self._app_root()
                rel = name[len(root):] if name.startswith(root) else name
            out[rel] = self.read(name)
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


def _submessages(raw: bytes, fields: List[Field]) -> List[Tuple[Field, List[Field]]]:
    """把 length-delimited 字段解析成子消息；解析失败的（通常是普通字符串）跳过。"""
    out: List[Tuple[Field, List[Field]]] = []
    for f in fields:
        if f.wire != 2 or f.end <= f.start:
            continue
        try:
            inner = parse_message(raw, f.start, f.end)
        except ValueError:
            continue
        if inner:
            out.append((f, inner))
    return out


def _looks_like_item(inner: List[Field]) -> bool:
    """Item 的第一字段是 MIME（`vnd.android.cursor.item/...` / `image/png` 等）。"""
    for f in inner:
        if f.number == 1 and f.wire == 2:
            text = _decode(f.value)
            if 0 < len(text) <= 200 and '/' in text and ' ' not in text:
                return True
    return False


def _pick_groups(raw: bytes, root: List[Field]) -> List[Field]:
    """从 Root 里取出「目录 / 笔记记录」列表。

    已知是 #6 包着全部记录；若版本把字段号改了，退化为遍历所有 length-delimited
    字段、找到确实装着记录（字段号 1 或 2 的子消息）的那一个。
    """
    def records_of(field: Field) -> List[Field]:
        try:
            inner = parse_message(raw, field.start, field.end)
        except ValueError:
            return []
        return [g for g in inner if g.wire == 2 and g.number in (1, 2)]

    for f in root:
        if f.number == 6 and f.wire == 2:
            found = records_of(f)
            if found:
                return found
    for f in root:
        if f.wire != 2:
            continue
        found = records_of(f)
        if found:
            return found
    return [g for g in root if g.wire == 2 and g.number in (1, 2)]


_SCALAR_FIELDS = {2, 5, 6, 8, 9, 10, 14, 15}


def _parse_record(raw: bytes, group: Field, fields: List[Field]) -> Note:
    note = Note(note_id='', field_number=group.number,
                record_offset=group.start, record_end=group.end)

    texts: Dict[int, str] = {}
    numbers: Dict[int, int] = {}
    for f in fields:
        if f.wire == 2:
            texts.setdefault(f.number, _decode(f.value))
        elif f.wire == 0:
            numbers.setdefault(f.number, int(f.value))

    note.created = numbers.get(5, 0)
    note.modified = numbers.get(6, 0)

    # Item：已知在 #9，但按「内部 #1 是 MIME」识别，字段号漂移也能命中
    subs = _submessages(raw, fields)
    item_subs = [(f, inner) for f, inner in subs if _looks_like_item(inner)]
    if not item_subs:
        item_subs = [(f, inner) for f, inner in subs if f.number == 9]
    for _f, inner in item_subs:
        item = Item()
        for f2 in inner:
            item.values[f2.number] = f2.value
            if f2.number == 1 and f2.wire == 2:
                item.mime = _decode(f2.value)
            elif f2.number == 4 and f2.wire == 2:
                item.content = f2.value
            elif f2.number == 10 and f2.wire == 2:
                item.kind = _decode(f2.value)
        note.items.append(item)

    note.summary = texts.get(8, '')
    note.folder = texts.get(10, '')
    note.title = texts.get(14, '')
    note.kind = texts.get(15, '') or 'common'
    note.note_id = texts.get(2, '')

    # 目录：没有 item，且 #9（或记录本身就是字段 1）是目录名
    if not note.items and (group.number == 1 or texts.get(9)):
        note.field_number = 1
        if not note.title:
            note.title = texts.get(9, '')
    else:
        note.field_number = 2
        if not note.note_id:
            for _num, s in texts.items():
                if s and len(s) <= 24 and s.isdigit():
                    note.note_id = s
                    break

    # 时间戳兜底：记录级缺失时用 item 里的时间
    if not note.created or not note.modified:
        for item in note.items:
            for num in (2, 3):
                val = item.values.get(num)
                if isinstance(val, int) and val > 10 ** 12:
                    if not note.created:
                        note.created = val
                    elif not note.modified:
                        note.modified = val
    if not note.note_id:
        note.note_id = 'off-%d' % group.start

    for f in fields:
        if f.wire == 2 and f.number not in _SCALAR_FIELDS:
            note.extra[f.number] = _decode(f.value)
    return note


def load_notes(backup: MiuiBackup) -> Tuple[List[Note], bytes]:
    """从 .bak 中读出全部目录与笔记，返回（笔记列表, _tmp_bak 原始字节）。"""
    raw = backup.read(backup._find_entry('miui_bak/_tmp_bak'))
    root = parse_message(raw)
    groups = _pick_groups(raw, root)
    notes = [_parse_record(raw, g, parse_message(raw, g.start, g.end)) for g in groups]
    return notes, raw

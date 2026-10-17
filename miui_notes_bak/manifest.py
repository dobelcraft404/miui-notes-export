# -*- coding: utf-8 -*-
"""导出清单与「合并去重」支持。

解决的问题：同一个备份包反复导出、或者两个笔记内容重叠的备份包导出到同一个
目录时，会出现多份一模一样的笔记。这里用**内容指纹**判定「这篇笔记是不是已经
导出过了」：

    内容指纹 = SHA1(笔记 ID + 正文内容 + 全部关联附件的内容哈希)
    完整指纹 = SHA1(内容指纹 + 创建时间 + 修改时间 + 类型 + 目录名 + 标题)

**内容指纹刻意不含时间、标题和目录**：把笔记改个标题再备份一次时，笔记还是
同一篇（内容没变），不应该为它再写一份文件；只改标题就重导会让 `名字 (2)` 之类
的副本越积越多。完整指纹则记录了「这一版长什么样」，合并时用它判断要不要原地
刷新元数据。

清单文件是输出目录下的 `export-manifest.json`，纯文本、可读、可以随手删掉
（删掉只会让下次导出重新写一遍，不会丢笔记）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

MANIFEST_NAME = 'export-manifest.json'
MANIFEST_VERSION = 1
FINGERPRINT_TAG = 'n2'                     # 指纹算法版本，换算法时改这里

# 笔记正文里可能带 \r\n、行尾空格，归一化后再算哈希，避免同内容算出两个指纹
_TRAILING_WS_RE = re.compile(rb'[ \t]+\r?\n')
_CRLF_RE = re.compile(rb'\r\n?')
_BAD_BYTES_RE = re.compile(rb'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def normalize_content(content) -> bytes:
    """归一化正文（bytes）：去掉控制字符、统一换行、去掉行尾空白。"""
    if isinstance(content, str):
        raw = content.encode('utf-8')
    else:
        raw = bytes(content or b'')
    raw = _BAD_BYTES_RE.sub(b'', raw)
    raw = _CRLF_RE.sub(b'\n', raw)
    return _TRAILING_WS_RE.sub(b'\n', raw)


def content_fingerprint(note, attachment_hashes: Iterable[str] = ()) -> str:
    """内容指纹：只跟「笔记 ID + 正文 + 关联附件内容」有关。

    刻意不含时间、类型、目录名和标题，原因见模块说明：改了标题的同一篇笔记
    仍然算同一篇。
    """
    payload = '\x1f'.join([
        'id', str(note.note_id or ''),
        'content',
    ]).encode('utf-8')
    parts: List[bytes] = [payload, normalize_content(note.content)]
    for digest in sorted(set(attachment_hashes)):
        parts.append(b'\x1e')
        parts.append(str(digest).encode('utf-8'))
    return hashlib.sha1(b''.join(parts)).hexdigest()


def note_fingerprint(note, attachment_hashes: Iterable[str] = ()) -> str:
    """完整指纹：内容指纹 + 时间 + 类型 + 目录名 + 标题（metadata 变了一定变）。"""
    payload = '\x1f'.join([
        'id', str(note.note_id or ''),
        'created', str(note.created or 0),
        'modified', str(note.modified or 0),
        'kind', str(note.kind or ''),
        'folder', str(note.folder or ''),
        'title', str(note.title or ''),
        'content',
    ]).encode('utf-8')
    parts: List[bytes] = [payload, normalize_content(note.content)]
    for digest in sorted(set(attachment_hashes)):
        parts.append(b'\x1e')
        parts.append(str(digest).encode('utf-8'))
    return hashlib.sha1(b''.join(parts)).hexdigest()


def attachment_hashes(refs: Iterable[str], attachments: Dict[str, bytes]) -> List[str]:
    """把附件引用（相对路径）换算成内容哈希，用于指纹计算。"""
    out = []
    for ref in refs:
        blob = attachments.get(ref)
        if blob is not None:
            out.append(hashlib.sha1(blob).hexdigest())
    return out


def note_keys(note, refs: Iterable[str], attachments: Dict[str, bytes]
              ) -> Tuple[str, str]:
    """一次算出 (内容指纹, 完整指纹)，避免同一条笔记算两遍附件哈希。"""
    hashes = attachment_hashes(refs, attachments)
    return content_fingerprint(note, hashes), note_fingerprint(note, hashes)


@dataclass
class Entry:
    """清单里的一条记录：一篇笔记导出到了哪里。"""

    note_id: str = ''
    folder: str = ''
    title: str = ''
    fingerprint: str = ''                   # 完整指纹（含标题/时间）
    content_key: str = ''                   # 内容指纹（去重、复用目录都用它）
    path: str = ''                          # 相对于输出目录，形如 未分类/标题
    files: List[str] = field(default_factory=list)   # 该笔记产出的文件名
    assets: List[str] = field(default_factory=list)  # note.assets/ 下的文件名

    @classmethod
    def from_dict(cls, data: dict) -> 'Entry':
        fingerprint = str(data.get('fingerprint', ''))
        # 老版本清单只有 fingerprint（含标题），把它当内容指纹读，让旧清单还能用
        content_key = str(data.get('content_key', '')) or fingerprint
        return cls(
            note_id=str(data.get('note_id', '')),
            folder=str(data.get('folder', '')),
            title=str(data.get('title', '')),
            fingerprint=fingerprint,
            content_key=content_key,
            path=str(data.get('path', '')).replace('\\', '/'),
            files=[str(x) for x in data.get('files', [])],
            assets=[str(x) for x in data.get('assets', [])],
        )

    def to_dict(self) -> dict:
        return dict(note_id=self.note_id, folder=self.folder, title=self.title,
                    fingerprint=self.fingerprint, content_key=self.content_key,
                    path=self.path, files=list(self.files), assets=list(self.assets))


class Manifest:
    """输出目录的导出清单，支持按内容指纹查重。

    判定「已导出」有三个条件，缺一不可：① 指纹一致；② 要求的格式文件都在；
    ③ 记录里提到的东西在磁盘上真的存在。任何一条不满足就重新导出，
    这样清单坏了也不会让笔记凭空消失。
    """

    def __init__(self, out_root: str, entries: Optional[Iterable[Entry]] = None):
        self.out_root = out_root
        self.entries: List[Entry] = list(entries or [])
        self._by_fingerprint: Dict[str, Entry] = {}
        self._by_content: Dict[str, Entry] = {}
        self._by_note_id: Dict[str, Entry] = {}
        self._reindex()

    def _reindex(self) -> None:
        """重建索引。空指纹不参与索引，免得一条坏记录顶掉真记录。"""
        self._by_fingerprint = {e.fingerprint: e for e in self.entries if e.fingerprint}
        self._by_content = {}
        for entry in self.entries:
            if entry.content_key:
                self._by_content.setdefault(entry.content_key, entry)
        self._by_note_id = {}
        for entry in self.entries:
            if entry.note_id:
                self._by_note_id.setdefault(entry.note_id, entry)

    # -- 读 / 写 ---------------------------------------------------------
    @property
    def path(self) -> str:
        return os.path.join(self.out_root, MANIFEST_NAME)

    @classmethod
    def load(cls, out_root: str) -> 'Manifest':
        path = os.path.join(out_root, MANIFEST_NAME)
        if not os.path.isfile(path):
            return cls(out_root)
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return cls(out_root)           # 清单损坏就当没有，下次重写
        entries = [Entry.from_dict(x) for x in data.get('entries', []) if isinstance(x, dict)]
        return cls(out_root, entries)

    def save(self) -> None:
        os.makedirs(self.out_root, exist_ok=True)
        data = {
            'version': MANIFEST_VERSION,
            'fingerprint': FINGERPRINT_TAG,
            'out_root': os.path.abspath(self.out_root),
            'notes': len(self.entries),
            'entries': [e.to_dict() for e in sorted(self.entries, key=lambda x: x.path)],
        }
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write('\n')
        os.replace(tmp, self.path)

    # -- 查询 / 登记 -----------------------------------------------------
    def get(self, fingerprint: str) -> Optional[Entry]:
        return self._by_fingerprint.get(fingerprint)

    def get_by_content(self, content_key: str) -> Optional[Entry]:
        return self._by_content.get(content_key)

    def get_by_note_id(self, note_id: str) -> Optional[Entry]:
        return self._by_note_id.get(str(note_id))

    def existing_path(self, content_key: str, formats: Iterable[str],
                      verify: bool = True) -> Optional[str]:
        """若这篇笔记（内容指纹一致）已经导出过且文件都在，返回它的相对目录。"""
        entry = self._by_content.get(content_key)
        if entry is None:
            return None
        if verify and not self._files_present(entry, formats):
            return None
        return entry.path

    def _files_present(self, entry: Entry, formats: Iterable[str]) -> bool:
        if not entry.path:
            return False
        base = os.path.join(self.out_root, *entry.path.split('/'))
        if not os.path.isdir(base):
            return False
        wanted = ['note.%s' % ('md' if f == 'md' else ('docx' if f == 'docx' else 'html'))
                  for f in formats]
        for name in wanted:
            if not os.path.isfile(os.path.join(base, name)):
                return False
        for name in entry.assets:
            if not os.path.isfile(os.path.join(base, 'note.assets', name)):
                return False
        return True

    def record(self, note, content_key: str, rel_dir: str, files: Iterable[str],
               assets: Iterable[str], fingerprint: str = '') -> Entry:
        """登记（或更新）一篇笔记的导出位置。

        内容指纹（content_key）没变、只有标题/时间变了时，沿用原来的目录，
        只把元数据刷新一遍 —— 这样反复导出不会原地多出「标题 (2)」。
        """
        entry = Entry(note_id=str(note.note_id or ''), folder=note.folder or '',
                      title=note.title or '',
                      fingerprint=fingerprint or content_key, content_key=content_key,
                      path=rel_dir.replace('\\', '/'),
                      files=sorted(set(files)), assets=sorted(set(assets)))
        old = self._by_content.get(content_key)
        if old is None and entry.note_id:
            # 同一篇笔记内容变了：沿用旧目录，避免原地多出一份
            stale = self._by_note_id.get(entry.note_id)
            if stale is not None and stale in self.entries:
                entry.path = stale.path
                self.entries[self.entries.index(stale)] = entry
            else:
                self.entries.append(entry)
        elif old is not None and old in self.entries:
            entry.path = old.path                # 沿用旧目录，只刷新标题/时间
            self.entries[self.entries.index(old)] = entry
        else:
            self.entries.append(entry)
        self._reindex()
        return entry

    def prune_missing(self) -> int:
        """丢掉磁盘上已经不存在的记录，返回删掉的条数。"""
        kept = [entry for entry in self.entries if self._entry_alive(entry)]
        removed = len(self.entries) - len(kept)
        self.entries = kept
        self._reindex()
        return removed

    def _entry_alive(self, entry: Entry) -> bool:
        if not entry.path:
            return False
        base = os.path.join(self.out_root, *entry.path.split('/'))
        if not os.path.isdir(base):
            return False
        names = entry.files or ['note.md']
        return any(os.path.isfile(os.path.join(base, name)) for name in names)


# ---------------------------------------------------------------------------
# 目标目录分配：导出和「重建清单」必须用同一套规则，否则重建出的路径对不上
# ---------------------------------------------------------------------------

def allocate_dirs(notes: Iterable, prefer_path: Optional[Dict[str, str]] = None,
                  used: Optional[Dict[str, set]] = None,
                  content_keys: Optional[Dict[str, str]] = None) -> List[Tuple[object, str]]:
    """给每个笔记记录分配相对目录，返回 [(note, '目录/笔记名'), ...]，目录记录跳过。

    同名冲突按 `名字`、`名字 (2)`、`名字 (3)` 依次分配。
    `prefer_path` 是 内容指纹 -> 已有相对目录：同一篇笔记（内容一致）沿用原目录，
    所以改了标题再导出也不会多出一份；
    `used` 是 目录名 -> 已占用的小写名字集合，导出过程可以传进来保持连续。
    """
    if used is None:
        used = {}
    prefer_path = prefer_path or {}
    content_keys = content_keys or {}
    out: List[Tuple[object, str]] = []
    for note in notes:
        if getattr(note, 'is_folder', False):
            continue
        rel_dir = _folder_component(note)
        hold = prefer_path.get(content_keys.get(str(note.note_id), ''))
        if hold:
            hold_dir = hold.rpartition('/')[0]
            out.append((note, hold))
            used.setdefault(hold_dir or rel_dir, set()).add(
                hold.rpartition('/')[2].lower())
            continue
        base = _note_component(note)
        bucket = used.setdefault(rel_dir, set())
        name = base
        seq = 2
        while name.lower() in bucket:
            name = '%s (%d)' % (base, seq)
            seq += 1
        bucket.add(name.lower())
        out.append((note, '%s/%s' % (rel_dir, name)))
    return out


def _folder_component(note) -> str:
    from . import export
    return export.safe_component(note.folder, '未分类') if note.folder else '未分类'


def _note_component(note) -> str:
    from . import export
    return export.safe_component(export.display_name(note),
                                 '未命名笔记-%s' % note.note_id)


def scan_existing(out_root: str) -> Dict[str, set]:
    """扫描输出目录，返回 目录名 -> 已存在的小写笔记名集合（用于避免重名）。"""
    used: Dict[str, set] = {}
    if not os.path.isdir(out_root):
        return used
    for name in sorted(os.listdir(out_root)):
        path = os.path.join(out_root, name)
        if not os.path.isdir(path) or name in ('note.assets', 'raw_attachments'):
            continue
        used[name] = {x.lower() for x in os.listdir(path) if os.path.isdir(os.path.join(path, x))}
    return used

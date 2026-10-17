# -*- coding: utf-8 -*-
"""MIUI 笔记正文（自定义富文本标记）的解析与中间表示。

正文不是标准 XML/HTML，而是小米自有的轻标记，实测出现的形式：

    <text indent="1">一行文字</text>          段落（indent 为缩进层级，0/1/2…）
    <input type="checkbox" indent="1" level="3" checked="true" />   待办项
    <b>粗体</b> <i>斜体</i> <u>下划线</u>
    <br>                                      强制换行
    \\xe2\\x98\\xba <40 位 hash><0/></>         行内图片，hash 指向 miui_att 附件

解析结果是「块」的列表：文本块带若干带样式的片段，图片块带附件名。
Markdown 与 Word 两种输出共用这一层，保证内容一致。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

HASH_RE = re.compile(r'[0-9a-f]{40}')

SPAN_OPEN_RE = re.compile(r'<(b|i|u|strong|em)>', re.I)
SPAN_CLOSE_RE = re.compile(r'</(b|i|u|strong|em)>', re.I)
TEXT_OPEN_RE = re.compile(r'<text\b([^>]*)>', re.I)
TEXT_CLOSE_RE = re.compile(r'</text>', re.I)
INPUT_RE = re.compile(r'<input\b([^>]*)/?>', re.I)
BR_RE = re.compile(r'<br\s*/?>', re.I)
IMG_RE = re.compile(r'\u263a\s*([0-9a-f]{40})\s*<0\s*/>\s*</>')
ATTR_RE = re.compile(r'([A-Za-z_][\w:.-]*)\s*=\s*"([^"]*)"')

# 其余无法识别的尖括号标签一律丢弃，只保留可见文字
JUNK_TAG_RE = re.compile(r'</?[A-Za-z][^<>]*>')


@dataclass
class Span:
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False


@dataclass
class TextBlock:
    spans: List[Span] = field(default_factory=list)
    indent: int = 0
    checkbox: Optional[bool] = None     # None=普通文本；True/False=待办项勾选状态
    level: Optional[int] = None         # 待办项层级

    @property
    def text(self) -> str:
        return ''.join(s.text for s in self.spans)


@dataclass
class ImageBlock:
    ref: str            # 附件名（miui_att 下的文件名，或 data/user/0/... 相对路径）
    caption: str = ''


@dataclass
class NoteDocument:
    blocks: List[object] = field(default_factory=list)
    references: List[str] = field(default_factory=list)   # 正文引用到的附件名（按出现顺序）


def image_ref(hex_name: str, available: Sequence[str]) -> str:
    """附件名可能是 40 位 hash（无扩展名）或 `<hash>.ent`，按实际存在的条目匹配。"""
    if hex_name in available:
        return hex_name
    for cand in (hex_name + '.ent', hex_name + '.jpg', hex_name + '.png', hex_name + '.json'):
        if cand in available:
            return cand
    prefixed = [a for a in available if a.rsplit('/', 1)[-1] == hex_name]
    if prefixed:
        return prefixed[0]
    return hex_name


def _attrs(raw: str) -> dict:
    return {k.lower(): v for k, v in ATTR_RE.findall(raw)}


def _to_int(value: Optional[str], default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _push_spans(target: List[Span], text: str, bold: bool, italic: bool, underline: bool) -> None:
    text = html.unescape(text)
    if not text:
        return
    if target and target[-1].bold == bold and target[-1].italic == italic \
            and target[-1].underline == underline:
        target[-1].text += text
    else:
        target.append(Span(text, bold, italic, underline))


def parse_inline(chunk: str, state=()) -> List[Span]:
    """把一段（不含换行）标记解析成带样式的片段。"""
    bold, italic, underline = state if state else (False, False, False)
    out: List[Span] = []
    buf: List[str] = []
    i = 0
    n = len(chunk)

    def flush() -> None:
        if buf:
            _push_spans(out, ''.join(buf), bold, italic, underline)
            buf.clear()

    while i < n:
        ch = chunk[i]
        if ch == '<':
            end = chunk.find('>', i)
            if end < 0:
                # 未闭合的 '<' 当普通文字，避免丢内容
                buf.append(ch)
                i += 1
                continue
            raw = chunk[i + 1:end]
            low = raw.lower()
            if low in ('b', 'strong'):
                flush()
                bold = True
            elif low in ('/b', '/strong'):
                flush()
                bold = False
            elif low == 'i' or low == 'em':
                flush()
                italic = True
            elif low in ('/i', '/em'):
                flush()
                italic = False
            elif low == 'u':
                flush()
                underline = True
            elif low == '/u':
                flush()
                underline = False
            elif low.startswith('br'):
                flush()
                _push_spans(out, '\n', bold, italic, underline)
            else:
                # 未知标签（text/span/div…）只剥离标签本身
                pass
            i = end + 1
            continue
        buf.append(ch)
        i += 1
    flush()
    return out


def parse_content(content: bytes, available: Sequence[str]) -> NoteDocument:
    """解析一条笔记的正文（bytes）为块列表。"""
    text = content.decode('utf-8', 'replace')
    doc = NoteDocument()
    pos = 0
    indent = 0
    n = len(text)
    buf: List[str] = []

    def emit(seg: str, block_indent: int, checkbox: Optional[bool] = None) -> None:
        spans = parse_inline(seg)
        if spans:
            if checkbox is not None:
                doc.blocks.append(TextBlock(spans=spans, indent=block_indent,
                                            checkbox=checkbox))
                return
            # 片段内的换行（来自 <br>）在这里再拆一层，保证一个块就是一段
            current: List[Span] = []
            for span in spans:
                parts = span.text.split('\n')
                for idx, part in enumerate(parts):
                    if idx > 0:
                        if current:
                            doc.blocks.append(TextBlock(spans=current, indent=block_indent))
                        current = []
                    if part:
                        current.append(Span(part, span.bold, span.italic, span.underline))
            if current:
                doc.blocks.append(TextBlock(spans=current, indent=block_indent))
        elif checkbox is None and seg == '' and doc.blocks \
                and isinstance(doc.blocks[-1], TextBlock):
            # 保留空行（用空文本块表示），便于还原段落间距
            doc.blocks.append(TextBlock(spans=[], indent=block_indent))

    def flush_text() -> None:
        raw = ''.join(buf)
        buf.clear()
        if not raw:
            # 缓冲区为空时不要产出空块：块级标签（<text>/</text>/<input>）与
            # 行内图片标记紧挨着出现时，会在它们之间挤出空块，造成多余空行。
            return
        for seg in raw.split('\n'):
            emit(seg, indent)

    while pos < n:
        m_img = IMG_RE.search(text, pos)
        m_tag = re.search(r'<text\b[^>]*>|</text>|<input\b[^>]*/?>', text[pos:], re.I)
        m_tag_abs = None
        if m_tag:
            m_tag_abs = (pos + m_tag.start(), pos + m_tag.end(), m_tag.group(0))

        use_img = m_img is not None and (m_tag_abs is None or m_img.start() < m_tag_abs[0])
        if use_img:
            buf.append(text[pos:m_img.start()])
            flush_text()
            name = image_ref(m_img.group(1), available)
            doc.blocks.append(ImageBlock(ref=name))
            doc.references.append(name)
            pos = m_img.end()
            continue

        if m_tag_abs is None:
            buf.append(text[pos:])
            pos = n
            continue

        tag = m_tag_abs[2]
        m_prev_img = IMG_RE.search(text, pos, m_tag_abs[0])
        if m_prev_img is not None:
            # 该标签前面还夹着图片标记：先产出图片，本轮只推进到图片起点之前
            buf.append(text[pos:m_prev_img.start()])
            pos = m_prev_img.start()
            continue

        buf.append(text[pos:m_tag_abs[0]])
        low = tag.lower()
        if low.startswith('<text'):
            attrs = _attrs(tag)
            indent = _to_int(attrs.get('indent'), 0)
            flush_text()
            pos = m_tag_abs[1]
        elif low.startswith('</text'):
            flush_text()
            indent = 0
            pos = m_tag_abs[1]
        else:  # <input ...>
            attrs = _attrs(tag)
            flush_text()
            checked = str(attrs.get('checked', '')).lower() == 'true'
            doc.blocks.append(TextBlock(
                spans=[Span('')],
                indent=_to_int(attrs.get('indent'), 0),
                checkbox=checked,
                level=_to_int(attrs.get('level'), 0),
            ))
            pos = m_tag_abs[1]

    buf.append(text[pos:]) if pos < n else None
    flush_text()

    # 去掉首尾空块
    while doc.blocks and isinstance(doc.blocks[0], TextBlock) and not doc.blocks[0].text \
            and doc.blocks[0].checkbox is None:
        doc.blocks.pop(0)
    while doc.blocks and isinstance(doc.blocks[-1], TextBlock) and not doc.blocks[-1].text \
            and doc.blocks[-1].checkbox is None:
        doc.blocks.pop()
    return doc

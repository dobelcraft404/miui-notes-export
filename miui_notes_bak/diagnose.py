# -*- coding: utf-8 -*-
"""`--diagnose`：只输出「备份包的结构长什么样」，不出正文。

换设备 / 换系统版本 / 换笔记版本之后，如果导出的结果不对（少了笔记、图片没出来、
干脆报错），需要先弄清是哪一层和预期不一样：头部字段变了？tar 成员改了？记录字段号
挪了？附件换命名了？

这个模块逐层做统计并给出可疑点。报告里**不含**笔记正文、标题、附件内容——只有长度、
数量、字段号、类型名，所以可以直接贴到 issue 里让人帮忙看。
"""

import os
import sys
from typing import Dict, List, Optional, Sequence

from . import attachments as att_mod
from . import bakfile
from . import content as content_mod
from . import media

#: 已知会用到的记录级字段号（解析器认识的 1..22）
_KNOWN_RECORD_FIELDS = set(range(1, 23))

#: 已知会出现的笔记类型
_KNOWN_KINDS = ('common', 'handwrite', 'multi_handwrite')


def _hist(values: Sequence[str]) -> Dict[str, int]:
    """按出现次数降序的直方图。"""
    out: Dict[str, int] = {}
    for v in values:
        if v:
            out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _field_hist(fields: Sequence[bakfile.Field]) -> Dict[str, int]:
    return _hist(['#%d' % f.number for f in fields])


def _summarize_header(backup: bakfile.MiuiBackup) -> Dict[str, object]:
    lines = list(backup.header_lines)
    version = lines[1] if len(lines) > 1 else ''
    return {
        'lines': len(lines),
        'package': backup.package,
        'bak_version': version,
        'encrypted': backup.encrypted,
        'tar_offset': backup.tar_offset,
    }


def _summarize_tar(backup: bakfile.MiuiBackup) -> Dict[str, object]:
    sizes = backup.member_sizes()
    roots = _hist([name.split('/')[0] + '/' for name in sizes])
    return {
        'count': len(sizes),
        'total_bytes': sum(sizes.values()),
        'roots': roots,
    }


def _summarize_records(backup: bakfile.MiuiBackup) -> Dict[str, object]:
    """解析 _tmp_bak，统计记录结构与字段号分布。"""
    name = backup._find_entry('miui_bak/_tmp_bak')
    raw = backup.read(name)
    root = bakfile.parse_message(raw)
    groups = bakfile._pick_groups(raw, root)

    notes: List[bakfile.Note] = []
    field_hits: List[str] = []
    unknown: Dict[str, int] = {}
    failed = 0
    for group in groups:
        try:
            fields = bakfile.parse_message(raw, group.start, group.end)
            note = bakfile._parse_record(raw, group, fields)
        except Exception:
            failed += 1
            continue
        notes.append(note)
        for f in fields:
            field_hits.append('#%d' % f.number)
            if f.number not in _KNOWN_RECORD_FIELDS:
                key = '#%d' % f.number
                unknown[key] = unknown.get(key, 0) + 1

    notes_only = [n for n in notes if not n.is_folder]
    contents = [n.content for n in notes_only]
    missing_ids = [n for n in notes_only if n.note_id.startswith('off-')]
    missing_ts = [n for n in notes_only if not n.created or not n.modified]
    unknown_kinds = sorted({n.kind for n in notes_only if n.kind not in _KNOWN_KINDS})

    return {
        'entry': name,
        'raw_size': len(raw),
        'top_fields': _field_hist(root),
        'total': len(groups),
        'parsed': len(notes),
        'failed': failed,
        'folders': len(notes) - len(notes_only),
        'notes': len(notes_only),
        'kinds': _hist([n.kind for n in notes_only]),
        'mimes': _hist([i.mime for n in notes_only for i in n.items]),
        'record_fields': _hist(field_hits),
        'unknown_fields': unknown,
        'content_bytes': sum(len(c) for c in contents),
        'content_max': max((len(c) for c in contents), default=0),
        'missing_ids': len(missing_ids),
        'missing_timestamps': len(missing_ts),
        'unknown_kinds': unknown_kinds,
        'notes_parsed': notes,
        'raw': raw,
    }


def _summarize_attachments(backup: bakfile.MiuiBackup) -> Dict[str, object]:
    files = backup.attachments()
    types: List[str] = []
    unknown: List[str] = []
    for rel, blob in files.items():
        kind, _ext = media.detect(blob)
        types.append(kind)
        if kind == 'application/octet-stream':
            unknown.append(rel)
    return {
        'count': len(files),
        'total_bytes': sum(len(b) for b in files.values()),
        'types': _hist(types),
        'unknown': sorted(unknown),
        'files': files,
    }


def _summarize_refs(notes: Sequence[bakfile.Note], raw: bytes,
                    files: Dict[str, bytes]) -> Dict[str, object]:
    """统计笔记引用了多少附件、其中多少找不到。"""
    available = list(files)
    resolver = att_mod.Resolver(available)
    total = 0
    missing: List[str] = []
    for note in notes:
        if note.is_folder:
            continue
        try:
            doc = content_mod.parse_content(note.content, available)
            refs = att_mod.collect_refs(note, doc, resolver, raw)
        except Exception:
            continue
        for ref in refs:
            total += 1
            if resolver(ref) is None:
                missing.append(ref)
    return {'total': total, 'missing': missing}


def diagnose(bak_path: str) -> Dict[str, object]:
    """跑一遍诊断，返回报告数据（结构化，便于测试）。"""
    report: Dict[str, object] = {
        'path': os.path.abspath(bak_path),
        'size': os.path.getsize(bak_path),
        'errors': [],
        'warnings': [],
        'header': {},
        'tar': {},
        'records': {},
        'attachments': {},
        'refs': {},
    }
    errors: List[str] = report['errors']          # type: ignore[assignment]
    warnings: List[str] = report['warnings']      # type: ignore[assignment]

    try:
        backup = bakfile.MiuiBackup(bak_path)
    except Exception as exc:
        errors.append('打开备份失败：%s: %s' % (type(exc).__name__, exc))
        return report

    try:
        for label, fn, key in (
                ('文本头', _summarize_header, 'header'),
                ('tar 归档', _summarize_tar, 'tar'),
                ('记录', _summarize_records, 'records'),
                ('附件', _summarize_attachments, 'attachments')):
            try:
                report[key] = fn(backup)  # type: ignore[operator]
            except Exception as exc:
                errors.append('%s解析失败：%s: %s' % (label, type(exc).__name__, exc))
    finally:
        backup.close()

    header = report['header']                         # type: ignore[assignment]
    records = report['records']                       # type: ignore[assignment]
    attachments = report['attachments']               # type: ignore[assignment]

    if header.get('encrypted'):
        errors.append('这个备份带密码（头部加密标志为 1），本工具无法离线解析')
    if records:
        try:
            report['refs'] = _summarize_refs(records.get('notes_parsed', []),
                                             records.get('raw', b''),
                                             attachments.get('files', {}))
        except Exception as exc:
            errors.append('引用统计失败：%s: %s' % (type(exc).__name__, exc))

    if records:
        if not records.get('total'):
            errors.append('没有解析出任何记录（_tmp_bak 可能换了结构）')
        if records.get('failed'):
            warnings.append('有 %d 条记录解析抛出异常' % records['failed'])
        if records.get('missing_ids'):
            warnings.append('有 %d 篇笔记没有读到 ID（已用偏移量兜底命名）'
                            % records['missing_ids'])
        if records.get('missing_timestamps'):
            warnings.append('有 %d 篇笔记的时间戳为空（时间列会显示成 1970 年）'
                            % records['missing_timestamps'])
        if records.get('unknown_fields'):
            warnings.append('出现了没见过的记录字段号：%s'
                            % ', '.join(sorted(records['unknown_fields'])))
        if records.get('unknown_kinds'):
            warnings.append('出现了没见过的笔记类型：%s'
                            % ', '.join(records['unknown_kinds']))
    if attachments.get('unknown'):
        warnings.append('有 %d 个附件认不出类型（会按 .bin 原样保留）：%s'
                        % (len(attachments['unknown']), ', '.join(attachments['unknown'][:5])))
    refs = report['refs']                            # type: ignore[assignment]
    if refs.get('missing'):
        warnings.append('正文引用了 %d 个找不到的附件' % len(refs['missing']))
    return report


def format_report(report: Dict[str, object]) -> str:
    """把报告数据渲染成可以直接贴到 issue 里的中文文本。"""
    lines: List[str] = []
    add = lines.append

    add('== 小米笔记备份诊断 ==')
    add('文件：%s（%s）' % (report.get('path'), media.human_size(int(report.get('size') or 0))))
    errors = report.get('errors') or []
    warnings = report.get('warnings') or []
    if errors:
        add('')
        add('[0] 错误')
        for err in errors:                            # type: ignore[union-attr]
            add('  - %s' % err)

    header = report.get('header') or {}
    if header:
        add('')
        add('[1] 文本头')
        add('  行数：%s' % header.get('lines'))
        add('  包名：%s' % header.get('package'))
        add('  bak 版本：%s' % header.get('bak_version'))
        add('  加密：%s' % ('是' if header.get('encrypted') else '否'))
        add('  tar 起点：%s' % header.get('tar_offset'))

    tar = report.get('tar') or {}
    if tar:
        add('')
        add('[2] tar 归档')
        add('  成员：%s 个，合计 %s' % (tar.get('count'),
                                      media.human_size(int(tar.get('total_bytes') or 0))))
        roots = tar.get('roots') or {}
        for name, count in roots.items():             # type: ignore[union-attr]
            add('  %s %d 项' % (name, count))

    records = report.get('records') or {}
    if records:
        add('')
        add('[3] 记录（%s）' % records.get('entry'))
        add('  条目大小：%s' % media.human_size(int(records.get('raw_size') or 0)))
        add('  顶层字段：%s' % _fmt_hist(records.get('top_fields') or {}))
        add('  记录：%s 条（目录 %s / 笔记 %s）'
            % (records.get('total'), records.get('folders'), records.get('notes')))
        add('  类型：%s' % _fmt_hist(records.get('kinds') or {}))
        add('  MIME：%s' % _fmt_hist(records.get('mimes') or {}))
        add('  记录字段号：%s' % _fmt_hist(records.get('record_fields') or {}))
        add('  未识别字段：%s' % _fmt_hist(records.get('unknown_fields') or {}))
        add('  正文：%s，最长 %s'
            % (media.human_size(int(records.get('content_bytes') or 0)),
               media.human_size(int(records.get('content_max') or 0))))
        add('  缺 ID：%s 条；缺时间戳：%s 条'
            % (records.get('missing_ids'), records.get('missing_timestamps')))

    atts = report.get('attachments') or {}
    if atts:
        add('')
        add('[4] 附件')
        add('  数量：%s 个，合计 %s' % (atts.get('count'),
                                      media.human_size(int(atts.get('total_bytes') or 0))))
        add('  类型：%s' % _fmt_hist(atts.get('types') or {}))

    refs = report.get('refs') or {}
    if refs:
        add('')
        add('[5] 引用')
        add('  笔记引用的附件：%s 处，其中找不到的：%s 处'
            % (refs.get('total'), len(refs.get('missing') or [])))

    add('')
    add('[6] 结论')
    if errors:
        add('  解析在中途失败了，上面的错误信息是定位的起点。')
    elif warnings:
        add('  能读出来，但有几处和预期不完全一样：')
        for warn in warnings:                         # type: ignore[union-attr]
            add('  - %s' % warn)
    else:
        add('  没有发现异常。')
    add('')
    add('提示：这份报告不含任何笔记正文、标题或附件内容，可以直接贴出来求助。')
    return '\n'.join(lines)


def _fmt_hist(hist: Dict[str, int]) -> str:
    if not hist:
        return '无'
    parts = ['%s ×%d' % (k, v) for k, v in hist.items()]
    return '  '.join(parts)


def print_report(report: Dict[str, object], stream=None) -> None:
    out = stream or sys.stdout
    out.write(format_report(report) + '\n')

# -*- coding: utf-8 -*-
"""对解析与导出逻辑做可重复的断言检查（跑在真实备份上，只读）。

    python tests/test_export.py [备份文件路径]

退出码 0 表示全部通过。不打印任何笔记正文，只打印统计数字。
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from miui_notes_bak import attachments as att_mod          # noqa: E402
from miui_notes_bak import bakfile, content, cli, export, media  # noqa: E402

DEFAULT_BAK = os.path.join(ROOT, '_work', '笔记(com.miui.notes).bak')

FAILURES = []


def find_source() -> str:
    """找一个真实备份：命令行参数 > _work 里的 .bak > 仓库根目录的 .bak/.zip。"""
    if len(sys.argv) > 1:
        return sys.argv[1]
    if os.path.exists(DEFAULT_BAK):
        return DEFAULT_BAK
    for name in sorted(os.listdir(ROOT)):
        if name.lower().endswith(('.bak', '.zip')):
            return os.path.join(ROOT, name)
    return ''


def check(condition, message):
    if condition:
        print('  [通过] %s' % message)
    else:
        print('  [失败] %s' % message)
        FAILURES.append(message)


def main() -> int:
    source = find_source()
    if not source:
        print('跳过：没有找到真实备份。')
        print('本脚本用于在你自己的 MIUI 备份上做端到端校验（只读，不会修改备份）。')
        print('用法：python tests/test_export.py "备份包.zip" 或 "笔记(com.miui.notes).bak"')
        return 0
    print('备份来源：%s' % source)

    print('\n== 1. 容器与 protobuf 解析 ==')
    # 注意：source 是 zip 时，prepare_input 会把 .bak 解到临时目录，cleanup 就是那个目录；
    # 必须留到全部步骤跑完再删（第 4 步还要用 bak_path 重新读一遍）。
    bak_path, cleanup = cli.prepare_input(source)
    bak = bakfile.MiuiBackup(bak_path)
    try:
        notes, raw = bakfile.load_notes(bak)
        attachments = bak.attachments()
        check(bak.header_lines[0] == 'MIUI BACKUP', '魔数正确')
        check(bak.tar_offset == 65, 'tar 起始偏移为 65')
        check(len(notes) > 0, '读到 %d 条记录' % len(notes))
        folders = [n for n in notes if n.is_folder]
        others = [n for n in notes if not n.is_folder]
        print('  目录 %d 个（目录是可选的，没有目录时全部归入「未分类」）' % len(folders))
        check(all(n.title for n in folders), '目录名均非空')
        check(all(n.note_id for n in others), '笔记 ID 均非空')
        check(all(n.created > 0 for n in others), '笔记创建时间均有效')
        check(all(n.kind in ('common', 'handwrite', 'multi_handwrite') for n in others),
              '笔记类型取值合法')
        print('  附件 %d 个' % len(attachments))
    finally:
        bak.close()

    print('\n== 2. 正文解析 ==')
    resolver = att_mod.Resolver(sorted(attachments))
    image_refs = 0
    for note in others:
        doc = content.parse_content(note.content, list(attachments))
        image_refs += sum(1 for b in doc.blocks if isinstance(b, content.ImageBlock))
        for ref in doc.references:
            if ref not in attachments:
                FAILURES.append('笔记 %s 引用了不存在的附件 %s' % (note.note_id, ref))
        for block in doc.blocks:
            if isinstance(block, content.TextBlock):
                for span in block.spans:
                    if '<' in span.text and '>' in span.text:
                        FAILURES.append('笔记 %s 的正文仍残留尖括号标记' % note.note_id)
    check(not [f for f in FAILURES if '尖括号' in f], '正文标记已全部剥离')
    print('  解析出行内图片 %d 张' % image_refs)
    check(not [f for f in FAILURES if '不存在的附件' in f], '正文引用的附件全部存在')

    print('\n== 3. 附件类型识别 ==')
    kinds = {}
    for blob in attachments.values():
        name, ext = media.detect(blob)
        kinds[name] = kinds.get(name, 0) + 1
    print('  ' + '，'.join('%s %d 个' % (k, v) for k, v in sorted(kinds.items())))
    check(kinds.get('application/octet-stream', 0) == 0, '不存在无法识别的附件')

    print('\n== 4. 全量导出 ==')
    out = tempfile.mkdtemp(prefix='miui-notes-verify-')
    exporter = cli.Exporter(out, ['md', 'docx', 'html'], keep_raw=False,
                            copy_unreferenced=True, index_csv=True)
    stat = exporter.run(bak_path)
    check(stat['notes'] == len(others), '导出笔记篇数 %d' % stat['notes'])
    check(stat['md'] == stat['notes'] and stat['docx'] == stat['notes']
          and stat['html'] == stat['notes'],
          'Markdown/Word/HTML 各 %d 份' % stat['md'])
    check(stat['missing'] == 0, '正文缺失引用 0')
    print('  未被任何笔记引用的附件：%d 个' % stat['unreferenced'])

    # 三个多页手写笔记各有一个同名 multi_page_ent.ent，若按文件名匹配会全部指向同一个。
    twin = [n for n in others if n.kind == 'multi_handwrite']
    if twin:
        seen_ent = {}
        for note in twin:
            doc = content.parse_content(note.content, list(attachments))
            refs = att_mod.collect_refs(note, doc, resolver, raw)
            for r in refs:
                if r.endswith('multi_page_ent.ent'):
                    seen_ent.setdefault(r, []).append(note.note_id)
        check(len(seen_ent) == len(twin),
              '多页手写 %d 篇各指向自己的 multi_page_ent.ent（实际 %d 份）'
              % (len(twin), len(seen_ent)))
    check(os.path.exists(os.path.join(out, 'index.csv')), '生成 index.csv')

    md_files, docx_files, html_files, broken = [], [], [], []
    for root, _dirs, files in os.walk(out):
        for f in files:
            p = os.path.join(root, f)
            if f == 'note.md':
                md_files.append(p)
            elif f == 'note.docx':
                docx_files.append(p)
            elif f == 'note.html':
                html_files.append(p)
    check(len(md_files) == stat['notes'], '磁盘上 note.md %d 份' % len(md_files))
    check(len(docx_files) == stat['notes'], '磁盘上 note.docx %d 份' % len(docx_files))
    check(len(html_files) == stat['notes'], '磁盘上 note.html %d 份' % len(html_files))

    img_re = re.compile(r'!\[[^\]]*\]\(([^)]+)\)')
    total_img = 0
    for p in md_files:
        text = open(p, encoding='utf-8').read()
        if not text.startswith('# '):
            broken.append('缺少标题：%s' % p)
        for link in img_re.findall(text):
            total_img += 1
            target = os.path.normpath(os.path.join(os.path.dirname(p),
                                                   link.replace('/', os.sep)))
            if not os.path.exists(target):
                broken.append('图片链接失效：%s -> %s' % (p, link))
    for p in docx_files:
        try:
            with zipfile.ZipFile(p) as zf:
                if 'word/document.xml' not in zf.namelist() or zf.testzip() is not None:
                    broken.append('docx 结构异常：%s' % p)
        except Exception as exc:
            broken.append('docx 打不开：%s (%r)' % (p, exc))
    check(not broken, '全部 markdown 图片链接有效、全部 docx 可打开')
    for line in broken[:5]:
        print('    ', line)
    print('  markdown 中共 %d 处图片' % total_img)

    print('\n== 4b. HTML 输出 ==')
    html_img = 0
    html_broken = []
    src_re = re.compile(r'(?:src|href)="([^"]+)"')
    for p in html_files:
        raw = open(p, encoding='utf-8').read()
        if not raw.lstrip().lower().startswith('<!doctype html>'):
            html_broken.append('缺少 doctype：%s' % p)
        if 'charset="utf-8"' not in raw:
            html_broken.append('未声明 utf-8：%s' % p)
        for val in src_re.findall(raw):
            if val.startswith(('http://', 'https://', 'data:', '#')):
                continue
            if val.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                html_img += 1
            target = os.path.normpath(os.path.join(os.path.dirname(p),
                                                   val.replace('/', os.sep)))
            if not os.path.exists(target):
                html_broken.append('%s 引用了不存在的文件：%s' % (p, val))
    check(not html_broken, '全部 HTML 结构完整、引用文件都存在')
    for line in html_broken[:5]:
        print('    ', line)
    check(html_img == total_img, 'HTML 图片 %d 张，与 markdown 一致' % html_img)

    index_path = os.path.join(out, 'index.html')
    check(os.path.exists(index_path), '生成 index.html 目录页')
    index_raw = open(index_path, encoding='utf-8').read()
    index_links = [v for v in src_re.findall(index_raw) if not v.startswith(('http', 'data:', '#'))]
    index_missing = [v for v in index_links
                     if not os.path.exists(os.path.normpath(
                         os.path.join(out, v.replace('/', os.sep))))]
    check(not index_missing and len(index_links) >= stat['notes'],
          'index.html 的 %d 个链接全部有效' % len(index_links))
    for v in index_missing[:5]:
        print('     index.html -> %s' % v)

    print('\n== 5. Word 内嵌图片 ==')
    media_count = 0
    for p in docx_files:
        with zipfile.ZipFile(p) as zf:
            media_count += sum(1 for n in zf.namelist() if n.startswith('word/media/'))
    check(media_count == total_img, 'docx 内嵌图片 %d 张，与 markdown 一致' % media_count)

    import shutil
    shutil.rmtree(out, ignore_errors=True)
    if cleanup:
        shutil.rmtree(cleanup, ignore_errors=True)
    print('\n== 结果 ==')
    if FAILURES:
        print('失败 %d 项：' % len(FAILURES))
        for f in FAILURES:
            print('  - %s' % f)
        return 1
    print('全部检查通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

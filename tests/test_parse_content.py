# -*- coding: utf-8 -*-
"""针对 `parse_content` 的死循环回归测试（不读真实备份，纯合成数据）。

历史 bug：`<input ...>` 这种「块级标签」前面紧跟一个行内图片标记时，
旧代码判定“没有图片在前面”才处理标签，结果两条分支都不满足，
pos 原地不动 → while 循环无限跑、buf 无限增长直至内存爆掉/进程假死。

    python tests/test_parse_content.py

退出码 0 表示通过。用超时兜底：真的死循环时整个脚本会被外部超时杀掉。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from miui_notes_bak import attachments as att_mod  # noqa: E402
from miui_notes_bak import content  # noqa: E402

HASH_A = '32cbc4587467a266977f5eeeb291bb99132aff16'
HASH_B = '1e42b214c3d0aa6f5dd2e0b0f5b7a9c8e1d2f3a4'

FAILURES = []


def check(cond, msg):
    print('  [%s] %s' % ('通过' if cond else '失败', msg))
    if not cond:
        FAILURES.append(msg)


def parse(blob: str, available=()):
    return content.parse_content(blob.encode('utf-8'), list(available))


def kinds(doc):
    return [type(b).__name__ for b in doc.blocks]


def main() -> int:
    img = '\u263a %s<0/></>' % HASH_A

    print('== 1. 图片标记紧跟 <input>（历史死循环点）==')
    doc = parse('<text indent="1">%s<input type="checkbox" indent="1" checked="true" />'
                '待办事项</text>' % img, [HASH_A])
    check(kinds(doc) == ['ImageBlock', 'TextBlock', 'TextBlock'], '块序列 %s' % kinds(doc))
    check(doc.blocks[0].ref == HASH_A, '图片引用正确')
    check(doc.blocks[1].checkbox is True, '待办项勾选状态正确')
    check(doc.blocks[2].text == '待办事项', '待办项文字正确')
    check(doc.references == [HASH_A], 'references 记录正确')

    print('== 2. 图片标记紧跟 </text> ==')
    doc = parse('<text indent="1">前文%s</text>后文' % img, [HASH_A])
    check(kinds(doc) == ['TextBlock', 'ImageBlock', 'TextBlock'], '块序列 %s' % kinds(doc))
    check(doc.blocks[0].text == '前文', '图片前文字正确')
    check(doc.blocks[2].text == '后文', '图片后文字正确')

    print('== 3. 图片标记紧跟 <text ...> ==')
    doc = parse('%s<text indent="2">说明文字</text>' % img, [HASH_A])
    check(kinds(doc) == ['ImageBlock', 'TextBlock'], '块序列 %s' % kinds(doc))
    check(doc.blocks[1].indent == 2, '缩进取自 indent 属性')

    print('== 4. 一张图夹在两个标签之间 ==')
    doc = parse('<text indent="0">A</text>%s<text indent="1">B</text>' % img, [HASH_A])
    check(kinds(doc) == ['TextBlock', 'ImageBlock', 'TextBlock'], '块序列 %s' % kinds(doc))
    check([b.text for b in doc.blocks if isinstance(b, content.TextBlock)] == ['A', 'B'],
          '标签文字顺序正确')

    print('== 5. 连续多张图片 ==')
    doc = parse('%s%s' % (img, '\u263a %s<0/></>' % HASH_B), [HASH_A, HASH_B])
    check(kinds(doc) == ['ImageBlock', 'ImageBlock'], '块序列 %s' % kinds(doc))
    check(doc.references == [HASH_A, HASH_B], '两张图都记录在 references')

    print('== 6. 常规富文本仍正常 ==')
    doc = parse('<text indent="1"><b>粗</b>普通<i>斜</i><br>换行</text>')
    check(kinds(doc) == ['TextBlock', 'TextBlock'], 'br 拆成两块')
    bold = [s for s in doc.blocks[0].spans if s.bold]
    italic = [s for s in doc.blocks[0].spans if s.italic]
    check(len(bold) == 1 and bold[0].text == '粗', '粗体片段正确')
    check(len(italic) == 1 and italic[0].text == '斜', '斜体片段正确')

    print('== 7. 合成备份端到端：手写缩略图与图片不被重复 ==')
    import shutil
    import tempfile
    from miui_notes_bak import bakfile, cli, media
    import make_synthetic_bak

    tmp = tempfile.mkdtemp(prefix='miui-notes-syn-')
    try:
        bak_path = make_synthetic_bak.build_bak(os.path.join(tmp, 'synthetic.bak'))
        out = os.path.join(tmp, 'out')
        exporter = cli.Exporter(out, ['md', 'docx'], keep_raw=False,
                                copy_unreferenced=True, index_csv=True)
        stat = exporter.run(bak_path)
        check(stat['notes'] == 4 and stat['folders'] == 2, '导出 %d 篇 / 目录 %d 个'
              % (stat['notes'], stat['folders']))
        check(stat['missing'] == 0 and stat['unreferenced'] == 0,
              '没有缺失引用，全部附件都被引用')

        with bakfile.MiuiBackup(bak_path) as bak:
            notes, _raw = bakfile.load_notes(bak)
            atts = bak.attachments()
        resolver = att_mod.Resolver(sorted(atts))
        hand = [n for n in notes if n.kind == 'handwrite'][0]
        doc = content.parse_content(hand.content, list(atts))
        refs = att_mod.collect_refs(hand, doc, resolver, _raw)
        check(len(refs) == 3, '手写笔记引用 %d 个附件（笔迹/识别/缩略图）' % len(refs))

        # 手写缩略图必须作为图片块出现在正文最前面
        note_md = os.path.join(out, '未分类', '手写笔记', 'note.md')
        text = open(note_md, encoding='utf-8').read()
        check(text.count('![') == 1, '手写笔记 markdown 只有 1 张图')
        thumb = [r for r in refs if media.is_image(atts[r])]
        check(len(thumb) == 1, '手写笔记的缩略图被识别为图片')
        check(os.path.exists(os.path.join(out, '未分类', '手写笔记', 'note.assets',
                                         media.safe_name(thumb[0]) + '.jpg')),
              '缩略图落盘')

        # 正文里有图的笔记：图片不能被复制到开头（避免重复渲染）
        note_md = os.path.join(out, '测试目录', '图片笔记', 'note.md')
        text = open(note_md, encoding='utf-8').read()
        check(text.count('![') == 1, '正文图片笔记 markdown 只有 1 张图')
        check(text.index('看图') < text.index('!['), '图片保持在正文中的位置')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

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

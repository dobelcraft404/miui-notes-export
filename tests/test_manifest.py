# -*- coding: utf-8 -*-
"""合并去重语义的可重复断言检查（纯合成数据，不读真实备份）。

    python tests/test_manifest.py

覆盖：反复导出不产生第二份、只改标题仍算同一篇、只改内容原地覆盖、
关掉合并才允许重复、清单缺失时按 note.md 正文认领旧目录、`--rebuild-index` 重建。
退出码 0 表示全部通过。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'tests'))

from miui_notes_bak import MiuiBackup, cli, load_notes, manifest   # noqa: E402
from make_synthetic_bak import build_bak                          # noqa: E402

FORMATS = ['md', 'docx', 'html']
FAILURES = []


def check(condition, message):
    if condition:
        print('  [通过] %s' % message)
    else:
        print('  [失败] %s' % message)
        FAILURES.append(message)


def count_files(root):
    return sum(len(fs) for _, _, fs in os.walk(root))


def note_dirs(root):
    """磁盘上真正有 note.md 的笔记目录数。"""
    return len([d for d, _, _ in os.walk(root)
                if os.path.isfile(os.path.join(d, 'note.md'))])


def run_export(out, bak, merge=True):
    return cli.Exporter(out, FORMATS, False, False, True, merge=merge).run(bak)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix='merge-check-')
    try:
        bak = build_bak(os.path.join(tmp, 'synthetic.bak'))
        # 内容与 bak 完全一样，只是标题/目录名整体变了，用来模拟「笔记被改过标题」
        bak2 = build_bak(os.path.join(tmp, 'synthetic2.bak'), title_suffix='（改过）')

        print('== 1. 首次导出 ==')
        out = os.path.join(tmp, 'out')
        st1 = run_export(out, bak)
        check(st1['notes'] == 4, '首次导出 4 篇（实际 %d）' % st1['notes'])
        check(st1['skipped'] == 0, '首次没有跳过（实际 %d）' % st1['skipped'])
        check(os.path.isfile(os.path.join(out, manifest.MANIFEST_NAME)), '生成了清单文件')
        check(len(manifest.Manifest.load(out).entries) == 4, '清单里 4 条')
        n_files = count_files(out)

        print('== 2. 同样内容再导出一次（应全部跳过）==')
        st2 = run_export(out, bak)
        check(st2['skipped'] == 4, '全部 4 篇被跳过（实际 %d）' % st2['skipped'])
        check(st2['notes'] == 0, '没有新导出（实际 %d）' % st2['notes'])
        check(count_files(out) == n_files, '文件数没有增加（%d）' % n_files)
        check(st2['indexed'] == 4, '目录页仍收录 4 篇（实际 %d）' % st2['indexed'])

        print('== 3. 只改标题后重导：算同一篇，沿用原目录 ==')
        out2 = os.path.join(tmp, 'out2')
        st3a = run_export(out2, bak)
        check(st3a['notes'] == 4 and st3a['skipped'] == 0, '干净目录首次导出 4 篇')
        st3b = run_export(out2, bak2)
        check(st3b['skipped'] == 4, '4 篇都按同一篇跳过（实际 %d）' % st3b['skipped'])
        check(st3b['notes'] == 0, '没有重写任何一篇（实际 %d）' % st3b['notes'])
        check(note_dirs(out2) == 4, '仍然是 4 个笔记目录（实际 %d）' % note_dirs(out2))
        mf3 = manifest.Manifest.load(out2)
        check(len(mf3.entries) == 4, '清单仍然是 4 条（实际 %d）' % len(mf3.entries))
        check(len({e.path for e in mf3.entries}) == 4, '4 条指向 4 个不同目录')

        print('== 4. 关掉合并才会重复导出 ==')
        out4 = os.path.join(tmp, 'out4')
        run_export(out4, bak)
        before = count_files(out4)
        st4 = run_export(out4, bak, merge=False)
        check(st4['notes'] == 4, '又导出了 4 篇（实际 %d）' % st4['notes'])
        check(count_files(out4) > before, '文件数确实增加了（%d -> %d）'
              % (before, count_files(out4)))
        check(note_dirs(out4) == 8, '磁盘上出现 8 个笔记目录（实际 %d）' % note_dirs(out4))

        print('== 5. 指纹：内容相同 -> 相同；改一个字 -> 不同 ==')
        with MiuiBackup(bak) as obj:
            notes, _raw = load_notes(obj)
        real = [n for n in notes if not n.is_folder]
        fp = [manifest.content_fingerprint(n, ['a' * 40]) for n in real]
        check(len(set(fp)) == len(fp), '不同笔记内容指纹互不相同')
        check(fp == [manifest.content_fingerprint(n, ['a' * 40]) for n in real],
              '同一笔记两次算出的内容指纹一致')
        check(fp != [manifest.content_fingerprint(n, ['b' * 40]) for n in real],
              '附件变化会改变内容指纹')
        fake = type('FakeNote', (), {})()
        src = real[0]
        for attr in ('note_id', 'created', 'modified', 'kind', 'folder', 'title'):
            setattr(fake, attr, getattr(src, attr))
        fake.content = src.content
        check(manifest.content_fingerprint(fake) == manifest.content_fingerprint(src),
              '构造同内容笔记得到相同内容指纹')
        fake.title = '换个标题'
        check(manifest.content_fingerprint(fake) == manifest.content_fingerprint(src),
              '只改标题不影响内容指纹（判断「还是同一篇」）')
        check(manifest.note_fingerprint(fake) != manifest.note_fingerprint(src),
              '只改标题会改变完整指纹（元数据要刷新）')
        fake.content = (src.content or b'') + b'x'
        check(manifest.content_fingerprint(fake) != manifest.content_fingerprint(src),
              '正文变化会改变内容指纹')

        print('== 6. --rebuild-index 重建清单（清单还在）==')
        st6 = cli.rebuild_manifest(bak, out, FORMATS)
        check(st6['entries'] == 4, '登记 4 篇（实际 %d）' % st6['entries'])
        mf6 = manifest.Manifest.load(out)
        check(len({e.path for e in mf6.entries}) == 4, '重建后没有重复目录')
        check(all(e.content_key for e in mf6.entries), '重建后的记录都带内容指纹')
        check(all(' (2)' not in e.path for e in mf6.entries), '登记的目录名都不带「(2)」')

        print('== 7. 清单被删 + 备份里改了标题：仍按 note.md 正文认领旧目录 ==')
        out7 = os.path.join(tmp, 'out7')
        run_export(out7, bak)
        os.remove(os.path.join(out7, manifest.MANIFEST_NAME))
        run_export(out7, bak2)
        st7 = cli.rebuild_manifest(bak2, out7, FORMATS)
        check(st7['entries'] == 4, '改名后仍然登记 4 篇（实际 %d）' % st7['entries'])
        mf7 = manifest.Manifest.load(out7)
        check(len({e.path for e in mf7.entries}) == 4, '4 条记录指向 4 个不同目录')
        check(all(os.path.isfile(os.path.join(out7, *e.path.split('/'), 'note.md'))
                  for e in mf7.entries), '登记的每个目录里都真的有 note.md')
        check(st7.get('renamed', 0) <= 4, '按正文认领的次数不超过 4（实际 %d）'
              % st7.get('renamed', 0))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print('结果：失败 %d 项：%s' % (len(FAILURES), FAILURES))
        return 1
    print('结果：全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())

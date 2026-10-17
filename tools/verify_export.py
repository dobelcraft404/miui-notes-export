# -*- coding: utf-8 -*-
"""导出结果自检：每个 note.assets 里的文件是否都被 note.md 提到过。

用法：

    python tools/verify_export.py <导出目录> [-v]

默认只打印汇总（导出目录里可能包含私人笔记标题，逐篇输出不适合直接贴出去）。
退出码：0 = 没问题，1 = 有问题。
"""

from __future__ import annotations

import os
import re
import sys

IMG_RE = re.compile(r'!\[[^\]]*\]\(([^)]+)\)')
ATT_RE = re.compile(r'^- 附件文件：`([^`]+)`', re.M)


def main() -> int:
    if len(sys.argv) < 2:
        print('用法：python tools/verify_export.py <导出目录> [-v]')
        return 2
    out = sys.argv[1]
    verbose = '-v' in sys.argv[2:] or '--verbose' in sys.argv[2:]
    if not os.path.isdir(out):
        print('不是目录：%s' % out)
        return 2

    mds = []
    for root, _dirs, files in os.walk(out):
        for name in files:
            if name == 'note.md':
                mds.append(os.path.join(root, name))
    mds.sort()
    if not mds:
        print('没找到 note.md（是不是只导出了 docx/html？）')
        return 2
    print('note.md 共 %d 份' % len(mds))
    if verbose:
        print('每篇：图片块数 / 附件条目数 / 正文行数')

    missing = []
    docx_pairs = 0
    for path in mds:
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        links = set(IMG_RE.findall(text))
        listed = set(ATT_RE.findall(text))
        base = os.path.dirname(path)

        if os.path.exists(os.path.join(base, 'note.docx')):
            docx_pairs += 1

        if verbose:
            body_lines = len([line for line in text.splitlines()
                              if line and not line.startswith(('#', '-', '> ', '!'))])
            rel = os.path.relpath(path, out)
            print('  %-52s 图 %2d  附件 %2d  正文行 %3d'
                  % (rel, len(links), len(listed), body_lines))

        assets = os.path.join(base, 'note.assets')
        if not os.path.isdir(assets):
            continue
        for root, _dirs, files in os.walk(assets):
            for name in sorted(files):
                rel = os.path.relpath(os.path.join(root, name), base).replace('\\', '/')
                if rel not in links and rel not in listed:
                    missing.append((os.path.relpath(path, out), rel))

    print('未被 markdown 提到（图片链接或附件清单）的资产文件：%d' % len(missing))
    for page, ref in missing[:10]:
        print('  -', page, '->', ref)
    print('note.md 与 note.docx 配对：%d/%d' % (docx_pairs, len(mds)))
    return 1 if missing else 0


if __name__ == '__main__':
    raise SystemExit(main())

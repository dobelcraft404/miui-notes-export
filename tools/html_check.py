# -*- coding: utf-8 -*-
"""导出结果自检：HTML 结构、链接与图片引用是否真的指向存在的文件。

不依赖浏览器（故意用标准库），所以任何有 Python 的机器都能跑。
用法：

    python tools/html_check.py <导出目录> [-v]

`-v` 会逐页打印，默认只打印汇总（导出目录里可能包含私人笔记标题，逐页输出
不适合直接贴到聊天窗口/issue 里）。

退出码：0 = 没问题，1 = 有问题。
"""

from __future__ import annotations

import os
import re
import sys
from html.parser import HTMLParser

VOID_TAGS = ('img', 'br', 'input', 'meta', 'link', 'hr')


class Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs = []          # (tag, attr, value)
        self.text = []
        self.stack = []
        self.unbalanced = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID_TAGS:
            self.stack.append(tag)
        for key, value in attrs:
            if key in ('src', 'href'):
                self.refs.append((tag, key, value))

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            self.unbalanced.append('闭合顺序异常：%s（栈顶 %s）' % (tag, self.stack[-1]))
            while self.stack and self.stack.pop() != tag:
                pass
        else:
            self.unbalanced.append('多余闭合标签：%s' % tag)

    def handle_data(self, data):
        self.text.append(data)


def main() -> int:
    if len(sys.argv) < 2:
        print('用法：python tools/html_check.py <导出目录> [-v]')
        return 2
    out = sys.argv[1]
    verbose = '-v' in sys.argv[2:] or '--verbose' in sys.argv[2:]
    if not os.path.isdir(out):
        print('不是目录：%s' % out)
        return 2

    pages = []
    for root, _dirs, files in os.walk(out):
        for name in files:
            if name.endswith('.html'):
                pages.append(os.path.join(root, name))
    pages.sort()
    if not pages:
        print('没找到 .html 文件（是不是只导出了 md/docx？）')
        return 2

    problems = []
    img_total = 0
    link_total = 0
    for path in pages:
        with open(path, encoding='utf-8') as fh:
            raw = fh.read()
        if not raw.lstrip().lower().startswith('<!doctype html>'):
            problems.append('%s 缺少 <!DOCTYPE html>' % path)
        if 'charset="utf-8"' not in raw:
            problems.append('%s 未声明 utf-8' % path)

        collector = Collector()
        collector.feed(raw)
        if collector.stack:
            collector.unbalanced.append('未闭合标签：%s' % collector.stack)
        for msg in collector.unbalanced:
            problems.append('%s %s' % (os.path.relpath(path, out), msg))

        page_imgs = 0
        page_links = 0
        for tag, _attr, value in collector.refs:
            if value.startswith(('http://', 'https://', 'data:', '#')):
                continue
            if tag == 'img':
                page_imgs += 1
            if tag == 'a':
                page_links += 1
            target = os.path.normpath(
                os.path.join(os.path.dirname(path), value.replace('/', os.sep)))
            if not os.path.exists(target):
                problems.append('%s 引用了不存在的文件：%s'
                                % (os.path.relpath(path, out), value))
        img_total += page_imgs
        link_total += page_links

        text = re.sub(r'\s+', ' ', ''.join(collector.text)).strip()
        if len(text) < 10:
            problems.append('%s 正文几乎是空的' % os.path.relpath(path, out))
        if verbose:
            print('%-56s 图片 %2d 链接 %2d 文本 %5d 字' % (
                os.path.relpath(path, out), page_imgs, page_links, len(text)))

    print('HTML 文件 %d 个，图片引用 %d 处，链接 %d 处' % (len(pages), img_total, link_total))
    print('问题 %d 个' % len(problems))
    for item in problems:
        print('  -', item)
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main())

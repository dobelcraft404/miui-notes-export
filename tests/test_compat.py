# -*- coding: utf-8 -*-
"""兼容性测试：版本字段差异、多应用备份包、加密备份、可选依赖缺失。

全部使用合成数据，不读真实备份，几秒内跑完。

    python tests/test_compat.py
"""
import os
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import make_synthetic_bak as synth          # noqa: E402

from miui_notes_bak import bakfile, cli     # noqa: E402

FAILED = []


def check(condition, message):
    print(('  [通过] ' if condition else '  [失败] ') + message)
    if not condition:
        FAILED.append(message)


def note_texts(out_root):
    texts = []
    for dirpath, _dirnames, filenames in os.walk(out_root):
        if 'note.md' in filenames:
            with open(os.path.join(dirpath, 'note.md'), encoding='utf-8') as fh:
                texts.append(fh.read())
    return texts


def run_export(bak_path, out_root, formats=('md', 'html')):
    exporter = cli.Exporter(out_root, list(formats), False, False, True, merge=True)
    return exporter.run(bak_path)


def main():
    tmp = tempfile.mkdtemp(prefix='miui-compat-')
    try:
        # ---- 1. 字段号变体：标准 vs alt（容器 #6→#7、item #9→#11、头部多一行、PAX tar）
        results = {}
        for variant in ('', 'alt'):
            tag = variant or 'std'
            bak = os.path.join(tmp, 'v-%s.bak' % tag)
            synth.build_bak(bak, variant=variant)
            out = os.path.join(tmp, 'out-%s' % tag)
            stat = run_export(bak, out)
            results[tag] = (stat, note_texts(out))
            check(stat['notes'] == 4 and stat['folders'] == 2,
                  '变体 %r：解析出 4 篇笔记 / 2 个目录' % tag)
            check(stat['missing'] == 0, '变体 %r：正文引用的附件都能找到' % tag)

        other_tree = os.path.join(tmp, 'out-alt')
        std_count = len(results['std'][1])
        alt_count = len(results['alt'][1])
        check(std_count == alt_count == 4, '两种变体导出的笔记文件数一致（%d）' % std_count)

        alt_blob = '\n'.join(results['alt'][1])
        check('第一段' in alt_blob and '缩进一段' in alt_blob,
              '变体 alt：正文内容正确解析')

        # ---- 2. 下划线：Markdown 里不再出现 HTML 标签
        std_blob = '\n'.join(results['std'][1])
        check('<u>' not in std_blob and '</u>' not in std_blob,
              'Markdown 里没有 <u> 标签（无标题笔记不会出现“代码”）')
        check('下划线' in std_blob, '下划线的文字本身仍然保留')
        html_blob = ''
        for dirpath, _d, filenames in os.walk(other_tree):
            if 'note.html' in filenames:
                with open(os.path.join(dirpath, 'note.html'), encoding='utf-8') as fh:
                    html_blob += fh.read()
        check('<u>下划线</u>' in html_blob, 'HTML 输出里仍保留下划线样式')

        # ---- 3. zip 里同时有别的应用备份与笔记备份：自动挑笔记
        notes_bak = os.path.join(tmp, '笔记(com.miui.notes).bak')
        synth.build_bak(notes_bak)
        foreign_bak = os.path.join(tmp, '别的应用(com.tencent.mm).bak')
        synth.build_foreign_bak(foreign_bak)
        bundle = os.path.join(tmp, 'backup.zip')
        with zipfile.ZipFile(bundle, 'w') as zf:
            zf.write(foreign_bak, '别的应用(com.tencent.mm).bak')
            zf.write(notes_bak, '笔记(com.miui.notes).bak')
        picked, cleanup = cli.prepare_input(bundle)
        try:
            check(bakfile.MiuiBackup(picked).package == bakfile.PACKAGE,
                  'zip 里有多个 .bak 时自动挑出笔记备份')
        finally:
            if cleanup:
                shutil.rmtree(cleanup, ignore_errors=True)

        # ---- 4. zip 里只有别的应用备份：明确报错，而不是解析出乱七八糟的结果
        only_foreign = os.path.join(tmp, 'only-foreign.zip')
        with zipfile.ZipFile(only_foreign, 'w') as zf:
            zf.write(foreign_bak, '别的应用(com.tencent.mm).bak')
        try:
            cli.prepare_input(only_foreign)
            check(False, 'zip 里没有笔记备份时应报错')
        except SystemExit as exc:
            message = str(exc)
            check('小米笔记' in message or bakfile.PACKAGE in message,
                  'zip 里没有笔记备份时给出明确提示')

        # ---- 5. 加密备份：给出可理解的提示
        encrypted = os.path.join(tmp, 'encrypted.bak')
        with open(notes_bak, 'rb') as fh:
            data = fh.read()
        data = data.replace(b'ANDROID BACKUP\n5\n0\nnone\n',
                            b'ANDROID BACKUP\n5\n1\nAES-256\n', 1)
        with open(encrypted, 'wb') as fh:
            fh.write(data)
        try:
            run_export(encrypted, os.path.join(tmp, 'out-enc'))
            check(False, '加密备份应被拒绝')
        except SystemExit as exc:
            check('加密' in str(exc), '加密备份给出中文提示')

        # ---- 6. 格式组合参数
        check(cli.parse_formats('md,html') == ['md', 'html'], '支持 -f md,html 组合')
        check(cli.parse_formats('all') == ['md', 'docx', 'html'], 'all 展开为三种格式')
        try:
            cli.parse_formats('pdf')
            check(False, '不认识的格式应报错')
        except SystemExit:
            check(True, '不认识的格式给出提示')

        # ---- 7. 没有 python-docx 时：能出什么出什么，而不是直接崩
        saved = sys.modules.get('docx')
        sys.modules['docx'] = None          # 让 import docx 失败
        try:
            degrade = os.path.join(tmp, 'out-degrade')
            stat = run_export(notes_bak, degrade, formats=('md', 'docx'))
            check(stat['md'] == 4 and stat['docx'] == 0,
                  '缺少 python-docx 时自动跳过 Word，Markdown 照常导出')
            try:
                run_export(notes_bak, os.path.join(tmp, 'out-docx-only'), formats=('docx',))
                check(False, '只要求 docx 且缺依赖时应报错')
            except SystemExit as exc:
                check('python-docx' in str(exc), '只要求 Word 时提示安装 python-docx')
        finally:
            if saved is None:
                sys.modules.pop('docx', None)
            else:
                sys.modules['docx'] = saved
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILED:
        print('结果：%d 项失败' % len(FAILED))
        for item in FAILED:
            print('  - ' + item)
        return 1
    print('结果：全部通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

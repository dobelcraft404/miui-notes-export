# -*- coding: utf-8 -*-
"""诊断模式测试：结构统计是否准确、报告是否真的不含正文。

全部使用合成数据，不读真实备份，几秒内跑完。

    python tests/test_diagnose.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import make_synthetic_bak as synth          # noqa: E402

from miui_notes_bak import diagnose         # noqa: E402

FAILED = []


def check(condition, message):
    print(('  [通过] ' if condition else '  [失败] ') + message)
    if not condition:
        FAILED.append(message)


def main():
    tmp = tempfile.mkdtemp(prefix='miui-diagnose-')
    try:
        # ---- 1. 标准备份：统计值应与导出阶段的数字一致
        print('== 1. 标准备份诊断 ==')
        bak = os.path.join(tmp, 'std.bak')
        synth.build_bak(bak)
        report = diagnose.diagnose(bak)
        text = diagnose.format_report(report)

        check(not report['errors'], '没有错误（实际：%s）' % (report['errors'],))
        check(report['header']['package'] == 'com.miui.notes',
              '读到包名 com.miui.notes')
        check(report['header']['tar_offset'] == 65, 'tar 起点为 65')
        check(report['header']['encrypted'] is False, '识别为未加密')
        check(report['tar']['count'] > 0, 'tar 成员数大于 0')
        check(report['records']['total'] == 6, '记录 6 条（实际 %s）' % report['records']['total'])
        check(report['records']['folders'] == 2, '目录 2 个')
        check(report['records']['notes'] == 4, '笔记 4 篇')
        check(report['attachments']['count'] == 4, '附件 4 个')
        check(not report['refs']['missing'], '正文引用的附件都找得到')

        # ---- 2. 报告里不能出现正文
        print('== 2. 报告不含正文 ==')
        check('下划线' not in text, '报告里没有正文文字「下划线」')
        check('文本笔记' not in text, '报告里没有笔记标题「文本笔记」')
        check('测试目录' not in text, '报告里没有目录名「测试目录」')
        for section in ('[1] 文本头', '[2] tar 归档', '[3] 记录', '[4] 附件', '[5] 引用', '[6] 结论'):
            check(section in text, '报告包含小节 %s' % section)
        check('不含任何笔记正文' in text, '报告末尾有「不含正文」提示')

        # ---- 3. 字段号变体（容器 #6→#7、item #9→#11）也要能诊断出来
        print('== 3. 字段号变体 ==')
        alt = os.path.join(tmp, 'alt.bak')
        synth.build_bak(alt, variant='alt')
        alt_report = diagnose.diagnose(alt)
        check(not alt_report['errors'], '变体诊断没有错误（实际：%s）' % (alt_report['errors'],))
        check(alt_report['records']['total'] == 6, '变体仍是 6 条记录')
        check(alt_report['records']['notes'] == 4, '变体仍是 4 篇笔记')

        # ---- 4. 加密备份要明确指出，而不是静默给出半份结果
        print('== 4. 加密备份 ==')
        with open(bak, 'rb') as fh:
            data = fh.read()
        marker = b'ANDROID BACKUP\n5\n0\n'
        check(marker in data, '合成备份里有加密标志字段')
        enc = os.path.join(tmp, 'enc.bak')
        with open(enc, 'wb') as fh:
            fh.write(data.replace(marker, b'ANDROID BACKUP\n5\n1\n', 1))
        enc_report = diagnose.diagnose(enc)
        check(enc_report['header']['encrypted'] is True, '识别出加密标志')
        joined = ' '.join(enc_report['errors'])
        check('加密' in joined, '错误里说明了是加密备份')
        check('加密' in diagnose.format_report(enc_report), '报告文本里也提示了加密')

        # ---- 5. 其它应用的备份：包名要如实读出来，不冒充笔记备份
        print('== 5. 其它应用的备份 ==')
        foreign = os.path.join(tmp, 'foreign.bak')
        synth.build_foreign_bak(foreign)
        fr = diagnose.diagnose(foreign)
        check(fr['header']['package'] == 'com.tencent.mm',
              '读到包名 com.tencent.mm（实际 %r）' % fr['header']['package'])
        check(isinstance(diagnose.format_report(fr), str), '能正常渲染出报告')
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

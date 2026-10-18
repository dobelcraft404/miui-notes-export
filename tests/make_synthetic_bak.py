# -*- coding: utf-8 -*-
"""构造一份脱敏的最小小米笔记备份，供端到端测试自检使用。

不依赖任何真实备份：先按 MIUI BACKUP 容器格式拼 tar，再把笔记 protobuf
手写出来，最后命名为 .bak。生成结果只包含合成数据。
"""
import io
import os
import tarfile

# ---- 合成用的附件内容 -------------------------------------------------------
PNG = bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489'
                    '0000000a49444154789c6300010000050001' '0d0a2db4' '0000000049454e44ae426082')
JPEG = bytes.fromhex('ffd8ffe000104a46494600010100000100010000ffdb004300'
                     'ffc00011080001000103012200021101031101'
                     'ffc4001f0000010501010101010100000000000000000102030405060708'
                     '0a0bffda0008010100003f00d2cf20ffd9')
JSON = b'{"recognized": [], "synthetic": true}'
THUMB = 'd' * 39 + '4'   # 手写笔记的缩略图附件名（纯合成，含 a-f）


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _tag(field, wire):
    return _varint((field << 3) | wire)


def _fvarint(field, value):
    return _tag(field, 0) + _varint(value)


def _fbytes(field, value):
    if isinstance(value, str):
        value = value.encode('utf-8')
    return _tag(field, 2) + _varint(len(value)) + value


def _item(mime, content, kind=0, field=9):
    """项：1=mime, 2/3=时间戳占位, 4=正文, 10=kind"""
    body = _fbytes(1, mime) + _fvarint(2, 1673258077000) + _fvarint(3, 1673258077000)
    if content:
        body += _fbytes(4, content)
    if kind:
        body += _fvarint(10, kind)
    return body


def _note(note_id, title, folder, created, modified, items, extra=(), kind='common',
          item_field=9):
    """#2 笔记记录。"""
    body = _fbytes(2, note_id) + _fvarint(3, 0) + _fvarint(4, 0)
    body += _fvarint(5, created) + _fvarint(6, modified) + _fvarint(7, 0)
    body += _fbytes(8, title)          # 摘要（此处与标题相同）
    for it in items:
        body += _fbytes(item_field, it)
    if folder:
        body += _fbytes(10, folder)
    body += _fbytes(14, title)
    body += _fbytes(15, kind)
    for f, v in extra:
        body += f
    return body


def _folder(name, folder_id):
    """#1 目录记录：字段号 1，其 #9 是目录名。"""
    body = _fbytes(2, folder_id) + _fvarint(5, 1700000000000) + _fvarint(6, 1700000000000)
    body += _fbytes(9, name) + _fbytes(15, 'folder')
    return body


def _layout(variant=''):
    """不同版本可能用的字段号。alt 用来模拟「容器字段换成 #7、item 换成 #11」。"""
    if variant == 'alt':
        return {'container': 7, 'note': 2, 'item': 11}
    return {'container': 6, 'note': 2, 'item': 9}


def build_records(title_suffix='', variant=''):
    """返回顶层容器的内容：2 个目录 + 4 篇笔记。

    传入 title_suffix 可以让目录名与笔记标题整体变一个样，用来模拟「笔记被编辑过」。
    variant='alt' 时换一套字段号，用来验证解析器对版本差异的兼容性。
    """
    lay = _layout(variant)
    folder = '测试目录' + title_suffix
    att_a = 'a' * 39 + '1'          # 含 a-f，满足 40hex 约束
    att_b = 'b' * 39 + '2'
    att_c = 'c' * 39 + '3'
    out = b''
    out += _tag(1, 2) + _varint(len(_folder(folder, '1'))) + _folder(folder, '1')
    out += _tag(1, 2) + _varint(len(_folder('空目录', '2'))) + _folder('空目录', '2')

    def note_field(body):
        return _tag(lay['note'], 2) + _varint(len(body)) + body

    # 1) 纯文本 + 粗体斜体 + 待办
    text = ('<text indent="0">第一段<b>加粗</b>与<i>斜体</i><u>下划线</u></text>'
            '<text indent="1">缩进一段<br>换行后</text>'
            '<input type="checkbox" indent="1" level="3" checked="true" />')
    out += note_field(_note('1', '文本笔记' + title_suffix, folder,
                            1700000000000, 1700000001000,
                            [_item('vnd.android.cursor.item/text_note', text,
                                   field=lay['item'])], item_field=lay['item']))

    # 2) 图片紧跟块级标签（历史死循环的边界）
    img_body = ('<text indent="0">看图</text>\u263a %s<0/></>'
                '<input type="checkbox" indent="1" checked="true" />'
                '<text indent="1">尾段</text>') % att_a
    out += note_field(_note('2', '图片笔记', folder, 1700000002000, 1700000003000,
                            [_item('vnd.android.cursor.item/text_note', img_body,
                                   field=lay['item'])], item_field=lay['item']))

    # 3) 无目录、无标题（应回退到摘要/未命名笔记）
    blank = _note('3', '', '', 1700000004000, 1700000005000,
                  [_item('vnd.android.cursor.item/text_note', '<text indent="1"></text>',
                         field=lay['item'])], item_field=lay['item'])
    out += note_field(blank)

    # 4) 手写笔记：text_note + ent/file(笔迹) + ent/file(识别) + image/jpeg(缩略图)
    #    与真实备份一致：缩略图项的内容就是附件名（40 位 hash）。
    hand = _note('4', '手写笔记', '', 1700000006000, 1700000007000, [
        _item('vnd.android.cursor.item/text_note', '<text indent="0"></text>',
              field=lay['item']),
        _item('ent/file', att_b + '.ent', kind=1, field=lay['item']),
        _item('ent/file', att_c, kind=1, field=lay['item']),
        _item('image/jpeg', THUMB, field=lay['item']),
    ], kind='handwrite', item_field=lay['item'])
    out += note_field(hand)
    return out


def build_bak(path, title_suffix='', variant=''):
    """写出一个合成的 .bak（MIUI BACKUP 头 + tar）。

    variant='alt' 会同时换掉 pb 字段号、在头部插入多一行、并把 tar 写成 PAX 格式，
    用来模拟版本差异与时序差异。
    """
    lay = _layout(variant)
    records = build_records(title_suffix, variant)
    top = _tag(lay['container'], 2) + _varint(len(records)) + records
    tmp_bak = top + _fvarint(7, 1) + _fbytes(8, ' ') + _fbytes(9, '0' * 6)

    attached = {
        'apps/com.miui.notes/miui_att/' + 'a' * 39 + '1': PNG,
        'apps/com.miui.notes/miui_att/' + 'b' * 39 + '2.ent': b'HWPENKITDATA' + b'\x00' * 32,
        'apps/com.miui.notes/miui_att/' + 'c' * 39 + '3': JSON,
        'apps/com.miui.notes/miui_att/' + THUMB: JPEG,
        'apps/com.miui.notes/miui_bak/_tmp_bak': tmp_bak,
    }
    buf = io.BytesIO()
    fmt = tarfile.PAX_FORMAT if variant == 'alt' else tarfile.USTAR_FORMAT
    with tarfile.open(fileobj=buf, mode='w', format=fmt) as tf:
        for name, data in attached.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 1700000000
            tf.addfile(info, io.BytesIO(data))
    if variant == 'alt':
        header = ('MIUI BACKUP\n2\nextra-field\ncom.miui.notes 笔记\n-1\n0\n'
                  'ANDROID BACKUP\n5\n0\nnone\n').encode('utf-8')
    else:
        header = ('MIUI BACKUP\n2\ncom.miui.notes 笔记\n-1\n0\n'
                  'ANDROID BACKUP\n5\n0\nnone\n').encode('utf-8')
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(header)
        f.write(buf.getvalue())
    return path


def build_foreign_bak(path, package='com.tencent.mm'):
    """写出一个「其它应用」的合成备份，用来测试自动挑包。"""
    body = b'fake-records'
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w', format=tarfile.USTAR_FORMAT) as tf:
        info = tarfile.TarInfo('apps/%s/fake.bin' % package)
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    header = ('MIUI BACKUP\n2\n%s 别的应用\n-1\n0\n'
              'ANDROID BACKUP\n5\n0\nnone\n' % package).encode('utf-8')
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(header)
        f.write(buf.getvalue())
    return path


if __name__ == '__main__':
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'synthetic.bak')
    print('已生成合成备份:', build_bak(out), os.path.getsize(out), '字节')

# -*- coding: utf-8 -*-
"""小米笔记备份导出工具 —— 图形界面。

双击目录下的 `启动.bat`，或者在本目录执行 `python main.py` 即可打开。
界面只做三件事：选备份包、选输出目录、点开始；导出在后台线程里跑，
窗口不会卡住。命令行等价于：

    python -m miui_notes_bak <备份.zip> -o <输出目录> -f all --merge
"""

from __future__ import annotations

import os
import queue
import sys
import tempfile
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from miui_notes_bak import cli, export                     # noqa: E402

FORMAT_LABELS = [
    ('md', 'Markdown（.md）'),
    ('docx', 'Word（.docx）'),
    ('html', 'HTML（可整目录打包浏览）'),
]


def _open_path(path: str) -> None:
    """用系统默认程序打开文件或目录（Windows / macOS / Linux 都兼容）。"""
    try:
        if hasattr(os, 'startfile'):
            os.startfile(path)                              # type: ignore[attr-defined]
        elif sys.platform == 'darwin':
            import subprocess
            subprocess.Popen(['open', path])
        else:
            import subprocess
            subprocess.Popen(['xdg-open', path])
    except OSError as exc:
        print('打开失败：%r' % (exc,), file=sys.stderr)


def run_export(source: str, out_root: str, formats, merge: bool, keep_raw: bool,
               log) -> dict:
    """执行一次导出，`log` 是收字符串的回调（进度、错误都走它）。"""
    bak_path, cleanup = cli.prepare_input(source)
    try:
        exporter = cli.Exporter(out_root, formats, keep_raw, False, True, merge=merge)
        if merge:
            old = cli.manifest.Manifest.load(out_root)
            log('已有清单：%d 篇（内容相同的笔记不会重复导出）' % len(old.entries))
        exporter._log = log                                # 单篇进度回调（可选）
        stat = exporter.run(bak_path)
        return stat
    finally:
        if cleanup:
            import shutil
            shutil.rmtree(cleanup, ignore_errors=True)


def _stat_lines(stat: dict, out_root: str, formats) -> list:
    lines = [
        '笔记 %d 篇，目录 %d 个' % (stat.get('notes', 0), stat.get('folders', 0)),
    ]
    if stat.get('skipped'):
        lines.append('内容完全相同、没有重复导出：%d 篇' % stat['skipped'])
    for key, label in (('md', 'Markdown'), ('docx', 'Word'), ('html', 'HTML')):
        if key in formats:
            lines.append('%s：%d 份' % (label, stat.get(key, 0)))
    lines.append('附件落盘：%d 个' % stat.get('assets', 0))
    if stat.get('missing'):
        lines.append('正文里引用了但备份中找不到的附件：%d 处' % stat['missing'])
    if stat.get('unreferenced'):
        lines.append('没有被任何笔记引用的附件：%d 个' % stat['unreferenced'])
    lines.append('目录页：%s' % os.path.join(out_root, 'index.html'))
    return lines


def build_app():
    """构造主窗口。tkinter 不可用时返回 None。"""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except Exception as exc:                                # pragma: no cover
        print('当前 Python 没有可用的 tkinter：%r' % (exc,), file=sys.stderr)
        return None

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title('小米笔记备份导出工具')
            self.geometry('760x560')
            self.minsize(660, 480)
            self.queue: queue.Queue = queue.Queue()
            self.worker: threading.Thread = None
            self.formats = {}
            self._build()
            self.after(120, self._drain)

        # -- 界面 ----------------------------------------------------------
        def _build(self):
            pad = dict(padx=10, pady=6)
            row = ttk.Frame(self)
            row.pack(fill='x', **pad)
            ttk.Label(row, text='备份包（.zip 或 .bak）', width=20).pack(side='left')
            self.var_src = tk.StringVar(value=self._guess_source())
            ttk.Entry(row, textvariable=self.var_src).pack(side='left', fill='x',
                                                           expand=True)
            ttk.Button(row, text='浏览…', command=self._pick_source).pack(side='left')

            row = ttk.Frame(self)
            row.pack(fill='x', **pad)
            ttk.Label(row, text='输出目录', width=20).pack(side='left')
            self.var_out = tk.StringVar(value=os.path.join(HERE, '导出结果'))
            ttk.Entry(row, textvariable=self.var_out).pack(side='left', fill='x',
                                                           expand=True)
            ttk.Button(row, text='选择…', command=self._pick_out).pack(side='left')

            box = ttk.LabelFrame(self, text='导出格式')
            box.pack(fill='x', **pad)
            for key, label in FORMAT_LABELS:
                var = tk.BooleanVar(value=(key != 'html'))
                self.formats[key] = var
                ttk.Checkbutton(box, text=label, variable=var).pack(anchor='w', padx=8)

            box = ttk.LabelFrame(self, text='重复内容')
            box.pack(fill='x', **pad)
            self.var_merge = tk.BooleanVar(value=True)
            ttk.Radiobutton(box, value=True, variable=self.var_merge,
                            text='与已有文档合并：内容完全相同的笔记不再导出第二份（推荐）'
                            ).pack(anchor='w', padx=8)
            ttk.Radiobutton(box, value=False, variable=self.var_merge,
                            text='不合并：即使内容相同也在输出目录里再写一份'
                            ).pack(anchor='w', padx=8)
            self.var_raw = tk.BooleanVar(value=False)
            ttk.Checkbutton(box, text='额外原样释放全部附件（含无法解码的手写笔迹）',
                            variable=self.var_raw).pack(anchor='w', padx=8)

            row = ttk.Frame(self)
            row.pack(fill='x', **pad)
            self.btn_run = ttk.Button(row, text='开始导出', command=self._start)
            self.btn_run.pack(side='left')
            self.btn_open = ttk.Button(row, text='打开输出目录', state='disabled',
                                       command=self._open_out)
            self.btn_open.pack(side='left', padx=6)
            self.btn_index = ttk.Button(row, text='打开目录页 index.html',
                                        state='disabled', command=self._open_index)
            self.btn_index.pack(side='left')
            self.var_status = tk.StringVar(value='就绪')
            ttk.Label(row, textvariable=self.var_status).pack(side='right')

            self.log = tk.Listbox(self, height=12, activestyle='none')
            sb = ttk.Scrollbar(self, orient='vertical', command=self.log.yview)
            self.log.configure(yscrollcommand=sb.set)
            sb.pack(side='right', fill='y')
            self.log.pack(fill='both', expand=True, **pad)
            ttk.Label(self, text='说明：图片等附件会放进每篇笔记的 note.assets 目录，'
                                 '移动整个输出目录也不会断链。').pack(anchor='w', padx=10)

        def _guess_source(self):
            """在常见位置找备份包：桌面 / 下载 / 主目录下的 backup / 程序目录。"""
            cands = []
            home = os.path.expanduser('~')
            for sub in ('Desktop', 'Downloads', 'backup'):
                cands.append(os.path.join(home, sub))
            cands.append(HERE)
            for folder in cands:
                if not os.path.isdir(folder):
                    continue
                try:
                    names = sorted(os.listdir(folder))
                except OSError:
                    continue
                for name in names:
                    low = name.lower()
                    if low.endswith('.bak') or (low.endswith('.zip') and 'backup' in low):
                        return os.path.join(folder, name)
                for name in names:
                    if name.lower().endswith('.zip'):
                        return os.path.join(folder, name)
            return ''

        # -- 交互 ----------------------------------------------------------
        def _pick_source(self):
            path = filedialog.askopenfilename(
                title='选择小米备份包',
                filetypes=[('备份包', '*.zip *.bak'), ('zip 包', '*.zip'),
                           ('小米备份', '*.bak'), ('所有文件', '*.*')])
            if path:
                self.var_src.set(path)
                if not self.var_out.get().strip():
                    self.var_out.set(os.path.join(HERE, '导出结果'))

        def _pick_out(self):
            path = filedialog.askdirectory(title='选择输出目录')
            if path:
                self.var_out.set(path)

        def _say(self, text: str) -> None:
            self.queue.put(('log', text))

        def _start(self):
            if self.worker is not None and self.worker.is_alive():
                return
            src = self.var_src.get().strip().strip('"')
            out = self.var_out.get().strip().strip('"')
            formats = [k for k, v in self.formats.items() if v.get()]
            if not src or not os.path.exists(src):
                messagebox.showerror('缺少备份包', '请选择备份包（.zip 或 .bak）。')
                return
            if not formats:
                messagebox.showerror('缺少格式', '至少勾选一种导出格式。')
                return
            if not out:
                messagebox.showerror('缺少输出目录', '请选择输出目录。')
                return
            self.log.delete(0, 'end')
            self.btn_run.configure(state='disabled')
            self.btn_open.configure(state='disabled')
            self.btn_index.configure(state='disabled')
            self.var_status.set('导出中…')
            self.worker = threading.Thread(
                target=self._work, args=(src, out, formats,
                                         bool(self.var_merge.get()),
                                         bool(self.var_raw.get())),
                daemon=True)
            self.worker.start()

        def _work(self, src, out, formats, merge, keep_raw):
            started = time.time()
            try:
                self._say('输入：%s' % src)
                self._say('输出：%s' % out)
                self._say('读取备份包…（大文件需要几秒）')
                stat = run_export(src, out, formats, merge, keep_raw, self._say)
                for line in _stat_lines(stat, out, formats):
                    self._say(line)
                self._say('用时 %.1f 秒' % (time.time() - started))
                self.queue.put(('done', out))
            except SystemExit as exc:
                # cli 用 SystemExit 抛「人话」错误（加密备份、没有笔记包、缺 python-docx 等），
                # 不能让它悄悄结束线程，否则界面会一直停在“导出中”。
                msg = str(exc) or '程序提前退出'
                self._say('停止：%s' % msg)
                self.queue.put(('fail', msg))
            except Exception as exc:
                self._say('出错了：')
                for line in traceback.format_exc().splitlines():
                    self._say('  ' + line)
                self.queue.put(('fail', '%s: %s' % (type(exc).__name__, exc)))

        def _drain(self):
            try:
                while True:
                    kind, payload = self.queue.get_nowait()
                    if kind == 'log':
                        self.log.insert('end', payload)
                        self.log.see('end')
                    elif kind == 'done':
                        self._finish(payload, ok=True)
                    else:
                        self._finish(None, ok=False, detail=payload or '')
            except queue.Empty:
                pass
            self.after(120, self._drain)

        def _finish(self, out, ok, detail=''):
            self.btn_run.configure(state='normal')
            self.var_status.set('完成' if ok else '失败')
            if ok:
                self.btn_open.configure(state='normal')
                if os.path.isfile(os.path.join(out, 'index.html')):
                    self.btn_index.configure(state='normal')
                if messagebox.askyesno('导出完成', '导出完成。要打开目录看看吗？'):
                    self._open_out()
            else:
                text = (detail or '未知错误').strip()
                if len(text) > 600:
                    text = text[:600] + '…'
                messagebox.showerror(
                    '导出失败',
                    '导出没有完成。\n\n%s\n\n详细的错误堆栈在下面的日志里。\n'
                    '如果是第一次在这个设备上用，可以把命令行加 --diagnose 跑一遍，'
                    '把输出的诊断报告贴出来（报告不含笔记内容）。' % text)

        def _open_out(self):
            out = self.var_out.get().strip().strip('"')
            if out and os.path.isdir(out):
                _open_path(out)

        def _open_index(self):
            out = self.var_out.get().strip().strip('"')
            index = os.path.join(out, 'index.html')
            if os.path.isfile(index):
                _open_path(index)

    return App


def _selftest() -> int:
    """无显示器/沙箱环境下的自检：构造窗口但不进事件循环。"""
    if not os.environ.get('DISPLAY') and sys.platform.startswith('linux'):
        print('跳过：没有 DISPLAY')
        return 0
    factory = build_app()
    if factory is None:
        print('失败：tkinter 不可用')
        return 1
    app = factory()
    app.update_idletasks()
    print('窗口构造成功：标题=%r 尺寸=%s 格式勾选=%s 合并默认=%s'
          % (app.title(), app.geometry(), [k for k, v in app.formats.items() if v.get()],
             app.var_merge.get()))

    # 失败路径自检：导出出错时必须把错误放进队列（界面据此弹窗），
    # 而不是让后台线程悄悄死掉、界面一直停在「导出中…」。
    bad = os.path.join(HERE, '__selftest_no_such_backup__.zip')
    out = os.path.join(tempfile.gettempdir(), 'miui-notes-selftest-out')
    try:
        app._work(bad, out, ['md'], True, False)
    except BaseException as exc:                    # noqa: BLE001
        print('失败：导出失败时把异常抛了出来：%r' % (exc,))
        app.destroy()
        return 1
    kinds = []
    while True:
        try:
            kinds.append(app.queue.get_nowait()[0])
        except queue.Empty:
            break
    if 'fail' not in kinds:
        print('失败：导出失败时没有把错误送进队列（队列里只有 %s）' % (kinds,))
        app.destroy()
        return 1
    print('失败路径：已捕获并进入队列（%d 条消息）' % len(kinds))
    app.destroy()
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--selftest' in argv:
        return _selftest()
    factory = build_app()
    if factory is None:
        print('tkinter 不可用，请改用命令行：python -m miui_notes_bak <备份包> -o <输出目录>')
        return 2
    app = factory()
    app.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())

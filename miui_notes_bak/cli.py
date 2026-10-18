# -*- coding: utf-8 -*-
"""把 MIUI 笔记备份（.bak，或含 .bak 的 zip 备份包）导出为 Markdown / Word。"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import zipfile
from typing import Dict, List, Optional, Sequence, Tuple

if __package__ in (None, ''):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from miui_notes_bak import __version__ as VERSION
    from miui_notes_bak import attachments as att_mod
    from miui_notes_bak import bakfile, content, diagnose as diagnose_mod
    from miui_notes_bak import export, manifest, media
else:
    from . import __version__ as VERSION
    from . import attachments as att_mod
    from . import bakfile, content, diagnose as diagnose_mod
    from . import export, manifest, media

BAK_SUFFIX = '.bak'
ASSET_DIRNAME = 'note.assets'
FORMAT_FILE = {'md': 'note.md', 'docx': 'note.docx', 'html': 'note.html'}


# ---------------------------------------------------------------------------
# 输入准备：.bak 或 包含 .bak 的 zip 备份包
# ---------------------------------------------------------------------------

def _bak_package(path: str) -> str:
    """只读文件头判断这个 .bak 属于哪个应用；读不出来返回空串。"""
    try:
        with open(path, 'rb') as fh:
            head = fh.read(65536)
        lines, _offset = bakfile.read_header(head)
    except (OSError, bakfile.BackupFormatError):
        return ''
    for line in lines:
        token = line.split(' ', 1)[0].strip().lower()
        if token.count('.') >= 1 and token.startswith(('com.', 'org.', 'net.', 'cn.', 'io.')):
            return token
    return ''


def _pick_bak(paths: Sequence[str], source: str) -> str:
    """从若干 .bak 里挑出笔记备份（zip 备份包通常包含多个应用）。"""
    seen = []
    unknown = []
    for path in paths:
        package = _bak_package(path)
        if package == bakfile.PACKAGE:
            return path
        if package:
            seen.append('%s（%s）' % (os.path.basename(path), package))
        else:
            unknown.append(path)
    if len(paths) == 1 and unknown:
        # 只有一个 .bak 且读不出包名：试着解析，后面会给出更具体的错误
        return paths[0]
    raise SystemExit('备份包里没有小米笔记（%s）。候选：%s'
                     % (bakfile.PACKAGE, '、'.join(seen) or '、'.join(
                         os.path.basename(p) for p in paths)))


def prepare_input(source: str, workdir: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """返回 (bak 路径, 需要清理的临时目录)。

    支持三种输入：单个 .bak、含一个或多个 .bak 的 zip 备份包、含 .bak 的目录。
    zip 里通常同时有多个应用的备份，这里按头部包名挑出笔记那一个。
    """
    if os.path.isdir(source):
        cands = [os.path.join(source, f) for f in sorted(os.listdir(source))
                 if f.lower().endswith(BAK_SUFFIX)]
        if not cands:
            raise SystemExit('目录里没有找到 .bak 文件：%s' % source)
        return _pick_bak(cands, source), None

    if source.lower().endswith('.zip'):
        tmp = workdir or tempfile.mkdtemp(prefix='miui-notes-')
        with zipfile.ZipFile(source) as zf:
            infos = [i for i in zf.infolist() if i.filename.lower().endswith(BAK_SUFFIX)]
            if not infos:
                raise SystemExit('zip 里没有找到 .bak：%s' % source)
            extracted: List[str] = []
            for idx, info in enumerate(infos):
                name = os.path.basename(info.filename.replace('\\', '/')) or 'backup.bak'
                out = os.path.join(tmp, '%02d_%s' % (idx, name))
                with zf.open(info) as src, open(out, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                extracted.append(out)
            return _pick_bak(extracted, source), tmp
    return source, None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

class Exporter:
    def __init__(self, out_root: str, formats: Sequence[str], keep_raw: bool,
                 copy_unreferenced: bool, index_csv: bool, merge: bool = True):
        self.out_root = out_root
        self.formats = list(formats)
        self.keep_raw = keep_raw
        self.copy_unreferenced = copy_unreferenced
        self.index_csv = index_csv
        self.merge = merge
        self.stat = dict(notes=0, folders=0, md=0, docx=0, html=0, assets=0, missing=0,
                         unreferenced=0, unreferenced_copied=0, skipped=0)
        self.rows: List[dict] = []
        self.referenced = set()
        self._used_names: Dict[str, set] = {}
        self._raw_dir = os.path.join(out_root, 'raw_attachments')
        # 合并用：清单 + 预先分配好的目标目录（note_id -> 相对目录）
        self._manifest = manifest.Manifest(out_root)
        self._plan: Dict[str, str] = {}
        self._rows_by_path: Dict[str, dict] = {}
        # 规划阶段算一次就缓存：内容指纹、附件引用（导出阶段不再重复解析）
        self._content_key: Dict[str, str] = {}
        self._fingerprint: Dict[str, str] = {}
        self._cache: Dict[str, Tuple[content.NoteDocument, List[str], Dict[str, str]]] = {}
        self._skip: Dict[str, str] = {}

    # -- 输出位置 -------------------------------------------------------
    def _note_dir(self, note: bakfile.Note) -> Tuple[str, str]:
        rel = self._plan.get(str(note.note_id))
        if rel is None:
            rel_dir, name = self._allocate(note)
        else:
            rel_dir, _, name = rel.rpartition('/')
        return rel_dir or '未分类', name

    def _allocate(self, note: bakfile.Note) -> Tuple[str, str]:
        rel_dir = export.safe_component(note.folder, '未分类') if note.folder else '未分类'
        base = export.safe_component(export.display_name(note),
                                     '未命名笔记-%s' % note.note_id)
        bucket = self._used_names.setdefault(rel_dir, set())
        name = base
        seq = 2
        while name.lower() in bucket:
            name = '%s (%d)' % (base, seq)
            seq += 1
        bucket.add(name.lower())
        return rel_dir, name

    def _plan_notes(self, notes: Sequence[bakfile.Note],
                    attachments: Dict[str, bytes], resolver: att_mod.Resolver,
                    raw: bytes) -> None:
        """导出前统一分配目录：已有内容的笔记沿用原目录，新笔记接着序号排。"""
        self._cache.clear()
        self._skip.clear()
        for note in notes:
            if note.is_folder:
                continue
            key = str(note.note_id)
            doc = content.parse_content(note.content, list(attachments))
            refs = self._collect_refs(note, doc, resolver, raw)
            for ent in att_mod.handwrite_preview(note, resolver):
                if ent not in refs:
                    refs.append(ent)
            hashes = manifest.attachment_hashes(refs, attachments)
            content_key = manifest.content_fingerprint(note, hashes)
            self._content_key[key] = content_key
            self._fingerprint[key] = manifest.note_fingerprint(note, hashes)
            self._cache[key] = (doc, refs, att_mod.handwrite_preview(note, resolver))

            # 内容一致 + 要求的文件都在 -> 这次不用重写；但目录名要沿用原样
            already = None
            if self.merge:
                already = self._manifest.existing_path(content_key, self.formats)
            if already is not None:
                self._skip[key] = already
                self._plan[key] = already
                continue
            old = self._manifest.get_by_note_id(note.note_id)
            if old is not None and old.path:
                # 同一篇笔记内容变了：沿用原目录，原地覆盖这一篇
                self._plan[key] = old.path

        prefer: Dict[str, str] = {}
        if self.merge:                       # --no-merge 就是「再写一份」，不复用旧目录
            for entry in self._manifest.entries:
                if entry.content_key and entry.path:
                    prefer.setdefault(entry.content_key, entry.path)
        # 不管合不合并，都要先看磁盘上已经有哪些目录，否则会往同一个目录里重复写
        self._used_names = manifest.scan_existing(self.out_root)
        for note, rel in manifest.allocate_dirs(notes, prefer, self._used_names,
                                                self._content_key):
            self._plan[str(note.note_id)] = rel

        if self.merge:
            self._claim_renamed_dirs(notes)

    def _claim_renamed_dirs(self, notes: Sequence[bakfile.Note]) -> None:
        """清单丢了、或者笔记在备份里改了标题时，用 note.md 正文找回原目录。

        这样把同一份备份反复导到同一个目录，不会因为改了个标题就原地多出一份。
        """
        leftover = [n for n in notes if not n.is_folder
                    and str(n.note_id) not in self._skip]
        if not leftover:
            return
        bodies: Optional[Dict[str, List[str]]] = None
        claimed: set = set()
        for note in leftover:
            key = str(note.note_id)
            cached = self._cache.get(key)
            if cached is None:
                continue
            note_dir = self._plan.get(key, '')
            base = os.path.join(self.out_root, *note_dir.replace('\\', '/').split('/'))
            if os.path.isdir(base):
                continue                     # 目标目录已经在了，不用找
            if bodies is None:
                bodies = _body_map(self.out_root)
            hit = _match_by_content(note, cached[0], self.out_root, claimed, bodies)
            if hit is None:
                continue
            self._plan[key] = hit
            claimed.add(hit)

    # -- 附件 -----------------------------------------------------------
    def _collect_refs(self, note: bakfile.Note, doc: content.NoteDocument,
                      resolver: att_mod.Resolver, raw: bytes) -> List[str]:
        return att_mod.collect_refs(note, doc, resolver, raw)

    # -- 导出一篇 -------------------------------------------------------
    def export_note(self, note: bakfile.Note, attachments: Dict[str, bytes],
                    resolver: att_mod.Resolver, raw: bytes) -> None:
        key = str(note.note_id)

        # 合并模式：这篇笔记的内容和格式都已存在 -> 只补一条目录行，不重写文件
        already = self._skip.get(key)
        if already is not None:
            self.stat['skipped'] += 1
            self._rows_by_path[already] = self._index_row(note, already)
            return

        rel_dir, name = self._note_dir(note)
        note_dir_rel = os.path.join(rel_dir, name)
        note_dir = os.path.join(self.out_root, note_dir_rel)

        cached = self._cache.get(key)
        if cached is None:                       # 直接调 export_note 时兜底
            doc = content.parse_content(note.content, list(attachments))
            refs = self._collect_refs(note, doc, resolver, raw)
            preview_of = att_mod.handwrite_preview(note, resolver)
            for ent in preview_of:
                if ent not in refs:
                    refs.append(ent)
            hashes = manifest.attachment_hashes(refs, attachments)
            self._content_key[key] = manifest.content_fingerprint(note, hashes)
            self._fingerprint[key] = manifest.note_fingerprint(note, hashes)
        else:
            doc, refs, preview_of = cached

        content_key = self._content_key[key]
        fingerprint = self._fingerprint[key]

        os.makedirs(note_dir, exist_ok=True)

        # 手写 / 多页手写笔记的正文只有一个占位文字，真正的画面在附件里：
        # 把「正文没有引用到」的图片作为图片块放在正文之前，导出结果才看得出
        # “这是一篇手写笔记”。已经在正文里的图片不能被复制到开头。
        scene_blocks: List[content.ImageBlock] = []
        if note.kind in ('handwrite', 'multi_handwrite'):
            in_body = set(doc.references)
            for ref in refs:
                target = preview_of.get(ref, ref)
                if target in in_body or ref in in_body:
                    continue
                blob = attachments.get(target)
                if blob and media.is_image(blob):
                    scene_blocks.append(content.ImageBlock(ref=target))
            if scene_blocks:
                doc.blocks = scene_blocks + list(doc.blocks)

        asset_dir = os.path.join(note_dir, ASSET_DIRNAME)
        image_links: Dict[str, str] = {}
        image_paths: Dict[str, str] = {}
        listing: List[Tuple[str, str]] = []
        copied: Dict[str, str] = {}

        for ref in refs:
            if ref not in attachments:
                continue
            source = preview_of.get(ref, ref)
            blob = attachments[source]
            kind, ext = media.detect(blob)
            filename = media.safe_name(att_mod.basename(source))
            if not filename.lower().endswith(ext):
                filename += ext
            if filename not in copied:
                media.write_asset(asset_dir, filename, blob)
                copied[filename] = '%s/%s' % (ASSET_DIRNAME, filename)
                self.referenced.add(ref)
                self.stat['assets'] += 1
            link = copied[filename]
            rel = '%s/%s' % (ASSET_DIRNAME, filename)
            image_links[ref] = link
            image_paths[ref] = os.path.join(asset_dir, filename)
            listing.append((ref, rel))

        self.stat['missing'] += sum(1 for ref in doc.references if ref not in attachments)

        if 'md' in self.formats:
            text = export.note_markdown(note, doc, image_links, listing)
            with open(os.path.join(note_dir, 'note.md'), 'w',
                      encoding='utf-8', newline='\n') as fh:
                fh.write(text)
            self.stat['md'] += 1
        if 'html' in self.formats:
            page = export.note_html(note, doc, image_links, listing)
            with open(os.path.join(note_dir, 'note.html'), 'w',
                      encoding='utf-8', newline='\n') as fh:
                fh.write(page)
            self.stat['html'] += 1
        if 'docx' in self.formats:
            try:
                export.note_docx(note, doc, image_paths,
                                 os.path.join(note_dir, 'note.docx'), listing)
                self.stat['docx'] += 1
            except Exception as exc:                  # 单篇失败不中断整批
                with open(os.path.join(note_dir, 'ERROR.txt'), 'w',
                          encoding='utf-8') as fh:
                    fh.write('导出 Word 失败：%r\n' % (exc,))
                print('  ! 笔记 %s 导出 Word 失败：%r' % (note.note_id, exc),
                      file=sys.stderr)

        self.rows.append(dict(
            note_id=note.note_id, folder=note.folder, title=note.display_title,
            kind=note.kind, created=export.fmt_time(note.created),
            modified=export.fmt_time(note.modified),
            blocks=sum(1 for b in doc.blocks if not isinstance(b, content.ImageBlock)),
            images=sum(1 for b in doc.blocks if isinstance(b, content.ImageBlock)),
            attachments=len(refs), md='note.md' if 'md' in self.formats else '',
            docx='note.docx' if 'docx' in self.formats else '',
            html='note.html' if 'html' in self.formats else '',
            path=note_dir_rel.replace('\\', '/'),
        ))
        self._rows_by_path[note_dir_rel.replace('\\', '/')] = self.rows[-1]
        self._manifest.record(
            note, content_key, note_dir_rel,
            files=[FORMAT_FILE[f] for f in self.formats],
            assets=sorted(copied), fingerprint=fingerprint)
        self.stat['notes'] += 1

    # -- 清单里的旧笔记，补一条目录行 ------------------------------------
    def _index_row(self, note: bakfile.Note, rel_dir: str) -> dict:
        """给「这次没重写」的旧笔记生成目录行：格式按磁盘上真实存在的文件来。"""
        base = os.path.join(self.out_root, *rel_dir.replace('\\', '/').split('/'))
        return dict(
            note_id=note.note_id, folder=note.folder, title=note.display_title,
            kind=note.kind, created=export.fmt_time(note.created),
            modified=export.fmt_time(note.modified), blocks=0, images=0, attachments=0,
            md='note.md' if os.path.isfile(os.path.join(base, 'note.md')) else '',
            docx='note.docx' if os.path.isfile(os.path.join(base, 'note.docx')) else '',
            html='note.html' if os.path.isfile(os.path.join(base, 'note.html')) else '',
            path=rel_dir.replace('\\', '/'),
        )

    # -- 生成目录页（含本次没重写的旧笔记）-------------------------------
    def _index_rows(self) -> List[dict]:
        """目录页要包含全部笔记：本次新写的 + 清单里已有但跳过的。"""
        return list(self._rows_by_path.values())

    # -- 全部导出 -------------------------------------------------------
    def _check_formats(self) -> None:
        """按运行环境调整格式：没装 python-docx 时能出什么出什么，而不是直接崩掉。"""
        if 'docx' not in self.formats:
            return
        try:
            import docx  # noqa: F401
            return
        except Exception:
            pass
        rest = [f for f in self.formats if f != 'docx']
        if not rest:
            raise SystemExit('需要 Word 格式，但没有安装 python-docx。\n'
                             '  安装：pip install python-docx\n'
                             '  或者改用：-f md / -f html / -f md,html')
        print('提示：没有安装 python-docx，本次跳过 Word 导出，其余格式照常。\n'
              '      需要 .docx 请先执行：pip install python-docx')
        self.formats = rest

    def run(self, bak_path: str) -> dict:
        self._check_formats()
        try:
            with bakfile.MiuiBackup(bak_path) as backup:
                if backup.encrypted:
                    raise SystemExit('这个备份是加密的（头部加密标志为 1），本工具无法离线解析。'
                                     '请在手机上备份时不设密码，或先解密再试。')
                notes, raw = bakfile.load_notes(backup)
                attachments = backup.attachments()
        except bakfile.BackupFormatError as exc:
            raise SystemExit('无法解析这个备份：%s\n'
                             '请确认选的是「笔记」应用的备份（com.miui.notes）。' % exc)

        resolver = att_mod.Resolver(sorted(attachments))
        self.stat['folders'] = sum(1 for n in notes if n.is_folder)

        os.makedirs(self.out_root, exist_ok=True)
        if self.keep_raw:
            for name, blob in attachments.items():
                path = os.path.join(self._raw_dir, *name.replace('\\', '/').split('/'))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'wb') as fh:
                    fh.write(blob)

        # 一律先读旧清单：① 合并模式要拿它查重；② 不合并时也要拿它避开已有目录名，
        # 否则同一篇笔记会被导到「标题」和「标题 (2)」两个目录里。
        self._manifest = manifest.Manifest.load(self.out_root)
        self.stat['pruned'] = self._manifest.prune_missing()
        self._plan_notes(notes, attachments, resolver, raw)

        for note in notes:
            if note.is_folder:
                continue
            self.export_note(note, attachments, resolver, raw)

        if self.copy_unreferenced:
            left = [n for n in sorted(attachments) if n not in self.referenced]
            self.stat['unreferenced'] = len(left)
            for name in left:
                path = os.path.join(self._raw_dir, 'unreferenced',
                                    *name.replace('\\', '/').split('/'))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'wb') as fh:
                    fh.write(attachments[name])
                self.stat['unreferenced_copied'] += 1

        if self.merge:
            self._manifest.save()
        if self.index_csv:
            rows = self._index_rows()
            export.write_index_csv(os.path.join(self.out_root, 'index.csv'), rows)
            export.write_index_html(os.path.join(self.out_root, 'index.html'), rows,
                                    self.formats)
            self.stat['indexed'] = len(rows)
        return self.stat

# ---------------------------------------------------------------------------
# 只为已有导出目录重建清单与目录页（不重新导出任何笔记）
# ---------------------------------------------------------------------------

def unzip_bak(source: str, workdir: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """`prepare_input` 的同义函数，语义更直白：把 zip 里的 .bak 取出来。"""
    return prepare_input(source, workdir)


def _note_md_paths(out_root: str) -> Dict[str, str]:
    """扫描导出目录，返回 相对目录 -> note.md 全文（用来按内容认领被改名的笔记）。"""
    found: Dict[str, str] = {}
    for cur, dirs, files in os.walk(out_root):
        dirs[:] = [d for d in dirs if d not in (ASSET_DIRNAME, 'raw_attachments')]
        if 'note.md' not in files:
            continue
        rel = os.path.relpath(cur, out_root).replace('\\', '/')
        try:
            with open(os.path.join(cur, 'note.md'), 'r', encoding='utf-8') as fh:
                found[rel] = fh.read()
        except OSError:
            continue
    return found


def _note_body(text: str) -> str:
    """取 note.md 的正文部分（跳过开头的 # 标题与 - 元信息行），用于按内容认领目录。"""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (lines[i].startswith('# ') or lines[i].startswith('- ')
                              or not lines[i].strip()):
        i += 1
    return '\n'.join(lines[i:]).strip()


def _body_map(out_root: str) -> Dict[str, List[str]]:
    """扫描导出目录：note.md 的正文 -> 出现它的相对目录列表。"""
    out: Dict[str, List[str]] = {}
    for rel, text in _note_md_paths(out_root).items():
        body = _note_body(text)
        if body:
            out.setdefault(body, []).append(rel)
    return out


def _match_by_content(note, doc: content.NoteDocument, out_root: str,
                      claimed: Optional[set] = None,
                      bodies: Optional[Dict[str, List[str]]] = None) -> Optional[str]:
    """备份里改了标题或目录名、磁盘目录还是旧名字时，用 note.md 正文认领已有目录。

    `claimed` 里是本次已认领走的目录，避免两个笔记抢同一个目录。
    """
    body = _note_body(export.note_markdown(note, doc, {}, []))
    if not body:
        return None
    table = bodies if bodies is not None else _body_map(out_root)
    for rel in table.get(body, ()):
        if claimed is not None and rel in claimed:
            continue
        return rel
    return None


def rebuild_manifest(bak_path: str, out_root: str, formats: Sequence[str]) -> dict:
    """给「没有清单的老导出目录」补一份清单和目录页。

    只登记磁盘上确实存在对应 note.md/note.docx/note.html 的笔记，
    因此不会把不存在的文件写进清单，也不会改动任何笔记内容。
    """
    with bakfile.MiuiBackup(bak_path) as backup:
        notes, raw = bakfile.load_notes(backup)
        attachments = backup.attachments()
    resolver = att_mod.Resolver(sorted(attachments))

    mf = manifest.Manifest.load(out_root)
    stat = dict(folders=0, entries=0, skipped_existing=0, missing=0, pruned=0,
                notes=0, md=0, docx=0, html=0, assets=0, unreferenced=0,
                unreferenced_copied=0, skipped=0, indexed=0)
    stat['folders'] = sum(1 for n in notes if n.is_folder)

    prefer = {e.content_key: e.path for e in mf.entries if e.content_key and e.path}
    content_keys: Dict[str, str] = {}
    for note in notes:
        if note.is_folder:
            continue
        doc = content.parse_content(note.content, list(attachments))
        refs = att_mod.collect_refs(note, doc, resolver, raw)
        for ent in att_mod.handwrite_preview(note, resolver):
            if ent not in refs:
                refs.append(ent)
        content_keys[str(note.note_id)] = manifest.content_fingerprint(
            note, manifest.attachment_hashes(refs, attachments))
    used = manifest.scan_existing(out_root)
    plan = manifest.allocate_dirs(notes, prefer, used, content_keys)

    rows: List[dict] = []
    claimed: set = set()
    for note, rel_dir in plan:
        doc = content.parse_content(note.content, list(attachments))
        refs = att_mod.collect_refs(note, doc, resolver, raw)
        preview_of = att_mod.handwrite_preview(note, resolver)
        for ent in preview_of:
            if ent not in refs:
                refs.append(ent)
        hashes = manifest.attachment_hashes(refs, attachments)
        content_key = content_keys.get(str(note.note_id)) or \
            manifest.content_fingerprint(note, hashes)
        fingerprint = manifest.note_fingerprint(note, hashes)
        if mf.existing_path(content_key, [], verify=False) is not None:
            stat['skipped_existing'] += 1

        base = os.path.join(out_root, *rel_dir.split('/'))
        files = [FORMAT_FILE[f] for f in formats
                 if os.path.isfile(os.path.join(base, FORMAT_FILE[f]))]
        if not os.path.isdir(base) or ('md' in formats and 'note.md' not in files):
            # 备份里改了标题/目录名，磁盘上还是旧目录名：按 note.md 正文认领它
            hit = _match_by_content(note, doc, out_root, claimed)
            if hit is not None:
                rel_dir = hit
                base = os.path.join(out_root, *rel_dir.split('/'))
                files = [FORMAT_FILE[f] for f in formats
                         if os.path.isfile(os.path.join(base, FORMAT_FILE[f]))]
                stat['renamed'] = stat.get('renamed', 0) + 1
        claimed.add(rel_dir)
        if not files:
            stat['missing'] += 1
            continue
        assets_dir = os.path.join(base, ASSET_DIRNAME)
        assets = sorted(os.listdir(assets_dir)) if os.path.isdir(assets_dir) else []
        mf.record(note, content_key, rel_dir, files, assets, fingerprint)
        stat['entries'] += 1
        rows.append(dict(
            note_id=note.note_id, folder=note.folder, title=note.display_title,
            kind=note.kind, created=export.fmt_time(note.created),
            modified=export.fmt_time(note.modified),
            blocks=sum(1 for b in doc.blocks if not isinstance(b, content.ImageBlock)),
            images=sum(1 for b in doc.blocks if isinstance(b, content.ImageBlock)),
            attachments=len(refs),
            md='note.md' if 'note.md' in files else '',
            docx='note.docx' if 'note.docx' in files else '',
            html='note.html' if 'note.html' in files else '',
            path=rel_dir))

    stat['pruned'] = mf.prune_missing()
    mf.save()
    if rows:
        export.write_index_csv(os.path.join(out_root, 'index.csv'), rows)
        export.write_index_html(os.path.join(out_root, 'index.html'), rows, formats)
        stat['indexed'] = len(rows)
    stat['notes'] = stat['entries']
    return stat


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='miui-notes-export',
        description='把小米澎湃OS 笔记备份导出为 Markdown / Word / HTML（保留图片等附件）')
    p.add_argument('source', help='备份包 .zip 或 .bak（也接受含 .bak 的目录）')
    p.add_argument('-o', '--out', default='notes-export', help='输出目录（默认 notes-export）')
    p.add_argument('-f', '--format', default='both',
                   help='导出格式：md / docx / html，可用逗号组合（如 md,html）；'
                        'both=md+docx（默认），all=三种都要')
    p.add_argument('--keep-raw', action='store_true',
                   help='额外原样释放全部 miui_att 附件（含无法解析的手写笔迹）')
    p.add_argument('--copy-unreferenced', action='store_true',
                   help='把没有被任何笔记引用的附件也复制到 raw_attachments/unreferenced/')
    p.add_argument('--workdir', default=None, help='解压 zip 时使用的临时目录')
    p.add_argument('--no-index', action='store_true', help='不生成 index.csv / index.html')
    p.add_argument('--merge', dest='merge', action='store_true', default=True,
                   help='与输出目录里已有的笔记合并：内容完全相同的笔记不再重复导出（默认开启）')
    p.add_argument('--no-merge', dest='merge', action='store_false',
                   help='关闭合并，输出目录里已经有的同样内容也重新写一份')
    p.add_argument('--rebuild-index', action='store_true',
                   help='只给输出目录重建清单（export-manifest.json）和目录页，不重新导出笔记')
    p.add_argument('--diagnose', action='store_true',
                   help='只体检备份包结构并输出诊断报告（不含笔记正文），用于换设备后排查')
    p.add_argument('--version', action='version', version='%(prog)s ' + VERSION)
    return p


FORMAT_ALIASES = {'both': ['md', 'docx'], 'all': ['md', 'docx', 'html']}
VALID_FORMATS = ('md', 'docx', 'html')


def parse_formats(value: str) -> List[str]:
    """把 `-f` 的值解析成格式列表，支持 md,html 这种逗号组合。"""
    key = (value or '').strip().lower()
    if key in FORMAT_ALIASES:
        return list(FORMAT_ALIASES[key])
    out: List[str] = []
    for part in key.replace('，', ',').split(','):
        part = part.strip()
        if not part:
            continue
        if part not in VALID_FORMATS:
            raise SystemExit('不认识的格式：%s（可选 md / docx / html，也可用 both / all）' % part)
        if part not in out:
            out.append(part)
    if not out:
        raise SystemExit('-f 至少要指定一种格式（md / docx / html）')
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    formats = parse_formats(args.format)
    source = os.path.abspath(args.source)
    if not os.path.exists(source):
        print('找不到输入：%s' % source, file=sys.stderr)
        return 2
    out_root = os.path.abspath(args.out)
    if not args.diagnose:
        print('输入：%s' % source)
        print('输出：%s' % out_root)
    bak_path, cleanup = prepare_input(source, args.workdir)
    try:
        if args.diagnose:
            report = diagnose_mod.diagnose(bak_path)
            diagnose_mod.print_report(report)
            ok = not report.get('errors')
            return 0 if ok else 1
        if args.rebuild_index:
            stat = rebuild_manifest(bak_path, out_root, formats)
        else:
            exporter = Exporter(out_root, formats, args.keep_raw,
                                args.copy_unreferenced, not args.no_index,
                                merge=args.merge)
            stat = exporter.run(bak_path)
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)
    if args.rebuild_index:
        print('重建清单：登记 %d 篇（磁盘上缺少文件的 %d 篇，清理失效记录 %d 条）'
              % (stat['entries'], stat['missing'], stat['pruned']))
        print('目录页：%s' % os.path.join(out_root, 'index.html'))
        return 0
    print('完成：笔记 %d 篇 / 目录 %d 个；Markdown %d，Word %d，HTML %d；'
          '附件落盘 %d，正文缺失引用 %d' % (
              stat['notes'], stat['folders'], stat['md'], stat['docx'], stat['html'],
              stat['assets'], stat['missing']))
    if stat.get('skipped'):
        print('合并：%d 篇内容与已有文件完全相同，没有重复导出（清单共 %d 篇）'
              % (stat['skipped'], stat.get('indexed', stat['notes'])))
    if stat['unreferenced']:
        print('未被引用的附件 %d 个%s' % (
            stat['unreferenced'],
            '，已复制到 raw_attachments/unreferenced/' if args.copy_unreferenced else ''))
    if not args.no_index:
        print('目录页：%s' % os.path.join(out_root, 'index.html'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

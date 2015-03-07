#!/usr/bin/env python2
# vim:fileencoding=utf-8
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__ = 'GPL v3'
__copyright__ = '2015, Kovid Goyal <kovid at kovidgoyal.net>'

import os, uuid
from threading import Thread
from functools import partial

from PyQt5.Qt import (
    QApplication, QStackedLayout, QVBoxLayout, QWidget, QLabel, Qt,
    QListWidget, QSize, pyqtSignal, QListWidgetItem, QIcon, QByteArray,
    QBuffer, QPixmap, QAction, QKeySequence)

from calibre import as_unicode
from calibre.constants import iswindows, isosx
from calibre.gui2 import error_dialog, choose_files, choose_images, elided_text, sanitize_env_vars
from calibre.gui2.widgets2 import Dialog
from calibre.gui2.progress_indicator import ProgressIndicator
from calibre.utils.config import JSONConfig
from calibre.utils.icu import numeric_sort_key as sort_key

ENTRY_ROLE = Qt.UserRole

def pixmap_to_data(pixmap):
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.WriteOnly)
    pixmap.save(buf, 'PNG')
    return bytearray(ba.data())

def run_program(entry, path, parent):
    import subprocess
    cmdline = entry_to_cmdline(entry, path)
    print('Running Open With commandline:', repr(cmdline))
    try:
        with sanitize_env_vars():
            process = subprocess.Popen(cmdline)
    except Exception as err:
        return error_dialog(
            parent, _('Failed to run'), _(
            'Failed to run program, click "Show Details" for more information'),
            det_msg='Command line: %r\n%s' %(cmdline, as_unicode(err)))
    t = Thread(name='WaitProgram', target=process.wait)
    t.daemon = True
    t.start()

def entry_to_icon_text(entry, only_text=False):
    if only_text:
        return entry.get('name', entry.get('Name')) or _('Unknown')
    data = entry.get('icon_data')
    if data is None:
        icon = QIcon(I('blank.png'))
    else:
        pmap = QPixmap()
        pmap.loadFromData(bytes(data))
        icon = QIcon(pmap)
    return icon, entry.get('name', entry.get('Name')) or _('Unknown')

if iswindows:
    # Windows {{{
    from calibre.utils.winreg.default_programs import find_programs, friendly_app_name
    from calibre.utils.open_with.windows import load_icon_resource
    from win32process import CreateProcess, STARTUPINFO
    from win32event import WaitForInputIdle
    import win32con
    oprefs = JSONConfig('windows_open_with')

    def entry_sort_key(entry):
        return sort_key(entry.get('name') or '')

    def finalize_entry(entry):
        data = load_icon_resource(entry.pop('icon_resource', None), as_data=True)
        if data:
            entry['icon_data'] = data
        return entry

    def entry_to_item(entry, parent):
        icon = load_icon_resource(entry.get('icon_resource'))
        if not icon:
            icon = entry_to_icon_text(entry)[0]
        ans = QListWidgetItem(QIcon(icon), entry.get('name') or _('Unknown'), parent)
        ans.setData(ENTRY_ROLE, entry)
        ans.setToolTip(_('Command line:') + '\n' + entry['cmdline'])

    def choose_manually(filetype, parent):
        ans = choose_files(
            parent, 'choose-open-with-program-manually-win',
            _('Choose a program to open %s files') % filetype.upper(),
            filters=[(_('Executable files'), ['exe', 'bat', 'com'])], select_only_single_file=True)
        if ans:
            ans = os.path.abspath(ans[0])
            if not os.access(ans, os.X_OK):
                return error_dialog(parent, _('Cannot execute'), _(
                    'The program %s is not an executable file') % ans, show=True)
            qans = ans.replace('"', r'\"')
            name = friendly_app_name(exe=ans) or os.path.splitext(os.path.basename(ans))[0]
            return {'cmdline':'"%s" "%%1"' % qans, 'name':name, 'icon_resource':ans + ',0'}

    def entry_to_cmdline(entry, path):
        cmdline = entry['cmdline']
        qpath = path.replace('"', r'\"')
        return cmdline.replace('%1', qpath)

    del run_program
    def run_program(entry, path, parent):
        cmdline = entry_to_cmdline(entry, path)
        print('Running Open With commandline:', repr(entry['cmdline']), ' |==> ', repr(cmdline))
        try:
            with sanitize_env_vars():
                process_handle, thread_handle, process_id, thread_id = CreateProcess(
                    None, cmdline, None, None, False, win32con.CREATE_DEFAULT_ERROR_MODE | win32con.CREATE_NEW_PROCESS_GROUP | win32con.DETACHED_PROCESS,
                    None, None, STARTUPINFO())
            WaitForInputIdle(process_handle, 2000)
        except Exception as err:
            return error_dialog(
                parent, _('Failed to run'), _(
                'Failed to run program, click "Show Details" for more information'),
                det_msg='Command line: %r\n%s' %(cmdline, as_unicode(err)))
    # }}}

elif isosx:
    oprefs = JSONConfig('osx_open_with')
else:
    # XDG {{{
    oprefs = JSONConfig('xdg_open_with')
    from calibre.utils.open_with.linux import entry_to_cmdline, find_programs, entry_sort_key

    def entry_to_item(entry, parent):
        icon_path = entry.get('Icon') or I('blank.png')
        ans = QListWidgetItem(QIcon(icon_path), entry.get('Name') or _('Unknown'), parent)
        ans.setData(ENTRY_ROLE, entry)
        comment = (entry.get('Comment') or '')
        if comment:
            comment += '\n'
        ans.setToolTip(comment + _('Command line:') + '\n' + (' '.join(entry['Exec'])))

    def choose_manually(filetype, parent):
        ans = choose_files(parent, 'choose-open-with-program-manually', _('Choose a program to open %s files') % filetype.upper(), select_only_single_file=True)
        if ans:
            ans = ans[0]
            if not os.access(ans, os.X_OK):
                return error_dialog(parent, _('Cannot execute'), _(
                    'The program %s is not an executable file') % ans, show=True)
            return {'Exec':[ans, '%f'], 'Name':os.path.basename(ans)}

    def finalize_entry(entry):
        icon_path = entry.get('Icon')
        if icon_path:
            ic = QIcon(icon_path)
            if not ic.isNull():
                pmap = ic.pixmap(48, 48)
                if not pmap.isNull():
                    entry['icon_data'] = pixmap_to_data(pmap)
        entry['MimeType'] = tuple(entry['MimeType'])
        return entry
# }}}

class ChooseProgram(Dialog):  # {{{

    found = pyqtSignal()

    def __init__(self, file_type='jpeg', parent=None, prefs=oprefs):
        self.file_type = file_type
        self.programs = self.find_error = self.selected_entry = None
        self.select_manually = False
        Dialog.__init__(self, _('Choose a program'), 'choose-open-with-program-dialog', parent=parent, prefs=prefs)
        self.found.connect(self.programs_found, type=Qt.QueuedConnection)
        self.pi.startAnimation()
        t = Thread(target=self.find_programs)
        t.daemon = True
        t.start()

    def setup_ui(self):
        self.stacks = s = QStackedLayout(self)
        self.w = w = QWidget(self)
        self.w.l = l = QVBoxLayout(w)
        self.pi = pi = ProgressIndicator(self, 256)
        l.addStretch(1), l.addWidget(pi, alignment=Qt.AlignHCenter), l.addSpacing(10)
        w.la = la = QLabel(_('Gathering data, please wait...'))
        la.setStyleSheet('QLabel { font-size: 30pt; font-weight: bold }')
        l.addWidget(la, alignment=Qt.AlignHCenter), l.addStretch(1)
        s.addWidget(w)

        self.w2 = w = QWidget(self)
        self.l = l = QVBoxLayout(w)
        s.addWidget(w)

        self.la = la = QLabel(_('Choose a program to open %s files') % self.file_type.upper())
        self.plist = pl = QListWidget(self)
        pl.setIconSize(QSize(48, 48)), pl.setSpacing(5)
        pl.doubleClicked.connect(self.accept)
        l.addWidget(la), l.addWidget(pl)
        la.setBuddy(pl)

        b = self.bb.addButton(_('&Browse computer for program'), self.bb.ActionRole)
        b.clicked.connect(self.manual)
        l.addWidget(self.bb)

    def sizeHint(self):
        return QSize(600, 500)

    def find_programs(self):
        try:
            self.programs = find_programs(self.file_type.split())
        except Exception:
            import traceback
            self.find_error = traceback.print_exc()
        self.found.emit()

    def programs_found(self):
        if self.find_error is not None:
            error_dialog(self, _('Error finding programs'), _(
                'Failed to find programs on your computer, click "Show details" for'
                ' more information'), det_msg=self.find_error, show=True)
            self.select_manually = True
            return self.reject()
        if not self.programs:
            self.select_manually = True
            return self.reject()
        for entry in self.programs:
            entry_to_item(entry, self.plist)
        self.stacks.setCurrentIndex(1)

    def accept(self):
        ci = self.plist.currentItem()
        if ci is not None:
            self.selected_entry = ci.data(ENTRY_ROLE)
        return Dialog.accept(self)

    def manual(self):
        self.select_manually = True
        self.reject()

oprefs.defaults['entries'] = {}

def choose_program(file_type='jpeg', parent=None, prefs=oprefs):
    oft = file_type = file_type.lower()
    file_type = {'cover_image':'jpeg'}.get(oft, oft)
    d = ChooseProgram(file_type, parent, prefs)
    d.exec_()
    entry = choose_manually(file_type, parent) if d.select_manually else d.selected_entry
    if entry is not None:
        entry = finalize_entry(entry)
        entry['uuid'] = type('')(uuid.uuid4())
        entries = oprefs['entries']
        if oft not in entries:
            entries[oft] = []
        entries[oft].append(entry)
        entries[oft].sort(key=entry_sort_key)
        oprefs['entries'] = entries
        register_keyboard_shortcuts(finalize=True)
    return entry

def populate_menu(menu, receiver, file_type):
    file_type = file_type.lower()
    for entry in oprefs['entries'].get(file_type, ()):
        icon, text = entry_to_icon_text(entry)
        text = elided_text(text, pos='right')
        sa = registered_shortcuts.get(entry['uuid'])
        if sa is not None:
            text += '\t' + sa.shortcut().toString(QKeySequence.NativeText)
        ac = menu.addAction(icon, text)

        ac.triggered.connect(partial(receiver, entry))
    return menu

# }}}

class EditPrograms(Dialog):  # {{{

    def __init__(self, file_type='jpeg', parent=None):
        self.file_type = file_type.lower()
        Dialog.__init__(self, _('Edit the applications for %s files') % file_type.upper(), 'edit-open-with-programs', parent=parent)

    def setup_ui(self):
        self.l = l = QVBoxLayout(self)
        self.plist = pl = QListWidget(self)
        pl.setIconSize(QSize(48, 48)), pl.setSpacing(5)
        l.addWidget(pl)

        self.bb.clear(), self.bb.setStandardButtons(self.bb.Close)
        self.rb = b = self.bb.addButton(_('&Remove'), self.bb.ActionRole)
        b.clicked.connect(self.remove), b.setIcon(QIcon(I('list_remove.png')))
        self.cb = b = self.bb.addButton(_('Change &Icon'), self.bb.ActionRole)
        b.clicked.connect(self.change_icon), b.setIcon(QIcon(I('icon_choose.png')))
        l.addWidget(self.bb)

        self.populate()

    def sizeHint(self):
        return QSize(600, 400)

    def populate(self):
        self.plist.clear()
        for entry in oprefs['entries'].get(self.file_type, ()):
            entry_to_item(entry, self.plist)

    def change_icon(self):
        ci = self.plist.currentItem()
        if ci is None:
            return error_dialog(self, _('No selection'), _(
                'No application selected'), show=True)
        paths = choose_images(self, 'choose-new-icon-for-open-with-program', _(
            'Choose new icon'))
        if paths:
            ic = QIcon(paths[0])
            if ic.isNull():
                return error_dialog(self, _('Invalid icon'), _(
                    'Could not load image from %s') % paths[0], show=True)
            pmap = ic.pixmap(48, 48)
            if not pmap.isNull():
                entry = ci.data(ENTRY_ROLE)
                entry['icon_data'] = pixmap_to_data(pmap)
                ci.setData(ENTRY_ROLE, entry)
                self.update_stored_config()
                ci.setIcon(ic)

    def remove(self):
        ci = self.plist.currentItem()
        if ci is None:
            return error_dialog(self, _('No selection'), _(
                'No application selected'), show=True)
        row = self.plist.row(ci)
        self.plist.takeItem(row)
        self.update_stored_config()
        register_keyboard_shortcuts(finalize=True)

    def update_stored_config(self):
        entries = [self.plist.item(i).data(ENTRY_ROLE) for i in xrange(self.plist.count())]
        oprefs['entries'][self.file_type] = entries
        oprefs['entries'] = oprefs['entries']

def edit_programs(file_type, parent):
    d = EditPrograms(file_type, parent)
    d.exec_()
# }}}

registered_shortcuts = {}

def register_keyboard_shortcuts(gui=None, finalize=False):
    if gui is None:
        from calibre.gui2.ui import get_gui
        gui = get_gui()
    if gui is None:
        return
    for unique_name, action in registered_shortcuts.iteritems():
        gui.keyboard.unregister_shortcut(unique_name)
        gui.removeAction(action)
    registered_shortcuts.clear()

    for filetype, applications in oprefs['entries'].iteritems():
        for application in applications:
            text = entry_to_icon_text(application, only_text=True)
            t = _('cover image') if filetype.upper() == 'COVER_IMAGE' else filetype.upper()
            name = _('Open %s files with %s') % (t, text)
            ac = QAction(gui)
            unique_name = application['uuid']
            ac.triggered.connect(partial(gui.open_with_action_triggerred, filetype, application))
            gui.keyboard.register_shortcut(unique_name, name, action=ac, group=_('Open With'))
            gui.addAction(ac)
            registered_shortcuts[unique_name] = ac
    if finalize:
        gui.keyboard.finalize()

if __name__ == '__main__':
    from pprint import pprint
    app = QApplication([])
    pprint(choose_program('pdf'))
    del app

# -*- coding: utf-8 -*-
from textwrap import dedent
import maya.cmds as cmds
import maya.mel as mel
import maya.utils
from . import siweighteditor
from . import qt

MAIN_WINDOW_MENU = 'MayaWindow|mainWindowMenu'
MENU_PREFIX = 'siweighteditor_improve_'
_STARTUP_EXECUTED = False


def _delete_menu_ui(item_path):
    """メニュー項目またはサブメニューを種類に応じて安全に削除する。"""
    for kwargs in ({'menuItem': True}, {'menu': True}, {}):
        try:
            if cmds.menuItem(item_path, exists=True) or cmds.menu(item_path, exists=True):
                cmds.deleteUI(item_path, **kwargs)
            if not (cmds.menuItem(item_path, exists=True) or cmds.menu(item_path, exists=True)):
                return True
        except Exception:
            pass
    return False


def remove_menu():
    """メインウィンドウメニューに登録した SiWeightEditor Improve 項目を確実に削除する。"""
    global _STARTUP_EXECUTED
    _STARTUP_EXECUTED = False

    if cmds.menu(MAIN_WINDOW_MENU, exists=True):
        try:
            items = cmds.menu(MAIN_WINDOW_MENU, query=True, itemArray=True) or []
        except Exception:
            items = []
        for item in list(items):
            short_name = str(item).split('|')[-1]
            if short_name.startswith(MENU_PREFIX):
                full_path = '{0}|{1}'.format(MAIN_WINDOW_MENU, short_name)
                _delete_menu_ui(full_path)

    for item_name in ('siweighteditor_improve_open', 'siweighteditor_improve_folder', 'siweighteditor_improve_divider'):
        _delete_menu_ui(item_name)


def _is_menu_ready():
    """Windows メニュー内にサブメニューおよび起動用子項目が正しく存在しているか確認する。"""
    if not cmds.menu(MAIN_WINDOW_MENU, exists=True):
        return False
    try:
        items = cmds.menu(MAIN_WINDOW_MENU, query=True, itemArray=True) or []
    except Exception:
        return False
    for item in items:
        short_name = str(item).split('|')[-1]
        if short_name.startswith('siweighteditor_improve_folder'):
            folder_path = '{0}|{1}'.format(MAIN_WINDOW_MENU, short_name)
            try:
                sub_items = cmds.menu(folder_path, query=True, itemArray=True) or []
                if sub_items:
                    return True
            except Exception:
                pass
    return False


def menu_setup():
    # Maya ウィンドウが見つからない場合は処理しない
    if not qt.get_maya_window():
        return False

    cmd = '''
    buildViewMenu MayaWindow|mainWindowMenu;
    setParent -menu "MayaWindow|mainWindowMenu";
    '''

    mel.eval(cmd)

    if not cmds.menu(MAIN_WINDOW_MENU, exists=True):
        return False

    # 多重登録や空サブメニュー残骸を防ぐため既存メニューを先にすべて削除する
    remove_menu()

    cmds.menuItem('siweighteditor_improve_divider', divider=True, parent=MAIN_WINDOW_MENU)
    folder_menu = cmds.menuItem(
        'siweighteditor_improve_folder',
        label='SiWeightEditor Improve',
        subMenu=True,
        tearOff=True,
        parent=MAIN_WINDOW_MENU
    )

    cmds.menuItem(
        'siweighteditor_improve_open',
        label=jpn('SiWeightEditor Improve'),
        annotation="open SiWeightEditor Improve",
        parent=folder_menu,
        echoCommand=True,
        command=dedent(
            '''
                import siweighteditor_improve.siweighteditor
                siweighteditor_improve.siweighteditor.Option()
            ''').strip()
    )

    try:
        cmds.setParent(MAIN_WINDOW_MENU, menu=True)
    except Exception:
        pass
    return True


def register_runtime_command(opt):

    # check if command already exists, then skip register
    runtime_cmd = dedent('''
        runTimeCommand
            -annotation "{annotation}"
            -category "{category}"
            -commandLanguage "{commandLanguage}"
            -command ({command})
            {cmd_name};
    ''')

    name_cmd = dedent('''
        nameCommand
            -annotation "{annotation}"
            -sourceType "{commandLanguage}"
            -command ("{cmd_name}")
            {cmd_name}NameCommand;
    ''')

    exits = mel.eval('''exists "{}";'''.format(opt['cmd_name']))
    if exits:
        #print('custom command is already exist : *-*-*-*-*-*-*-*-*-*-*-*-*-***-**-*')
        return
    try:
        mel.eval(runtime_cmd.format(**opt))
        mel.eval(name_cmd.format(**opt))

    except Exception as e:
        print(opt['cmd_name'])
        print(opt['command'])
        raise e


def register_siwieighteditor_runtime_command():
    opts = {
        'annotation':      "Open SiWeightEditor Improve",
        'category':        "SiWeightEditorImprove",
        'commandLanguage': "python",
        'command':         r'''"import importlib\rfrom siweighteditor_improve import siweighteditor\rimportlib.reload(siweighteditor)\rsiweighteditor.Option()"''',
        'cmd_name':        "OpenSiWeightEditorImprove"
    }
    register_runtime_command(opts)


def jpn(string):
    # Python 3 環境ではそのまま文字列を返し、Python 2 環境のみ cp932 へ変換する
    try:
        if isinstance(string, bytes):
            return string.decode("utf-8").encode("cp932")
        return string
    except Exception:
        return string


def execute(force=False):
    global _STARTUP_EXECUTED
    if _STARTUP_EXECUTED and not force and _is_menu_ready():
        return True
    ok = menu_setup()
    register_siwieighteditor_runtime_command()
    if ok:
        _STARTUP_EXECUTED = True
    return ok
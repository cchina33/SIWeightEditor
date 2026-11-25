# -*- coding: utf-8 -*-
from textwrap import dedent
import maya.cmds as cmds
import maya.mel as mel
import maya.utils
from . import siweighteditor
from . import qt

def remove_menu():
    """メインウィンドウメニューに登録した SiWeightEditor Improve 項目を削除する。"""
    for item_name in ('siweighteditor_improve_open', 'siweighteditor_improve_folder', 'siweighteditor_improve_divider'):
        try:
            if cmds.menuItem(item_name, exists=True):
                cmds.deleteUI(item_name, menuItem=True)
        except Exception:
            pass


def menu_setup():
    # Maya ウィンドウが見つからない場合は処理しない
    if not qt.get_maya_window():
        return

    cmd = '''
    buildViewMenu MayaWindow|mainWindowMenu;
    setParent -menu "MayaWindow|mainWindowMenu";
    '''

    mel.eval(cmd)

    # 多重登録を防ぐため既存メニューがあれば先に削除する
    remove_menu()

    cmds.menuItem('siweighteditor_improve_divider', divider=True, parent='MayaWindow|mainWindowMenu')
    cmds.menuItem(
        'siweighteditor_improve_folder',
        label='SiWeightEditor Improve',
        subMenu=True,
        tearOff=True,
        parent='MayaWindow|mainWindowMenu'
    )

    cmds.menuItem(
        'siweighteditor_improve_open',
        label=jpn('SiWeightEditor Improve'),
        annotation="open SiWeightEditor Improve",
        parent='siweighteditor_improve_folder',
        echoCommand=True,
        command=dedent(
            '''
                import siweighteditor_improve.siweighteditor
                siweighteditor_improve.siweighteditor.Option()
            ''')
    )


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


_STARTUP_EXECUTED = False


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
    if _STARTUP_EXECUTED and not force:
        return
    _STARTUP_EXECUTED = True
    menu_setup()
    register_siwieighteditor_runtime_command()
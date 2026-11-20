# -*- coding: utf-8 -*-
"""SIWeightEditor Improve の定数・共有状態・デバッグログ・プラグイン読み込み・ウィンドウ位置補助モジュール。"""

import datetime as dt
import os
import sys

from maya import cmds

from ..maya_version import MAYA_VER
from ..viewport import bone_visualizer
from .. import qt
from ..qt import *


PYTHON_VER = sys.version_info.major

VERSION = "r1.4.5"
TITLE = "SIWeightEditor Improve"
WINDOW_OBJECT_NAME = "SIWeightEditorImprove"
BAKE_PLUGIN_NAME = "bake_skin_weight_improve.py"

HELP_PATH = "https://github.com/ShikouYamaue/SIWeightEditor/blob/master/README.md"
REREASE_PATH = "https://github.com/ShikouYamaue/SIWeightEditor/releases"

WIDGET_HEIGHT = 26
BUTTON_HEIGHT = 22
COUNTER_PRINT = False

# エディタ全体で共有する実行時状態（ウィジェット・テーブルモデル・本体間で一元参照）
WINDOW = None
FLOAT_DECIMALS = 4
FLOAT_FORMAT = "{:.4f}"
MAXIMUM_WEIGHT = 100
MAXIMUM_INFLUENCE_COUNT = 3
ZERO_CELL_DARKEN = 60
WEIGHT_TRANSFER_MULTIPLE = None

# 焼き込みプラグイン（bake_skin_weight_improve.py）とのデータ受け渡し用スロット
BAKE_NODE_ID_DICT = None
BAKE_NODE_WEIGHT_DICT = None
BAKE_NODE_INF_DICT = None
NODE_SKINFN_DICT = None
UNDO_NODE_WEIGHT_DICT = None
REDO_NODE_WEIGHT_DICT = None
ORG_NODE_WEIGHT_DICT = None

DEBUG_LOG_PATHS = [
    r"C:\tmp\siwe_improve_debug.log",
    os.path.join(os.getenv("TEMP") or os.path.expanduser("~"), "siwe_improve_debug.log"),
    os.path.join(os.getenv("MAYA_APP_DIR") or os.path.expanduser("~"), "siwe_improve_debug.log"),
]
DEBUG_LOG_ACTIVE_PATH = None
DEBUG_LOG_MAX_SIZE = 2 * 1024 * 1024


def timer(func):
    """計測用デコレータ（通常時はオーバーヘッドなしでそのまま関数を返す）。"""
    return func


def set_window(win):
    """現在アクティブなメインウィンドウ参照を共有状態へ登録する。"""
    global WINDOW
    WINDOW = win
    return win


def set_maximum_weight(value):
    """表示単位の最大ウェイト（1.0 または 100.0）を更新する。"""
    global MAXIMUM_WEIGHT
    MAXIMUM_WEIGHT = value


def set_float_decimals(decimals):
    """表示小数点桁数とフォーマット文字列を同期更新する。"""
    global FLOAT_DECIMALS, FLOAT_FORMAT
    FLOAT_DECIMALS = int(decimals)
    FLOAT_FORMAT = "{:." + str(FLOAT_DECIMALS) + "f}"


def set_maximum_influence_count(limit):
    """警告ハイライト対象の最大インフルエンス数を更新する。"""
    global MAXIMUM_INFLUENCE_COUNT
    MAXIMUM_INFLUENCE_COUNT = int(limit)


def set_zero_cell_darken(darken_value):
    """ゼロウェイトセルの暗色化オフセット値を更新する。"""
    global ZERO_CELL_DARKEN
    ZERO_CELL_DARKEN = darken_value


def set_current_data(node_id, node_weight, node_inf, node_skin, undo_node_dict, redo_node_dict, org_node_dict):
    """焼き込みコマンドへ渡す直近のウェイト編集データを共有スロットへ格納する。"""
    global BAKE_NODE_ID_DICT
    global BAKE_NODE_WEIGHT_DICT
    global BAKE_NODE_INF_DICT
    global NODE_SKINFN_DICT
    global UNDO_NODE_WEIGHT_DICT
    global REDO_NODE_WEIGHT_DICT
    global ORG_NODE_WEIGHT_DICT
    BAKE_NODE_ID_DICT = node_id
    BAKE_NODE_WEIGHT_DICT = node_weight
    BAKE_NODE_INF_DICT = node_inf
    NODE_SKINFN_DICT = node_skin
    UNDO_NODE_WEIGHT_DICT = undo_node_dict
    REDO_NODE_WEIGHT_DICT = redo_node_dict
    ORG_NODE_WEIGHT_DICT = org_node_dict


def get_current_data():
    """焼き込みコマンド側から直近のウェイト編集データを取得する。"""
    return (
        BAKE_NODE_ID_DICT,
        BAKE_NODE_WEIGHT_DICT,
        BAKE_NODE_INF_DICT,
        NODE_SKINFN_DICT,
        UNDO_NODE_WEIGHT_DICT,
        REDO_NODE_WEIGHT_DICT,
        ORG_NODE_WEIGHT_DICT,
    )


def debug_log(message):
    """診断ログを書き込み可能なログファイルへ追記する。"""
    global DEBUG_LOG_ACTIVE_PATH
    try:
        timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        paths = [DEBUG_LOG_ACTIVE_PATH] if DEBUG_LOG_ACTIVE_PATH else []
        paths += [path for path in DEBUG_LOG_PATHS if path not in paths]
        for path in paths:
            if not path:
                continue
            try:
                log_dir = os.path.dirname(path)
                if log_dir and not os.path.exists(log_dir):
                    os.makedirs(log_dir)
                if os.path.exists(path) and os.path.getsize(path) > DEBUG_LOG_MAX_SIZE:
                    with open(path, "w") as f:
                        f.write("")
                with open(path, "a") as f:
                    f.write("{0} {1}\n".format(timestamp, message))
                DEBUG_LOG_ACTIVE_PATH = path
                return
            except Exception:
                continue
    except Exception:
        pass


def get_debug_log_path():
    """現在有効なデバッグログのパスを返す。"""
    return DEBUG_LOG_ACTIVE_PATH or DEBUG_LOG_PATHS[0]


def get_package_dir():
    """siweighteditor_improve パッケージ直下のディレクトリパスを返す。"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(current_dir, os.pardir))


def get_bundled_plugin_path(plugin_name):
    """パッケージ位置から相対パスで同梱プラグインのフルパスを返す。"""
    package_dir = get_package_dir()
    contents_dir = os.path.abspath(os.path.join(package_dir, os.pardir, os.pardir))
    return os.path.join(contents_dir, "plug-ins", plugin_name)


def load_bundled_plugin(plugin_name, autoload=False):
    """同梱プラグインを相対パス優先でロードする。"""
    try:
        loaded = cmds.pluginInfo(plugin_name, query=True, l=True)
    except Exception:
        loaded = False
    if not loaded:
        plugin_path = get_bundled_plugin_path(plugin_name)
        target = plugin_path if os.path.exists(plugin_path) else plugin_name
        cmds.loadPlugin(target, qt=True)
        if autoload:
            try:
                cmds.pluginInfo(plugin_name, e=True, autoload=True)
            except Exception:
                pass


def load_plugin():
    """焼き込みプラグインおよび 3D ボーン視覚化プラグインをロードする。"""
    try:
        load_bundled_plugin(BAKE_PLUGIN_NAME, autoload=False)
    except Exception as e:
        print("load plugin error :", "{}".format(e))
    try:
        load_bundled_plugin(bone_visualizer.PLUGIN_FILENAME, autoload=False)
    except Exception as e:
        print("load visualizer plugin error :", "{}".format(e))
    # ツールチップもあわせて有効化
    cmds.help(popupMode=True)


def clamp_window_position(x, y, width, height):
    """保存されたウィンドウ座標が画面外へ外れていた場合に表示可能領域内へ補正する。"""
    try:
        screens = QApplication.screens() or []
        if not screens:
            return int(x), int(y)
        pt = QPoint(int(x) + max(20, int(width) // 2), int(y) + 20)
        for scr in screens:
            if scr.availableGeometry().contains(pt):
                return int(x), int(y)
        primary = QApplication.primaryScreen() or screens[0]
        geo = primary.availableGeometry()
        clamped_x = max(geo.left(), min(int(x), geo.right() - max(120, min(int(width), geo.width()))))
        clamped_y = max(geo.top(), min(int(y), geo.bottom() - 80))
        return clamped_x, clamped_y
    except Exception:
        return int(x), int(y)


def ensure_maya_owned_floating_window(win):
    """非ドッキング時のメインウィンドウが Maya 本体より背面へ回らないようオーナー関係を保証する。"""
    maya_win = qt.get_maya_window()
    if maya_win is None or win is None:
        return
    try:
        if win.parent() is not maya_win:
            pos = win.pos()
            size = win.size()
            win.setParent(maya_win, Qt.Window)
            win.resize(size)
            win.move(pos)
            win.show()
        win.raise_()
        win.activateWindow()
    except Exception:
        pass

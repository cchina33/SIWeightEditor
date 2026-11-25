# -*- coding: utf-8 -*-
"""SIWeightEditor Improve のメインエントリポイント（ファサード・ライフサイクル・プラグイン窓口）。"""

import json
import os
import sys

from maya import cmds
import maya.OpenMayaUI as omui

try:
    from importlib import reload
except ImportError:
    from imp import reload

from .maya_version import MAYA_VER
from . import common
from . import lang
from . import qt
from . import freeze
from . import weight
from . import weight_transfer_multiple
from . import modeling
from . import symmetrize
from . import joint_rule_editor
from . import go
from . import prof
from . import smooth_setting_editor
from .viewport import bone_visualizer
from .viewport import joint_display_helper
from .tools import rig_cleanup_helper
from .tools import reference_joint_helper
from .tools import bone_picker_window
from .tools import bone_remap_window
from .tools import model_edit_window
from .tools import unweighted_bind_window

reload(smooth_setting_editor)
reload(prof)
reload(bone_visualizer)
reload(joint_display_helper)
reload(rig_cleanup_helper)
reload(reference_joint_helper)
reload(bone_picker_window)
reload(bone_remap_window)
reload(model_edit_window)
reload(unweighted_bind_window)

if MAYA_VER >= 2025:
    from PySide6.QtWidgets import *
    from PySide6.QtGui import *
    from PySide6.QtCore import *
elif 2017 <= MAYA_VER < 2025:
    from PySide2.QtWidgets import *
    from PySide2.QtGui import *
    from PySide2.QtCore import *
else:
    from PySide.QtGui import *
    from PySide.QtCore import *

if MAYA_VER >= 2016:
    from . import store_skin_weight_om2 as store_skin_weight
else:
    from . import store_skin_weight
reload(store_skin_weight)

from .editor import state as editor_state
from .editor import widgets as editor_widgets
from .editor import ui_builder as editor_ui_builder
from .editor import weight_ops as editor_weight_ops
from .editor import view_controller as editor_view_controller

reload(editor_state)
reload(editor_widgets)
reload(editor_ui_builder)
reload(editor_weight_ops)
reload(editor_view_controller)

from .editor.state import (
    PYTHON_VER,
    VERSION,
    TITLE,
    WINDOW_OBJECT_NAME,
    BAKE_PLUGIN_NAME,
    WIDGET_HEIGHT,
    BUTTON_HEIGHT,
    COUNTER_PRINT,
    HELP_PATH,
    REREASE_PATH,
    debug_log,
    get_debug_log_path,
    get_bundled_plugin_path,
    load_bundled_plugin,
    load_plugin,
    timer,
    clamp_window_position as _clamp_window_position,
    ensure_maya_owned_floating_window as _ensure_maya_owned_floating_window,
)
from .editor.widgets import (
    EditorDoubleSpinbox,
    EditorLineEdit,
    JointSearchDialog,
    EditorSpinbox,
    PopInputBox,
    RightClickTableView,
    MyHeaderView,
    set_table_header_width,
    TableModel,
    model_iter,
)
from .editor.ui_builder import EditorUIBuilderMixin
from .editor.weight_ops import EditorWeightOpsMixin
from .editor.view_controller import EditorViewControllerMixin

if PYTHON_VER >= 3:
    round = common.round_half_up

# 既存参照互換の共有状態ミラー
state = editor_state
WINDOW = None
select_job = None
select_job_ids = set()


def _get_window_save_file_path():
    """ウィンドウ設定 JSON ファイルの保存パスを返す。"""
    base_dir = os.getenv('MAYA_APP_DIR') or os.path.expanduser('~')
    return os.path.join(base_dir, 'Scripting_Files', 'siweighteditor_improve_window.json')


def _load_saved_dockable_flag():
    """ウィンドウを生成する前に保存済み JSON から dockable 設定だけを読み込む。"""
    save_file = _get_window_save_file_path()
    if not os.path.exists(save_file):
        return False
    try:
        with open(save_file, 'r') as fp:
            data = json.load(fp)
        return bool(data.get('dockable', False))
    except Exception:
        return False


def _close_existing_windows():
    """既存の SIWeightEditor Improve ウィンドウおよび残存 workspaceControl を 1 回で安全に閉じる。"""
    global WINDOW
    if WINDOW is not None:
        try:
            WINDOW.save_flag = False
            WINDOW.close()
        except Exception:
            pass
        WINDOW = None
    get_ui(WINDOW_OBJECT_NAME, "WeightEditorImproveWindow")
    try:
        ws_ctrl = WINDOW_OBJECT_NAME + "WorkspaceControl"
        if cmds.workspaceControl(ws_ctrl, exists=True):
            cmds.deleteUI(ws_ctrl)
    except Exception:
        pass


class OptionWindow():
    """非ドッキング時の SIWeightEditor Improve ウィンドウ起動クラス（1 回でスムーズに起動）。"""

    def __init__(self, offset=False):
        global WINDOW
        _close_existing_windows()
        maya_win = qt.get_maya_window()
        WINDOW = WeightEditorImproveWindow(parent=maya_win)
        editor_state.set_window(WINDOW)
        WINDOW.setWindowFlag(Qt.Window, True)

        if WINDOW.dockable or offset:
            pos_x, pos_y = _clamp_window_position(WINDOW.pw - 8, WINDOW.ph - 31, WINDOW.sw, WINDOW.sh)
        else:
            pos_x, pos_y = _clamp_window_position(WINDOW.pw, WINDOW.ph, WINDOW.sw, WINDOW.sh)

        WINDOW.init_flag = False
        WINDOW.resize(WINDOW.sw, WINDOW.sh)
        WINDOW.move(pos_x, pos_y)
        # 表示前に最終サイズでボタン配置を 1 回だけ適用してチラつきを防ぐ
        try:
            WINDOW.re_arrangement_but(win_x=WINDOW.sw, grid_v=0, but_list=WINDOW.but_list, loop=0, layout=WINDOW.unique_layout)
            WINDOW.re_arrangement_but(win_x=WINDOW.sw, grid_v=0, but_list=WINDOW.but_list3, loop=0, layout=WINDOW.unique_layout3)
            WINDOW.re_arrangement_but(win_x=WINDOW.sw, grid_v=0, but_list=WINDOW.but_list2, loop=0, layout=WINDOW.unique_layout2)
        except Exception:
            pass

        # 非ドッキング時は QMainWindow.show を直接 1 回だけ呼び、Mixin の余計な再生成を防ぐ
        QMainWindow.show(WINDOW)
        _ensure_maya_owned_floating_window(WINDOW)


class WeightEditorImproveWindow(
    EditorUIBuilderMixin,
    EditorWeightOpsMixin,
    EditorViewControllerMixin,
    qt.DockWindow,
):
    """SIWeightEditor Improve メインウィンドウ本体（各機能 Mixin を統合）。"""

    selection_mode = 'tree'
    filter_type = 'scene'
    icon_path = os.path.join(os.path.dirname(__file__), 'icon/')
    
    def init_save(self):
        self.dir_path = os.path.join(
            os.getenv('MAYA_APP_DIR'),
            'Scripting_Files')
        self.w_file = self.dir_path + '/siweighteditor_improve_window.json'
    
    #セーブファイルを読み込んで返す　   
    def load_window_data(self, init_pos=False):
        #セーブデータが無いかエラーした場合はデフォファイルを作成
        if init_pos:
            self.init_save_data()
            return None
        #読み込み処理
        if os.path.exists(self.w_file):#保存ファイルが存在したら
            try:
                with open(self.w_file, 'r') as f:
                    save_data = json.load(f)
                    self.pw = save_data['pw']
                    self.ph = save_data['ph']
                    self.sw = save_data['sw']
                    self.sh = save_data['sh']
                    self.hilite_vtx = save_data['hilite']
                    self.lock = save_data['lock'] 
                    self.mesh = True#メッシュ選択変更切り替え反映機能はいったんなしに
                    self.focus = save_data['focus']
                    self.mode = save_data['mode']
                    self.norm = save_data['norm']
                    self.limit = save_data['limit']
                    self.filter = save_data['filter']
                    self.no_limit = save_data['no_limit']
                    self.digit = save_data['digit']
                    self.round = save_data['round']
                    self.under_wt = save_data['under_wt']
                    self.over_wt = save_data['over_wt']
                    self.over_inf = save_data['over_inf']
                    self.keep_mmi = save_data['keep_mmi']
                    self.max_wt = save_data['max']
                    self.joint_tool = save_data['joint_tool']
                    self.joint_hilite = save_data['joint_hilite']
                    self.search_mode = save_data['search_mode']
                    self.darken_value = save_data['darken_value']
                    self.interactive = save_data['interactive']
                    self.scriptjob = save_data['scriptjob']
                    self.dockable = save_data['dockable']
                    self.floating = save_data['floating']
                    self.area=save_data['area']
                    self.smooth_parcent = save_data['smooth_parcent']
                    self.bone_vis = save_data.get('bone_vis', False)
                    self.bone_vis_lines = save_data.get('bone_vis_lines', True)
                    self.bone_vis_labels = save_data.get('bone_vis_labels', True)
                    self.bone_vis_dashed_tiny = save_data.get('bone_vis_dashed_tiny', True)
                    self.bone_vis_mode = save_data.get('bone_vis_mode', 'avg')
                    self.slider_stat_mode = save_data.get('slider_stat_mode', 'avg')
                    self.keep_bone = save_data.get('keep_bone', True)
                    self.joint_hilite_mode = save_data.get('joint_hilite_mode', 'heatmap')
                    self.used_joints = save_data.get('used_joints', False)
                    self.used_joints_mode = save_data.get('used_joints_mode', 'exact')
                    return save_data
            except Exception as e:
                save_data = self.init_save_data()
                return save_data
        else:
            save_data = self.init_save_data()
            return save_data
            
    def init_save_data(self):
        print('data load error , init save data :')
        self.area = None
        self.pw = 200
        self.ph = 200
        self.sw = 440
        self.sh = 700
        self.digit = 1
        self.round = 1
        self.hilite_vtx = False
        self.lock = False
        self.focus = True
        self.mode = 0
        self.norm = True
        self.limit = 0
        self.filter = False
        self.no_limit = False
        self.over_wt = False
        self.under_wt = False
        self.over_inf = False
        self.keep_mmi = False
        self.joint_tool = 0
        self.joint_hilite = False
        self.max_wt = 100
        self.search_mode = 0
        self.darken_value = 150
        self.interactive = False
        self.scriptjob = True
        self.dockable = False
        self.floating = True
        self.area = None
        self.smooth_parcent = 25
        self.bone_vis = False
        self.bone_vis_lines = True
        self.bone_vis_labels = True
        self.bone_vis_dashed_tiny = True
        self.bone_vis_mode = 'avg'
        self.slider_stat_mode = 'avg'
        self.keep_bone = True
        self.joint_hilite_mode = 'heatmap'
        self.used_joints = False
        self.used_joints_mode = 'exact'
        save_data = {}
        save_data['scriptjob'] = True
        save_data['dockable'] = False
        return save_data
        
    def save_window_data(self, display=True):
        if not os.path.exists(self.dir_path):
            os.makedirs(self.dir_path)
        save_data = {}
        if self.isDockable() is True:
            dock_dtrl = self.parent()
            pos = dock_dtrl.mapToGlobal(QPoint(0, 0))
        else:
            pos = self.pos()
        #print('check pos :', pos)
        size = self.size()
        save_data['pw'] = pos.x()
        save_data['ph'] = pos.y()
        save_data['sw'] = size.width()
        save_data['sh'] = size.height()
        save_data['hilite'] = self.highlite_but.isChecked()
        save_data['lock'] = self.lock_but.isChecked()
        save_data['focus'] = self.focus_but.isChecked()
        save_data['mode'] = self.mode_but_group.checkedId()
        save_data['norm'] = self.norm_but.isChecked()
        save_data['limit'] = self.limit_box.value()
        save_data['filter'] = self.filter_but.isChecked()
        save_data['no_limit'] = self.no_limit
        save_data['max'] = self.max_wt
        save_data['digit'] = self.digit_box.value()
        save_data['round'] = self.round_box.value()
        save_data['under_wt'] = self.under_wt_but.isChecked()
        save_data['over_wt'] = self.over_wt_but.isChecked()
        save_data['over_inf'] = self.over_inf_but.isChecked()
        save_data['keep_mmi'] = self.keep_mmi_but.isChecked()
        save_data['joint_tool'] = self.joint_tool
        save_data['joint_hilite'] = self.joint_hl_but.isChecked()
        save_data['search_mode'] = self.search_but_group.checkedId()
        save_data['darken_value'] = self.zero_darken.value()
        save_data['interactive'] = self.interactive_but.isChecked()
        save_data['scriptjob'] = self.scriptjob_but.isChecked()
        save_data['dockable'] = self.docking_but.isChecked()
        save_data['floating'] = self.isFloating()
        save_data['area'] = self.dockArea()
        save_data['smooth_parcent'] = self.smooth_ratio_box.value()
        save_data['bone_vis'] = self.bone_vis_but.isChecked() if hasattr(self, 'bone_vis_but') else False
        save_data['bone_vis_lines'] = getattr(self, 'bone_vis_lines', True)
        save_data['bone_vis_labels'] = getattr(self, 'bone_vis_labels', True)
        save_data['bone_vis_dashed_tiny'] = getattr(self, 'bone_vis_dashed_tiny', True)
        save_data['bone_vis_mode'] = getattr(self, 'bone_vis_mode', 'avg')
        save_data['slider_stat_mode'] = getattr(self, 'slider_stat_mode', 'avg')
        save_data['keep_bone'] = self.keep_bone_but.isChecked() if hasattr(self, 'keep_bone_but') else True
        save_data['joint_hilite_mode'] = getattr(self, 'joint_hilite_mode', 'heatmap')
        save_data['used_joints'] = self.used_joint_vis_but.isChecked() if hasattr(self, 'used_joint_vis_but') else False
        save_data['used_joints_mode'] = getattr(self, 'used_joints_mode', 'exact')
        with open(self.w_file, 'w') as f:
            json.dump(save_data, f)
            
    #def moveEvent(self, e):
        #print('window_move :' , e)
        
    pre_selection_node = []
    def __init__(self, parent = None, init_pos=False):
        if parent is None:
            parent = qt.get_maya_window()
        super(WeightEditorImproveWindow, self).__init__(parent)
        self.setWindowFlag(Qt.Window, True)
        editor_state.load_plugin()#プラグインロード
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.init_save()
        self.wdata = self.load_window_data()
        self.icon_path = os.path.join(os.path.dirname(__file__), 'icons/')
        #OpenMayaでウェイト取得するクラスをインスタンス化
        self.store_skin_weight = store_skin_weight.StoreSkinWeight()
        #self.setAcceptDrops(True)#ドロップ可能にしておく

        self.setObjectName(state.WINDOW_OBJECT_NAME)  # <- objectNameを設定
        self.setProperty("saveWindowPref", True)  # <- ウィンドウ設定の保存を許可
        debug_log('window init')
        try:
            print('SIWeightEditor Improve debug log:', get_debug_log_path())
        except Exception:
            pass
        self._init_ui()

    def create_job(self):
        global select_job
        global select_job_ids
        self.remove_job()
        select_job = cmds.scriptJob(cu=True, e=("SelectionChanged", self.get_set_skin_weight))
        self.select_job_id = select_job
        select_job_ids.add(select_job)
        debug_log('create_job SelectionChanged id={0}'.format(select_job))
        
    def remove_job(self):
        global select_job
        global select_job_ids
        job_ids = set()
        if select_job:
            job_ids.add(select_job)
        if hasattr(self, 'select_job_id') and self.select_job_id:
            job_ids.add(self.select_job_id)
        job_ids.update(select_job_ids)
        
        for job_id in list(job_ids):
            try:
                if cmds.scriptJob(ex=job_id):
                    cmds.scriptJob(k=job_id, f=True)
                    debug_log('remove_job killed id={0}'.format(job_id))
                else:
                    debug_log('remove_job already dead id={0}'.format(job_id))
            except Exception as e:
                debug_log('remove_job failed id={0} error={1}'.format(job_id, e))
        
        select_job = None
        select_job_ids = set()
        self.select_job_id = None
            
    closed_flag = False
    save_flag = True
    def closeEvent(self, e):
        #print('window closed :')
        debug_log('closeEvent start')
        if hasattr(self, '_reset_slider_drag_state'):
            try:
                self._reset_slider_drag_state()
            except Exception:
                pass
        self.closed_flag=True
        self.remove_job()
        try:
            if hasattr(self, 'sel_model'):
                self.sel_model.selectionChanged.disconnect(self.cell_changed)
                debug_log('closeEvent disconnected selectionChanged')
        except Exception as ex:
            debug_log('closeEvent disconnect selectionChanged skipped {0}'.format(ex))
        joint_display_helper.cleanup()
        bone_visualizer.cleanup()
        if getattr(self, 'bone_remap_window', None) is not None:
            try:
                self.bone_remap_window.close()
            except Exception:
                pass
            self.bone_remap_window = None
        if getattr(self, 'model_edit_window', None) is not None:
            try:
                self.model_edit_window.close()
            except Exception:
                pass
            self.model_edit_window = None
        if getattr(self, 'joint_search_dialog', None) is not None:
            try:
                self.joint_search_dialog.close()
            except Exception:
                pass
            self.joint_search_dialog = None
        if getattr(self, 'bone_picker_window', None) is not None:
            try:
                self.bone_picker_window.close()
            except Exception:
                pass
            self.bone_picker_window = None
        self.erase_func_data()
        self.remove_job()
        if self.save_flag:
            #print('save data :')
            self.save_window_data()
        self.deleteLater()
        debug_log('closeEvent end')
        
    def dockCloseEventTriggered(self):
        #print('dock_close_event :')
        debug_log('dockCloseEventTriggered')
        self.closeEvent(None)
        
    #メモリ解放しっかり
    #ちゃんと消さないと莫大なUIデータがメモリに残り続けるので注意
    def erase_func_data(self):
        #print(self.weight_model._data)
        try:
            del self.weight_model._data#一番でかいっぽい
            self.weight_model._data = {}
        except Exception as e:
            #print('{}'.format(e), 'in close')
            pass
        try:
            self.weight_model.deleteLater()
            del self.weight_model
        except Exception as e:
            #print('{}'.format(e), 'in close')
            pass
        try:
            self.sel_model.deleteLater()
            del self.sel_model
        except Exception as e:
            #print('{}'.format(e), 'in close')
            pass
        try:
            self.view_widget.deleteLater()
            del self.view_widget
        except Exception as e:
            #print('{}'.format(e), 'in close')
            pass
        
        try:
            del self._data
            del self.hl_nodes
            del self.all_influences
            del self.all_skin_clusters
            del self.influences_dict 
            del self.node_vtx_dict 
            del self.node_weight_dict
            del self.node_skinFn_dict 
            
            del self.header_selection
            del self.all_rows
            del self.v_header_list
            del self.mesh_rows
            del self.vtx_row_dict
            del self.under_weight_rows
            del self.over_weight_rows
            del self.over_influence_limit_dict
            del self.over_influence_limit_rows
            del self.all_editable_rows
            del self.weight_lock_data
            del self.vtx_lock_data_dict
            del self.node_id_dict
            del self.mesh_model_items_dict
            del self.filter_weight_dict
            del self.vtx_weight_dict
            del self.lock_data_dict
        except Exception as e:
            #print('{}'.format(e), 'in close')
            pass


def _get_live_window():
    """現在開いていて安全に操作可能なメインウィンドウを返す（閉鎖・破棄済みなら None）。"""
    win = editor_state.WINDOW or WINDOW
    if win is None or getattr(win, "closed_flag", False):
        return None
    try:
        if hasattr(qt, "shiboken") and not qt.shiboken.isValid(win):
            return None
    except Exception:
        pass
    return win


# アンドゥ・リドゥ時に辞書を更新しておく（bake_skin_weight_improve.py 向け公開 API）
def update_dict(node_weight_dict, node_id_dict):
    win = _get_live_window()
    if win is None or not hasattr(win, "node_weight_dict"):
        return
    for node, new_weight_list in (node_weight_dict or {}).items():
        if node not in win.node_weight_dict:
            continue
        target_list = win.node_weight_dict[node]
        node_v_id_list = (node_id_dict or {}).get(node)
        if node_v_id_list is None:
            continue
        for vid, new_weight in zip(node_v_id_list, new_weight_list):
            if 0 <= vid < len(target_list):
                target_list[vid] = list(new_weight)


def reverse_dict(node_weight_dict, node_id_dict):
    win = _get_live_window()
    if win is None or not hasattr(win, "node_weight_dict"):
        return
    for node, old_weight_list in (node_weight_dict or {}).items():
        if node not in win.node_weight_dict:
            continue
        target_list = win.node_weight_dict[node]
        node_v_id_list = (node_id_dict or {}).get(node)
        if node_v_id_list is None:
            continue
        for vid, old_weight in zip(node_v_id_list, old_weight_list):
            if 0 <= vid < len(target_list):
                target_list[vid] = list(old_weight)


def _capture_selected_cell_keys(win):
    """テーブル再構築前に選択セルを (ノード名, 頂点ID, インフルエンス名) 集合として記憶する。"""
    keys = []
    sel_items = getattr(win, "selected_items", None) or []
    vtx_row_dict = getattr(win, "vtx_row_dict", None) or {}
    all_infs = getattr(win, "all_influences", None) or []
    for idx in sel_items:
        try:
            row = idx.row()
            col = idx.column()
        except Exception:
            continue
        row_info = vtx_row_dict.get(row)
        if row_info is None or not (0 <= col < len(all_infs)):
            continue
        keys.append((row_info[6], row_info[0], all_infs[col]))
    return keys


def _restore_selected_cell_keys(win, cell_keys):
    """記憶していた (ノード名, 頂点ID, インフルエンス名) のセル選択をテーブルへ復元する。"""
    if not cell_keys:
        return False
    vtx_row_dict = getattr(win, "vtx_row_dict", None) or {}
    inf_col_dict = getattr(win, "inf_column_dict", None) or {}
    sel_model = getattr(win, "sel_model", None)
    weight_model = getattr(win, "weight_model", None)
    if sel_model is None or weight_model is None:
        return False

    node_vid_to_row = {}
    for row, row_info in vtx_row_dict.items():
        node_vid_to_row[(row_info[6], row_info[0])] = row

    selection = QItemSelection()
    first_index = None
    for node, vid, inf_name in cell_keys:
        row = node_vid_to_row.get((node, vid))
        col = inf_col_dict.get(inf_name)
        if row is None or col is None:
            continue
        model_idx = weight_model.index(row, col)
        if not model_idx.isValid():
            continue
        selection.select(model_idx, model_idx)
        if first_index is None:
            first_index = model_idx

    if selection.isEmpty() or first_index is None:
        return False

    win._restoring_bone_selection = True
    try:
        sel_model.setCurrentIndex(first_index, QItemSelectionModel.NoUpdate)
        sel_model.select(selection, QItemSelectionModel.ClearAndSelect)
    finally:
        win._restoring_bone_selection = False
    return True


def refresh_window():
    win = _get_live_window()
    if win is None:
        return
    if hasattr(win, "_reset_slider_drag_state"):
        win._reset_slider_drag_state()
    prev_cells = _capture_selected_cell_keys(win)
    try:
        win.reset_hilite(force_reset=True)
    except Exception:
        pass
    try:
        win.get_set_skin_weight(undo=True)
    except Exception:
        return
    restored = _restore_selected_cell_keys(win, prev_cells)
    if not restored:
        try:
            win.change_add_mode(win.add_mode, change_only=True)
        except Exception:
            if hasattr(win, "_set_slider_and_spinbox_silent"):
                win._set_slider_and_spinbox_silent(0.0)


def set_current_data(node_id, node_weight, node_inf, node_skin, undo_node_dict, redo_node_dict, org_node_dict):
    editor_state.set_current_data(
        node_id, node_weight, node_inf, node_skin, undo_node_dict, redo_node_dict, org_node_dict
    )


def get_current_data():
    return editor_state.get_current_data()


def get_ui(name, weight_type):
    for widget in list(QApplication.topLevelWidgets() or []):
        try:
            if widget.__class__.__name__ == weight_type or widget.objectName() == name:
                widget.save_flag = False
                widget.close()
        except Exception:
            pass


def make_ui():
    get_ui(WINDOW_OBJECT_NAME, "WeightEditorImproveWindow")
    if MAYA_VER < 2025:
        os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"
        if hasattr(Qt, "AA_EnableHighDpiScaling"):
            QGuiApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    ui = WeightEditorImproveWindow()
    return ui


def Option(x=None, y=None):
    global WINDOW
    # 非ドッキング設定の場合はダミーウィンドウを作らず、直接 1 回だけ起動する
    if MAYA_VER <= 2016 or not _load_saved_dockable_flag():
        OptionWindow()
        return

    _close_existing_windows()
    window = make_ui()
    save_data = window.wdata or window.load_window_data(init_pos=False)

    if not save_data.get("dockable", False):
        window.save_flag = False
        window.close()
        OptionWindow()
        return

    WINDOW = window
    editor_state.set_window(WINDOW)

    if save_data:
        width = save_data.get("sw")
        height = save_data.get("sh")
    else:
        width = None
        height = None

    ui_script = "import siweighteditor_improve.siweighteditor;siweighteditor_improve.siweighteditor.restoration_workspacecontrol()"
    opts = {
        "scriptjob": save_data.get("scriptjob", True),
        "dockable": save_data.get("dockable", True),
        "floating": True,
        "area": None,
        "allowedArea": None,
        "x": None,
        "y": None,
        "widthSizingProperty": width,
        "heightSizingProperty": height,
        "initWidthAsMinimum": None,
        "retain": False,
        "plugins": None,
        "controls": None,
        "uiScript": ui_script,
        "closeCallback": None,
    }
    WINDOW.setDockableParameters(**opts)
    WINDOW.init_flag = False
    WINDOW.resizeEvent(WINDOW)
    try:
        WINDOW.raise_()
    except Exception:
        pass


def restoration_workspacecontrol():
    print("SI Weight Editor : restoration_workspacecontrol")
    global WINDOW
    WINDOW = make_ui()
    editor_state.set_window(WINDOW)
    restoredControl = omui.MQtUtil.getCurrentParent()
    mixinPtr = omui.MQtUtil.findControl(WINDOW.objectName())
    if PYTHON_VER >= 3:
        omui.MQtUtil.addWidgetToMayaLayout(int(mixinPtr), int(restoredControl))
    else:
        omui.MQtUtil.addWidgetToMayaLayout(long(mixinPtr), long(restoredControl))
    WINDOW.init_flag = False
    WINDOW.resizeEvent(WINDOW)


if __name__ == "__main__":
    Option()

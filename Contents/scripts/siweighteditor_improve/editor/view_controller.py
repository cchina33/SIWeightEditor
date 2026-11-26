# -*- coding: utf-8 -*-
"""SIWeightEditor Improve のデータ読込・テーブル更新・選択連動・Keep Bone・サブウィンドウ連携 Mixin。"""

import copy
import fnmatch
import re
from collections import defaultdict

from maya import cmds

from ..maya_version import MAYA_VER
from .. import common
from .. import lang
from .. import qt
from ..viewport import bone_visualizer
from ..viewport import joint_display_helper
from ..tools import bone_picker_window
from ..tools import bone_remap_window
from ..tools import model_edit_window
from ..qt import *
from . import state
from .state import debug_log, timer
from .widgets import JointSearchDialog, TableModel, set_table_header_width

if state.PYTHON_VER >= 3:
    round = common.round_half_up


class EditorViewControllerMixin(object):
    """スキンデータ読み込み・テーブル選択連動・ビューポート強調・各ウィンドウ連携を提供する Mixin。"""

    def focus_vertex_from_header(self, pos):
        v_header = self.view_widget.verticalHeader()
        row = v_header.logicalIndexAt(pos)
        if row >= 0 and hasattr(self, 'vtx_row_dict') and row in self.vtx_row_dict:
            vtx_name = self.vtx_row_dict[row][5]
            cmds.viewFit(vtx_name, fitFactor=0.8)

    #-----------------------------------------------------------
    # セル選択解除（記憶中のボーン選択もクリア）
    def clear_selection(self):
        self.last_selected_influences = []
        self.sel_model.clearSelection()
        self._set_slider_and_spinbox_silent(0.0)
        self.update_quick_bind_button()

    # ゼロセルの文字の色を変える
    def zero_cell_darken(self):
        state.ZERO_CELL_DARKEN = 210 - self.zero_darken.value()
        state.set_zero_cell_darken(state.ZERO_CELL_DARKEN)
        self.refresh_table_view()
    
    #お知らせをUIに表示する
    def set_message(self, msg='', error=True):
        self.msg_label.setText(msg)
        if error:
            qt.change_button_color(self.msg_label, textColor=[250, 128, 64])
        else:
            qt.change_button_color(self.msg_label, textColor=200)
            
    #お知らせを消す
    def reset_message(self):
        self.msg_label.setText('')
        
    search_joint_list = []
    def search_joint(self, as_interractive=False):
        debug_log('search_joint start interactive={0} text="{1}"'.format(as_interractive, self.search_line.text()))
        if not self.interactive_but.isChecked() and as_interractive:
            debug_log('search_joint skip interactive off')
            return
        if self.interactive_but.isChecked() and not as_interractive:
            debug_log('search_joint skip waiting interactive')
            return
        if len(self.search_line.text().strip()) == 0:
            self.search_joint_list = []
        else:
            self.search_joint_list = [name for name in self.search_line.text().split(' ') if name != '']
        self.get_set_skin_weight()

    def selection_has_polygon_target(self):
        selection = cmds.ls(sl=True, l=True) or []
        debug_log('selection_has_polygon_target selection_count={0} selection={1}'.format(len(selection), selection[:5]))
        if not selection:
            debug_log('selection_has_polygon_target result=True empty')
            return True
        for sel in selection:
            node = sel.split('.')[0]
            if any([token in sel for token in ['.vtx[', '.e[', '.f[']]):
                debug_log('selection_has_polygon_target result=True component={0}'.format(sel))
                return True
            try:
                node_type = cmds.nodeType(node)
            except Exception:
                continue
            if node_type == 'mesh':
                debug_log('selection_has_polygon_target result=True mesh={0}'.format(node))
                return True
            if node_type == 'transform':
                shapes = cmds.listRelatives(node, s=True, ni=True, f=True) or []
                for shape in shapes:
                    try:
                        if cmds.nodeType(shape) == 'mesh':
                            debug_log('selection_has_polygon_target result=True mesh_transform={0}'.format(node))
                            return True
                    except Exception:
                        pass
        debug_log('selection_has_polygon_target result=False')
        return False

    @staticmethod
    def _match_single_search_token(short_name, pattern):
        """単一検索パターンとボーンのショートネームを照合する（=厳密一致・*ワイルドカード・部分一致対応）。"""
        if not pattern:
            return False
        base_name = short_name.split(':')[-1]
        short_upper = short_name.upper()
        base_upper = base_name.upper()

        # = またはダブルクォート囲みは厳密一致
        exact = False
        inner = pattern
        if inner.startswith('='):
            exact = True
            inner = inner[1:]
        elif len(inner) >= 2 and inner[0] == '"' and inner[-1] == '"':
            exact = True
            inner = inner[1:-1]
        if not inner:
            return False

        inner_upper = inner.upper()
        if exact:
            return short_upper == inner_upper or base_upper == inner_upper
        if '*' in inner_upper or '?' in inner_upper:
            return fnmatch.fnmatchcase(short_upper, inner_upper) or fnmatch.fnmatchcase(base_upper, inner_upper)
        return inner_upper in short_upper

    def _match_influence_search(self, inf, query_tokens, require_positive=False):
        """拡張検索クエリ（通常OR / +AND / -!除外 / =厳密一致 / *ワイルドカード）でインフルエンスを判定する。"""
        if not query_tokens:
            return not require_positive
        short_name = inf.split('|')[-1]
        or_tokens = []
        and_tokens = []
        not_tokens = []

        for raw in query_tokens:
            if not raw:
                continue
            if len(raw) > 1 and raw[0] in ('-', '!'):
                not_tokens.append(raw[1:])
            elif len(raw) > 1 and raw[0] == '+':
                and_tokens.append(raw[1:])
            else:
                or_tokens.append(raw)

        # 除外条件に該当すれば即対象外
        for tok in not_tokens:
            if self._match_single_search_token(short_name, tok):
                return False

        # 必須（AND）条件をすべて満たす必要がある
        for tok in and_tokens:
            if not self._match_single_search_token(short_name, tok):
                return False

        # Add モードでは除外条件だけのときに全ボーンが加算されないよう肯定条件を要求する
        if require_positive and not or_tokens and not and_tokens:
            return False

        if or_tokens:
            return any(self._match_single_search_token(short_name, tok) for tok in or_tokens)
        return True

    @staticmethod
    def _flip_lr_in_token(token):
        """検索トークン内の接頭辞（= / + / - / !）を保ったまま左右表記（L/R, Left/Right）を反転する。"""
        prefix = ''
        body = token
        while body and body[0] in ('=', '+', '-', '!'):
            prefix += body[0]
            body = body[1:]
        quote_wrapped = len(body) >= 2 and body[0] == '"' and body[-1] == '"'
        if quote_wrapped:
            body = body[1:-1]
        if not body:
            return token

        # 単語境界や区切り文字単位で L⇔R / Left⇔Right を一時プレースホルダ経由で安全に反転
        def _swap_word(match):
            word = match.group(0)
            lower = word.lower()
            if lower == 'left':
                if word.isupper():
                    return 'RIGHT'
                if word[0].isupper():
                    return 'Right'
                return 'right'
            if lower == 'right':
                if word.isupper():
                    return 'LEFT'
                if word[0].isupper():
                    return 'Left'
                return 'left'
            if lower == 'l':
                return 'R' if word.isupper() else 'r'
            if lower == 'r':
                return 'L' if word.isupper() else 'l'
            return word

        # キャメルケース含む Left⇔Right と区切り付き L⇔R を安全に反転
        pattern = re.compile(
            r'(?<=[a-z0-9_])(Left|Right)(?=[A-Z0-9_.]|$)'
            r'|^(Left|Right)(?=[A-Z0-9_.]|$)'
            r'|(?<![A-Za-z])(Left|Right|LEFT|RIGHT|left|right|L|R|l|r)(?![A-Za-z])'
        )
        flipped = pattern.sub(_swap_word, body)
        if quote_wrapped:
            flipped = '"' + flipped + '"'
        return prefix + flipped

    def flip_search_lr(self):
        """検索欄の左右表記（L ⇔ R / Left ⇔ Right）をワンクリックで反転して再検索する。"""
        raw_text = self.search_line.text().strip()
        if not raw_text:
            return
        tokens = [t for t in raw_text.split(' ') if t]
        flipped_tokens = [self._flip_lr_in_token(t) for t in tokens]
        new_text = ' '.join(flipped_tokens)
        if new_text == raw_text:
            return
        self.search_line.blockSignals(True)
        self.search_line.setText(new_text)
        self.search_line.blockSignals(False)
        self.search_joint_list = flipped_tokens
        self.get_set_skin_weight()

    def get_influence_weight_stats(self):
        """現在表示中の頂点データからインフルエンスごとの最大ウェイト値と影響頂点数を集計する。"""
        stats = {}
        if not hasattr(self, 'vtx_row_dict') or not hasattr(self, '_data'):
            return stats
        for row, row_data in self.vtx_row_dict.items():
            if row >= len(self._data):
                continue
            influences = row_data[2]
            weights = self._data[row]
            for inf, w in zip(influences, weights):
                if w > 0.00001:
                    entry = stats.get(inf)
                    if entry is None:
                        stats[inf] = {'max': float(w), 'count': 1}
                    else:
                        if w > entry['max']:
                            entry['max'] = float(w)
                        entry['count'] += 1
        return stats

    def _collect_bound_influences(self):
        """現在対象となっているメッシュの全バインド済みインフルエンス一覧を返す。"""
        if hasattr(self.store_skin_weight, 'all_influences') and self.store_skin_weight.all_influences:
            return copy.copy(self.store_skin_weight.all_influences)
        if self.all_influences:
            return copy.copy(self.all_influences)
        return []

    def open_joint_search_dialog(self):
        """非モーダルの Joints 検索ダイアログを開く（開いている場合は前面化して最新化）。"""
        debug_log('open_joint_search_dialog start')
        influences = self._collect_bound_influences()
        if not influences:
            self.get_set_skin_weight()
            influences = self._collect_bound_influences()
        if not influences:
            debug_log('open_joint_search_dialog no influences')
            self.set_message(msg='- No bound joints found -', error=True)
            return

        current_names = [name for name in self.search_line.text().split(' ') if name != '']
        nearby_names = self.get_selected_joint_short_names()
        weight_stats = self.get_influence_weight_stats()

        win = getattr(self, 'joint_search_dialog', None)
        if win is not None:
            try:
                if win.isVisible():
                    win.update_data(
                        influences,
                        selected_names=current_names,
                        nearby_names=nearby_names,
                        weight_stats=weight_stats,
                        preserve_selection=False,
                    )
                    win.raise_()
                    win.activateWindow()
                    return
            except Exception:
                self.joint_search_dialog = None

        debug_log('open_joint_search_dialog show influences={0} nearby={1}'.format(len(influences), len(nearby_names)))
        self.joint_search_dialog = JointSearchDialog(
            influences,
            selected_names=current_names,
            nearby_names=nearby_names,
            weight_stats=weight_stats,
            parent=self,
        )
        self.joint_search_dialog.show()
        self.joint_search_dialog.raise_()
        self.joint_search_dialog.activateWindow()

    def update_joint_search_dialog(self, force=False):
        """Joints 検索ダイアログが開いている場合に一覧とウェイト統計をリアルタイム更新する。"""
        win = getattr(self, 'joint_search_dialog', None)
        if win is None:
            return
        try:
            if not force and not win.isVisible():
                return
            influences = self._collect_bound_influences()
            nearby_names = self.get_selected_joint_short_names()
            weight_stats = self.get_influence_weight_stats()
            win.update_data(
                influences,
                nearby_names=nearby_names,
                weight_stats=weight_stats,
                preserve_selection=True,
            )
        except Exception:
            self.joint_search_dialog = None

    def open_bone_picker_window(self):
        """Anim Picker 風のボーンピッカー別ウィンドウを開く（開いていれば前面化して最新化）。"""
        influences = self._collect_bound_influences()
        if not influences:
            self.get_set_skin_weight()
            influences = self._collect_bound_influences()
        if not influences:
            self.set_message(msg='- No bound joints found -', error=True)
            return

        win = getattr(self, 'bone_picker_window', None)
        if win is not None:
            try:
                if win.isVisible():
                    win.refresh_from_editor(refresh_positions=True)
                    win.raise_()
                    win.activateWindow()
                    return
            except Exception:
                self.bone_picker_window = None

        self.bone_picker_window = bone_picker_window.BonePickerWindow(self, parent=qt.get_maya_window())
        self.bone_picker_window.refresh_from_editor(refresh_positions=True)
        self.bone_picker_window.show()
        self.bone_picker_window.raise_()
        self.bone_picker_window.activateWindow()

    def update_bone_picker_window(self):
        """ボーンピッカーが開いている場合に最新ウェイト統計・一覧へリアルタイム同期する。"""
        win = getattr(self, 'bone_picker_window', None)
        if win is None:
            return
        try:
            if win.isVisible():
                win.refresh_from_editor(refresh_positions=False)
        except Exception:
            self.bone_picker_window = None
        
    def get_selected_joint_short_names(self):
        if not hasattr(self, 'selected_items') or not self.selected_items:
            debug_log('get_selected_joint_short_names skip no selected_items')
            return []
        if not hasattr(self, 'vtx_row_dict') or not hasattr(self, 'weight_model'):
            debug_log('get_selected_joint_short_names skip missing data')
            return []
        
        joint_names = set()
        selected_cells = [(item.row(), item.column()) for item in self.selected_items]
        debug_log('get_selected_joint_short_names selected_cells={0}'.format(len(selected_cells)))
        if len(set([row for row, column in selected_cells])) > 100:
            debug_log('get_selected_joint_short_names skip over 100 rows')
            return []
        
        for row, column in selected_cells:
            if column is not None and 0 <= column < len(self.all_influences):
                joint_names.add(self.all_influences[column].split('|')[-1])
            if row not in self.vtx_row_dict:
                continue
            
            row_data = self.vtx_row_dict[row]
            influences = row_data[2]
            if not hasattr(self.weight_model, '_data') or row >= len(self.weight_model._data):
                continue
            weight_list = self.weight_model._data[row]
            for inf_id, weight_value in enumerate(weight_list):
                if weight_value > 0.0001 and inf_id < len(influences):
                    joint_names.add(influences[inf_id].split('|')[-1])
        
        debug_log('get_selected_joint_short_names result={0}'.format(len(joint_names)))
        return sorted(joint_names, key=lambda name: name.upper())

    def set_joint_names_to_search(self, joint_tokens):
        """検索欄を指定ボーン一覧で上書きして即座にテーブルへ反映する。"""
        tokens = [t for t in (joint_tokens or []) if t]
        self.search_line.blockSignals(True)
        self.search_line.setText(' '.join(tokens))
        self.search_line.blockSignals(False)
        self.search_joint_list = tokens
        self.get_set_skin_weight()
        
    def append_joint_names_to_search(self, joint_names):
        debug_log('append_joint_names_to_search count={0}'.format(len(joint_names)))
        if not joint_names:
            return
        current_names = [name for name in self.search_line.text().split(' ') if name != '']
        for name in joint_names:
            if name not in current_names:
                current_names.append(name)
        self.search_line.blockSignals(True)
        self.search_line.setText(' '.join(current_names))
        self.search_line.blockSignals(False)
        self.search_joint_list = current_names
        self.get_set_skin_weight()
        
    def clear_search(self):
        self.search_line.blockSignals(True)
        self.search_line.setText('')
        self.search_line.blockSignals(False)
        self.search_joint_list = []
        self.get_set_skin_weight()

    def check_unlock(self):
        if not self.lock_but.isChecked():
            self.get_set_skin_weight()

    #UIに選択コンポーネントのバーテックスカラーを反映
    weight_list = []
    pre_hl_nodes = []
    hl_nodes = None
    pre_sel = None
    pre_vtx_id_list = []
    temp_vf_face_dict = {}
    temp_vf_vtx_dict = {}
    node_vertex_dict_dict = {}#頂点とIDの対応辞書のノードごとの辞書
    pre_node_vtx_dict ={}
    hilite_flag = False
    all_influences = []
    pre_current_vtx_dict = {}
    #@timer
    #@prof.profileFunction()
    def get_set_skin_weight(self, node_vtx_dict={}, cycle=False, clear=False, filter=False, 
                                        show_dict={}, show_all=False, show_bad=False, undo=False):
        if self.closed_flag:
            debug_log('get_set_skin_weight skip closed window')
            return
        debug_log('get_set_skin_weight start flags node_vtx={0} cycle={1} clear={2} filter={3} show_dict={4} show_all={5} show_bad={6} undo={7}'.format(
            bool(node_vtx_dict), cycle, clear, filter, bool(show_dict), show_all, show_bad, undo))
        if not any([node_vtx_dict, cycle, clear, filter, show_dict, show_all, show_bad, undo]):
            if not self.selection_has_polygon_target():
                debug_log('get_set_skin_weight skip non polygon selection')
                return
        self.reset_message()#メッセージ初期化
        '''機能上整合性とれなくなるのでオミット
        #選択変更時に何も選択されてなかったら表示キープ
        if self.no_sel_lock_but.isChecked():
            if not cmds.ls(sl=True, l=True) and not filter and not clear and not cycle and self.pre_hl_nodes:
                if not show_bad:
                    return
                else:
                    node_vtx_dict = self.pre_node_vtx_dict
                    self.hl_nodes = self.pre_hl_nodes
        '''
        try:
            if not undo and cmds.selectMode(q=True, co=True) and not self.focus_but.isChecked():
                debug_log('get_set_skin_weight return component mode focus off')
                return
        except Exception as e:
            #print('{}'.format(e))
            #print('UI Allready Closed :')
            debug_log('get_set_skin_weight return selectMode exception {0}'.format(e))
            return
        if self.hilite_flag:
            self.hilite_flag = False
            debug_log('get_set_skin_weight return hilite_flag')
            return
            
        self.node_vtx_dict = node_vtx_dict
        
        self.counter.reset()
        self.it_counter.reset()
        
        self.original_selection = cmds.ls(sl=True, l=True)#ハイライトの時にまとめて選択する元の選択
        self.show_dict = show_dict
        
        # クリアボタン押されたときは全部初期化
        if clear:
            self.last_selected_influences = []
            self.hl_nodes = []
            self.node_vtx_dict = {}
            self.show_dict = defaultdict(lambda : [])
            self.show_dict['clear'] = []#クリア用に初期値を与えたデフォルトディクトを作る
            
        #インフルエンスフィルター更新の時は頂点変更しない
        elif filter:
            self.node_vtx_dict = {}
            if self.pre_node_vtx_dict:
                self.node_vtx_dict = self.pre_node_vtx_dict
            #フィルター解除されたら全インフルエンス情報をもとに戻す
            if not self.filter_but.isChecked():
                self.all_influences = copy.copy(self.store_skin_weight.all_influences)
                
        #ロックボタンが押されているときの挙動
        elif self.lock_but.isChecked()  and self.pre_hl_nodes and not cycle and not show_bad:
            self.hl_nodes = self.pre_hl_nodes
            if cmds.selectMode(q=True, o=True):
                return
            else:
                self.node_vtx_dict = {node : self.store_skin_weight.om_selected_mesh_vertex(node) for node in self.pre_hl_nodes}
                if not any(self.node_vtx_dict.values()):#全てのリストが空だったら何もせず戻る
                    return
        else:#ロックされてないとき
            pass
        
        self.counter.count(string='get mesh vtx :')
        
        #頂点ID、ウェイト、インフルエンスを格納したインスタンスから各種データをとりだしておく
        if not self.node_vtx_dict:
            debug_log('get_set_skin_weight before run_store')
            self.store_skin_weight.run_store()
            debug_log('get_set_skin_weight after run_store meshes={0} influences={1}'.format(
                len(getattr(self.store_skin_weight, 'mesh_node_list', [])),
                len(getattr(self.store_skin_weight, 'all_influences', []))))
            self.hl_nodes = list(set(self.store_skin_weight.mesh_node_list))
            self.all_influences = copy.copy(self.store_skin_weight.all_influences)
            self.all_skin_clusters = self.store_skin_weight.all_skin_clusters#ロック全解除のためにスキンクラスタ全部入りリスト作っておく
            self.influences_dict  = self.store_skin_weight.influences_dict#メッシュごとのインフルエンス一覧
            self.node_vtx_dict  = self.store_skin_weight.node_vtx_dict#メッシュごとの頂点ID一覧
            self.node_weight_dict  = self.store_skin_weight.node_weight_dict#メッシュごとのウェイト一覧
            self.node_skinFn_dict = self.store_skin_weight.node_skinFn_dict
        else:
            pass

        if not self.show_dict:
            self.show_dict = self.store_skin_weight.show_dict
        
        self.pre_node_vtx_dict = self.node_vtx_dict#選択状態を保持するために前回分を取っておく
                    
        self.counter.count(string='get vtx weight :')
        
        #ジョイント選択機能が働いていたらツールを戻す
        if self.pre_tool:
            cmds.setToolTo(self.pre_tool)
            self.pre_tool = None
        
        self.header_selection = []#ヘッダーからのジョイント選択復元リストを初期化
        self.all_rows = 0#右クリックウィンドウ補正用サイズを出すため全行の桁数を数える
        self.v_header_list = []#縦ヘッダーの表示リスト
        self._data = []#全体のテーブルデータを格納する
        self.mesh_rows = []#メッシュ表示している行リスト
        self.vtx_row_dict = {}#行と頂点の対応辞書
        self.under_weight_rows = set()#ウェイト合計が1.0未満の行
        self.over_weight_rows = set()#ウェイト合計が1.0より大きい行
        self.over_influence_limit_dict = {}#行と使用中のインフルエンス数の対応辞書,model内で表示を変えるときに参照する
        self.over_influence_limit_rows = set()#インフルエンス数がオーバーしている行
        self.all_editable_rows = set()#編集可能な行のIDを格納
        self.weight_lock_data = set()#テーブルモデルに追加するようのロック状態格納セット
        self.vtx_lock_data_dict = defaultdict(lambda : set())#頂点のロックインフルエンスIDを格納する
        self.node_id_dict = {}#ノード名からノードIDを引く辞書
        self.mesh_model_items_dict = {}#後でメッシュ行に空セルを設定するための辞書
        self.filter_weight_dict = defaultdict(lambda : 0.0)#ウェイト0のインフルエンスの表示、非表示切り替えのためのすべての合計値辞書
        self.vtx_weight_dict = {}#焼きこみ時、正規化ウェイトをUIに反映するための辞書
        self.lock_data_dict = {}#ロック情報を格納する。全頂点捜査後、インフルエンスとセルのロック情報対応を作るための事前データ。
        self.node_influence_id_dict_dict = {}
        
        #self.it_counter.count(string='initalize :')
        for node_id, node in enumerate(self.hl_nodes):
            self.node_id_dict[node] = node_id
            skin_cluster = self.all_skin_clusters[node]
            influences = self.influences_dict[node]
            
            node_influence_id_dict =  {inf:influences.index(inf)  for inf in influences}#インフルエンスとウェイトIDの対応辞書
            self.node_influence_id_dict_dict[node] = node_influence_id_dict
            
            all_influences_count = len(influences)#インフルエンスの総数
            inf_id_list = range(len( influences))#インフルエンスの連番IDリスト
            
            #ロック情報を取得する
            lock_data_dict = self.decode_lock_data(skin_cluster)
            
            self.v_header_list.append(node.split('|')[-1].split(':')[-1])
            self.mesh_rows.append(self.all_rows)
            self.all_rows += 1
            
            #self.it_counter.count(string='000 :')
            
            #メッシュ行にアイテムを設定、とりあえずメッシュ名だけ入れる
            #他の空セルは後ほどゼロカラム計算後に入れる
            items = defaultdict(lambda: None)
            self.mesh_model_items_dict[node] = items
            items[0] = node.split('|')[-1]
            self._data.append(items)
            
            #self.it_counter.count(string='005 :')
            
            if show_all:#全照会するときはノード全体のIDリストを取りに行く
                current_vtx = self.node_vtx_dict[node]
            elif undo:
                current_vtx = self.pre_current_vtx_dict.get(node, [])
            else:#通常取得
                #選択ノード数が多いとここがめっちゃ重い※重さ解消済み
                current_vtx = self.store_skin_weight.om_selected_mesh_vertex(node, show_bad=show_bad)#表示用頂点
            self.pre_current_vtx_dict[node] = current_vtx
            if not current_vtx:#現在の頂点がなければ次へ
                continue
            
            #self.it_counter.count(string='008 :')
                
            filter_vtx = self.show_dict[node]
            target_vertices = sorted(list(set(current_vtx) & set(filter_vtx)))
            
            #self.it_counter.count(string='010 :')
            
            if target_vertices:
                vertex_dict = self.node_vtx_dict[node]
                node_weight_list = self.node_weight_dict[node]
                sel_weight_list = []#選択頂点のリストをまとめる
                
                items = []#各行のカラムを格納するアイテムリスト
                for v in target_vertices:
                    vtx_name = "{0:}.vtx[{1:}]".format(node, v)#IDを頂点名に変換しておく
                    #頂点のロック情報を取得しておく
                    # try:#if v in keys比較が重いので例外処理で高速化
                        # self.lock_data_dict[self.all_rows] = [vtx_name, lock_data_dict[v]]
                    # except:
                        # pass
                        
                    #if v in lock_data_dict.keys():#比較遅い
                        #self.lock_data_dict[self.all_rows] = [vtx_name, lock_data_dict[v]]
                        
                    #例外補足とあまり変わらないかちょっと早い
                    lock_data = lock_data_dict.get(v, None)
                    if lock_data:
                        self.lock_data_dict[self.all_rows] = [vtx_name, lock_data]
                        
                    self.all_editable_rows.add(self.all_rows)
                    self.v_header_list.append(v)
                    
                    weight_list = node_weight_list[v][:]#頂点ウェイトを取得、アンドゥできるように参照ではなくコピー
                    sel_weight_list.append(weight_list)
                    self.vtx_weight_dict[vtx_name] = weight_list#UI更新用
                    self._data.append(weight_list)#モデル用データに入れておく
                    
                    #ウェイトの合計、使用インフルエンス数が不正かどうかを格納しておく
                    weight_sum = round(sum(weight_list), 12)#合計値
                    #print('sum weights :', weight_sum)
                    influence_count = all_influences_count - weight_list.count(0.0)
                    self.over_influence_limit_dict[self.all_rows] = influence_count
                    if weight_sum < 1.0:
                        self.under_weight_rows.add(self.all_rows)
                    if weight_sum > 1.0:
                        self.over_weight_rows.add(self.all_rows)
                    if influence_count > state.MAXIMUM_INFLUENCE_COUNT > 0:
                        self.over_influence_limit_rows.add(self.all_rows)
                        
                    #データ編集用に行をキーに頂点とスキンクラスタを格納しておく
                    self.vtx_row_dict[self.all_rows] = [v, skin_cluster, influences, [], 
                                                                    inf_id_list, vtx_name, node, node_id]
                    self.all_rows += 1#全体の行数を数えておく
                    
                #0フィルター用にインフルエンスごとのウェイト合計値を出す
                if self.filter_but.isChecked():
                    rot_weight_list = map(list, zip(*sel_weight_list))
                    for j, inf_weight in enumerate(rot_weight_list):
                        inf_weight_sum = sum(inf_weight)
                        self.filter_weight_dict[influences[j]] += inf_weight_sum
                        
            #self.it_counter.count(string='020 :')
    
        self.mesh_rows.append(self.all_rows)#最後のメッシュの末尾探索のためにもう一個分メッシュ行を追加しておく
        
        self.counter.count('adjust weight data :')
        
        # Keep Bone が有効なら記憶中のボーンがバインド済みである限り列候補へ残す
        keep_bone_enabled = hasattr(self, 'keep_bone_but') and self.keep_bone_but.isChecked()
        kept_inf_set = set(getattr(self, 'last_selected_influences', [])) if keep_bone_enabled else set()
        if kept_inf_set and hasattr(self, 'influences_dict') and self.hl_nodes:
            bound_infs = set()
            for node in self.hl_nodes:
                bound_infs.update(self.influences_dict.get(node, []))
            for inf in kept_inf_set:
                if inf in bound_infs and inf not in self.all_influences:
                    self.all_influences.append(inf)
            self.all_influences.sort()

        # 表示中のインフルエンスの合計が0ならフィルターする（記憶中のボーンは維持する）
        if self.filter_but.isChecked():
            for inf, value in self.filter_weight_dict.items():
                if inf in self.all_influences:
                    if inf in kept_inf_set:
                        continue
                    # フィルター対象でも Add 検索条件に一致する場合は表示を残す
                    if self.search_joint_list and self.search_but_group.checkedId() == 1:
                        if self._match_influence_search(inf, self.search_joint_list, require_positive=True):
                            continue
                    if value == 0.0:
                        self.all_influences.remove(inf)
                        
        # サーチ文字リストがあれば拡張クエリでフィルタする（Refine モード）
        if self.search_joint_list and self.search_but_group.checkedId() == 0:
            self.all_influences = [
                inf for inf in self.all_influences
                if self._match_influence_search(inf, self.search_joint_list, require_positive=False)
            ]
        
        self.node_influence_id_list_dict = {}#カラム抜けを回避するためのノードごとのリスト辞書
        for node in self.hl_nodes:          
            #未バインドのカラム抜け対策のためインフルエンスとカラムの対応リストを作っておく
            influences = self.influences_dict[node]
            node_influence_id_list = [influences.index(inf)  if inf in influences else None for inf in self.all_influences]       
            self.node_influence_id_list_dict[node] = node_influence_id_list
            
            #メッシュ行にフィルタリング後のインフルエンス数分の空データを設定しておく
            items = self.mesh_model_items_dict[node]
            for inf in self.all_influences:
                items[inf] = None
                
        #インフルエンスのカラーを取得してモデルに渡す
        self.inf_color_list = [cmds.getAttr(j+'.objectColor') for j in self.all_influences]
        #インフルエンス名とカラムの対応辞書を作る
        self.inf_column_dict = {inf:i for i,inf in enumerate(self.all_influences)}
        
        #事前に集めたロック情報をもとにロック設定する
        inf_column_dict_keys = self.inf_column_dict.keys()
        for row, lock_data in self.lock_data_dict.items():
            node = self.vtx_row_dict[row][6]
            node_influence_id_dict = self.node_influence_id_dict_dict[node]
            
            vtx_name = lock_data[0]
            lock_influences = lock_data[1]
            for influence in lock_influences:
                if influence in inf_column_dict_keys:
                    column = self.inf_column_dict[influence]
                    self.weight_lock_data.add((row, column))
                #ロックされているインフルエンスIDをすべて格納する
                try:
                    self.vtx_lock_data_dict[vtx_name].add(node_influence_id_dict[influence])
                except Exception as e:
                    print('lock setting error :', '{}'.format(e))
                    pass
        
        try:#都度メモリをきれいに
            del self.weight_model._data
            self.weight_model._data = {}
        except Exception as e:
            pass
            #print('{}'.format(e), 'in get set' )
        try:#都度メモリをきれいに
            self.weight_model.deleteLater()
            del self.weight_model
        except Exception as e:
            pass
            #print('{}'.format(e), 'in get set' )
        try:
            self.sel_model.deleteLater()
            del self.sel_model
        except Exception as e:
            pass
            #print('{}'.format(e), 'in get set' )
        try:
            debug_log('get_set_skin_weight before TableModel rows={0} influences={1}'.format(len(self._data), len(self.all_influences)))
            self.weight_model = TableModel(self._data, self.view_widget, self.mesh_rows, 
                                            self.all_influences, self.v_header_list, self.inf_color_list)
        except:
            debug_log('get_set_skin_weight TableModel exception')
            return
        self.weight_model.over_influence_limit_dict = self.over_influence_limit_dict
        self.weight_model.under_weight_rows = self.under_weight_rows
        self.weight_model.over_weight_rows = self.over_weight_rows
        self.weight_model.over_influence_limit_rows = self.over_influence_limit_rows
        self.weight_model.norm = self.norm_but.isChecked()#ノーマル状態かどうかを渡しておく
        self.weight_model.weight_lock_cells = self.weight_lock_data#ロック状態を渡す
        
        self.counter.count('setup ui model :')
            
        self.sel_model = QItemSelectionModel(self.weight_model)#選択モデルをつくる
        self.sel_model.selectionChanged.connect(self.cell_changed)#シグナルをつなげておく
        self.view_widget.setModel(self.weight_model)#表示用モデル設定
        self.view_widget.setSelectionModel(self.sel_model)#選択用モデルを設定
        self.selected_items = []
        
        if not self.show_flag:
            self.show_flag = True
            self.show()
        
        self.counter.count('ui data finalaize :')
        
        #前回の選択を格納
        self.pre_hl_nodes = self.hl_nodes
        
        #ヘッダーのサイズを整える
        set_table_header_width(self.view_widget, self.weight_model, add=(state.FLOAT_DECIMALS-2)*7-10)
        
        self.counter.count('ui create model list dict :')
        
        self.counter.lap_print(print_flag=state.COUNTER_PRINT, window=self)
        #self.it_counter.integration_print(print_flag=state.COUNTER_PRINT, window=self)
        debug_log('get_set_skin_weight end rows={0} influences={1}'.format(len(self._data), len(self.all_influences)))
        restored_sel = self.restore_kept_bone_selection()
        self.update_quick_bind_button()
        if not restored_sel:
            if hasattr(self, 'joint_hl_but') and self.joint_hl_but.isChecked():
                self.hilite_joints()
            if hasattr(self, 'used_joint_vis_but') and self.used_joint_vis_but.isChecked():
                self.update_used_joint_visibility()
            self.update_bone_visualizer(clear=clear)
            self.update_bone_remap_window()
        self.update_joint_search_dialog()
        self.update_bone_picker_window()
        
        
    #セルの選択変更があった場合に現在の選択セルを格納する
    #@timer
    #@prof.profileFunction()
    def cell_changed(self, selected, deselected):
        if self.closed_flag:
            debug_log('cell_changed skip closed window')
            return
        debug_log('cell_changed start joint_hl={0}'.format(self.joint_hl_but.isChecked()))
            
        self.reset_message()#メッセージ初期化
        if self.pre_tool:
            self.hilite_flag = True#get_setしないためのフラグ
            #print('hl nodes :', self.hl_nodes)
            if self.pre_comp_mode:
                cmds.selectMode(co=True)
                #cmds.hilite(self.hl_nodes, r=True)
            else:
                cmds.selectMode(o=True)
            if self.pre_tool:
                cmds.setToolTo(self.pre_tool)
            #print('header selection :', self.header_selection)
            if self.header_selection:
                cmds.hilite(self.hl_nodes, r=True)
                cmds.select(self.header_selection, r=True)
            self.pre_tool = None
            self.header_selection = None
        self.select_change_flag = True
        #ここめっちゃ重い↓対応要件等
        debug_log('cell_changed before selectedIndexes')
        self.selected_items =  self.sel_model.selectedIndexes()
        debug_log('cell_changed after selectedIndexes count={0}'.format(len(self.selected_items)))
        #self.selected_items =  []
        
        if not self.selected_items:
            self._set_slider_and_spinbox_silent(0.0)
        else:
            # 選択されたボーン列を記憶し、次の頂点選択時にも引き継げるようにする
            if not getattr(self, '_restoring_bone_selection', False):
                sel_cols = sorted(set(item.column() for item in self.selected_items))
                kept = [
                    self.all_influences[c]
                    for c in sel_cols
                    if 0 <= c < len(self.all_influences)
                ]
                if kept:
                    self.last_selected_influences = kept
                    self.update_quick_bind_button()
            self.change_add_mode(self.add_mode, change_only=True)#スピンボックスの値を更新するためにAddMode関数にいく
        self.pre_add_value = 0.0#加算量を初期化
        self.sel_rows = list(set([item.row() for item in self.selected_items]))
        
        if self.highlite_but.isChecked() and not getattr(self, '_restoring_bone_selection', False):
            debug_log('cell_changed before hilite_vertices')
            self.hilite_vertices()
            debug_log('cell_changed after hilite_vertices')

        if hasattr(self, 'joint_hl_but') and self.joint_hl_but.isChecked():
            self.hilite_joints()

        if hasattr(self, 'used_joint_vis_but') and self.used_joint_vis_but.isChecked():
            self.update_used_joint_visibility()

        self.update_bone_visualizer()
        self.update_bone_remap_window()
        self.update_joint_search_dialog()
        self.update_bone_picker_window()
        debug_log('cell_changed end')
        
    @timer
    def hilite_vertices(self):
        with qt.suspend_undo():
            self.counter.reset()

            vertices = []
            selected_item = self.sel_model.currentIndex()
            if not selected_item:
                return

            rows = sorted(set([item.row() for item in self.selected_items if item.row() in self.vtx_row_dict]))
            if rows:
                # 高速選択するために頂点番号の連続範囲を vtx[a:b] 形式へ圧縮する
                pre_vid = None
                pre_node = None
                vtx_connection = []
                for r in rows:
                    node = self.vtx_row_dict[r][6]
                    vid = self.vtx_row_dict[r][0]
                    if pre_node is None:
                        pre_node = node
                        pre_vid = vid
                        vtx_connection = [vid]
                        continue
                    if pre_node == node and pre_vid == vid - 1:
                        pre_vid = vid
                        vtx_connection.append(vid)
                        continue
                    vertices.append('{0}.vtx[{1}:{2}]'.format(pre_node, vtx_connection[0], vtx_connection[-1]))
                    pre_node = node
                    pre_vid = vid
                    vtx_connection = [vid]
                if pre_node is not None and vtx_connection:
                    vertices.append('{0}.vtx[{1}:{2}]'.format(pre_node, vtx_connection[0], vtx_connection[-1]))

            self.hilite_flag = True
            self.counter.count(string='get cell vtx :')

            orig_sel = self.original_selection or []
            if not vertices:
                if orig_sel:
                    cmds.select(orig_sel, r=True)
            else:
                if cmds.selectMode(q=True, o=True):
                    cmds.select(vertices + orig_sel, r=True)
                else:
                    cmds.select(vertices, r=True)

            self.counter.count(string='select vtx :')
            self.counter.lap_print(print_flag=state.COUNTER_PRINT)

    # ハイライトボタン押された時に選択反映するスロット
    def reset_hilite(self, force_reset=False):
        with qt.suspend_undo():
            if not self.highlite_but.isChecked() or force_reset:
                self.hilite_flag = True
                cmds.select(cl=True)
                if self.original_selection:
                    cmds.select(self.original_selection, r=True)
            else:
                self.cell_changed(self.selected_items, None)
            
    def hilite_joints(self):
        """Joint Hilite を最新状態へ更新する。"""
        joint_display_helper.update_joint_hilite(self, maya_ver=MAYA_VER)

    def reset_joint_hl(self):
        """Joint Hilite ボタンの ON/OFF に応じてボーン色を更新または復元する（Remap ハイライトは維持）。"""
        self.joint_hilite = self.joint_hl_but.isChecked()
        joint_display_helper.update_joint_hilite(self, maya_ver=MAYA_VER)

    def disable_joint_override(self):
        """退避していた元のジョイントカラー設定をすべて復元する。"""
        joint_display_helper.restore_joint_colors(keep_remap=True)

    def _set_joint_hilite_mode(self, mode):
        """Joint Hilite の表示モードを切り替える。"""
        self.joint_hilite_mode = mode
        if self.joint_hl_but.isChecked():
            joint_display_helper.update_joint_hilite(self, maya_ver=MAYA_VER)

    def pop_joint_hilite_menu(self):
        """Joint Hilite ボタン右クリック時のモード切替メニューを表示する。"""
        menu = QMenu(self)
        mode_group = QActionGroup(menu)
        mode_group.setExclusive(True)
        current_mode = getattr(self, 'joint_hilite_mode', 'heatmap')

        heat_act = menu.addAction(
            lang.Lang(en='Mode: Vertex Weight Heatmap', ja=u'モード：頂点ウェイト・ヒートマップ').output()
        )
        heat_act.setCheckable(True)
        heat_act.setChecked(current_mode != 'selected')
        heat_act.triggered.connect(lambda: self._set_joint_hilite_mode('heatmap'))
        mode_group.addAction(heat_act)

        sel_act = menu.addAction(
            lang.Lang(en='Mode: Selected Column Only', ja=u'モード：選択列のボーンのみ強調').output()
        )
        sel_act.setCheckable(True)
        sel_act.setChecked(current_mode == 'selected')
        sel_act.triggered.connect(lambda: self._set_joint_hilite_mode('selected'))
        mode_group.addAction(sel_act)

        exec_func = getattr(menu, 'exec_', None) or getattr(menu, 'exec')
        exec_func(QCursor.pos())

    def restore_used_joint_visibility(self):
        """退避していたジョイントの drawStyle 設定をすべて復元する。"""
        joint_display_helper.restore_used_joints()

    def update_used_joint_visibility(self):
        """Used Joints（.drawStyle ピンポイント表示）を最新状態へ更新する。"""
        self.used_joints = self.used_joint_vis_but.isChecked()
        joint_display_helper.update_used_joints(self)

    def _set_used_joints_mode(self, mode):
        """Used Joints の表示範囲モードを切り替える。"""
        self.used_joints_mode = mode
        if self.used_joint_vis_but.isChecked():
            joint_display_helper.update_used_joints(self)

    def pop_used_joints_menu(self):
        """Used Joints ボタン右クリック時の表示範囲切替メニューを表示する。"""
        menu = QMenu(self)
        mode_group = QActionGroup(menu)
        mode_group.setExclusive(True)
        current_mode = getattr(self, 'used_joints_mode', 'exact')

        exact_act = menu.addAction(
            lang.Lang(en='Show: Used Bones Only', ja=u'表示範囲：使用ボーンのみ').output()
        )
        exact_act.setCheckable(True)
        exact_act.setChecked(current_mode != 'with_children')
        exact_act.triggered.connect(lambda: self._set_used_joints_mode('exact'))
        mode_group.addAction(exact_act)

        child_act = menu.addAction(
            lang.Lang(en='Show: Keep Bone Shape (+Direct Children)', ja=u'表示範囲：骨形状維持（直下の子1階層を含む）').output()
        )
        child_act.setCheckable(True)
        child_act.setChecked(current_mode == 'with_children')
        child_act.triggered.connect(lambda: self._set_used_joints_mode('with_children'))
        mode_group.addAction(child_act)

        exec_func = getattr(menu, 'exec_', None) or getattr(menu, 'exec')
        exec_func(QCursor.pos())

    def toggle_bone_visualizer(self):
        """3D ビューポート上のウェイトボーン視覚化の ON/OFF を切り替える。"""
        self.bone_vis = self.bone_vis_but.isChecked()
        self.update_bone_visualizer()

    def _set_bone_vis_option(self, key, value):
        """ボーン視覚化の表示オプションを更新して即座に再描画する。"""
        setattr(self, key, value)
        bone_visualizer.set_options(
            show_lines=getattr(self, 'bone_vis_lines', True),
            show_labels=getattr(self, 'bone_vis_labels', True),
            dashed_tiny=getattr(self, 'bone_vis_dashed_tiny', True),
            aggregate_mode=getattr(self, 'bone_vis_mode', 'avg'),
        )
        self.update_bone_visualizer()

    def pop_bone_vis_menu(self):
        """Bone Vis ボタン右クリック時のオプションメニューを表示する。"""
        menu = QMenu(self)

        lines_act = menu.addAction(lang.Lang(en='Show Lines', ja=u'接続ラインを表示').output())
        lines_act.setCheckable(True)
        lines_act.setChecked(getattr(self, 'bone_vis_lines', True))
        lines_act.triggered.connect(lambda checked: self._set_bone_vis_option('bone_vis_lines', checked))

        labels_act = menu.addAction(lang.Lang(en='Show Labels', ja=u'ボーン名・ウェイト値ラベルを表示').output())
        labels_act.setCheckable(True)
        labels_act.setChecked(getattr(self, 'bone_vis_labels', True))
        labels_act.triggered.connect(lambda checked: self._set_bone_vis_option('bone_vis_labels', checked))

        tiny_act = menu.addAction(lang.Lang(en='Dashed Tiny Weights (<5%)', ja=u'微小ウェイト（5%未満）を破線で強調').output())
        tiny_act.setCheckable(True)
        tiny_act.setChecked(getattr(self, 'bone_vis_dashed_tiny', True))
        tiny_act.triggered.connect(lambda checked: self._set_bone_vis_option('bone_vis_dashed_tiny', checked))

        menu.addSeparator()

        mode_group = QActionGroup(menu)
        mode_group.setExclusive(True)
        current_mode = getattr(self, 'bone_vis_mode', 'avg')

        avg_act = menu.addAction(lang.Lang(en='Multi-Vertex Mode: Average', ja=u'複数頂点時：平均ウェイト').output())
        avg_act.setCheckable(True)
        avg_act.setChecked(current_mode != 'max')
        avg_act.triggered.connect(lambda: self._set_bone_vis_option('bone_vis_mode', 'avg'))
        mode_group.addAction(avg_act)

        max_act = menu.addAction(lang.Lang(en='Multi-Vertex Mode: Max', ja=u'複数頂点時：最大ウェイト').output())
        max_act.setCheckable(True)
        max_act.setChecked(current_mode == 'max')
        max_act.triggered.connect(lambda: self._set_bone_vis_option('bone_vis_mode', 'max'))
        mode_group.addAction(max_act)

        exec_func = getattr(menu, 'exec_', None) or getattr(menu, 'exec')
        exec_func(QCursor.pos())

    def update_bone_visualizer(self, clear=False):
        """現在の頂点選択・セル選択・ウェイト値をもとに 3D ボーン視覚化を更新する。"""
        if not hasattr(self, 'bone_vis_but'):
            return
        if clear:
            bone_visualizer.clear_state(delete_node=not self.bone_vis_but.isChecked())
            return
        bone_visualizer.set_options(
            show_lines=getattr(self, 'bone_vis_lines', True),
            show_labels=getattr(self, 'bone_vis_labels', True),
            dashed_tiny=getattr(self, 'bone_vis_dashed_tiny', True),
            aggregate_mode=getattr(self, 'bone_vis_mode', 'avg'),
        )
        bone_visualizer.update_from_window(
            self,
            max_weight=state.MAXIMUM_WEIGHT,
            decimals=state.FLOAT_DECIMALS,
        )

    def open_bone_remap_window(self):
        """ボーン付け替え（ウェイト移動）専用ウィンドウを開く。"""
        win = getattr(self, 'bone_remap_window', None)
        if win is not None:
            try:
                if win.isVisible():
                    win.refresh_from_editor(max_weight=state.MAXIMUM_WEIGHT, decimals=state.FLOAT_DECIMALS)
                    win.raise_()
                    win.activateWindow()
                    return
            except Exception:
                self.bone_remap_window = None

        self.bone_remap_window = bone_remap_window.BoneRemapWindow(self)
        self.bone_remap_window.refresh_from_editor(max_weight=state.MAXIMUM_WEIGHT, decimals=state.FLOAT_DECIMALS)
        self.bone_remap_window.show()
        self.bone_remap_window.raise_()
        self.bone_remap_window.activateWindow()

    def update_bone_remap_window(self):
        """ボーン付け替えウィンドウが開いている場合に表示をリアルタイム更新する。"""
        win = getattr(self, 'bone_remap_window', None)
        if win is None:
            return
        try:
            if win.isVisible():
                win.refresh_from_editor(max_weight=state.MAXIMUM_WEIGHT, decimals=state.FLOAT_DECIMALS)
        except Exception:
            self.bone_remap_window = None

    def open_model_edit_window(self):
        """簡易モデリング修正（ウェイト保護・補間復元）ウィンドウを開く。"""
        win = getattr(self, 'model_edit_window', None)
        if win is not None:
            try:
                if win.isVisible():
                    win.reload_record_list()
                    win.raise_()
                    win.activateWindow()
                    return
            except Exception:
                self.model_edit_window = None

        self.model_edit_window = model_edit_window.ModelEditWindow(self)
        self.model_edit_window.show()
        self.model_edit_window.raise_()
        self.model_edit_window.activateWindow()

    def toggle_keep_bone(self):
        """Keep Bone の ON/OFF 切替時に記憶状態と表示を更新する。"""
        self.keep_bone = self.keep_bone_but.isChecked()
        if not self.keep_bone:
            self.last_selected_influences = []
            self.update_quick_bind_button()
            if self.filter_but.isChecked():
                self.get_set_skin_weight(filter=True)
        else:
            if getattr(self, 'selected_items', None):
                sel_cols = sorted(set(item.column() for item in self.selected_items))
                self.last_selected_influences = [
                    self.all_influences[c]
                    for c in sel_cols
                    if 0 <= c < len(self.all_influences)
                ]
            self.update_quick_bind_button()

    def restore_kept_bone_selection(self):
        """直前に選択していたボーン列を新しい頂点行に対して自動選択する。"""
        if not hasattr(self, 'keep_bone_but') or not self.keep_bone_but.isChecked():
            return False
        kept_infs = getattr(self, 'last_selected_influences', None) or []
        if not kept_infs or not getattr(self, 'all_editable_rows', None):
            return False
        if not hasattr(self, 'inf_column_dict') or not hasattr(self, 'sel_model'):
            return False

        target_cols = [
            self.inf_column_dict[inf]
            for inf in kept_infs
            if inf in self.inf_column_dict
        ]
        if not target_cols:
            return False

        editable_rows = sorted(self.all_editable_rows)
        selection = QItemSelection()
        first_valid_index = None

        for col in target_cols:
            # 連続する行範囲ごとにまとめて QItemSelection へ追加して高速化する
            range_start = None
            range_end = None
            for row in editable_rows:
                node = self.vtx_row_dict[row][6]
                inf_id_list = self.node_influence_id_list_dict.get(node)
                if not inf_id_list or col >= len(inf_id_list) or inf_id_list[col] is None:
                    if range_start is not None:
                        top_idx = self.weight_model.index(range_start, col)
                        bot_idx = self.weight_model.index(range_end, col)
                        selection.select(top_idx, bot_idx)
                        if first_valid_index is None:
                            first_valid_index = top_idx
                        range_start = None
                        range_end = None
                    continue
                if range_start is None:
                    range_start = row
                    range_end = row
                elif row == range_end + 1:
                    range_end = row
                else:
                    top_idx = self.weight_model.index(range_start, col)
                    bot_idx = self.weight_model.index(range_end, col)
                    selection.select(top_idx, bot_idx)
                    if first_valid_index is None:
                        first_valid_index = top_idx
                    range_start = row
                    range_end = row
            if range_start is not None:
                top_idx = self.weight_model.index(range_start, col)
                bot_idx = self.weight_model.index(range_end, col)
                selection.select(top_idx, bot_idx)
                if first_valid_index is None:
                    first_valid_index = top_idx

        if selection.isEmpty() or first_valid_index is None:
            return False

        self._restoring_bone_selection = True
        try:
            self.sel_model.setCurrentIndex(first_valid_index, QItemSelectionModel.NoUpdate)
            self.sel_model.select(selection, QItemSelectionModel.ClearAndSelect)
        finally:
            self._restoring_bone_selection = False
        return True

    def _get_missing_kept_influences(self):
        """記憶中ボーンのうち現在の対象メッシュに未バインドのものを返す。"""
        if not hasattr(self, 'keep_bone_but') or not self.keep_bone_but.isChecked():
            return []
        kept_infs = getattr(self, 'last_selected_influences', None) or []
        hl_nodes = getattr(self, 'hl_nodes', None) or []
        influences_dict = getattr(self, 'influences_dict', None) or {}
        if not kept_infs or not hl_nodes:
            return []

        missing = []
        for inf in kept_infs:
            if not cmds.objExists(inf):
                continue
            for node in hl_nodes:
                if inf not in influences_dict.get(node, []):
                    missing.append(inf)
                    break
        return missing

    def update_quick_bind_button(self):
        """未バインドの記憶ボーンがあるときだけワンクリック追加ボタンを表示する。"""
        if not hasattr(self, 'quick_bind_but'):
            return
        missing = self._get_missing_kept_influences()
        if not missing:
            self.quick_bind_but.setVisible(False)
            return

        short_names = [inf.split('|')[-1].split(':')[-1] for inf in missing]
        if len(short_names) == 1:
            label = '+ Bind: ' + short_names[0]
        else:
            label = '+ Bind: {0} (+{1})'.format(short_names[0], len(short_names) - 1)
        self.quick_bind_but.setText(label)
        self.quick_bind_but.setVisible(True)

    def quick_bind_kept_influences(self):
        """記憶中の未バインドボーンを現在の対象メッシュへ追加し、即座にセル選択する。"""
        missing = self._get_missing_kept_influences()
        hl_nodes = getattr(self, 'hl_nodes', None) or []
        if not missing or not hl_nodes:
            return

        added_any = False
        for node in hl_nodes:
            skin_clusters = cmds.ls(cmds.listHistory(node), type='skinCluster') or []
            if not skin_clusters:
                continue
            current_infs = cmds.ls(cmds.skinCluster(skin_clusters[0], q=True, inf=True) or [], l=True) or []
            to_add = [inf for inf in missing if inf not in current_infs and cmds.objExists(inf)]
            if to_add:
                cmds.skinCluster(skin_clusters[0], e=True, ai=to_add, lw=True, ug=False, wt=0, ps=0)
                added_any = True

        if added_any:
            self.get_set_skin_weight(cycle=True)
            names = ', '.join(inf.split('|')[-1].split(':')[-1] for inf in missing)
            self.set_message(msg='- Bound & Selected : {0} -'.format(names), error=False)

        
    joint_tool = 0
    def change_joint_tool_mode(self, id):
        self.joint_tool = id
        
    #ヘッダーからジョイントを選択しに行く
    pre_tool = None
    header_selection = None
    pre_comp_mode = None
    target_tool_list = [None, 'scaleSuperContext', 'RotateSuperContext', 'moveSuperContext']
    def _select_influence_in_viewport(self, influence):
        """指定インフルエンスをビューポートで選択し、設定されたマニピュレータへ切り替える。"""
        if not influence or not cmds.objExists(influence):
            return
        with qt.suspend_undo():
            self.pre_tool = cmds.currentCtx()  # ツールを復旧するために取得
            self.header_selection = list(self.original_selection) if self.original_selection else []
            self.pre_comp_mode = cmds.selectMode(q=True, co=True)
            self.hilite_flag = True  # get_set しないためのフラグ
            cmds.select(influence)
            if self.target_tool_list[self.joint_tool]:
                cmds.setToolTo(self.target_tool_list[self.joint_tool])

    def pop_header_bone_menu(self, column):
        """ボーン名ヘッダーの Shift / Ctrl 右クリック時にボーン名コピー・選択メニューを表示する。"""
        all_infs = getattr(self, 'all_influences', None) or []
        if column < 0 or column >= len(all_infs):
            return
        influence = all_infs[column]
        short_name = influence.split('|')[-1]

        menu = QMenu(self)
        copy_act = menu.addAction(
            lang.Lang(
                en='Copy Bone Name ({0})',
                ja=u'ボーン名をコピー ({0})',
            ).output().format(short_name)
        )
        copy_act.triggered.connect(
            lambda _=False, jp=influence: self.copy_bone_names_to_clipboard([jp], full_path=False)
        )

        # 複数列が選択中なら選択ボーン名の一括コピーも提供する
        sel_infs = []
        seen_cols = set()
        for item in (getattr(self, 'selected_items', None) or []):
            col = item.column()
            if col not in seen_cols and 0 <= col < len(all_infs):
                seen_cols.add(col)
                sel_infs.append(all_infs[col])
        if len(sel_infs) > 1:
            copy_all_act = menu.addAction(
                lang.Lang(
                    en='Copy All Selected Bone Names ({0})',
                    ja=u'選択中のボーン名をすべてコピー ({0}本)',
                ).output().format(len(sel_infs))
            )
            copy_all_act.triggered.connect(
                lambda _=False, jps=list(sel_infs): self.copy_bone_names_to_clipboard(jps, full_path=False)
            )

        copy_path_act = menu.addAction(
            lang.Lang(
                en='Copy Bone Full Path',
                ja=u'ボーンのフルパスをコピー',
            ).output()
        )
        copy_path_act.triggered.connect(
            lambda _=False, jp=influence: self.copy_bone_names_to_clipboard([jp], full_path=True)
        )

        menu.addSeparator()
        sel_act = menu.addAction(
            lang.Lang(
                en='Select Joint ({0})',
                ja=u'ジョイントを選択 ({0})',
            ).output().format(short_name)
        )
        sel_act.triggered.connect(lambda _=False, jp=influence: self._select_influence_in_viewport(jp))

        exec_func = getattr(menu, 'exec_', None) or getattr(menu, 'exec')
        exec_func(QCursor.pos())

    def select_joint_from_header(self):
        if not getattr(self, 'all_influences', None):
            return
        v_header = self.view_widget.verticalHeader()
        v_header_width = v_header.sizeHint().width()
        pos = self.view_widget.mapFromGlobal(QCursor.pos())
        pos_x = pos.x() - v_header_width - 2  # 誤差補正
        column = self.view_widget.columnAt(pos_x)
        if column < 0 or column >= len(self.all_influences):
            return
        shift_mod = qt.check_key_modifiers(Qt.ShiftModifier)
        ctrl_mod = qt.check_key_modifiers(Qt.ControlModifier)
        if shift_mod or ctrl_mod:
            self.pop_header_bone_menu(column)
            return
        influence = self.all_influences[column]
        self._select_influence_in_viewport(influence)

    # Adjustボタン押したとき、頂点選択しにいく
    def select_vertex_from_cells(self):
        rows = list(set([item.row() for item in self.selected_items if item.row() in self.vtx_row_dict]))
        vertices = [self.vtx_row_dict[r][5] for r in rows]
        if vertices:
            with qt.undo_chunk('SIWE_SelectVertexFromCells'):
                cmds.selectMode(co=True)
                cmds.select(vertices, r=True)
            
    #選択されているセルの行のみの表示に絞る
    def show_selected_cells(self):
        rows = list(set([item.row() for item in self.selected_items]))
        self.show_dict = defaultdict(lambda : [])
        self.show_dict['clear'] = []#何も選択されてなかったときのために初期値与える
        for r in rows:
            vtx_row_dict = self.vtx_row_dict[r]
            node = vtx_row_dict[6]
            self.show_dict[node]  += [vtx_row_dict[0]]
        self.get_set_skin_weight(show_dict=self.show_dict)
                
    def show_all_cells(self):
        self.get_set_skin_weight(cycle=True, show_all=True)
            
    def show_bad_rows(self):
        self.get_set_skin_weight(show_all=True)
        rows = set()
        self.show_dict = defaultdict(lambda : [])
        self.show_dict['clear'] = []#何も見つからなかった時のために初期値与える
        if self.under_wt_but.isChecked():
            rows = rows | self.weight_model.under_weight_rows
        if self.over_wt_but.isChecked():
            rows = rows | self.weight_model.over_weight_rows
        if self.over_inf_but.isChecked():
            rows = rows | self.weight_model.over_influence_limit_rows
        for r in rows:
            vtx_row_dict = self.vtx_row_dict[r]
            node = vtx_row_dict[6]
            self.show_dict[node]  += [vtx_row_dict[0]]
        self.get_set_skin_weight(show_dict=self.show_dict, show_bad=True)
        

    #ビューの状態を更新する
    def refresh_table_view(self):
        #フォーカス移してテーブルの状態を更新する
        self.view_widget.setFocus()
        self.view_widget.clearFocus()
        self.view_widget.setFocus()
        #self.setFocus()
        #self.clearFocus()
        #縦ヘッダーを取得
        header = self.view_widget.verticalHeader()
        #ヘッダーの数だけセクション（行の頭）の状態をアップデートする
        for i in range(header.count()):
            header.updateSection(i)
        '''
        try:
            self.weight_model.reset()
        except:
            pass
        '''
        if hasattr(self, 'joint_hl_but') and self.joint_hl_but.isChecked():
            self.hilite_joints()
        if hasattr(self, 'used_joint_vis_but') and self.used_joint_vis_but.isChecked():
            self.update_used_joint_visibility()
        self.update_bone_visualizer()
        self.update_bone_remap_window()
        self.update_joint_search_dialog()
        self.update_bone_picker_window()

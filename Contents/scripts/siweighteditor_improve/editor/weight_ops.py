# -*- coding: utf-8 -*-
"""SIWeightEditor Improve のウェイト編集・計算・焼き込み・スムース・コピペ・ロック Mixin。"""

import copy
import itertools
from collections import defaultdict

from maya import cmds
import maya.OpenMaya as om
import maya.api.OpenMaya as om2

from ..maya_version import MAYA_VER
from .. import common
from .. import freeze
from .. import joint_rule_editor
from .. import lang
from .. import qt
from .. import smooth_setting_editor
from .. import weight
from ..qt import *
from . import state
from .state import timer
from .widgets import PopInputBox, set_table_header_width

if state.PYTHON_VER >= 3:
    round = common.round_half_up


class EditorWeightOpsMixin(object):
    """ウェイトの数値計算・焼き込み・ロック・コピペ・スムース操作を提供する Mixin。"""

    def open_joint_rule_editor(self):
        joint_rule_editor.Option()
        
    #サブツールコマンド群-----------------------------------------------------------
    move_inf_list = []
    def copy_move_influences(self):
        self.move_inf_list = []
        for id, inf in enumerate(self.all_influences):
            is_active = self.headerView.check_section_is_active(id)
            if is_active:
                self.move_inf_list.append(inf)
        #print('get move inf :', self.move_inf_list)
        
    def paste_move_influences(self):
        if not self.move_inf_list:
            return
        self.counter.reset()
        self.update_rows = set()
        self.row_column_dict = defaultdict(lambda : [])
        for cell_id in self.selected_items:
            row = cell_id.row()
            column = cell_id.column() 
            self.update_rows.add(row)
            self.row_column_dict[row].append(column)#行に対応する選択カラムを記録する
        #コピー元インフルエンスのノード毎IDを事前に辞書化する
        self.node_copy_id_dict = {}
        for node in self.hl_nodes:
            influences = self.influences_dict[node]
            copy_id_set = {influences.index(inf) for inf in self.move_inf_list if inf in influences}
            self.node_copy_id_dict[node] = copy_id_set
        
        self.precalculate_bake_weights(move_weight=True)
        self.counter.lap_print(print_flag=state.COUNTER_PRINT, window=self)
        
        
    #インフルエンスの追加削除
    def add_remove_influences(self, mode=''):
        add_remove_inf_list = []
        for id, inf in enumerate(self.all_influences):
            is_active = self.headerView.check_section_is_active(id)
            if is_active:
                add_remove_inf_list.append(inf)

        sel_joints = cmds.ls(sl=True, type='joint', l=True) or []
        add_remove_inf_list = list(set(add_remove_inf_list + sel_joints))

        with qt.undo_chunk('SIWE_AddRemoveInfluences'):
            for node in self.hl_nodes:
                srcSkinCluster = cmds.ls(cmds.listHistory(node) or [], type='skinCluster') or []
                if not srcSkinCluster:
                    continue
                srcSkinCluster = srcSkinCluster[0]
                influences = cmds.skinCluster(srcSkinCluster, q=True, inf=True) or []
                influences = cmds.ls(influences, l=True) or []
                if mode == 'add':
                    sub_influences = list(set(add_remove_inf_list) - set(influences))
                    if sub_influences:
                        cmds.skinCluster(srcSkinCluster, e=True, ai=sub_influences, lw=True, ug=False, wt=0, ps=0)
                elif mode == 'remove':
                    if len(influences) <= 1:
                        continue
                    sub_influences = list(set(add_remove_inf_list) & set(influences))
                    if sub_influences:
                        cmds.skinCluster(srcSkinCluster, e=True, ri=sub_influences)

            if self.original_selection:
                cmds.select(self.original_selection, r=True)
        self.get_set_skin_weight()

    # 未使用インフルエンスの削除
    def remove_unused_influences(self):
        with qt.undo_chunk('SIWE_RemoveUnusedInfluences'):
            for node in self.hl_nodes:
                srcSkinCluster = cmds.ls(cmds.listHistory(node) or [], type='skinCluster') or []
                if not srcSkinCluster:
                    continue
                srcSkinCluster = srcSkinCluster[0]
                all_influences = cmds.skinCluster(srcSkinCluster, q=True, inf=True) or []
                weighted_influences = cmds.skinCluster(srcSkinCluster, q=True, wi=True) or []
                sub_influences = list(set(all_influences) - set(weighted_influences))
                if sub_influences and len(sub_influences) < len(all_influences):
                    cmds.skinCluster(srcSkinCluster, e=True, ri=sub_influences)
        self.get_set_skin_weight()
                
    #頂点ウェイトコピーを独自実装
    copy_weights_list = []
    copy_influences_list = []
    def vertex_copy(self):
        self.copy_vtx_rows = list(set([item.row() for item in self.selected_items]))
        #row = max(rows)
        #vertex = self.vtx_row_dict[row][5]
        #skin_cluster = self.vtx_row_dict[row][1]
        self.copy_weights_list = []
        self.copy_influences_list = []
        for row in self.copy_vtx_rows:
            self.copy_weights_list.append(self._data[row])
            self.copy_influences_list.append(self.vtx_row_dict[row][2])
        #print('copy vertices :', vertex)
        #print('copy nfluences :', self.copy_influences_list)
        #print('copy weights :', self.copy_weights_list)
        #モデルのコピー対象リストを更新
        self.weight_model.copy_vertex_rows = self.copy_vtx_rows
        self.sel_model.clearSelection()
        self.refresh_table_view()
        self.change_static_text()#右クリック時リフレッシュされない対策
        
    #頂点ウェイトのペースト
    def vertex_paste(self):
        if not self.copy_weights_list or not self.selected_items:
            self.change_static_text()#右クリック時リフレッシュされない対策
            return
        self.update_rows = set()
        self.row_column_dict = defaultdict(lambda : [])
        self.locked_cells = self.weight_model.weight_lock_cells#ロック情報を取得
        rows = list(set([item.row() for item in self.selected_items]))
        paste_influences_dict = {}
        
        mesh_list = []
        mesh_inflences_list = []
        
        #メッシュに足りないインフルエンスを追加する
        for row in rows:
            node = self.vtx_row_dict[row][6]
            if node in mesh_list:
                continue
            mesh_list.append(node)
            mesh_inflences_list.append(self.vtx_row_dict[row][2])
        #print('mesh inf :', mesh_list, mesh_inflences_list)
        copy_influences  = []
        for influences in self.copy_influences_list:
            if not influences in copy_influences:
                copy_influences.append(influences)
        #コピー元のインフルエンス
        all_copy_influences = []
        for influences in copy_influences:
            all_copy_influences += influences
        all_copy_influences = list(set(all_copy_influences))
        #print('all inf :', all_copy_influences)
        # なければ対象スキンクラスタへ不足インフルエンスを追加し、ウェイト焼き込みと同一チャンクでまとめる
        with qt.undo_chunk('SIWE_VertexPaste'):
            add_inf_flag = False
            for mesh_inflences, node in zip(mesh_inflences_list, mesh_list):
                sub_influences = list(set(all_copy_influences) - set(mesh_inflences))
                if sub_influences:
                    skin_clusters = cmds.ls(cmds.listHistory(node) or [], type='skinCluster') or []
                    target_skin = skin_clusters[0] if skin_clusters else node
                    add_inf_flag = True
                    cmds.skinCluster(target_skin, e=True, ai=sub_influences, lw=True, ug=False, wt=0, ps=0)
            # 追加が発生したらUI更新する
            if add_inf_flag:
                self.get_set_skin_weight(cycle=True)
                # コピーセル情報復元
                self.weight_model.copy_vertex_rows = self.copy_vtx_rows
                self.weight_model.copy_cell_list = self.copy_cell_list

            # 選択行にペースト
            copy_row_count = len(self.copy_influences_list)
            for i, row in enumerate(rows):
                paste_weights = self._data[row]
                paste_influences = self.vtx_row_dict[row][2]

                copy_id = i % copy_row_count
                copy_influences = self.copy_influences_list[copy_id]
                copy_weights = self.copy_weights_list[copy_id]

                for j, (pw, pinf) in enumerate(zip(paste_weights, paste_influences)):
                    if pinf in copy_influences:
                        try:
                            inf_id = copy_influences.index(pinf)
                            cw = copy_weights[inf_id]
                            paste_weights[j] = cw
                        except Exception:
                            paste_weights[j] = 0.0
                    else:
                        paste_weights[j] = 0.0
                self.update_rows.add(row)

            # ベイク計算に入る
            self.precalculate_bake_weights()
        self.change_static_text()#右クリック時リフレッシュされない対策
                    
    #エクセルライクコピー
    copy_cell_list = []
    copy_value_list = []
    copy_relative_pos_list = []#開始位置からの相対位置リスト
    @timer
    def cell_copy(self):
        self.copy_cell_list = self.selected_items
        #print('copy cells :', self.copy_cell_list)
        self.weight_model.copy_cell_list = self.copy_cell_list
        self.sel_model.clearSelection()
        self.refresh_table_view()
        
        #コピーした値をリストにしておく
        self.copy_value_list = []
        self.copy_relative_pos_list = []#開始位置からの相対位置リスト
        
        if not self.copy_cell_list:
            return
            
        pivot_row = self.copy_cell_list[0].row()
        pivot_column = self.copy_cell_list[0].column()
        for cell_id in self.copy_cell_list:
            row = cell_id.row()
            column = cell_id.column() 
            sub_row = row - pivot_row
            sub_column = column - pivot_column
            self.copy_relative_pos_list.append([sub_row, sub_column])
            value = self.weight_model.get_data(row=row, column=column)#もとの値
            self.copy_value_list.append(value)
        #コピー・ペーストのバウンディングサイズ
        min_row = min(self.copy_relative_pos_list, key=lambda x: x[0])[0]
        max_row = max(self.copy_relative_pos_list, key=lambda x: x[0])[0]
        min_column = min(self.copy_relative_pos_list, key=lambda x: x[1])[1]
        max_column = max(self.copy_relative_pos_list, key=lambda x: x[1])[1]
        self.cell_row_length = max_row - min_row + 1
        self.cell_column_length = max_column - min_column + 1
        #print('length :', self.cell_row_length, self.cell_column_length)
        #print('copied values :', self.copy_value_list)
        self.change_static_text()#右クリック時リフレッシュされない対策
        
    #エクセルライクペースト
    @timer
    def cell_paste(self):
        self.paste_cell_list = self.selected_items
        if not self.paste_cell_list or not self.copy_value_list:
            self.change_static_text()#右クリック時リフレッシュされない対策
            return
        
        #繰り返し可能回数を判定
        start_cell = self.paste_cell_list[0]
        start_row = start_cell.row()
        start_column = start_cell.column()
        start_pos_list = [[start_row, start_column]]#繰り返しスタートセルのリスト
        ignore_list = self.get_ignore_for_start_pos(start_row, start_column, self.cell_row_length, self.cell_column_length )
        for cell_id in self.paste_cell_list[1:]:
            row = cell_id.row()
            column = cell_id.column() 
            index = [row, column]
            if index in ignore_list:
                continue
            start_pos_list.append(index)
            ignore_list += self.get_ignore_for_start_pos(row, column, self.cell_row_length, self.cell_column_length )
        
        #開始位置を基準にペースト処理
        self.update_rows = set()
        self.row_column_dict = defaultdict(lambda : [])
        self.locked_cells = self.weight_model.weight_lock_cells#ロック情報を取得
        for [start_row, start_column] in start_pos_list:
            for value, pos in zip(self.copy_value_list,  self.copy_relative_pos_list):
                #新規行列位置
                row = start_row + pos[0]
                column = start_column + pos[1]
                #ロックされてたらスキップ
                if (row, column) in self.locked_cells:
                    continue
                try:
                #参照リストを直接書き換える
                    weight_list = self._data[row]
                    node = self.vtx_row_dict[row][6]
                    inf_id_list = self.node_influence_id_list_dict[node]
                    local_column = inf_id_list[column]#空白セルを考慮したローカルカラム取得
                    weight_list[local_column] = value
                    #焼き込みようリストを更新しておく
                    self.update_rows.add(row)
                    self.row_column_dict[row].append(column)#行に対応する選択カラムを記録する
                except:
                    pass
        #ベイク計算に入る
        self.precalculate_bake_weights()
        self.change_static_text()#右クリック時リフレッシュされない対策
        
    #ペースト開始地点から次の開始地点までの無視セル一覧を作る
    def get_ignore_for_start_pos(self, row, column, row_length, column_length):
        ignore_list = []
        for add_row, add_column in itertools.product(range(row_length), range(column_length)):
            ignore_list.append([row + add_row, column + add_column])
        return ignore_list
            
    #フリーズ
    def freeze(self):
        freeze.freeze()
    
    #フリーズM
    def freeze_m(self):
        freeze.main(pop_zero_poly=True)
    
    def weight_copy(self, method='index', engin='maya', saveName='simple.copypaste'):
        selection = cmds.ls(sl=True)
        skin_meshes = common.search_polygon_mesh(selection, serchChildeNode=True)
        if skin_meshes is not None:
            weight.WeightCopyPaste().main(skin_meshes, 
                                            mode = 'copy', 
                                            saveName = saveName, 
                                            engine = engin,
                                            viewmsg = True)
                                                
    def weight_paste(self, method='index', threshold=0.2, engin='maya', saveName='simple.copypaste'):
        selection = cmds.ls(sl=True)
        skin_meshes = common.search_polygon_mesh(selection, serchChildeNode=True)
        if skin_meshes is not None:
            weight.WeightCopyPaste().main(skin_meshes, 
                                            mode = 'paste', 
                                            saveName = saveName, 
                                            method = method, 
                                            threshold = threshold,
                                            engine = engin,
                                            viewmsg = True)
                                            
    def weight_transfer_copy(self):
        msg = state.WEIGHT_TRANSFER_MULTIPLE.stock_copy_mesh()
        print(msg)
        self.set_message(msg=msg, error=False)
        
    def weight_transfer_paste(self):
        msg = state.WEIGHT_TRANSFER_MULTIPLE.transfer_weight_multiple()
        
        cmds.evalDeferred(lambda : self.set_message(msg=msg, error=False))
        
    def open_smooth_setting_editor(self):
        smooth_setting_editor.Option()
        
    def hammer_weight(self,  keep_zero=False, blend=False):
        #パラメータ決め打ちでスムースする
        self.weight_smoothing(keep_zero=keep_zero, blend=blend)
    
    def smooth_weight(self,  keep_zero=False, blend=False):
        save_data = smooth_setting_editor.SubWindow.load_data()
        #print('smooth setting :', save_data)
        sw = save_data['weight_difference']
        swi = save_data['smoothing_iterations']
        omi = save_data['obey_max_inf']
        self.weight_smoothing(keep_zero=keep_zero, sw=sw, swi=swi, omi=omi, blend=blend)
        
    def _read_current_vertex_weights(self, node, vtx_indices):
        """OpenMaya API 2.0 経由で対象ノードの指定頂点ウェイトを高速取得する。"""
        skin_fn = self.node_skinFn_dict.get(node)
        if skin_fn is None or not vtx_indices:
            return None
        try:
            sel = om2.MSelectionList()
            sel.add(node)
            dag = sel.getDagPath(0)
            shape_dag = None
            if dag.node().hasFn(om2.MFn.kMesh):
                shape_dag = dag
            else:
                shape_count = dag.numberOfShapesDirectlyBelow()
                for idx in range(shape_count):
                    cand = om2.MDagPath(dag)
                    try:
                        cand.extendToShape(idx)
                    except Exception:
                        continue
                    if not cand.node().hasFn(om2.MFn.kMesh):
                        continue
                    if shape_dag is None:
                        shape_dag = cand
                    if not om2.MFnDagNode(cand).isIntermediateObject:
                        shape_dag = cand
                        break
            if shape_dag is None:
                return None

            single_comp = om2.MFnSingleIndexedComponent()
            vtx_comp = single_comp.create(om2.MFn.kMeshVertComponent)
            single_comp.addElements(vtx_indices)
            flat_weights, inf_count = skin_fn.getWeights(shape_dag, vtx_comp)
            if inf_count <= 0:
                return None
            result = {}
            for idx, vid in enumerate(vtx_indices):
                start = idx * inf_count
                result[vid] = [flat_weights[start + c] for c in range(inf_count)]
            return result
        except Exception:
            return None

    # ウェイトスムースおよびウェイトハンマーの実行（ロック保護・セル選択維持・Undo 1 回化対応）
    def weight_smoothing(self, keep_zero=False, sw=0, swi=5, omi=2, nrm=True, blend=False):
        """選択セル（未選択時は全表示頂点）に対しスムースを実行し、ロック保護とセル選択維持を行う。"""
        # 選択情報を整理
        self.setup_update_row_data()
        if not self.update_rows:
            return

        mesh_vert_dict = defaultdict(lambda: [])
        mesh_row_dict = defaultdict(lambda: [])
        vertex_org_weight_dict = {}
        for row in sorted(self.update_rows):
            if row not in self.vtx_row_dict:
                continue
            node = self.vtx_row_dict[row][6]
            row_vtx = self.vtx_row_dict[row][5]
            mesh_vert_dict[node].append(row_vtx)
            mesh_row_dict[node].append(row)
            vertex_org_weight_dict[row] = self._data[row][:]

        if not mesh_vert_dict:
            return

        # Maya 側のスムース計算のみを Undo 履歴外で実行し、最終結果は焼き込みプラグイン経由で 1 回にまとめる
        with qt.suspend_undo():
            for node, mesh_vertices in mesh_vert_dict.items():
                self.hilite_flag = True
                cmds.select(mesh_vertices, r=True)
                skin_cluster = self.all_skin_clusters[node]
                try:
                    mmi = None
                    if self.keep_mmi_but.isChecked():
                        mmi = cmds.getAttr(skin_cluster + '.maintainMaxInfluences')

                    cmds.skinCluster(skin_cluster, e=True, sw=sw, swi=swi, omi=omi)
                    if nrm:
                        cmds.skinCluster(skin_cluster, e=True, fnw=True)

                    if self.keep_mmi_but.isChecked() and mmi is not None:
                        cmds.setAttr(skin_cluster + '.maintainMaxInfluences', mmi)
                except Exception:
                    pass
            if self.original_selection:
                self.hilite_flag = True
                cmds.select(self.original_selection, r=True)
            else:
                self.hilite_flag = False

        # スムース直後のウェイトを OpenMaya 2.0 で取得し、テーブルモデルを再構築せずに反映する
        ratio = (self.smooth_ratio_box.value() / 100.0) if blend else 1.0
        updated_any = False
        for node, rows in mesh_row_dict.items():
            vtx_indices = [self.vtx_row_dict[r][0] for r in rows]
            smoothed_map = self._read_current_vertex_weights(node, vtx_indices)
            if not smoothed_map:
                continue
            for row in rows:
                vid = self.vtx_row_dict[row][0]
                vtx_name = self.vtx_row_dict[row][5]
                org_weights = vertex_org_weight_dict[row]
                new_weights = smoothed_map.get(vid)
                if new_weights is None or len(new_weights) != len(org_weights):
                    continue

                # ゼロ値維持オプションが有効なら元が 0.0 のインフルエンスを 0.0 に戻す
                if keep_zero:
                    for i, ow in enumerate(org_weights):
                        if ow == 0.0:
                            new_weights[i] = 0.0

                # 適用率ブレンド（右クリック実行時）
                if blend:
                    for i, (ow, nw) in enumerate(zip(org_weights, new_weights)):
                        new_weights[i] = ow * (1.0 - ratio) + nw * ratio

                # ロックされているインフルエンスのウェイトを実行前の元の値へ確実に復元する
                locked_ids = self.vtx_lock_data_dict.get(vtx_name)
                if locked_ids:
                    for locked_idx in locked_ids:
                        if 0 <= locked_idx < len(org_weights):
                            new_weights[locked_idx] = org_weights[locked_idx]

                # 参照中の行データ配列へ書き込み
                target_weights = self._data[row]
                for i, w in enumerate(new_weights):
                    target_weights[i] = w
                updated_any = True

        if not updated_any:
            self.get_set_skin_weight(node_vtx_dict=self.node_vtx_dict)
            return

        # ロック済みセルを保護しつつアンロック済みボーン間で正規化し、焼き込みプラグイン経由で一括反映する
        enforce_flag = -1 if (nrm and not self.no_limit_but.isChecked()) else 0
        self.precalculate_bake_weights(enforce=enforce_flag)

        # スライダー・スピンボックス表示もスムース後の現在値へ同期する（セル選択は維持）
        if getattr(self, 'selected_items', None):
            self.change_add_mode(self.add_mode, change_only=True)
                    
        '''
        #直接ウェイトハンマーのmel呼び出し版
        cmds.undoInfo(swf=False)
        self.hilite_flag = True
        cmds.select(target_vertices, r=True)
        cmds.undoInfo(swf=True)
        
        mel.eval('weightHammerVerts;')
        
        cmds.undoInfo(swf=False)
        self.hilite_flag = True
        cmds.select(current_selection, r=True)
        cmds.undoInfo(swf=True)
        '''

    #直接入力機能
    numeric_list = ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '-', '.']
    def direct_cell_input(self, string):
        if not self.sel_model.selectedIndexes():
            return
        if string in self.numeric_list:
            self.view_widget.ignore_key_input = True#一時的にテーブルへのキー入力無効
            self.input_box = PopInputBox(value=string, 
                                        mode=self.mode_but_group.checkedId(),
                                        direct=True)
            self.input_box.closed.connect(lambda : self.apply_input_box_value(direct=True))

    #エンフォース、ラウンド、ノーマライズなど強制焼きこみの時は事前に行データを設定する
    def setup_update_row_data(self):
        self.update_rows = set()
        if not  self.selected_items:
            self.update_rows = self.all_editable_rows
        else:
            for cell_id in self.selected_items:
                row = cell_id.row()
                self.update_rows.add(row)
        self.row_column_dict = {}
        #行に対するカラムの選択はないことにする
        for row in self.update_rows:
            self.row_column_dict[row] = []
            
    #エンフォースリミットと強制ノーマライズ実行
    def enforce_limit_and_normalize(self, force_norm=False):
        if not force_norm:#エンフォース
            limit = self.limit_box.value()
            if limit == 0:
                return
        else:#強制正規化
            limit = -1
        self.setup_update_row_data()#焼きこみ対象の行を設定する
        self.precalculate_bake_weights(enforce=limit)
        
    #してい桁数で丸める
    def round_off(self):
        id = self.max_value_but_group.checkedId()
        offset =  id * 2
        round_digit = self.round_box.value()+offset
        self.setup_update_row_data()#焼きこみ対象の行を設定する
        self.precalculate_bake_weights(round_digit=round_digit)
        
    def keep_no_limit_flag(self):
        self.no_limit = self.no_limit_but.isChecked()
        
    def toggle_no_limit_but_enable(self):
        if self.norm_but.isChecked():
            self.no_limit_but.setDisabled(True)#無効化
            self.no_limit_but.setChecked(False)
            qt.change_button_color(self.no_limit_but, textColor=128, bgColor=68, hiColor=68, hiText=128, hiBg=64, dsColor=160,
                                        mode='button', toggle=False, hover=False, destroy=True, dsWidth=1)
        else:
            self.no_limit_but.setDisabled(False)
            self.no_limit_but.setChecked(self.no_limit)
            qt.change_button_color(self.no_limit_but, textColor=200, bgColor=68, hiColor=120, hiText=200, hiBg=120, dsColor=160,
                                        mode='button', toggle=True, hover=False, destroy=True, dsWidth=1)
            
    #小数点以下の最大表示桁数を100と1の時で切り替える
    def change_max_decimal_digit(self, id):
        if self.pre_max_id == id:
            return
        self.pre_max_id = id
        offset = (1 - id) * 2
        value = self.digit_box.value()
        if id == 0:
            self.digit_box.setRange(offset , 9)
            self.digit_box.setValue(value + 2 )
            self.round_box.setRange(offset , 9)
            self.round_box.setValue(value + 2 )
        else:
            self.digit_box.setRange(offset , 7)
            self.digit_box.setValue(value- 2 )
            self.round_box.setRange(offset , 7)
            self.round_box.setValue(value- 2 )
            
    # 表示桁数を変える
    def change_decimal_digits(self):
        state.FLOAT_DECIMALS = self.digit_box.value()
        state.set_float_decimals(state.FLOAT_DECIMALS)
        
        self.weight_input.setDecimals(state.FLOAT_DECIMALS)
        max_slider_val = int(round((10 ** state.FLOAT_DECIMALS) * state.MAXIMUM_WEIGHT))
        if self.add_mode == 0:
            self.weight_input.setRange(0, state.MAXIMUM_WEIGHT)
            self.weight_input_sld.setRange(0, max_slider_val)
        else:
            self.weight_input.setRange(-1 * state.MAXIMUM_WEIGHT, state.MAXIMUM_WEIGHT)
            self.weight_input_sld.setRange(-1 * max_slider_val, max_slider_val)
        
        # テーブルビューでゼロ埋めするためのフォーマットを作っておく
        state.FLOAT_FORMAT = '{:.'+str(state.FLOAT_DECIMALS)+'f}'
        try:
            set_table_header_width(self.view_widget, self.weight_model, add=(state.FLOAT_DECIMALS-2)*7-10)
            self.refresh_table_view()
            if self.add_mode == 0 and getattr(self, 'selected_items', None):
                self.change_add_mode(0, change_only=True)
        except:
            pass
        
    def change_maximum_weight(self, id):
        if id == 0:
            state.MAXIMUM_WEIGHT = 1.0
            self.max_wt = 1.0
        else:
            state.MAXIMUM_WEIGHT = 100.0
            self.max_wt = 100.0
        state.set_maximum_weight(state.MAXIMUM_WEIGHT)
        max_slider_val = int(round((10 ** state.FLOAT_DECIMALS) * state.MAXIMUM_WEIGHT))
        if self.add_mode == 0:
            self.weight_input.setRange(0, state.MAXIMUM_WEIGHT)
            self.weight_input_sld.setRange(0, max_slider_val)
        else:
            self.weight_input.setRange(-1 * state.MAXIMUM_WEIGHT, state.MAXIMUM_WEIGHT)
            self.weight_input_sld.setRange(-1 * max_slider_val, max_slider_val)
        self.refresh_table_view()
        self.change_static_text()
        if self.add_mode == 0 and getattr(self, 'selected_items', None):
            self.change_add_mode(0, change_only=True)
        self.update_bone_visualizer()
        self.update_bone_remap_window()
        
    @timer
    def change_limit(self, e):
        state.MAXIMUM_INFLUENCE_COUNT = self.limit_box.value()
        state.set_maximum_influence_count(state.MAXIMUM_INFLUENCE_COUNT)
        self.refresh_table_view()
        
    #リミットゼロの時は入力されてないように見せる
    def change_limit_box_color(self):
        if self.limit_box.value() == 0:
            qt.change_widget_color(self.limit_box, textColor=42, hitxColor=42, hibgColor=42)
        else:
            qt.change_widget_color(self.limit_box, textColor=200, hitxColor=255)

    @Slot(int)#シグナルから整数を引数として受け取る
    def toggle_lock_weight(self,section_id):
        for lock_data in self.weight_lock_data:
            if lock_data[1] == section_id:
                #print('unlock wt')
                self.unlock_weight(all_lock=True, section_ids=[section_id])
                break
        else:
            #print('lock wt')
            self.lock_weight(all_lock=True, section_ids=[section_id])
        self.sel_model.clearSelection()
            
    def lock_all_vtx_weight(self, section_ids=[], add=True):
        with qt.undo_chunk('SIWE_LockAllVtxWeight'):
            for node in self.hl_nodes:
                skin_cluster = self.all_skin_clusters[node]
                vertices = self.node_vtx_dict[node]
                lock_influences = [self.all_influences[section_id] for section_id in section_ids]
                lock_influences = [inf for inf in lock_influences if inf in self.influences_dict[node]]
                pre_lock_data_dict = self.decode_lock_data(skin_cluster)
                for vtx in vertices:
                    try:
                        pre_lock_influences = pre_lock_data_dict[vtx]
                    except Exception:
                        pre_lock_influences = []
                    if add:
                        after_lock_influences = list(set(pre_lock_influences + lock_influences))
                    else:
                        after_lock_influences = list(set(pre_lock_influences) - set(lock_influences))
                    pre_lock_data_dict[vtx] = after_lock_influences
                new_lock_data_list = self.encode_lock_data(pre_lock_data_dict)
                if not new_lock_data_list:
                    if cmds.attributeQuery(self.lock_attr_name, node=skin_cluster, exists=True):
                        try:
                            cmds.deleteAttr(skin_cluster + '.' + self.lock_attr_name)
                        except Exception as e:
                            print('clear lock error :', skin_cluster, '{}'.format(e))
                else:
                    self._ensure_lock_attr(skin_cluster)
                    cmds.setAttr(
                        skin_cluster + '.' + self.lock_attr_name,
                        type='stringArray',
                        *([len(new_lock_data_list)] + new_lock_data_list)
                    )
            
    def _is_column_selected(self, index, selected_item):
        """Maya 2022 以降の PySide2/PySide6 と旧バージョン双方で列選択状態を判定する。"""
        if MAYA_VER >= 2022:
            return self.sel_model.columnIntersectsSelection(index)
        return self.sel_model.columnIntersectsSelection(index, selected_item)

    def lock_all_rows(self, lock=True):
        column_count = self.weight_model.columnCount()
        selected_item = self.sel_model.currentIndex()
        new_indexes = []
        section_ids = []
        for index in range(column_count):
            if self._is_column_selected(index, selected_item):
                section_ids.append(index)
                column_indexes = [[row, index] for row in self.all_editable_rows]
                new_indexes += column_indexes
        if not section_ids:
            return
        if lock:
            self.lock_weight(all_lock=True, section_ids=section_ids, indexes=new_indexes)
        else:
            self.unlock_weight(all_lock=True, section_ids=section_ids, indexes=new_indexes)
        self.refresh_table_view()
        
    def unlock_all_rows(self):
        column_count = self.weight_model.columnCount()
        selected_item = self.sel_model.currentIndex()
        new_indexes = []
        section_ids = []
        for index in range(column_count):
            if self._is_column_selected(index, selected_item):
                section_ids.append(index)
                column_indexes = [[row, index] for row in self.all_editable_rows]
                new_indexes += column_indexes
        if not section_ids:
            return
        self.unlock_weight(all_lock=False, section_ids=section_ids, indexes=new_indexes)
        self.refresh_table_view()
        
    #ウェイトロック
    #@prof.profileFunction()
    @timer
    def lock_weight(self, all_lock=False, section_ids=None, indexes=None):
        self.counter.reset()
        
        skin_vtx_dict = defaultdict(lambda : [])
        vtx_lock_dict = defaultdict(lambda : [])
        if not indexes:
            target_cells = self.selected_items
        else:
            target_cells = indexes
        for cell_id in target_cells:
            #セルの色替え
            if not indexes:
                row = cell_id.row()
                column = cell_id.column()
            else:
                row = cell_id[0]
                column = cell_id[1]
            index = (row, column)
            self.weight_lock_data.add(index)
            self.weight_model.weight_lock_cells.add(index)
            
            #事前に格納しておいた行のデータを引き出す
            row_data = self.vtx_row_dict[row]
            vtx = row_data[0]
            skin_cluster = row_data[1]
            influences =  row_data[2]
            vtx_name = row_data[5]
            node = row_data[6]
            node_influence_id_list = self.node_influence_id_list_dict[node]
            rock_id = node_influence_id_list[column]#ロックするインフルエンスのID
            try:
                rock_influence = influences[rock_id]#ロックするインフルエンス
            except:
                continue
            self.vtx_lock_data_dict[vtx_name].add(rock_id)#ロックインフルエンス辞書に追加
            
            #まとめて処理するためにいろいろ情報を整理して格納
            skin_vtx_dict[skin_cluster] += [vtx]
            vtx_lock_dict[skin_cluster+str(vtx)] += [rock_influence]
            
        self.counter.count(string='lock cell :')
        
        if all_lock:
            self.lock_all_vtx_weight(section_ids=section_ids, add=True)
        else:
            # ロック情報を文字配列としてスキンクラスタのカスタムアトリビュートに単一チャンクで格納する
            with qt.undo_chunk('SIWE_LockWeight'):
                for skin_cluster, vertices in skin_vtx_dict.items():
                    pre_lock_data_dict = self.decode_lock_data(skin_cluster)
                    for vtx in vertices:
                        lock_influences = vtx_lock_dict[skin_cluster + str(vtx)]
                        try:
                            pre_lock_influences = pre_lock_data_dict[vtx]
                            lock_influences = list(set(lock_influences + pre_lock_influences))
                        except Exception:
                            pass
                        pre_lock_data_dict[vtx] = lock_influences
                    new_lock_data_list = self.encode_lock_data(pre_lock_data_dict)
                    self._ensure_lock_attr(skin_cluster)
                    cmds.setAttr(
                        skin_cluster + '.' + self.lock_attr_name,
                        type='stringArray',
                        *([len(new_lock_data_list)] + new_lock_data_list)
                    )
                
        self.counter.count(string='set lock attr to skin_node :')
        self.counter.lap_print(print_flag=state.COUNTER_PRINT)
            
    #ウェイトアンロック
    #@prof.profileFunction()
    @timer
    def unlock_weight(self, all_lock=False, section_ids=None, indexes=None):
        skin_vtx_dict = defaultdict(lambda : [])
        vtx_lock_dict = defaultdict(lambda : [])
        if not indexes:
            target_cells = self.selected_items
        else:
            target_cells = indexes
        for cell_id in target_cells:
            #セルの色替え
            if not indexes:
                row = cell_id.row()
                column = cell_id.column()
            else:
                row = cell_id[0]
                column = cell_id[1]
            index = (row, column)
            try:
                self.weight_lock_data.remove(index)
            except:
                pass
            try:
                self.weight_model.weight_lock_cells.remove(index)
            except:
                pass
            #事前に格納しておいた行のデータを引き出す
            row_data = self.vtx_row_dict[row]
            vtx = row_data[0]
            skin_cluster = row_data[1]
            influences =  row_data[2]
            vtx_name = row_data[5]
            node = row_data[6]
            node_influence_id_list = self.node_influence_id_list_dict[node]
            rock_id = node_influence_id_list[column]#ロックするインフルエンスのID
            try:
                rock_influence = influences[rock_id]#ロックするインフルエンス
            except:
                continue
            node_influence_id_list = self.node_influence_id_list_dict[node]
            if rock_id in self.vtx_lock_data_dict[vtx_name]:
                self.vtx_lock_data_dict[vtx_name].remove(rock_id)#ロックインフルエンス辞書から削除
            rock_influence = influences[rock_id]#ロックするインフルエンス
            #まとめて処理するためにいろいろ情報を整理して格納
            skin_vtx_dict[skin_cluster] += [vtx]
            vtx_lock_dict[skin_cluster+str(vtx)] += [rock_influence]
        if all_lock:
            self.lock_all_vtx_weight(section_ids=section_ids, add=False)
        else:
            # ロック情報を文字配列としてスキンクラスタのカスタムアトリビュートに単一チャンクで更新する
            with qt.undo_chunk('SIWE_UnlockWeight'):
                for skin_cluster, vertices in skin_vtx_dict.items():
                    pre_lock_data_dict = self.decode_lock_data(skin_cluster)
                    for vtx in vertices:
                        lock_influences = vtx_lock_dict[skin_cluster + str(vtx)]
                        try:
                            pre_lock_influences = pre_lock_data_dict[vtx]
                            lock_influences = list(set(pre_lock_influences) - set(lock_influences))
                        except Exception:
                            pass
                        pre_lock_data_dict[vtx] = lock_influences
                    new_lock_data_list = self.encode_lock_data(pre_lock_data_dict)
                    if not new_lock_data_list:
                        if cmds.attributeQuery(self.lock_attr_name, node=skin_cluster, exists=True):
                            try:
                                cmds.deleteAttr(skin_cluster + '.' + self.lock_attr_name)
                            except Exception as e:
                                print('clear lock error :', skin_cluster, '{}'.format(e))
                    else:
                        self._ensure_lock_attr(skin_cluster)
                        cmds.setAttr(
                            skin_cluster + '.' + self.lock_attr_name,
                            type='stringArray',
                            *([len(new_lock_data_list)] + new_lock_data_list)
                        )

    # アトリビュートに格納する形式にロックデータをエンコードする
    def encode_lock_data(self, lock_dict):
        return [str(v) + self.spliter + ','.join(influences) for v, influences in lock_dict.items() if len(influences) != 0]

    def _ensure_lock_attr(self, skin_cluster):
        """書き込み時のみスキンクラスタへロック用カスタムアトリビュートを追加する。"""
        if not cmds.attributeQuery(self.lock_attr_name, node=skin_cluster, exists=True):
            cmds.addAttr(skin_cluster, longName=self.lock_attr_name, dataType='stringArray')

    # アトリビュートのロック情報を読み取り専用で翻訳する（選択だけで Undo 履歴を汚さない）
    lock_attr_name = 'SI_Weight_Lock_Data'
    spliter = '-|-'  # アトリビュートに格納するときのインフルエンスつなぎ文字
    def decode_lock_data(self, skin_cluster):
        try:
            lock_data_dict = {}
            if not cmds.attributeQuery(self.lock_attr_name, node=skin_cluster, exists=True):
                return lock_data_dict
            lock_list = cmds.getAttr(skin_cluster + '.' + self.lock_attr_name) or []
            for lock_data in lock_list:
                split_lock_data = lock_data.split(self.spliter)
                if len(split_lock_data) < 2:
                    continue
                lock_inf_str = split_lock_data[1]
                if lock_inf_str:
                    lock_data_dict[int(split_lock_data[0])] = lock_inf_str.split(',')
            return lock_data_dict
        except Exception as e:
            print('decode locke data error :', '{}'.format(e))
            return {}

    # ロック状態のクリア
    def clear_lock_weight(self):
        self.vtx_lock_data_dict = defaultdict(lambda: set())
        self.weight_model.weight_lock_cells = set()
        self.weight_lock_data = set()
        self.refresh_table_view()
        with qt.undo_chunk('SIWE_ClearLockWeight'):
            for skin_cluster in self.all_skin_clusters.values():
                if cmds.attributeQuery(self.lock_attr_name, node=skin_cluster, exists=True):
                    try:
                        cmds.deleteAttr(skin_cluster + '.' + self.lock_attr_name)
                    except Exception as e:
                        print('clear lock error :', skin_cluster, '{}'.format(e))
            
    def copy_bone_names_to_clipboard(self, joint_paths, full_path=False):
        """指定されたボーン名（またはフルパス）をクリップボードへコピーして通知する。"""
        names = []
        seen = set()
        for jp in (joint_paths or []):
            if not jp:
                continue
            val = str(jp) if full_path else str(jp).split('|')[-1]
            if val and val not in seen:
                seen.add(val)
                names.append(val)
        if not names:
            return
        text = '\n'.join(names)
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        disp = names[0] if len(names) == 1 else '{0} (+{1})'.format(names[0], len(names) - 1)
        msg = lang.Lang(
            en='- Copied Bone Name : {0} -',
            ja=u'- ボーン名をコピーしました : {0} -',
        ).output().format(disp)
        if hasattr(self, 'set_message'):
            self.set_message(msg=msg, error=False)

    # コピペ・ボーン名コピー右クリックメニュー（Shift / Ctrl + 右クリック）
    def pop_copy_menu(self, column=None):
        copy_menus = QMenu(self)
        action = copy_menus.addAction(
            lang.Lang(en='Copy Vertex Weights', ja=u'頂点ウェイトをコピー').output()
        )
        action.triggered.connect(self.vertex_copy)
        action = copy_menus.addAction(
            lang.Lang(en='Paste Vertex Weights', ja=u'頂点ウェイトをペースト').output()
        )
        action.triggered.connect(self.vertex_paste)
        copy_menus.addSeparator()
        action = copy_menus.addAction(
            lang.Lang(en='Copy Cell Values', ja=u'セルの数値をコピー').output()
        )
        action.triggered.connect(self.cell_copy)
        action = copy_menus.addAction(
            lang.Lang(en='Paste Cell Values', ja=u'セルの数値をペースト').output()
        )
        action.triggered.connect(self.cell_paste)

        all_infs = getattr(self, 'all_influences', None) or []
        clicked_inf = None
        if column is not None and 0 <= column < len(all_infs):
            clicked_inf = all_infs[column]

        sel_infs = []
        seen_cols = set()
        for item in (getattr(self, 'selected_items', None) or []):
            col = item.column()
            if col not in seen_cols and 0 <= col < len(all_infs):
                seen_cols.add(col)
                sel_infs.append(all_infs[col])

        if clicked_inf or sel_infs:
            copy_menus.addSeparator()
            primary_inf = clicked_inf or sel_infs[0]
            primary_short = primary_inf.split('|')[-1]
            copy_one_act = copy_menus.addAction(
                lang.Lang(
                    en='Copy Bone Name ({0})',
                    ja=u'ボーン名をコピー ({0})',
                ).output().format(primary_short)
            )
            copy_one_act.triggered.connect(
                lambda _=False, jp=primary_inf: self.copy_bone_names_to_clipboard([jp], full_path=False)
            )

            if len(sel_infs) > 1:
                copy_all_act = copy_menus.addAction(
                    lang.Lang(
                        en='Copy All Selected Bone Names ({0})',
                        ja=u'選択中のボーン名をすべてコピー ({0}本)',
                    ).output().format(len(sel_infs))
                )
                copy_all_act.triggered.connect(
                    lambda _=False, jps=list(sel_infs): self.copy_bone_names_to_clipboard(jps, full_path=False)
                )

            path_targets = sel_infs if len(sel_infs) > 1 else [primary_inf]
            copy_path_act = copy_menus.addAction(
                lang.Lang(
                    en='Copy Bone Full Path',
                    ja=u'ボーンのフルパスをコピー',
                ).output()
            )
            copy_path_act.triggered.connect(
                lambda _=False, jps=list(path_targets): self.copy_bone_names_to_clipboard(jps, full_path=True)
            )

        cursor = QCursor.pos()
        exec_func = getattr(copy_menus, 'exec_', None) or getattr(copy_menus, 'exec')
        exec_func(cursor)
            
    # 右クリックしたときの小窓を反映する。
    def get_clicke_item_value(self):
        shift_mod = qt.check_key_modifiers(Qt.ShiftModifier)
        ctrl_mod = qt.check_key_modifiers(Qt.ControlModifier)
        # ヘッダーの幅を取得してカラム位置をオフセットする
        v_header = self.view_widget.verticalHeader()
        v_header_width = v_header.sizeHint().width()
        h_header = self.view_widget.horizontalHeader()
        h_header_height = h_header.sizeHint().height()
        
        pos = self.view_widget.mapFromGlobal(QCursor.pos())
        pos_x = pos.x() - v_header_width - 2  # 誤差補正
        pos_y = pos.y() - h_header_height - 2  # 誤差補正
        row = self.view_widget.rowAt(pos_y)
        column = self.view_widget.columnAt(pos_x)
        if column == -1:
            column = len(self.all_influences)
        text = self.weight_model.get_data(row=row, column=column)
        if text is None or shift_mod or ctrl_mod:
            self.pop_copy_menu(column=column)
            return
        else:
            text = text*state.MAXIMUM_WEIGHT
        value = float(text)
        self.input_box = PopInputBox(value=value, mode=self.mode_but_group.checkedId())
        self.input_box.closed.connect(self.apply_input_box_value)
    
    #右クリック入力を確定して反映する
    def apply_input_box_value(self, direct=True):
        if direct:
            try:
                self.input_box_value = float(self.input_box.input.text())
            except:
                return
        else:
            self.input_box_value = self.input_box.input.value()
        self.calc_cell_value(from_spinbox=False, from_input_box=True)
        
        #フォーカスを取る
        self.activateWindow()
        self.raise_()
        #Pyside1だとなぜか繰り返さないとフォーカス取れない
        self.view_widget.setFocus()
        self.weight_input.clearFocus()
        self.view_widget.setFocus()
        #キー入力受付をもとに戻す
        self.view_widget.ignore_key_input = False
        
    #モディファイヤによって固定値入力ボタンのテキスト表示を変える
    static_input_list = [0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0]
    static_input_list_int = [0, 10, 25, 50, 75, 90, 100]
    def change_static_text(self, reserve_mod=False):
        if reserve_mod:
            shift_mod = self.shift_isPressed
            ctrl_mod = self.ctrl_isPressed
        else:
            shift_mod = qt.check_key_modifiers(Qt.ShiftModifier)
            ctrl_mod = qt.check_key_modifiers(Qt.ControlModifier)
        
        if state.MAXIMUM_WEIGHT == 100:
            static_input_list  = self.static_input_list_int
        else:
            static_input_list  = self.static_input_list
        
        if shift_mod:
            static_input_list = map(lambda  x : '+' + str(x), static_input_list)
        elif ctrl_mod:
            static_input_list = map(lambda  x : '-' + str(x), static_input_list)
        else:
            static_input_list = map(lambda  x :  str(x), static_input_list)
            
        for but, text in zip(self.static_input_buttons, static_input_list):
            but.setText(text)
    
    # 固定値を入力する
    def apply_static_value(self, value):
        pre_add_mode = self.add_mode
        pre_maximum = state.MAXIMUM_WEIGHT
        state.MAXIMUM_WEIGHT = 1.0
        state.set_maximum_weight(state.MAXIMUM_WEIGHT)
        try:
            shift_mod = qt.check_key_modifiers(Qt.ShiftModifier)
            ctrl_mod = qt.check_key_modifiers(Qt.ControlModifier)

            if shift_mod or ctrl_mod:
                self.add_mode = 1
            else:
                self.add_mode = 0

            if ctrl_mod:
                value *= -1

            self.input_box_value = value
            self.calc_cell_value(from_spinbox=False, from_input_box=True)
        finally:
            self.add_mode = pre_add_mode
            state.MAXIMUM_WEIGHT = pre_maximum
            state.set_maximum_weight(state.MAXIMUM_WEIGHT)
        if self.add_mode == 0 and getattr(self, 'selected_items', None):
            self.change_add_mode(0, change_only=True)
        else:
            self._set_slider_and_spinbox_silent(0.0)

    # 加算モードが変更されたらスライダー反映式を変える
    add_mode = 0
    def change_from_sld(self):
        div_num = 10.0 ** state.FLOAT_DECIMALS
        self.weight_input.blockSignals(True)
        try:
            self.weight_input.setValue(self.weight_input_sld.value() / div_num)
        finally:
            self.weight_input.blockSignals(False)

    def change_from_spinbox(self):
        mul_num = 10.0 ** state.FLOAT_DECIMALS
        self.weight_input_sld.blockSignals(True)
        try:
            self.weight_input_sld.setValue(int(round(self.weight_input.value() * mul_num)))
        finally:
            self.weight_input_sld.blockSignals(False)

    def _set_slider_and_spinbox_silent(self, display_value):
        """シグナルを発火させずに数値ボックスとスライダーの表示値を同期する。"""
        scale = 10 ** state.FLOAT_DECIMALS
        rounded_val = round(display_value, state.FLOAT_DECIMALS)
        self.weight_input.blockSignals(True)
        self.weight_input_sld.blockSignals(True)
        try:
            self.weight_input.setValue(rounded_val)
            self.weight_input_sld.setValue(int(round(rounded_val * scale)))
        finally:
            self.weight_input.blockSignals(False)
            self.weight_input_sld.blockSignals(False)

    def _calc_selected_cells_stat_value(self):
        """選択中セルの有効値から平均または中央値を算出する。"""
        values = []
        for item in self.selected_items:
            sel_value = self.weight_model.get_data(item)
            if sel_value is not None:
                values.append(float(sel_value))
        if not values:
            return None

        if getattr(self, 'slider_stat_mode', 'avg') == 'median':
            values.sort()
            mid = len(values) // 2
            if len(values) % 2 == 1:
                return values[mid]
            return (values[mid - 1] + values[mid]) * 0.5
        return sum(values) / float(len(values))

    def _set_slider_stat_mode(self, mode):
        """複数セル選択時のスライダー基準値モード（平均 / 中央値）を切り替える。"""
        self.slider_stat_mode = mode
        if self.add_mode == 0:
            self.change_add_mode(0, change_only=True)

    def pop_slider_stat_menu(self, *args):
        """複数セル選択時のスライダー基準値（平均 / 中央値）を切り替える右クリックメニュー。"""
        menu = QMenu(self)
        mode_group = QActionGroup(menu)
        mode_group.setExclusive(True)
        current_mode = getattr(self, 'slider_stat_mode', 'avg')

        avg_act = menu.addAction(
            lang.Lang(en='Multi-Cell Slider Value: Average', ja=u'複数セル選択時のスライダー値：平均').output()
        )
        avg_act.setCheckable(True)
        avg_act.setChecked(current_mode != 'median')
        avg_act.triggered.connect(lambda: self._set_slider_stat_mode('avg'))
        mode_group.addAction(avg_act)

        med_act = menu.addAction(
            lang.Lang(en='Multi-Cell Slider Value: Median', ja=u'複数セル選択時のスライダー値：中央値').output()
        )
        med_act.setCheckable(True)
        med_act.setChecked(current_mode == 'median')
        med_act.triggered.connect(lambda: self._set_slider_stat_mode('median'))
        mode_group.addAction(med_act)

        exec_func = getattr(menu, 'exec_', None) or getattr(menu, 'exec')
        exec_func(QCursor.pos())
    
    #加算モード変更時、最大最小と小数点以下桁数を変更する
    #@timer
    def change_add_mode(self, id, change_only=False):
        #モード変更があった場合はレンジを変更する
        if id != self.add_mode:
            max_slider_val = int(round((10 ** state.FLOAT_DECIMALS) * state.MAXIMUM_WEIGHT))
            if id == 0:
                self.weight_input.setRange(0, state.MAXIMUM_WEIGHT)
                self.weight_input.setDecimals(state.FLOAT_DECIMALS)
                self.weight_input_sld.setRange(0, max_slider_val)
            if id == 2 or id == 1:
                self.weight_input.setRange(-1 * state.MAXIMUM_WEIGHT, state.MAXIMUM_WEIGHT)
                self.weight_input.setDecimals(3)
                self.weight_input_sld.setRange(-1 * max_slider_val, max_slider_val)
                self._set_slider_and_spinbox_silent(0.0)
        
        self.add_mode = id
        if id == 0:
            if not self.selected_items:
                self.pre_add_value = 0.0
                self._set_slider_and_spinbox_silent(0.0)
                return
            stat_value = self._calc_selected_cells_stat_value()
            if stat_value is not None:
                self.pre_add_value = 0.0
                self._set_slider_and_spinbox_silent(stat_value * state.MAXIMUM_WEIGHT)
                return
        self.pre_add_value = 0.0
        self._set_slider_and_spinbox_silent(0.0)

    # スピンボックスがフォーカス持ってからキーが押されたかどうかを格納しておく
    key_pressed = False
    _applying_spinbox = False
    def store_keypress(self, pressed):
        self.key_pressed = bool(pressed)
        self.editing_count = 0
        self.re_forcus_flag = False

    pre_new_value = 0.0
    selected_items = []
    select_change_flag = True
    from_spinbox = False
    # 入力値をモードに合わせてセルの値と合算、セルに値を戻す
    caluc_times = 0
    editing_count = 0
    @timer
    def calc_cell_value(self, from_spinbox=False, from_input_box=False, from_slider=False):
        self.from_spinbox = from_spinbox
        wheel_flag = bool(getattr(self.weight_input, 'wheel_flag', False))

        if self.closed_flag:
            return

        # ドラッグ以外のスライダー単発操作（バークリック・矢印キー・ホイール）を検出する
        one_shot_slider = False
        if from_slider and not self.change_flag:
            consume_fn = getattr(self.weight_input_sld, 'consume_one_shot', None)
            if callable(consume_fn):
                one_shot_slider = bool(consume_fn())
            if not one_shot_slider and not wheel_flag:
                self.from_spinbox = False
                return

        if from_spinbox:
            self.from_spinbox = False
            if getattr(self, '_applying_spinbox', False):
                return
            if not self.weight_input.hasFocus() and not wheel_flag and not self.key_pressed:
                return

        if not self.selected_items:
            self.from_spinbox = False
            return

        if from_spinbox:
            self._applying_spinbox = True
            self.key_pressed = False

        try:
            # ウィンドウが非アクティブの場合はアクティブにする（Qt6 互換の activateWindow を使用）
            if not self.isActiveWindow():
                self.activateWindow()

            self.counter.reset()
            self.text_value_list = []
            self.locked_cells = self.weight_model.weight_lock_cells

            if state.MAXIMUM_WEIGHT == 100.0 or self.add_mode == 2:
                if not from_input_box:
                    new_value = self.weight_input.value() / 100.0
                else:
                    new_value = self.input_box_value / 100.0
            else:
                if not from_input_box:
                    new_value = self.weight_input.value()
                else:
                    new_value = self.input_box_value

            self.caluc_times += 1
            self.update_rows = set()
            self.row_column_dict = defaultdict(lambda: [])

            is_slider_drag = bool(self.change_flag and from_slider)
            press_start = getattr(self, 'press_start_data', None) if is_slider_drag else None

            if self.add_mode == 0:  # Abs
                new_value = max(0.0, min(new_value, 1.0))
                for cell_id in self.selected_items:
                    row = cell_id.row()
                    column = cell_id.column()
                    if (row, column) in self.locked_cells or row not in self.vtx_row_dict:
                        continue
                    node = self.vtx_row_dict[row][6]
                    inf_id_list = self.node_influence_id_list_dict.get(node)
                    if not inf_id_list or column >= len(inf_id_list):
                        continue
                    local_column = inf_id_list[column]
                    if local_column is None:
                        continue
                    weight_list = self._data[row]
                    weight_list[local_column] = new_value
                    self.update_rows.add(row)
                    self.row_column_dict[row].append(column)
                after_value = new_value
            else:
                # 単発の加算入力で 0 なら変化なしのためスキップする（ドラッグ中は 0 へ戻せるよう計算を通す）
                if not is_slider_drag and new_value == 0.0:
                    return
                for cell_id in self.selected_items:
                    row = cell_id.row()
                    column = cell_id.column()
                    if (row, column) in self.locked_cells or row not in self.vtx_row_dict:
                        continue
                    node = self.vtx_row_dict[row][6]
                    inf_id_list = self.node_influence_id_list_dict.get(node)
                    if not inf_id_list or column >= len(inf_id_list):
                        continue
                    local_column = inf_id_list[column]
                    if local_column is None:
                        continue

                    if press_start is not None and row < len(press_start) and local_column < len(press_start[row]):
                        base_value = press_start[row][local_column]
                    else:
                        base_value = self.weight_model.get_data(row=row, column=column)
                    if base_value is None:
                        continue

                    self.update_rows.add(row)
                    self.row_column_dict[row].append(column)

                    if self.add_mode == 1:  # Add
                        added_value = base_value + new_value
                    elif self.add_mode == 2:  # Add%
                        added_value = base_value * (1.0 + new_value)
                    else:
                        added_value = base_value

                    if not self.no_limit_but.isChecked():
                        if added_value > 1.0:
                            added_value = 1.0
                    if added_value < 0.0:
                        added_value = 0.0

                    weight_list = self._data[row]
                    weight_list[local_column] = added_value

                # ドラッグ中のみ現在オフセットを表示維持し、単発入力時は 0 へ戻す
                if is_slider_drag:
                    self.pre_new_value = new_value
                    after_value = new_value
                else:
                    after_value = 0.0
                    self.pre_new_value = 0.0

            self.counter.count(string='Cell Value Calculation :')

            if not self.update_rows:
                return

            # スライダードラッグ中は Undo キューを汚さない軽量プレビューとして焼き込む
            self.precalculate_bake_weights(realbake=True, ignoreundo=is_slider_drag)

            if is_slider_drag:
                self._slider_drag_modified = True
                self._slider_last_update_rows.update(self.update_rows)

            # シグナル再入を防ぎつつ数値ボックスとスライダーの表示を同期する
            if not is_slider_drag:
                self._set_slider_and_spinbox_silent(after_value * state.MAXIMUM_WEIGHT)

            self.counter.count(string='Refresh UI :')
            self.counter.lap_print(print_flag=state.COUNTER_PRINT, window=self)
        finally:
            if from_spinbox:
                self._applying_spinbox = False

    # 変更されたウェイトを事前計算して焼き込む
    @timer
    def precalculate_bake_weights(
        self,
        realbake=True,
        ignoreundo=False,
        enforce=0,
        round_digit=None,
        move_weight=False,
        skip_normalize=False,
        org_override_dict=None,
    ):
        # enforce=-1 は強制正規化フラグ扱い
        if round_digit:
            norm_flag = False
        else:
            norm_flag = self.norm_but.isChecked()
        new_data = self.weight_model._data
        if MAYA_VER >= 2016:
            self.bake_node_id_dict = defaultdict(lambda: om2.MIntArray())
            self.bake_node_weight_dict = defaultdict(lambda: om2.MDoubleArray())
            self.bake_node_inf_dict = defaultdict(lambda: om2.MIntArray())
            self.org_node_weight_dict = defaultdict(lambda: om2.MDoubleArray())
        else:
            self.bake_node_id_dict = defaultdict(lambda: om.MIntArray())
            self.bake_node_weight_dict = defaultdict(lambda: om.MDoubleArray())
            self.bake_node_inf_dict = defaultdict(lambda: om.MIntArray())
            self.org_node_weight_dict = defaultdict(lambda: om.MDoubleArray())
        self.undo_node_weight_dict = defaultdict(lambda: [])
        self.redo_node_weight_dict = defaultdict(lambda: [])
        node_weight_list = []
        org_node_weight_list = []
        pre_node = None
        pre_row_inf_id_list = None
        self.update_rows = sorted(list(self.update_rows))
        if not self.update_rows:
            return
        for row in self.update_rows:
            force_norm_flag = False
            bake_weight_flag = True
            row_data = self.vtx_row_dict[row]
            vtx = row_data[0]
            influences = row_data[2]
            all_influences_count = len(influences)
            row_inf_id_list = row_data[4]
            vtx_name = row_data[5]
            node = row_data[6]
            node_influence_id_list = self.node_influence_id_list_dict[node]

            # アンドゥと不正計算防止のために元の値を取得（スライダー確定時は開始前スナップショットを優先）
            if org_override_dict is not None and (node, vtx) in org_override_dict:
                org_weight = list(org_override_dict[(node, vtx)])
            else:
                org_weight = list(self.node_weight_dict[node][vtx])

            new_weight = list(new_data[row])

            # ロックされたセル情報からロックインフルエンスのIDリストを作る
            locked_inf_id_list = self.vtx_lock_data_dict[vtx_name]

            # ウェイト移動を実行
            if move_weight:
                copy_id_set = self.node_copy_id_dict[node]
                locked_inf_id_set = set(locked_inf_id_list)
                move_sum = 0.0
                copy_column_id_set = copy_id_set - locked_inf_id_set
                for id in copy_column_id_set:
                    move_sum += new_weight[id]
                    new_weight[id] = 0.0
                node_influence_id_dict = self.node_influence_id_dict_dict[node]
                selected_inf_ids = [node_influence_id_dict[self.all_influences[column]] for column in self.row_column_dict[row]]
                paste_column_id_set = set(selected_inf_ids) - locked_inf_id_set
                paste_count = len(paste_column_id_set)
                if paste_count:
                    paste_value = move_sum / paste_count
                    for id in paste_column_id_set:
                        new_weight[id] += paste_value

            # エンフォースリミットを実行
            if enforce >= 1:
                lock_sign_count = 0
                if locked_inf_id_list:
                    org_weight_list = new_weight[:]
                    for i in locked_inf_id_list:
                        if new_weight[i]:
                            lock_sign_count += 1
                            new_weight[i] = float('inf')
                if enforce <= lock_sign_count:
                    temp_enforce = lock_sign_count + 1
                else:
                    temp_enforce = enforce
                influence_count = all_influences_count - new_weight.count(0.0)
                if influence_count <= temp_enforce:
                    new_weight = org_weight[:]
                else:
                    enforce_list = sorted([[i, w] for i, w in enumerate(new_weight)], key=lambda x: x[1], reverse=True)
                    enforce_list = [iw if i < temp_enforce else [iw[0], 0.0] for i, iw in enumerate(enforce_list)]
                    new_weight = [iw[1] for iw in sorted(enforce_list, key=lambda x: x[0])]
                    for i in locked_inf_id_list:
                        new_weight[i] = org_weight_list[i]
                    self.row_column_dict[row] = []
                    force_norm_flag = True
            # 桁数丸めを実行
            elif round_digit is not None:
                pre_round_sum = round(sum(new_weight), 10)
                round_weight = list(map(lambda w: round(w, round_digit), new_weight))
                after_round_sum = round(sum(round_weight), 10)
                sub_value = pre_round_sum - after_round_sum
                if sub_value == 0.0:
                    new_weight = round_weight
                else:
                    for i, w in enumerate(round_weight):
                        if w > abs(sub_value):
                            round_weight[i] = round(w + sub_value, round_digit)
                            break
                    new_weight = round_weight
                force_norm_flag = False
                norm_flag = False
            elif enforce == 0:
                force_norm_flag = False
            else:
                force_norm_flag = True

            all_weight_sum = sum(new_weight)
            if all_weight_sum > 1.0 and not self.no_limit_but.isChecked():
                force_norm_flag = True
            elif force_norm_flag is True:
                pass
            else:
                force_norm_flag = False

            # ウェイト正規化処理
            if (norm_flag or force_norm_flag) and not move_weight and not skip_normalize:
                norm_weight = []
                locked_sum = sum([new_weight[i] for i in locked_inf_id_list])
                if locked_sum >= 1.0:
                    for i, w in enumerate(new_weight):
                        if i in locked_inf_id_list:
                            norm_weight.append(w)
                        else:
                            norm_weight.append(0.0)
                else:
                    column_list = self.row_column_dict[row]
                    column_inf_id_list = [
                        node_influence_id_list[i]
                        for i in column_list
                        if 0 <= i < len(node_influence_id_list) and node_influence_id_list[i] is not None
                    ]

                    # スライダ中は値が振り切っても戻せるように選択外の値はプレス直後のコピーを使う
                    if self.change_flag and getattr(self, 'press_start_data', None) and row < len(self.press_start_data):
                        pressed_weight = list(self.press_start_data[row])
                        for inf_id in column_inf_id_list:
                            if 0 <= inf_id < len(pressed_weight):
                                pressed_weight[inf_id] = new_weight[inf_id]
                        new_weight = pressed_weight

                    select_sum = sum([new_weight[i] for i in column_inf_id_list])
                    other_inf_id_list = list(set(row_inf_id_list) - set(column_inf_id_list))
                    other_sum = sum([new_weight[i] for i in other_inf_id_list if i not in locked_inf_id_list])

                    # ロックセル以外の合計が 0.0 の場合は正規化できないので変更前の値に戻す
                    if other_sum + select_sum == 0.0:
                        new_weight = org_weight[:]
                        select_sum = sum([new_weight[i] for i in column_inf_id_list])
                    if select_sum + locked_sum >= 1.0:
                        for i, w in enumerate(new_weight):
                            if i in locked_inf_id_list:
                                norm_weight.append(w)
                            elif i in column_inf_id_list:
                                norm_weight.append(w / select_sum * (1.0 - locked_sum))
                            else:
                                norm_weight.append(0.0)
                    elif select_sum < 1.0:
                        all_sum = select_sum + locked_sum
                        if all_sum >= 1.0 or other_sum == 0.0:
                            if other_sum == select_sum == 0.0:
                                if not enforce:
                                    bake_weight_flag = False
                            other_ratio = 0.0
                            if select_sum != 0.0:
                                ratio = (1.0 - locked_sum) / select_sum
                            else:
                                ratio = 0.0
                        else:
                            new_other_sum = 1.0 - all_sum
                            other_ratio = new_other_sum / other_sum
                            ratio = 1.0
                        for i, w in enumerate(new_weight):
                            if i in locked_inf_id_list:
                                norm_weight.append(w)
                            elif i in column_inf_id_list:
                                norm_weight.append(new_weight[i] * ratio)
                            else:
                                norm_weight.append(new_weight[i] * other_ratio)
                new_weight = norm_weight[:]

            if bake_weight_flag:
                if node != pre_node and pre_node is not None and node_weight_list:
                    self.bake_node_weight_dict[pre_node] += node_weight_list
                    self.org_node_weight_dict[pre_node] += org_node_weight_list
                    if MAYA_VER >= 2016:
                        self.bake_node_inf_dict[pre_node] = om2.MIntArray() + pre_row_inf_id_list
                    else:
                        self.bake_node_inf_dict[pre_node] = om.MIntArray() + pre_row_inf_id_list
                    node_weight_list = []
                    org_node_weight_list = []

                node_weight_list += new_weight
                org_node_weight_list += org_weight
                self.bake_node_id_dict[node] += [vtx]
                self.redo_node_weight_dict[node].append(list(new_weight))
                self.undo_node_weight_dict[node].append(list(org_weight))
                pre_node = node
                pre_row_inf_id_list = row_inf_id_list

            # テーブルモデルが参照している行リストへ最終値を同期する
            if norm_flag or force_norm_flag or round_digit is not None or move_weight:
                weight_list = self.vtx_weight_dict[vtx_name]
                for i, w in enumerate(new_weight):
                    weight_list[i] = w

            # 表示切替のためにテーブルの不正ウェイトセットを更新する
            after_weight_sum = round(sum(new_weight), 10)
            if after_weight_sum < 1.0:
                self.weight_model.under_weight_rows.add(row)
                self.weight_model.over_weight_rows.discard(row)
            elif after_weight_sum > 1.0:
                self.weight_model.over_weight_rows.add(row)
                self.weight_model.under_weight_rows.discard(row)
            else:
                self.weight_model.under_weight_rows.discard(row)
                self.weight_model.over_weight_rows.discard(row)
            influence_count = all_influences_count - list(new_weight).count(0.0)
            self.weight_model.over_influence_limit_dict[row] = influence_count

        # ループ終了時に最後のノードへ蓄積されたウェイト分を確実にフラッシュする
        if pre_node is not None and node_weight_list:
            self.bake_node_weight_dict[pre_node] += node_weight_list
            self.org_node_weight_dict[pre_node] += org_node_weight_list
            if MAYA_VER >= 2016:
                self.bake_node_inf_dict[pre_node] = om2.MIntArray() + pre_row_inf_id_list
            else:
                self.bake_node_inf_dict[pre_node] = om.MIntArray() + pre_row_inf_id_list

        self.counter.count(string='Weight Calculation :')
        if not self.bake_node_id_dict:
            self.refresh_table_view()
            return

        # 実際の焼き込み（引数の realbake / ignoreundo をそのまま適用する）
        try:
            self.om_bake_skin_weight(realbake=realbake, ignoreundo=ignoreundo)
        except Exception as e:
            try:
                print('Bake Skin Weight Failure :', '{}'.format(e))
                state.load_bundled_plugin(state.BAKE_PLUGIN_NAME, autoload=False)
                self.om_bake_skin_weight(realbake=realbake, ignoreundo=ignoreundo)
            except Exception:
                msg = lang.Lang(
                    en='Failed to write weights. Please check the plugin manager\n' + state.BAKE_PLUGIN_NAME,
                    ja='書き込みに失敗しました。プラグインマネージャを確認してください\n' + state.BAKE_PLUGIN_NAME,
                ).output()
                self.set_message(msg=msg, error=True)

        self.counter.count(string='Bake Skin Weight :')

        # 最後に表示状態の更新
        self.refresh_table_view()

    def om_bake_skin_weight(self, realbake=True, ignoreundo=False):
        # 焼き込みデータをグローバルに展開
        state.set_current_data(
            self.bake_node_id_dict,
            self.bake_node_weight_dict,
            self.bake_node_inf_dict,
            self.node_skinFn_dict,
            self.undo_node_weight_dict,
            self.redo_node_weight_dict,
            self.org_node_weight_dict,
        )
        # 焼き込みコマンド実行
        cmds.bakeSkinWeightImprove(rb=realbake, iu=ignoreundo)

    change_flag = False
    press_start_data = None
    _slider_start_node_weights = None
    _slider_drag_modified = False
    _slider_last_update_rows = None

    def _reset_slider_drag_state(self):
        """スライダードラッグ用の一時状態とスナップショットを安全に初期化する。"""
        self.change_flag = False
        self.press_start_data = None
        self._slider_start_node_weights = None
        self._slider_drag_modified = False
        self._slider_last_update_rows = set()
        self.pre_new_value = 0.0

    def sld_pressed(self):
        """ドラッグ開始時の基準ウェイトを退避し、プレビューモード（Undo 履歴非登録）を開始する。"""
        if self.closed_flag or not getattr(self, 'selected_items', None) or not hasattr(self, 'weight_model'):
            self._reset_slider_drag_state()
            return
        self.press_start_data = copy.deepcopy(self.weight_model._data)
        start_node_weights = {}
        for cell_id in self.selected_items:
            row = cell_id.row()
            row_data = self.vtx_row_dict.get(row)
            if not row_data:
                continue
            vtx = row_data[0]
            node = row_data[6]
            key = (node, vtx)
            if key not in start_node_weights and node in self.node_weight_dict:
                start_node_weights[key] = list(self.node_weight_dict[node][vtx])
        self._slider_start_node_weights = start_node_weights
        self._slider_drag_modified = False
        self._slider_last_update_rows = set()
        self.pre_new_value = 0.0
        self.change_flag = True

    def _commit_slider_drag_undo(self):
        """ドラッグ開始前の元ウェイトから最終ウェイトへの差分を 1 回の Undo コマンドとして確定する。"""
        start_weights = getattr(self, '_slider_start_node_weights', None) or {}
        candidate_rows = sorted(getattr(self, '_slider_last_update_rows', None) or [])
        if not start_weights or not candidate_rows:
            return

        changed_rows = set()
        for row in candidate_rows:
            row_data = self.vtx_row_dict.get(row)
            if not row_data or row >= len(self.weight_model._data):
                continue
            vtx = row_data[0]
            node = row_data[6]
            org_list = start_weights.get((node, vtx))
            cur_list = self.weight_model._data[row]
            if org_list is None or len(org_list) != len(cur_list):
                changed_rows.add(row)
                continue
            if any(abs(a - b) > 1e-12 for a, b in zip(org_list, cur_list)):
                changed_rows.add(row)

        if not changed_rows:
            return

        self.update_rows = changed_rows
        self.row_column_dict = {r: [] for r in changed_rows}
        self.precalculate_bake_weights(
            realbake=True,
            ignoreundo=False,
            skip_normalize=True,
            org_override_dict=start_weights,
        )

    def sld_released(self):
        """ドラッグ終了時に最終差分だけを 1 回の Undo として確定し、Add / Add% 表示を 0 へ戻す。"""
        if not self.change_flag:
            self._reset_slider_drag_state()
            return
        self.change_flag = False
        try:
            if self._slider_drag_modified:
                self._commit_slider_drag_undo()
        finally:
            if self.add_mode in (1, 2):
                self._set_slider_and_spinbox_silent(0.0)
            self._reset_slider_drag_state()

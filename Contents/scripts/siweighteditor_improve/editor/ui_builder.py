# -*- coding: utf-8 -*-
"""SIWeightEditor Improve のメイン UI 構築および動的ボタン再配置 Mixin。"""

import webbrowser

from maya import cmds
from maya import mel

from ..maya_version import MAYA_VER
from ..viewport import bone_visualizer
from .. import common
from .. import go
from .. import lang
from .. import modeling
from .. import prof
from .. import qt
from .. import symmetrize
from .. import weight
from .. import weight_transfer_multiple
from ..qt import *
from . import state
from .widgets import (
    EditorDoubleSpinbox,
    EditorLineEdit,
    EditorSpinbox,
    MyHeaderView,
    RightClickTableView,
    WeightSlider,
)

if state.PYTHON_VER >= 3:
    round = common.round_half_up


class EditorUIBuilderMixin(object):
    """メインウィンドウの UI ウィジェット構築・動的レイアウト・イベント処理を提供する Mixin。"""

    show_flag = False    
    def _init_ui(self, job_create=True):
        if state.WEIGHT_TRANSFER_MULTIPLE is None:
            state.WEIGHT_TRANSFER_MULTIPLE = weight_transfer_multiple.WeightTransferMultiple()
        
        self.counter = prof.LapCounter()#ラップタイム計測クラス
        self.it_counter = prof.IntegrationCounter()#積算カウンタークラス
        self.init_flag=True
        sq_widget = QScrollArea(self)
        sq_widget.setWidgetResizable(True)#リサイズに中身が追従するかどうか
        sq_widget.setFocusPolicy(Qt.NoFocus)#スクロールエリアをフォーカスできるかどうか
        sq_widget.setMinimumHeight(1)#ウィンドウの最小サイズ
        #self.setWindowTitle(u'- SI Weight Editor / ver_'+state.VERSION+' -')
        self.setWindowTitle(state.TITLE)
        self.setObjectName(state.WINDOW_OBJECT_NAME)
        
        self.setCentralWidget(sq_widget)
        
        self.main_layout = QVBoxLayout()
        sq_widget.setLayout(self.main_layout)
        
        self.unique_layout = QGridLayout()
        self.unique_layout.setSpacing(0)#ウェジェットどうしの間隔を設定する
        self.main_layout.addLayout(self.unique_layout)
        
        self.but_list = []
        
        self.ui_color = 68
        self.hilite = 114
        self.lock_col = [180, 60, 60]
        self.red = [150, 60, 60]
        self.orange = [150,96,32]
        self.yellow = [150,125,20]
        self.higreen = [95, 237, 152]
        self.hired = [237, 103, 95]
        but_h = state.BUTTON_HEIGHT
        
        # 表示ボタンをはめる
        show_widget = QWidget()
        show_layout = QHBoxLayout()
        show_layout.setContentsMargins(2, 2, 4, 2)
        show_layout.setSpacing(0)
        show_widget.setLayout(show_layout)
        show_w_list = (48, 66, 52, 48, 62)
        show_total_w = sum(show_w_list) + 10
        show_widget.setMinimumWidth(show_total_w)
        show_widget.setMaximumWidth(show_total_w)
        show_widget.setMaximumHeight(state.WIDGET_HEIGHT+5)
        tip = lang.Lang(en='Show only selected cells', ja=u'選択セルのみ表示').output()
        self.show_but = qt.make_flat_btton(name='Show', bg=self.hilite, border_col=180, w_max=show_w_list[0], w_min=show_w_list[0], h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip)
        tip = lang.Lang(en='Show all cells', ja=u'全てのセルを表示').output()
        self.show_all_but = qt.make_flat_btton(name='Show All', bg=self.hilite, border_col=180, w_max=show_w_list[1], w_min=show_w_list[1], h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip)
        tip = lang.Lang(en='Reflect component selection change in UI', ja=u'コンポーネント選択変更をUIに反映する').output()
        self.focus_but = qt.make_flat_btton(name='Focus', bg=self.hilite, border_col=180, w_max=show_w_list[2], w_min=show_w_list[2], h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip)
        self.focus_but.setChecked(self.focus)
        tip = lang.Lang(en='Toggle display / non-display of Influence with all zero weights being displayed', ja=u'表示中のウェイトがすべてゼロのインフルエンスの表示/非表示を切り替える').output()
        self.filter_but = qt.make_flat_btton(name='Filter', bg=self.hilite, border_col=180, w_max=show_w_list[3], w_min=show_w_list[3], h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip)
        self.filter_but.setChecked(self.filter)
        tip = lang.Lang(en='Highlite points in 3D view to reflect the weight editor selection', ja=u'ウェイトエディタの選択をポイントハイライトに反映').output()
        self.highlite_but = qt.make_flat_btton(name='Highlite', bg=self.hilite, border_col=180, w_max=show_w_list[4], w_min=show_w_list[4], h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip)
        self.highlite_but.setChecked(self.hilite_vtx)
        self.show_but.clicked.connect(self.show_selected_cells)
        self.show_all_but.clicked.connect(self.show_all_cells)
        self.highlite_but.clicked.connect(self.reset_hilite)
        self.filter_but.clicked.connect(lambda : self.get_set_skin_weight(filter=True))
        show_layout.addWidget(self.show_but)
        show_layout.addWidget(self.show_all_but)
        show_layout.addWidget(self.focus_but)
        show_layout.addWidget(self.filter_but)
        show_layout.addWidget(self.highlite_but)
        
        # 不具合行にフォーカスするボタン
        show_bad_widget = QWidget()
        show_bad_layout = QHBoxLayout()
        show_bad_layout.setContentsMargins(2, 2, 4, 2)
        show_bad_layout.setSpacing(0)
        show_bad_widget.setLayout(show_bad_layout)
        bad_w_list = (74, 72, 66, 76, 74)
        bad_total_w = sum(bad_w_list) + 10
        show_bad_widget.setMinimumWidth(bad_total_w)
        show_bad_widget.setMaximumWidth(bad_total_w)
        show_bad_widget.setMaximumHeight(state.WIDGET_HEIGHT+5)
        tip = lang.Lang(en='Focus on the vertex of an incorrect weight value',  ja=u'不正なウェイト値の頂点にフォーカスする').output()
        self.show_bad_but = qt.make_flat_btton(name='Show Bad', bg=self.hilite, border_col=180, w_max=bad_w_list[0], w_min=bad_w_list[0], h_max=but_h, h_min=but_h, 
                                                                flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip)
        tip = lang.Lang(en='Incorrect display setting: Vertex weight of total less than 1.0',  ja=u'不正表示設定 ： 合計1.0未満の頂点ウェイト').output()
        self.under_wt_but = qt.make_flat_btton(name='Under Wt', bg=self.red, border_col=180, w_max=bad_w_list[1], w_min=bad_w_list[1], h_max=but_h, h_min=but_h, 
                                                                flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip)
        tip = lang.Lang(en='Incorrect display setting: Vertex weight larger than 1.0 in total',  ja=u'不正表示設定 ： 合計1.0より大きい頂点ウェイト').output()
        self.over_wt_but = qt.make_flat_btton(name='Over Wt', bg=self.orange, border_col=180, w_max=bad_w_list[2], w_min=bad_w_list[2], h_max=but_h, h_min=but_h, 
                                                                flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip)
        tip = lang.Lang(en='Incorrect display setting: vertices larger than the specified influence number',  ja=u'不正表示設定 ： 指定インフルエンス数以上の頂点').output()
        self.over_inf_but = qt.make_flat_btton(name='Influences', bg=self.yellow, border_col=180, w_max=bad_w_list[3], w_min=bad_w_list[3], h_max=but_h, h_min=but_h, 
                                                                flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip)
        tip = lang.Lang(en='MaintainMaxInfluences check state for skincluster nodes',  ja=u'スキンクラスターノードの maintainMaxInfluences のチェック状態を維持する').output()
        self.keep_mmi_but = qt.make_flat_btton(name='Keep Mmi', bg=self.hilite, border_col=180, w_max=bad_w_list[4], w_min=bad_w_list[4], h_max=but_h, h_min=but_h,
                                                                flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip)

        self.show_bad_but.clicked.connect(self.show_bad_rows)
        self.under_wt_but.setChecked(self.under_wt)
        self.over_wt_but.setChecked(self.over_wt)
        self.over_inf_but.setChecked(self.over_inf)
        self.keep_mmi_but.setChecked(self.keep_mmi)
        show_bad_layout.addWidget(self.show_bad_but)
        show_bad_layout.addWidget(self.under_wt_but)
        show_bad_layout.addWidget(self.over_wt_but)
        show_bad_layout.addWidget(self.over_inf_but)
        show_bad_layout.addWidget(self.keep_mmi_but)
        # アイコンボタン群
        icon_widget = QWidget()
        icon_layout = QHBoxLayout()
        icon_layout.setContentsMargins(2, 2, 4, 2)
        icon_layout.setSpacing(0)
        icon_widget.setLayout(icon_layout)
        but_w = state.BUTTON_HEIGHT
        wid_a = but_w * 4
        tip = lang.Lang(en='Lock display mesh', ja=u'表示メッシュのロック').output()
        self.lock_but = qt.make_flat_btton(name='', bg=self.lock_col, border_col=180, w_max=but_w, w_min=but_w, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=True, destroy_flag=True, icon=self.icon_path+'lock.png', tip=tip)
        tip = lang.Lang(en='Update the view based on object selection', ja=u'オブジェクト選択に基づきビューを更新').output()
        self.cycle_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=but_w, w_min=but_w, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'cycle.png', tip=tip)
        tip = lang.Lang(en='Clear the view', ja=u'ビューのクリア').output()
        self.clear_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=but_w, w_min=but_w, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'clear.png', tip=tip)
        tip = lang.Lang(en='Select vertices from cell selection', ja=u'セル選択から頂点を選択').output()
        self.adjust_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=but_w, w_min=but_w, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'adjust.png', tip=tip)
        self.lock_but.setChecked(self.lock)
        self.lock_but.clicked.connect(self.check_unlock)
        self.cycle_but.clicked.connect(lambda : self.get_set_skin_weight(cycle=True))
        self.clear_but.clicked.connect(lambda : self.get_set_skin_weight(clear=True))
        self.adjust_but.clicked.connect(self.select_vertex_from_cells)
        
        w1_w = 36
        w100_w = 50
        wid_b = w1_w + w100_w + 10
        
        self.max_value_but_group = QButtonGroup()
        tip = lang.Lang(en='Displaying the weight value from 0.0 to 1.0', ja=u'0.0～1.0でウェイト値を表示する').output()
        self.w1_but = qt.make_flat_btton(name='0-1', bg=self.hilite, border_col=180, w_max=w1_w, w_min=w1_w, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip)
        tip = lang.Lang(en='Displaying the weight value from 0 to 100', ja=u'0～100でウェイト値を表示する').output()
        self.w100_but = qt.make_flat_btton(name='0-100', bg=self.hilite, border_col=180, w_max=w100_w, w_min=w100_w, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip)
        self.max_value_but_group.addButton(self.w1_but, 0)
        self.max_value_but_group.addButton(self.w100_but, 1)
        self.pre_max_id = int(round(self.max_wt / 100.0, 0))
        self.max_value_but_group.button(self.pre_max_id).setChecked(True)
        if MAYA_VER >= 2025:
            self.max_value_but_group.idClicked.connect(self.change_maximum_weight)
            self.max_value_but_group.idClicked.connect(self.change_max_decimal_digit)
        else:
            self.max_value_but_group.buttonClicked[int].connect(self.change_maximum_weight)
            self.max_value_but_group.buttonClicked[int].connect(self.change_max_decimal_digit)

        state.MAXIMUM_WEIGHT = self.max_wt
        state.set_maximum_weight(state.MAXIMUM_WEIGHT)
        
        icon_widget.setMinimumWidth(wid_a+wid_b)
        icon_widget.setMaximumWidth(wid_a+wid_b)
        icon_widget.setMaximumHeight(state.WIDGET_HEIGHT+5)
        
        icon_layout.addWidget(self.lock_but)
        icon_layout.addWidget(self.cycle_but)
        icon_layout.addWidget(self.clear_but)
        icon_layout.addWidget(self.adjust_but)
        icon_layout.addWidget(self.w1_but)
        icon_layout.addWidget(self.w100_but)
        
        # enforce_limit を設定する
        enforce_widget = QWidget()
        enforce_layout = QHBoxLayout()
        enforce_layout.setContentsMargins(2, 2, 4, 2)
        enforce_layout.setSpacing(3)
        enforce_widget.setLayout(enforce_layout)
        enf_w = 94
        dec_w = 92
        rnd_w = 74
        spin_w = 28
        enforce_total_w = enf_w + dec_w + rnd_w + spin_w * 3 + 3 * 5 + 10
        enforce_widget.setMinimumWidth(enforce_total_w)
        enforce_widget.setMaximumWidth(enforce_total_w)
        enforce_widget.setMaximumHeight(state.WIDGET_HEIGHT+5)
        tip = lang.Lang(en='Enforce the limit on the number of deformers that can affect a given vertex\n* Apply to all vertices by executing without selecting a cell', 
                        ja=u'指定された頂点に影響を与えるデフォーマの数に制限を設定\n※セルを選択せずに実行で全頂点に適用').output()
        self.enforce_but = qt.make_flat_btton(name='Enforce Limit', bg=self.hilite, border_col=180, w_max=enf_w, w_min=enf_w, h_max=but_h, h_min=but_h, 
                                                                flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip)
        self.enforce_but.clicked.connect(self.enforce_limit_and_normalize)
        self.limit_box = EditorSpinbox()
        self.limit_box.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.limit_box.setRange(0, 16)
        self.limit_box.setValue(self.limit)
        self.limit_box.setMaximumWidth(spin_w)
        self.limit_box.setMinimumWidth(spin_w)
        self.limit_box.setMinimumHeight(state.BUTTON_HEIGHT)
        self.limit_box.valueChanged.connect(self.change_limit_box_color)
        self.limit_box.valueChanged.connect(self.change_limit)
        self.change_limit_box_color()
        
        state.MAXIMUM_INFLUENCE_COUNT = self.limit
        state.set_maximum_influence_count(state.MAXIMUM_INFLUENCE_COUNT)
        
        # 桁数設定ウィジェット
        tip = lang.Lang(en='Set the number of decimal places to be displayed', 
                        ja=u'表示される小数点以下の桁数を設定').output()
        self.decimal_but = qt.make_flat_btton(name='Display Digit', bg=self.hilite, border_col=180, w_max=dec_w, w_min=dec_w, h_max=but_h, h_min=but_h, 
                                                                flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip)
        self.digit_box = EditorSpinbox()
        self.digit_box.setButtonSymbols(QAbstractSpinBox.NoButtons)
        offset = (1 - self.max_value_but_group.checkedId()) * 2
        self.digit_box.setRange(offset, 7 + offset)
        self.digit_box.setValue(self.digit)
        self.digit_box.setMaximumWidth(spin_w)
        self.digit_box.setMinimumWidth(spin_w)
        self.digit_box.setMinimumHeight(state.BUTTON_HEIGHT)
        self.digit_box.valueChanged.connect(self.change_decimal_digits)
        
        state.FLOAT_DECIMALS = self.digit
        state.FLOAT_FORMAT = '{:.'+str(state.FLOAT_DECIMALS)+'f}'
        state.set_float_decimals(state.FLOAT_DECIMALS)
        
        tip = lang.Lang(en='Round off the decimal point with the specified number of digits\n* Apply to all vertices by executing without selecting a celｌ', 
                        ja=u'小数点以下を指定桁数で丸める\n※セルを選択せずに実行で全頂点に適用').output()
        self.round_but = qt.make_flat_btton(name='Round Off', bg=self.hilite, border_col=180, w_max=rnd_w, w_min=rnd_w, h_max=but_h, h_min=but_h, 
                                                                flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip)
        self.round_but.clicked.connect(self.round_off)
        self.round_box = EditorSpinbox()
        self.round_box.setButtonSymbols(QAbstractSpinBox.NoButtons)
        offset = (1 - self.max_value_but_group.checkedId()) * 2
        self.round_box.setRange(offset, 7 + offset)
        self.round_box.setValue(self.round)
        self.round_box.setMaximumWidth(spin_w)
        self.round_box.setMinimumWidth(spin_w)
        self.round_box.setMinimumHeight(state.BUTTON_HEIGHT)
        
        enforce_layout.addWidget(self.enforce_but)
        enforce_layout.addWidget(self.limit_box)
        enforce_layout.addWidget(self.decimal_but)
        enforce_layout.addWidget(self.digit_box)
        enforce_layout.addWidget(self.round_but)
        enforce_layout.addWidget(self.round_box)
        
        # ジョイント選択ツールタイプ（標準機能：ヘッダー右クリック時のツール切替）
        sel_joint_widget = QWidget()
        sel_joint_widget.setGeometry(QRect(0, 0, 0 ,0))
        sel_joint_layout = QHBoxLayout()
        sel_joint_layout.setContentsMargins(2, 2, 4, 2)
        sel_joint_layout.setSpacing(0)
        sel_joint_widget.setLayout(sel_joint_layout)
        but_w = 22
        sel_joint_total_w = but_w * 4 + 10
        sel_joint_widget.setMinimumWidth(sel_joint_total_w)
        sel_joint_widget.setMaximumWidth(sel_joint_total_w)
        sel_joint_widget.setMaximumHeight(state.WIDGET_HEIGHT+5)
        self.sel_joint_but_group = QButtonGroup()
        tip = lang.Lang(en='Do not change the tool when selecting the joint by right clicking on the header', 
                            ja=u'ヘッダー右クリックでジョイント選択するときにツール変更しない').output()
        self.n_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=but_w, w_min=but_w, h_max=but_h, h_min=but_h, 
                                                            flat=True, hover=True, checkable=True, destroy_flag=True, icon=self.icon_path+'n.png', tip=tip)
        tip = lang.Lang(en='Change tool to scaling when joint selection by header right click', 
                            ja=u'ヘッダー右クリックでジョイント選択するときにツールをスケールにする').output()
        self.s_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=but_w, w_min=but_w, h_max=but_h, h_min=but_h, 
                                                            flat=True, hover=True, checkable=True, destroy_flag=True, icon=self.icon_path+'s.png', tip=tip)
        tip = lang.Lang(en='Change tool to rotation when joint selection by header right click', 
                            ja=u'ヘッダー右クリックでジョイント選択するときにツールをローテーションにする').output()
        self.r_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=but_w, w_min=but_w, h_max=but_h, h_min=but_h, 
                                                            flat=True, hover=True, checkable=True, destroy_flag=True, icon=self.icon_path+'r.png', tip=tip)
        tip = lang.Lang(en='Change tool to translation when joint selection by header right click', 
                            ja=u'ヘッダー右クリックでジョイント選択するときにツールをトランスレーションにする').output()
        self.t_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=but_w, w_min=but_w, h_max=but_h, h_min=but_h, 
                                                            flat=True, hover=True, checkable=True, destroy_flag=True, icon=self.icon_path+'t.png', tip=tip)
        if MAYA_VER >= 2025:
            self.sel_joint_but_group.idClicked.connect(self.change_joint_tool_mode)
        else:
            self.sel_joint_but_group.buttonClicked[int].connect(self.change_joint_tool_mode)
        self.sel_joint_but_group.addButton(self.n_but, 0)
        self.sel_joint_but_group.addButton(self.s_but, 1)
        self.sel_joint_but_group.addButton(self.r_but, 2)
        self.sel_joint_but_group.addButton(self.t_but, 3)
        self.sel_joint_but_group.button(self.joint_tool).setChecked(True)

        sel_joint_layout.addWidget(self.n_but)
        sel_joint_layout.addWidget(self.s_but)
        sel_joint_layout.addWidget(self.r_but)
        sel_joint_layout.addWidget(self.t_but)

        # Improve 追加機能：表示・選択補助系グループ（Joint Hilite / Used Joints / Bone Vis / Keep Bone）
        view_ui_col = [50, 68, 76]
        view_hi_col = [74, 134, 156]
        view_border_col = [116, 170, 190]
        view_text_col = 230

        improve_view_widget = QWidget()
        improve_view_layout = QHBoxLayout()
        improve_view_layout.setContentsMargins(2, 2, 4, 2)
        improve_view_layout.setSpacing(0)
        improve_view_widget.setLayout(improve_view_layout)
        tag_view_w = 42
        j_hl_w = 82
        used_jnt_w = 84
        bone_vis_w = 68
        keep_bone_w = 78
        improve_view_total_w = tag_view_w + j_hl_w + used_jnt_w + bone_vis_w + keep_bone_w + 10
        improve_view_widget.setMinimumWidth(improve_view_total_w)
        improve_view_widget.setMaximumWidth(improve_view_total_w)
        improve_view_widget.setMaximumHeight(state.WIDGET_HEIGHT+5)

        self.improve_view_tag = QLabel('+View')
        self.improve_view_tag.setAlignment(Qt.AlignCenter)
        self.improve_view_tag.setFixedSize(tag_view_w, but_h)
        self.improve_view_tag.setStyleSheet(
            'QLabel { background-color: #263840; color: #8ad8f0; font-weight: bold; font-size: 10px;'
            ' border: 1px solid #74aabe; border-right: 0px; }'
        )
        self.improve_view_tag.setToolTip(
            lang.Lang(
                en='[Improve Add-on: Display / Selection Helpers]\nToggle buttons for viewport bone display and selection retention.',
                ja=u'[Improve 追加機能 / 表示・選択補助系]\nビューポート表示やボーン選択維持を切り替えるトグルボタン群です。',
            ).output()
        )

        tip = lang.Lang(
            en='[Improve Add-on / Display]\nHighlight weighted / selected joints in viewport (restores original colors safely)\nRight click to switch mode (Heatmap / Selected Column)',
            ja=u'[Improve 追加機能 / 表示系]\n影響ボーンや選択列ボーンをビューポート上で色強調（終了・保存時に元の色を復元）\n右クリックでモード切替（ヒートマップ / 選択列のみ）'
        ).output()
        self.joint_hl_but = qt.make_flat_btton(
            name='Joint Hilite', text=view_text_col, ui_color=view_ui_col, bg=view_hi_col, border_col=view_border_col,
            w_max=j_hl_w, w_min=j_hl_w, h_max=but_h, h_min=but_h,
            flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip
        )
        self.joint_hl_but.setChecked(self.joint_hilite)
        self.joint_hl_but.clicked.connect(self.reset_joint_hl)
        self.joint_hl_but.rightClicked.connect(self.pop_joint_hilite_menu)

        tip = lang.Lang(
            en='[Improve Add-on / Display]\nShow only weighted / selected joints using drawStyle (hides unrelated parents & children)\nRight click to switch mode (Used Only / Keep Bone Shape)',
            ja=u'[Improve 追加機能 / 表示系]\ndrawStyle を使って使用中ボーンだけをピンポイント表示（無関係な親子も非表示・保存時自動復元）\n右クリックで表示範囲切替（使用ボーンのみ / 骨形状維持）'
        ).output()
        self.used_joint_vis_but = qt.make_flat_btton(
            name='Used Joints', text=view_text_col, ui_color=view_ui_col, bg=view_hi_col, border_col=view_border_col,
            w_max=used_jnt_w, w_min=used_jnt_w, h_max=but_h, h_min=but_h,
            flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip
        )
        self.used_joint_vis_but.setChecked(getattr(self, 'used_joints', False))
        self.used_joint_vis_but.clicked.connect(self.update_used_joint_visibility)
        self.used_joint_vis_but.rightClicked.connect(self.pop_used_joints_menu)

        tip = lang.Lang(
            en='[Improve Add-on / Display]\nVisualize weighted bones of selected vertices in 3D viewport (X-Ray)\nRight click for display options',
            ja=u'[Improve 追加機能 / 表示系]\n選択頂点にウェイトが入っているボーンを3Dビューポート上に透視表示（X-Ray）\n右クリックで表示オプションを設定'
        ).output()
        self.bone_vis_but = qt.make_flat_btton(
            name='Bone Vis', text=view_text_col, ui_color=view_ui_col, bg=view_hi_col, border_col=view_border_col,
            w_max=bone_vis_w, w_min=bone_vis_w, h_max=but_h, h_min=but_h,
            flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip
        )
        self.bone_vis_but.setChecked(getattr(self, 'bone_vis', False))
        self.bone_vis_but.clicked.connect(self.toggle_bone_visualizer)
        self.bone_vis_but.rightClicked.connect(self.pop_bone_vis_menu)
        bone_visualizer.set_options(
            show_lines=getattr(self, 'bone_vis_lines', True),
            show_labels=getattr(self, 'bone_vis_labels', True),
            dashed_tiny=getattr(self, 'bone_vis_dashed_tiny', True),
            aggregate_mode=getattr(self, 'bone_vis_mode', 'avg'),
        )

        self.last_selected_influences = []
        self._restoring_bone_selection = False
        tip = lang.Lang(
            en='[Improve Add-on / Display]\nKeep previous bone column selected when switching vertices (even when 0 weight under Filter)',
            ja=u'[Improve 追加機能 / 表示系]\n別の頂点を選択しても直前に選んでいたボーン列の選択状態を維持します\nFilter ON 時でもウェイト0の列として残るため、そのままスライダーで追加できます'
        ).output()
        self.keep_bone_but = qt.make_flat_btton(
            name='Keep Bone', text=view_text_col, ui_color=view_ui_col, bg=view_hi_col, border_col=view_border_col,
            w_max=keep_bone_w, w_min=keep_bone_w, h_max=but_h, h_min=but_h,
            flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip
        )
        self.keep_bone_but.setChecked(getattr(self, 'keep_bone', True))
        self.keep_bone_but.clicked.connect(self.toggle_keep_bone)

        improve_view_layout.addWidget(self.improve_view_tag)
        improve_view_layout.addWidget(self.joint_hl_but)
        improve_view_layout.addWidget(self.used_joint_vis_but)
        improve_view_layout.addWidget(self.bone_vis_but)
        improve_view_layout.addWidget(self.keep_bone_but)

        # Improve 追加機能：専用ウィンドウ起動系グループ（Remap / Model Edit）
        win_ui_col = [66, 56, 86]
        win_hi_col = [114, 92, 160]
        win_border_col = [152, 134, 198]
        win_text_col = 235

        improve_win_widget = QWidget()
        improve_win_layout = QHBoxLayout()
        improve_win_layout.setContentsMargins(2, 2, 4, 2)
        improve_win_layout.setSpacing(0)
        improve_win_widget.setLayout(improve_win_layout)
        tag_win_w = 38
        remap_w = 58
        model_edit_w = 74
        improve_win_total_w = tag_win_w + remap_w + model_edit_w + 10
        improve_win_widget.setMinimumWidth(improve_win_total_w)
        improve_win_widget.setMaximumWidth(improve_win_total_w)
        improve_win_widget.setMaximumHeight(state.WIDGET_HEIGHT+5)

        self.improve_win_tag = QLabel('+Win')
        self.improve_win_tag.setAlignment(Qt.AlignCenter)
        self.improve_win_tag.setFixedSize(tag_win_w, but_h)
        self.improve_win_tag.setStyleSheet(
            'QLabel { background-color: #312844; color: #cdb8ff; font-weight: bold; font-size: 10px;'
            ' border: 1px solid #9886c6; border-right: 0px; }'
        )
        self.improve_win_tag.setToolTip(
            lang.Lang(
                en='[Improve Add-on: Tool Windows]\nButtons that open dedicated auxiliary tool windows.',
                ja=u'[Improve 追加機能 / ウィンドウ起動系]\n押すと専用の補助ツールウィンドウが開くボタン群です。',
            ).output()
        )

        self.bone_remap_window = None
        tip = lang.Lang(
            en='[Improve Add-on / Window]\nOpen Bone Weight Remap window to move or add weights between bones in real time',
            ja=u'[Improve 追加機能 / ウィンドウ]\n選択頂点のウェイトを別ボーンへ移動・追加する専用ウィンドウを開く'
        ).output()
        self.bone_remap_but = qt.make_flat_btton(
            name='Remap', text=win_text_col, ui_color=win_ui_col, bg=win_hi_col, border_col=win_border_col,
            w_max=remap_w, w_min=remap_w, h_max=but_h, h_min=but_h,
            flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip
        )
        self.bone_remap_but.clicked.connect(self.open_bone_remap_window)

        self.model_edit_window = None
        tip = lang.Lang(
            en='[Improve Add-on / Window]\nOpen Model & Bone Edit / Cleanup window (JSON weight backup, safe bind, mirror/copy, joint cleanup)',
            ja=u'[Improve 追加機能 / ウィンドウ]\nモデル＆ボーン修正 / バインド・ミラー / ジョイント整理の専用ウィンドウを開く'
        ).output()
        self.model_edit_but = qt.make_flat_btton(
            name='Model Edit', text=win_text_col, ui_color=win_ui_col, bg=win_hi_col, border_col=win_border_col,
            w_max=model_edit_w, w_min=model_edit_w, h_max=but_h, h_min=but_h,
            flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip
        )
        self.model_edit_but.clicked.connect(self.open_model_edit_window)

        improve_win_layout.addWidget(self.improve_win_tag)
        improve_win_layout.addWidget(self.bone_remap_but)
        improve_win_layout.addWidget(self.model_edit_but)
        
        # ボタン群の並びを一括設定
        self.but_list.append(show_widget)
        self.but_list.append(sel_joint_widget)
        self.but_list.append(show_bad_widget)
        self.but_list.append(icon_widget)
        self.but_list.append(enforce_widget)
        self.but_list.append(improve_view_widget)
        self.but_list.append(improve_win_widget)
        #self.but_list.append(decimal_widget)
        #self.but_list.append(lock_widget)
        #self.but_list.append(mode_widget)
        self.set_column_stretch(self.but_list, self.unique_layout)#ボタンの並び伸びないように固定
        
        self.main_layout.addWidget(qt.make_h_line())
        
        #-----------------------------------------------------------------------------------------------------
        self.unique_layout3 = QGridLayout()#3個目のユニークレイアウト
        self.unique_layout3.setSpacing(0)#ウェジェットどうしの間隔を設定する
        self.main_layout.addLayout(self.unique_layout3)
        self.but_list3 = []
        
        #サブツール群を配置
        sub_tool0_widget = QWidget()
        #qt.change_widget_color(sub_tool0_widget, bgColor=[255])
        sub_tool0_widget.setContentsMargins(-9, -7,  0 ,2)#マージン設定
        sub_tool0_widget.setGeometry(QRect(0, 0, 0 ,0))
        sub_tool0_layout = QHBoxLayout()
        sub_tool0_layout.setSpacing(0)#ウェジェットどうしの間隔を設定する
        sub_tool0_widget.setLayout(sub_tool0_layout)
        size =210
        sub_tool0_widget.setMinimumWidth(size)
        sub_tool0_widget.setMaximumWidth(size)
        sub_tool0_widget.setMaximumHeight(state.WIDGET_HEIGHT+5)
        
        #サブツール類のレイアウト
        #label = QLabel('Sub Tools :')
        #sub_tool_layout.addWidget(label)
        
        #ウェイトハンマー
        tip = lang.Lang(
                            en='*Weight Hummer\n\n' +\
                            'Execute a weight hammer at the vertex of the selected cell\n'+\
                            'Right-click Run with the application rate of smooth', 
                            ja=u'・ウェイトハンマー\n\n' +\
                            u'選択セルの頂点にウェイトハンマーを実行する\n' +\
                            u'右クリック スムースの適用率を指定して実行'
                            ).output()
        self.hummer_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'hummer.png', tip=tip)
        self.hummer_but.clicked.connect(qt.Callback(self.hammer_weight))
        self.hummer_but.rightClicked.connect(qt.Callback(lambda : self.hammer_weight(blend=True)))
        sub_tool0_layout.addWidget(self.hummer_but)
        
        #キープゼロウェイトハンマー
        tip = lang.Lang(
                            en='*Weight Hummer\n\nExecute a weight hammer at the vertex of the selected cell\n'+\
                            '*Hold weight value 0.0\n' +\
                            'Right-click Run with the application rate of smooth', 
                            ja=u'・ウェイトハンマー\n\n選択セルの頂点にウェイトハンマーを実行する\n' +\
                            u'※ウェイト値0.0を維持する\n' +\
                            u'右クリック スムースの適用率を指定して実行'
                            ).output()
        self.keep_zero_hummer_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'hummer_zero.png', tip=tip)
        self.keep_zero_hummer_but.clicked.connect(qt.Callback(lambda : self.hammer_weight(keep_zero=True)))
        self.keep_zero_hummer_but.rightClicked.connect(qt.Callback(lambda : self.hammer_weight(keep_zero=True, blend=True)))
        sub_tool0_layout.addWidget(self.keep_zero_hummer_but)
        
        sub_tool0_layout.addWidget(QLabel(' '))
        
        #ウェイトスムース
        tip = lang.Lang(en='*Smooth skin weights \n\nRight-click Run with the application rate of smooth', 
                            ja=u'・スキンウェイトのスムーズを実行\n\n右クリック スムースの適用率を指定して実行').output()
        self.smooth_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'smooth.png', tip=tip)
        self.smooth_but.clicked.connect(qt.Callback(self.smooth_weight))
        self.smooth_but.rightClicked.connect(qt.Callback(lambda : self.smooth_weight(blend=True)))
        sub_tool0_layout.addWidget(self.smooth_but)
        
        #キープゼロウェイトスムース
        tip = lang.Lang(en='*Smooth skin weights\n\n*Hold weight value 0.0 \n Right-click Run with the application rate of smooth', 
                            ja=u'・スキンウェイトのスムーズを実行\n\n※ウェイト値0.0を維持する\n右クリック スムースの適用率を指定して実行').output()
        self.keep_zero_smooth_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'smooth_zero.png', tip=tip)
        self.keep_zero_smooth_but.clicked.connect(qt.Callback(lambda : self.smooth_weight(keep_zero=True)))
        self.keep_zero_smooth_but.rightClicked.connect(qt.Callback(lambda : self.smooth_weight(keep_zero=True, blend=True)))
        sub_tool0_layout.addWidget(self.keep_zero_smooth_but)
        
        
        sub_tool0_layout.addWidget(QLabel(' '))
        
        spin_w = 30
        tip = lang.Lang(en='*Open Smooth Setting window', 
                                ja=u'・スムース設定ウィンドウを開く').output()
        self.smooth_ratio_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'smooth_setting.png', tip=tip)
        self.smooth_ratio_but.clicked.connect(self.open_smooth_setting_editor)
        sub_tool0_layout.addWidget(self.smooth_ratio_but)
        sub_tool0_layout.addWidget(QLabel(' '))
        
        tip = lang.Lang(en='*Specify application rate of smooth and weight hammers(%) \nSmooth and hammers are applied by right click', 
                                ja=u'・スムース、ウェイトハンマーの適用率を%指定\nスムース、ハンマーを右クリック実行で適用されます').output()
        self.smooth_ratio_box = EditorSpinbox()
        self.smooth_ratio_box.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.smooth_ratio_box.setRange(0, 100)
        self.smooth_ratio_box.setToolTip(tip)
        self.smooth_ratio_box.setValue(self.smooth_parcent)
        self.smooth_ratio_box.setMaximumWidth(spin_w)
        self.smooth_ratio_box.setMinimumWidth(spin_w)
        self.smooth_ratio_box.setMinimumHeight(state.BUTTON_HEIGHT)
        sub_tool0_layout.addWidget(self.smooth_ratio_box)
        
        sub_tool0_layout.addWidget(QLabel(' '))
        
        #フリーズ
        tip = lang.Lang(en='*Freeze\n\nDelete all history and write back deformer cluster and blend shape\nEasy history cleanup function', 
                            ja=u'・Freeze\n\nヒストリを全削除したあとデフォーマクラスタとブレンドシェイプを書き戻します\nヒストリの簡単クリーンアップ機能').output()
        self.freeze_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'freeze.png', tip=tip)
        self.freeze_but.clicked.connect(qt.Callback(self.freeze))
        sub_tool0_layout.addWidget(self.freeze_but)
        
        #フリーズM
        tip = lang.Lang(en='*Freeze_M\n\nAfter deleting all history, write back skin weight, deformer cluster, blend shape \n'+\
                                'Function to clean up history while keeping skinning \n'+\
                                'You can also bake lattice while skinning', 
                            ja=u'・Freeze_M\n\nヒストリを全削除したあと、スキンウェイト、デフォーマクラスタ、ブレンドシェイプを書き戻します\n'+\
                                u'スキニングを保持したままヒストリをきれいにする機能\n'+\
                                u'スキニングしたままラティスをベイクしたりもできます').output()
        self.freeze_m_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'freeze_m.png', tip=tip)
        self.freeze_m_but.clicked.connect(qt.Callback(self.freeze_m))
        sub_tool0_layout.addWidget(self.freeze_m_but)
        
        #サブツール群を配置-------------------------------------------------------------------------------------------------------------------------------
        sub_tool1_widget = QWidget()
        #qt.change_widget_color(sub_tool1_widget, bgColor=[255])
        sub_tool1_widget.setContentsMargins(-9, -7, 0 ,2)#マージン設定
        sub_tool1_widget.setGeometry(QRect(0, 0, 0 ,0))
        sub_tool1_layout = QHBoxLayout()
        sub_tool1_layout.setSpacing(0)#ウェジェットどうしの間隔を設定する
        sub_tool1_widget.setLayout(sub_tool1_layout)
        if MAYA_VER >= 2016:
            size = 212 + 44
        else:
            size = 190 + 44
        sub_tool1_widget.setMinimumWidth(size)
        sub_tool1_widget.setMaximumWidth(size)
        sub_tool1_widget.setMaximumHeight(state.WIDGET_HEIGHT+5)
        #ウェイトコピー
        tip = lang.Lang(en='*Simple_Weight_Copy\n\nCopy the mesh weight \nWrite it out externally as a temporary file', 
                            ja=u'・Simple_Weight_Copy\n\nメッシュのウェイトをコピーします\n一時ファイルとして外部に書き出し').output()
        self.simple_copy_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'simple_copy.png', tip=tip)
        self.simple_copy_but.clicked.connect(qt.Callback(self.weight_copy))
        sub_tool1_layout.addWidget(self.simple_copy_but)

        #ウェイトペースト
        tip = lang.Lang(en='*Simple_Weight_Paste(Name / Index)\n\nPaste the weight of the mesh from the copy information \nWrite back with mesh name and vertex number', 
                            ja=u'・Simple_Weight_Paste(Name / Index)\n\nメッシュのウェイトをコピー情報からペーストします\nメッシュ名と頂点番号で書き戻し').output()
        self.simple_paste_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'simple_topology.png', tip=tip)
        self.simple_paste_but.clicked.connect(qt.Callback(self.weight_paste))
        sub_tool1_layout.addWidget(self.simple_paste_but)
        
        #ウェイトペーストポジション
        if MAYA_VER >= 2016:#Maya2016以上はNearestが使える
            tip = lang.Lang(en='*Simple_Weight_Paste(Name / Position)\n\nPaste the weight of mesh from copy information \nWrite back in mesh name and neighborhood coordinates of vertex', 
                                ja=u'・Simple_Weight_Paste(Name / Position)\n\nメッシュのウェイトをコピー情報からペーストします\nメッシュ名と頂点の近傍座標で書き戻し').output()
            self.simple_paste_position_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                        flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'simple_position.png', tip=tip)
            self.simple_paste_position_but.clicked.connect(qt.Callback(lambda : self.weight_paste(method='nearest', threshold=2.0)))
            sub_tool1_layout.addWidget(self.simple_paste_position_but)
        
        sub_tool1_layout.addWidget(QLabel(' '))
        
        #ウェイトトランスファー
        tip = lang.Lang(en='*Transfer_Weight_Multiple / Copy\n\nWeight Specify the transfer source mesh \nMultiple meshes can be specified', 
                            ja=u'・Transfer_Weight_Multiple / Copy\n\nウェイト転送元のメッシュを指定します\n複数メッシュ指定可能').output()
        self.transfer_copy_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'transfer_copy.png', tip=tip)
        self.transfer_copy_but.clicked.connect(qt.Callback(self.weight_transfer_copy))
        sub_tool1_layout.addWidget(self.transfer_copy_but)
        
        #ウェイトトランスファー
        tip = lang.Lang(en='*Transfer_Weight_Multiple / Transfer\n\nWait transfer source Weight is transferred from the specified mesh to the selected mesh \nMultiple mesh can be specified', 
                            ja=u'・Transfer_Weight_Multiple / Transfer\n\nウェイト転送元指定したメッシュから選択メッシュにウェイトを転送します\n複数メッシュ指定可能').output()
        self.transfer_paste_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'transfer_paste.png', tip=tip)
        self.transfer_paste_but.clicked.connect(qt.Callback(self.weight_transfer_paste))
        sub_tool1_layout.addWidget(self.transfer_paste_but)
        
        sub_tool1_layout.addWidget(QLabel(' '))
        #ウェイトムーブコピー
        tip = lang.Lang(en='*Move Inflence Weight / Copy\n\nSet the influence of the weight transfer source from the selected cell \nMultiple designation possible', 
                            ja=u'・Move Inflence Weight / Copy\n\n選択セルからウェイト移動元のインフルエンスを設定\n複数指定可能').output()
        self.move_copy_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'move_copy.png', tip=tip)
        self.move_copy_but.clicked.connect(qt.Callback(self.copy_move_influences))
        sub_tool1_layout.addWidget(self.move_copy_but)
        
        #ウェイトムーブペースト
        tip = lang.Lang(en='*Move Inflence Weight / Paste\n\nWeight Distribute the influence weight specified as the source of movement evenly over the influence of the selected cell \nMultiple cells can be specified', 
                            ja=u'・Move Inflence Weight / Paste\n\nウェイト移動元に指定したインフルエンスのウェイトを選択セルのインフルエンスに均等分配します\n複数セル指定可能').output()
        self.move_paste_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'move_paste.png', tip=tip)
        self.move_paste_but.clicked.connect(qt.Callback(self.paste_move_influences))
        sub_tool1_layout.addWidget(self.move_paste_but)
        
        sub_tool1_layout.addWidget(QLabel(' '))
        
        #バーテックスウェイトのコピー
        tip = lang.Lang(en='*Copy vertex weight \nMultiple rows selectable',
                                ja=u'・頂点ウェイトのコピー\n複数行選択可').output()
        self.vertex_copy_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'vtx_copy.png', tip=tip)
        self.vertex_copy_but.clicked.connect(self.vertex_copy)
        sub_tool1_layout.addWidget(self.vertex_copy_but)
        
        #バーテックスウェイトのペースト
        tip = lang.Lang(en='*Paste vertex weight \nMultiple rows selectable\nInsufficient influence is added automatically',
                                ja=u'・頂点ウェイトのペースト\n複数行選択可\n※不足しているインフルエンスは自動追加されます').output()
        self.vertex_paste_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'vtx_paste.png', tip=tip)
        self.vertex_paste_but.clicked.connect(qt.Callback(self.vertex_paste))
        sub_tool1_layout.addWidget(self.vertex_paste_but)
        
        sub_tool1_layout.addWidget(QLabel(' '))
        
        #セルウェイトのコピー
        tip = lang.Lang(en='*Excel-like cell copy \nMultiple cell selection is possible',
                                ja=u'・エクセルライクなセルのコピー\n複数セル選択可').output()
        self.cell_copy_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'cell_copy.png', tip=tip)
        self.cell_copy_but.clicked.connect(self.cell_copy)
        sub_tool1_layout.addWidget(self.cell_copy_but)
        
        #セルウェイトのペースト
        tip = lang.Lang(en='*Excel-like cell paste \nMultiple cell selection is possible',
                                ja=u'・エクセルライクなセルのペースト\n複数セル選択可').output()
        self.cell_paste_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'cell_paste.png', tip=tip)
        self.cell_paste_but.clicked.connect(self.cell_paste)
        sub_tool1_layout.addWidget(self.cell_paste_but)
        #-----------------------------------------------------------------------------------------------------
        #サブツール群を配置
        sub_tool2_widget = QWidget()
        #qt.change_widget_color(sub_tool2_widget, bgColor=[255])
        sub_tool2_widget.setContentsMargins(-9, -7, 0 ,2)#マージン設定
        sub_tool2_widget.setGeometry(QRect(0, 0, 0 ,0))
        sub_tool2_layout = QHBoxLayout()
        sub_tool2_layout.setSpacing(0)#ウェジェットどうしの間隔を設定する
        sub_tool2_widget.setLayout(sub_tool2_layout)
        size = 230
        sub_tool2_widget.setMinimumWidth(size)
        sub_tool2_widget.setMaximumWidth(size)
        sub_tool2_widget.setMaximumHeight(state.WIDGET_HEIGHT+5)
        #ウェイトシンメトリ
        tip = lang.Lang(en='*Weight_Symmetrize\n\n'+\
                            'Mirror weights of selected objects and components\n'+\
                            'Joint label is automatically generated from setting\n\n'+\
                            'Right click to open label setting rule editor', 
                            ja=u'・Weight_Symmetrize\n\n'+\
                            u'選択したオブジェクト、コンポーネントのウェイトをミラーリングします\n'+\
                            u'ジョイントラベルは設定から自動生成されます\n\n'+\
                            u'右クリックでラベル設定ルールエディタを開きます').output()
        self.sym_weight_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'sym_weight.png', tip=tip)
        self.sym_weight_but.clicked.connect(qt.Callback(symmetrize.WeightSymmetrize))
        self.sym_weight_but.rightClicked.connect(self.open_joint_rule_editor)
        sub_tool2_layout.addWidget(self.sym_weight_but)
        
        #メッシュとウェイトをセットでシンメトリ
        tip = lang.Lang(en='*Auto_Symmetry\n\n'+\
                            'Copy selected objects in reverse\n'+\
                            'In case of skin meshes, we also mirror the weight automatically\n\n'+\
                            'Right click to open label setting rule editor', 
                            ja=u'・Auto_Symmetry\n\n'+\
                            u'選択オブジェクトを反転コピー\n'+\
                            u'スキンメッシュの場合はウェイトも自動でミラーリングします\n\n'+\
                            u'右クリックでラベル設定ルールエディタを開きます').output()
        self.auto_symmetry_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'auto_symmetry.png', tip=tip)
        self.auto_symmetry_but.clicked.connect(qt.Callback(symmetrize.mesh_weight_symmetrize))
        self.auto_symmetry_but.rightClicked.connect(self.open_joint_rule_editor)
        sub_tool2_layout.addWidget(self.auto_symmetry_but)
        
        #メッシュマージ
        tip = lang.Lang(en='*Merge Mesh with Skinning\n\n'+\
                            'Combine the selected mesh while keeping the skinning\n'+\
                            'With standard functions, data can not be broken or can not be undone, so original implementation\n\n'+\
                            'It is convenient to merge after Auto Symmetry', 
                            ja=u'・Mesh Marge with Skinning\n\n'+\
                            u'選択メッシュをスキニングを保持したまま結合します\n'+\
                            u'標準機能ではデータが壊れたりアンドゥできない（Mayaごと落ちる）ことが多いので独自実装\n\n'+\
                            u'Auto Symmetry後にマージしたりとか便利です').output()
        self.marge_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'marge.png', tip=tip)
        self.marge_but.clicked.connect(qt.Callback(lambda : modeling.MeshMarge().main(cmds.ls(sl=True, l=True))))
        sub_tool2_layout.addWidget(self.marge_but)
        
        sub_tool2_layout.addWidget(QLabel(' '))
        
        #ウェイトのミュート
        tip = lang.Lang(en='*Toggle_Mute_Skinning \n\nToggle the muting state of the selected mesh skinning \n If you do not select anything, apply it to all skin meshes', 
                            ja=u'・Toggle_Mute_Skinning \n\n選択メッシュのスキニングのミュート状態をトグル\n何も選択せずに実行すると全てのスキンメッシュに適用').output()
        self.toggle_mute_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'mute.png', tip=tip)
        self.toggle_mute_but.clicked.connect(qt.Callback(weight.toggle_mute_skinning))
        sub_tool2_layout.addWidget(self.toggle_mute_but)
        
        #バインドポーズ
        tip = lang.Lang(en='*Go_to_Bind_Pose \n\nReturn to bind pose', 
                            ja=u'・Go_to_Bind_Pose \n\nバインドポーズにもどります').output()
        self.bind_pose_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'bind_pose.png', tip=tip)
        self.bind_pose_but.clicked.connect(lambda : mel.eval('gotoBindPose;'))
        sub_tool2_layout.addWidget(self.bind_pose_but)
        
        sub_tool2_layout.addWidget(QLabel(' '))
        
        #Go Maya Exoport
        tip = lang.Lang(en='*Go_Maya_Export\n\n'+\
                            'Export the selected object as a temporary file\n'+\
                            'The last output is overwritten\n\n'+\
                            'Object passing tool between maya scenes\n'+\
                            'It is compatible with the same function of SI Side Bar',
                            ja=u'・Go_Maya_Export\n\n'+\
                            u'選択したオブジェクトを一時ファイルとして書き出します\n'+\
                            u'前回出力分は上書きされます\n\n'+\
                            u'シーン間のオブジェクト受け渡しツール\n'+\
                            u'SI Side Bar の同機能とも互換性があります').output()
        self.go_export_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'go_export.png', tip=tip)
        self.go_export_but.clicked.connect(go.maya_export)
        sub_tool2_layout.addWidget(self.go_export_but)
        
        #Go Maya Import
        tip = lang.Lang(en='*Go_Maya_Import\n\n'+\
                            'Receive objects output by Go_Maya_Export\n\n'+\
                            'Object passing tool between maya scenes\n'+\
                            'It is compatible with the same function of SI Side Bar',
                            ja=u'・Go_Maya_Import\n\n'+\
                            u'Go_Maya_Export で出力したオブジェクトを受け取ります\n\n'+\
                            u'シーン間のオブジェクト受け渡しツール\n'+\
                            u'SI Side Bar の同機能とも互換性があります').output()
        self.go_import_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'go_import.png', tip=tip)
        self.go_import_but.clicked.connect(qt.Callback(go.maya_import))
        sub_tool2_layout.addWidget(self.go_import_but)
        
        sub_tool2_layout.addWidget(QLabel(' '))
        
        #インフルエンスの追加
        tip = lang.Lang(en='*Add Influence to Skin'+\
                            'Add joints and meshes directly selected\n'+\
                            'Add by adding influence on UI',
                            ja=u'・スキンにインフルエンスを追加\n\n'+\
                            u'ジョイントとメッシュを直接選択して追加\n'+\
                            u'UI上のインフルエンスを選択して追加').output()
        self.add_inf_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'inf_add.png', tip=tip)
        self.add_inf_but.clicked.connect(qt.Callback(lambda : self.add_remove_influences(mode='add')))
        sub_tool2_layout.addWidget(self.add_inf_but)
        
        #インフルエンスの削除
        tip = lang.Lang(en='*Remove skin influence'+\
                            'Select joint and mesh directly and remove\n'+\
                            'Select influence on UI and remove',
                            ja=u'・スキンのインフルエンスを削除\n\n'+\
                            u'ジョイントとメッシュを直接選択して削除\n'+\
                            u'UI上のインフルエンスを選択して削除').output()
        self.rem_inf_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'inf_rem.png', tip=tip)
        self.rem_inf_but.clicked.connect(qt.Callback(lambda : self.add_remove_influences(mode='remove')))
        sub_tool2_layout.addWidget(self.rem_inf_but)
        
        #未使用インフルエンスの除去
        tip = lang.Lang(en='*Remove Unused Influences',
                            ja=u'・使われていないインフルエンスの除去').output()
        self.rem_unused_inf_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'inf_clean.png', tip=tip)
        self.rem_unused_inf_but.clicked.connect(qt.Callback(self.remove_unused_influences))
        sub_tool2_layout.addWidget(self.rem_unused_inf_but)
        
        self.but_list3.append(sub_tool0_widget)
        self.but_list3.append(sub_tool1_widget)
        self.but_list3.append(sub_tool2_widget)
        
        self.set_column_stretch(self.but_list3, self.unique_layout3)#ボタンの並び伸びないように固定
        #self.init_but_width_list(but_list=self.but_list)#配置実行
        
        self.main_layout.addWidget(qt.make_h_line())

        #-------------------------------------------------------------------------------------------
        #2個目のユニークレイアウト
        self.unique_layout2 = QGridLayout()
        self.unique_layout2.setSpacing(0)#ウェジェットどうしの間隔を設定する
        self.main_layout.addLayout(self.unique_layout2)
        self.but_list2 = []
        
        # 計算モードボタンをはめる
        mode_widget = QWidget()
        mode_layout = QHBoxLayout()
        mode_layout.setContentsMargins(2, 2, 4, 2)
        mode_widget.setLayout(mode_layout)
        mode_layout.setSpacing(0)
        abs_w = 36
        add_w = 38
        add_par_w = 46
        norm_w = 74
        unlim_w = 72
        length = abs_w + add_w + add_par_w + norm_w + unlim_w + 10
        mode_widget.setMinimumWidth(length)
        mode_widget.setMaximumWidth(length)
        mode_widget.setMaximumHeight(state.WIDGET_HEIGHT+5)
        
        self.mode_but_group = QButtonGroup()
        tip = lang.Lang(
            en='Values entered represent absolute values\nRight click to switch multi-cell slider mode (Average / Median)',
            ja=u'絶対値で再入力\n右クリックで複数セル選択時のスライダー基準値（平均 / 中央値）を切替'
        ).output()
        self.abs_but = qt.make_flat_btton(name='Abs', bg=self.hilite, border_col=180, w_max=abs_w, w_min=abs_w, h_max=but_h, h_min=but_h, 
                                                            flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip)
        self.abs_but.rightClicked.connect(self.pop_slider_stat_menu)
        tip = lang.Lang(en='Values entered are added to exisiting values', ja=u'既存値への加算入力').output()
        self.add_but = qt.make_flat_btton(name='Add', bg=self.hilite, border_col=180, w_max=add_w, w_min=add_w, h_max=but_h, h_min=but_h, 
                                                            flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip)
        tip = lang.Lang(en='Values entered are percentages added to exisiting values', ja=u'既存値への率加算入力').output()
        self.add_par_but = qt.make_flat_btton(name='Add%', bg=self.hilite, border_col=180, w_max=add_par_w, w_min=add_par_w, h_max=but_h, h_min=but_h, 
                                                                flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip)
        tip = lang.Lang(en='Normalize new weights\n\nRight-click forced normalization execution\n*If you execute without selecting a cell, it will be applied to all cells', 
                                ja=u'新規ウェイトの正規化\n\n右クリックで強制正規化実行\n※セルを選択せずに実行すると全てのセルに適用されます').output()
        self.norm_but = qt.make_flat_btton(name='Normalize', bg=self.hilite, border_col=180, w_max=norm_w, w_min=norm_w, h_max=but_h, h_min=but_h, 
                                                            flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip)
        tip = lang.Lang(en='Accept weight values ​​greater than 1.0 or 100 in total', ja=u'合計1.0もしくは100以上のウェイト値を許容する').output()
        self.no_limit_but = qt.make_flat_btton(name='Unlimited', bg=self.hilite, border_col=180, w_max=unlim_w, w_min=unlim_w, h_max=but_h, h_min=but_h, 
                                                            flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip)
        self.norm_but.clicked.connect(self.toggle_no_limit_but_enable)
        self.norm_but.rightClicked.connect(lambda : self.enforce_limit_and_normalize(force_norm=True))
        self.no_limit_but.clicked.connect(self.keep_no_limit_flag)
        if MAYA_VER >= 2025:
            self.mode_but_group.idClicked.connect(self.change_add_mode)
        else:
            self.mode_but_group.buttonClicked[int].connect(self.change_add_mode)

        self.norm_but.setChecked(self.norm)
        self.no_limit_but.setChecked(self.no_limit)
        self.toggle_no_limit_but_enable()
        
        self.mode_but_group.addButton(self.abs_but, 0)
        self.mode_but_group.addButton(self.add_but, 1)
        self.mode_but_group.addButton(self.add_par_but, 2)
        if self.mode < 0:
            self.mode = 0
        self.mode_but_group.button(self.mode).setChecked(True)
        mode_layout.addWidget(self.abs_but)
        mode_layout.addWidget(self.add_but)
        mode_layout.addWidget(self.add_par_but)
        mode_layout.addWidget(self.norm_but)
        mode_layout.addWidget(self.no_limit_but)
        #self.main_layout.addWidget(mode_widget)
        
        # 固定入力ボタン
        static_input_widget = QWidget()
        static_input_layout = QHBoxLayout()
        static_input_layout.setContentsMargins(2, 2, 4, 2)
        static_input_widget.setLayout(static_input_layout)
        static_input_layout.setSpacing(0)
        but_w = 34
        length = but_w * 7 + 10
        static_input_widget.setMinimumWidth(length)
        static_input_widget.setMaximumWidth(length)
        static_input_widget.setMaximumHeight(state.WIDGET_HEIGHT+5)
        
        #固定値ボタン
        tip = lang.Lang(en='Fixed value input \n Left click -> absolute value input \n Shift + left click -> addition \n Ctrl + left click -> subtraction', 
                                    ja=u'・固定数値入力 \n左クリックで絶対値入力\nShift + 左クリックで加算\nCtrl + 左クリックで減算').output()
        self.n00_but = qt.make_flat_btton(name='0.0', bg=self.hilite, border_col=180, w_max=but_w, w_min=but_w, h_max=but_h, h_min=but_h, 
                                                            flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip)
        static_input_layout.addWidget(self.n00_but)
        self.n01_but = qt.make_flat_btton(name='0.1', bg=self.hilite, border_col=180, w_max=but_w, w_min=but_w, h_max=but_h, h_min=but_h, 
                                                            flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip)
        static_input_layout.addWidget(self.n01_but)
        self.n025_but = qt.make_flat_btton(name='0.25', bg=self.hilite, border_col=180, w_max=but_w, w_min=but_w, h_max=but_h, h_min=but_h, 
                                                            flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip)
        static_input_layout.addWidget(self.n025_but)
        self.n05_but = qt.make_flat_btton(name='0.5', bg=self.hilite, border_col=180, w_max=but_w, w_min=but_w, h_max=but_h, h_min=but_h, 
                                                            flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip)
        static_input_layout.addWidget(self.n05_but)
        self.n075_but = qt.make_flat_btton(name='0.75', bg=self.hilite, border_col=180, w_max=but_w, w_min=but_w, h_max=but_h, h_min=but_h, 
                                                            flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip)
        static_input_layout.addWidget(self.n075_but)
        self.n09_but = qt.make_flat_btton(name='0.9', bg=self.hilite, border_col=180, w_max=but_w, w_min=but_w, h_max=but_h, h_min=but_h, 
                                                            flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip)
        static_input_layout.addWidget(self.n09_but)
        self.n10_but = qt.make_flat_btton(name='1.0', bg=self.hilite, border_col=180, w_max=but_w, w_min=but_w, h_max=but_h, h_min=but_h, 
                                                            flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip)
        static_input_layout.addWidget(self.n10_but)
        
        #文字替えのためにリストにしておく
        self.static_input_buttons = [self.n00_but, self.n01_but, self.n025_but, self.n05_but, self.n075_but, self.n09_but, self.n10_but]
        
        self.n00_but.clicked.connect(lambda : self.apply_static_value(0.0))
        self.n01_but.clicked.connect(lambda : self.apply_static_value(0.1))
        self.n025_but.clicked.connect(lambda : self.apply_static_value(0.25))
        self.n05_but.clicked.connect(lambda : self.apply_static_value(0.5))
        self.n075_but.clicked.connect(lambda : self.apply_static_value(0.75))
        self.n09_but.clicked.connect(lambda : self.apply_static_value(0.9))
        self.n10_but.clicked.connect(lambda : self.apply_static_value(1.0))
        
        static_input_layout.addStretch(0)
        
        # Weight ロックボタン
        lock_widget = QWidget()
        lock_layout = QHBoxLayout()
        lock_layout.setContentsMargins(2, 2, 4, 2)
        lock_layout.setSpacing(0)
        lock_widget.setLayout(lock_layout)
        lock_w_list = (64, 76, 78)
        lock_total_w = sum(lock_w_list) + 10
        lock_widget.setMinimumWidth(lock_total_w)
        lock_widget.setMaximumWidth(lock_total_w)
        lock_widget.setMaximumHeight(state.WIDGET_HEIGHT+5)
        tip = lang.Lang(en='Lock selected weights\nRight click to lock influence of all vertices', 
                                ja=u'選択ウェイトのロック\n右クリックで全頂点のインフルエンスロック').output()
        self.weight_lock_but = qt.make_flat_btton(name='Lock Wt', bg=self.hilite, border_col=180, w_max=lock_w_list[0], w_min=lock_w_list[0], h_max=but_h, h_min=but_h, 
                                                                flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip)
        tip = lang.Lang(en='Unlock selected weights\nRight click to unlock influence of all vertices',  
                                ja=u'選択ウェイトのロック解除\n右クリックで全頂点のインフルエンスアンロック').output()
        self.weight_unlock_but = qt.make_flat_btton(name='Unlock Wt', bg=self.hilite, border_col=180, w_max=lock_w_list[1], w_min=lock_w_list[1], h_max=but_h, h_min=but_h, 
                                                                flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip)
        tip = lang.Lang(en='Unlock selected weights',  
                                ja=u'すべてのウェイトロックの解除').output()
        self.weight_lock_clear_but = qt.make_flat_btton(name='Clear locks', bg=self.hilite, border_col=180, w_max=lock_w_list[2], w_min=lock_w_list[2], h_max=but_h, h_min=but_h, 
                                                                flat=True, hover=True, checkable=False, destroy_flag=True, tip=tip)
        self.weight_lock_but.clicked.connect(self.lock_weight)
        self.weight_lock_but.rightClicked.connect(self.lock_all_rows)
        self.weight_unlock_but.clicked.connect(self.unlock_weight)
        self.weight_unlock_but.rightClicked.connect(lambda : self.lock_all_rows(lock=False))
        self.weight_lock_clear_but.clicked.connect(self.clear_lock_weight)
        lock_layout.addWidget(self.weight_lock_but)
        lock_layout.addWidget(self.weight_unlock_but)
        lock_layout.addWidget(self.weight_lock_clear_but)
        
        self.but_list2.append(lock_widget)
        self.but_list2.append(mode_widget)
        self.but_list2.append(static_input_widget)
        
        self.set_column_stretch(self.but_list2, self.unique_layout2)#ボタン間隔が伸びないようにする
        
        self.main_layout.addWidget(qt.make_h_line())
        #---------------------------------------------------------------------------------------------------
                
        #スライダー作成
        sld_layout = QHBoxLayout()
        self.main_layout.addLayout(sld_layout)
        self.weight_input = EditorDoubleSpinbox()#スピンボックス
        self.weight_input.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.weight_input.setRange(0, 255)
        self.weight_input.setValue(0.0)#値を設定
        self.weight_input.setDecimals(1)#値を設定
        self.weight_input.setContextMenuPolicy(Qt.CustomContextMenu)
        self.weight_input.customContextMenuRequested.connect(self.pop_slider_stat_menu)
        sld_layout.addWidget(self.weight_input)
        #スライダバーを設定
        self.weight_input_sld = WeightSlider(Qt.Horizontal)
        self.weight_input_sld.setRange(0, 255)
        self.weight_input_sld.setContextMenuPolicy(Qt.CustomContextMenu)
        self.weight_input_sld.customContextMenuRequested.connect(self.pop_slider_stat_menu)
        sld_layout.addWidget(self.weight_input_sld)
        #スライダーとボックスの値をコネクト。連動するように設定。
        self.weight_input.valueChanged.connect(self.change_from_spinbox)
        self.weight_input.wheeled.connect(lambda : self.store_keypress(True))
        self.weight_input.wheeled.connect(lambda : self.calc_cell_value(from_spinbox=True))
        self.weight_input.editingFinished.connect(lambda : self.calc_cell_value(from_spinbox=True))
        self.weight_input.focused.connect(lambda : self.store_keypress(False))
        self.weight_input.keypressed.connect(lambda : self.store_keypress(True))
        self.weight_input_sld.valueChanged.connect(self.change_from_sld)
        self.weight_input_sld.sliderPressed.connect(self.sld_pressed)
        self.weight_input_sld.valueChanged.connect(lambda : self.calc_cell_value(from_slider=True))
        self.weight_input_sld.sliderReleased.connect(self.sld_released)
        
        tip = lang.Lang(en='Clear cell selection and spin box value', 
                            ja=u'セル選択とスピンボックスの値をクリア').output()
        self.clear_selection_but = qt.make_flat_btton(name='C', bg=self.hilite, border_col=180, w_max=18, w_min=18, h_max=18, h_min=18, 
                                                            flat=True, hover=True, checkable=False, destroy_flag=True, icon=None, tip=tip)
        self.clear_selection_but.clicked.connect(self.clear_selection)
        sld_layout.addWidget(self.clear_selection_but)
        
        self.main_layout.addWidget(qt.make_h_line())
        #-------------------------------------------------------------------------------------------
        seach_layout = QHBoxLayout()
        self.main_layout.addLayout(seach_layout)
        
        label = QLabel('Search: ')
        seach_layout.addWidget(label)
        seach_layout.setSpacing(0)#ウェジェットどうしの間隔を設定する
        
        self.search_but_group = QButtonGroup()
        tip = lang.Lang(en='Refine joints search by specified character string', 
                            ja=u'表示中のジョイントを指定文字列で絞り込み検索').output()
        self.refine_but = qt.make_flat_btton(name='Refine', bg=self.hilite, border_col=180, w_max=45, w_min=45, h_max=18, h_min=18, 
                                                            flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip)
        tip = lang.Lang(en='Add the joint containing the specified character string to the current display', 
                            ja=u'指定文字列を含むジョイントを現在の表示に加える').output()
        self.search_add_but = qt.make_flat_btton(name='Add', bg=self.hilite, border_col=180, w_max=35, w_min=35, h_max=18, h_min=18, 
                                                            flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip)
        self.search_but_group.addButton(self.refine_but , 0)
        self.search_but_group.addButton(self.search_add_but , 1)
        #print('search  mode :', self.search_mode)
        if self.search_mode < 0:
            self.search_mode = 0
        self.search_but_group.button(self.search_mode).setChecked(True)
        self.search_but_group.buttonClicked.connect(lambda : self.get_set_skin_weight())
        seach_layout.addWidget(self.refine_but)
        seach_layout.addWidget(self.search_add_but)
        
        seach_layout.addWidget(QLabel(' '))
        
        tip = lang.Lang(en='Clear Search Window', 
                            ja=u'検索窓をクリア').output()
        self.clear_search_but = qt.make_flat_btton(name='C', bg=self.hilite, border_col=180, w_max=18, w_min=18, h_max=18, h_min=18, 
                                                            flat=True, hover=True, checkable=False, destroy_flag=True, icon=None, tip=tip)
        self.clear_search_but.clicked.connect(self.clear_search)
        seach_layout.addWidget(self.clear_search_but)
        
        seach_layout.addWidget(QLabel(' '))
        
        self.search_line = EditorLineEdit()
        search_tip = lang.Lang(
            en='Search joints (space-separated):\n'
               '  arm leg     : partial match (OR)\n'
               '  +arm +_L    : must match all (AND)\n'
               '  -twist      : exclude matching joints\n'
               '  =Arm_L      : exact name match\n'
               '  Arm_*_L     : wildcard match',
            ja=u'ジョイント検索（スペース区切り）:\n'
               u'  arm leg     : いずれかに部分一致（OR）\n'
               u'  +arm +_L    : すべて含む（AND）\n'
               u'  -twist      : 該当ボーンを除外（!も可）\n'
               u'  =Arm_L      : ボーン名の厳密一致\n'
               u'  Arm_*_L     : ワイルドカード一致'
        ).output()
        self.search_line.setToolTip(search_tip)
        self.search_line.modifired.connect(self.change_static_text)
        self.search_line.editingFinished.connect(lambda : self.search_joint(as_interractive=False))
        self.search_line.textChanged.connect(lambda : self.search_joint(as_interractive=True))
        seach_layout.addWidget(self.search_line)

        tip = lang.Lang(
            en='Flip L / R naming in search bar (L <-> R, Left <-> Right)',
            ja=u'検索欄の左右表記（L ⇔ R / Left ⇔ Right）をワンクリックで反転'
        ).output()
        self.search_flip_lr_but = qt.make_flat_btton(
            name='L<>R', bg=self.hilite, border_col=180, w_max=38, w_min=38, h_max=18, h_min=18,
            flat=True, hover=True, checkable=False, destroy_flag=True, icon=None, tip=tip
        )
        self.search_flip_lr_but.clicked.connect(self.flip_search_lr)
        seach_layout.addWidget(self.search_flip_lr_but)
        
        tip = lang.Lang(en='Open non-modal Joints search window', 
                            ja=u'非モーダルのジョイント検索ウィンドウを開く').output()
        self.joint_list_but = qt.make_flat_btton(name='Joints...', bg=self.hilite, border_col=180, w_max=58, w_min=58, h_max=18, h_min=18, 
                                                            flat=True, hover=True, checkable=False, destroy_flag=True, icon=None, tip=tip)
        self.joint_list_but.clicked.connect(self.open_joint_search_dialog)
        seach_layout.addWidget(self.joint_list_but)
        
        seach_layout.addWidget(QLabel(' '))
        
        tip = lang.Lang(en='Incremental search', 
                            ja=u'インクリメンタルサーチ').output()
        self.interactive_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=18, w_min=18, h_max=18, h_min=18, 
                                                            flat=True, hover=True, checkable=True, destroy_flag=True, icon=self.icon_path+'i.png', tip=tip)
        seach_layout.addWidget(self.interactive_but)
        self.interactive_but.setChecked(self.interactive)

        # Keep Bone 中に対象メッシュへ未バインドのボーンがあるときだけ表示されるワンクリック追加ボタン
        self.quick_bind_but = qt.make_flat_btton(
            name='+ Bind', bg=[70, 125, 90], border_col=180, h_max=18, h_min=18,
            flat=True, hover=True, checkable=False, destroy_flag=True,
            tip=lang.Lang(
                en='Bind the kept bone(s) to the current mesh and select them',
                ja=u'記憶中の未バインドボーンを現在のメッシュへ追加してセル選択します'
            ).output()
        )
        self.quick_bind_but.clicked.connect(qt.Callback(self.quick_bind_kept_influences))
        self.quick_bind_but.setVisible(False)
        seach_layout.addWidget(self.quick_bind_but)
        
        #----------------------------------------------------------------------------------------------------
        
        #HorizontalHeaderを縦書きにするカスタムクラス
        self.headerView = MyHeaderView()
        self.headerView.doubleClicked.connect(self.toggle_lock_weight)
        self.headerView.rightClicked.connect(self.select_joint_from_header)
        
        #テーブル作成
        self.view_widget = RightClickTableView(self)
        self.view_widget.setHorizontalHeader(self.headerView)
        self.view_widget.verticalHeader().setDefaultSectionSize(20)
        self.view_widget.verticalHeader().setContextMenuPolicy(Qt.CustomContextMenu)
        self.view_widget.verticalHeader().customContextMenuRequested.connect(self.focus_vertex_from_header)
        self.view_widget.rightClicked.connect(self.get_clicke_item_value)
        self.view_widget.setEditTriggers(QAbstractItemView.NoEditTriggers)#編集スタート不可能にする
        self.view_widget.setAlternatingRowColors(True)
        #self.view_widget.setEditTriggers(QAbstractItemView.AllEditTriggers)#全ての方式で編集スタート可能にする
        self.view_widget.keyPressed .connect(self.direct_cell_input)
        self.main_layout.addWidget(self.view_widget)
        
        #--------------------------------------------------------------------------------------------
        msg_layout = QHBoxLayout()
        
        tip = lang.Lang(en='Set the brightness of the cell character whose value is zero', 
                            ja=u'値がゼロのセル文字の明るさを設定').output()
        label = QLabel('')
        label.setPixmap(QPixmap(self.icon_path+'value.png'))
        label.setToolTip(tip)
        label.setMaximumWidth(20)
        label.setMinimumWidth(20)
        msg_layout.addWidget(label)
        self.zero_darken = EditorDoubleSpinbox()#スピンボックス
        self.zero_darken.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.zero_darken.setRange(0, 999)
        self.zero_darken.setValue(self.darken_value)#値を設定
        self.zero_darken.setDecimals(0)#値を設定
        self.zero_darken.editingFinished.connect(self.zero_cell_darken)#値を設定
        self.zero_darken.setToolTip(tip)
        self.zero_darken.setMaximumWidth(42)
        self.zero_darken.setMinimumWidth(42)
        msg_layout.addWidget(self.zero_darken)
        
        
        but_w = 92
        tip = lang.Lang(en='Change whether dockable to Maya UI',  
                                ja=u'Maya UIにドッキング可能かどうかを変更する').output()
        self.docking_but = qt.make_flat_btton(name='Dock Enable', bg=self.hilite, border_col=180, w_max=but_w, w_min=but_w, h_max=but_h, h_min=but_h, 
                                                                flat=True, hover=True, checkable=True, destroy_flag=True, tip=tip)
        self.docking_but.clicked.connect(self.change_dockable)
        self.docking_but.setChecked(self.dockable)
        msg_layout.addWidget(self.docking_but)
        
        msg_layout.addWidget(qt.make_v_line())
        
        tip = lang.Lang(en='Show latest release page', ja=u'最新リリースページを表示').output()
        self.release_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'release.png', tip=tip)
        self.release_but.clicked.connect(lambda : webbrowser.open(state.REREASE_PATH))
        msg_layout.addWidget(self.release_but)
        
        tip = lang.Lang(en='Display help page', ja=u'ヘルプページの表示').output()
        self.help_but = qt.make_flat_btton(name='', bg=self.hilite, border_col=180, w_max=state.BUTTON_HEIGHT, w_min=state.BUTTON_HEIGHT, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=False, destroy_flag=True, icon=self.icon_path+'help.png', tip=tip)
        self.help_but.clicked.connect(lambda : webbrowser.open(state.HELP_PATH))
        msg_layout.addWidget(self.help_but)
        
        label = QLabel(state.VERSION)
        msg_layout.addWidget(label)
        
        msg_layout.addWidget(qt.make_v_line())
        
        #実行時間のお知らせ
        self.main_layout.addLayout(msg_layout)
        self.time_label = QLabel('- Calculation Time - 0.00000 sec')
        self.time_label.setMaximumWidth(180)
        self.time_label.setMinimumWidth(165)
        msg_layout.addWidget(self.time_label)
        
        msg_layout.addWidget(qt.make_v_line())

        but_w = 65

        tip = lang.Lang(en='Live Updating', ja=u'リスト更新中').output()
        self.scriptjob_but = qt.make_flat_btton(name='', bg=self.higreen, border_col=180, w_max=state.BUTTON_HEIGHT+104, w_min=state.BUTTON_HEIGHT+104, h_max=but_h, h_min=but_h, 
                                                    flat=True, hover=True, checkable=True, destroy_flag=True, icon=self.icon_path+'power-off.png', tip=tip)
        self.scriptjob_but.clicked.connect(self.switch_scriptjob)
        self.scriptjob_but.setChecked(self.scriptjob)
        msg_layout.addWidget(self.scriptjob_but)

        self.scriptjob_label = QLabel('run')
        self.scriptjob_label.setMaximumWidth(160)
        self.scriptjob_label.setMinimumWidth(160)
        # msg_layout.addWidget(self.scriptjob_label)

        self.update_scriptjob_state()

        msg_layout.addWidget(qt.make_v_line())

        #ウェイトエディタからの通知
        self.msg_label = QLabel('')
        msg_layout.addWidget(self.msg_label)
        
        msg_layout.setSpacing(6)#ウェジェットどうしの間隔を設定する
        msg_layout.addStretch(0)
        
        self.create_job()
        #self.change_add_mode(id=self.mode)
        self.change_decimal_digits()
        self.zero_cell_darken()
        self.get_set_skin_weight()
        self.change_add_mode(id=self.mode)
        
        self.installEventFilter(self)
        
    #PySide2以降でモディファイアの状態を正しく取得できないので自前フラグ管理
    shift_isPressed = False
    ctrl_isPressed = False
    def eventFilter(self, obj, event):
        shift_num = 16777248
        ctrl_num = 16777249
        #shift と　ctrlでボタンの文字変える
        if event.type() == QEvent.KeyPress:
            if event.key() == shift_num:
                self.shift_isPressed = True
            if event.key() == ctrl_num:
                self.ctrl_isPressed = True
            self.change_static_text(reserve_mod=True)
        if event.type() == QEvent.KeyRelease:
            if event.key() == shift_num:
                self.shift_isPressed = False
            if event.key() == ctrl_num:
                self.ctrl_isPressed = False
            self.change_static_text(reserve_mod=True)
        return False
    
    
    def switch_scriptjob(self):
        
        self.scriptjob = self.scriptjob_but.isChecked()
        if self.scriptjob:
            self.create_job()
        else:
            self.remove_job()
        
        self.update_scriptjob_state()
            
    def update_scriptjob_state(self):
        if self.scriptjob:
            self.scriptjob_but.setText("Live Updating")
            tip = lang.Lang(en='Enable loading of weights', ja=u'ウェイトリストの更新が有効です。').output()
            self.scriptjob_but.setToolTip(tip)
            qt.change_button_color(self.scriptjob_but, textColor=60, bgColor=self.higreen, mode='button', destroy=True, dsColor=180)
        else:
            self.scriptjob_but.setText("Update Paused")
            tip = lang.Lang(en='Disable loading of weights', ja=u'ウェイトリストの更新が無効です。').output()
            self.scriptjob_but.setToolTip(tip)
            qt.change_button_color(self.scriptjob_but, textColor=60, bgColor=self.hired, mode='button', destroy=True, dsColor=180)
        
        styleSheet = self.scriptjob_but.styleSheet()
        if not "text-align:left" in styleSheet:
            self.scriptjob_but.setStyleSheet(styleSheet.replace("QPushButton{", "QPushButton{text-align:left;"))
        
        # self.lock_col = [180, 60, 60]
        # self.red = [150, 60, 60]
        # self.orange = [150,96,32]
    
    #MayaUIにドッキングかのうかどうかを変更
    def change_dockable(self):
        if MAYA_VER <= 2016:
            if self.docking_but.isChecked():
                self.show(dockable=True,
                                area=None,
                                floating=True ,
                                width=self.width(),
                                height=self.height())
            else:
                from .. import siweighteditor
                siweighteditor.OptionWindow(offset=True)
        else:
            if self.docking_but.isChecked():
                self.save_window_data()
                self.show(dockable=True,
                        area=None,
                        floating=False,
                        width=self.width(),
                        height=self.height())
            else:
                self.save_window_data()
                from .. import siweighteditor
                siweighteditor.Option()

    #ボタンが左詰めになるように調整
    def set_column_stretch(self, but_list, layout):
        self.def_but_width_list = self.init_but_width_list(but_list)
        for i in range(self.def_but_width_list[-1]):
            layout.setColumnStretch(i, 0)
        layout.setColumnStretch(i + 1, 1)
        
    def init_but_width_list(self, but_list):
        but_width_list = [0]
        sum_width = 0
        for but in but_list:
            sum_width += max(but.width(), but.minimumWidth())
            but_width_list.append(sum_width)
        return but_width_list
        
    def resizeEvent(self, event):
        if self.init_flag:
            return
        win_x = event.size().width()
        self.re_arrangement_but(win_x=win_x, grid_v=0, but_list=self.but_list, loop=0, layout=self.unique_layout)
        self.re_arrangement_but(win_x=win_x, grid_v=0, but_list=self.but_list3, loop=0, layout=self.unique_layout3)
        self.re_arrangement_but(win_x=win_x, grid_v=0, but_list=self.but_list2, loop=0, layout=self.unique_layout2)
        
        
    pre_row_count = 0
    def re_arrangement_but(self, win_x, grid_v, but_list, loop, layout):
        if loop > 100:
            return
        if not but_list:
            return
        but_width_list = self.init_but_width_list(but_list)
        for i, but_width in enumerate(but_width_list[::-1][:-1]):
            if win_x > but_width + 16:
                if i != 0:
                    set_but_list = but_list[:-i]
                else:
                    set_but_list = but_list[:]
                for j, but in enumerate(set_but_list):
                    layout.addWidget(but, grid_v, but_width_list[j], 1, max(but.width(), but.minimumWidth()))
                break
        but_num = len(but_list) - i
        new_but_list = but_list[but_num:]
        self.re_arrangement_but(win_x=win_x, grid_v=grid_v+1, but_list=new_but_list, loop=loop+1, layout=layout)

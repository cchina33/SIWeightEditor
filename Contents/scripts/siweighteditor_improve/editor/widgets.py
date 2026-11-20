# -*- coding: utf-8 -*-
"""SIWeightEditor Improve で使用するカスタム Qt ウィジェットおよびテーブルモデル群。"""

from maya import cmds

from ..maya_version import MAYA_VER
from .. import qt
from ..qt import *
from . import state


class EditorDoubleSpinbox(QDoubleSpinBox):
    """ホイール増減・フォーカス時全選択を備えたカスタム浮動小数点スピンボックス。"""

    wheeled = Signal()
    focused = Signal()
    keypressed = Signal()
    mousepressed = Signal()
    wheel_flag = False

    def __init__(self, parent=None):
        super(EditorDoubleSpinbox, self).__init__(parent)
        self.installEventFilter(self)

    def wheelEvent(self, e):
        pass

    def eventFilter(self, obj, event):
        if event.type() == QEvent.FocusIn:
            self.sel_all_input()
            self.focused.emit()
        if event.type() == QEvent.Wheel:
            raw_delta = event.angleDelta().y() if hasattr(event, "angleDelta") else event.delta()
            if raw_delta != 0:
                delta = raw_delta / abs(raw_delta)
                shift_mod = self.check_shift_modifiers()
                ctrl_mod = self.check_ctrl_modifiers()
                max_wt = state.MAXIMUM_WEIGHT
                if shift_mod:
                    self.setValue(self.value() + delta * 0.001 * max_wt)
                elif ctrl_mod:
                    self.setValue(self.value() + delta * 0.1 * max_wt)
                else:
                    self.setValue(self.value() + delta * 0.01 * max_wt)
                self.wheel_flag = True
                cmds.evalDeferred(self.emit_wheel_event)
        if event.type() == QEvent.KeyPress:
            self.keypressed.emit()
        if event.type() == QEvent.MouseButtonPress:
            self.mousepressed.emit()
        return False

    def emit_wheel_event(self):
        self.wheeled.emit()
        self.wheel_flag = False

    def sel_all_input(self):
        cmds.evalDeferred(self.select_box_all)

    def select_box_all(self):
        try:
            self.selectAll()
        except Exception:
            pass

    def check_shift_modifiers(self):
        mods = QApplication.keyboardModifiers()
        return bool(mods & Qt.ShiftModifier)

    def check_ctrl_modifiers(self):
        mods = QApplication.keyboardModifiers()
        return bool(mods & Qt.ControlModifier)


class EditorLineEdit(QLineEdit):
    """Ctrl / Shift 押下時にフォーカスが飛ばないカスタムラインエディット。"""

    modifired = Signal()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Control or key == Qt.Key.Key_Shift:
            self.modifired.emit()
            return
        super(EditorLineEdit, self).keyPressEvent(event)


class JointSearchDialog(QDialog):
    """現在のインフルエンス一覧から検索対象ジョイントを選択するダイアログ。"""

    def __init__(self, influences, selected_names=None, nearby_names=None, parent=None):
        super(JointSearchDialog, self).__init__(parent)
        self.setWindowTitle("Joints")
        self.resize(300, 420)
        self.selected_names = selected_names or []
        self.nearby_names = set(nearby_names or [])
        self.influences = sorted(influences, key=lambda inf: inf.split("|")[-1].upper())

        layout = QVBoxLayout(self)
        filter_layout = QHBoxLayout()
        self.all_but = QPushButton("All")
        self.nearby_but = QPushButton("Selected")
        self.all_but.setCheckable(True)
        self.nearby_but.setCheckable(True)
        self.all_but.setChecked(True)
        self.nearby_but.setEnabled(bool(self.nearby_names))
        filter_layout.addWidget(self.all_but)
        filter_layout.addWidget(self.nearby_but)
        layout.addLayout(filter_layout)

        self.list_widget = QListWidget(self)
        self.list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        layout.addWidget(self.list_widget)
        self.populate_list()

        button_layout = QHBoxLayout()
        button_layout.addStretch()
        self.apply_but = QPushButton("Apply")
        self.ok_but = QPushButton("OK")
        self.cancel_but = QPushButton("Cancel")
        button_layout.addWidget(self.apply_but)
        button_layout.addWidget(self.ok_but)
        button_layout.addWidget(self.cancel_but)
        layout.addLayout(button_layout)

        self.all_but.clicked.connect(lambda: self.set_filter_mode(False))
        self.nearby_but.clicked.connect(lambda: self.set_filter_mode(True))
        self.apply_but.clicked.connect(self.apply_selection)
        self.ok_but.clicked.connect(self.accept)
        self.cancel_but.clicked.connect(self.reject)
        self.list_widget.itemDoubleClicked.connect(lambda item: self.accept())

    def set_filter_mode(self, nearby_only):
        self.all_but.setChecked(not nearby_only)
        self.nearby_but.setChecked(nearby_only)
        self.populate_list(nearby_only=nearby_only)

    def populate_list(self, nearby_only=False):
        selected_set = set(self.selected_names)
        selected_set.update(self.get_selected_names())
        self.selected_names = list(selected_set)
        self.list_widget.clear()
        for influence in self.influences:
            short_name = influence.split("|")[-1]
            if nearby_only and short_name not in self.nearby_names:
                continue
            item = QListWidgetItem(short_name)
            item.setData(Qt.UserRole, short_name)
            item.setToolTip(influence)
            self.list_widget.addItem(item)
            if short_name in selected_set:
                item.setSelected(True)

    def get_selected_names(self):
        return [item.data(Qt.UserRole) for item in self.list_widget.selectedItems()]

    def apply_selection(self):
        parent = self.parent()
        if parent and hasattr(parent, "append_joint_names_to_search"):
            parent.append_joint_names_to_search(self.get_selected_names())


class EditorSpinbox(QSpinBox):
    """フォーカス時に数値を全選択するカスタム整数スピンボックス。"""

    def __init__(self, parent=None):
        super(EditorSpinbox, self).__init__(parent)
        self.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.FocusIn:
            self.sel_all_input()
        return False

    def sel_all_input(self):
        cmds.evalDeferred(self.select_box_all)

    def select_box_all(self):
        try:
            self.selectAll()
        except Exception:
            pass


class PopInputBox(qt.SubWindow):
    """セル右クリック・直接キー入力時にカーソル位置へ表示する小型入力ウィンドウ。"""

    closed = Signal()

    def __init__(self, parent=None, value=0.0, mode=0, direct=False, pos=None):
        super(PopInputBox, self).__init__(parent)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)

        if direct:
            self.input = qt.LineEdit(self)
            self.setCentralWidget(self.input)
            self.input.setText(value)
        else:
            self.input = QDoubleSpinBox(self)
            self.input.setButtonSymbols(QAbstractSpinBox.NoButtons)
            self.setCentralWidget(self.input)

            max_wt = state.MAXIMUM_WEIGHT
            if mode == 0:
                self.input.setDecimals(state.FLOAT_DECIMALS)
                self.input.setRange(0, max_wt * 10)
                self.input.setValue(value)
            elif mode == 1:
                self.input.setDecimals(state.FLOAT_DECIMALS)
                self.input.setRange(-1 * max_wt * 10, max_wt * 10)
                self.input.setValue(value)
            elif mode == 2:
                self.input.setDecimals(1)
                self.input.setRange(-999, 999)
            self.input.selectAll()

        pos = QCursor.pos()
        self.resize(50, 24)
        self.move(pos.x() - 20, pos.y() - 12)

        self.input.editingFinished.connect(self.close)
        self.show()

        self.activateWindow()
        self.raise_()
        self.input.setFocus()

    def closeEvent(self, e):
        self.closed.emit()


class RightClickTableView(QTableView):
    """右クリックおよび直接キー入力シグナルを発行するテーブルビュー。"""

    rightClicked = Signal()
    keyPressed = Signal(str)
    ignore_key_input = False

    def mouseReleaseEvent(self, e):
        self.mouse_pos = QCursor.pos()
        if e.button() == Qt.RightButton:
            self.rightClicked.emit()
        else:
            super(RightClickTableView, self).mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        self.keyPressed.emit(e.text())
        if self.ignore_key_input:
            return
        super(RightClickTableView, self).keyPressEvent(e)


class MyHeaderView(QHeaderView):
    """ボーン名を 90 度縦書き表示するカスタム水平ヘッダー。"""

    doubleClicked = Signal(int)
    rightClicked = Signal()
    active_section_list = []

    def __init__(self, parent=None):
        super(MyHeaderView, self).__init__(Qt.Horizontal, parent)
        self._font = QFont("helvetica", 9)
        self._actived_font = QFont("helvetica", 9)
        self._actived_font.setWeight(QFont.DemiBold)
        self._metrics = QFontMetrics(self._font)
        self._descent = self._metrics.descent()
        self._margin = 5
        try:
            self.setClickable(True)
        except Exception:
            self.setSectionsClickable(True)
        self.setHighlightSections(True)
        self.sectionDoubleClicked.connect(self.double_clicked_section)

    def mouseReleaseEvent(self, e):
        if state.WINDOW is not None and hasattr(state.WINDOW, "view_widget"):
            state.WINDOW.view_widget.mouse_pos = QCursor.pos()
        if e.button() == Qt.RightButton:
            self.rightClicked.emit()
        else:
            super(MyHeaderView, self).mouseReleaseEvent(e)

    def double_clicked_section(self, e):
        self.doubleClicked.emit(e)

    def clicked_section(self, e):
        print(" clicked_section", e)

    def section_enter(self, e):
        print("section_enter", e)

    def paintSection(self, painter, rect, index):
        painter.rotate(-90)
        data = self._get_data(index)
        data = data.split(":")[-1]
        is_active = self.check_section_is_active(index)

        font = self._actived_font if is_active else self._font
        painter.setFont(font)

        color = self.model().headerData(index, Qt.Horizontal, Qt.BackgroundRole)
        dark_color = QColor(*map(lambda c: c - 42, color.getRgb()[:3]))
        draw_rect = self.rotate_rect(index, rect)
        painter.fillRect(draw_rect, color)

        painter.setPen(QPen(dark_color))
        painter.drawRect(draw_rect)

        painter.setPen(QColor(*[230] * 3))
        tx = int(-rect.height() + self._margin)
        ty = int(rect.left() + (rect.width() + self._descent) / 2.0)
        painter.drawText(tx, ty, data)

    def check_section_is_active(self, index):
        sel_model = self.selectionModel()
        if MAYA_VER >= 2022:
            return sel_model.columnIntersectsSelection(index)
        selected_item = sel_model.currentIndex()
        return sel_model.columnIntersectsSelection(index, selected_item)

    def rotate_rect(self, index, rect):
        offset = 1 if index == 0 else 0
        rect = rect.getRect()
        rect = [rect[1] - rect[3] + 1, rect[0] - 1 + offset, rect[3] - 1, rect[2] - offset]
        return QRect(*rect)

    def sizeHint(self):
        return QSize(0, int(self._get_text_width() + 2 * self._margin))

    def _get_text_width(self):
        if self.model() is None:
            return 0
        if MAYA_VER >= 2025:
            width_list = [
                self._metrics.horizontalAdvance(self._get_data(i))
                for i in range(0, self.model().columnCount())
            ] or [0]
        else:
            width_list = [
                self._metrics.width(self._get_data(i))
                for i in range(0, self.model().columnCount())
            ] or [0]
        return max(width_list) * 1.1

    def _get_data(self, index):
        return self.model().headerData(index, self.orientation(), 0)


def set_table_header_width(view, model, index=None, space=55, max=55, add=0):
    """QTableView の各カラム幅を表示単位と桁数に合わせて設定する。"""
    max += add

    def __resize_main(col_index):
        width = space + add
        if state.MAXIMUM_WEIGHT == 1.0:
            width -= 7
        if width > max:
            width = max
        view.setColumnWidth(col_index, int(width))

    if index is None:
        count = model.columnCount()
        for i in range(count):
            __resize_main(i)
    else:
        __resize_main(index)


class TableModel(QAbstractTableModel):
    """頂点×インフルエンスのウェイト一覧を描画・編集するテーブルモデル。"""

    norm = False

    def __init__(self, data, parent=None, mesh_rows=[], influences=[],
                 v_header_list=[], header_color_list=[]):
        super(TableModel, self).__init__(parent)
        self.under_weight_rows = set()
        self.over_weight_rows = set()
        self.over_influence_limit_dict = {}
        self.over_influence_limit_rows = set()
        self.none_cells = []
        self.v_header_list = v_header_list
        self.mesh_rows = mesh_rows
        self.weight_lock_cells = set()
        self.header_list = influences
        self.header_color_list = header_color_list
        self.copy_cell_list = []
        self.copy_vertex_rows = []
        self._data = data

    def headerData(self, id, orientation, role):
        """見出しラベルおよび背景色・配置を返す。"""
        if orientation == Qt.Horizontal:
            if id >= len(self.header_list):
                return None
            if role == Qt.DisplayRole:
                return self.header_list[id].split("|")[-1]
            elif role == Qt.BackgroundRole:
                color_id = self.header_color_list[id]
                return QColor(*self.joint_color_list[color_id])

        if orientation == Qt.Vertical:
            if role == Qt.DisplayRole:
                return self.v_header_list[id]
            elif role == Qt.BackgroundRole:
                max_inf = state.MAXIMUM_INFLUENCE_COUNT
                if id in self.mesh_rows:
                    end_of_mesh_rows = self.mesh_rows[self.mesh_rows.index(id) + 1]
                    mesh_rows = set(range(id + 1, end_of_mesh_rows))
                    if mesh_rows & self.under_weight_rows:
                        return QColor(*self.under_weight_color)
                    if mesh_rows & self.over_weight_rows:
                        return QColor(*self.over_weight_color)
                    for mrow in mesh_rows:
                        if self.over_influence_limit_dict[mrow] > max_inf > 0:
                            return QColor(*self.over_influence_color)

                if id in self.under_weight_rows:
                    return QColor(*self.under_weight_color)
                if id in self.over_weight_rows:
                    return QColor(*self.over_weight_color)
                try:
                    if self.over_influence_limit_dict[id] > max_inf > 0:
                        return QColor(*self.over_influence_color)
                except Exception:
                    pass
                return QColor(*self.v_header_bg)
            elif role == Qt.TextAlignmentRole:
                return Qt.AlignRight
        return None

    def rowCount(self, parent=None):
        return len(self._data)

    def columnCount(self, parent=None):
        return len(self._data[0]) - 1 if self.rowCount() else 0

    joint_color_list = [
        [161, 105, 48],
        [159, 161, 48],
        [104, 161, 48],
        [48, 161, 93],
        [48, 161, 161],
        [48, 103, 161],
        [111, 48, 161],
        [161, 48, 105],
    ]
    v_header_text = [220] * 3
    v_header_bg = [100] * 3
    under_weight_color = [230, 54, 54]
    over_weight_color = [210, 116, 32]
    over_influence_color = [210, 190, 40]
    lock_weight_bg_color = [100] * 3
    lock_weight_text_color = [160] * 3
    cell_copy_color = [90, 120, 90]
    vertex_copy_color = [90, 90, 120]
    cell_vertex_copy_color = [70, 120, 120]

    def data(self, index, role=Qt.DisplayRole):
        row = index.row()
        column = index.column()
        win = state.WINDOW
        if role == Qt.DisplayRole:
            try:
                node = win.vtx_row_dict[row][6]
                inf_id_list = win.node_influence_id_list_dict[node]
                local_column = inf_id_list[column]
                weight = self._data[row][local_column] * state.MAXIMUM_WEIGHT
                return state.FLOAT_FORMAT.format(weight)
            except Exception:
                return None
        elif role == Qt.BackgroundRole:
            if row in self.mesh_rows:
                color_id = self.header_color_list[column]
                return QColor(*self.joint_color_list[color_id])
            if row in self.copy_vertex_rows and index in self.copy_cell_list:
                return QColor(*self.cell_vertex_copy_color)
            if row in self.copy_vertex_rows:
                return QColor(*self.vertex_copy_color)
            if index in self.copy_cell_list:
                return QColor(*self.cell_copy_color)
            if (row, column) in self.weight_lock_cells:
                return QColor(*self.lock_weight_bg_color)
        elif role == Qt.ForegroundRole:
            zero_flag = False
            color = [210] * 3
            try:
                node = win.vtx_row_dict[row][6]
                inf_id_list = win.node_influence_id_list_dict[node]
                local_column = inf_id_list[column]
                weight = self._data[row][local_column] * state.MAXIMUM_WEIGHT
                if weight == 0.0:
                    zero_flag = True
            except Exception:
                pass

            if (row, column) in self.weight_lock_cells:
                color = self.lock_weight_text_color
            else:
                if row in self.under_weight_rows:
                    color = self.under_weight_color
                elif row in self.over_weight_rows:
                    color = self.over_weight_color
                else:
                    try:
                        if self.over_influence_limit_dict[row] > state.MAXIMUM_INFLUENCE_COUNT > 0:
                            color = self.over_influence_color
                    except Exception:
                        pass
                if zero_flag:
                    darken = state.ZERO_CELL_DARKEN
                    color = [min([c - darken, 255]) if c - darken > 43 else 43 for c in color]

            return QColor(*color)
        elif role == Qt.TextAlignmentRole:
            return Qt.AlignRight

    def get_data(self, index=None, row=0, column=0):
        try:
            if index:
                row = index.row()
                column = index.column()
            win = state.WINDOW
            node = win.vtx_row_dict[row][6]
            inf_id_list = win.node_influence_id_list_dict[node]
            local_column = inf_id_list[column]
            value = self._data[row][local_column]
        except Exception:
            value = None
        return value

    def setData(self, index, value, role=Qt.EditRole):
        if not isinstance(index, tuple):
            if not index.isValid() or not 0 <= index.row() < len(self._data):
                print("can not set data : retrun")
                return
            row = index.row()
            column = index.column()
        else:
            row = index[0]
            column = index[1]
        if role == Qt.EditRole and value != "":
            win = state.WINDOW
            node = win.vtx_row_dict[row][6]
            inf_id_list = win.node_influence_id_list_dict[node]
            local_column = inf_id_list[column]
            self._data[row][local_column] = value
            model_index = self.index(row, column)
            self.dataChanged.emit(model_index, model_index)
            return True
        return False

    def flags(self, index):
        row = index.row()
        column = index.column()
        try:
            win = state.WINDOW
            node = win.vtx_row_dict[row][6]
            inf_id_list = win.node_influence_id_list_dict[node]
            local_column = inf_id_list[column]
            value = self._data[row][local_column]
        except Exception:
            value = None
        if row in self.mesh_rows or value is None:
            return Qt.ItemIsEnabled
        return Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable


def model_iter(model, parent_index=QModelIndex(), col_iter=True):
    """テーブルモデルの全アイテムインデックスを順に返すジェネレータ。"""
    index = model.index(0, 0, parent_index)

    while True:
        if col_iter:
            for col in range(0, model.columnCount(parent_index)):
                yield index.sibling(index.row(), col)
        else:
            yield index

        if model.rowCount(index) > 0:
            for item in model_iter(model, index, col_iter):
                yield item

        index = index.sibling(index.row() + 1, index.column())
        if not index.isValid():
            break

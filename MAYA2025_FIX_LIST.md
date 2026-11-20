# Maya 2025 以降 互換性チェック・修正リスト

`README.md` に記載されている全機能を対象に、**Maya 2025 以降（Python 3.11+ / PySide6・Qt6 / Maya 2022+ デフォーマ仕様）** および **Maya 2026 以降（Python 3.12+）** での互換性・不具合を検証し、実施した修正内容をまとめた一覧です。

---

## AIネイティブ開発 (AI-Powered Development)

本フォークは、**最先端AIアシスタント（Antigravity / Gemini / Claude）とのペアプログラミング**により改良されています。

---

## README 機能別 修正サマリー

| README の機能項目                                        | 修正前の問題                                                                                                                     |                                        対応状況                                        |
| :------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------: | ------ |
| **インストール（ApplicationPlugins / 起動）**            | `PackageContents.xml` の `<Components>` が `win64` のみ／Qt6 非推奨属性 `AA_EnableHighDpiScaling` 呼び出し                       |                                         修正済                                         |
| **基本 UI 表示 2（0-1 / 0-100）**                        | `QButtonGroup.button(int)` へ `float` を渡していた                                                                               |                                         修正済                                         |
| **ジョイント選択（ヘッダー右クリック）**                 | `original_selection` の `None` 参照・列外クリック時の範囲外アクセス                                                              |                                         修正済                                         |
| **入力方法 4 種類（スピンボックス / スライダー等）**     | Qt6 廃止済み `QApplication.setActiveWindow()`・`QWheelEvent.delta()` 呼び出しでクラッシュ                                        |                                         修正済                                         |
| **入力モード / 検索 UI**                                 | `self.add_but`・`self.clear_search_but` の属性名重複上書き                                                                       |                                         修正済                                         |
| **ウェイトロック（Lock Wt / Unlock Wt 右クリック）**     | Maya 2022+ の `columnIntersectsSelection` 引数仕様変更未対応で全頂点ロックが機能しない                                           |                                         修正済                                         |
| **スムース設定ウィンドウ（SS ボタン）**                  | `QSlider.setValue(float)` で起動時に `TypeError` クラッシュ＋ホイール操作でクラッシュ                                            |                                         修正済                                         |
| **Freeze / Freeze_M**                                    | Maya 2022+ クラスター（`componentTags`）で `IndexError`、子無しノードで `TypeError`、未スキニング時のシェイプ誤削除リスク        |                                         修正済                                         |
| **Simple Weight Copy / Paste**                           | アトリビュート名に余計な半角スペース（`' .skm'` 等）、`protect_pat` タイポ、`xml_name` 未定義リスク                              |                                         修正済                                         |
| **Transfer Weight Multiple**                             | コンポーネント選択が頂点（`float3`）のみ対象、`cmds.skinCluster` にトランスフォーム渡し、`WINDOW` 未チェック                     |                                         修正済                                         |
| **Vertex Weight Copy / Paste**                           | `cmds.skinCluster` にトランスフォーム渡し、右クリックメニューが `exec_()` でクラッシュ                                           |                                         修正済                                         |
| **Excel like Cell Copy / Paste（右クリックメニュー）**   | PySide6 廃止済み `QMenu.exec_()` 呼び出しで確実にクラッシュ                                                                      |                                         修正済                                         |
| **Weight_Symmetrize / Auto_Symmetry / 命名規則エディタ** | 未使用の `import imp` による Python 3.12+ 起動エラー、`_R` 末尾リネームのタイポ、ブレンドシェイプ併用時の `skinCluster` 取得失敗 |                                         修正済                                         |
| **Mesh Marge with Skinning**                             | フルパス記号 `                                                                                                                   | `付きで`cmds.rename`して`RuntimeError`、`qt.Callback` 呼び出しミスで Undo チャンク無効 | 修正済 |
| **Toggle Mute Skinning**                                 | 子無しメッシュ選択時に `cmds.ls(None, type='transform')` でシーン全メッシュがミュートされる                                      |                                         修正済                                         |
| **Go Maya Export / Import**                              | `.json` ファイルを `.ma` として読み込もうとして確実にエラー                                                                      |                                         修正済                                         |
| **Add / Remove / Remove Unused Influences**              | `weighted_influences` が `None` で `TypeError`、`cmds.skinCluster` へのノード直接渡し                                            |                                         修正済                                         |

---

## 修正詳細リスト（深刻度別）

### 深刻度：高（クラッシュ・致命的誤動作）

- **セル右クリックメニューの PySide6 対応**
  - 対象：`Contents/scripts/siweighteditor_improve/editor/weight_ops.py`
  - 内容：`copy_menus.exec_(cursor)` を `getattr(copy_menus, 'exec_', None) or getattr(copy_menus, 'exec')` に変更し、PySide6（Qt6）での `AttributeError` を解消。

- **スムース設定ウィンドウの `QSlider.setValue` 型エラーおよびホイール操作エラー修正**
  - 対象：`Contents/scripts/siweighteditor_improve/smooth_setting_editor.py`、`Contents/scripts/siweighteditor_improve/qt.py`
  - 内容：`tolerance_sld.setValue(...)` への引数を `int(round(...))` へ明示変換。`qt.EditorDoubleSpinbox` の `event.delta()` を `event.angleDelta().y()`（旧バージョンフォールバック・0 除算ガード付き）へ修正。

- **ウェイト計算時の廃止済み `QApplication.setActiveWindow` を置換**
  - 対象：`Contents/scripts/siweighteditor_improve/editor/weight_ops.py`
  - 内容：Qt6 で削除された `QApplication.setActiveWindow(self)` を `self.activateWindow()` へ変更。

- **Lock Wt / Unlock Wt 右クリック（全頂点インフルエンスロック・アンロック）修正**
  - 対象：`Contents/scripts/siweighteditor_improve/editor/weight_ops.py`
  - 内容：`_is_column_selected()` を追加し、Maya 2022 以降では `columnIntersectsSelection(index)`（1 引数）を使用するよう統一。`unlock_all_rows()` で `section_ids` に整数を渡していた不整合も解消。

- **Mesh Marge with Skinning のリネームエラーおよび Undo チャンク修正**
  - 対象：`Contents/scripts/siweighteditor_improve/modeling.py`
  - 内容：`cmds.rename(target_mesh, objects[0].split('|')[-1])` へ変更しフルパス記号 `|` による `RuntimeError` を解消。`qt.Callback(self.marge_run())` を `qt.Callback(self.marge_run)()` に修正し Undo チャンクを有効化。

- **Freeze / Freeze_M の Maya 2022+ `componentTags` 対応およびシェイプ保護**
  - 対象：`Contents/scripts/siweighteditor_improve/modeling.py`、`Contents/scripts/siweighteditor_improve/freeze.py`
  - 内容：
    - `ClusterCopy` で `objectSet` が存在しない Maya 2022+ の `componentTags` ベースクラスターに対し、`cmds.deformer(cls, q=True, components=True)` および頂点ウェイト走査フォールバックを追加。
    - `freeze()` の `cmds.listRelatives(sel, ad=True) or []` に `None` ガードを追加。
    - `deleteZeroShape()` で唯一の可視シェイプを削除しないよう保護し、未スキニングメッシュへの `Freeze_M` 実行時の消失を防止。
    - `store_blend_shape()` / `set_blend_shape()` / `repareDagSetMember()` に `None`・未初期化変数ガードを追加。

- **Toggle Mute Skinning のシーン全体ミュート誤動作修正**
  - 対象：`Contents/scripts/siweighteditor_improve/weight.py`
  - 内容：`cmds.listRelatives(node, ad=True, f=True) or []` が空のときは `cmds.ls` を呼ばないよう修正し、選択メッシュのみを正しくトグルするよう改善。

- **Go Maya Import の `.json` 誤読込エラー修正**
  - 対象：`Contents/scripts/siweighteditor_improve/go.py`
  - 内容：インポート対象を `.ma` 拡張子のファイルのみに絞り込み、空フォルダ時の判定およびマテリアルリネーム時の例外保護を追加。

- **Maya 2026 以降（Python 3.12+）向けの `imp` モジュール削除対応**
  - 対象：`Contents/scripts/siweighteditor_improve/joint_rule_editor.py`
  - 内容：Python 3.12 で廃止された未使用の `import imp` を削除し、`load_data()` の `dict_keys` / `dict_values` を `list` 化。

---

### 深刻度：中（条件付きエラー・一部機能の不具合）

- **PySide6 向けの `int` 明示キャスト追加**
  - 対象：`Contents/scripts/siweighteditor_improve/editor/ui_builder.py`、`Contents/scripts/siweighteditor_improve/editor/widgets.py`
  - 内容：`self.pre_max_id`、`MyHeaderView.paintSection()` の `drawText` 座標、および `MyHeaderView.sizeHint()` の高さをすべて `int(...)` に変換。

- **Simple Weight Copy / Paste・Transfer Weight のアトリビュート名修正と保護強化**
  - 対象：`Contents/scripts/siweighteditor_improve/weight.py`
  - 内容：`srcSkinCluster + ' .skm'` 等の不要スペースを削除、`self.protect_pat` を `self.protect_path` へ修正、`weightPaste()` の `xml_name` 未定義対策と `skinCluster` 直接指定を追加。

- **Symmetry 機能のリネームタイポ修正とスキンクラスタ取得改善**
  - 対象：`Contents/scripts/siweighteditor_improve/symmetrize.py`、`Contents/scripts/siweighteditor_improve/weight.py`
  - 内容：`renameLR()` の `startswith('_R')` を `endswith('_R')` へ修正。`symmetry_weight()` の `dstSkinCluster` 取得を `cmds.listHistory(dstNode)` 経由に変更し、ブレンドシェイプ併用時でも動作するよう改善。`WeightSymmetrize` に空頂点ガードを追加。

- **インフルエンス操作・Transfer Weight Multiple の安定化**
  - 対象：`Contents/scripts/siweighteditor_improve/editor/weight_ops.py`、`Contents/scripts/siweighteditor_improve/weight_transfer_multiple.py`
  - 内容：`cmds.skinCluster` へトランスフォーム名ではなく `skinCluster` ノード名を渡すよう統一。`remove_unused_influences()` の `None` ガードと全インフルエンス誤削除防止を追加。`Transfer Weight Multiple` でエッジ・フェース選択もコンポーネント転送対象として認識できるよう拡張。

- **ヘッダー右クリック（ジョイント選択）の境界チェック追加**
  - 対象：`Contents/scripts/siweighteditor_improve/editor/view_controller.py`
  - 内容：`original_selection` の `None` ガード、列外クリック（`column < 0` 等）およびノード存在チェックを追加。

- **その他ユーティリティのバグ修正**
  - 対象：`Contents/scripts/siweighteditor_improve/modeling.py`、`Contents/scripts/siweighteditor_improve/common.py`
  - 内容：`duplicate_with_skin()` の未定義クラス `SimpleWeightCopyPaste` を `WeightCopyPaste` へ修正。`search_polygon_mesh()` の `nodes` 未初期化エラーを解消。

---

### 深刻度：低（非推奨 API・軽微な整理）

- **Qt6 非推奨属性のバージョン分岐**
  - 対象：`Contents/scripts/siweighteditor_improve/siweighteditor.py`
  - 内容：`AA_EnableHighDpiScaling` の設定を `MAYA_VER < 2025` のみ実行するよう分岐。

- **`TableModel.setData` のシグナル引数修正**
  - 対象：`Contents/scripts/siweighteditor_improve/editor/widgets.py`
  - 内容：`dataChanged.emit` にタプルではなく `QModelIndex` を渡すよう修正。

- **UI 属性名重複およびタイポの整理**
  - 対象：`Contents/scripts/siweighteditor_improve/editor/ui_builder.py`、`Contents/scripts/siweighteditor_improve/editor/weight_ops.py`
  - 内容：検索側の `self.add_but` を `self.search_add_but`、スライダー側の `self.clear_search_but` を `self.clear_selection_but` に分離。`precalculate_bake_weights()` の `round is not None` を `round_digit is not None` に修正。

- **PackageContents.xml の OS 指定統一**
  - 対象：`PackageContents.xml`
  - 内容：`<Components>` の `OS` 属性を `win64|macOS|linux` に統一。

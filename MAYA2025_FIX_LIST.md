# Maya 2025 以降 互換性チェック・修正リスト

`README.md` に記載されている全機能を対象に、**Maya 2025 以降（Python 3.11+ / PySide6・Qt6 / Maya 2022+ デフォーマ仕様）** および **Maya 2026 以降（Python 3.12+）** での互換性・不具合を検証し、実施した修正内容をまとめた一覧です。

---

## AIネイティブ開発 (AI-Powered Development)

本フォークは、**最先端AIアシスタント（Antigravity / Gemini / Claude）とのペアプログラミング**により改良されています。

---

## README 機能別 修正サマリー

| README の機能項目 | 修正前の問題 | 対応状況 |
| :--- | :--- | :---: |
| **インストール（ApplicationPlugins / 起動）** | `PackageContents.xml` の `<Components>` が `win64` のみ／Qt6 非推奨属性 `AA_EnableHighDpiScaling` 呼び出し | 修正済 |
| **基本 UI 表示 2（0-1 / 0-100）** | `QButtonGroup.button(int)` へ `float` を渡していた | 修正済 |
| **ジョイント選択（ヘッダー右クリック）** | `original_selection` の `None` 参照・列外クリック時の範囲外アクセス | 修正済 |
| **入力方法 4 種類（スピンボックス / スライダー等）** | Qt6 廃止済み `QApplication.setActiveWindow()`・`QWheelEvent.delta()` 呼び出しでクラッシュ | 修正済 |
| **入力モード / 検索 UI** | `self.add_but`・`self.clear_search_but` の属性名重複上書き | 修正済 |
| **ウェイトロック（Lock Wt / Unlock Wt 右クリック）** | Maya 2022+ の `columnIntersectsSelection` 引数仕様変更未対応で全頂点ロックが機能しない | 修正済 |
| **スムース設定ウィンドウ（SS ボタン）** | `QSlider.setValue(float)` で起動時に `TypeError` クラッシュ＋ホイール操作でクラッシュ | 修正済 |
| **Freeze / Freeze_M** | Maya 2022+ クラスター（`componentTags`）で `IndexError`、子無しノードで `TypeError`、未スキニング時のシェイプ誤削除リスク | 修正済 |
| **Simple Weight Copy / Paste** | アトリビュート名に余計な半角スペース（`' .skm'` 等）、`protect_pat` タイポ、`xml_name` 未定義リスク | 修正済 |
| **Transfer Weight Multiple** | コンポーネント選択が頂点（`float3`）のみ対象、`cmds.skinCluster` にトランスフォーム渡し、`WINDOW` 未チェック | 修正済 |
| **Vertex Weight Copy / Paste** | `cmds.skinCluster` にトランスフォーム渡し、右クリックメニューが `exec_()` でクラッシュ | 修正済 |
| **Excel like Cell Copy / Paste（右クリックメニュー）** | PySide6 廃止済み `QMenu.exec_()` 呼び出しで確実にクラッシュ | 修正済 |
| **Weight_Symmetrize / Auto_Symmetry / 命名規則エディタ** | 未使用の `import imp` による Python 3.12+ 起動エラー、`_R` 末尾リネームのタイポ、ブレンドシェイプ併用時の `skinCluster` 取得失敗 | 修正済 |
| **Mesh Marge with Skinning** | フルパス記号（パイプ記号）付きで `cmds.rename` して `RuntimeError`、`qt.Callback` 呼び出しミスで Undo チャンク無効 | 修正済 |
| **Toggle Mute Skinning** | 子無しメッシュ選択時に `cmds.ls(None, type='transform')` でシーン全メッシュがミュートされる | 修正済 |
| **Go Maya Export / Import** | `.json` ファイルを `.ma` として読み込もうとして確実にエラー | 修正済 |
| **Add / Remove / Remove Unused Influences** | `weighted_influences` が `None` で `TypeError`、`cmds.skinCluster` へのノード直接渡し | 修正済 |

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

---

## Improve 版 機能拡張・不具合修正の詳細リスト

Maya 2025 互換化に続き、実務運用で発生した不具合の解消および操作性・安全性向上のために実施した追加機能・改修の詳細です。

### サマリー一覧

| カテゴリ | 主な対応内容 | 対象ファイル |
| :--- | :--- | :--- |
| **ウィンドウ制御・起動安定化** | Maya 背面潜り込み防止（Qt + Win32 オーナー固定）、起動時の二重開閉・チラつき解消、`ApplicationPlugins` 配置互換＆二重初期化ガード | `qt.py`, `editor/state.py`, `editor/widgets.py`, `siweighteditor.py`, `userSetup.py`, `startup.py` |
| **Undo / スライダー刷新** | 1 ドラッグ = 1 Undo 化、`Add`/`Add%` 基準計算修正、Undo 破壊・閉鎖後エラー・履歴汚染・Undo 無効化リーク解消 | `bake_skin_weight_improve.py`, `qt.py`, `siweighteditor.py`, `editor/*`, `modeling.py`, `weight_transfer_multiple.py` |
| **正規化警告の抑止** | `Specified weights required modification for normalization.` の警告出力を Undo 非汚染で抑止 | `bake_skin_weight_improve.py`, `tools/rig_cleanup_helper.py`, `tools/model_edit_window.py` |
| **モデル修正タブの安全性向上** | 0 フレームの回転 XYZ 不正値混入バグ修正、記憶ジョイントの `[キャラ名]_Hips` 確認ダイアログ＆注意表示 | `tools/model_edit_window.py` |
| **選択ボーン回転フリーズ** | 現在回転を `jointOrient` へ焼き込んで 0F = 0 に固定、選択ボーンと全子ボーンのキー退避＆書き戻し（0F 以降は元の値をそのまま復元） | `tools/rig_cleanup_helper.py`, `tools/model_edit_window.py` |
| **バインド / ミラー＆コピー新設** | ボーンの安全バインド、高精度ミラー＆コピー、ウェイト無しメッシュ一覧バインド専用ウィンドウ（非表示オブジェクト除外） | `tools/rig_cleanup_helper.py`, `tools/weight_mirror_copy_helper.py`, `tools/unweighted_bind_window.py`, `tools/model_edit_window.py` |
| **ジョイント検索 / ピッカー** | 非モーダル検索ウィンドウ（リアルタイム絞り込み・範囲／左右フィルタ・統計表示・厳密一致・シーン選択同期）および人型／実スケルトン 2D ボーンピッカー連携 | `editor/widgets.py`, `editor/view_controller.py`, `tools/bone_picker_window.py` |
| **ツールバー / Remap UI 改善** | 追加機能ボタンを `+View`（表示系）と `+Win`（ウィンドウ系）へ分離・色分け、Bone Weight Remap 上部ガイド追加 | `editor/ui_builder.py`, `tools/bone_remap_window.py` |

---

### 1. ウィンドウ背面潜り込み防止・1 回起動化・`ApplicationPlugins` 配置互換

- **Maya 本体より後ろへ潜り込む現象の解消**
  - 対象：`Contents/scripts/siweighteditor_improve/qt.py`、`Contents/scripts/siweighteditor_improve/editor/state.py`、`Contents/scripts/siweighteditor_improve/editor/widgets.py`
  - 原因：
    - `qt.SubWindow` / `qt.MainWindow` が引数 `parent` を無視し、モジュール読み込み時にキャッシュした `MAYA_WIDNOW` を使っていたため、初期化タイミング次第で親なしトップレベルウィンドウになっていた。
    - Windows + Maya 2025（PySide6 / Qt6）では、`MayaQWidgetDockableMixin.show(dockable=False)` 等の処理経由で OS 上のオーナー関係が外れ、Maya クリック時にウィンドウが背面へ回ることがあった。
  - 修正内容：
    - `qt.attach_maya_window_owner(widget)` を新設。Qt の `windowHandle().setTransientParent(maya_win.windowHandle())` に加え、Windows 環境では `ctypes` 経由で 64bit 安全に Win32 `SetWindowLongPtrW(..., GWLP_HWNDPARENT, maya_hwnd)` を適用（非ドッキング時のみ）。他アプリ切替時は自然に背面へ回りつつ、Maya より後ろには潜り込まない動作を保証。
    - `MainWindow` / `SubWindow` / `DockWindow` / `JointSearchDialog` がコンストラクタで毎回動的に `get_maya_window()` を親として取得し、`showEvent()` で自動的にオーナー固定を適用するよう統一（メインウィンドウ・`Model Edit`・`Remap`・`ウェイト無し一覧バインド`・`Smooth Setting`・`Joint Rules`・`Joints Search` など全ウィンドウへ適用）。
    - `state.ensure_maya_owned_floating_window()` からウィンドウ再生成を伴う `setParent()` → `show()` を廃止し、`qt.attach_maya_window_owner()` のみを行うよう変更。

- **起動時の 2 回以上の開閉・チラつき解消**
  - 対象：`Contents/scripts/siweighteditor_improve/siweighteditor.py`
  - 原因：
    - `Option()` が `dockable` 設定を読むためだけに `make_ui()` でメインウィンドウを丸ごと 1 回生成し、`dockable=False` を確認した直後に `close()` で破棄してから `OptionWindow()` で 2 回目を生成していた。
    - `OptionWindow()` 内でも 16 回連続のリサイズループ（`range(0, 800, 50)`）と `show()` 後の `setParent()` 再表示を行っていた。
  - 修正内容：
    - ウィンドウ生成前に設定 JSON から `dockable` フラグだけを読む `_load_saved_dockable_flag()` と既存ウィンドウをまとめて閉じる `_close_existing_windows()` を追加。
    - 非ドッキング時はダミーウィンドウを生成せず `OptionWindow()` を 1 回のみ実行。`show()` 前に位置・サイズ・ボタン再配置を 1 回だけ済ませ、`QMainWindow.show(WINDOW)` を直接 1 回だけ呼ぶ構成へ刷新。

- **従来の `C:\Program Files\Autodesk\ApplicationPlugins` 配置方式の維持と安定化**
  - 対象：`PackageContents.xml`、`Contents/scripts/userSetup.py`、`Contents/scripts/siweighteditor_improve/startup.py`
  - 内容：
    - `PackageContents.xml`（`SupportPath="./Contents"` / `ComponentEntry ModuleName="./scripts/userSetup.py"`）による従来の `ApplicationPlugins` 配置方式をそのまま維持。
    - `PackageContents.xml` と `PYTHONPATH` 経由で `userSetup.py` が二重に呼ばれるケースに備え、`SIWE_IMPROVE_USERSETUP_REGISTERED` 環境変数ガードおよび `startup._STARTUP_EXECUTED` フラグによる二重初期化防止を追加。
    - `startup.jpn()` を Python 3 で例外を起こさない実装へ整理。
    - 設定ファイル・バックアップ JSON・ログの書き込み先がすべて `MAYA_APP_DIR` / `TEMP` 配下であり、読み取り専用の `C:\Program Files` 配下へ書き込まないことを確認済み。

---

### 2. Undo 機能・スライダー調整アーキテクチャの全面改修

- **スライダードラッグの「ライブプレビュー＋リリース時 1 回確定」化**
  - 対象：`Contents/plug-ins/bake_skin_weight_improve.py`、`Contents/scripts/siweighteditor_improve/editor/widgets.py`、`Contents/scripts/siweighteditor_improve/editor/weight_ops.py`、`Contents/scripts/siweighteditor_improve/editor/ui_builder.py`
  - 内容：
    - `BakeSkinWeightClass.isUndoable()` が `ignore_undo` フラグを正しく反映するよう修正し、ドラッグ中は `ignore_undo=True` でライブプレビュー、リリース時のみドラッグ開始前スナップショットを基準に 1 回だけ Undo 登録する設計へ刷新。
    - ドラッグ中に値が変わらなかった場合は Undo 履歴を積まないよう最適化し、ドラッグ中の未閉鎖 Undo チャンク残留も解消。
    - `Add` / `Add%` モードをドラッグ開始時のウェイト基準で計算する方式へ修正し、`Add%` の指数的暴走・リリース時二重適用・振り切り復帰時の値劣化を解消。
    - カスタム `WeightSlider` を追加し、バークリックや矢印キーによる単発操作も正確に 1 Undo として確定するよう対応。

- **Undo 関連バグ・履歴汚染の解消**
  - 対象：`Contents/plug-ins/bake_skin_weight_improve.py`、`Contents/scripts/siweighteditor_improve/qt.py`、`Contents/scripts/siweighteditor_improve/siweighteditor.py`、`Contents/scripts/siweighteditor_improve/editor/view_controller.py`、`Contents/scripts/siweighteditor_improve/editor/weight_ops.py`、`Contents/scripts/siweighteditor_improve/modeling.py`、`Contents/scripts/siweighteditor_improve/weight_transfer_multiple.py`
  - 内容：
    - `reverse_dict()` が複数メッシュ／頂点リストを上書き破壊していたバグを修正。
    - ウィンドウ閉鎖後に Undo/Redo してもエラーにならないよう安全ガードを追加し、開いている場合は `Focus` OFF 時も含めてテーブルとセル選択を復元。
    - `qt.undo_chunk()` / `qt.suspend_undo()` を整備し、選択時の `addAttr` 読み取り（`decode_lock_data()` の読み取り専用化）による履歴汚染や、`hilite_vertices` での Undo 無効化リークを解消。

---

### 3. ウェイト正規化警告（`Specified weights required modification for normalization.`）の抑止

- 対象：`Contents/plug-ins/bake_skin_weight_improve.py`、`Contents/scripts/siweighteditor_improve/tools/rig_cleanup_helper.py`、`Contents/scripts/siweighteditor_improve/tools/model_edit_window.py`
- 原因：正規化済みウェイトを `MFnSkinCluster.setWeights(..., normalize=False)` で設定しても、浮動小数点の微小誤差に対して Maya の `skinCluster.normalizeWeights` が自動補正を走らせ、スクリプトエディタへ毎回警告を出力していた。
- 修正内容：`_set_weights_without_norm_warning()` を導入し、`setWeights` 実行直前のみ OpenMaya `MPlug` で `normalizeWeights` を一時的に `0`（Undo 非汚染）へ切り替え、`finally` で元の値へ復元（ロック／接続時は `scriptEditorInfo(suppressWarnings=True)` へフォールバック）。

---

### 4. モデル / ボーン修正の 0 フレーム回転混入バグ修正 ＆ `[キャラ名]_Hips` 記憶ジョイント確認

- **0 フレーム（A ポーズ）への不正回転 XYZ 混入バグ修正**
  - 対象：`Contents/scripts/siweighteditor_improve/tools/model_edit_window.py`
  - 原因：ステップ 1（`start_modeling_selected()`）で 0 フレームへ移動した直後に `mel.eval("gotoBindPose;")` を無条件実行しており、古い `dagPose`（バインドポーズノード）由来の回転ズレが 0 フレームの A ポーズ（Rotate = `0, 0, 0`）へ強制書き込みされていた。また `autoKeyframe` が停止されておらず、0 フレームのキー自体を上書きするケースもあった。
  - 修正内容：
    - `start_modeling_selected()` / `finish_modeling_selected()` の処理中は `cmds.autoKeyframe` を `try / finally` で一時停止。
    - `gotoBindPose` 実行前にスケルトンを検査（`_inspect_skeleton_rotation_state()`）し、基準フレーム（0F）に回転キーがある、または全ボーンが既に Rotate = `(0, 0, 0)` の A ポーズである場合は `gotoBindPose` をスキップ（理由を詳細ログへ記録）。
    - `gotoBindPose` 実行時にも微小誤差（`< 1e-4`）を `0.0` へ整え、意図しない回転変化があれば詳細ログへ警告出力。

- **記憶ジョイントの `[キャラ名]_Hips` 確認機能**
  - 対象：`Contents/scripts/siweighteditor_improve/tools/model_edit_window.py`
  - 内容：
    - `_is_hips_named_joint()`（ネームスペース除外・`^.+_Hips$` 判定）および同一スケルトン内 Hips 探索 `_find_hips_joint_in_skeleton()` を追加。
    - 記憶ジョイント表示欄で `[キャラ名]_Hips` 以外が記憶されている場合はオレンジ系の注意表示へ切り替え。
    - ステップ 1 開始時（記憶ジョイント使用時・新規登録時の両方）に `[キャラ名]_Hips` でない場合は確認ダイアログを表示。同一スケルトン内に `[キャラ名]_Hips` が見つかった場合は「`[キャラ名]_Hips` を記憶して続行 / このまま続行 / キャンセル」を選択可能。

---

### 5. 選択ボーンの回転 XYZ フリーズ（子ボーン含むキー退避＆書き戻し）

- 対象：`Contents/scripts/siweighteditor_improve/tools/rig_cleanup_helper.py`、`Contents/scripts/siweighteditor_improve/tools/model_edit_window.py`
- 実装機能：
  - `snapshot_joints_keyframes_exact()` / `restore_joints_keyframes_exact()` / `freeze_selected_bone_rotation_preserving_keyframes()` を追加し、「ジョイント整理 / Orient」タブに専用 UI セクションを配置。
  - 処理中は `autoKeyframe` を一時停止し、選択ボーンと配下の **全子ボーン** の全チャネル（`translate` / `rotate` / `scale`）のキー時刻・値・タンジェント種別・角度・ウェイト・インフィニティ設定をそのまま退避（JSON バックアップも保存）。
  - 選択ボーンの基準フレーム（デフォルト `0F`）における現在回転（未キー値含む）を `jointOrient` へ焼き込んで `Rotate = (0, 0, 0)`・`RotateAxis = (0, 0, 0)` へフリーズ（ワールド姿勢を変えないため子ボーンの位置・向きもズレない）。
  - 書き戻し時、フリーズ対象ボーンの回転は **基準フレーム（0F）を `0.0` に固定し、0F 以外のフレーム（0 以降の値）は元の値・タンジェントを加工せずそのまま復元**。非フリーズの子ボーンのキーも全フレームそのまま復元（単一 Undo チャンク `SIWE_FreezeBoneRotKeepKeys` 対応）。

---

### 6. バインド / ミラー＆コピー専用タブ ＆ ウェイト無しメッシュ一覧バインドウィンドウ

- **専用タブ新設（`TAB_VISIBILITY["bind_mirror"] = True`）**
  - 対象：`Contents/scripts/siweighteditor_improve/tools/rig_cleanup_helper.py`、`Contents/scripts/siweighteditor_improve/tools/weight_mirror_copy_helper.py`、`Contents/scripts/siweighteditor_improve/tools/model_edit_window.py`
  - 内容：
    - 「ボーンの安全バインド」を専用タブへ移動（反対側ボーンからミラー転写 / 近傍の自動バインド / インフルエンス追加のみの 3 モード・既存ウェイト保護・1 回 Undo）。
    - 高精度スキンウェイトミラー＆安全コピー（`weight_mirror_copy_helper.py`）を新規実装。ボーン選択・1 メッシュ全体・2 メッシュ間・頂点部分選択すべてに対応し、名前＋位置ハイブリッド照合および三角形サーフェス重心座標補間で高精度に転写。

- **ウェイト無しメッシュ一覧バインド専用ウィンドウ（`UnweightedMeshBindWindow`）**
  - 対象：`Contents/scripts/siweighteditor_improve/tools/unweighted_bind_window.py`（新規）、`Contents/scripts/siweighteditor_improve/tools/rig_cleanup_helper.py`、`Contents/scripts/siweighteditor_improve/tools/model_edit_window.py`、`Contents/scripts/siweighteditor_improve/siweighteditor.py`
  - 内容：
    - シーン内の「未バインド」または「スキンクラスタはあるが全頂点ウェイト 0」のポリゴンメッシュを自動検出し、左ペイン（メッシュ一覧・チェックボックス・状態・頂点数・フィルタ・ダブルクリックでビューポート選択確認）と右ペイン（階層順ボーン一覧・複数選択・子階層含めるオプション・ビューポート選択取得）を並べて表示。
    - `_is_dag_visible()` により、**非表示オブジェクト（自身の `visibility=False`、シェイプ非表示、親グループ非表示、`MDagPath.isVisible()`／ディスプレイレイヤー／`overrideVisibility` 非表示）は一覧から自動除外**。
    - 割り当て方式として「選択ボーンへ 100% 割り当て（単一ボーンなら全頂点 1.0、複数ボーン選択時は各頂点から最寄りボーンへ 1.0 を振り分け）」「近傍の自動バインド」「バインド / 登録のみ」を提供し、実行前サマリー確認と 1 回 Undo（`SIWE_BindUnweightedMeshes`）に対応。

---

### 7. ジョイント検索ウィンドウ（`JointSearchDialog`）＆ ボーンピッカー（`BonePickerWindow`）

- **非モーダル ジョイント検索ウィンドウ（`JointSearchDialog`）**
  - 対象：`Contents/scripts/siweighteditor_improve/editor/widgets.py`、`Contents/scripts/siweighteditor_improve/editor/view_controller.py`
  - 内容：
    - メイン画面の `Joints...` ボタンから非モーダル表示で開き、テーブル編集や 3D ビュー操作と並行して利用可能。`showEvent()` で `qt.attach_maya_window_owner()` を適用しており、Maya 本体より後ろへ潜り込まない。
    - **リアルタイム絞り込み**：スペース区切りの複数キーワード AND 検索、および `-` / `!` 接頭辞による除外検索に対応。
    - **範囲フィルタ**：`すべて (All)` / `ウェイトあり (Weighted)` / `選択関連 (Selected)`（現在選択中の頂点に関係するボーンのみ）をワンクリック切替。
    - **左右フィルタ**：`左右すべて` / `左 (L/Left)` / `右 (R/Right)` / `中央 (Center)` を切替可能。`_detect_bone_side()` によりキャメルケース（`Left`/`Right`）、区切り文字（`_L`/`_R`）、末尾数字付きなど多様な命名規則を自動判別。
    - **ウェイト統計表示＆選択維持**：各ボーン項目に `[Max: 最大ウェイト / 影響頂点数vtx]` を併記してウェイト有無を色分け表示し、フィルタを切り替えても選択済みボーン（`selected_names`）を保持。
    - **メイン検索欄への反映＆シーン連動**：`厳密一致 (=)` チェック（デフォルト ON・`=ボーン名` 形式で派生ボーンの誤巻き込みを防止）に対応し、`置換 (Replace)`・`追加 (Add)`・行ダブルクリックで即座に検索欄へ反映。`シーン選択 (From Scene)` で 3D ビュー／アウトライナ選択ジョイントを一覧へ取り込み、`再読込 (Refresh)` で最新状態へ同期。

- **ボーンピッカー連携（`BonePickerWindow`）**
  - 対象：`Contents/scripts/siweighteditor_improve/tools/bone_picker_window.py`
  - 内容：
    - ジョイント検索ウィンドウの `ボーンピッカー...` ボタンから非モーダル起動できる視覚的ボーン選択ウィンドウ。
    - **2 モード切替タブ**：体幹・手足・5 本指・補助ボーンを配置した `人型ピッカー (Humanoid Picker)` と、実スケルトンの 3D 座標を投影する `実スケルトン 2D (Skeleton 2D Map)`（正面 XY / 側面 ZY / 上面 XZ 切替・パン＆ズーム・矩形囲み選択対応）を搭載。
    - 左右フィルタ、`検索へ自動反映 (Auto Search)`、`厳密一致 (=)`、およびピッカー選択中のボーンを Maya 3D ビューポート上でシアン色強調する `3D ハイライト` に対応。

---

### 8. UI / 操作性の改善

- **ツールバー追加機能ボタンのグループ分離・視覚識別化**
  - 対象：`Contents/scripts/siweighteditor_improve/editor/ui_builder.py`
  - 内容：
    - 標準の `n / s / r / t` アイコン群から追加機能を分離し、ツールバー後半に以下の 2 グループとして配置：
      - **`+View`（表示・選択補助系・ティール系配色）**：`Joint Hilite` → `Used Joints` → `Bone Vis` → `Keep Bone`
      - **`+Win`（ウィンドウ起動系・インディゴ系配色）**：`Remap` → `Model Edit`
    - 各タグおよび 6 ボタンのツールチップ先頭に `[Improve 追加機能 / 表示系]`・`[Improve 追加機能 / ウィンドウ]` を明記。

- **Bone Weight Remap の UI 上部ガイド追加**
  - 対象：`Contents/scripts/siweighteditor_improve/tools/bone_remap_window.py`
  - 内容：ウィンドウ最上部にウェイト移動・ボーン追加・「対象を固定」の使い方をまとめた日英対応の説明パネルを追加し、初期ウィンドウ高さを `520` → `585` に調整。

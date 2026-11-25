# -*- coding: utf-8 -*-
import sys
from maya import cmds
import maya.OpenMaya as om
import maya.OpenMayaAnim as oma
import maya.api.OpenMaya as om2
import maya.api.OpenMayaRender as omr
#import maya.api.OpenMayaUI as omui
 
kPluginCmdName = "bakeSkinWeightImprove" # MELコマンド名
 
kShortFlagName = "-ts"     # 引数のショートネーム
kLongFlagName = "-test"   # 引数のロングネーム
 
MAYA_VER = int(cmds.about(v=True)[:4])


def _get_visible_mesh_shape_dag(node):
    """メッシュノードから中間オブジェクト以外の可視シェイプ DAG を取得する。"""
    try:
        sel = om2.MSelectionList()
        sel.add(node)
        dag = sel.getDagPath(0)
        if dag.node().hasFn(om2.MFn.kMesh):
            return dag
        shape_count = dag.numberOfShapesDirectlyBelow()
        fallback = None
        for idx in range(shape_count):
            shape_dag = om2.MDagPath(dag)
            try:
                shape_dag.extendToShape(idx)
            except Exception:
                continue
            if not shape_dag.node().hasFn(om2.MFn.kMesh):
                continue
            if fallback is None:
                fallback = shape_dag
            if not om2.MFnDagNode(shape_dag).isIntermediateObject:
                return shape_dag
        return fallback
    except Exception:
        return None


def _set_weights_without_norm_warning(skinFn, meshDag, vertexComp, infIndices, weights):
    """正規化警告を抑止しつつ OpenMaya 経由でスキンクラスタへウェイトを書き込む。"""
    nw_plug = None
    prev_nw = None
    nw_switched = False
    try:
        plug = skinFn.findPlug("normalizeWeights", False)
        is_null = plug.isNull() if callable(plug.isNull) else bool(plug.isNull)
        if not is_null:
            is_locked = plug.isLocked() if callable(plug.isLocked) else bool(plug.isLocked)
            is_conn = plug.isConnected() if callable(plug.isConnected) else bool(plug.isConnected)
            if not is_locked and not is_conn:
                prev_nw = plug.asShort()
                nw_plug = plug
                if prev_nw != 0:
                    plug.setShort(0)
                nw_switched = True
    except Exception:
        nw_plug = None
        prev_nw = None
        nw_switched = False

    prev_suppress = None
    if not nw_switched:
        try:
            prev_suppress = bool(cmds.scriptEditorInfo(query=True, suppressWarnings=True))
            if not prev_suppress:
                cmds.scriptEditorInfo(suppressWarnings=True)
        except Exception:
            prev_suppress = None

    try:
        skinFn.setWeights(meshDag, vertexComp, infIndices, weights, False)
    finally:
        if nw_plug is not None and prev_nw is not None and prev_nw != 0:
            try:
                nw_plug.setShort(prev_nw)
            except Exception:
                pass
        if prev_suppress is False:
            try:
                cmds.scriptEditorInfo(suppressWarnings=False)
            except Exception:
                pass


def _notify_mesh_geometry_dirty(node, force_dg_sync=False):
    """ウェイト書き込み後に Viewport 2.0 の描画・ピッキングキャッシュを同期させる。"""
    shape_dag = _get_visible_mesh_shape_dag(node)
    if shape_dag is None:
        return
    try:
        omr.MRenderer.setGeometryDrawDirty(shape_dag.node())
    except Exception:
        pass
    if force_dg_sync:
        try:
            om2.MFnMesh(shape_dag).updateSurface()
        except Exception:
            pass
        try:
            shape_path = shape_dag.fullPathName()
            if cmds.objExists(shape_path):
                cmds.dgdirty(shape_path)
        except Exception:
            pass

def maya_useNewAPI():
    pass
    
##########################################################
# Plug-in :メインの記述部分
##########################################################
class BakeSkinWeightClass( om2.MPxCommand ):
    """OpenMaya 経由の高速ウェイト書き込みおよび Undo / Redo を担当するコマンド。"""

    def __init__(self):
        global siweighteditor
        from siweighteditor_improve import siweighteditor
        om2.MPxCommand.__init__(self)
        self.ignore_undo = False
        self.bake_node_id_dict = {}
        self.bake_node_weight_dict = {}
        self.bake_node_inf_dict = {}
        self.node_skinFn_dict = {}
        self.undo_node_weight_dict = {}
        self.redo_node_weight_dict = {}
        self.org_node_weight_dict = {}

    def doIt(self, args):
        realbake, ignore_undo = self.parseArguments( args )
        self.ignore_undo = bool(ignore_undo)
        (
            bake_node_id_dict,
            bake_node_weight_dict,
            bake_node_inf_dict,
            node_skinFn_dict,
            undo_node_weight_dict,
            redo_node_weight_dict,
            org_node_weight_dict,
        ) = siweighteditor.get_current_data()

        # 後続のコマンド実行で履歴データが書き換わらないようスナップショットを保持する
        self.bake_node_id_dict = dict(bake_node_id_dict or {})
        self.bake_node_weight_dict = dict(bake_node_weight_dict or {})
        self.bake_node_inf_dict = dict(bake_node_inf_dict or {})
        self.node_skinFn_dict = dict(node_skinFn_dict or {})
        self.undo_node_weight_dict = {
            node: [list(w) for w in w_list]
            for node, w_list in (undo_node_weight_dict or {}).items()
        }
        self.redo_node_weight_dict = {
            node: [list(w) for w in w_list]
            for node, w_list in (redo_node_weight_dict or {}).items()
        }
        self.org_node_weight_dict = dict(org_node_weight_dict or {})

        if realbake:
            self.redoIt(flash=False)

    def parseArguments(self, args):
        argData = om2.MArgParser(self.syntax(), args)
        flagValue = True
        ignore_undo = False
        if argData.isFlagSet( '-rb' ):
            flagValue = argData.flagArgumentBool( '-rb', 0)
        if argData.isFlagSet( '-iu' ):
            ignore_undo = argData.flagArgumentBool( '-iu', 0)
        return flagValue, ignore_undo

    def _apply_weights_map(self, weight_dict, force_dg_sync=True):
        """指定されたノードごとのウェイト辞書をスキンクラスタへ安全に適用する。"""
        for node, vtxIndices in self.bake_node_id_dict.items():
            if not vtxIndices:
                continue
            if node not in weight_dict or node not in self.bake_node_inf_dict or node not in self.node_skinFn_dict:
                continue
            try:
                if not cmds.objExists(node):
                    continue
            except Exception:
                continue

            weights = weight_dict[node]
            infIndices = self.bake_node_inf_dict[node]
            skinFn = self.node_skinFn_dict[node]

            try:
                if MAYA_VER >= 2016:
                    meshDag = _get_visible_mesh_shape_dag(node)
                    if meshDag is None:
                        sList = om2.MSelectionList()
                        sList.add(node)
                        meshDag, _ = sList.getComponent(0)
                    singleIdComp = om2.MFnSingleIndexedComponent()
                    vertexComp = singleIdComp.create(om2.MFn.kMeshVertComponent)
                    singleIdComp.addElements(vtxIndices)
                else:
                    sList = om.MSelectionList()
                    sList.add(node)
                    meshDag = om.MDagPath()
                    component = om.MObject()
                    sList.getDagPath(0, meshDag, component)
                    singleIdComp = om.MFnSingleIndexedComponent()
                    vertexComp = singleIdComp.create(om.MFn.kMeshVertComponent)
                    singleIdComp.addElements(vtxIndices)

                _set_weights_without_norm_warning(skinFn, meshDag, vertexComp, infIndices, weights)
                _notify_mesh_geometry_dirty(node, force_dg_sync=force_dg_sync)
            except Exception as exc:
                sys.stderr.write("bakeSkinWeightImprove apply error ({0}): {1}\n".format(node, exc))

    def redoIt(self, flash=True):
        self._apply_weights_map(
            self.bake_node_weight_dict,
            force_dg_sync=not self.ignore_undo,
        )
        # アンドゥ・リドゥ用にエディタ側のキャッシュ辞書を更新する
        try:
            siweighteditor.update_dict(self.redo_node_weight_dict, self.bake_node_id_dict)
        except Exception:
            pass
        if flash:
            if self.ignore_undo:
                return
            try:
                siweighteditor.refresh_window()
            except Exception:
                pass

    def undoIt(self):
        if self.ignore_undo:
            return
        try:
            siweighteditor.reverse_dict(self.undo_node_weight_dict, self.bake_node_id_dict)
        except Exception:
            pass
        self._apply_weights_map(
            self.org_node_weight_dict,
            force_dg_sync=True,
        )
        try:
            siweighteditor.refresh_window()
        except Exception:
            pass

    def isUndoable(self):
        # プレビュー専用の焼き込み（iu=True）は Maya の Undo キューへ積まない
        return not self.ignore_undo
 
##########################################################
# Plug-in initialization.
##########################################################
def cmdCreator():
    return BakeSkinWeightClass() 
    
def syntaxCreator():
    syntax = om2.MSyntax()
    syntax.addFlag( '-rb', '-realbake', om2.MSyntax.kBoolean )
    syntax.addFlag( '-iu', '-ignoreundo', om2.MSyntax.kBoolean )
    return syntax

#
def initializePlugin( mobject ):
    mplugin = om2.MFnPlugin( mobject )
    try:
        mplugin.registerCommand( kPluginCmdName, cmdCreator, syntaxCreator )
        #引数持たせないバージョン
        #mplugin.registerCommand( kPluginCmdName, cmdCreator)
    except:
        sys.stderr.write( 'Failed to register command: ' + kPluginCmdName )
 
def uninitializePlugin( mobject ):
    mplugin = om2.MFnPlugin( mobject )
    try:
        mplugin.deregisterCommand( kPluginCmdName )
    except:
        sys.stderr.write( 'Failed to unregister command: ' + kPluginCmdName ) 
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
    
    def __init__(self):
        global siweighteditor
        from siweighteditor_improve import siweighteditor
        ''' Constructor. '''
        om2.MPxCommand.__init__(self)
    
    def doIt(self, args):
        realbake, ignore_undo = self.parseArguments( args )
        self.ignore_undo = ignore_undo
        self.bake_node_id_dict, self.bake_node_weight_dict, \
        self.bake_node_inf_dict, self.node_skinFn_dict, \
        self.undo_node_weight_dict, self.redo_node_weight_dict, \
        self.org_node_weight_dict = siweighteditor.get_current_data()
        if realbake:
            self.redoIt(flash=False)
        
    def parseArguments(self, args):
        argData = om2.MArgParser(self.syntax(), args)
        
        if argData.isFlagSet( '-rb' ):
            flagValue = argData.flagArgumentBool( '-rb', 0)
        if argData.isFlagSet( '-iu' ):
            ignore_undo = argData.flagArgumentBool( '-iu', 0)
        return flagValue, ignore_undo
        
    def redoIt(self, flash=True):
        for node, vtxIndices in self.bake_node_id_dict.items():
            weights = self.bake_node_weight_dict[node]
            infIndices = self.bake_node_inf_dict[node]
            skinFn = self.node_skinFn_dict[node]
            
            if MAYA_VER >= 2016:
                meshDag = _get_visible_mesh_shape_dag(node)
                if meshDag is None:
                    sList = om2.MSelectionList()
                    sList.add(node)
                    meshDag, component = sList.getComponent(0)
                if not vtxIndices:
                    continue
                # 指定の頂点をコンポーネントとして取得する
                singleIdComp = om2.MFnSingleIndexedComponent()
                vertexComp = singleIdComp.create(om2.MFn.kMeshVertComponent )
                singleIdComp.addElements(vtxIndices)
            else:
                sList = om.MSelectionList()
                sList.add(node)
                meshDag = om.MDagPath()
                component = om.MObject()
                sList.getDagPath(0, meshDag, component)
                singleIdComp = om.MFnSingleIndexedComponent()
                vertexComp = singleIdComp.create(om.MFn.kMeshVertComponent )
                singleIdComp.addElements(vtxIndices)
                            
            ##引数（dag_path, MIntArray, MIntArray, MDoubleArray, Normalize, old_weight_undo）
            #print meshDag, vertexComp , infIndices , weights
            #print type(infIndices)
            #print type(vertexComp)
            skinFn.setWeights(meshDag, vertexComp , infIndices , weights, False)
            _notify_mesh_geometry_dirty(node, force_dg_sync=not self.ignore_undo)
        #アンドゥ用ウェイトデータをアップデートする
        siweighteditor.update_dict(self.redo_node_weight_dict, self.bake_node_id_dict)
        if flash:
            if self.ignore_undo:#スライダー制御中のアンドゥ履歴は全無視する
                return
            siweighteditor.refresh_window()
            
    
    def undoIt(self):
        siweighteditor.reverse_dict(self.undo_node_weight_dict, self.bake_node_id_dict)
        if self.ignore_undo:#スライダー制御中のアンドゥ履歴は全無視する
            return
        for node, vtxIndices in self.bake_node_id_dict.items():
            weights = self.org_node_weight_dict[node]
            infIndices = self.bake_node_inf_dict[node]
            skinFn = self.node_skinFn_dict[node]
            
            if MAYA_VER >= 2016:
                meshDag = _get_visible_mesh_shape_dag(node)
                if meshDag is None:
                    sList = om2.MSelectionList()
                    sList.add(node)
                    meshDag, component = sList.getComponent(0)
                if not vtxIndices:
                    continue
                singleIdComp = om2.MFnSingleIndexedComponent()
                vertexComp = singleIdComp.create(om2.MFn.kMeshVertComponent )
                singleIdComp.addElements(vtxIndices)
            else:
                sList = om.MSelectionList()
                sList.add(node)
                meshDag = om.MDagPath()
                component = om.MObject()
                sList.getDagPath(0, meshDag, component)
                singleIdComp = om.MFnSingleIndexedComponent()
                vertexComp = singleIdComp.create(om.MFn.kMeshVertComponent )
                singleIdComp.addElements(vtxIndices)
                        
            ##引数（dag_path, MIntArray, MIntArray, MDoubleArray, Normalize, old_weight_undo）
            skinFn.setWeights(meshDag, vertexComp , infIndices , weights, False)
            _notify_mesh_geometry_dirty(node, force_dg_sync=True)
        #アンドゥの度に読み込むと重いからどうしよう。
        siweighteditor.refresh_window()
 
    def isUndoable(self):
        return True
 
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
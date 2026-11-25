# -*- coding: utf-8 -*-

import os
import sys
import maya.cmds as cmds


def __register_siweighteditor_improve_startup():
    # ApplicationPlugins の PackageContents.xml と PYTHONPATH の両方から二重実行されるのを防ぐ
    if os.environ.get("SIWE_IMPROVE_USERSETUP_REGISTERED") == "1":
        return
    os.environ["SIWE_IMPROVE_USERSETUP_REGISTERED"] = "1"

    from textwrap import dedent
    cmds.evalDeferred(dedent(
        """
        import siweighteditor_improve.startup as s

        s.execute()
        """
    ))


if __name__ == '__main__':
    try:
        print("SiWeightEditor Improve startup script has begun")
        __register_siweighteditor_improve_startup()
        print("SiWeightEditor Improve startup script has finished")

    except Exception as e:
        print("SiWeightEditor Improve startup script has ended with error")
        # avoidng the "call userSetup.py chain" accidentally stop,
        # all exception must be collapsed
        import traceback
        traceback.print_exc()

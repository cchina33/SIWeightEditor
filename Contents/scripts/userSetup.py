# # -*- coding: utf-8 -*-

import maya.cmds as cmds


def __register_siweighteditor_improve_startup():
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

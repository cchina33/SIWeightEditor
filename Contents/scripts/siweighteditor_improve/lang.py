# -*- coding: utf-8 -*-
"""Windows API を用いた表示言語の自動判定と日英テキスト切替ヘルパー。"""

import ctypes
import locale
import os
import sys

LANG_PRIMARY_JAPANESE = 0x11


def detect_ui_language():
    """Windows API で表示言語を取得し、日本語なら 'ja_JP'、それ以外は 'en_US' を返す。"""
    if sys.platform.startswith("win"):
        try:
            kernel32 = ctypes.windll.kernel32
            lang_id = kernel32.GetUserDefaultUILanguage()
            if (int(lang_id) & 0x3FF) == LANG_PRIMARY_JAPANESE:
                return "ja_JP"
            return "en_US"
        except Exception:
            try:
                lcid = ctypes.windll.kernel32.GetUserDefaultLCID()
                if (int(lcid) & 0x3FF) == LANG_PRIMARY_JAPANESE:
                    return "ja_JP"
                return "en_US"
            except Exception:
                pass

    # Windows 以外の環境ではロケール情報からフォールバック判定する
    for env_key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        env_val = os.environ.get(env_key, "")
        if env_val.lower().startswith("ja"):
            return "ja_JP"
    try:
        loc_code = locale.getlocale()[0] or ""
        if loc_code.lower().startswith("ja"):
            return "ja_JP"
    except Exception:
        pass
    return "en_US"


UI_LANGUAGE = detect_ui_language()


class Lang(object):
    def __init__(self, en='', ja=''):
        self.jp = ja
        self.en = en

    def output(self):
        if UI_LANGUAGE == "ja_JP":
            return self.jp if self.jp else self.en
        return self.en if self.en else self.jp

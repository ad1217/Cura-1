#!/usr/bin/env python3

# Copyright (c) 2015 Ultimaker B.V.
# Cura is released under the terms of the AGPLv3 or higher.

import sys

def exceptHook(type, value, traceback):
    import cura.CrashHandler
    cura.CrashHandler.show(type, value, traceback)

sys.excepthook = exceptHook

import cura.CuraApplication

if sys.platform == "win32" and hasattr(sys, "frozen"):
    import os
    dirpath = os.path.expanduser("~/AppData/Local/cura/")
    os.makedirs(dirpath, exist_ok = True)
    sys.stdout = open(os.path.join(dirpath, "stdout.log"), "w")
    sys.stderr = open(os.path.join(dirpath, "stderr.log"), "w")

app = cura.CuraApplication.CuraApplication.getInstance()
app.run()

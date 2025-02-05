# Copyright (c) 2015 Ultimaker B.V.
# Cura is released under the terms of the AGPLv3 or higher.

from . import ChangeLog

from UM.i18n import i18nCatalog
catalog = i18nCatalog("cura")

def getMetaData():
    return {
        "plugin": {
            "name": catalog.i18nc("@label", "Change Log"),
            "author": "Ultimaker",
            "version": "1.0",
            "description":  catalog.i18nc("@info:whatsthis", "Shows changes since latest checked version"),
            "api": 2
        }
    }

def register(app):
    return {"extension": ChangeLog.ChangeLog()}
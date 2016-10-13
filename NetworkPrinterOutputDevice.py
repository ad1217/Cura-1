from UM.i18n import i18nCatalog
from UM.Application import Application
from UM.Logger import Logger
from UM.Signal import signalemitter

from UM.Message import Message

import UM.Settings

from cura.PrinterOutputDevice import PrinterOutputDevice, ConnectionState
import cura.Settings.ExtruderManager

from PyQt5.QtNetwork import QHttpMultiPart, QHttpPart, QNetworkRequest, QNetworkAccessManager, QNetworkReply
from PyQt5.QtCore import QUrl, QTimer, pyqtSignal, pyqtProperty, pyqtSlot, QCoreApplication
from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QMessageBox

import json
import os
import gzip
import zlib

from time import time
from time import sleep

i18n_catalog = i18nCatalog("cura")

from enum import IntEnum

class AuthState(IntEnum):
    NotAuthenticated = 1
    AuthenticationRequested = 2
    Authenticated = 3
    AuthenticationDenied = 4

##  Network connected (wifi / lan) printer that uses the Ultimaker API
@signalemitter
class NetworkPrinterOutputDevice(PrinterOutputDevice):
    def __init__(self, key, address, properties, api_prefix):
        super().__init__(key)
        self._address = address
        self._key = key
        self._properties = properties  # Properties dict as provided by zero conf
        self._api_prefix = api_prefix

        self._gcode = None
        self._print_finished = True  # _print_finsihed == False means we're halfway in a print

        self._use_gzip = True  # Should we use g-zip compression before sending the data?

        # This holds the full JSON file that was received from the last request.
        # The JSON looks like:
        #{
        #    "led": {"saturation": 0.0, "brightness": 100.0, "hue": 0.0},
        #    "beep": {},
        #    "network": {
        #        "wifi_networks": [],
        #        "ethernet": {"connected": true, "enabled": true},
        #        "wifi": {"ssid": "xxxx", "connected": False, "enabled": False}
        #    },
        #    "diagnostics": {},
        #    "bed": {"temperature": {"target": 60.0, "current": 44.4}},
        #    "heads": [{
        #        "max_speed": {"z": 40.0, "y": 300.0, "x": 300.0},
        #        "position": {"z": 20.0, "y": 6.0, "x": 180.0},
        #        "fan": 0.0,
        #        "jerk": {"z": 0.4, "y": 20.0, "x": 20.0},
        #        "extruders": [
        #            {
        #                "feeder": {"max_speed": 45.0, "jerk": 5.0, "acceleration": 3000.0},
        #                "active_material": {"GUID": "xxxxxxx", "length_remaining": -1.0},
        #                "hotend": {"temperature": {"target": 0.0, "current": 22.8}, "id": "AA 0.4"}
        #            },
        #            {
        #                "feeder": {"max_speed": 45.0, "jerk": 5.0, "acceleration": 3000.0},
        #                "active_material": {"GUID": "xxxx", "length_remaining": -1.0},
        #                "hotend": {"temperature": {"target": 0.0, "current": 22.8}, "id": "BB 0.4"}
        #            }
        #        ],
        #        "acceleration": 3000.0
        #    }],
        #    "status": "printing"
        #}

        self._json_printer_state = {}

        ##  Todo: Hardcoded value now; we should probably read this from the machine file.
        ##  It's okay to leave this for now, as this plugin is um3 only (and has 2 extruders by definition)
        self._num_extruders = 2

        # These are reinitialised here (from PrinterOutputDevice) to match the new _num_extruders
        self._hotend_temperatures = [0] * self._num_extruders
        self._target_hotend_temperatures = [0] * self._num_extruders

        self._material_ids = [""] * self._num_extruders
        self._hotend_ids = [""] * self._num_extruders

        self.setPriority(2) # Make sure the output device gets selected above local file output
        self.setName(key)
        self.setShortDescription(i18n_catalog.i18nc("@action:button", "Print over network"))
        self.setDescription(i18n_catalog.i18nc("@properties:tooltip", "Print over network"))
        self.setIconName("print")

        self._manager = None

        self._post_request = None
        self._post_reply = None
        self._post_multi_part = None
        self._post_part = None

        self._material_multi_part = None
        self._material_part = None

        self._progress_message = None
        self._error_message = None
        self._connection_message = None

        self._update_timer = QTimer()
        self._update_timer.setInterval(2000)  # TODO; Add preference for update interval
        self._update_timer.setSingleShot(False)
        self._update_timer.timeout.connect(self._update)

        self._camera_timer = QTimer()
        self._camera_timer.setInterval(2000)  # Todo: Add preference for camera update interval
        self._camera_timer.setSingleShot(False)
        self._camera_timer.timeout.connect(self._update_camera)

        self._camera_image_id = 0

        self._authentication_counter = 0
        self._max_authentication_counter = 5 * 60  # Number of attempts before authentication timed out (5 min)

        self._authentication_timer = QTimer()
        self._authentication_timer.setInterval(1000)  # TODO; Add preference for update interval
        self._authentication_timer.setSingleShot(False)
        self._authentication_timer.timeout.connect(self._onAuthenticationTimer)
        self._authentication_request_active = False

        self._authentication_state = AuthState.NotAuthenticated
        self._authentication_id = None
        self._authentication_key = None

        self._authentication_requested_message = Message(i18n_catalog.i18nc("@info:status", "Access to the printer requested. Please approve the request on the printer"), lifetime = 0, dismissable = False, progress = 0)
        self._authentication_failed_message = Message(i18n_catalog.i18nc("@info:status", ""))
        self._authentication_failed_message.addAction("Retry", i18n_catalog.i18nc("@action:button", "Retry"), None, i18n_catalog.i18nc("@info:tooltip", "Re-send the access request"))
        self._authentication_failed_message.actionTriggered.connect(self.requestAuthentication)
        self._authentication_succeeded_message = Message(i18n_catalog.i18nc("@info:status", "Access to the printer accepted"))
        self._not_authenticated_message = Message(i18n_catalog.i18nc("@info:status", "No access to print with this printer. Unable to send print job."))
        self._not_authenticated_message.addAction("Request", i18n_catalog.i18nc("@action:button", "Request Access"), None, i18n_catalog.i18nc("@info:tooltip", "Send access request to the printer"))
        self._not_authenticated_message.actionTriggered.connect(self.requestAuthentication)

        self._camera_image = QImage()

        self._material_post_objects = {}
        self._connection_state_before_timeout = None

        self._last_response_time = time()
        self._last_request_time = None
        self._response_timeout_time = 10
        self._recreate_network_manager_time = 30 # If we have no connection, re-create network manager every 30 sec.
        self._recreate_network_manager_count = 1

        self._send_gcode_start = time()  # Time when the sending of the g-code started.

        self._last_command = ""

        self._compressing_print = False

        printer_type = self._properties.get(b"machine", b"").decode("utf-8")
        if printer_type.startswith("9511"):
            self._updatePrinterType("ultimaker3_extended")
        elif printer_type.startswith("9066"):
            self._updatePrinterType("ultimaker3")
        else:
            self._updatePrinterType("unknown")

    def _onNetworkAccesibleChanged(self, accessible):
        Logger.log("d", "Network accessible state changed to: %s", accessible)

    def _onAuthenticationTimer(self):
        self._authentication_counter += 1
        self._authentication_requested_message.setProgress(self._authentication_counter / self._max_authentication_counter * 100)
        if self._authentication_counter > self._max_authentication_counter:
            self._authentication_timer.stop()
            Logger.log("i", "Authentication timer ended. Setting authentication to denied")
            self.setAuthenticationState(AuthState.AuthenticationDenied)

    def _onAuthenticationRequired(self, reply, authenticator):
        if self._authentication_id is not None and self._authentication_key is not None:
            Logger.log("d", "Authentication was required. Setting up authenticator.")
            authenticator.setUser(self._authentication_id)
            authenticator.setPassword(self._authentication_key)

    def getProperties(self):
        return self._properties

    @pyqtSlot(str, result = str)
    def getProperty(self, key):
        key = key.encode("utf-8")
        if key in self._properties:
            return self._properties.get(key, b"").decode("utf-8")
        else:
            return ""

    ##  Get the unique key of this machine
    #   \return key String containing the key of the machine.
    @pyqtSlot(result = str)
    def getKey(self):
        return self._key

    ##  Name of the printer (as returned from the zeroConf properties)
    @pyqtProperty(str, constant = True)
    def name(self):
        return self._properties.get(b"name", b"").decode("utf-8")

    ##  Firmware version (as returned from the zeroConf properties)
    @pyqtProperty(str, constant=True)
    def firmwareVersion(self):
        return self._properties.get(b"firmware_version", b"").decode("utf-8")

    ## IPadress of this printer
    @pyqtProperty(str, constant=True)
    def ipAddress(self):
        return self._address

    def _update_camera(self):
        if not self._manager.networkAccessible():
            return
        ## Request new image
        url = QUrl("http://" + self._address + ":8080/?action=snapshot")
        image_request = QNetworkRequest(url)
        self._manager.get(image_request)
        self._last_request_time = time()

    ##  Set the authentication state.
    #   \param auth_state \type{AuthState} Enum value representing the new auth state
    def setAuthenticationState(self, auth_state):
        if auth_state == AuthState.AuthenticationRequested:
            Logger.log("d", "Authentication state changed to authentication requested.")
            self.setAcceptsCommands(False)
            self.setConnectionText(i18n_catalog.i18nc("@info:status", "Connected over the network to {0}. Please approve the access request on the printer.").format(self.name))
            self._authentication_requested_message.show()
            self._authentication_request_active = True
            self._authentication_timer.start()  # Start timer so auth will fail after a while.
        elif auth_state == AuthState.Authenticated:
            Logger.log("d", "Authentication state changed to authenticated")
            self.setAcceptsCommands(True)
            self.setConnectionText(i18n_catalog.i18nc("@info:status", "Connected over the network to {0}.").format(self.name))
            self._authentication_requested_message.hide()
            if self._authentication_request_active:
                self._authentication_succeeded_message.show()

            # Stop waiting for a response
            self._authentication_timer.stop()
            self._authentication_counter = 0

            # Once we are authenticated we need to send all material profiles.
            self.sendMaterialProfiles()
        elif auth_state == AuthState.AuthenticationDenied:
            self.setAcceptsCommands(False)
            self.setConnectionText(i18n_catalog.i18nc("@info:status", "Connected over the network to {0}. No access to control the printer.").format(self.name))
            self._authentication_requested_message.hide()
            if self._authentication_request_active:
                if self._authentication_timer.remainingTime() > 0:
                    Logger.log("d", "Authentication state changed to authentication denied before the request timeout.")
                    self._authentication_failed_message.setText(i18n_catalog.i18nc("@info:status", "Access request was denied on the printer."))
                else:
                    Logger.log("d", "Authentication state changed to authentication denied due to a timeout")
                    self._authentication_failed_message.setText(i18n_catalog.i18nc("@info:status", "Access request failed due to a timeout."))

                self._authentication_failed_message.show()
            self._authentication_request_active = False

            # Stop waiting for a response
            self._authentication_timer.stop()
            self._authentication_counter = 0

        if auth_state != self._authentication_state:
            self._authentication_state = auth_state
            self.authenticationStateChanged.emit()

    authenticationStateChanged = pyqtSignal()

    @pyqtProperty(int, notify = authenticationStateChanged)
    def authenticationState(self):
        return self._authentication_state

    @pyqtSlot()
    def requestAuthentication(self, message_id = None, action_id = "Retry"):
        if action_id == "Request" or action_id == "Retry":
            self._authentication_failed_message.hide()
            self._not_authenticated_message.hide()
            self._authentication_state = AuthState.NotAuthenticated
            self._authentication_counter = 0
            self._authentication_requested_message.setProgress(0)
            self._authentication_id = None
            self._authentication_key = None
            self._createNetworkManager() # Re-create network manager to force re-authentication.

    ##  Request data from the connected device.
    def _update(self):
        if self._last_response_time:
            time_since_last_response = time() - self._last_response_time
        else:
            time_since_last_response = 0
        if self._last_request_time:
            time_since_last_request = time() - self._last_request_time
        else:
            time_since_last_request = float("inf") # An irrelevantly large number of seconds

        # Connection is in timeout, check if we need to re-start the connection.
        # Sometimes the qNetwork manager incorrectly reports the network status on Mac & Windows.
        # Re-creating the QNetworkManager seems to fix this issue.
        if self._last_response_time and self._connection_state_before_timeout:
            if time_since_last_response > self._recreate_network_manager_time * self._recreate_network_manager_count:
                self._recreate_network_manager_count += 1
                counter = 0  # Counter to prevent possible indefinite while loop.
                # It can happen that we had a very long timeout (multiple times the recreate time).
                # In that case we should jump through the point that the next update won't be right away.
                while time_since_last_response - self._recreate_network_manager_time * self._recreate_network_manager_count > self._recreate_network_manager_time and counter < 10:
                    counter += 1
                    self._recreate_network_manager_count += 1
                Logger.log("d", "Timeout lasted over %.0f seconds (%.1fs), re-checking connection.", self._recreate_network_manager_time, time_since_last_response)
                self._createNetworkManager()
                return

        # Check if we have an connection in the first place.
        if not self._manager.networkAccessible():
            if not self._connection_state_before_timeout:
                Logger.log("d", "The network connection seems to be disabled. Going into timeout mode")
                self._connection_state_before_timeout = self._connection_state
                self.setConnectionState(ConnectionState.error)
                self._connection_message = Message(i18n_catalog.i18nc("@info:status",
                                                                      "The connection with the network was lost."))
                self._connection_message.show()

                if self._progress_message:
                    self._progress_message.hide()

                # Check if we were uploading something. Abort if this is the case.
                # Some operating systems handle this themselves, others give weird issues.
                try:
                    if self._post_reply:
                        Logger.log("d", "Stopping post upload because the connection was lost.")
                        try:
                            self._post_reply.uploadProgress.disconnect(self._onUploadProgress)
                        except TypeError:
                            pass  # The disconnection can fail on mac in some cases. Ignore that.

                        self._post_reply.abort()
                        self._post_reply = None
                except RuntimeError:
                    self._post_reply = None  # It can happen that the wrapped c++ object is already deleted.
            return
        else:
            if not self._connection_state_before_timeout:
                self._recreate_network_manager_count = 1

        # Check that we aren't in a timeout state
        if self._last_response_time and self._last_request_time and not self._connection_state_before_timeout:
            if time_since_last_response > self._response_timeout_time and time_since_last_request <= self._response_timeout_time:
                # Go into timeout state.
                Logger.log("d", "We did not receive a response for %0.1f seconds, so it seems the printer is no longer accessible.", time_since_last_response)
                self._connection_state_before_timeout = self._connection_state
                self._connection_message = Message(i18n_catalog.i18nc("@info:status", "The connection with the printer was lost. Check your printer to see if it is connected."))
                self._connection_message.show()

                if self._progress_message:
                    self._progress_message.hide()

                # Check if we were uploading something. Abort if this is the case.
                # Some operating systems handle this themselves, others give weird issues.
                try:
                    if self._post_reply:
                        Logger.log("d", "Stopping post upload because the connection was lost.")
                        try:
                            self._post_reply.uploadProgress.disconnect(self._onUploadProgress)
                        except TypeError:
                            pass  # The disconnection can fail on mac in some cases. Ignore that.

                        self._post_reply.abort()
                        self._post_reply = None
                except RuntimeError:
                    self._post_reply = None  # It can happen that the wrapped c++ object is already deleted.
                self.setConnectionState(ConnectionState.error)
                return

        if self._authentication_state == AuthState.NotAuthenticated:
            self._verifyAuthentication()  # We don't know if we are authenticated; check if we have correct auth.
        elif self._authentication_state == AuthState.AuthenticationRequested:
            self._checkAuthentication()  # We requested authentication at some point. Check if we got permission.

        ## Request 'general' printer data
        url = QUrl("http://" + self._address + self._api_prefix + "printer")
        printer_request = QNetworkRequest(url)
        self._manager.get(printer_request)

        ## Request print_job data
        url = QUrl("http://" + self._address + self._api_prefix + "print_job")
        print_job_request = QNetworkRequest(url)
        self._manager.get(print_job_request)

        self._last_request_time = time()

    def _createNetworkManager(self):
        if self._manager:
            self._manager.finished.disconnect(self._onFinished)
            self._manager.networkAccessibleChanged.disconnect(self._onNetworkAccesibleChanged)
            self._manager.authenticationRequired.disconnect(self._onAuthenticationRequired)

        self._manager = QNetworkAccessManager()
        self._manager.finished.connect(self._onFinished)
        self._manager.authenticationRequired.connect(self._onAuthenticationRequired)
        self._manager.networkAccessibleChanged.connect(self._onNetworkAccesibleChanged)  # for debug purposes

    ##  Convenience function that gets information from the received json data and converts it to the right internal
    #   values / variables
    def _spliceJSONData(self):
        # Check for hotend temperatures
        for index in range(0, self._num_extruders):
            temperature = self._json_printer_state["heads"][0]["extruders"][index]["hotend"]["temperature"]["current"]
            self._setHotendTemperature(index, temperature)
            try:
                material_id = self._json_printer_state["heads"][0]["extruders"][index]["active_material"]["GUID"]
            except KeyError:
                material_id = ""
            self._setMaterialId(index, material_id)
            try:
                hotend_id = self._json_printer_state["heads"][0]["extruders"][index]["hotend"]["id"]
            except KeyError:
                hotend_id = ""
            self._setHotendId(index, hotend_id)

        bed_temperature = self._json_printer_state["bed"]["temperature"]["current"]
        self._setBedTemperature(bed_temperature)

        head_x = self._json_printer_state["heads"][0]["position"]["x"]
        head_y = self._json_printer_state["heads"][0]["position"]["y"]
        head_z = self._json_printer_state["heads"][0]["position"]["z"]
        self._updateHeadPosition(head_x, head_y, head_z)
        self._updatePrinterState(self._json_printer_state["status"])


    def close(self):
        Logger.log("d", "Closing connection of printer %s with ip %s", self._key, self._address)
        self._updateJobState("")
        self.setConnectionState(ConnectionState.closed)
        if self._progress_message:
            self._progress_message.hide()

        # Reset authentication state
        self._authentication_requested_message.hide()
        self._authentication_state = AuthState.NotAuthenticated
        self._authentication_counter = 0
        self._authentication_timer.stop()

        self._authentication_requested_message.hide()
        self._authentication_failed_message.hide()
        self._authentication_succeeded_message.hide()

        # Reset stored material & hotend data.
        self._material_ids = [""] * self._num_extruders
        self._hotend_ids = [""] * self._num_extruders

        if self._error_message:
            self._error_message.hide()

        # Reset timeout state
        self._connection_state_before_timeout = None
        self._last_response_time = time()
        self._last_request_time = None

        # Stop update timers
        self._update_timer.stop()
        self._camera_timer.stop()

    def requestWrite(self, node, file_name = None, filter_by_machine = False):
        if self._progress != 0:
            self._error_message = Message(i18n_catalog.i18nc("@info:status", "Unable to start a new print job because the printer is busy. Please check the printer."))
            self._error_message.show()
            return
        if self._printer_state != "idle":
            self._error_message = Message(
                i18n_catalog.i18nc("@info:status", "Unable to start a new print job, printer is busy. Current printer status is %s.") % self._printer_state)
            self._error_message.show()
            return
        elif self._authentication_state != AuthState.Authenticated:
            self._not_authenticated_message.show()
            Logger.log("d", "Attempting to perform an action without authentication. Auth state is %s", self._authentication_state)
            return

        Application.getInstance().showPrintMonitor.emit(True)
        self._print_finished = True
        self._gcode = getattr(Application.getInstance().getController().getScene(), "gcode_list")

        print_information = Application.getInstance().getPrintInformation()

        # Check if print cores / materials are loaded at all. Any failure in these results in an error.
        for index in range(0, self._num_extruders):
            if print_information.materialLengths[index] != 0:
                if self._json_printer_state["heads"][0]["extruders"][index]["hotend"]["id"] == "":
                    Logger.log("e", "No cartridge loaded in slot %s, unable to start print", index + 1)
                    self._error_message = Message(
                        i18n_catalog.i18nc("@info:status", "Unable to start a new print job. No PrinterCore loaded in slot {0}".format(index + 1)))
                    self._error_message.show()
                    return
                if self._json_printer_state["heads"][0]["extruders"][index]["active_material"]["GUID"] == "":
                    Logger.log("e", "No material loaded in slot %s, unable to start print", index + 1)
                    self._error_message = Message(
                        i18n_catalog.i18nc("@info:status",
                                           "Unable to start a new print job. No material loaded in slot {0}".format(index + 1)))
                    self._error_message.show()
                    return

        warnings = []  # There might be multiple things wrong. Keep a list of all the stuff we need to warn about.

        for index in range(0, self._num_extruders):
            # Check if there is enough material. Any failure in these results in a warning.
            material_length = self._json_printer_state["heads"][0]["extruders"][index]["active_material"]["length_remaining"]
            if material_length != -1 and print_information.materialLengths[index] > material_length:
                Logger.log("w", "Printer reports that there is not enough material left for extruder %s. We need %s and the printer has %s", index + 1, print_information.materialLengths[index], material_length)
                warnings.append(i18n_catalog.i18nc("@label", "Not enough material for spool {0}.").format(index+1))

            # Check if the right cartridges are loaded. Any failure in these results in a warning.
            extruder_manager = cura.Settings.ExtruderManager.getInstance()
            if print_information.materialLengths[index] != 0:
                variant = extruder_manager.getExtruderStack(index).findContainer({"type": "variant"})
                core_name = self._json_printer_state["heads"][0]["extruders"][index]["hotend"]["id"]
                if variant:
                    if variant.getName() != core_name:
                        Logger.log("w", "Extruder %s has a different Cartridge (%s) as Cura (%s)", index + 1, core_name, variant.getName())
                        warnings.append(i18n_catalog.i18nc("@label", "Different PrintCore (Cura: {0}, Printer: {1}) selected for extruder {2}".format(variant.getName(), core_name, index + 1)))

                material = extruder_manager.getExtruderStack(index).findContainer({"type": "material"})
                if material:
                    remote_material_guid = self._json_printer_state["heads"][0]["extruders"][index]["active_material"]["GUID"]
                    if material.getMetaDataEntry("GUID") != remote_material_guid:
                        Logger.log("w", "Extruder %s has a different material (%s) as Cura (%s)", index + 1,
                                   remote_material_guid,
                                   material.getMetaDataEntry("GUID"))

                        remote_materials = UM.Settings.ContainerRegistry.getInstance().findInstanceContainers(type = "material", GUID = remote_material_guid, read_only = True)
                        remote_material_name = "Unknown"
                        if remote_materials:
                            remote_material_name = remote_materials[0].getName()
                        warnings.append(i18n_catalog.i18nc("@label", "Different material (Cura: {0}, Printer: {1}) selected for extruder {2}").format(material.getName(), remote_material_name, index + 1))

        if warnings:
            text = i18n_catalog.i18nc("@label", "Are you sure you wish to print with the selected configuration?")
            informative_text = i18n_catalog.i18nc("@label", "There is a mismatch between the configuration of the printer and Cura. "
                                                "For the best result, always slice for the PrintCores and materials that are inserted in your printer.")
            detailed_text = ""
            for warning in warnings:
                detailed_text += warning + "\n"

            Application.getInstance().messageBox(i18n_catalog.i18nc("@window:title", "Mismatched configuration"),
                                                 text,
                                                 informative_text,
                                                 detailed_text,
                                                 buttons=QMessageBox.Yes + QMessageBox.No,
                                                 icon=QMessageBox.Question,
                                                 callback=self._configurationMismatchMessageCallback
                                                 )
            return

        self.startPrint()

    def _configurationMismatchMessageCallback(self, button):
        if button == QMessageBox.Yes:
            self.startPrint()
        else:
            Application.getInstance().showPrintMonitor.emit(False)

    def isConnected(self):
        return self._connection_state != ConnectionState.closed and self._connection_state != ConnectionState.error

    ##  Start requesting data from printer
    def connect(self):
        self.close()  # Ensure that previous connection (if any) is killed.

        self._createNetworkManager()

        self.setConnectionState(ConnectionState.connecting)
        self._update()  # Manually trigger the first update, as we don't want to wait a few secs before it starts.
        self._update_camera()
        Logger.log("d", "Connection with printer %s with ip %s started", self._key, self._address)

        ## Check if this machine was authenticated before.
        self._authentication_id = Application.getInstance().getGlobalContainerStack().getMetaDataEntry("network_authentication_id", None)
        self._authentication_key = Application.getInstance().getGlobalContainerStack().getMetaDataEntry("network_authentication_key", None)

        self._update_timer.start()
        self._camera_timer.start()

    ##  Stop requesting data from printer
    def disconnect(self):
        Logger.log("d", "Connection with printer %s with ip %s stopped", self._key, self._address)
        self.close()

    newImage = pyqtSignal()

    @pyqtProperty(QUrl, notify = newImage)
    def cameraImage(self):
        self._camera_image_id += 1
        # There is an image provider that is called "camera". In order to ensure that the image qml object, that
        # requires a QUrl to function, updates correctly we add an increasing number. This causes to see the QUrl
        # as new (instead of relying on cached version and thus forces an update.
        temp = "image://camera/" + str(self._camera_image_id)
        return QUrl(temp, QUrl.TolerantMode)

    def getCameraImage(self):
        return self._camera_image

    def _setJobState(self, job_state):
        self._last_command = job_state
        url = QUrl("http://" + self._address + self._api_prefix + "print_job/state")
        put_request = QNetworkRequest(url)
        put_request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        data = "{\"target\": \"%s\"}" % job_state
        self._manager.put(put_request, data.encode())

    ##  Convenience function to get the username from the OS.
    #   The code was copied from the getpass module, as we try to use as little dependencies as possible.
    def _getUserName(self):
        for name in ("LOGNAME", "USER", "LNAME", "USERNAME"):
            user = os.environ.get(name)
            if user:
                return user
        return "Unknown User"  # Couldn't find out username.

    def _progressMessageActionTrigger(self, message_id = None, action_id = None):
        if action_id == "Abort":
            Logger.log("d", "User aborted sending print to remote.")
            self._progress_message.hide()
            self._compressing_print = False
            if self._post_reply:
                self._post_reply.abort()
                self._post_reply = None
            Application.getInstance().showPrintMonitor.emit(False)

    ##  Attempt to start a new print.
    #   This function can fail to actually start a print due to not being authenticated or another print already
    #   being in progress.
    def startPrint(self):
        try:
            self._send_gcode_start = time()
            self._progress_message = Message(i18n_catalog.i18nc("@info:status", "Sending data to printer"), 0, False, -1)
            self._progress_message.addAction("Abort", i18n_catalog.i18nc("@action:button", "Cancel"), None, "")
            self._progress_message.actionTriggered.connect(self._progressMessageActionTrigger)
            self._progress_message.show()
            Logger.log("d", "Started sending g-code to remote printer.")
            self._compressing_print = True
            ## Mash the data into single string
            byte_array_file_data = b""
            for line in self._gcode:
                if not self._compressing_print:
                    self._progress_message.hide()
                    return  # Stop trying to zip, abort was called.
                if self._use_gzip:
                    byte_array_file_data += gzip.compress(line.encode("utf-8"))
                    QCoreApplication.processEvents()  # Ensure that the GUI does not freeze.
                    # Pretend that this is a response, as zipping might take a bit of time.
                    self._last_response_time = time()
                else:
                    byte_array_file_data += line.encode("utf-8")

            if self._use_gzip:
                file_name = "%s.gcode.gz" % Application.getInstance().getPrintInformation().jobName
            else:
                file_name = "%s.gcode" % Application.getInstance().getPrintInformation().jobName

            self._compressing_print = False
            ##  Create multi_part request
            self._post_multi_part = QHttpMultiPart(QHttpMultiPart.FormDataType)

            ##  Create part (to be placed inside multipart)
            self._post_part = QHttpPart()
            self._post_part.setHeader(QNetworkRequest.ContentDispositionHeader,
                           "form-data; name=\"file\"; filename=\"%s\"" % file_name)
            self._post_part.setBody(byte_array_file_data)
            self._post_multi_part.append(self._post_part)

            url = QUrl("http://" + self._address + self._api_prefix + "print_job")

            ##  Create the QT request
            self._post_request = QNetworkRequest(url)

            ##  Post request + data
            self._post_reply = self._manager.post(self._post_request, self._post_multi_part)
            self._post_reply.uploadProgress.connect(self._onUploadProgress)

        except IOError:
            self._progress_message.hide()
            self._error_message = Message(i18n_catalog.i18nc("@info:status", "Unable to send data to printer. Is another job still active?"))
            self._error_message.show()
        except Exception as e:
            self._progress_message.hide()
            Logger.log("e", "An exception occurred in network connection: %s" % str(e))

    ##  Verify if we are authenticated to make requests.
    def _verifyAuthentication(self):
        url = QUrl("http://" + self._address + self._api_prefix + "auth/verify")
        request = QNetworkRequest(url)
        self._manager.get(request)

    ##  Check if the authentication request was allowed by the printer.
    def _checkAuthentication(self):
        Logger.log("d", "Checking if authentication is correct.")
        self._manager.get(QNetworkRequest(QUrl("http://" + self._address + self._api_prefix + "auth/check/" + str(self._authentication_id))))

    ##  Request a authentication key from the printer so we can be authenticated
    def _requestAuthentication(self):
        url = QUrl("http://" + self._address + self._api_prefix + "auth/request")
        request = QNetworkRequest(url)
        request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        self.setAuthenticationState(AuthState.AuthenticationRequested)
        self._manager.post(request, json.dumps({"application": "Cura-" + Application.getInstance().getVersion(), "user": self._getUserName()}).encode())

    ##  Send all material profiles to the printer.
    def sendMaterialProfiles(self):
        for container in UM.Settings.ContainerRegistry.getInstance().findInstanceContainers(type = "material"):
            try:
                xml_data = container.serialize()
                if xml_data == "" or xml_data is None:
                    continue
                material_multi_part = QHttpMultiPart(QHttpMultiPart.FormDataType)

                material_part = QHttpPart()
                file_name = "none.xml"
                material_part.setHeader(QNetworkRequest.ContentDispositionHeader, "form-data; name=\"file\";filename=\"%s\"" % file_name)
                material_part.setBody(xml_data.encode())
                material_multi_part.append(material_part)
                url = QUrl("http://" + self._address + self._api_prefix + "materials")
                material_post_request = QNetworkRequest(url)
                reply = self._manager.post(material_post_request, material_multi_part)

                # Keep reference to material_part, material_multi_part and reply so the garbage collector won't touch them.
                self._material_post_objects[id(reply)] = (material_part, material_multi_part, reply)
            except NotImplementedError:
                # If the material container is not the most "generic" one it can't be serialized an will raise a
                # NotImplementedError. We can simply ignore these.
                pass

    ##  Handler for all requests that have finished.
    def _onFinished(self, reply):
        if reply.error() == QNetworkReply.TimeoutError:
            Logger.log("w", "Received a timeout on a request to the printer")
            self._connection_state_before_timeout = self._connection_state
            # Check if we were uploading something. Abort if this is the case.
            # Some operating systems handle this themselves, others give weird issues.
            if self._post_reply:
                self._post_reply.abort()
                self._post_reply.uploadProgress.disconnect(self._onUploadProgress)
                Logger.log("d", "Uploading of print failed after %s", time() - self._send_gcode_start)
                self._post_reply = None
                self._progress_message.hide()

            self.setConnectionState(ConnectionState.error)
            return

        if self._connection_state_before_timeout and reply.error() == QNetworkReply.NoError:  # There was a timeout, but we got a correct answer again.
            Logger.log("d", "We got a response (%s) from the server after %0.1f of silence. Going back to previous state %s", reply.url().toString(), time() - self._last_response_time, self._connection_state_before_timeout)
            self.setConnectionState(self._connection_state_before_timeout)
            self._connection_state_before_timeout = None

        if reply.error() == QNetworkReply.NoError:
            self._last_response_time = time()

        status_code = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        if not status_code:
            if self._connection_state != ConnectionState.error:
                Logger.log("d", "A reply from %s did not have status code.", reply.url().toString())
            # Received no or empty reply
            return
        reply_url = reply.url().toString()

        if reply.operation() == QNetworkAccessManager.GetOperation:
            if "printer" in reply_url:  # Status update from printer.
                if status_code == 200:
                    if self._connection_state == ConnectionState.connecting:
                        self.setConnectionState(ConnectionState.connected)
                    self._json_printer_state = json.loads(bytes(reply.readAll()).decode("utf-8"))
                    self._spliceJSONData()

                    # Hide connection error message if the connection was restored
                    if self._connection_message:
                        self._connection_message.hide()
                        self._connection_message = None
                else:
                    Logger.log("w", "We got an unexpected status (%s) while requesting printer state", status_code)
                    pass  # TODO: Handle errors
            elif "print_job" in reply_url:  # Status update from print_job:
                if status_code == 200:
                    json_data = json.loads(bytes(reply.readAll()).decode("utf-8"))
                    progress = json_data["progress"]
                    ## If progress is 0 add a bit so another print can't be sent.
                    if progress == 0:
                        progress += 0.001
                    elif progress == 1:
                        self._print_finished = True
                    else:
                        self._print_finished = False
                    self.setProgress(progress * 100)

                    state = json_data["state"]

                    # There is a short period after aborting or finishing a print where the printer
                    # reports a "none" state (but the printer is not ready to receive a print)
                    # If this happens before the print has reached progress == 1, the print has
                    # been aborted.
                    if state == "none" or state == "":
                        if self._last_command == "abort":
                            self.setErrorText(i18n_catalog.i18nc("@label:MonitorStatus", "Aborting print..."))
                            state = "error"
                        else:
                            state = "printing"
                    if state == "wait_cleanup" and self._last_command == "abort":
                        # Keep showing the "aborted" error state until after the buildplate has been cleaned
                        self.setErrorText(i18n_catalog.i18nc("@label:MonitorStatus", "Print aborted. Please check the printer"))
                        state = "error"

                    # NB/TODO: the following two states are intentionally added for future proofing the i18n strings
                    #          but are currently non-functional
                    if state == "!pausing":
                        self.setErrorText(i18n_catalog.i18nc("@label:MonitorStatus", "Pausing print..."))
                    if state == "!resuming":
                        self.setErrorText(i18n_catalog.i18nc("@label:MonitorStatus", "Resuming print..."))

                    self._updateJobState(state)
                    self.setTimeElapsed(json_data["time_elapsed"])
                    self.setTimeTotal(json_data["time_total"])
                    self.setJobName(json_data["name"])
                elif status_code == 404:
                    self.setProgress(0)  # No print job found, so there can't be progress or other data.
                    self._updateJobState("")
                    self.setErrorText("")
                    self.setTimeElapsed(0)
                    self.setTimeTotal(0)
                    self.setJobName("")
                else:
                    Logger.log("w", "We got an unexpected status (%s) while requesting print job state", status_code)
            elif "snapshot" in reply_url:  # Status update from image:
                if status_code == 200:
                    self._camera_image.loadFromData(reply.readAll())
                    self.newImage.emit()
            elif "auth/verify" in reply_url:  # Answer when requesting authentication
                if status_code == 401:
                    if self._authentication_state != AuthState.AuthenticationRequested:
                        # Only request a new authentication when we have not already done so.
                        Logger.log("i", "Not authenticated. Attempting to request authentication")
                        self._requestAuthentication()
                elif status_code == 403:
                    # If we already had an auth (eg; didn't request one), we only need a single 403 to see it as denied.
                    if self._authentication_state != AuthState.AuthenticationRequested:
                        Logger.log("d", "While trying to verify the authentication state, we got a forbidden response. Our own auth state was %s", self._authentication_state)
                        self.setAuthenticationState(AuthState.AuthenticationDenied)
                elif status_code == 200:
                    self.setAuthenticationState(AuthState.Authenticated)
                    global_container_stack = Application.getInstance().getGlobalContainerStack()
                    ## Save authentication details.
                    if global_container_stack:
                        if "network_authentication_key" in global_container_stack.getMetaData():
                            global_container_stack.setMetaDataEntry("network_authentication_key", self._authentication_key)
                        else:
                            global_container_stack.addMetaDataEntry("network_authentication_key", self._authentication_key)
                        if "network_authentication_id" in global_container_stack.getMetaData():
                            global_container_stack.setMetaDataEntry("network_authentication_id", self._authentication_id)
                        else:
                            global_container_stack.addMetaDataEntry("network_authentication_id", self._authentication_id)
                    Application.getInstance().saveStack(global_container_stack)  # Force save so we are sure the data is not lost.
                    Logger.log("i", "Authentication succeeded")
                else:  # Got a response that we didn't expect, so something went wrong.
                    Logger.log("w", "While trying to authenticate, we got an unexpected response: %s", reply.attribute(QNetworkRequest.HttpStatusCodeAttribute))
                    self.setAuthenticationState(AuthState.NotAuthenticated)

            elif "auth/check" in reply_url:  # Check if we are authenticated (user can refuse this!)
                data = json.loads(bytes(reply.readAll()).decode("utf-8"))
                if data.get("message", "") == "authorized":
                    Logger.log("i", "Authentication was approved")
                    self._verifyAuthentication()  # Ensure that the verification is really used and correct.
                elif data.get("message", "") == "unauthorized":
                    Logger.log("i", "Authentication was denied.")
                    self.setAuthenticationState(AuthState.AuthenticationDenied)
                else:
                    pass

        elif reply.operation() == QNetworkAccessManager.PostOperation:
            if "/auth/request" in reply_url:
                # We got a response to requesting authentication.
                data = json.loads(bytes(reply.readAll()).decode("utf-8"))

                global_container_stack = Application.getInstance().getGlobalContainerStack()
                if global_container_stack:  # Remove any old data.
                    global_container_stack.removeMetaDataEntry("network_authentication_key")
                    global_container_stack.removeMetaDataEntry("network_authentication_id")
                    Application.getInstance().saveStack(global_container_stack)  # Force saving so we don't keep wrong auth data.

                self._authentication_key = data["key"]
                self._authentication_id = data["id"]
                Logger.log("i", "Got a new authentication ID. Waiting for authorization: %s", self._authentication_id )

                # Check if the authentication is accepted.
                self._checkAuthentication()
            elif "materials" in reply_url:
                # Remove cached post request items.
                del self._material_post_objects[id(reply)]
            elif "print_job" in reply_url:
                reply.uploadProgress.disconnect(self._onUploadProgress)
                Logger.log("d", "Uploading of print succeeded after %s", time() - self._send_gcode_start)
                # Only reset the _post_reply if it was the same one.
                if reply == self._post_reply:
                    self._post_reply = None
                self._progress_message.hide()

        elif reply.operation() == QNetworkAccessManager.PutOperation:
            if status_code == 204:
                pass  # Request was successful!
            else:
                Logger.log("d", "Something went wrong when trying to update data of API (%s). Message: %s Statuscode: %s", reply_url, reply.readAll(), status_code)
        else:
            Logger.log("d", "NetworkPrinterOutputDevice got an unhandled operation %s", reply.operation())

    def _onUploadProgress(self, bytes_sent, bytes_total):
        if bytes_total > 0:
            new_progress = bytes_sent / bytes_total * 100
            # Treat upload progress as response. Uploading can take more than 10 seconds, so if we don't, we can get
            # timeout responses if this happens.
            self._last_response_time = time()
            if new_progress > self._progress_message.getProgress():
                self._progress_message.show()  # Ensure that the message is visible.
                self._progress_message.setProgress(bytes_sent / bytes_total * 100)
        else:
            self._progress_message.setProgress(0)
            self._progress_message.hide()

    ##  Let the user decide if the hotends and/or material should be synced with the printer
    def materialHotendChangedMessage(self, callback):
        Application.getInstance().messageBox(i18n_catalog.i18nc("@window:title", "Changes on the Printer"),
            i18n_catalog.i18nc("@label",
                "Do you want to change the PrintCores and materials in Cura to match your printer?"),
            i18n_catalog.i18nc("@label",
                "The PrintCores and/or materials on your printer were changed. For the best result, always slice for the PrintCores and materials that are inserted in your printer."),
            buttons=QMessageBox.Yes + QMessageBox.No,
            icon=QMessageBox.Question,
            callback=callback
        )

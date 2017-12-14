"""
Support for the Apple's Find My iPhone API platform.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/device_tracker.fmip/
"""

import json
import base64
import urllib.request
import logging
import random
import os

import voluptuous as vol

from homeassistant.config import load_yaml_config_file
from homeassistant.components.device_tracker import PLATFORM_SCHEMA, DOMAIN
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import track_utc_time_change
import homeassistant.util.dt as dt_util
from homeassistant.util import slugify

_LOGGER = logging.getLogger(__name__)

CONF_UPDATE_INTERVAL = 'update_interval'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_USERNAME): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
    vol.Optional(CONF_UPDATE_INTERVAL, default=5): cv.positive_int,
})

# Dictionary to hold multiple iCloudDeviceScanner objects
# This is needed for play_sound service.
ICLOUDDEVICES = {}


def setup_scanner(hass, config, see, discovery_info=None):
    """Validate the configuration and return an iCloudDevice scanner."""
    ICLOUDDEVICES[config.get(CONF_USERNAME)] \
        = ICloudDeviceScanner(hass, config, see)

    descriptions = load_yaml_config_file(
        os.path.join(os.path.dirname(__file__), 'services.yaml'))

    def play_sound(call):
        """
        Play the FMIP alert on an iDevice.

        This method requires a username and a list of device_ids ie ['']
        which means you can play alerts on multiple devices owned by username.
        """
        username = call.data.get('username', None)
        device_ids = call.data.get('device_ids', None)

        for device in device_ids:
            ICLOUDDEVICES[username].api.play_sound(device)

    hass.services.register(DOMAIN, 'fmip_play_sound', play_sound,
                           description=descriptions.get(
                               'fmip_play_sound'))

    def force_username_update(call):
        """
        Update all iDevice locations for a specific username.

        This method allows you to force a device update
        without waiting for the device scanner to run.
        """
        username = call.data.get('username', None)

        ICLOUDDEVICES[username].run_scanner("FORCE UPDATE", True)

    hass.services.register(DOMAIN, 'fmip_force_username_update',
                           force_username_update,
                           description=descriptions.get(
                               'fmip_force_username_update'))

    def force_update(call):
        """
        Update all iDevice locations for all usernames.

        This method allows you to force a device update
        without waiting for the device scanner to run.
        """
        for username in ICLOUDDEVICES:
            ICLOUDDEVICES[username].run_scanner("FORCE UPDATE", True)

    hass.services.register(DOMAIN, 'fmip_force_update',
                           force_update, description=descriptions.get(
                               'fmip_force_update'))

    def update_scan_interval(call):
        """
        Update device scan interval for a specific username.

        This method allows you to change the device scan interval for
        a specific username.
        """
        username = call.data.get('username', None)
        update_interval = call.data.get('update_interval', None)

        ICLOUDDEVICES[username].update_scan_interval(update_interval)

    hass.services.register(DOMAIN, 'fmip_update_scan_interval',
                           update_scan_interval,
                           description=descriptions.get(
                               'fmip_update_scan_interval'))

    return True


class ICloudDeviceScanner(object):
    """A class representing an iCloudDevice scanner."""

    def __init__(self, hass, config, see):
        """Initialise the iCloudDevice scanner."""
        self.hass = hass
        self._username = config.get(CONF_USERNAME)
        self._api = FmipApi(self._username,
                            config.get(CONF_PASSWORD))
        self.see = see

        self.update_interval = config.get(CONF_UPDATE_INTERVAL)
        self.run_scanner(force=True)

        track_utc_time_change(self.hass, self.run_scanner,
                              second=random.randint(10, 59))

    @property
    def api(self):
        """Return API object."""
        return self._api

    @property
    def update_interval(self):
        """Return update_interval attribute."""
        return self._update_interval

    # Define a setter for update_interval to make sure
    # the data type is an int and set to 1 or above.
    @update_interval.setter
    def update_interval(self, interval):
        interval = int(interval)

        if interval < 1:
            interval = 1

        self._update_interval = interval

    def run_scanner(self, now=None, force=False):
        """Scan and update device data."""
        self._update_device_data(now, force)

    def update_scan_interval(self, update_interval=None):
        """Update the scan interval of the scanner."""
        self.update_interval = update_interval

        _LOGGER.info("%s - Scan interval updated to every %s minutes",
                     self._username, self.update_interval)

    def _update_device_data(self, now=None, force=False):
        """Update device info."""
        update_interval = self.update_interval
        current_min = (dt_util.now().hour * 60 + dt_util.now().minute)

        if (((current_min % update_interval == 0) is True) or
           (force is True)):

            _LOGGER.info("%s - Updating devices %s", self._username,
                         dt_util.now())

            # Run update_device_data method.
            self.api.update_device_data()

            for device in self.api.device_list:
                device_data = device.get_data()

                device_device_display_name = \
                    device_data['device_display_name']
                device_latitude = device_data['latitude']
                device_longitude = device_data['longitude']

                # Add update_interval to device attributes.
                device_data.update({'update_interval': self.update_interval})

                # Make sure the device_id is "slugified".
                device_id = slugify(device_device_display_name)

                # This is the most important line - update the see object.
                self.see(
                    dev_id=device_id, gps=(device_latitude, device_longitude),
                    attributes=device_data
                )


class IDevice(object):
    """A class representing an iDevice object."""

    def __init__(self, _username, _device_id, _name, _device_display_name,
                 _battery_level, _battery_status, _latitude, _longitude,
                 _device_status):
        """Initialize the IDevice object."""
        self._username = _username
        self._device_id = _device_id
        self._name = _name
        self._device_display_name = _device_display_name
        self._battery_level = _battery_level
        self._battery_status = _battery_status
        self._latitude = _latitude
        self._longitude = _longitude
        self._device_status = _device_status

        # Make sure we capture when the iDevice object was created.
        self._last_update = dt_util.now()

        self.device_status_codes = {
            '200': 'online',
            '201': 'offline',
            '203': 'pending',
            '204': 'unregistered',
        }

    @property
    def username(self):
        """Return username."""
        return self._username

    @property
    def device_id(self):
        """Return device_id."""
        return self._device_id

    @property
    def name(self):
        """Return name without special characters."""
        return self._name.encode('ascii', 'ignore').decode('utf-8')

    @property
    def device_display_name(self):
        """Return device_display_name."""
        return self._device_display_name

    @property
    def battery_level(self):
        """Return battery_level as a %."""
        return "{:.0f}".format(self._battery_level * 100)

    @property
    def battery_status(self):
        """Return battery_status."""
        return self._battery_status

    @property
    def latitude(self):
        """Return latitude to 4 decimal places."""
        return "{:.4f}".format(self._latitude)

    @property
    def longitude(self):
        """Return longitude to 4 decimal places."""
        return "{:.4f}".format(self._longitude)

    @property
    def device_status(self):
        """Return device_status based on defined device_status_codes."""
        return self.device_status_codes.get(self._device_status)

    @property
    def last_update(self):
        """Return when the record was last updated."""
        return self._last_update

    def get_data(self):
        """Return an iDevice object as a dictionary."""
        return {'username': self.username,
                'device_id': self.device_id,
                'name': self.name,
                'device_display_name': self.device_display_name,
                'battery_level': self.battery_level,
                'battery_status': self.battery_status,
                'latitude': self.latitude,
                'longitude': self.longitude,
                'device_status': self.device_status,
                'last_update': self.last_update}


class FmipApi(object):
    """A class representing the FMIP API."""

    def __init__(self, username, password):
        """Initialize the FmipAPI with username and password."""
        self._username = username
        self._password = password
        self._auth_type = 'UserIDGuest'
        self._base64_auth = self._return_base64_auth(self._username,
                                                     self._password)

        self.fmip_headers = {
            'X-Apple-Realm-Support': '1.0',
            'Authorization': 'Basic %s' % self._base64_auth,
            'X-Apple-Find-API-Ver': '3.0',
            'X-Apple-AuthScheme': '%s' % self._auth_type,
            'User-Agent': "FindMyiPhone/500 CFNetwork/758.4.3 Darwin/15.5.0"
        }

        # URL to query device information.
        self._device_url = \
            'https://fmipmobile.icloud.com/fmipservice/device/ \
            %s/initClient' % self._username

        # URL to send FMIP alert to a device.
        self._device_play_sound_url = \
            'https://fmipmobile.icloud.com/fmipservice/device/ \
            %s/playSound' % self._username

        # Object to store json response data from _http_post.
        self._fmip_device_data = None
        # Object to store a list of iDevice objects.
        self._fmip_device_list = None

        self.update_device_data()

    def update_device_data(self):
        """Update device data."""
        self._fmip_device_data = self._return_fmip_device_data(
            self._device_url)
        self._fmip_device_list = self._return_fmip_device_list(
            self._fmip_device_data)

    @property
    def device_list(self):
        """Return a list of iDevice objects."""
        return self._fmip_device_list

    def play_sound(self, *args):
        """Send a FMIP alert to iDevice."""
        for device_id in args:

            json_data = {
                'device': device_id,
            }

            self._http_post(self._device_play_sound_url, json_data)

    def _http_post(self, url, json_data=None):
        if json_data is not None:
            json_data = json.dumps(json_data).encode('utf-8')

        request = urllib.request.Request(url, json_data, self.fmip_headers)
        request.get_method = lambda: 'POST'

        try:
            response = urllib.request.urlopen(request)
            http_response = (json.loads(response.read().decode('utf-8')))

            return json.dumps(http_response)

        except urllib.request.HTTPError as error:
            if error.code == 401:
                # print("Authorization Error 401. Try credentials again.")
                _LOGGER.error("Authorization Error 401. \
                              Try credentials again.")
            if error.code == 403:
                pass  # Can ignore.
            raise error

    def _return_fmip_device_data(self, url):
        return self._http_post(url)

    def _return_fmip_device_list(self, json_data):
        device_list = []  # This list will hold idevice object(s).
        json_data = json.loads(json_data)

        for device in json_data['content']:
            try:
                device_username = self._username
                device_device_id = device['id']
                device_name = device['name']
                device_device_display_name = device['deviceDisplayName']
                device_battery_level = device['batteryLevel']
                device_battery_status = device['batteryStatus']
                device_latitude = device['location']['latitude']
                device_longitude = device['location']['longitude']
                device_device_status = device['deviceStatus']

                device = IDevice(device_username,
                                 device_device_id, device_name,
                                 device_device_display_name,
                                 device_battery_level, device_battery_status,
                                 device_latitude, device_longitude,
                                 device_device_status)

                device_list.append(device)

            # Pass on devices that dont contain the right
            # json_data['content']
            except Exception as error:
                _LOGGER.debug("Account - %s", device_username)
                _LOGGER.debug("Device  - %s", device_device_display_name)
                _LOGGER.debug("Unable to find %s record", error)
                _LOGGER.debug("Skipping... device entry")

        return device_list

    def _return_base64_auth(self, username, password):
        string_to_encode = "{}:{}".format(username, password)
        return base64.b64encode(bytes(string_to_encode,
                                      'utf-8')).decode('utf-8')

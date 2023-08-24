#!/usr/bin/env python3
"""Start the LNXlink service"""

import os
import time
import json
import threading
import logging
import argparse
import platform
import subprocess
import importlib.metadata
import ssl

import yaml
import distro
import paho.mqtt.client as mqtt
from . import modules
from . import config_setup
from .system_monitor import MonitorSuspend, GracefulKiller

version = importlib.metadata.version(__package__ or __name__)
logger = logging.getLogger("lnxlink")


class LNXlink:
    """Start LNXlink service that loads all modules and connects to MQTT"""

    def __init__(self, config_path):

        logger.info("LNXlink %s started: %s", version, platform.python_version())
        self.kill = False

        # Read configuration from yaml file
        self.pref_topic = "lnxlink"
        self.config = self.read_config(config_path)

        # Run each addon included in the modules folder
        self.addons = {}
        conf_modules = self.config.get("modules", None)
        custom_modules = self.config.get("custom_modules", None)
        conf_exclude = self.config.get("exclude", [])
        conf_exclude = [] if conf_exclude is None else conf_exclude
        loaded_modules = modules.parse_modules(
            conf_modules, custom_modules, conf_exclude
        )
        for _, addon in loaded_modules.items():
            try:
                tmp_addon = addon(self)
                self.addons[addon.service] = tmp_addon
            except Exception as err:
                logger.error(
                    "Error with addon %s, please remove it from your config: %s",
                    addon.service,
                    err,
                )

        # Setup MQTT
        self.client = mqtt.Client()
        self.setup_mqtt()

    def subprocess(self, command):
        """Global subprocess command"""
        result = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=3,
        )
        stdout = result.stdout.decode("UTF-8")
        returncode = result.returncode
        return stdout, returncode

    def publish_monitor_data(self, topic, pub_data):
        """Publish info data to mqtt in the correct format"""
        # logger.info(topic, pub_data, type(pub_data))
        if pub_data is None:
            return
        if isinstance(pub_data, bool):
            if pub_data is True:
                pub_data = "ON"
            if pub_data is False:
                pub_data = "OFF"
        if isinstance(pub_data, dict):
            if all(v is None for v in pub_data.values()):
                return
        if isinstance(pub_data, list):
            if all(v is None for v in pub_data):
                return
        if pub_data is None:
            return
        if isinstance(pub_data, (dict, list)):
            pub_data = json.dumps(pub_data)
        self.client.publish(
            topic, payload=pub_data, retain=self.config["mqtt"]["lwt"]["retain"]
        )

    def monitor_run(self):
        """Gets information from each Addon and sends it to MQTT"""
        for service, addon in self.addons.items():
            if hasattr(addon, "get_old_info") or hasattr(addon, "get_info"):
                try:
                    subtopic = addon.name.lower().replace(" ", "_")
                    if hasattr(addon, "get_old_info"):
                        topic = f"{self.pref_topic}/monitor_old/{subtopic}"
                        pub_data = addon.get_old_info()
                        self.publish_monitor_data(topic, pub_data)
                    if hasattr(addon, "get_info"):
                        topic = f"{self.pref_topic}/monitor_controls/{subtopic}"
                        pub_data = addon.get_info()
                        self.publish_monitor_data(topic, pub_data)
                except Exception as err:
                    logger.error("Error with addon %s: %s", service, err)

    def monitor_run_thread(self):
        """Runs method to get sensor information every prespecified interval"""
        self.monitor_run()

        interval = self.config.get("update_interval", 1)
        if not self.kill:
            monitor = threading.Timer(interval, self.monitor_run_thread)
            monitor.start()

    def setup_mqtt(self):
        """Creates the mqtt object"""
        self.client = mqtt.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

        self.client.username_pw_set(
            self.config["mqtt"]["auth"]["user"], self.config["mqtt"]["auth"]["pass"]
        )
        if self.config["mqtt"]["auth"].get("tls", False):
            self.client.tls_set(
                certfile=None, keyfile=None, cert_reqs=ssl.CERT_REQUIRED
            )
        self.client.connect(
            self.config["mqtt"]["server"], self.config["mqtt"]["port"], 60
        )
        self.client.loop_start()

    def read_config(self, config_path):
        """Reads the config file and prepares module names for import"""
        with open(config_path, "r", encoding="utf8") as file:
            conf = yaml.load(file, Loader=yaml.FullLoader)

        if "prefix" in conf["mqtt"] and "clientId" in conf["mqtt"]:
            self.pref_topic = f"{conf['mqtt']['prefix']}/{conf['mqtt']['clientId']}"
        self.pref_topic = self.pref_topic.lower()

        conf["modules"] = conf.get("modules")
        conf["custom_modules"] = conf.get("custom_modules")
        if conf["modules"] is not None:
            conf["modules"] = [x.lower().replace("-", "_") for x in conf["modules"]]
        return conf

    def on_connect(self, client, userdata, flags, rcode):
        """Callback for MQTT connect which reports the connection status
        back to MQTT server"""
        logger.info("Connected to MQTT with code %s", rcode)
        client.subscribe(f"{self.pref_topic}/commands/#")
        if self.config["mqtt"]["lwt"]["enabled"]:
            self.client.publish(
                f"{self.pref_topic}/lwt",
                payload=self.config["mqtt"]["lwt"]["connectMsg"],
                qos=self.config["mqtt"]["lwt"]["qos"],
                retain=self.config["mqtt"]["lwt"]["retain"],
            )
        if self.config["mqtt"]["discovery"]["enabled"]:
            self.setup_discovery()
        self.kill = False
        self.monitor_run_thread()

    def disconnect(self, *args):
        """Reports to MQTT server that the service has stopped"""
        logger.info("Disconnected from MQTT.")
        if self.config["mqtt"]["lwt"]["enabled"]:
            self.client.publish(
                f"{self.pref_topic}/lwt",
                payload=self.config["mqtt"]["lwt"]["disconnectMsg"],
                qos=self.config["mqtt"]["lwt"]["qos"],
                retain=self.config["mqtt"]["lwt"]["retain"],
            )
        self.kill = True
        self.client.disconnect()

    def temp_connection_callback(self, status):
        """Report the connection status to MQTT server"""
        self.kill = True
        if self.config["mqtt"]["lwt"]["enabled"]:
            if status:
                logger.info("Power Down detected.")
                self.client.publish(
                    f"{self.pref_topic}/lwt",
                    payload=self.config["mqtt"]["lwt"]["disconnectMsg"],
                    qos=self.config["mqtt"]["lwt"]["qos"],
                    retain=self.config["mqtt"]["lwt"]["retain"],
                )
            else:
                logger.info("Power Up detected.")
                if self.kill:
                    self.kill = False
                    self.monitor_run_thread()
                self.client.publish(
                    f"{self.pref_topic}/lwt",
                    payload=self.config["mqtt"]["lwt"]["connectMsg"],
                    qos=self.config["mqtt"]["lwt"]["qos"],
                    retain=self.config["mqtt"]["lwt"]["retain"],
                )

    def on_message(self, client, userdata, msg):
        """MQTT message is received with a module command to excecute"""
        topic = msg.topic.replace(f"{self.pref_topic}/commands/", "")
        message = msg.payload
        logger.info("Message received %s: %s", topic, message)
        try:
            message = json.loads(message)
        except Exception as err:
            message = message.decode()
            logger.debug("String could not be converted to JSON: %s", err)

        select_service = topic.split("/")
        addon = self.addons.get(select_service[0])
        if addon is not None:
            if hasattr(addon, "start_control"):
                try:
                    result = addon.start_control(select_service, message)
                    if result is not None:
                        result_topic = (
                            f"{self.pref_topic}/command_result/{topic.strip('/')}"
                        )
                        self.client.publish(result_topic, payload=result, retain=False)
                    self.monitor_run()
                except Exception as err:
                    logger.error(err)

    def setup_discovery_entities_old(self, discovery_template, addon, service):
        """Send discovery information on Home Assistant for sensors"""
        subtopic = addon.name.lower().replace(" ", "_")
        state_topic = f"{self.pref_topic}/monitor_old/{subtopic}"

        discovery = discovery_template.copy()
        discovery["name"] = f"{self.config['mqtt']['clientId']} {addon.name}"
        discovery["unique_id"] = f"{self.config['mqtt']['clientId']}_{service}"
        discovery["state_topic"] = state_topic
        discovery["topic"] = state_topic
        if addon.get_old_info.__annotations__.get("return") == dict:
            discovery["value_template"] = "{{ value_json.status }}"
            discovery["json_attributes_topic"] = state_topic
            discovery["json_attributes_template"] = "{{ value_json | tojson }}"
        if hasattr(addon, "icon"):
            discovery["icon"] = addon.icon
        if hasattr(addon, "unit"):
            discovery["unit_of_measurement"] = addon.unit
        if hasattr(addon, "title"):
            discovery["title"] = addon.title
        if hasattr(addon, "entity_picture"):
            discovery["entity_picture"] = addon.entity_picture
        if hasattr(addon, "device_class"):
            discovery["device_class"] = addon.device_class
        if hasattr(addon, "state_class"):
            discovery["state_class"] = addon.state_class

        sensor_type = getattr(addon, "sensor_type", None)
        if sensor_type in ["sensor", "binary_sensor"]:
            discovery["expire_after"] = self.config.get("update_interval", 5) * 5
        if sensor_type is not None:
            self.client.publish(
                f"homeassistant/{sensor_type}/lnxlink/{discovery['unique_id']}/config",
                payload=json.dumps(discovery),
                retain=self.config["mqtt"]["lwt"]["retain"],
            )

    def setup_discovery_entities(
        self, discovery_template, addon, service, control_name, options
    ):
        """Send discovery information on Home Assistant for controls"""
        control_name_topic = control_name.lower().replace(" ", "_")
        subtopic = addon.name.lower().replace(" ", "_")
        unique_id = f"{self.config['mqtt']['clientId']}_{control_name_topic}"
        state_topic = f"{self.pref_topic}/monitor_controls/{subtopic}"
        command_topic = f"{self.pref_topic}/commands/{service}/{control_name_topic}/"

        lookup_options = {
            "value_template": {
                "value_template": options.get("value_template", ""),
                "json_attributes_topic": state_topic,
                "json_attributes_template": options.get(
                    "attributes_template", "{{ value_json | tojson }}"
                ),
            },
            "icon": {"icon": options.get("icon", "")},
            "unit": {"unit_of_measurement": options.get("unit", "")},
            "title": {"title": options.get("title", "")},
            "entity_picture": {"entity_picture": options.get("entity_picture", "")},
            "device_class": {"device_class": options.get("device_class", "")},
            "state_class": {"state_class": options.get("state_class", "")},
            "entity_category": {"entity_category": options.get("entity_category", "")},
            "enabled": {"enabled_by_default": options.get("enabled", True)},
        }
        lookup_entities = {
            "sensor": {
                "state_topic": state_topic,
                "expire_after": self.config.get("update_interval", 5) * 2,
            },
            "binary_sensor": {
                "state_topic": state_topic,
                "expire_after": self.config.get("update_interval", 5) * 2,
            },
            "camera": {"state_topic": state_topic},
            "update": {"state_topic": state_topic},
            "button": {"command_topic": command_topic},
            "switch": {
                "state_topic": state_topic,
                "command_topic": command_topic,
                "payload_off": "OFF",
                "payload_on": "ON",
            },
            "text": {
                "state_topic": state_topic,
                "command_topic": command_topic,
            },
            "number": {
                "state_topic": state_topic,
                "command_topic": command_topic,
                "min": options.get("min", 1),
                "max": options.get("max", 100),
                "step": options.get("step", 1),
            },
            "select": {
                "state_topic": state_topic,
                "command_topic": command_topic,
                "options": options.get("options", []),
            },
        }
        discovery = discovery_template.copy()
        discovery["name"] = f"{self.config['mqtt']['clientId']} {control_name}"
        discovery["unique_id"] = unique_id
        discovery.update(lookup_entities.get(options["type"], {}))
        for option in options:
            discovery.update(lookup_options.get(option, {}))

        if options["type"] not in lookup_entities:
            logger.error("Not supported: %s", options["type"])
            return
        if "value_template" in discovery and options["type"] == "camera":
            del discovery["json_attributes_topic"]
            del discovery["json_attributes_template"]
        self.client.publish(
            f"homeassistant/{options['type']}/lnxlink/{discovery['unique_id']}/config",
            payload=json.dumps(discovery),
            retain=self.config["mqtt"]["lwt"]["retain"],
        )

    def setup_discovery(self):
        """First time setup of discovery for Home Assistant"""
        discovery_template = {
            "availability": {
                "topic": f"{self.pref_topic}/lwt",
                "payload_available": "ON",
                "payload_not_available": "OFF",
            },
            "device": {
                "identifiers": [self.config["mqtt"]["clientId"]],
                "name": self.config["mqtt"]["clientId"],
                "model": f"{distro.name()} {distro.version()}",
                "manufacturer": "LNXlink",
                "sw_version": version,
            },
        }
        for service, addon in self.addons.items():
            if hasattr(addon, "get_old_info"):
                try:
                    self.setup_discovery_entities_old(
                        discovery_template, addon, service
                    )
                except Exception as err:
                    logger.error(err)
            if hasattr(addon, "exposed_controls"):
                for control_name, options in addon.exposed_controls().items():
                    try:
                        self.setup_discovery_entities(
                            discovery_template, addon, service, control_name, options
                        )
                    except Exception as err:
                        logger.error(err)


def setup_logger(config_path):
    """Save logs on the same directory as the config file"""
    config_dir = os.path.dirname(os.path.realpath(config_path))
    print(config_dir)
    logging.basicConfig(level=logging.INFO)
    start_sec = str(int(time.time()))[-4:]
    log_formatter = logging.Formatter(
        "%(asctime)s ["
        + start_sec
        + ":%(threadName)s.%(module)s.%(funcName)s.%(lineno)d] [%(levelname)s]  %(message)s"
    )

    file_handler = logging.FileHandler(f"{config_dir}/lnxlink.log")
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)


def main():
    """Starts the app with some arguments"""
    parser = argparse.ArgumentParser(
        prog="LNXlink", description="Send system information to MQTT broker"
    )
    parser.add_argument(
        "-c",
        "--config",
        help="Configuration file",
        default="/etc/config.yaml",
        required=True,
    )
    args = parser.parse_args()

    config_file = os.path.abspath(args.config)
    setup_logger(config_file)
    config_setup.setup_config(config_file)
    config_setup.setup_systemd(config_file)
    lnxlink = LNXlink(config_file)

    # Monitor for system changes (Shutdown/Suspend/Sleep)
    monitor_suspend = MonitorSuspend(lnxlink.temp_connection_callback)
    monitor_suspend.start()
    monitor_gracefulkiller = GracefulKiller(lnxlink.temp_connection_callback)
    while not monitor_gracefulkiller.kill_now:
        time.sleep(0.2)
    monitor_suspend.stop()
    lnxlink.disconnect()


if __name__ == "__main__":
    main()

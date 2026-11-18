"""MQTT 구독·발행 (paho-mqtt 2.x, CallbackAPIVersion.VERSION2)"""
import json
import logging

import paho.mqtt.client as mqtt

from . import config

log = logging.getLogger("mqtt")


class MqttBridge:
    def __init__(self, on_message):
        # on_message(topic, payload_dict): 메시지를 받을 때마다 호출 (MQTT 스레드에서 실행됨)
        self._handler = on_message
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="petwash-server")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        self.connected = False

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        log.warning("MQTT 연결 끊김: %s (자동 재연결 시도)", reason_code)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT 연결 실패: %s", reason_code)
            return
        self.connected = True
        log.info("MQTT 연결됨")
        # 재연결될 때도 다시 구독하도록 on_connect 안에서 구독
        for topic in (config.TOPIC_SENSOR, config.TOPIC_CAMERA, config.TOPIC_STATUS):
            client.subscribe(topic, qos=1)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.warning("JSON이 아닌 메시지 무시: %s", msg.topic)
            return
        try:
            self._handler(msg.topic, payload)
        except Exception:
            log.exception("메시지 처리 중 오류: %s", msg.topic)

    def start(self):
        self.client.connect_async(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
        self.client.loop_start()  # 별도 스레드에서 네트워크 처리, 끊기면 자동 재연결

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

    def publish(self, topic: str, payload: dict, retain: bool = False):
        data = json.dumps(payload, ensure_ascii=False)
        return self.client.publish(topic, data, qos=1, retain=retain)

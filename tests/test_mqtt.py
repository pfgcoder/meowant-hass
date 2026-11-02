from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock


MQTT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "ubpet" / "mqtt.py"
spec = importlib.util.spec_from_file_location("ubpet_mqtt", MQTT_PATH)
mqtt = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = mqtt
spec.loader.exec_module(mqtt)


def build_receipt_im_message(cid: str, status: str, cause: str = "") -> bytes:
    receipt = mqtt.field_string(1, status) + mqtt.field_string(2, cause)
    receipt_any = mqtt.encode_any(mqtt.ANY_RECEIPT_NOTIFICATION_TYPE_URL, receipt)
    message_content = mqtt.encode_message_content("receipt", receipt_any)
    return b"".join(
        [
            mqtt.field_string(1, cid),
            mqtt.field_string(2, "100"),
            mqtt.field_varint(3, 1700000000123),
            mqtt.field_string(4, "device-uid"),
            mqtt.field_string(5, "account-uid"),
            mqtt.field_varint(6, 2),
            mqtt.field_bytes(7, message_content),
            mqtt.field_string(8, mqtt.MESSAGE_PROTOCOL_VERSION),
        ]
    )


class MqttCodecUnitTests(unittest.TestCase):
    def test_operation_payload_hex_matches_recovered_handoff(self):
        parts = mqtt.build_payload_parts(kind="operation", op_mode=1, op_state=1, seq=1)

        self.assertEqual(parts.op_body.hex(), "08011001")
        self.assertEqual(parts.risp_frame.hex(), "db0b080210fb072004280138018b08011001de")
        self.assertEqual(parts.command_proto.hex(), "08021213db0b080210fb072004280138018b08011001de")

    def test_all_state_payload_hex_matches_recovered_handoff(self):
        parts = mqtt.build_payload_parts(kind="all-state", seq=1)

        self.assertEqual(parts.op_body.hex(), "")
        self.assertEqual(parts.risp_frame.hex(), "db09080210fd0728013801f6")
        self.assertEqual(parts.command_proto.hex(), "0802120cdb09080210fd0728013801f6")

    def test_all_state_response_decodes_app_state_names(self):
        decoded = mqtt.summarize_all_state_body(bytes.fromhex("185a9001b6beafd106"))

        self.assertEqual(decoded["w_mode"], 0)
        self.assertEqual(decoded["w_mode_name"], "idle")
        self.assertEqual(decoded["w_mode_app_name"], "IDLE")
        self.assertEqual(decoded["w_state"], 0)
        self.assertEqual(decoded["w_state_name"], "idle")
        self.assertEqual(decoded["w_state_app_name"], "PENDING")
        self.assertEqual(decoded["w_cause"], 90)
        self.assertEqual(decoded["timestamp"], 1781260086)

    def test_idle_mode_normalizes_running_state_for_sc01(self):
        decoded = mqtt.summarize_all_state_body(bytes.fromhex("1001185a9001f9cb84d306"))

        self.assertEqual(decoded["w_mode"], 0)
        self.assertEqual(decoded["w_state"], 1)
        self.assertEqual(decoded["w_state_name"], "running")
        self.assertEqual(decoded["w_state_app_name"], "PENDING")

    def test_idle_mode_keeps_blocked_state(self):
        # w_mode 0 (omitted), w_state 2: idle with a cat in the box.
        decoded = mqtt.summarize_all_state_body(bytes.fromhex("1002"))

        self.assertEqual(decoded["w_mode"], 0)
        self.assertEqual(decoded["w_state_app_name"], "BLOCKED")

    def test_other_idle_states_read_as_pending(self):
        # w_state 3 (paused) and an unknown 4 in idle mode: nothing to resume, so ready to start.
        for body in ("1003", "1004"):
            with self.subTest(body=body):
                self.assertEqual(mqtt.summarize_all_state_body(bytes.fromhex(body))["w_state_app_name"], "PENDING")

        # Paused while cleaning keeps its name, so the resume button works.
        self.assertEqual(mqtt.summarize_all_state_body(bytes.fromhex("08021003"))["w_state_app_name"], "PAUSED")


    def test_service_ids_map_to_app_operation_ordinals(self):
        services = mqtt.SERVICE_ID_MAP

        self.assertEqual(services["start_clean_up"]["op_mode_value"], 1)
        self.assertEqual(services["start_clean_up"]["op_state_value"], 1)
        self.assertEqual(services["pause_clean_up"]["op_mode_value"], 1)
        self.assertEqual(services["pause_clean_up"]["op_state_value"], 2)
        self.assertEqual(services["resume_clean_up"]["op_mode_value"], 1)
        self.assertEqual(services["resume_clean_up"]["op_state_value"], 3)
        self.assertEqual(services["start_flatten"]["op_mode_value"], 3)
        self.assertEqual(services["start_flatten"]["op_state_value"], 1)
        self.assertEqual(services["pause_flatten"]["op_mode_value"], 3)
        self.assertEqual(services["pause_flatten"]["op_state_value"], 2)
        self.assertEqual(services["resume_flatten"]["op_mode_value"], 3)
        self.assertEqual(services["resume_flatten"]["op_state_value"], 3)
        self.assertEqual(services["start_rise"]["op_mode_value"], 7)
        self.assertEqual(services["start_rise"]["op_state_value"], 1)
        self.assertEqual(services["start_drop"]["op_mode_value"], 8)
        self.assertEqual(services["start_drop"]["op_state_value"], 1)
        self.assertNotIn("start_clean_up_enum1", services)
        self.assertNotIn("pause_clean_up_enum1", services)
        self.assertNotIn("start_exchange_sand", services)
        self.assertNotIn("pause_rise", services)
        self.assertNotIn("resume_rise", services)
        self.assertNotIn("pause_drop", services)
        self.assertNotIn("resume_drop", services)
        self.assertNotIn("start_clean_up_mode2_state2", services)
        self.assertNotIn("start_clean_up_mode2_state1", services)
        self.assertNotIn("request_state_gid100", services)
        self.assertNotIn("start_clean_up_gid100", services)

    def test_resolve_service_payload_builds_expected_clean_command(self):
        self.assertEqual(mqtt.resolve_service_payload("start_clean_up", seq=1).op_body.hex(), "08011001")
        self.assertEqual(mqtt.resolve_service_payload("pause_clean_up", seq=1).op_body.hex(), "08011002")
        self.assertEqual(mqtt.resolve_service_payload("resume_clean_up", seq=1).op_body.hex(), "08011003")
        self.assertEqual(
            mqtt.resolve_service_payload("start_clean_up", seq=1).risp_frame.hex(),
            "db0b080210fb072004280138018b08011001de",
        )

    def test_resolve_service_payload_builds_expected_rake_commands(self):
        self.assertEqual(mqtt.resolve_service_payload("start_rise", seq=1).op_body.hex(), "08071001")
        self.assertEqual(mqtt.resolve_service_payload("start_drop", seq=1).op_body.hex(), "08081001")

    def test_resolve_service_payload_builds_expected_flatten_command(self):
        self.assertEqual(mqtt.resolve_service_payload("start_flatten", seq=1).op_body.hex(), "08031001")
        self.assertEqual(mqtt.resolve_service_payload("pause_flatten", seq=1).op_body.hex(), "08031002")
        self.assertEqual(mqtt.resolve_service_payload("resume_flatten", seq=1).op_body.hex(), "08031003")

    def test_im_message_wraps_command_proto_when_uids_are_supplied(self):
        parts = mqtt.build_payload_parts(
            kind="all-state",
            seq=1,
            from_uid="account-uid",
            to_uid="device-uid",
            cid="cid-1",
            gid="100",
            created_at_ms=1700000000123,
        )

        self.assertIsNotNone(parts.im_message)
        self.assertIn(parts.command_proto.hex(), parts.im_message.hex())
        self.assertIn(b"account-uid", parts.im_message)
        self.assertIn(b"device-uid", parts.im_message)

    def test_generated_cid_matches_app_timestamp_plus_suffix_shape(self):
        with (
            mock.patch.object(mqtt.time, "time", return_value=1700000000.123),
            mock.patch.object(mqtt.random, "random", return_value=0.234567),
        ):
            parts = mqtt.build_payload_parts(
                kind="all-state",
                seq=1,
                from_uid="account-uid",
                to_uid="device-uid",
            )

        self.assertEqual(parts.cid, "1700000000123+311110")

    def test_generated_gid_matches_app_timestamp_shape(self):
        mqtt._GID_COUNTER = 0

        self.assertEqual(mqtt.generate_gid(1780900866300), "1867409906781388801")

    def test_generate_gid_increments_timestamp_shape_counter(self):
        mqtt._GID_COUNTER = 0

        with mock.patch.object(mqtt.time, "time", return_value=1780900866.300):
            first = mqtt.generate_gid()
            second = mqtt.generate_gid()

        self.assertEqual(first, "1867409906781388801")
        self.assertEqual(second, "1867409906781388802")


    def test_decode_mqtt_payload_summarizes_receipt(self):
        decoded = mqtt.decode_mqtt_payload_safely(build_receipt_im_message("cid-1", "receipt/success"))

        self.assertEqual(decoded["cid"], "cid-1")
        self.assertEqual(decoded["from_uid"], "device-uid")
        self.assertEqual(decoded["to_uid"], "account-uid")
        self.assertEqual(decoded["content_type"], "receipt")
        self.assertEqual(decoded["receipt_status"], "receipt/success")


    # Push decoding, ported from bojanrajkovic/upet-hass tests/test_mqtt.py (ae7233b, 57ef90e, b0edf9b).

    def test_state_push_decodes_like_all_state_response(self):
        # Captured from a real box as a cat stepped in: block, with a cat on the scale.
        frame = mqtt.build_risp_frame(
            dev=2, cmd=mqtt.RISP_CMD_STATE_PUSH, attr=6, seq=65, body=bytes.fromhex("1002185a20013001900193a3d6d606")
        )

        decoded = mqtt.summarize_risp_frame(frame)

        self.assertEqual(decoded["risp_cmd"], 1024)
        self.assertEqual(decoded["w_state_name"], "block")
        self.assertEqual(decoded["toilet_state"], 1)
        self.assertEqual(decoded["weigh_state"], 1)
        self.assertEqual(decoded["timestamp"], 1792381331)

    def test_online_status_notification_decodes_status_and_uid(self):
        status = mqtt.field_string(1, "status/offline") + mqtt.field_string(2, "1001") + mqtt.field_string(3, "status/offline")
        any_value = mqtt.encode_any(mqtt.ANY_ONLINE_STATUS_TYPE_URL, status)
        message = mqtt.field_string(4, "1001") + mqtt.field_string(5, "1001") + mqtt.field_bytes(7, mqtt.encode_message_content("status", any_value))

        decoded = mqtt.summarize_im_message(message)

        self.assertEqual(decoded["content_type"], "status")
        self.assertEqual(decoded["online_status"], "status/offline")
        self.assertEqual(decoded["status_uid"], "1001")

    def test_classify_push_routes_each_message_kind(self):
        boxes = {"2001": "SN1"}
        cases = [
            ({"type_url": mqtt.ANY_FORCE_OFFLINE_TYPE_URL, "from_uid": "1"}, mqtt.Kicked()),
            ({"type_url": mqtt.ANY_ONLINE_STATUS_TYPE_URL, "online_status": "status/online", "status_uid": "2001"}, mqtt.OnlineChange("SN1", True)),
            ({"type_url": mqtt.ANY_ONLINE_STATUS_TYPE_URL, "online_status": "status/offline", "status_uid": "2001"}, mqtt.OnlineChange("SN1", False)),
            ({"type_url": mqtt.ANY_ONLINE_STATUS_TYPE_URL, "online_status": "status/online", "status_uid": "9999"}, mqtt.Ignored("unknown status")),
            ({"from_uid": "2001", "risp_cmd": mqtt.RISP_CMD_RECORD_PUSH}, mqtt.NewRecord("SN1")),
            ({"from_uid": "2001", "risp_cmd": mqtt.RISP_CMD_CONFIG_REQUEST}, mqtt.ConfigChanged("SN1")),
            ({"from_uid": "9999", "risp_cmd": mqtt.RISP_CMD_RECORD_PUSH}, mqtt.Ignored("unknown sender")),
            ({"from_uid": "2001", "risp_cmd": mqtt.RISP_CMD_OPERATION}, mqtt.Ignored("unhandled message")),
            ({"from_uid": "2001", "content_type": "receipt"}, mqtt.Ignored("receipt")),
            ({"from_uid": "1", "content_type": "receipt"}, mqtt.Ignored("receipt")),
        ]
        for decoded, expected in cases:
            with self.subTest(decoded=decoded):
                self.assertEqual(mqtt.classify_push(decoded, boxes), expected)

    def test_classify_push_returns_state_for_pushes_and_replies(self):
        for cmd in (mqtt.RISP_CMD_STATE_PUSH, mqtt.RISP_CMD_ALL_STATE_RESPONSE):
            decoded = {"from_uid": "2001", "risp_cmd": cmd, "w_state": 2}
            with self.subTest(cmd=cmd):
                self.assertEqual(mqtt.classify_push(decoded, {"2001": "SN1"}), mqtt.StatePush("SN1", decoded))

    def test_mqtt_host_from_base_url_strips_scheme_and_path(self):
        self.assertEqual(mqtt.mqtt_host_from_base_url("https://apis.airrobo-home.com/foo"), "apis.airrobo-home.com")
        self.assertEqual(mqtt.mqtt_host_from_base_url("apis.airrobo-home.com/"), "apis.airrobo-home.com")


if __name__ == "__main__":
    unittest.main()

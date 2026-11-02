from __future__ import annotations

from dataclasses import dataclass
import random
import time
from typing import Any
from urllib.parse import urlparse

MESSAGE_PROTOCOL_VERSION = "1.0.1"
ANY_BYTES_VALUE_TYPE_URL = "type.googleapis.com/google.protobuf.BytesValue"
ANY_RECEIPT_NOTIFICATION_TYPE_URL = "type.googleapis.com/rosa.im.protobuf.ReceiptNotification"
ANY_ONLINE_STATUS_TYPE_URL = "type.googleapis.com/rosa.im.protobuf.OnlineStatusNotification"
ANY_FORCE_OFFLINE_TYPE_URL = "type.googleapis.com/rosa.im.protobuf.ForceOfflineNotification"
DEFAULT_MQTT_PORT = 21000
DEFAULT_MQTT_KEEPALIVE = 20
RISP_SEQ_MAX = 0x7FFF
COMMAND_PROTO_PROTYPE_RISP = 2

RISP_START_BYTE = 0xDB
RISP_DEV_AIRPET = 0x02
RISP_CMD_OPERATION = 0x3FB
RISP_CMD_ALL_STATE = 0x3FD
RISP_CMD_ALL_STATE_RESPONSE = 0x3FD
# Unsolicited pushes from the box (bojanrajkovic/upet-hass ae7233b, b0edf9b): a new visit or
# device record, a work-state change, and the box asking the cloud for its settings, which it
# does after any settings change (d2c2535_intelligent_dev_config_info_get_rq).
RISP_CMD_RECORD_PUSH = 0x3FF
RISP_CMD_STATE_PUSH = 0x400
RISP_CMD_CONFIG_REQUEST = 0x9E7
RISP_ATTR_REQUEST = 1
RISP_HEADER_FIELD_NAMES = {
    1: "dev",
    2: "cmd",
    3: "id",
    4: "dataLen",
    5: "attr",
    6: "ack",
    7: "seq",
}
ALL_STATE_FIELD_NAMES = {
    1: "w_mode",
    2: "w_state",
    3: "w_cause",
    4: "toilet_state",
    5: "foreign_state",
    6: "weigh_state",
    7: "lid_state",
    8: "motor_state",
    9: "key_state",
    10: "object_state",
    11: "camera_state",
    12: "image_ctrl_state",
    13: "pic_ctrl_state",
    14: "update_state",
    15: "hall_state",
    16: "infrared_state",
    17: "box_state",
    18: "timestamp",
    19: "weigh_sensor",
    20: "led_state",
}
# Every field summarize_all_state_body can give, the box's whole state.
STATE_FIELD_NAMES = frozenset(ALL_STATE_FIELD_NAMES.values()) | {
    "w_mode_name",
    "w_mode_app_name",
    "w_state_name",
    "w_state_app_name",
}
WORK_MODE_NAMES = {
    0: "idle",
    1: "reset",
    2: "clean",
    3: "smooth",
    4: "clear",
    5: "rakeup",
}
WORK_MODE_APP_NAMES = {
    0: "IDLE",
    1: "RESETTING",
    2: "CLEANING",
    3: "SMOOTHING",
    4: "REPLACING",
    5: "RAKING_UP",
}
WORK_STATE_NAMES = {
    0: "idle",
    1: "running",
    2: "block",
    3: "paused",
}
WORK_STATE_APP_NAMES = {
    0: "PENDING",
    1: "RUNNING",
    2: "BLOCKED",
    3: "PAUSED",
}

OP_MODE_VALUES = {
    "clean": 1,
    "smooth": 3,
    "rise": 7,
    "drop": 8,
}
OP_STATE_VALUES = {
    "start": 1,
    "pause": 2,
    "resume": 3,
}
WORK_TYPE_TO_OP_MODE = {
    "clean-up": "clean",
    "flatten": "smooth",
    "rise": "rise",
    "drop": "drop",
}
WORK_ACTION_TO_OP_STATE = {
    "start": "start",
}


@dataclass(frozen=True, slots=True)
class PayloadParts:
    op_body: bytes
    risp_frame: bytes
    command_proto: bytes
    im_message: bytes | None
    cid: str | None
    gid: str | None
    created_at_ms: int | None
    seq: int
    dev: int
    cmd: int
    received_messages: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "cid": self.cid,
            "gid": self.gid,
            "created_at_ms": self.created_at_ms,
            "seq": self.seq,
            "dev": self.dev,
            "cmd": self.cmd,
            "op_body_hex": self.op_body.hex(),
            "risp_frame_hex": self.risp_frame.hex(),
            "command_proto_hex": self.command_proto.hex(),
            "im_message_hex": self.im_message.hex() if self.im_message is not None else None,
            "received_count": len(self.received_messages),
            "received_messages": list(self.received_messages),
        }


def service_id_for_work_command(work_type: str, work_action: str) -> str:
    return f"{work_action}_{work_type.replace('-', '_')}"


def service_id_map() -> dict[str, dict[str, Any]]:
    services: dict[str, dict[str, Any]] = {
        "request_state": {
            "service": "request_state",
            "kind": "all-state",
            "read_only": True,
        }
    }
    for work_type, op_mode_name in WORK_TYPE_TO_OP_MODE.items():
        for work_action, op_state_name in WORK_ACTION_TO_OP_STATE.items():
            service = service_id_for_work_command(work_type, work_action)
            services[service] = {
                "service": service,
                "kind": "operation",
                "work_type": work_type,
                "work_action": work_action,
                "op_mode": op_mode_name,
                "op_mode_value": OP_MODE_VALUES[op_mode_name],
                "op_state": op_state_name,
                "op_state_value": OP_STATE_VALUES[op_state_name],
            }
    for work_type, op_mode_name in {"clean-up": "clean", "flatten": "smooth"}.items():
        for work_action in ("pause", "resume"):
            service = service_id_for_work_command(work_type, work_action)
            services[service] = {
                "service": service,
                "kind": "operation",
                "work_type": work_type,
                "work_action": work_action,
                "op_mode": op_mode_name,
                "op_mode_value": OP_MODE_VALUES[op_mode_name],
                "op_state": work_action,
                "op_state_value": OP_STATE_VALUES[work_action],
            }
    return services


SERVICE_ID_MAP = service_id_map()
_NEXT_SEQ_VALUE = random.randint(1, 0x2710)
_GID_COUNTER = random.randint(0, 0xFFFFF)


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint value must be non-negative")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def field_varint(number: int, value: int, *, omit_zero: bool = True) -> bytes:
    if omit_zero and value == 0:
        return b""
    return encode_varint((number << 3) | 0) + encode_varint(value)


def field_bytes(number: int, value: bytes, *, omit_empty: bool = True) -> bytes:
    if omit_empty and not value:
        return b""
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def field_string(number: int, value: str, *, omit_empty: bool = True) -> bytes:
    return field_bytes(number, value.encode("utf-8"), omit_empty=omit_empty)


def crc8_app(data: bytes) -> int:
    """Native libcrclib model: CRC-8/poly 0x07/init 0/xorout 0x55."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc ^ 0x55


def encode_airpet_operation(op_mode: int, op_state: int) -> bytes:
    return field_varint(1, op_mode) + field_varint(2, op_state)


def encode_risp_header(*, dev: int, cmd: int, attr: int, seq: int, data_len: int) -> bytes:
    return b"".join(
        [
            field_varint(1, dev),
            field_varint(2, cmd),
            field_varint(4, data_len),
            field_varint(5, attr),
            field_varint(7, seq),
        ]
    )


def build_risp_frame(*, dev: int, cmd: int, attr: int, seq: int, body: bytes) -> bytes:
    header = encode_risp_header(dev=dev, cmd=cmd, attr=attr, seq=seq, data_len=len(body))
    if len(header) > 0xFF:
        raise ValueError("RISP header is too large for one-byte header length")
    frame = bytearray([RISP_START_BYTE, len(header)])
    frame += header
    frame.append(crc8_app(header))
    if body:
        frame += body
        frame.append(crc8_app(body))
    return bytes(frame)


def encode_command_proto(risp_frame: bytes) -> bytes:
    return field_varint(1, COMMAND_PROTO_PROTYPE_RISP) + field_bytes(2, risp_frame)


def encode_bytes_value(value: bytes) -> bytes:
    return field_bytes(1, value)


def encode_any(type_url: str, value: bytes) -> bytes:
    return field_string(1, type_url) + field_bytes(2, value)


def encode_message_content(content_type: str, content_any: bytes) -> bytes:
    return field_string(1, content_type) + field_bytes(2, content_any)


def encode_im_message(
    *,
    command_proto: bytes,
    from_uid: str,
    to_uid: str,
    cid: str,
    gid: str,
    created_at_ms: int,
) -> bytes:
    bytes_value = encode_bytes_value(command_proto)
    any_value = encode_any(ANY_BYTES_VALUE_TYPE_URL, bytes_value)
    message_content = encode_message_content("custom", any_value)
    return b"".join(
        [
            field_string(1, cid),
            field_string(2, gid),
            field_varint(3, created_at_ms),
            field_string(4, from_uid),
            field_string(5, to_uid),
            field_bytes(7, message_content),
            field_string(8, MESSAGE_PROTOCOL_VERSION),
        ]
    )


def build_payload_parts(
    *,
    kind: str,
    op_mode: int = 0,
    op_state: int = 0,
    from_uid: str | None = None,
    to_uid: str | None = None,
    seq: int | None = None,
    cid: str | None = None,
    gid: str | None = None,
    created_at_ms: int | None = None,
) -> PayloadParts:
    chosen_seq = seq if seq is not None else next_seq()
    if kind == "operation":
        body = encode_airpet_operation(op_mode, op_state)
        cmd = RISP_CMD_OPERATION
    elif kind == "all-state":
        body = b""
        cmd = RISP_CMD_ALL_STATE
    else:
        raise ValueError(f"unsupported payload kind: {kind}")

    risp_frame = build_risp_frame(dev=RISP_DEV_AIRPET, cmd=cmd, attr=RISP_ATTR_REQUEST, seq=chosen_seq, body=body)
    command_proto = encode_command_proto(risp_frame)
    im_message = None
    chosen_cid = cid
    chosen_gid = gid
    chosen_created_at_ms = created_at_ms
    if from_uid is not None or to_uid is not None:
        if not from_uid or not to_uid:
            raise ValueError("from_uid and to_uid must be provided together")
        chosen_cid = chosen_cid or generate_cid()
        chosen_created_at_ms = chosen_created_at_ms or int(time.time() * 1000)
        chosen_gid = chosen_gid or generate_gid(chosen_created_at_ms)
        im_message = encode_im_message(
            command_proto=command_proto,
            from_uid=from_uid,
            to_uid=to_uid,
            cid=chosen_cid,
            gid=chosen_gid,
            created_at_ms=chosen_created_at_ms,
        )

    return PayloadParts(
        op_body=body,
        risp_frame=risp_frame,
        command_proto=command_proto,
        im_message=im_message,
        cid=chosen_cid,
        gid=chosen_gid,
        created_at_ms=chosen_created_at_ms,
        seq=chosen_seq,
        dev=RISP_DEV_AIRPET,
        cmd=cmd,
    )


def next_seq() -> int:
    global _NEXT_SEQ_VALUE
    seq = _NEXT_SEQ_VALUE
    _NEXT_SEQ_VALUE += 1
    if _NEXT_SEQ_VALUE > RISP_SEQ_MAX:
        _NEXT_SEQ_VALUE = 1
    return seq


def generate_cid() -> str:
    suffix = int(((random.random() * 9) + 1) * 100000)
    return f"{int(time.time() * 1000)}+{suffix}"


def generate_gid(created_at_ms: int | None = None) -> str:
    global _GID_COUNTER
    timestamp_ms = created_at_ms or int(time.time() * 1000)
    _GID_COUNTER = (_GID_COUNTER + 1) & 0xFFFFF
    return str((timestamp_ms << 20) + _GID_COUNTER)


def resolve_service_payload(
    service_id: str,
    *,
    seq: int | None,
    from_uid: str | None = None,
    to_uid: str | None = None,
    gid: str | None = None,
) -> PayloadParts:
    service = SERVICE_ID_MAP.get(service_id)
    if service is None:
        raise ValueError(f"unknown MQTT service id: {service_id}")
    if service["kind"] == "all-state":
        return build_payload_parts(kind="all-state", seq=seq, from_uid=from_uid, to_uid=to_uid, gid=gid)
    return build_payload_parts(
        kind="operation",
        op_mode=int(service["op_mode_value"]),
        op_state=int(service["op_state_value"]),
        seq=seq,
        from_uid=from_uid,
        to_uid=to_uid,
        gid=gid,
    )


def mqtt_host_from_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.hostname:
        return parsed.hostname
    return base_url.replace("https://", "").replace("http://", "").strip("/")


@dataclass(frozen=True, slots=True)
class Kicked:
    """Another login on the account took over this IM session."""


@dataclass(frozen=True, slots=True)
class StatePush:
    serial: str
    state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NewRecord:
    """The box wrote a visit or device record; fetch it over REST."""

    serial: str


@dataclass(frozen=True, slots=True)
class ConfigChanged:
    """The box picked up a settings change, from the app, the box itself, or the integration."""

    serial: str


@dataclass(frozen=True, slots=True)
class OnlineChange:
    serial: str
    online: bool


@dataclass(frozen=True, slots=True)
class Ignored:
    reason: str


PushMessage = Kicked | StatePush | NewRecord | ConfigChanged | OnlineChange | Ignored


def classify_push(decoded: dict[str, Any], uid_to_serial: dict[str, str]) -> PushMessage:
    """Decide what a decoded IM message means for the integration (bojanrajkovic/upet-hass 57ef90e).

    `uid_to_serial` maps each box's IM user id to its serial number. Messages from anything
    else, such as command receipts, are ignored, so only a known box can set a box's state.
    """
    if decoded.get("content_type") == "receipt":
        return Ignored("receipt")
    type_url = decoded.get("type_url")
    if type_url == ANY_FORCE_OFFLINE_TYPE_URL:
        return Kicked()
    if type_url == ANY_ONLINE_STATUS_TYPE_URL:
        serial = uid_to_serial.get(str(decoded.get("status_uid")))
        status = decoded.get("online_status")
        if serial is None or status not in ("status/online", "status/offline"):
            return Ignored("unknown status")
        return OnlineChange(serial, status == "status/online")
    serial = uid_to_serial.get(str(decoded.get("from_uid")))
    if serial is None:
        return Ignored("unknown sender")
    cmd = decoded.get("risp_cmd")
    if cmd in (RISP_CMD_STATE_PUSH, RISP_CMD_ALL_STATE_RESPONSE):
        return StatePush(serial, decoded)
    if cmd == RISP_CMD_RECORD_PUSH:
        return NewRecord(serial)
    if cmd == RISP_CMD_CONFIG_REQUEST:
        return ConfigChanged(serial)
    return Ignored("unhandled message")


def decode_mqtt_payload_safely(payload: bytes) -> dict[str, Any]:
    try:
        return summarize_im_message(payload)
    except (ValueError, RuntimeError) as err:
        return {"decode_error": str(err)}


def summarize_im_message(payload: bytes) -> dict[str, Any]:
    fields = decode_proto_fields(payload)
    message_content = first_proto_value(fields, 7, 2)
    summary: dict[str, Any] = {
        "cid": first_proto_string(fields, 1),
        "gid": first_proto_string(fields, 2),
        "created_at_ms": first_proto_value(fields, 3, 0),
        "from_uid": first_proto_string(fields, 4),
        "to_uid": first_proto_string(fields, 5),
        "message_type": first_proto_value(fields, 6, 0),
        "protocol_version": first_proto_string(fields, 8),
    }
    if isinstance(message_content, bytes):
        summary.update(summarize_message_content(message_content))
    return summary


def summarize_message_content(payload: bytes) -> dict[str, Any]:
    fields = decode_proto_fields(payload)
    content_any = first_proto_value(fields, 2, 2)
    summary: dict[str, Any] = {"content_type": first_proto_string(fields, 1)}
    if isinstance(content_any, bytes):
        summary.update(summarize_any(content_any))
    return summary


def summarize_any(payload: bytes) -> dict[str, Any]:
    fields = decode_proto_fields(payload)
    type_url = first_proto_string(fields, 1)
    value = first_proto_value(fields, 2, 2)
    summary: dict[str, Any] = {"type_url": type_url}
    if type_url == ANY_RECEIPT_NOTIFICATION_TYPE_URL and isinstance(value, bytes):
        receipt_fields = decode_proto_fields(value)
        summary["receipt_status"] = first_proto_string(receipt_fields, 1)
        summary["receipt_cause"] = first_proto_string(receipt_fields, 2)
    elif type_url == ANY_ONLINE_STATUS_TYPE_URL and isinstance(value, bytes):
        status_fields = decode_proto_fields(value)
        summary["online_status"] = first_proto_string(status_fields, 1)
        summary["status_uid"] = first_proto_string(status_fields, 2)
    elif type_url == ANY_BYTES_VALUE_TYPE_URL and isinstance(value, bytes):
        bytes_fields = decode_proto_fields(value)
        command_proto = first_proto_value(bytes_fields, 1, 2)
        if isinstance(command_proto, bytes):
            summary.update(summarize_command_proto(command_proto))
    return summary


def summarize_command_proto(payload: bytes) -> dict[str, Any]:
    fields = decode_proto_fields(payload)
    risp_frame = first_proto_value(fields, 2, 2)
    summary: dict[str, Any] = {"command_protype": first_proto_value(fields, 1, 0)}
    if isinstance(risp_frame, bytes):
        summary.update(summarize_risp_frame(risp_frame))
    return summary


def summarize_risp_frame(frame: bytes) -> dict[str, Any]:
    if len(frame) < 3 or frame[0] != RISP_START_BYTE:
        raise ValueError("not a RISP frame")
    header_len = frame[1]
    header_start = 2
    header_end = header_start + header_len
    if header_end >= len(frame):
        raise ValueError("truncated RISP frame")
    header_fields = decode_proto_fields(frame[header_start:header_end])
    values: dict[str, Any] = {}
    for field in header_fields:
        if field["wire_type"] == 0:
            values[RISP_HEADER_FIELD_NAMES.get(field["field"], f"field_{field['field']}")] = field["value"]
    data_len = int(values.get("dataLen", 0) or 0)
    body_start = header_end + 1
    body = frame[body_start : body_start + data_len]
    summary = {
        "risp_cmd": values.get("cmd"),
        "risp_seq": values.get("seq"),
        "risp_attr": values.get("attr"),
        "risp_data_len": data_len,
        "risp_body_hex": body.hex(),
    }
    if values.get("cmd") == RISP_CMD_OPERATION:
        op_fields = decode_proto_fields(body) if body else []
        summary["op_mode"] = first_proto_value(op_fields, 1, 0)
        summary["op_state"] = first_proto_value(op_fields, 2, 0)
    elif values.get("cmd") in (RISP_CMD_ALL_STATE_RESPONSE, RISP_CMD_STATE_PUSH):
        summary.update(summarize_all_state_body(body))
    return summary


def summarize_all_state_body(body: bytes) -> dict[str, Any]:
    fields = decode_proto_fields(body) if body else []
    summary: dict[str, Any] = {}
    for field in fields:
        name = ALL_STATE_FIELD_NAMES.get(field["field"])
        if name and field["wire_type"] == 0:
            summary[name] = field["value"]
    w_mode = summary.setdefault("w_mode", 0)
    w_state = summary.setdefault("w_state", 0)
    if isinstance(w_mode, int):
        summary["w_mode_name"] = WORK_MODE_NAMES.get(w_mode, f"unknown_{w_mode}")
        summary["w_mode_app_name"] = WORK_MODE_APP_NAMES.get(w_mode, f"UNKNOWN_{w_mode}")
    if isinstance(w_state, int):
        summary["w_state_name"] = WORK_STATE_NAMES.get(w_state, f"unknown_{w_state}")
        # Some SC-01 firmware reports IDLE + RUNNING when it is ready. An idle box
        # reads as PENDING whatever its state, so start controls remain available;
        # only BLOCKED (a cat in the box) keeps its own name.
        summary["w_state_app_name"] = (
            "PENDING" if w_mode == 0 and w_state != 2 else WORK_STATE_APP_NAMES.get(w_state, f"UNKNOWN_{w_state}")
        )
    return summary


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated protobuf varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, offset
        shift += 7
        if shift >= 64:
            raise ValueError("protobuf varint is too long")


def decode_proto_fields(data: bytes) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    offset = 0
    while offset < len(data):
        tag, offset = decode_varint(data, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            value, offset = decode_varint(data, offset)
        elif wire_type == 2:
            length, offset = decode_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ValueError("truncated protobuf bytes field")
            value = data[offset:end]
            offset = end
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ValueError("truncated protobuf fixed64 field")
            value = int.from_bytes(data[offset:end], "little")
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ValueError("truncated protobuf fixed32 field")
            value = int.from_bytes(data[offset:end], "little")
            offset = end
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        fields.append({"field": field_number, "wire_type": wire_type, "value": value})
    return fields


def proto_values(fields: list[dict[str, Any]], field_number: int, wire_type: int | None = None) -> list[Any]:
    return [
        field["value"]
        for field in fields
        if field["field"] == field_number and (wire_type is None or field["wire_type"] == wire_type)
    ]


def first_proto_value(fields: list[dict[str, Any]], field_number: int, wire_type: int | None = None) -> Any:
    values = proto_values(fields, field_number, wire_type)
    return values[0] if values else None


def first_proto_string(fields: list[dict[str, Any]], field_number: int) -> str | None:
    value = first_proto_value(fields, field_number, 2)
    if not isinstance(value, bytes):
        return None
    return value.decode("utf-8", errors="replace")

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import random
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "CatLitterBoxOverseasEu/2.1.12 (com.ubtrobot.airpet.overseaseu; build:699; iOS 26.5.0) Alamofire/5.5.0"
NONCE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
# A day's worth of visits (about 15 a day with three cats) and of cleans (about 6), for the
# today counts. Households with more cats can raise it in the integration's options.
DEFAULT_RECORDS_PER_FETCH = 50
# Login reply codes, as the Air Pet app words them. The account was found but the password was
# refused: 2002 and 2006 "account or password incorrect", 2014 "password incorrect", 2342 "locked
# for 10 minutes" after too many.
WRONG_PASSWORD_CODES = frozenset({2002, 2006, 2014, 2342})
# No account in the form tried: 2004 "user does not exist", 2010 and 2042 "account format
# incorrect", 2011 bad phone number, 2019 bad email, and 1009 "parameter error", which an email
# account gets for account type 2.
NO_SUCH_ACCOUNT_CODES = frozenset({1009, 2004, 2010, 2011, 2019, 2042})
_LOGGER = logging.getLogger(__name__)


class UbpetApiError(RuntimeError):
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self.payload = payload
        code = payload.get("code") if isinstance(payload, dict) else None
        message = None
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("msg")
        super().__init__(f"UPET API error status={status} code={code} message={message}")


class UbpetLoginError(UbpetApiError):
    """Logging in failed, so every other call that needs a login would fail the same way."""


class UbpetAuthError(UbpetLoginError):
    """The server refused the login itself (account or password), rather than being unreachable."""


class UbpetPermissionError(UbpetApiError):
    """The account may not make this change.

    A box shared with an account can be run from it, and most settings change from it too, but
    some only change for the box's owner: the child lock came back as code 500, 没有修改权限
    ("no permission to modify"), while auto clean and the waste-bin alert changed fine.
    """


@dataclass(slots=True)
class UbpetAuth:
    token: str
    refresh_token: str | None
    expires_at_ms: int | None
    user_id: int | None


class UbtV2Signer:
    def __init__(self, app_key: str) -> None:
        self.app_key = app_key

    def sign(self, *, device_id: str) -> str:
        timestamp = str(int(time.time()))
        nonce = "".join(random.choice(NONCE_ALPHABET) for _ in range(10))
        preimage = timestamp + self.app_key + nonce + device_id
        digest = hashlib.md5(preimage.encode("utf-8")).hexdigest()
        return f"{digest} {timestamp} {nonce} v2"


class UbpetClient:
    def __init__(
        self,
        *,
        account: str,
        password: str,
        app_key: str,
        device_id: str,
        base_url: str,
        app_id: str,
        product: str,
        area_code: str = "",
        records_per_fetch: int = DEFAULT_RECORDS_PER_FETCH,
    ) -> None:
        self.account = account
        self.password = password
        self.device_id = device_id
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self.product = product
        self.area_code = area_code
        self.records_per_fetch = records_per_fetch
        self.signer = UbtV2Signer(app_key)
        self.auth: UbpetAuth | None = None
        self._auth_lock = threading.Lock()
        self._capabilities: dict[str, dict[str, Any]] = {}

    def ensure_login(self) -> UbpetAuth:
        if auth := self._usable_auth():
            return auth
        with self._auth_lock:
            if auth := self._usable_auth():
                return auth
            _LOGGER.info("UPET auth token is missing or expiring, logging in")
            return self._login_unlocked()

    def _usable_auth(self) -> UbpetAuth | None:
        now_ms = int(time.time() * 1000)
        auth = self.auth
        if auth is None or (auth.expires_at_ms and auth.expires_at_ms - now_ms < 300_000):
            return None
        return auth

    def login(self) -> UbpetAuth:
        with self._auth_lock:
            return self._login_unlocked()

    def _login_unlocked(self) -> UbpetAuth:
        last_error: UbpetApiError | None = None
        no_such_account = True
        for account_type in _account_type_candidates(self.account):
            try:
                _LOGGER.info(
                    "Trying UPET login with endpoint=%s areaCode=%s accountType=%s",
                    self.base_url,
                    self.area_code,
                    account_type,
                )
                return self._login_with_account_type(account_type)
            except UbpetApiError as err:
                _LOGGER.warning(
                    "UPET login failed with endpoint=%s areaCode=%s accountType=%s: %s",
                    self.base_url,
                    self.area_code,
                    account_type,
                    err,
                )
                code = _vendor_code(err)
                if code in WRONG_PASSWORD_CODES:
                    # Callers should ask for a new password: retrying this one, or trying it as
                    # another account type, only counts towards the lock.
                    raise UbpetAuthError(err.status, err.payload) from err
                last_error = err
                no_such_account = no_such_account and code in NO_SUCH_ACCOUNT_CODES
            except OSError as err:
                # The server can't be reached, as any other account type would find. As a login
                # error it ends the update, rather than each remaining call trying to log in again.
                raise UbpetLoginError(0, {"message": f"login failed: {err}"}) from err
        if last_error is not None:
            if no_such_account:
                # No account type found the account, so retrying can't help either.
                raise UbpetAuthError(last_error.status, last_error.payload) from last_error
            # Anything else (a busy server, an HTTP error, a reply without a token) may pass.
            raise UbpetLoginError(last_error.status, last_error.payload) from last_error
        raise RuntimeError("no account type candidates")

    def _refresh_after_unauthorized(self, rejected_token: str) -> UbpetAuth:
        with self._auth_lock:
            current_auth = self._usable_auth()
            if current_auth is not None and current_auth.token != rejected_token:
                return current_auth
            _LOGGER.info("UPET auth token was rejected, logging in again")
            self.auth = None
            return self._login_unlocked()

    def _login_with_account_type(self, account_type: str) -> UbpetAuth:
        payload = {
            "account": self.account,
            "password": _md5_password(self.password),
            "accountType": account_type,
            "areaCode": self.area_code,
        }
        response = self._request("PUT", "/user-service-rest/v2/user/login", payload=payload, auth=False)
        data = _response_data(response)
        token_data = data.get("token") if isinstance(data, dict) else None
        user_data = data.get("user") if isinstance(data, dict) else None
        if not isinstance(token_data, dict) or not token_data.get("token"):
            raise UbpetApiError(200, response)
        self.auth = UbpetAuth(
            token=token_data["token"],
            refresh_token=token_data.get("refreshToken"),
            expires_at_ms=token_data.get("expireAt"),
            user_id=user_data.get("userId") if isinstance(user_data, dict) else None,
        )
        _LOGGER.info("UPET login succeeded with accountType=%s", account_type)
        return self.auth

    def get_devices(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/user-service-rest/v2/robot/common/device/list", auth=True)
        return _require_list(data)

    def get_capabilities(self, serial_number: str) -> dict[str, Any]:
        """Optional features the box has, such as camera-based cat recognition (cat_face_upload).

        They don't change, so each box's are fetched until one call works, then kept.
        Ported from bojanrajkovic/upet-hass (ad084ff).
        """
        if serial_number not in self._capabilities:
            path = f"/catbox-server/box/config/device/capabilities?sn={_quote(serial_number)}"
            self._capabilities[serial_number] = _require_dict(self._request("GET", path, auth=True))
        return self._capabilities[serial_number]

    def get_all_config(self, serial_number: str) -> dict[str, Any]:
        data = self._request(
            "GET",
            f"/catbox-server/box/config/allConfig?serialNumber={_quote(serial_number)}",
            auth=True,
        )
        return _require_dict(data)

    def get_box_use_times(self, serial_number: str) -> dict[str, Any]:
        data = self._request(
            "GET",
            f"/catbox-server/box/config/box-use-times/?serialNumber={_quote(serial_number)}",
            auth=True,
        )
        return _require_dict(data)

    def reset_box_use_times(self, serial_number: str) -> None:
        payload = {
            "boxUseTimes": 0,
            "serialNumber": serial_number,
        }
        self._request("PUT", "/catbox-server/box/config/box-use-times/reset", payload=payload, auth=True)

    def get_deodorant_status(self, serial_number: str) -> dict[str, Any]:
        data = self._request(
            "GET",
            f"/catbox-server/app/deodorant-block/status?serialNumber={_quote(serial_number)}",
            auth=True,
        )
        return _require_dict(data)

    def get_device_online(self, serial_number: str) -> dict[str, Any]:
        data = self._request(
            "GET",
            f"/v1/ubtechinc-im-manager/im/online/device/?sn={_quote(serial_number)}",
            auth=True,
        )
        return _require_dict(data)

    def get_im_credentials(self) -> dict[str, Any]:
        data = self._request("POST", "/v1/ubtechinc-im-manager/im/login", payload={"type": 2}, auth=True)
        return _require_dict(data)

    def get_im_friends(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/v1/ubtechinc-im-manager/im/friends", auth=True)
        return _require_list(data)

    def get_cats(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/catbox-server/web/cat/info", auth=True)
        return _require_list(data)

    def get_box_records(self, serial_number: str, message_type: int, size: int = 1) -> list[dict[str, Any]]:
        payload = {
            "eventTime": "0",
            "serialNumber": serial_number,
            "size": size,
            "messageType": message_type,
        }
        data = self._request("POST", "/catbox-server/web/box/record/new", payload=payload, auth=True)
        # Anything but {"data": {"records": [...]}} (the server can send "data": null) must read as a
        # failed call, not crash the refresh.
        body = data.get("data") if isinstance(data, dict) else None
        records = body.get("records") if isinstance(body, dict) else None
        if not isinstance(records, list):
            raise UbpetApiError(200, data)
        return [record for record in records if isinstance(record, dict)]

    def set_auto_clean_delay(self, serial_number: str, minutes: int, enabled: bool = True) -> None:
        if minutes < 1:
            raise ValueError("auto clean delay must be at least 1 minute")
        # The delay and the auto clean switch are one setting, so send the switch as it is.
        self.set_auto_clean_enabled(serial_number, enabled, minutes * 60)

    def set_auto_clean_enabled(self, serial_number: str, enabled: bool, lazy_time_seconds: int | None) -> None:
        """Set auto clean and its delay, one setting on the box (the delay defaults to 8 minutes)."""
        payload = {
            "serialNumber": serial_number,
            "functionType": 2,
            "controlSwitch": 1 if enabled else 0,
            "optionInt": lazy_time_seconds or 480,
        }
        self._request("PUT", "/catbox-server/box/config/switch/update", payload=payload, auth=True)

    def set_function_switch(self, serial_number: str, function_type: int, enabled: bool) -> None:
        """Toggle one of the box's on/off functions, as the app's settings page does.

        function_type 0 is the child lock, 1 foreign object detection, 6 shake mode and
        7 the waste-bin-full alert. Auto clean (2) carries the delay, so it has its own call.
        """
        # The app sends optionInt 1 for these; 0 is rejected as an invalid value.
        payload = {"serialNumber": serial_number, "functionType": function_type, "controlSwitch": 1 if enabled else 0, "optionInt": 1}
        self._request("PUT", "/catbox-server/box/config/switch/update", payload=payload, auth=True)

    def set_box_full_alert(self, serial_number: str, uses: int) -> None:
        """Set how many uses fill the waste bin before the bin-full alert."""
        payload = {"serialNumber": serial_number, "boxFullAlertTimes": uses}
        self._request("PUT", "/catbox-server/box/config/box-full-alert", payload=payload, auth=True)

    def reset_deodorant(self, serial_number: str) -> None:
        """Mark the deodorant block replaced, restarting its remaining-days countdown."""
        self._request("POST", "/catbox-server/app/deodorant-block/replaced", payload={"serialNumber": serial_number}, auth=True)

    def set_deodorant_alert(self, serial_number: str, enabled: bool) -> None:
        payload = {
            "serialNumber": serial_number,
            "alertSwitch": 1 if enabled else 0,
        }
        self._request("POST", "/catbox-server/app/deodorant-block/switch", payload=payload, auth=True)

    def set_time_point(
        self,
        serial_number: str,
        *,
        config_id: int,
        function_type: int,
        open_switch: int,
        time_hour: int,
        time_minute: int,
        option_int: int,
    ) -> None:
        payload = {
            "serialNumber": serial_number,
            "configId": config_id,
            "functionType": function_type,
            "openSwitch": open_switch,
            "timeHour": time_hour,
            "timeMinute": time_minute,
            "optionInt": option_int,
        }
        self._request("PUT", "/catbox-server/box/config/timePoint/update", payload=payload, auth=True)

    def set_time_period(
        self,
        serial_number: str,
        *,
        config_id: int,
        function_type: int,
        open_switch: int,
        start_hour: int,
        start_minute: int,
        end_hour: int,
        end_minute: int,
    ) -> None:
        payload = {
            "serialNumber": serial_number,
            "functionType": function_type,
            "openSwitch": open_switch,
            "configId": config_id,
            "startTimeHour": start_hour,
            "startTimeMinute": start_minute,
            "endTimeHour": end_hour,
            "endTimeMinute": end_minute,
        }
        self._request("PUT", "/catbox-server/box/config/timePeriod/update", payload=payload, auth=True)

    def get_dashboard(self) -> dict[str, Any]:
        self.ensure_login()
        devices = self.get_devices()
        _LOGGER.info("UPET device list returned %s device(s)", len(devices))
        by_serial: dict[str, dict[str, Any]] = {}
        for device in devices:
            serial = device.get("serialNumber")
            if not serial:
                continue
            # Each part is None when its call failed, so the update can keep the last values instead.
            by_serial[serial] = {
                "device": device,
                "config": _optional_call(self.get_all_config, serial),
                "box_use_times": _optional_call(self.get_box_use_times, serial),
                "deodorant": _optional_call(self.get_deodorant_status, serial),
                "online": _optional_call(self.get_device_online, serial),
                # Only read when entities are set up, and None (keep every entity) if it failed.
                "capabilities": _optional_call(self.get_capabilities, serial),
                # Visits, then cleans.
                "cat_records": _optional_call(self.get_box_records, serial, 0, self.records_per_fetch),
                "device_records": _optional_call(self.get_box_records, serial, 1, self.records_per_fetch),
            }
        return {
            "devices": by_serial,
            "cats": _optional_call(self.get_cats),
            "user_id": self.auth.user_id if self.auth else None,
        }

    def _request(self, method: str, path: str, *, payload: dict[str, Any] | None = None, auth: bool) -> Any:
        auth_token = self.ensure_login().token if auth else None
        try:
            return self._request_once(method, path, payload=payload, auth_token=auth_token)
        except UbpetApiError as err:
            if not auth or err.status != 401 or auth_token is None:
                raise
            _LOGGER.warning("UPET HTTP %s %s returned 401; refreshing authentication once", method, path)
            refreshed_auth = self._refresh_after_unauthorized(auth_token)
            return self._request_once(method, path, payload=payload, auth_token=refreshed_auth.token)

    def _request_once(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None,
        auth_token: str | None,
    ) -> Any:
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        headers = {
            "x-ubt-language": "en",
            "x-ubt-deviceid": self.device_id,
            "content-type": "application/json; charset=utf-8",
            "accept": "application/json",
            "x-ubt-appid": self.app_id,
            "priority": "u=3, i",
            "user-agent": USER_AGENT,
            "x-ubt-sign": self.signer.sign(device_id=self.device_id),
        }
        if auth_token is not None:
            headers["authorization"] = auth_token
            headers["product"] = self.product

        req = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        started = time.monotonic()
        _LOGGER.debug("UPET HTTP %s %s auth=%s", method, path, auth_token is not None)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = _decode_response(resp.read())
                _raise_for_api_error(resp.status, data)
                _LOGGER.debug(
                    "UPET HTTP %s %s completed with status=%s in %.2fs",
                    method,
                    path,
                    resp.status,
                    time.monotonic() - started,
                )
                return data
        except urllib.error.HTTPError as err:
            try:
                data = _decode_response(err.read())
            finally:
                err.close()
            _LOGGER.warning("UPET HTTP %s %s failed with status=%s", method, path, err.code)
            raise UbpetApiError(err.code, data) from err


def _md5_password(password: str) -> str:
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def _account_type_candidates(account: str) -> tuple[str, ...]:
    if "@" in account:
        return ("1", "0", "2")
    return ("0", "1", "2")


def _vendor_code(err: UbpetApiError) -> Any:
    return err.payload.get("code") if isinstance(err.payload, dict) else None


def _decode_response(raw: bytes) -> Any:
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _raise_for_api_error(status: int, data: Any) -> None:
    if status >= 400:
        raise UbpetApiError(status, data)
    if isinstance(data, dict):
        code = data.get("code")
        if code not in (None, 0):
            if _is_permission_refusal(data):
                raise UbpetPermissionError(status, data)
            raise UbpetApiError(status, data)


def _is_permission_refusal(data: dict[str, Any]) -> bool:
    # Code 500 is the vendor's catch-all, so only its permission message counts.
    message = data.get("message") or data.get("msg")
    if data.get("code") != 500 or not isinstance(message, str):
        return False
    return "权限" in message or "permission" in message.lower()


def _response_data(payload: Any) -> Any:
    """Unwrap the standard vendor response envelope when it is present."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def _require_dict(payload: Any) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise UbpetApiError(200, payload)
    return data


def _require_list(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise UbpetApiError(200, payload)
    return data


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def find_account_uid(credentials: dict[str, Any], auth: UbpetAuth | None) -> str:
    for key in ("uid", "userId", "imUserId", "accountUid"):
        value = credentials.get(key)
        if value not in (None, ""):
            return str(value)
    if auth is not None and auth.user_id is not None:
        return str(auth.user_id)
    raise UbpetApiError(
        200,
        {
            "code": "missing_im_sender",
            "message": "No sender uid found in IM login response or auth session",
            "im_login_keys": sorted(str(key) for key in credentials),
        },
    )


def _optional_call(func, *args) -> Any:
    """The call's result, or None when it failed; the coordinator logs parts that keep failing."""
    try:
        return func(*args)
    except UbpetLoginError:
        # A failed login fails the whole update; swallowing it would log in again on each
        # remaining call, about 20 logins for one update.
        raise
    except (OSError, RuntimeError, UbpetApiError) as err:
        _LOGGER.debug("UPET optional endpoint failed: %s", err)
        return None

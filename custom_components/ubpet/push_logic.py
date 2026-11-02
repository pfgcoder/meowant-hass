"""Pure decisions for the persistent MQTT connection, free of Home Assistant imports so tests can load them.

Ported from bojanrajkovic/upet-hass (57ef90e), without its Cat inside presence logic.
"""

from __future__ import annotations

MIN_RECONNECT_S = 60
MAX_RECONNECT_S = 1800
# A session that lasted this long was healthy, so the next drop starts the backoff over.
STABLE_SESSION_S = 600
# Our own REST login invalidates our IM session, so reconnect almost at once.
OWN_LOGIN_RECONNECT_S = 5


def reconnect_delay(previous_s: float, *, session_s: float, own_login: bool) -> float:
    """Seconds to wait before reconnecting after a session ended.

    Another login on the account (the phone app, a script) kicks us, and
    reconnecting kicks it back, so repeated short sessions back off from one
    minute to thirty. A session ended by our own token refresh reconnects in
    seconds instead.
    """
    if own_login:
        return OWN_LOGIN_RECONNECT_S
    if session_s >= STABLE_SESSION_S or previous_s <= 0:
        return MIN_RECONNECT_S
    return min(previous_s * 2, MAX_RECONNECT_S)

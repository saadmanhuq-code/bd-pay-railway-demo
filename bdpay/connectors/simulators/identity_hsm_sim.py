"""Deterministic payShield host-command simulator (spec/12 §I, §K).

:class:`ScriptedPayshieldHost` is a fake HSM HOST behind the
``HostCommandTransport`` port: it parses the real host-command framing the
PRODUCTION client emits (2-byte length + 4-char header + 2-char command code +
``;``-joined hex fields), executes the verb on an internal deterministic
:class:`~bdpay.connectors.identity.hsm.SoftHsmSession` (re-exported), and
answers in the response framing the client reads (header + response code +
2-digit error code + hex payload). So the PRODUCTION wire path of
``hsm_thales_v1`` is exercised end-to-end with zero real hardware.

Scripting: ``reject_next(code)`` answers one command with a payShield error
code; ``hang_next()`` makes one exchange never return (the 5s-budget /
breaker fail-closed scenario ``hsm_down_fail_closed``); a MAC verify against
a wrong MAC answers error ``01`` which the client maps to ``False`` — the
``mac_mismatch`` scenario (not an error; callers fail closed).

Key material is label-addressed inside the session — nothing crosses the
boundary; determinism comes from the seed-derived soft keys.
"""

from __future__ import annotations

import asyncio

from bdpay.connectors.identity.hsm import (
    HsmCommandRejectedError,
    HsmKeyNotFoundError,
    SoftHsmSession,
)

__all__ = ["ScriptedPayshieldHost", "SoftHsmSession"]

#: command code -> verb (mirror of PayshieldHostCommandClient.DEFAULT_COMMANDS).
_VERB_BY_COMMAND = {
    "M6": "generate_mac",
    "M8": "verify_mac",
    "CA": "translate_pin",
    "A0": "encrypt_zmk",
    "A2": "decrypt_zmk",
}


class ScriptedPayshieldHost:
    """Deterministic payShield host endpoint behind ``HostCommandTransport``."""

    def __init__(
        self,
        *,
        seed: str = "soft-hsm-sim-seed",
        key_labels: tuple[str, ...] = (),
        commands: dict | None = None,
    ) -> None:
        self._session = SoftHsmSession(seed=seed, key_labels=key_labels)
        self._verbs = dict(_VERB_BY_COMMAND)
        for verb, code in (commands or {}).items():
            self._verbs[str(code)] = str(verb)
        self._reject_next: str | None = None
        self._hang_next = False
        self.exchanges = 0

    @property
    def session(self) -> SoftHsmSession:
        return self._session

    # -- scripting -------------------------------------------------------------------

    def reject_next(self, error_code: str) -> None:
        self._reject_next = error_code

    def hang_next(self) -> None:
        self._hang_next = True

    # -- HostCommandTransport ----------------------------------------------------------

    async def exchange(self, frame: bytes) -> bytes:
        self.exchanges += 1
        if self._hang_next:
            self._hang_next = False
            await asyncio.Event().wait()
        length = int.from_bytes(frame[:2], "big")
        message = frame[2 : 2 + length].decode("ascii")
        header, command, joined = message[:4], message[4:6], message[6:]
        if self._reject_next is not None:
            code = self._reject_next
            self._reject_next = None
            return self._respond(header, command, code, "")
        verb = self._verbs.get(command)
        if verb is None:
            return self._respond(header, command, "05", "")  # invalid command code
        fields = joined.split(";") if joined else []
        try:
            payload, error = self._execute(verb, fields)
        except HsmKeyNotFoundError:
            return self._respond(header, command, "02", "")  # key not found at host
        except HsmCommandRejectedError as exc:
            # SoftHsmSession reject codes surface as host error codes verbatim
            # (exc.code is "hsm_command_rejected_<NN>").
            return self._respond(header, command, exc.code.rsplit("_", 1)[-1], "")
        return self._respond(header, command, error, payload)

    def _execute(self, verb: str, fields: list[str]) -> tuple[str, str]:
        if verb == "generate_mac":
            key_label, preimage_hex = fields
            return self._session.generate_mac(key_label, bytes.fromhex(preimage_hex)).hex(), "00"
        if verb == "verify_mac":
            key_label, preimage_hex, mac_hex = fields
            ok = self._session.verify_mac(
                key_label, bytes.fromhex(preimage_hex), bytes.fromhex(mac_hex)
            )
            return "", "00" if ok else "01"  # 01 = MAC verification failure
        if verb == "translate_pin":
            src_label, dst_label, pin_block_hex = fields
            translated = self._session.translate_pin_block(
                src_label, dst_label, bytes.fromhex(pin_block_hex)
            )
            return translated.hex(), "00"
        if verb == "encrypt_zmk":
            zmk_label, clear_hex = fields
            return self._session.encrypt_under_zmk(zmk_label, bytes.fromhex(clear_hex)).hex(), "00"
        zmk_label, cipher_hex = fields  # decrypt_zmk (closed verb table)
        return self._session.decrypt_under_zmk(zmk_label, bytes.fromhex(cipher_hex)).hex(), "00"

    @staticmethod
    def _respond(header: str, command: str, error_code: str, payload: str) -> bytes:
        # Client framing: text[6:8] = error code, text[8:] = payload.
        return (header + command + error_code + payload).encode("ascii")

"""Config flow and options flow for the LINE Bot integration.

This module handles all UI-driven configuration via the HA integrations page.

Config flow steps (initial setup) - intentionally minimal:
  1. user          - Enter Channel Access Token and Channel Secret.
  2. webhook_info  - Display the webhook URL to paste into LINE Developers Console.
                     The config entry is created on Submit once the confirmation
                     checkbox is ticked. Recipients are added separately via the
                     options flow after setup completes.

Options flow steps (accessed via the gear icon on the integration card):
  init             - Menu: Add recipient / Remove recipient / Update credentials.
  add_recipient    - Progress spinner waiting for a LINE message or group event.
                     Skips spinner if messages are already captured in pending_users.
  select_recipient - Dropdown of captured LINE users and groups, including a
                     "Clear all pending" sentinel.
  name_recipient   - Enter an HA entity name and display name for the selected
                     account, with suggestions derived from its LINE display name.
                     Includes an "Add another" checkbox.
  remove_recipient - Dropdown of current recipients to delete.
  rotate_token     - Update Channel Access Token and/or Channel Secret.

Recipient storage format:
  Recipients are stored in the config entry data dict as:
    {"name": {"user_id": "U...", "display_name": "...", "friendly_name": "...", "type": "user"}}
  Groups use IDs starting with "C" and type "group". Users use IDs starting with
  "U" and type "user". The type is detected automatically from the LINE ID prefix.

Recipient name rules:
  Names must contain only ASCII letters, digits, spaces, hyphens, and underscores,
  and must produce a non-empty slug. Non-ASCII characters (Thai, emoji, etc.) are
  rejected with a clear error message. The _sanitize_name() helper suggests a safe
  default: emojis are converted to their Unicode name (e.g. nerd), non-ASCII
  scripts are romanized via slugify().

Webhook-based capture:
  The permanent webhook in __init__.py captures LINE user IDs and group IDs into
  the per-entry runtime pending_users dict whenever someone messages the bot or
  sends a message in a registered group. The options flow waits on the runtime
  pending_event (with a polling fallback) and automatically advances to the
  select step when a user or group appears.
"""

import asyncio
import re
import unicodedata

from homeassistant.util import slugify
import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import network
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from .const import (
    DOMAIN,
    CONF_CHANNEL_ACCESS_TOKEN,
    CONF_CHANNEL_SECRET,
    CONF_RECIPIENT_NAME,
    CONF_FRIENDLY_NAME,
    CONF_USER_ID,
    LINE_TOKEN_VERIFY_URL,
    LINE_WEBHOOK_PATH,
    PENDING_USERS_KEY,
    CLEAR_PENDING,
    CLEAR_PENDING_LABEL,
    RECIPIENTS_KEY,
)

CONF_ADD_ANOTHER = "add_another"

# Polling fallback: checks every _POLL_INTERVAL seconds for up to
# _POLL_ITERATIONS cycles. Total wait time: 300 * 2 = 600 seconds (10 minutes).
# The webhook's pending_event normally wakes the wait immediately.
_POLL_ITERATIONS = 300
_POLL_INTERVAL = 2


async def _verify_token(hass: HomeAssistant, token: str) -> str | None:
    """Verify a LINE channel access token against the LINE oauth endpoint.

    Returns None if valid (HTTP 200), or an error key string if not.
    """
    session = async_get_clientsession(hass)
    try:
        async with session.post(
            LINE_TOKEN_VERIFY_URL,
            data={"access_token": token},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                return None
            return "invalid_token"
    except aiohttp.ClientError:
        return "cannot_connect"


def _get_external_url(hass: HomeAssistant) -> str | None:
    """Return the HA external SSL URL, or None if not configured."""
    try:
        return network.get_url(
            hass,
            allow_internal=False,
            allow_ip=False,
            require_ssl=True,
        )
    except network.NoURLAvailableError:
        return None


def _is_valid_name(name: str) -> bool:
    """Return True if name is safe to use as an HA recipient name.

    Accepts only ASCII letters, digits, spaces, hyphens, and underscores,
    and requires that the name produces a non-empty slug (so names made up
    of only spaces, hyphens, or underscores are rejected).
    """
    return (
        bool(name)
        and bool(re.match(r'^[a-zA-Z0-9 _-]+$', name))
        and bool(slugify(name))
    )


def _name_slug_conflicts(name: str, existing_names: dict) -> bool:
    """Return True if the slugified name conflicts with any existing recipient.

    Prevents cases like "Steve" and "steve" both producing notify.line_bot_steve.
    """
    new_slug = slugify(name)
    return any(slugify(existing) == new_slug for existing in existing_names)


def _is_emoji(char: str) -> bool:
    """Return True if char is in one of the common emoji Unicode blocks.

    Best-effort coverage of the most common blocks; characters outside these
    ranges (e.g. flags, arrows) are simply dropped by slugify() later.
    """
    cp = ord(char)
    return (
        0x1F300 <= cp <= 0x1FAFF  # Misc symbols, emoticons, transport, etc.
        or 0x2600 <= cp <= 0x27BF  # Misc symbols and dingbats
        or 0xFE00 <= cp <= 0xFE0F  # Variation selectors (emoji modifiers)
        or 0x1F900 <= cp <= 0x1F9FF  # Supplemental symbols
        or 0x1FA00 <= cp <= 0x1FA6F  # Chess, etc.
    )


def _sanitize_name(display_name: str) -> str:
    """Derive a suggested HA entity name from a LINE display name.

     Two-step approach:
      1. Replace each emoji character with the first word of its Unicode name
         (e.g. U+1F913 NERD FACE becomes "nerd"). This preserves emoji semantics
         that would otherwise be silently dropped.
      2. Pass the result through HA's slugify(), which romanizes Thai, CJK,
         Japanese, and other non-ASCII scripts and produces a safe ASCII slug.

    Examples:
      "David"           -> "david"
      "สวัสดี"           -> "swasdii"
      "🤓"              -> "nerd"
      "สวัสดี 🤓 David" -> "swasdii_nerd_david"
    """
    parts = []
    for char in display_name:
        if _is_emoji(char):
            try:
                name = unicodedata.name(char, "")
                if name:
                    # Surround emoji name with spaces so slugify treats it as a separate word
                    first_word = name.split()[0].lower()
                    parts.append(f" {first_word} ")
            except (ValueError, TypeError):
                pass
        else:
            parts.append(char)
    preprocessed = "".join(parts)
    return slugify(preprocessed, separator="_")


class LineMessagingConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial configuration flow for LINE Bot.

    Intentionally kept to two steps: credentials and webhook setup.
    The config entry is created with empty recipients after webhook verification.
    All recipient management is done via the options flow after installation.
    Only one instance is allowed, enforced via single_config_entry in the manifest.
    """

    VERSION = 1

    def __init__(self):
        """Initialise flow state."""
        self._token: str | None = None
        self._secret: str | None = None

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Step 1: Collect and verify LINE API credentials.

        Asks for the Channel Access Token and Channel Secret, both found on the
        Basic Settings tab of the LINE Developers Console. Checks the secret is
        non-empty before verifying the token against LINE's oauth endpoint.
        """
        errors = {}
        if user_input is not None:
            token = user_input[CONF_CHANNEL_ACCESS_TOKEN].strip()
            secret = user_input[CONF_CHANNEL_SECRET].strip()
            if not secret:
                errors[CONF_CHANNEL_SECRET] = "invalid_secret"
            else:
                error = await _verify_token(self.hass, token)
                if error:
                    errors["base"] = error
                else:
                    self._token = token
                    self._secret = secret
                    return await self.async_step_webhook_info()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_CHANNEL_ACCESS_TOKEN): str,
                vol.Required(CONF_CHANNEL_SECRET): str,
            }),
            errors=errors,
        )

    async def async_step_webhook_info(self, user_input=None) -> FlowResult:
        """Step 2: Show webhook URL and create the config entry on Submit.

        Registers the webhook view immediately so LINE's Verify button succeeds
        before the user clicks Submit. The confirmation checkbox must be ticked.
        The config entry is created with empty recipients. Recipients are added
        via the options flow (gear icon) after installation.
        """
        from . import _async_register_webhook

        external_url = _get_external_url(self.hass)
        if external_url is None:
            return self.async_abort(reason="no_external_url")

        webhook_url = f"{external_url}{LINE_WEBHOOK_PATH}"

        _async_register_webhook(self.hass)

        errors = {}
        if user_input is not None:
            if user_input.get("confirmed"):
                return self.async_create_entry(
                    title="LINE Bot",
                    data={
                        CONF_CHANNEL_ACCESS_TOKEN: self._token,
                        CONF_CHANNEL_SECRET: self._secret,
                        RECIPIENTS_KEY: {},
                    },
                )
            errors["confirmed"] = "confirm_required"

        return self.async_show_form(
            step_id="webhook_info",
            data_schema=vol.Schema({
                vol.Optional("confirmed", default=False): bool,
            }),
            errors=errors,
            description_placeholders={"webhook_url": webhook_url},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow handler for this config entry."""
        return LineMessagingOptionsFlow()


class LineMessagingOptionsFlow(config_entries.OptionsFlow):
    """Handle the options flow for LINE Bot (gear icon on the integration card).

    Provides three actions via a translatable menu:
      - Add a recipient:       Webhook-based capture flow with spinner.
      - Remove a recipient:    Dropdown of current recipients to delete.
      - Update credentials:    Replace the Channel Access Token and/or Channel Secret.

    All changes are saved to the config entry data dict. The update listener in
    __init__.py triggers a reload so notify entities reflect the new state.
    """

    def __init__(self):
        """Initialise flow state. Entry data is loaded lazily in async_step_init."""
        self._recipients: dict[str, dict] | None = None
        self._token = ""
        self._secret = ""
        self._poll_task: asyncio.Task | None = None
        self._selected_uid: str | None = None

    async def async_step_init(self, user_input=None) -> FlowResult:
        """Show the action menu: Add / Remove / Update credentials."""
        if self._recipients is None:
            data = self.config_entry.data
            self._recipients = dict(data.get(RECIPIENTS_KEY, {}))
            self._token = data.get(CONF_CHANNEL_ACCESS_TOKEN, "")
            self._secret = data.get(CONF_CHANNEL_SECRET, "")

        return self.async_show_menu(
            step_id="init",
            menu_options=["add_recipient", "remove_recipient", "rotate_token"],
        )

    async def async_step_add_recipient(self, user_input=None) -> FlowResult:
        """Show spinner waiting for a LINE message, or skip to select if pending exist.

        If pending_users already has entries AND no poll task has been started yet
        (meaning we are entering fresh, not returning from a progress step), skip
        directly to select_recipient.

        Once a poll task has been started we must always go through the
        async_show_progress / async_show_progress_done transition, because HA
        does not allow a progress step to transition directly to a form step.
        The poll task itself detects pending_users and returns early, causing
        async_show_progress_done to fire immediately.
        """
        # Fresh entry with pending users already present - skip spinner entirely.
        if self._poll_task is None and self._get_pending_users():
            return await self.async_step_select_recipient()

        if self._poll_task is None:
            self._poll_task = self.hass.async_create_task(
                self._poll_for_pending_user()
            )

        if not self._poll_task.done():
            return self.async_show_progress(
                step_id="add_recipient",
                progress_action="waiting_for_message",
                progress_task=self._poll_task,
            )

        self._poll_task = None
        return self.async_show_progress_done(next_step_id="select_recipient")

    async def _poll_for_pending_user(self) -> None:
        """Background task that waits until a pending user ID appears.

        Primarily event-driven: the webhook sets the runtime pending_event as
        soon as it captures a new sender, which wakes this task immediately.
        Falls back to checking pending_users every _POLL_INTERVAL seconds (the
        event object is re-fetched each cycle so a config entry reload mid-wait
        cannot strand the task). Gives up after _POLL_ITERATIONS cycles; the
        select step then redirects back to add_recipient for a fresh wait.
        """
        for _ in range(_POLL_ITERATIONS):
            if self._get_pending_users():
                return
            event = self._get_pending_event()
            if event is None:
                await asyncio.sleep(_POLL_INTERVAL)
                continue
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=_POLL_INTERVAL)
            except TimeoutError:
                pass

    async def async_step_select_recipient(self, user_input=None) -> FlowResult:
        """Pick a captured LINE account or group from the dropdown.

        The dropdown lists all pending users and groups plus a "Clear all pending"
        sentinel (translated via the pending_user selector key). Selecting
        CLEAR_PENDING wipes pending_users, persists the wipe, and goes back to
        the spinner. If pending_users is empty (spinner timed out), goes back to
        add_recipient. Otherwise advances to name_recipient for the chosen account.
        """
        pending = self._get_pending_users()

        if user_input is not None:
            user_id = user_input[CONF_USER_ID]

            if user_id == CLEAR_PENDING:
                pending.clear()
                # Persist the wipe so cleared accounts do not reappear after a restart.
                self._persist()
                self._poll_task = None
                return await self.async_step_add_recipient()

            self._selected_uid = user_id
            return await self.async_step_name_recipient()

        if not pending:
            self._poll_task = None
            return await self.async_step_add_recipient()

        options = [
            SelectOptionDict(value=uid, label=display)
            for uid, display in pending.items()
        ]
        options.append(SelectOptionDict(value=CLEAR_PENDING, label=CLEAR_PENDING_LABEL))
        return self.async_show_form(
            step_id="select_recipient",
            data_schema=vol.Schema({
                vol.Required(CONF_USER_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        translation_key="pending_user",
                    )
                ),
            }),
        )

    async def async_step_name_recipient(self, user_input=None) -> FlowResult:
        """Name the selected account and optionally add another.

        Shows two name fields: recipient_name (ASCII only, used for entity ID
        slugification) and friendly_name (any characters including emoji and
        unicode, used as the HA display name). Both are suggested from the LINE
        display name of the account chosen in select_recipient. The recipient
        type (user or group) is detected from the LINE ID prefix (U vs C).
        """
        pending = self._get_pending_users()
        user_id = self._selected_uid
        if user_id is None or user_id not in pending:
            # Selection vanished (e.g. cleared elsewhere); start over.
            self._selected_uid = None
            return await self.async_step_select_recipient()

        line_display_name = pending.get(user_id, user_id)
        errors = {}

        if user_input is not None:
            name = user_input.get(CONF_RECIPIENT_NAME, "").strip()
            friendly_name = user_input.get(CONF_FRIENDLY_NAME, "").strip()
            add_another = user_input.get(CONF_ADD_ANOTHER, False)

            if not name:
                errors[CONF_RECIPIENT_NAME] = "name_required"
            elif not _is_valid_name(name):
                errors[CONF_RECIPIENT_NAME] = "invalid_name"
            elif name in self._recipients or _name_slug_conflicts(name, self._recipients):
                errors[CONF_RECIPIENT_NAME] = "duplicate_name"
            elif any(r["user_id"] == user_id for r in self._recipients.values()):
                errors["base"] = "duplicate_user_id"
            else:
                self._recipients[name] = {
                    "user_id": user_id,
                    "display_name": line_display_name,
                    "friendly_name": friendly_name or line_display_name,
                    "type": "group" if user_id.startswith("C") else "user",
                }
                pending.pop(user_id, None)
                self._selected_uid = None
                self._persist()
                if add_another:
                    self._poll_task = None
                    return await self.async_step_add_recipient()
                return self._save()

        suggested_name = _sanitize_name(line_display_name)
        return self.async_show_form(
            step_id="name_recipient",
            data_schema=vol.Schema({
                vol.Optional(CONF_FRIENDLY_NAME, default=line_display_name): str,
                vol.Optional(CONF_RECIPIENT_NAME, default=suggested_name): str,
                vol.Optional(CONF_ADD_ANOTHER, default=False): bool,
            }),
            errors=errors,
            description_placeholders={"line_name": line_display_name},
        )

    async def async_step_remove_recipient(self, user_input=None) -> FlowResult:
        """Show a dropdown of current recipients and remove the selected one."""
        if not self._recipients:
            return self.async_abort(reason="no_recipients")

        if user_input is not None:
            name = user_input[CONF_RECIPIENT_NAME]
            self._recipients.pop(name, None)
            return self._save()

        remove_options = {
            name: r.get("friendly_name", name)
            for name, r in self._recipients.items()
        }
        return self.async_show_form(
            step_id="remove_recipient",
            data_schema=vol.Schema({
                vol.Required(CONF_RECIPIENT_NAME): vol.In(remove_options),
            }),
        )

    async def async_step_rotate_token(self, user_input=None) -> FlowResult:
        """Replace the Channel Access Token and/or Channel Secret.

        Verifies the new token before saving. Both fields are pre-filled with
        current values so the user only needs to change what has rotated.
        """
        errors = {}
        if user_input is not None:
            token = user_input[CONF_CHANNEL_ACCESS_TOKEN].strip()
            secret = user_input[CONF_CHANNEL_SECRET].strip()
            if not secret:
                errors[CONF_CHANNEL_SECRET] = "invalid_secret"
            else:
                error = await _verify_token(self.hass, token)
                if error:
                    errors["base"] = error
                else:
                    self._token = token
                    self._secret = secret
                    return self._save()

        return self.async_show_form(
            step_id="rotate_token",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({
                    vol.Required(CONF_CHANNEL_ACCESS_TOKEN): str,
                    vol.Required(CONF_CHANNEL_SECRET): str,
                }),
                {
                    CONF_CHANNEL_ACCESS_TOKEN: self._token,
                    CONF_CHANNEL_SECRET: self._secret,
                },
            ),
            errors=errors,
        )

    def _get_pending_users(self) -> dict:
        """Return the live pending_users dict from the entry's runtime data.

        The runtime dict is the authoritative source during normal operation.
        It is pre-populated from config entry data at startup so captures
        survive HA restarts. Returns an empty dict if the entry is not loaded.
        """
        runtime = getattr(self.config_entry, "runtime_data", None)
        return getattr(runtime, "pending_users", None) or {}

    def _get_pending_event(self) -> asyncio.Event | None:
        """Return the runtime pending_event set by the webhook on capture."""
        runtime = getattr(self.config_entry, "runtime_data", None)
        return getattr(runtime, "pending_event", None)

    def _persist(self) -> None:
        """Write current recipients and credentials to the config entry without closing the flow.

        Called after each recipient is confirmed (or pending is cleared) so
        partial progress is not lost if the user cancels the flow. Preserves
        any other keys already in entry data, and writes the current in-memory
        pending_users back so the update listener can detect pending-only
        writes and skip the reload.
        """
        new_data = dict(self.config_entry.data)
        new_data.update({
            CONF_CHANNEL_ACCESS_TOKEN: self._token,
            CONF_CHANNEL_SECRET: self._secret,
            RECIPIENTS_KEY: self._recipients,
            PENDING_USERS_KEY: dict(self._get_pending_users()),
        })
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=new_data
        )

    def _save(self) -> FlowResult:
        """Persist and close the options flow.

        Calls _persist() to write data, then returns async_create_entry to
        signal completion. The update listener in __init__.py triggers a reload
        so notify entities are refreshed.
        """
        self._persist()
        return self.async_create_entry(title="", data={})

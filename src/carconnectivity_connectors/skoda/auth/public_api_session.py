"""Module implements session handling for the Skoda Public API (X-API-Key auth)."""
from __future__ import annotations
from typing import TYPE_CHECKING

from datetime import datetime, timedelta, timezone
import logging

import requests

from carconnectivity.errors import AuthenticationError, RetrievalError, TooManyRequestsError
from carconnectivity.util import robust_time_parse

if TYPE_CHECKING:
    from typing import Callable, List, Optional, Union


LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.skoda.auth")

BASE_URL: str = "https://public.api.connect.skoda-auto.cz"

DEFAULT_TIMEOUT: int = 10

# Log an info message once a key gets this close to its expiration date.
KEY_EXPIRY_WARNING_WINDOW: timedelta = timedelta(days=7)


class ApiKeyState:
    """
    Tracks the rate-limit and expiration state of a single API key.

    Args:
        key (str): The raw API key value.
    """

    def __init__(self, key: str) -> None:
        self.key: str = key
        self.remaining: Optional[int] = None
        self.limit: Optional[int] = None
        self.reset: Optional[int] = None
        # Absolute point in time at which the current rate-limit window resets, computed from the
        # relative "RateLimit-Reset" header (seconds) at the time it was received.
        self.reset_at: Optional[datetime] = None
        self.expires_at: Optional[datetime] = None
        self.expired: bool = False
        self.expiry_warning_logged: bool = False

    def is_available(self) -> bool:
        """
        Returns whether this key can currently be used for a request: either its remaining budget
        for the current window is unknown/positive, or the rate-limit window is known to have
        already reset (in which case the budget is assumed to be replenished).
        """
        if self.remaining is None or self.remaining > 0:
            return True
        return self.reset_at is not None and self.reset_at <= datetime.now(tz=timezone.utc)

    @property
    def masked_key(self) -> str:
        """
        Returns the API key with everything but the last 8 characters masked, so it can be safely
        displayed (e.g. in logs or the UI status page) without revealing the full key.
        """
        if len(self.key) <= 8:
            return self.key
        return f'...{self.key[-8:]}'

    def __repr__(self) -> str:
        return f'ApiKeyState(key={self.masked_key}, remaining={self.remaining}, limit={self.limit}, expires_at={self.expires_at})'


class PublicApiSession(requests.Session):
    """
    Session for the Skoda public API using X-API-Key authentication.

    Supports configuring one or more API keys. The public API allows a maximum of 5 keys with
    20 requests/hour each, so when multiple keys are configured requests are distributed across
    them to make use of the combined rate-limit budget.

    Args:
        api_key (str or list[str]): One or more API keys created in the MyŠkoda app.
    """

    def __init__(self, api_key: Union[str, List[str]]) -> None:
        super().__init__()
        if isinstance(api_key, str):
            keys: List[str] = [api_key]
        else:
            keys = list(api_key)
        keys = [key for key in keys if key]
        if not keys:
            raise AuthenticationError('At least one API key must be provided')
        self._keys: List[ApiKeyState] = [ApiKeyState(key=key) for key in keys]
        self._key_index: int = 0
        # Called (with no arguments) when the last unexpired API key is removed from the pool.
        self.on_all_keys_expired: Optional[Callable[[], None]] = None
        self.headers.update({
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        })
        # Rate-limit state of the key used for the most recent request (kept for backwards
        # compatibility, e.g. simple single-key setups or older UI templates).
        self.rate_limit_remaining: Optional[int] = None
        self.rate_limit_limit: Optional[int] = None
        self.rate_limit_reset: Optional[int] = None
        # Cache: url -> (data, cache_date_string)
        self.cache: dict = {}

    @property
    def keys(self) -> List[ApiKeyState]:
        """
        Returns the list of currently configured (i.e. not yet expired) API keys and their state.
        Useful for e.g. displaying per-key rate-limit information in a UI.
        """
        return list(self._keys)

    def _select_key(self) -> ApiKeyState:
        """
        Selects the next API key to use for a request, distributing requests over all configured
        keys in round-robin fashion while skipping keys that are known to be exhausted for the
        current rate-limit window.

        Raises:
            AuthenticationError: If no unexpired API key is configured.
            TooManyRequestsError: If all configured keys are exhausted for the current window.
        """
        if not self._keys:
            raise AuthenticationError('No unexpired API keys available. All configured API keys have expired.')
        num_keys: int = len(self._keys)
        for offset in range(num_keys):
            index = (self._key_index + offset) % num_keys
            candidate = self._keys[index]
            if candidate.is_available():
                self._key_index = (index + 1) % num_keys
                return candidate
        # All keys report no remaining requests for the current window; rotate past the first
        # (oldest) one so a subsequent retry, once headers refresh, starts checking a different key.
        self._key_index = (self._key_index + 1) % num_keys
        raise TooManyRequestsError('Rate limit exceeded for all configured API keys.')

    @staticmethod
    def _parse_rate_limit_headers(key_state: ApiKeyState, headers) -> None:
        """Updates rate-limit fields of a key from the response headers."""
        if 'RateLimit-Remaining' in headers:
            try:
                key_state.remaining = int(headers['RateLimit-Remaining'])
            except (ValueError, TypeError):
                pass
        if 'RateLimit-Limit' in headers:
            try:
                key_state.limit = int(headers['RateLimit-Limit'])
            except (ValueError, TypeError):
                pass
        if 'RateLimit-Reset' in headers:
            try:
                key_state.reset = int(headers['RateLimit-Reset'])
                key_state.reset_at = datetime.now(tz=timezone.utc) + timedelta(seconds=key_state.reset)
            except (ValueError, TypeError):
                pass
        if 'X-API-Key-Expires-At' in headers:
            try:
                key_state.expires_at = robust_time_parse(headers['X-API-Key-Expires-At'])
            except ValueError:
                LOG.warning('Could not parse X-API-Key-Expires-At header value for API key %s', key_state.masked_key)

    @staticmethod
    def _check_key_expiry(key_state: ApiKeyState) -> None:
        """Logs a warning/info and flags a key as expired based on its expires_at timestamp."""
        if key_state.expires_at is None:
            return
        now = datetime.now(tz=timezone.utc)
        if key_state.expires_at <= now:
            key_state.expired = True
            LOG.warning('API key %s has expired and will no longer be used. Please create a new one in the MyŠkoda app.',
                        key_state.masked_key)
        elif not key_state.expiry_warning_logged and key_state.expires_at - now <= KEY_EXPIRY_WARNING_WINDOW:
            key_state.expiry_warning_logged = True
            LOG.info('API key %s will expire on %s. Consider rotating it soon to avoid interruption of service.',
                     key_state.masked_key, key_state.expires_at.isoformat())

    def _remove_expired_key(self, key_state: ApiKeyState) -> None:
        """Removes an expired key from the pool, notifying if no unexpired key remains."""
        if not key_state.expired or key_state not in self._keys:
            return
        self._keys.remove(key_state)
        if self._keys:
            self._key_index = self._key_index % len(self._keys)
            return
        self._key_index = 0
        LOG.error('All configured API keys have expired. Please create a new one in the MyŠkoda app.')
        if self.on_all_keys_expired is not None:
            self.on_all_keys_expired()

    def _update_key_state(self, key_state: ApiKeyState, response: requests.Response) -> None:
        """Updates the rate-limit and expiration state of a key from the response headers."""
        self._parse_rate_limit_headers(key_state, response.headers)
        self.rate_limit_remaining = key_state.remaining
        self.rate_limit_limit = key_state.limit
        self.rate_limit_reset = key_state.reset
        self._check_key_expiry(key_state)
        self._remove_expired_key(key_state)

    def request(self, method, url, **kwargs):  # pylint: disable=arguments-differ
        kwargs.setdefault('timeout', DEFAULT_TIMEOUT)
        key_state = self._select_key()
        # Pass the API key via the per-request headers instead of mutating the shared self.headers
        # dict, so that concurrent calls to request() from multiple threads cannot race and send a
        # request with another thread's API key.
        request_headers = dict(kwargs.get('headers') or {})
        request_headers['X-API-Key'] = key_state.key
        kwargs['headers'] = request_headers
        response = super().request(method, url, **kwargs)
        self._update_key_state(key_state, response)
        return response

    def get_vehicle(self, vin: str) -> dict:
        """
        Fetch all data for a vehicle.

        Args:
            vin (str): Vehicle Identification Number.

        Returns:
            dict: The 'vehicle' object from the API response.

        Raises:
            RetrievalError: On connection or HTTP errors.
            TooManyRequestsError: On rate-limit exceeded (HTTP 429).
            AuthenticationError: On HTTP 401 / 403.
        """
        url = f'{BASE_URL}/api/v1/vehicles/{vin}'
        try:
            response = self.get(url, allow_redirects=False)
        except requests.exceptions.ConnectionError as e:
            raise RetrievalError(f'Connection error fetching vehicle {vin}: {e}') from e
        except requests.exceptions.ReadTimeout as e:
            raise RetrievalError(f'Timeout fetching vehicle {vin}: {e}') from e

        if response.status_code == requests.codes['ok']:
            try:
                data = response.json()
            except requests.exceptions.JSONDecodeError as e:
                raise RetrievalError(f'JSON decode error fetching vehicle {vin}: {e}') from e
            if 'vehicle' not in data:
                raise RetrievalError(f'Unexpected response format for vehicle {vin}: missing "vehicle" key')
            return data
        elif response.status_code == requests.codes['too_many_requests']:
            raise TooManyRequestsError(f'Rate limit exceeded for API key. Status: {response.status_code}')
        elif response.status_code in (requests.codes['unauthorized'], requests.codes['forbidden']):
            raise AuthenticationError(f'API key rejected or expired. Status: {response.status_code}')
        elif response.status_code == requests.codes['not_found']:
            raise RetrievalError(f'Vehicle {vin} not found. Check that the VIN is correct and the API key grants access to this vehicle.')
        else:
            raise RetrievalError(f'Could not fetch vehicle {vin}. Status: {response.status_code}')

    def post_action(self, path: str, json_body: Optional[dict] = None) -> None:
        """
        POST an action to the public API (e.g. start/stop charging).

        Args:
            path (str): Path relative to BASE_URL, e.g. '/api/v1/vehicles/{vin}/charging/start'.
            json_body (dict, optional): JSON body for the request.

        Raises:
            RetrievalError: On connection or HTTP errors.
            TooManyRequestsError: On rate-limit exceeded (HTTP 429).
            AuthenticationError: On HTTP 401 / 403.
        """
        url = f'{BASE_URL}{path}'
        try:
            response = self.post(url, json=json_body, allow_redirects=False)
        except requests.exceptions.ConnectionError as e:
            raise RetrievalError(f'Connection error posting to {path}: {e}') from e
        except requests.exceptions.ReadTimeout as e:
            raise RetrievalError(f'Timeout posting to {path}: {e}') from e

        if response.status_code in (requests.codes['ok'], requests.codes['accepted'], requests.codes['no_content']):
            return
        elif response.status_code == requests.codes['too_many_requests']:
            raise TooManyRequestsError(f'Rate limit exceeded for API key. Status: {response.status_code}')
        elif response.status_code in (requests.codes['unauthorized'], requests.codes['forbidden']):
            raise AuthenticationError(f'API key rejected or expired. Status: {response.status_code}')
        else:
            try:
                problem = response.json()
                detail = problem.get('detail', '')
                problem_type = problem.get('type', '')
                raise RetrievalError(f'Action {path} failed ({response.status_code}): {problem_type} — {detail}')
            except (ValueError, KeyError) as parse_err:
                raise RetrievalError(f'Action {path} failed. Status: {response.status_code}') from parse_err

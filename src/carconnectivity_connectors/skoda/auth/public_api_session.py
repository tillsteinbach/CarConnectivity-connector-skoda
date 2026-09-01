"""Module implements session handling for the Skoda Public API (X-API-Key auth)."""
from __future__ import annotations
from typing import TYPE_CHECKING

import logging

import requests

from carconnectivity.errors import AuthenticationError, RetrievalError, TooManyRequestsError

if TYPE_CHECKING:
    from typing import Optional


LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.skoda.auth")

BASE_URL: str = "https://public.api.connect.skoda-auto.cz"

DEFAULT_TIMEOUT: int = 10


class PublicApiSession(requests.Session):
    """
    Session for the Skoda public API using X-API-Key authentication.

    Args:
        api_key (str): The API key created in the MyŠkoda app.
    """

    def __init__(self, api_key: str) -> None:
        super().__init__()
        if not api_key:
            raise AuthenticationError('API key must not be empty')
        self._api_key: str = api_key
        self.headers.update({
            'X-API-Key': api_key,
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        })
        # Rate-limit state (updated from response headers)
        self.rate_limit_remaining: Optional[int] = None
        self.rate_limit_limit: Optional[int] = None
        self.rate_limit_reset: Optional[int] = None
        # Cache: url -> (data, cache_date_string)
        self.cache: dict = {}

    def request(self, method, url, **kwargs):  # pylint: disable=arguments-differ
        kwargs.setdefault('timeout', DEFAULT_TIMEOUT)
        response = super().request(method, url, **kwargs)
        # Track rate-limit headers
        if 'RateLimit-Remaining' in response.headers:
            try:
                self.rate_limit_remaining = int(response.headers['RateLimit-Remaining'])
            except (ValueError, TypeError):
                pass
        if 'RateLimit-Limit' in response.headers:
            try:
                self.rate_limit_limit = int(response.headers['RateLimit-Limit'])
            except (ValueError, TypeError):
                pass
        if 'RateLimit-Reset' in response.headers:
            try:
                self.rate_limit_reset = int(response.headers['RateLimit-Reset'])
            except (ValueError, TypeError):
                pass
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

"""
Module implements the WeConnect Session handling.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

import json
import logging
import base64
import hashlib
import random
import string

from urllib.parse import parse_qsl, urlparse

import requests
from requests.models import CaseInsensitiveDict

from oauthlib.common import add_params_to_uri, generate_nonce, to_unicode
from oauthlib.oauth2 import InsecureTransportError
from oauthlib.oauth2 import is_secure_transport

from carconnectivity.errors import AuthenticationError, RetrievalError, TemporaryAuthenticationError

from carconnectivity_connectors.skoda.auth.openid_session import AccessType
from carconnectivity_connectors.skoda.auth.skoda_web_session import SkodaWebSession

if TYPE_CHECKING:
    from typing import Set


LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.skoda.auth")


class MySkodaSession(SkodaWebSession):
    """
    MySkodaSession class handles the authentication and session management for Volkswagen's WeConnect service.
    """
    def __init__(self, session_user, **kwargs) -> None:
        super(MySkodaSession, self).__init__(client_id='7f045eee-7003-4379-9968-9355ed2adb06@apps_vw-dilab_com',
                                             refresh_url='https://mysmob.api.connect.skoda-auto.cz/api/v1/authentication/refresh-token?tokenType=CONNECT',
                                             scope='address badge birthdate cars driversLicense dealers email mileage mbb nationalIdentifier openid phone profession profile vin',
                                             redirect_uri='myskoda://redirect/login/',
                                             session_user=session_user,
                                             **kwargs)

        self.headers = CaseInsensitiveDict({
            'user-agent': 'Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 '
                          'Chrome/74.0.3729.185 Mobile Safari/537.36',
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,'
                      'application/signed-exchange;v=b3',
            'accept-language': 'en-US,en;q=0.9',
            'accept-encoding': 'gzip, deflate',
            'x-requested-with': 'cz.skodaauto.connect',
            'upgrade-insecure-requests': '1',
        })

    def login(self):
        super(MySkodaSession, self).login()

        verifier = "".join(random.choices(string.ascii_uppercase + string.digits, k=16))
        verifier_hash = hashlib.sha256(verifier.encode("utf-8")).digest()
        code_challenge = base64.b64encode(verifier_hash).decode("utf-8").replace("+", "-").replace("/", "_").rstrip("=")
        # retrieve authorization URL
        authorization_url = self.authorization_url(url='https://identity.vwgroup.io/oidc/v1/authorize', prompt='login', code_challenge=code_challenge,
                                                   code_challenge_method='s256')
        # perform web authentication
        response = self.do_web_auth(authorization_url)
        # fetch tokens from web authentication response
        self.fetch_tokens('https://mysmob.api.connect.skoda-auto.cz/api/v1/authentication/exchange-authorization-code?tokenType=CONNECT',
                          authorization_response=response, verifier=verifier)

    def refresh(self) -> None:
        # refresh tokens from refresh endpoint
        self.refresh_tokens(
            'https://mysmob.api.connect.skoda-auto.cz/api/v1/authentication/refresh-token?tokenType=CONNECT',
        )

    def fetch_tokens(
        self,
        token_url,
        authorization_response,
        verifier,
        **_
    ):
        """
        Fetches tokens using the given token URL using the tokens from authorization response.

        Args:
            token_url (str): The URL to request the tokens from.
            authorization_response (str, optional): The authorization response containing the tokens. Defaults to None.
            **_ : Additional keyword arguments.

        Returns:
            dict: A dictionary containing the fetched tokens if successful.
            None: If the tokens could not be fetched.

        Raises:
            TemporaryAuthenticationError: If the token request fails due to a temporary WeConnect failure.
        """
        # take token from authorization response (those are stored in self.token now!)
        self.parse_from_fragment(authorization_response)
        
        if self.token is not None and all(key in self.token for key in ('code', 'id_token')):
            # Generate json body for token request
            body: str = json.dumps(
                {
                    'redirectUri': 'myskoda://redirect/login/',
                    'code': self.token['code'],
                    'verifier': verifier
                })

            request_headers: CaseInsensitiveDict = self.headers  # pyright: ignore reportAssignmentType
            request_headers['accept'] = 'application/json'
            request_headers['content-type'] = 'application/json'

            # request tokens from token_url
            token_response = self.post(token_url, headers=request_headers, data=body, allow_redirects=False,
                                            access_type=AccessType.NONE)  # pyright: ignore reportCallIssue
            if token_response.status_code != requests.codes['ok']:
                raise TemporaryAuthenticationError(f'Token could not be fetched due to temporary WeConnect failure: {token_response.status_code}')
            # parse token from response body
            token = self.parse_from_body(token_response.text)
            return token
        return None

    def parse_from_body(self, token_response, state=None):
        """
            Fix strange token naming before parsing it with OAuthlib.
        """
        try:
            # Tokens are in body of response in json format
            token = json.loads(token_response)
        except json.decoder.JSONDecodeError as err:
            raise TemporaryAuthenticationError('Token could not be refreshed due to temporary WeConnect failure: json could not be decoded') from err
        found_tokens: Set[str] = set()
        # Fix token keys, we want access_token instead of accessToken
        if 'accessToken' in token:
            found_tokens.add('accessToken')
            token['access_token'] = token.pop('accessToken')
        # Fix token keys, we want id_token instead of idToken
        if 'idToken' in token:
            found_tokens.add('idToken')
            token['id_token'] = token.pop('idToken')
        # Fix token keys, we want refresh_token instead of refreshToken
        if 'refreshToken' in token:
            found_tokens.add('refreshToken')
            token['refresh_token'] = token.pop('refreshToken')
        LOG.debug(f'Found tokens in answer: {found_tokens}')
        # generate json from fixed dict
        fixed_token_response = to_unicode(json.dumps(token)).encode("utf-8")
        # Let OAuthlib parse the token
        return super(MySkodaSession, self).parse_from_body(token_response=fixed_token_response)

    def refresh_tokens(
        self,
        token_url,
        refresh_token=None,
        auth=None,
        timeout=None,
        headers=None,
        verify=True,
        proxies=None,
        **_
    ):
        """
        Refreshes the authentication tokens using the provided refresh token.
        Args:
            token_url (str): The URL to request new tokens from.
            refresh_token (str, optional): The refresh token to use. Defaults to None.
            auth (tuple, optional): Authentication credentials. Defaults to None.
            timeout (float or tuple, optional): How long to wait for the server to send data before giving up. Defaults to None.
            headers (dict, optional): Headers to include in the request. Defaults to None.
            verify (bool, optional): Whether to verify the server's TLS certificate. Defaults to True.
            proxies (dict, optional): Proxies to use for the request. Defaults to None.
            **_ (dict): Additional arguments.
        Raises:
            ValueError: If no token endpoint is set for auto_refresh.
            InsecureTransportError: If the token URL is not secure.
            AuthenticationError: If the server requests new authorization.
            TemporaryAuthenticationError: If the token could not be refreshed due to a temporary server failure.
            RetrievalError: If the status code from the server is not recognized.
        Returns:
            dict: The new tokens.
        """
        LOG.info('Refreshing tokens')
        if not token_url:
            raise ValueError("No token endpoint set for auto_refresh.")

        if not is_secure_transport(token_url):
            raise InsecureTransportError()

        refresh_token = refresh_token or self.refresh_token
        if refresh_token is None:
            self.login()
            return self.token

        # Generate json body for token request
        body: str = json.dumps(
            {
                'token': refresh_token,
            })

        request_headers: CaseInsensitiveDict = self.headers  # pyright: ignore reportAssignmentType
        request_headers['accept'] = 'application/json'
        request_headers['content-type'] = 'application/json'

        try:
            # request tokens from token_url
            token_response = self.post(token_url, headers=request_headers, data=body, allow_redirects=False,
                                            access_type=AccessType.NONE)  # pyright: ignore reportCallIssue
            if token_response.status_code == requests.codes['ok']:
                # parse token from response body
                token = self.parse_from_body(token_response.text)
                return token
            elif token_response.status_code == requests.codes['unauthorized']:
                LOG.info('Refreshing tokens failed: Server requests new authorization, will login now')
                self.login()
                return self.token
            else:
                raise TemporaryAuthenticationError(f'Token could not be fetched due to temporary MySkoda failure: {token_response.status_code}')
        except ConnectionError:
            self.login()
            return self.token

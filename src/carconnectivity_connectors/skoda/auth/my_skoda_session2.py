from typing import Dict, Optional, Match

import re
import json
import logging
import requests

from urllib.parse import parse_qsl, urlparse, urlsplit

from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

from oauthlib.common import add_params_to_uri, generate_nonce, to_unicode
from oauthlib.oauth2 import InsecureTransportError
from oauthlib.oauth2 import is_secure_transport

from requests.models import CaseInsensitiveDict
from carconnectivity_connectors.skoda.auth.openid_session import AccessType


from carconnectivity_connectors.skoda.auth.skoda_web_session import SkodaWebSession
from carconnectivity.errors import APICompatibilityError, AuthenticationError, RetrievalError, TemporaryAuthenticationError


LOG = logging.getLogger("weconnect")


class MySkodaSession2(SkodaWebSession):
    def __init__(self, session_user, **kwargs):
        super(MySkodaSession2, self).__init__(client_id='7f045eee-7003-4379-9968-9355ed2adb06@apps_vw-dilab_com',
                                               refresh_url='https://tokenrefreshservice.apps.emea.vwapps.io/refreshTokens',
                                               scope='openid dealers profile email cars address',
                                               redirect_uri='skodaconnect://oidc.login/',
                                               state=None,
                                               session_user=session_user,
                                               **kwargs)

        self.headers = CaseInsensitiveDict({
            'api-key': 'ok',
            'accept': 'application/json',
            'content-type': 'application/json;charset=utf-8',
            'user-agent': 'OneConnect/000000164 CFNetwork/1494.0.7 Darwin/23.4.0',
            'accept-language': 'de-de',
            'Pragma': 'no-cache'
        })

    def login(self):
        super(MySkodaSession2, self).login()
        authorizationUrl = self.authorizationUrl(url='https://identity.vwgroup.io/oidc/v1/authorize', scope='openid dealers profile email cars address')
        response = self.doWebAuth(authorizationUrl)
        self.fetchTokens('https://api.connect.skoda-auto.cz/api/v1/authentication/token?systemId=CONNECT',
                         authorization_response=response
                         )

    def refresh(self):
        self.refreshTokens(
            'https://tokenrefreshservice.apps.emea.vwapps.io/refreshTokens',
        )

    

    def doWebAuth(self, authorizationUrl):  # noqa: C901
        websession: requests.Session = requests.Session()
        retries = Retry(total=self.retries,
                        backoff_factor=0.1,
                        status_forcelist=[500],
                        raise_on_status=False)
        websession.proxies.update(self.proxies)
        websession.mount('https://', HTTPAdapter(max_retries=retries))
        websession.headers = CaseInsensitiveDict({
            'user-agent': 'Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 '
                          'Chrome/74.0.3729.185 Mobile Safari/537.36',
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,'
                      'application/signed-exchange;v=b3',
            'accept-language': 'en-US,en;q=0.9',
            'accept-encoding': 'gzip, deflate',
            'x-requested-with': 'cz.skodaauto.connect',
            'upgrade-insecure-requests': '1',
        })
        while True:
            loginFormResponse: requests.Response = websession.get(authorizationUrl, allow_redirects=False)
            if loginFormResponse.status_code == requests.codes['ok']:
                break
            elif loginFormResponse.status_code in (requests.codes['found'], requests.codes['see_other']):
                if 'Location' in loginFormResponse.headers:
                    authorizationUrl = loginFormResponse.headers['Location']
                else:
                    raise APICompatibilityError('Forwarding without Location in Header')
            elif loginFormResponse.status_code == requests.codes['internal_server_error']:
                raise RetrievalError('Temporary server error during login')
            else:
                raise APICompatibilityError('Retrieving credentials page was not successfull,'
                                            f' status code: {loginFormResponse.status_code}')

        # Find login form on page to obtain inputs
        emailFormRegex = r'<form.+id=\"emailPasswordForm\".*action=\"(?P<formAction>[^\"]+)\"[^>]*>' \
            r'(?P<formContent>.+?(?=</form>))</form>'
        match: Optional[Match[str]] = re.search(emailFormRegex, loginFormResponse.text, flags=re.DOTALL)
        if match is None:
            raise APICompatibilityError('No login email form found')
        # retrieve target url from form
        target: str = match.groupdict()['formAction']

        # Find all inputs and put those in formData dictionary
        inputRegex = r'<input[\\n\\r\s][^/]*name=\"(?P<name>[^\"]+)\"([\\n\\r\s]value=\"(?P<value>[^\"]+)\")?[^/]*/>'
        formData: Dict[str, str] = {}
        for match in re.finditer(inputRegex, match.groupdict()['formContent']):
            if match.groupdict()['name']:
                formData[match.groupdict()['name']] = match.groupdict()['value']
        if not all(x in ['_csrf', 'relayState', 'hmac', 'email'] for x in formData):
            raise APICompatibilityError('Could not find all required input fields in login page')

        # Set email to the provided username
        formData['email'] = self.session_user.username

        # build url from form action
        login2Url: str = 'https://identity.vwgroup.io' + target

        loginHeadersForm: CaseInsensitiveDict = websession.headers.copy()
        loginHeadersForm['Content-Type'] = 'application/x-www-form-urlencoded'

        # Post form content and retrieve credentials page
        login2Response: requests.Response = websession.post(login2Url, headers=loginHeadersForm, data=formData, allow_redirects=True)

        if login2Response.status_code != requests.codes['ok']:  # pylint: disable=E1101
            if login2Response.status_code == requests.codes['internal_server_error']:
                raise RetrievalError('Temporary server error during login')
            raise APICompatibilityError('Retrieving credentials page was not successfull,'
                                        f' status code: {login2Response.status_code}')

        credentialsTemplateRegex = r'<script>\s+window\._IDK\s+=\s+\{\s' \
            r'(?P<templateModel>.+?(?=\s+\};?\s+</script>))\s+\};?\s+</script>'
        match = re.search(credentialsTemplateRegex, login2Response.text, flags=re.DOTALL)
        if match is None:
            raise APICompatibilityError('No credentials form found')
        if match.groupdict()['templateModel']:
            lineRegex = r'\s*(?P<name>[^\:]+)\:\s+[\'\{]?(?P<value>.+)[\'\}][,]?'
            form2Data: Dict[str, str] = {}
            for match in re.finditer(lineRegex, match.groupdict()['templateModel']):
                if match.groupdict()['name'] == 'templateModel':
                    templateModelString = '{' + match.groupdict()['value'] + '}'
                    if templateModelString.endswith(','):
                        templateModelString = templateModelString[:-len(',')]
                    templateModel = json.loads(templateModelString)
                    if 'relayState' in templateModel:
                        form2Data['relayState'] = templateModel['relayState']
                    if 'hmac' in templateModel:
                        form2Data['hmac'] = templateModel['hmac']
                    if 'emailPasswordForm' in templateModel and 'email' in templateModel['emailPasswordForm']:
                        form2Data['email'] = templateModel['emailPasswordForm']['email']
                    if 'error' in templateModel and templateModel['error'] is not None:
                        if templateModel['error'] == 'validator.email.invalid':
                            raise AuthenticationError('Error during login, email invalid')
                        raise AuthenticationError(f'Error during login: {templateModel["error"]}')
                    if 'registerCredentialsPath' in templateModel and templateModel['registerCredentialsPath'] == 'register':
                        raise AuthenticationError(f'Error during login, account {self.session_user.username} does not exist')
                    if 'errorCode' in templateModel:
                        raise AuthenticationError('Error during login, is the username correct?')
                    if 'postAction' in templateModel:
                        target = templateModel['postAction']
                    else:
                        raise APICompatibilityError('Form does not contain postAction')
                elif match.groupdict()['name'] == 'csrf_token':
                    form2Data['_csrf'] = match.groupdict()['value']
        form2Data['password'] = self.session_user.password
        if not all(x in ['_csrf', 'relayState', 'hmac', 'email', 'password'] for x in form2Data):
            raise APICompatibilityError('Could not find all required input fields in login page')

        login3Url = f'https://identity.vwgroup.io/signin-service/v1/{self.client_id}/{target}'

        # Post form content and retrieve userId in forwarding Location
        login3Response: requests.Response = websession.post(login3Url, headers=loginHeadersForm, data=form2Data, allow_redirects=False)
        if login3Response.status_code not in (requests.codes['found'], requests.codes['see_other']):
            if login3Response.status_code == requests.codes['internal_server_error']:
                raise RetrievalError('Temporary server error during login')
            raise APICompatibilityError('Forwarding expected (status code 302),'
                                        f' but got status code {login3Response.status_code}')
        if 'Location' not in login3Response.headers:
            raise APICompatibilityError('No url for forwarding in response headers')

        # Parse parametes from forwarding url
        params: Dict[str, str] = dict(parse_qsl(urlsplit(login3Response.headers['Location']).query))

        # Check if error
        if 'error' in params and params['error']:
            errorMessages: Dict[str, str] = {
                'login.errors.password_invalid': 'Password is invalid',
                'login.error.throttled': 'Login throttled, probably too many wrong logins. You have to wait some'
                                         ' minutes until a new login attempt is possible'
            }
            if params['error'] in errorMessages:
                error = errorMessages[params['error']]
            else:
                error = params['error']
            raise AuthenticationError(error)

        # Check for user id
        if 'userId' not in params or not params['userId']:
            if 'updated' in params and params['updated'] == 'dataprivacy':
                raise AuthenticationError('You have to login at myskoda and accept the terms and conditions')
            raise APICompatibilityError('No user id provided. A possible reason is that you have to reconfirm the terms and conditions.')
        self.userId = params['userId']

        # Now follow the forwarding until forwarding URL starts with 'weconnect://authenticated#'
        afterLoginUrl: str = login3Response.headers['Location']

        consentURL = None
        while True:
            if 'consent' in afterLoginUrl:
                consentURL = afterLoginUrl
            if 'terms-and-conditions' in afterLoginUrl:
                raise AuthenticationError('It seems like you need to accept the terms and conditions for the MySkoda service.'
                                            f' Try to visit the URL "https://identity.vwgroup.io/{afterLoginUrl}" or log into the Volkswagen smartphone app')
            afterLoginResponse = self.get(afterLoginUrl, allow_redirects=False, access_type=AccessType.NONE)
            if afterLoginResponse.status_code == requests.codes['internal_server_error']:
                raise RetrievalError('Temporary server error during login')

            if 'Location' not in afterLoginResponse.headers:
                if consentURL is not None:
                    raise AuthenticationError('It seems like you need to accept the terms and conditions for the Volkswagen service.'
                                                f' Try to visit the URL "{consentURL}" or log into the Volkswagen smartphone app')
                raise APICompatibilityError('No Location for forwarding in response headers')

            afterLoginUrl = afterLoginResponse.headers['Location']

            if afterLoginUrl.startswith(self.redirect_uri):
                break

        if afterLoginUrl.startswith(self.redirect_uri + '#'):
            queryurl = afterLoginUrl.replace(self.redirect_uri + '#', 'https://egal?')
        else:
            queryurl = afterLoginUrl
        return queryurl

    def fetchTokens(
        self,
        token_url,
        authorization_response=None,
        **kwargs
    ):
        self.parseFromFragment(authorization_response)

        if all(key in self.token for key in ('state', 'id_token', 'access_token', 'code')):
            body: str = json.dumps(
                {
                    'state': self.token['state'],
                    'id_token': self.token['id_token'],
                    'redirect_uri': self.redirect_uri,
                    'region': 'emea',
                    'access_token': self.token['access_token'],
                    'authorizationCode': self.token['code'],
                })

            loginHeadersForm: CaseInsensitiveDict = self.headers
            loginHeadersForm['accept'] = 'application/json'

            tokenResponse = self.post(token_url, headers=loginHeadersForm, data=body, allow_redirects=False, access_type=AccessType.ID)
            if tokenResponse.status_code != requests.codes['ok']:
                raise TemporaryAuthenticationError(f'Token could not be fetched due to temporary WeConnect failure: {tokenResponse.status_code}')
            token = self.parseFromBody(tokenResponse.text)

            return token

    def parseFromBody(self, token_response, state=None):
        try:
            token = json.loads(token_response)
        except json.decoder.JSONDecodeError:
            raise TemporaryAuthenticationError('Token could not be refreshed due to temporary WeConnect failure: json could not be decoded')
        if 'accessToken' in token:
            token['access_token'] = token.pop('accessToken')
        if 'idToken' in token:
            token['id_token'] = token.pop('idToken')
        if 'refreshToken' in token:
            token['refresh_token'] = token.pop('refreshToken')
        fixedTokenresponse = to_unicode(json.dumps(token)).encode("utf-8")
        return super(MySkodaSession2, self).parseFromBody(token_response=fixedTokenresponse, state=state)

    def refreshTokens(
        self,
        token_url,
        refresh_token=None,
        auth=None,
        timeout=None,
        headers=None,
        verify=True,
        proxies=None,
        **kwargs
    ):
        LOG.info('Refreshing tokens')
        if not token_url:
            raise ValueError("No token endpoint set for auto_refresh.")

        if not is_secure_transport(token_url):
            raise InsecureTransportError()

        refresh_token = refresh_token or self.refreshToken

        if headers is None:
            headers = self.headers

        if headers is None:
            headers = self.headers

        body: Dict[str, str] = {
            'brand': 'skoda',
            'client_id': self.client_id,
            'client_secret': 'eb8814e641c81a2640ad62eeccec11c98effc9bccd4269ab7af338b50a94b3a2',
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token
        }

        headers['content-type'] = 'application/x-www-form-urlencoded; charset=utf-8'

        tokenResponse = self.post(
            token_url,
            data=body,
            auth=auth,
            timeout=timeout,
            headers=headers,
            verify=verify,
            withhold_token=False,
            proxies=proxies,
            access_type=AccessType.NONE
        )
        if tokenResponse.status_code == requests.codes['unauthorized']:
            raise AuthenticationError('Refreshing tokens failed: Server requests new authorization')
        elif tokenResponse.status_code in (requests.codes['internal_server_error'], requests.codes['service_unavailable'], requests.codes['gateway_timeout']):
            raise TemporaryAuthenticationError('Token could not be refreshed due to temporary WeConnect failure: {tokenResponse.status_code}')
        elif tokenResponse.status_code == requests.codes['ok']:
            self.parseFromBody(tokenResponse.text)
            if "refresh_token" not in self.token:
                LOG.debug("No new refresh token given. Re-using old.")
                self.token["refresh_token"] = refresh_token
            return self.token
        else:
            print(tokenResponse.text)
            raise RetrievalError(f'Status Code from WeConnect while refreshing tokens was: {tokenResponse.status_code}')

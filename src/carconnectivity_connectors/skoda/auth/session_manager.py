from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from typing import Dict, Any

from enum import Enum

import hashlib

import json
import logging

from requests import Session

from carconnectivity_connectors.skoda.auth.my_skoda_session import MySkodaSession

LOG = logging.getLogger("carconnectivity.connectors.skoda.auth")


class SessionUser():
    def __init__(self, username: str, password: str) -> None:
        self.username: str = username
        self.password: str = password

    def __str__(self) -> str:
        return f'{self.username}:{self.password}'


class Service(Enum):
    MY_SKODA = 'MySkoda'

    def __str__(self) -> str:
        return self.value


class SessionManager():
    def __init__(self, tokenstore: Dict[str, Any], cache:  Dict[str, Any]) -> None:
        self.tokenstore: Dict[str, Any] = tokenstore
        self.cache: Dict[str, Any] = cache
        self.sessions: dict[tuple[Service, SessionUser], Session] = {}

    @staticmethod
    def generate_hash(service: Service, session_user: SessionUser) -> str:
        hash_str: str = service.value + str(session_user)
        return hashlib.sha512(hash_str.encode()).hexdigest()

    @staticmethod
    def generate_identifier(service: Service, session_user: SessionUser) -> str:
        return 'CarConnectivity-connector-skoda:' + SessionManager.generate_hash(service, session_user)

    def get_session(self, service: Service, session_user: SessionUser) -> Session:
        session = None
        if (service, session_user) in self.sessions:
            return self.sessions[(service, session_user)]

        identifier: str = SessionManager.generate_identifier(service, session_user)
        token = None
        cache = {}
        metadata = {}

        if identifier in self.tokenstore:
            if 'token' in self.tokenstore[identifier]:
                LOG.info('Reusing tokens from previous session')
                token = self.tokenstore[identifier]['token']
            if 'metadata' in self.tokenstore[identifier]:
                metadata = self.tokenstore[identifier]['metadata']
        if identifier in self.cache:
            cache = self.cache[identifier]

        if service == Service.MY_SKODA:
            session = MySkodaSession(session_user=session_user, token=token, metadata=metadata, cache=cache)
        self.sessions[(service, session_user)] = session
        return session

    def persist(self) -> None:
        for (service, user), session in self.sessions.items():
            if session.token is not None:
                identifier: str = SessionManager.generate_identifier(service, user)
                self.tokenstore[identifier] = {}
                self.tokenstore[identifier]['token'] = session.token
                self.tokenstore[identifier]['metadata'] = session.metadata
                self.cache[identifier] = session.cache

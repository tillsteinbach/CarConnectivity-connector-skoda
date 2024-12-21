from carconnectivity_connectors.skoda.auth.openid_session import OpenIDSession


class SkodaWebSession(OpenIDSession):
    def __init__(self, session_user, cache, **kwargs):
        super(SkodaWebSession, self).__init__(**kwargs)
        self.session_user = session_user
        self.cache = cache

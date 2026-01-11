""" User interface for the Skoda connector in the Car Connectivity application. """
from __future__ import annotations
from typing import TYPE_CHECKING

from datetime import datetime, timedelta
import time

import os

import flask
from flask_login import login_required

from carconnectivity_connectors.base.ui.connector_ui import BaseConnectorUI

if TYPE_CHECKING:
    from typing import Optional, List, Dict, Union, Literal

    from carconnectivity_connectors.base.connector import BaseConnector


class ConnectorUI(BaseConnectorUI):
    """
    A user interface class for the Skoda connector in the Car Connectivity application.
    """
    def __init__(self, connector: BaseConnector, app: flask.Flask, *args, **kwargs):
        blueprint: Optional[flask.Blueprint] = flask.Blueprint(name=connector.id, import_name='carconnectivity-connector-skoda', url_prefix=f'/{connector.id}',
                                                               template_folder=os.path.dirname(__file__) + '/templates')
        super().__init__(connector, blueprint=blueprint, app=app, *args, **kwargs)

        @self.blueprint.route('/', methods=['GET'])
        def root():
            return flask.redirect(flask.url_for('connectors.'+self.blueprint.name+'.status'))

        @self.blueprint.route('/status', methods=['GET'])
        @login_required
        def status():
            return flask.render_template('skoda/status.html', current_app=flask.current_app, connector=self.connector,
                                         monotonic_zero=datetime.now()-timedelta(seconds=time.monotonic()))

    def get_nav_items(self) -> List[Dict[Literal['text', 'url', 'sublinks', 'divider'], Union[str, List]]]:
        """
        Generates a list of navigation items for the Skoda connector UI.
        """
        return super().get_nav_items() + [{"text": "Status", "url": flask.url_for('connectors.'+self.blueprint.name+'.status')}]

    def get_title(self) -> str:
        """
        Returns the title of the connector.

        Returns:
            str: The title of the connector, which is "Skoda".
        """
        return "Skoda"

import os
import sys
import tempfile
import logging
import requests
from requests.exceptions import ConnectionError

from flask import Flask
from flask import jsonify
from flask import abort
from flask.ext.sqlalchemy import SQLAlchemy
from flask.ext.restful import Api
from flask.ext.migrate import Migrate

logging.basicConfig(level=logging.DEBUG)

app = Flask(__name__)
db = SQLAlchemy(app)
migrate = Migrate(app, db)

from helpers import http_request
from application.modules.settings.model import Setting

try:
    from application import config
    app.config.from_object(config.Config)
except ImportError:
    """If a config is not defined, we use the default settings, importing the
    BLENDER_PATH and SETTINGS_PATH from the server.
    """
    logging.error("No config.py file found, importing config from Server.")

    app.config['FLAMENCO_SERVER'] = 'localhost:9999'
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(os.path.dirname(__file__), '../manager.sqlite')
    app.config['TMP_FOLDER'] = tempfile.gettempdir()
    app.config['THUMBNAIL_EXTENSIONS'] = set(['png'])
    app.config['MANAGER_STORAGE'] = '{0}/static/storage'.format(
        os.path.join(os.path.dirname(__file__)))

api = Api(app)

from modules.tasks import TaskFileApi
from modules.tasks import TaskManagementApi
from modules.tasks import TaskApi
from modules.tasks import TaskThumbnailListApi
from modules.tasks import TaskCompiledApi
from modules.tasks import TaskZipApi
from modules.tasks import TaskSupZipApi
from modules.tasks import TaskDepZipApi
api.add_resource(TaskFileApi, '/tasks/file/<int:job_id>')
api.add_resource(TaskManagementApi, '/tasks')
api.add_resource(TaskApi, '/tasks/<int:task_id>')
api.add_resource(TaskThumbnailListApi, '/tasks/thumbnails')
api.add_resource(TaskCompiledApi, '/tasks/compiled/<int:task_id>')
api.add_resource(TaskZipApi, '/tasks/zip/<int:job_id>')
api.add_resource(TaskSupZipApi, '/tasks/zip/sup/<int:job_id>')
api.add_resource(TaskDepZipApi, '/tasks/zip/dep/<int:job_id>')

from modules.workers import WorkerListApi
from modules.workers import WorkerApi
from modules.workers import WorkerStatusApi
api.add_resource(WorkerListApi, '/workers')
api.add_resource(WorkerApi, '/workers/<int:worker_id>')
api.add_resource(WorkerStatusApi, '/workers/status/<int:worker_id>')

from modules.settings import SettingsListApi
from modules.settings import SettingApi
api.add_resource(SettingsListApi, '/settings')
api.add_resource(SettingApi, '/settings/<string:name>')

from modules.job_types import JobTypeListApi
from modules.job_types import JobTypeApi
api.add_resource(JobTypeListApi, '/job-types')
api.add_resource(JobTypeApi, '/job-types/<string:name>')

def register_manager(host, name, has_virtual_workers):
    """This is going to be an HTTP request to the server with all the info for
    registering the render node. This is called by the runserver script.
    """
    import httplib
    import socket
    import time
    while True:
        try:
            connection = httplib.HTTPConnection(app.config['FLAMENCO_SERVER'])
            connection.request("GET", "/managers")
            break
        except socket.error:
            print ("Can't connect with Server, retrying...")
        time.sleep(1)

    params = dict(
        host=host,
        name=name,
        has_virtual_workers=has_virtual_workers)

    # Search in the settings if we have a uuid for the manager
    token = Setting.query.filter_by(name='token').first()
    if token:
        params['token'] = token.value

    r = http_request(app.config['FLAMENCO_SERVER'], '/managers', 'post', params=params)

    # If we don't find one, we proceed to create it, using the server reponse
    if not token:
        token = Setting(name='token', value=r['token'])
        db.session.add(token)
        db.session.commit()
    else:
        token.value = r['token']
        db.session.commit()


@app.route('/')
def index():
    return jsonify(message='Flamenco manager up and running!')


@app.errorhandler(404)
def not_found(error):
    response = jsonify({'code' : 404, 'message' : 'No interface defined for URL'})
    response.status_code = 404
    return response

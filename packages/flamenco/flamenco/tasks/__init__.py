"""Task management."""

import attr

import werkzeug.exceptions as wz_exceptions

from pillar import attrs_extra
from pillar.web.system_util import pillar_api

from pillarsdk.exceptions import ResourceNotFound

# Keep this synced with _config.sass
COLOR_FOR_TASK_STATUS = {
    'queued': '#b4bbaa',
    'canceled': '#999',
    'failed': '#ff8080',
    'claimed-by-manager': '#d1c5d3',
    'processing': '#ffbe00',
    'active': '#00ceff',
    'completed': '#bbe151',
}


@attr.s
class TaskManager(object):
    _log = attrs_extra.log('%s.TaskManager' % __name__)

    def api_create_task(self, job, commands, name, parents=None):
        """Creates a task in MongoDB for the given job, executing commands.

        Returns the ObjectId of the created task.
        """

        from eve.methods.post import post_internal

        task = {
            'job': job['_id'],
            'manager': job['manager'],
            'user': job['user'],
            'name': name,
            'status': 'queued',
            'job_type': job['job_type'],
            'commands': [cmd.to_dict() for cmd in commands],
            'priority': job['priority'],
            'project': job['project'],
        }
        # Insertion of None parents is not supported
        if parents:
            task['parents'] = parents

        self._log.info('Creating task %s for manager %s, user %s',
                       name, job['manager'], job['user'])

        r, _, _, status = post_internal('flamenco_tasks', task)
        if status != 201:
            self._log.error('Error %i creating task %s: %s',
                            status, task, r)
            raise wz_exceptions.InternalServerError('Unable to create task')

        return r['_id']

    def tasks_for_job(self, job_id, status=None, page=1):
        from .sdk import Task

        api = pillar_api()
        payload = {
            'where': {
                'job': unicode(job_id),
            }}
        if status:
            payload['where']['status'] = status
        tasks = Task.all(payload, api=api)
        return tasks

    def tasks_for_project(self, project_id):
        """Returns the tasks for the given project.

        :returns: {'_items': [task, task, ...], '_meta': {Eve metadata}}
        """
        from .sdk import Task

        api = pillar_api()
        try:
            tasks = Task.all({
                'where': {
                    'project': project_id,
                }}, api=api)
        except ResourceNotFound:
            return {'_items': [], '_meta': {'total': 0}}

        return tasks


def setup_app(app):
    from . import eve_hooks, patch

    eve_hooks.setup_app(app)
    patch.setup_app(app)

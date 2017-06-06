import logging
import pathlib

import bson

from pillar import current_app
from pillar.api.utils import dumps

from flamenco import current_flamenco

log = logging.getLogger(__name__)


class ArchivalError(Exception):
    """Raised when there was an error archiving a job."""


@current_app.celery.task(ignore_result=True)
def archive_job(job_id: str):
    """Archives a given job.

    - Sets job status "archiving" (if not already that status).
    - For each task, de-chunks the task logs and gz-compresses them.
    - Creates a ZIP file with the job+task definitions in JSON and compressed logs.
    - Uploads the ZIP to the project's file storage.
    - Records the link of the ZIP in the job document.
    - Deletes the tasks and task logs in MongoDB.
    - Sets the job status to "archived".
    """
    import tempfile
    import celery

    job_oid = bson.ObjectId(job_id)
    log.info('Archiving job %s', job_oid)

    # Create a temporary directory for the file operations.
    storage_path = tempfile.mkdtemp(prefix=f'job-archival-{job_id}-')
    zip_path = pathlib.Path(storage_path) / f'flamenco-job-{job_id}.zip'
    log.info('Job archival path: %s', storage_path)

    # TODO: store the ZIP link in the job JSON in MongoDB.

    # Write the job to JSON.
    jobs_coll = current_flamenco.db('jobs')
    job = jobs_coll.find_one({'_id': job_oid})
    # TODO: move original job status from 'pre-archive-status' to 'status'
    job_json_path = pathlib.Path(storage_path) / f'job-{job_id}.json'
    with job_json_path.open(mode='w', encoding='utf8') as outfile:
        outfile.write(dumps(job, indent=4, sort_keys=True))

    # Set job status to 'archiving'.
    res = current_flamenco.job_manager.api_set_job_status(job_oid, 'archiving')
    if res.matched_count != 1:
        raise ArchivalError(f'Unable to update job {job_oid}, matched count={res.matched_count}')

    # Run each task log compression in a separate Celery task.
    tasks_coll = current_flamenco.db('tasks')
    tasks = tasks_coll.find({'job': job_oid}, {'_id': 1})

    task_group = celery.group(*(
        download_task_and_log.si(storage_path, str(task['_id']))
        for task in tasks
    ))

    chain = (
        task_group |
        create_upload_zip.si(job_id, str(job['project']), storage_path, str(zip_path)) |
        update_mongo.si(job_id) |
        cleanup.si(storage_path)
    )
    chain()


# Unable to ignore results, see
# http://docs.celeryproject.org/en/latest/userguide/canvas.html#chord-important-notes
@current_app.celery.task(ignore_result=False)
def download_task_and_log(storage_path: str, task_id: str):
    """Downloads task + task log and stores them."""

    import gzip
    import pymongo

    task_oid = bson.ObjectId(task_id)
    log.info('Archiving task %s to %s', task_oid, storage_path)

    tasks_coll = current_flamenco.db('tasks')
    logs_coll = current_flamenco.db('task_logs')

    task = tasks_coll.find_one({'_id': task_oid})
    logs = logs_coll.find({'task': task_oid}).sort([
        ('received_on_manager', pymongo.ASCENDING),
        ('_id', pymongo.ASCENDING),
    ])

    # Save the task as JSON
    spath = pathlib.Path(storage_path)
    task_path = spath / f'task-{task_id}.json'
    with open(task_path, mode='w', encoding='utf8') as outfile:
        outfile.write(dumps(task, indent=4, sort_keys=True))

    # Get the task log bits and write to compressed file.
    log_path = spath / f'task-{task_id}.log.gz'
    with gzip.open(log_path, mode='wb') as outfile:
        for log_entry in logs:
            outfile.write(log_entry['log'].encode())


@current_app.celery.task(ignore_result=True)
def create_upload_zip(job_id: str, project_id: str, storage_path: str, zip_path: str):
    """Uploads the ZIP file to the storage backend.

    Also stores the link to the ZIP in the job document.
    """

    import itertools
    import zipfile

    from pillar.api.file_storage_backends import default_storage_backend

    log.info('Creating ZIP %s', zip_path)
    spath = pathlib.Path(storage_path)
    zpath = pathlib.Path(zip_path)

    with zipfile.ZipFile(zip_path, mode='w', compression=zipfile.ZIP_DEFLATED) as outfile:
        for fpath in itertools.chain(spath.glob('*.gz'),
                                     spath.glob('*.json')):
            outfile.write(fpath, fpath.name)

    bucket = default_storage_backend(project_id)
    blob = bucket.blob(f'flamenco-jobs/{zpath.name}')

    log.info('Uploading ZIP %s to %s', zpath, blob)

    file_size = zpath.stat().st_size
    with zpath.open(mode='rb') as stream_to_upload:
        blob.create_from_file(stream_to_upload,
                              file_size=file_size,
                              content_type='application/zip')


@current_app.celery.task(ignore_result=True)
def update_mongo(job_id: str):
    """Updates MongoDB by removing tasks and logs, and setting the job status."""

    job_oid = bson.ObjectId(job_id)
    tasks_coll = current_flamenco.db('tasks')
    logs_coll = current_flamenco.db('task_logs')

    log.info('Purging Flamenco tasks and task logs for job %s', job_id)

    # Task log entries don't have a job ID, so we have to fetch the task IDs first.
    task_ids = [
        task['_id']
        for task in tasks_coll.find({'job': job_oid})
    ]
    logs_coll.delete_many({'task_id': {'$in': task_ids}})
    tasks_coll.delete_many({'job': job_oid})

    # Update the job status to 'archived'
    res = current_flamenco.job_manager.api_set_job_status(job_oid, 'archived')
    if res.matched_count != 1:
        raise ArchivalError(
            f"Unable to update job {job_oid} to status 'archived', "
            f"matched count={res.matched_count}")


@current_app.celery.task(ignore_result=True)
def cleanup(storage_path: str):
    """Removes the temporary storage path."""

    import shutil

    log.info('Removing temporary job archival path %r', storage_path)
    shutil.rmtree(storage_path)
from pillarsdk.resource import List
from pillarsdk.resource import Find


class Task(List, Find):
    """Task class wrapping the REST nodes endpoint
    """
    path = 'flamenco/tasks'
    ensure_query_projections = {'project': 1, 'job': 1}


class TaskLog(List, Find):
    """Task log class wrapping the REST nodes endpoint
    """
    path = 'flamenco/task_logs'

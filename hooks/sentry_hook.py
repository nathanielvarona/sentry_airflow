from airflow import configuration as conf
from airflow.exceptions import AirflowException
from airflow.hooks.base_hook import BaseHook
from airflow.models import TaskInstance
from airflow.utils.db import provide_session
from airflow.utils.state import State

from sentry_sdk import (
    configure_scope,
    push_scope,
    capture_exception,
    add_breadcrumb,
    init,
)
from sentry_sdk.integrations.logging import ignore_logger
from sentry_sdk.integrations.flask import FlaskIntegration
from sqlalchemy import exc, or_

SCOPE_TAGS = frozenset(("task_id", "dag_id", "execution_date", "operator"))
SCOPE_CRUMBS = frozenset(
    ("dag_id", "task_id", "execution_date", "state", "operator", "duration")
)


@provide_session
def get_task_instances(dag_id, task_ids, execution_date, session=None):
    """
    Retrieve attribute from task.
    """
    if session is None or not task_ids:
        return []
    TI = TaskInstance
    ti = (
        session.query(TI)
        .filter(
            TI.dag_id == dag_id,
            TI.task_id.in_(task_ids),
            TI.execution_date == execution_date,
            or_(TI.state == State.SUCCESS, TI.state == State.FAILED),
        )
        .all()
    )
    return ti


def add_tagging(task_instance):
    """
    Add customized tagging to TaskInstances.
    """
    with configure_scope() as scope:
        for tag_name in SCOPE_TAGS:
            attribute = getattr(task_instance, tag_name)
            if tag_name == "operator":
                attribute = task_instance.task.__class__.__name__
            scope.set_tag(tag_name, attribute)


@provide_session
def add_breadcrumbs(task_instance, session=None):
    """
    Add customized breadcrumbs to TaskInstances.
    """
    task_ids = task_instance.task.dag.task_ids
    execution_date = task_instance.execution_date
    dag_id = task_instance.dag_id
    task_instances = get_task_instances(dag_id, task_ids, execution_date, session)
    for ti in task_instances:
        data = {}
        for crumb_tag in SCOPE_CRUMBS:
            data[crumb_tag] = getattr(ti, crumb_tag)

        add_breadcrumb(category="completed_tasks", data=data, level="info")


@provide_session
def add_sentry(task_instance, *args, session=None, **kwargs):
    """
    Create a scope for tagging and breadcrumbs in TaskInstance._run_raw_task.
    """
    # Avoid leaking tags by using push_scope.
    with push_scope():
        add_tagging(task_instance)
        add_breadcrumbs(task_instance, session)
        try:
            original_run_raw_task(task_instance, *args, session=session, **kwargs)
        except Exception:
            capture_exception()
            raise

def get_dsn(conn):
    if None in (conn.conn_type, conn.login):
        return conn.host

    dsn = '{conn.conn_type}://{conn.login}@{conn.host}/{conn.schema}'.format(conn=conn)
    return dsn

class SentryHook(BaseHook):
    """
    Wrap around the Sentry SDK.
    """

    def __init__(self, sentry_conn_id="sentry_dsn"):
        ignore_logger("airflow.task")
        ignore_logger("airflow.jobs.backfill_job.BackfillJob")
        executor_name = conf.get("core", "EXECUTOR")

        sentry_flask = FlaskIntegration()
        integrations = [sentry_flask]

        if executor_name == "CeleryExecutor":
            from sentry_sdk.integrations.celery import CeleryIntegration

            sentry_celery = CeleryIntegration()
            integrations += [sentry_celery]

        try:
            dsn = None
            conn = self.get_connection(sentry_conn_id)
            dsn = get_dsn(conn)
            init_args = {'dsn':dsn, 'integrations':integrations}

            environment = conn.extra_dejson.get('environment')
            if environment:
                init_args.update({'environment':environment})

        except (AirflowException, exc.OperationalError, exc.ProgrammingError):
            self.log.debug("Sentry defaulting to environment variable.")
            init_args = {'integrations':integrations}

        init(**init_args)

        TaskInstance._run_raw_task = add_sentry
        TaskInstance._sentry_integration_ = True


if not getattr(TaskInstance, "_sentry_integration_", False):
    original_run_raw_task = TaskInstance._run_raw_task
    SentryHook()

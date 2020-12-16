# MIT License
#
# Copyright (c) 2018-2019 Red Hat, Inc.

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
This file defines classes for job handlers specific for Github hooks
"""
import logging
from typing import Callable, Optional

from celery.app.task import Task

from ogr.abstract import CommitStatus, GitProject
from packit.api import PackitAPI
from packit.config import (
    JobConfig,
    JobType,
)
from packit.config.aliases import get_branches
from packit.config.package_config import PackageConfig
from packit.exceptions import PackitException
from packit.local_project import LocalProject
from packit_service import sentry_integration
from packit_service.constants import (
    FAQ_URL_HOW_TO_RETRIGGER,
    FILE_DOWNLOAD_FAILURE,
    MSG_RETRIGGER,
    PERMISSIONS_ERROR_WRITE_OR_ADMIN,
    RETRY_LIMIT,
)
from packit_service.models import (
    AbstractTriggerDbType,
    CoprBuildModel,
    InstallationModel,
)
from packit_service.service.events import (
    EventData,
    InstallationEvent,
    MergeRequestGitlabEvent,
    PullRequestGithubEvent,
    PushGitHubEvent,
    ReleaseEvent,
)
from packit_service.worker.build import CoprBuildJobHelper
from packit_service.worker.build.koji_build import KojiBuildJobHelper
from packit_service.worker.handlers import (
    JobHandler,
)
from packit_service.worker.handlers.abstract import (
    TaskName,
    configured_as,
    required_for,
    run_for_comment,
)
from packit_service.worker.result import TaskResults
from packit_service.worker.testing_farm import TestingFarmJobHelper
from packit_service.worker.whitelist import Whitelist

logger = logging.getLogger(__name__)


class GithubAppInstallationHandler(JobHandler):
    task_name = TaskName.installation

    # https://developer.github.com/v3/activity/events/types/#events-api-payload-28

    def __init__(
        self,
        package_config: PackageConfig,
        job_config: JobConfig,
        data: EventData,
        installation_event: InstallationEvent,
    ):
        super().__init__(
            package_config=package_config,
            job_config=job_config,
            data=data,
        )
        self.installation_event = installation_event
        self.account_type = installation_event.account_type
        self.account_login = installation_event.account_login
        self.sender_login = installation_event.sender_login
        self._project = self.service_config.get_project(
            url="https://github.com/packit/notifications"
        )

    def run(self) -> TaskResults:
        """
        Discover information about organization/user which wants to install packit on his repository
        Try to whitelist automatically if mapping from github username to FAS account can prove that
        user is a packager.
        :return: TaskResults
        """
        InstallationModel.create(event=self.installation_event)
        # try to add user to whitelist
        whitelist = Whitelist(
            fas_user=self.service_config.fas_user,
            fas_password=self.service_config.fas_password,
        )
        if not whitelist.add_account(self.account_login, self.sender_login):
            # Create an issue in our repository, so we are notified when someone install the app
            self.project.create_issue(
                title=f"{self.account_type} {self.account_login} needs to be approved.",
                body=(
                    f"Hi @{self.sender_login}, we need to approve you in "
                    "order to start using Packit-as-a-Service. Someone from our team will "
                    "get back to you shortly.\n\n"
                    "For more info, please check out the documentation: "
                    "http://packit.dev/packit-as-a-service/"
                ),
            )
            msg = f"{self.account_type} {self.account_login} needs to be approved manually!"
        else:
            msg = f"{self.account_type} {self.account_login} whitelisted!"

        logger.info(msg)
        return TaskResults(success=True, details={"msg": msg})


@configured_as(job_type=JobType.propose_downstream)
@run_for_comment(command="propose-downstream")
class ProposeDownstreamHandler(JobHandler):
    task_name = TaskName.propose_downstream

    def __init__(
        self,
        package_config: PackageConfig,
        job_config: JobConfig,
        data: EventData,
        task: Task,
    ):
        super().__init__(
            package_config=package_config,
            job_config=job_config,
            data=data,
        )
        self.task = task

    def run(self) -> TaskResults:
        """
        Sync the upstream release to dist-git as a pull request.
        """

        self.local_project = LocalProject(
            git_project=self.project,
            working_dir=self.service_config.command_handler_work_dir,
        )

        self.api = PackitAPI(self.service_config, self.job_config, self.local_project)

        errors = {}
        for branch in get_branches(
            *self.job_config.metadata.dist_git_branches, default="master"
        ):
            try:
                self.api.sync_release(dist_git_branch=branch, tag=self.data.tag_name)
            except Exception as ex:
                # the archive has not been uploaded to PyPI yet
                if FILE_DOWNLOAD_FAILURE in str(ex):
                    # retry for the archive to become available
                    logger.info(f"We were not able to download the archive: {ex}")
                    # when the task hits max_retries, it raises MaxRetriesExceededError
                    # and the error handling code would be never executed
                    retries = self.task.request.retries
                    if retries < RETRY_LIMIT:
                        logger.info(f"Retrying for the {retries + 1}. time...")
                        self.task.retry(exc=ex, countdown=15 * 2 ** retries)
                sentry_integration.send_to_sentry(ex)
                errors[branch] = str(ex)

        if errors:
            branch_errors = ""
            for branch, err in sorted(
                errors.items(), key=lambda branch_error: branch_error[0]
            ):
                err_without_new_lines = err.replace("\n", " ")
                branch_errors += f"| `{branch}` | `{err_without_new_lines}` |\n"

            body_msg = (
                f"Packit failed on creating pull-requests in dist-git:\n\n"
                f"| dist-git branch | error |\n"
                f"| --------------- | ----- |\n"
                f"{branch_errors}\n\n"
                f"{MSG_RETRIGGER.format(job='update', command='propose-update', place='issue')}\n"
            )

            self.project.create_issue(
                title=f"[packit] Propose update failed for release {self.data.tag_name}",
                body=body_msg,
            )

            return TaskResults(
                success=False,
                details={"msg": "Propose update failed.", "errors": errors},
            )

        return TaskResults(success=True, details={})


@configured_as(job_type=JobType.copr_build)
@configured_as(job_type=JobType.build)
@required_for(job_type=JobType.tests)
@run_for_comment(command="build")
@run_for_comment(command="copr-build")
class CoprBuildHandler(JobHandler):
    task_name = TaskName.copr_build

    def __init__(
        self,
        package_config: PackageConfig,
        job_config: JobConfig,
        data: EventData,
    ):
        super().__init__(
            package_config=package_config,
            job_config=job_config,
            data=data,
        )

        self._copr_build_helper: Optional[CoprBuildJobHelper] = None

    @property
    def copr_build_helper(self) -> CoprBuildJobHelper:
        if not self._copr_build_helper:
            self._copr_build_helper = CoprBuildJobHelper(
                service_config=self.service_config,
                package_config=self.package_config,
                project=self.project,
                metadata=self.data,
                db_trigger=self.data.db_trigger,
                job_config=self.job_config,
            )
        return self._copr_build_helper

    def run(self) -> TaskResults:

        if self.data.event_type in (
            PullRequestGithubEvent.__name__,
            MergeRequestGitlabEvent.__name__,
        ):
            user_can_merge_pr = self.project.can_merge_pr(self.data.user_login)
            if not (
                user_can_merge_pr or self.data.user_login in self.service_config.admins
            ):
                self.copr_build_helper.report_status_to_all(
                    description=PERMISSIONS_ERROR_WRITE_OR_ADMIN,
                    state=CommitStatus.failure,
                    url=FAQ_URL_HOW_TO_RETRIGGER,
                )
                return TaskResults(
                    success=True, details={"msg": PERMISSIONS_ERROR_WRITE_OR_ADMIN}
                )

        return self.copr_build_helper.run_copr_build()

    def pre_check(self) -> bool:
        is_copr_build: Callable[[JobConfig], bool] = (
            lambda job: job.type == JobType.copr_build
            and job.trigger == self.job_config.trigger
        )

        if self.job_config.type == JobType.tests and any(
            filter(is_copr_build, self.package_config.jobs)
        ):
            logger.info(
                "Skipping build for testing. The COPR build is defined "
                "in the config with the same trigger."
            )
            return False
        return True


@configured_as(job_type=JobType.production_build)
@run_for_comment(command="production-build")
class KojiBuildHandler(JobHandler):
    task_name = TaskName.koji_build

    def __init__(
        self,
        package_config: PackageConfig,
        job_config: JobConfig,
        data: EventData,
    ):
        super().__init__(
            package_config=package_config,
            job_config=job_config,
            data=data,
        )

        if not (
            self.data.event_type
            in (
                PullRequestGithubEvent.__name__,
                PushGitHubEvent.__name__,
                ReleaseEvent.__name__,
            )
        ):
            raise PackitException(
                "Unknown event, only "
                "PullRequestEvent, ReleaseEvent, and PushGitHubEvent "
                "are accepted."
            )

        # lazy property
        self._koji_build_helper: Optional[KojiBuildJobHelper] = None
        self._project: Optional[GitProject] = None

    @property
    def koji_build_helper(self) -> KojiBuildJobHelper:
        if not self._koji_build_helper:
            self._koji_build_helper = KojiBuildJobHelper(
                service_config=self.service_config,
                package_config=self.package_config,
                project=self.project,
                metadata=self.data,
                db_trigger=self.data.db_trigger,
                job_config=self.job_config,
            )
        return self._koji_build_helper

    def run(self) -> TaskResults:
        if self.data.event_type == PullRequestGithubEvent.__name__:
            user_can_merge_pr = self.project.can_merge_pr(self.data.user_login)
            if not (
                user_can_merge_pr or self.data.user_login in self.service_config.admins
            ):
                self.koji_build_helper.report_status_to_all(
                    description=PERMISSIONS_ERROR_WRITE_OR_ADMIN,
                    state=CommitStatus.failure,
                )
                return TaskResults(
                    success=True, details={"msg": PERMISSIONS_ERROR_WRITE_OR_ADMIN}
                )
        return self.koji_build_helper.run_koji_build()

    def pre_check(self) -> bool:
        is_copr_build: Callable[[JobConfig], bool] = (
            lambda job: job.type == JobType.copr_build
        )

        if self.job_config.type == JobType.tests and any(
            filter(is_copr_build, self.package_config.jobs)
        ):
            logger.info(
                "Skipping build for testing. The COPR build is defined in the config."
            )
            return False
        return True


@run_for_comment(command="test")
class TestingFarmHandler(JobHandler):
    """
    This class intentionally does not have a @configured_as decorator as its
    trigger is finished copr build.
    """

    task_name = TaskName.testing_farm

    def __init__(
        self,
        package_config: PackageConfig,
        job_config: JobConfig,
        data: EventData,
        chroot: str,
        build_id: int,
    ):
        super().__init__(
            package_config=package_config,
            job_config=job_config,
            data=data,
        )
        self.chroot = chroot
        self.build_id = build_id
        self._db_trigger: Optional[AbstractTriggerDbType] = None

    @property
    def db_trigger(self) -> Optional[AbstractTriggerDbType]:
        if not self._db_trigger:
            build = CoprBuildModel.get_by_id(self.build_id)
            self._db_trigger = build.job_trigger.get_trigger_object()
        return self._db_trigger

    def run(self) -> TaskResults:
        # TODO: once we turn handlers into respective celery tasks, we should iterate
        #       here over *all* matching jobs and do them all, not just the first one
        testing_farm_helper = TestingFarmJobHelper(
            service_config=self.service_config,
            package_config=self.package_config,
            project=self.project,
            metadata=self.data,
            db_trigger=self.db_trigger,
            job_config=self.job_config,
        )
        logger.info("Running testing farm.")
        return testing_farm_helper.run_testing_farm(chroot=self.chroot)

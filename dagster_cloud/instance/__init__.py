from contextlib import ExitStack
from typing import Any, Dict, List, Optional

import yaml
from dagster import Field, check
from dagster.builtins import Bool
from dagster.config.validate import process_config, resolve_to_config_type
from dagster.core.errors import DagsterInvalidConfigError, DagsterInvariantViolationError
from dagster.core.instance import DagsterInstance
from dagster.core.instance.config import config_field_for_configurable_class
from dagster.core.instance.ref import ConfigurableClassData, InstanceRef, configurable_class_data
from dagster.core.launcher import RunLauncher

from ..auth.constants import get_organization_name_from_agent_token
from ..storage.client import (
    create_cloud_requests_session,
    create_proxy_client,
    dagster_cloud_api_config,
    get_agent_headers,
)
from ..util import get_env_names_from_config


class DagsterCloudInstance(DagsterInstance):
    @property
    def telemetry_enabled(self) -> bool:
        return False


class DagsterCloudAgentInstance(DagsterCloudInstance):
    def __init__(
        self, *args, dagster_cloud_api, user_code_launcher=None, agent_replicas=None, **kwargs
    ):
        super().__init__(*args, **kwargs)

        self._unprocessed_dagster_cloud_api_config = dagster_cloud_api
        self._dagster_cloud_api_config = self._get_processed_config(
            "dagster_cloud_api", dagster_cloud_api, dagster_cloud_api_config()
        )

        self._user_code_launcher_data = (
            configurable_class_data(user_code_launcher) if user_code_launcher else None
        )

        if not self._user_code_launcher_data:
            # This is a user facing error. We should have more actionable advice and link to docs here.
            raise DagsterInvariantViolationError(
                "User code launcher is not configured for DagsterCloudAgentInstance. "
                "Configure a user code launcher under the user_code_launcher: key in your dagster.yaml file."
            )

        self._exit_stack = ExitStack()

        self._user_code_launcher = None
        self._requests_session = None
        self._graphql_client = None

        assert self.dagster_cloud_url

        self._agent_replicas_config = self._get_processed_config(
            "agent_replicas", agent_replicas, self._agent_replicas_config_schema()
        )

    def _get_processed_config(
        self, name: str, config: Optional[Dict[str, Any]], config_type: Dict[str, Any]
    ):
        config_dict = check.opt_dict_param(config, "config", key_type=str)
        processed_config = process_config(config_type, config_dict)
        if not processed_config.success:
            raise DagsterInvalidConfigError(
                f"Errors whilst loading {name} config",
                processed_config.errors,
                config_dict,
            )
        return processed_config.value

    def create_graphql_client(self):
        return create_proxy_client(
            self.dagster_cloud_graphql_url,
            self._dagster_cloud_api_config,
            self.requests_session,
        )

    @property
    def requests_session(self):
        if self._requests_session == None:
            self._requests_session = self._exit_stack.enter_context(
                create_cloud_requests_session(self.dagster_cloud_api_retries)
            )

        return self._requests_session

    @property
    def graphql_client(self):
        if self._graphql_client == None:
            self._graphql_client = self._exit_stack.enter_context(self.create_graphql_client())

        return self._graphql_client

    @property
    def dagster_cloud_url(self):
        if "url" in self._dagster_cloud_api_config:
            return self._dagster_cloud_api_config["url"]

        organization = get_organization_name_from_agent_token(self.dagster_cloud_agent_token)
        if not organization:
            raise DagsterInvariantViolationError(
                "Could not derive Dagster Cloud URL from agent token. Create a new agent token or set the `url` field under `dagster_cloud_api` in your `dagster.yaml`."
            )

        return f"https://{organization}.agent.dagster.cloud"

    @property
    def dagster_cloud_graphql_url(self):
        return f"{self.dagster_cloud_url}/graphql"

    @property
    def dagster_cloud_upload_logs_url(self):
        return f"{self.dagster_cloud_url}/upload_logs"

    @property
    def dagster_cloud_upload_workspace_entry_url(self):
        return f"{self.dagster_cloud_url}/upload_workspace_entry"

    @property
    def dagster_cloud_upload_api_response_url(self):
        return f"{self.dagster_cloud_url}/upload_api_response"

    @property
    def dagster_cloud_api_headers(self):
        return get_agent_headers(self._dagster_cloud_api_config)

    @property
    def dagster_cloud_agent_token(self):
        return self._dagster_cloud_api_config.get("agent_token")

    @property
    def dagster_cloud_api_retries(self) -> int:
        return self._dagster_cloud_api_config["retries"]

    @property
    def dagster_cloud_api_timeout(self) -> int:
        return self._dagster_cloud_api_config["timeout"]

    @property
    def dagster_cloud_api_agent_label(self) -> Optional[str]:
        return self._dagster_cloud_api_config.get("agent_label")

    @property
    def dagster_cloud_api_env_vars(self) -> List[str]:
        return get_env_names_from_config(
            dagster_cloud_api_config(), self._unprocessed_dagster_cloud_api_config
        )

    @property
    def user_code_launcher(self):
        # Lazily load in case the user code launcher requires dependencies (like dagster-k8s)
        # that we don't neccesarily need to load in every context that loads a
        # DagsterCloudAgentInstance (for example, a step worker)
        if not self._user_code_launcher:
            self._user_code_launcher = self._exit_stack.enter_context(
                self._user_code_launcher_data.rehydrate()
            )
            self._user_code_launcher.register_instance(self)
        return self._user_code_launcher

    @property
    def run_launcher(self) -> RunLauncher:
        # Run launcher is determined by the user code launcher
        return self.user_code_launcher.run_launcher()

    @staticmethod
    def get():  # pylint: disable=arguments-differ
        instance = DagsterInstance.get()
        if not isinstance(instance, DagsterCloudAgentInstance):
            raise DagsterInvariantViolationError(
                """
DagsterInstance.get() did not return a DagsterCloudAgentInstance. Make sure that your"
`dagster.yaml` file is correctly configured to include the following:
instance_class:
  module: dagster_cloud.instance
  class: DagsterCloudAgentInstance
"""
            )
        return instance

    @classmethod
    def config_schema(cls):
        return {
            "dagster_cloud_api": Field(dagster_cloud_api_config(), is_required=True),
            "user_code_launcher": config_field_for_configurable_class(),
            "agent_replicas": Field(cls._agent_replicas_config_schema(), is_required=False),
        }

    @classmethod
    def _agent_replicas_config_schema(cls):
        return {"enabled": Field(Bool, is_required=False, default_value=False)}

    def get_required_daemon_types(self):
        return []

    @staticmethod
    def config_defaults(base_dir):
        defaults = InstanceRef.config_defaults(base_dir)

        empty_yaml = yaml.dump({})

        defaults["run_storage"] = ConfigurableClassData(
            "dagster_cloud.storage.runs",
            "GraphQLRunStorage",
            empty_yaml,
        )
        defaults["event_log_storage"] = ConfigurableClassData(
            "dagster_cloud.storage.event_logs",
            "GraphQLEventLogStorage",
            empty_yaml,
        )
        defaults["schedule_storage"] = ConfigurableClassData(
            "dagster_cloud.storage.schedules",
            "GraphQLScheduleStorage",
            empty_yaml,
        )

        defaults["compute_logs"] = ConfigurableClassData(
            "dagster_cloud.storage.compute_logs", "CloudComputeLogManager", empty_yaml
        )

        return defaults

    def dispose(self):
        super().dispose()
        self._exit_stack.close()

    @property
    def should_start_background_run_thread(self):
        return self.agent_replicas_enabled

    @property
    def agent_replicas_enabled(self):
        return self._agent_replicas_config.get("enabled", False)

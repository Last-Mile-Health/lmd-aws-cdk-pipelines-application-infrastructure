"""CDK assertions for AgentAppRunnerStack.

Run: .venv/bin/python -m pytest tests/ -q
"""
import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Match, Template

from lib.agent_apprunner_stack import AgentAppRunnerStack
from lib.configuration import DEV, PROD

ACCOUNTS = {DEV: "002190277880", PROD: "301323023124"}


@pytest.fixture(params=[DEV, PROD])
def env_template(request):
    env = request.param
    app = App()
    stack = AgentAppRunnerStack(
        app, f"{env}-agent-apprunner", target_environment=env,
        env=Environment(account=ACCOUNTS[env], region="us-east-1"),
    )
    return env, Template.from_stack(stack)


def secrets_statements(template):
    statements = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for s in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
            if any(a.startswith("secretsmanager:") for a in actions):
                statements.append(s)
    return statements


def test_instance_role_reads_only_agent_readonly_secret(env_template):
    env, t = env_template
    statements = secrets_statements(t)
    assert len(statements) == 1
    assert statements[0]["Action"] == "secretsmanager:GetSecretValue"
    assert statements[0]["Resource"] == (
        f"arn:aws:secretsmanager:us-east-1:{ACCOUNTS[env]}:secret:"
        f"{env.lower()}/aurora-postgres/agent-readonly-*"
    )


def test_service_env_vars_unchanged(env_template):
    # Live values are set in the console; changing this map in CDK would
    # overwrite them on the next deploy.
    _, t = env_template
    t.has_resource_properties("AWS::AppRunner::Service", {
        "SourceConfiguration": {"ImageRepository": {"ImageConfiguration": {
            "RuntimeEnvironmentVariables": Match.array_with([
                {"Name": "DB_URI", "Value": ""},
                {"Name": "MEM_DB_URI", "Value": ""},
            ]),
        }}},
    })

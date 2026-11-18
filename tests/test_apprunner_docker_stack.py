"""CDK assertions for AppRunnerDockerStack (retired older FastAPI build).

Run: .venv/bin/python -m pytest tests/ -q
"""
import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Match, Template

from lib.apprunner_docker_stack import AppRunnerDockerStack
from lib.configuration import DEV, PROD

ACCOUNTS = {DEV: "002190277880", PROD: "301323023124"}


@pytest.fixture(params=[DEV, PROD])
def template(request):
    env = request.param
    stack = AppRunnerDockerStack(
        App(), f"{env}-apprunner-docker", target_environment=env,
        env=Environment(account=ACCOUNTS[env], region="us-east-1"),
    )
    return Template.from_stack(stack)


def test_build_stage_transition_disabled(template):
    # lmd-portal-services main/prod are the current FastAPI now; this unused
    # pipeline must not build and deploy them (disabled 2026-11-18).
    template.has_resource_properties("AWS::CodePipeline::Pipeline", {
        "DisableInboundStageTransitions": [
            {"StageName": "Build", "Reason": Match.string_like_regexp("Retired")},
        ],
    })

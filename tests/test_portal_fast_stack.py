"""CDK assertions for PortalFastStack (both bootstrap passes).

Run: .venv/bin/python -m pytest tests/ -q
"""
import json

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Match, Template

import lib.portal_fast_stack as portal_fast_module
from lib.configuration import PORTAL_FAST_CREATE_SERVICE, PROD
from lib.portal_fast_stack import PortalFastStack
from lib.tagging import tag

PROD_ENV = Environment(account="301323023124", region="us-east-1")
STANDARD_TAG_KEYS = {
    "lmd-application:cost-center",
    "lmd-application:environment",
    "lmd-application:team",
    "lmd-application:application",
}
SECRET_ENV_NAMES = {
    "DB_HOST", "DB_PORT", "DB_USERNAME", "DB_PASS", "REDSHIFT_PASS",
    "SECRET_KEY", "JWT_SECRET_KEY", "SERVICE_TOKEN", "SLACK_API_TOKEN",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
}


def synth(monkeypatch, create_service: bool) -> Template:
    real = portal_fast_module.get_all_configurations

    def patched():
        mappings = real()
        mappings[PROD][PORTAL_FAST_CREATE_SERVICE] = create_service
        return mappings

    monkeypatch.setattr(portal_fast_module, "get_all_configurations", patched)
    app = App()
    stack = PortalFastStack(app, "Prod-portal-fast", target_environment=PROD, env=PROD_ENV)
    tag(stack, PROD)
    return Template.from_stack(stack)


@pytest.fixture(params=[False, True], ids=["pass1", "pass2"])
def template(request, monkeypatch):
    return synth(monkeypatch, request.param)


@pytest.fixture
def pass2(monkeypatch):
    return synth(monkeypatch, True)


def test_pass1_has_no_service(monkeypatch):
    t = synth(monkeypatch, False)
    t.resource_count_is("AWS::AppRunner::Service", 0)
    t.resource_count_is("AWS::ECR::Repository", 1)


def test_no_iam_users_or_access_keys(template):
    template.resource_count_is("AWS::IAM::User", 0)
    template.resource_count_is("AWS::IAM::AccessKey", 0)


def test_every_taggable_resource_has_standard_tags(template):
    for logical_id, res in template.to_json()["Resources"].items():
        tags = res.get("Properties", {}).get("Tags")
        if tags is None:
            continue
        keys = {t["Key"] for t in tags} if isinstance(tags, list) else set(tags)
        assert STANDARD_TAG_KEYS <= keys, f"{logical_id} missing tags: {STANDARD_TAG_KEYS - keys}"


def test_stateful_resources_are_retained(template):
    for rtype in ("AWS::ECR::Repository", "AWS::S3::Bucket"):
        for logical_id, res in template.find_resources(rtype).items():
            assert res.get("DeletionPolicy") == "Retain", logical_id


def test_reports_bucket_blocks_public_access_and_scopes_cors(template):
    template.has_resource_properties("AWS::S3::Bucket", {
        "PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True, "BlockPublicPolicy": True,
            "IgnorePublicAcls": True, "RestrictPublicBuckets": True,
        },
        "CorsConfiguration": {"CorsRules": [Match.object_like({
            "AllowedOrigins": ["https://lastmilehealthdata.org", "https://www.lastmilehealthdata.org"],
        })]},
    })


def test_github_role_trusts_only_the_prod_environment(template):
    template.has_resource_properties("AWS::IAM::Role", {
        "RoleName": "prod-lmd-portal-fast-github-deploy",
        "AssumeRolePolicyDocument": {"Statement": [Match.object_like({
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {"StringEquals": {
                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                "token.actions.githubusercontent.com:sub":
                    "repo:Last-Mile-Health/lmd-portal-services:environment:prod",
            }},
        })]},
    })


def test_instance_role_has_no_wildcard_resources(template):
    roles = template.find_resources("AWS::IAM::Role", {
        "Properties": {"AssumeRolePolicyDocument": {"Statement": [Match.object_like({
            "Principal": {"Service": "tasks.apprunner.amazonaws.com"}})]}}})
    (role_id,) = roles
    for res in template.find_resources("AWS::IAM::Policy").values():
        if {"Ref": role_id} not in res["Properties"]["Roles"]:
            continue
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
            resources = stmt["Resource"] if isinstance(stmt["Resource"], list) else [stmt["Resource"]]
            assert "*" not in resources, stmt


def test_service_settings(pass2):
    pass2.has_resource_properties("AWS::AppRunner::Service", {
        "ServiceName": "prod-lmd-portal-fast",
        "HealthCheckConfiguration": Match.object_like({"Protocol": "HTTP", "Path": "/api/health"}),
        "SourceConfiguration": Match.object_like({
            "AutoDeploymentsEnabled": True,
            "ImageRepository": Match.object_like({"ImageRepositoryType": "ECR"}),
        }),
    })
    autoscaling = pass2.find_resources("AWS::AppRunner::AutoScalingConfiguration", {
        "Properties": {"MaxSize": 1, "MinSize": 1}})
    (autoscaling_id,) = autoscaling
    pass2.has_resource_properties("AWS::AppRunner::Service", {
        "AutoScalingConfigurationArn": {"Fn::GetAtt": [autoscaling_id, "AutoScalingConfigurationArn"]},
    })


def test_secrets_only_via_environment_secrets(pass2):
    (service,) = pass2.find_resources("AWS::AppRunner::Service").values()
    image_cfg = service["Properties"]["SourceConfiguration"]["ImageRepository"]["ImageConfiguration"]
    plain = {v["Name"]: v["Value"] for v in image_cfg["RuntimeEnvironmentVariables"]}
    secret_names = {v["Name"] for v in image_cfg["RuntimeEnvironmentSecrets"]}

    assert not SECRET_ENV_NAMES & set(plain), SECRET_ENV_NAMES & set(plain)
    assert secret_names == SECRET_ENV_NAMES - {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
    assert plain["NOTIFICATIONS_JOB_ENABLED"] == "false"
    assert plain["RUN_MIGRATIONS_ON_STARTUP"] == "false"
    assert plain["WEB_CONCURRENCY"] == "1"
    assert json.loads(plain["CORS_ORIGINS"]) == [
        "https://lastmilehealthdata.org", "https://www.lastmilehealthdata.org"]

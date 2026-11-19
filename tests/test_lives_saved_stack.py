"""CDK assertions for LivesSavedStack (Dev and Prod).

Run: .venv/bin/python -m pytest tests/ -q
"""
import json

import pytest
from aws_cdk import App, Environment
from aws_cdk.assertions import Match, Template

from lib.configuration import DEV, PROD
from lib.lives_saved_stack import LivesSavedStack
from lib.tagging import tag

ENVS = {
    DEV: Environment(account="002190277880", region="us-east-1"),
    PROD: Environment(account="301323023124", region="us-east-1"),
}
STANDARD_TAG_KEYS = {
    "lmd-application:cost-center",
    "lmd-application:environment",
    "lmd-application:team",
    "lmd-application:application",
}


def synth(target_environment: str) -> Template:
    app = App()
    stack = LivesSavedStack(
        app, f"{target_environment}-lives-saved",
        target_environment=target_environment, env=ENVS[target_environment])
    tag(stack, target_environment)
    return Template.from_stack(stack)


@pytest.fixture(scope="module", params=[DEV, PROD])
def template(request):
    return synth(request.param)


@pytest.fixture(scope="module")
def dev():
    return synth(DEV)


@pytest.fixture(scope="module")
def prod():
    return synth(PROD)


def test_every_taggable_resource_has_standard_tags(template):
    for logical_id, res in template.to_json()["Resources"].items():
        tags = res.get("Properties", {}).get("Tags")
        if tags is None:
            continue
        keys = {t["Key"] for t in tags} if isinstance(tags, list) else set(tags)
        assert STANDARD_TAG_KEYS <= keys, f"{logical_id} missing tags: {STANDARD_TAG_KEYS - keys}"


def test_volumes_get_tags(template):
    template.has_resource_properties("AWS::EC2::Instance", {
        "PropagateTagsToVolumeOnCreation": True,
        "BlockDeviceMappings": [Match.object_like({
            "Ebs": Match.object_like({"Encrypted": True, "VolumeType": "gp3"}),
        })],
    })


def test_instance_shape(template):
    template.resource_count_is("AWS::EC2::Instance", 1)
    template.has_resource_properties("AWS::EC2::Instance", {
        "InstanceType": "t3.small",
        "ImageId": "ami-06f98347e4f9c7328",
    })
    # IMDSv2 only.
    template.has_resource_properties("AWS::EC2::LaunchTemplate", {
        "LaunchTemplateData": {"MetadataOptions": {"HttpTokens": "required"}},
    })


def test_only_http_and_https_in(template):
    ingress = []
    for res in template.find_resources("AWS::EC2::SecurityGroup").values():
        ingress += res["Properties"].get("SecurityGroupIngress", [])
    assert sorted((r["FromPort"], r["ToPort"], r["CidrIp"]) for r in ingress) == [
        (80, 80, "0.0.0.0/0"), (443, 443, "0.0.0.0/0")]


def test_no_iam_users_or_access_keys(template):
    template.resource_count_is("AWS::IAM::User", 0)
    template.resource_count_is("AWS::IAM::AccessKey", 0)


def test_repositories_retained(template):
    repos = template.find_resources("AWS::ECR::Repository")
    assert len(repos) == 2
    for logical_id, res in repos.items():
        assert res.get("DeletionPolicy") == "Retain", logical_id


def test_instance_reads_only_its_own_db_secret(dev):
    policies = json.dumps(dev.find_resources("AWS::IAM::Policy"))
    assert "dev/aurora-postgres/lives-saved-tool-??????" in policies
    assert "aurora-postgres/credentials" not in policies


def test_deploy_role_trusts_only_its_github_environment(dev, prod):
    for template, environment in ((dev, "dev"), (prod, "prod")):
        template.has_resource_properties("AWS::IAM::Role", {
            "RoleName": f"{environment}-lmh-lives-saved-github-deploy",
            "AssumeRolePolicyDocument": {"Statement": [Match.object_like({
                "Condition": {"StringEquals": {
                    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                    "token.actions.githubusercontent.com:sub":
                        "repo:Last-Mile-Health@9609034/lmh-lives-saved-tool@1463055911"
                        f":environment:{environment}",
                }},
            })]},
        })


def test_oidc_provider_created_in_dev_reused_in_prod(dev, prod):
    dev.resource_count_is("AWS::IAM::OIDCProvider", 1)
    prod.resource_count_is("AWS::IAM::OIDCProvider", 0)


def test_dns_record_only_where_zone_is_in_account(dev, prod):
    dev.resource_count_is("AWS::Route53::RecordSet", 0)
    prod.has_resource_properties("AWS::Route53::RecordSet", {
        "Name": "livessaved.lastmilehealthdata.org.",
        "Type": "A",
        "HostedZoneId": "Z06020672MB24JCV66DHV",
    })

from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_logs as logs,
    aws_route53 as route53,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

from .configuration import (
    DEPLOYMENT, GITHUB_OIDC_PROVIDER_ARN, GITHUB_REPOSITORY_OWNER_NAME,
    LIVES_SAVED_AMI_ID, LIVES_SAVED_DOMAIN, LIVES_SAVED_GITHUB_ENVIRONMENT,
    LIVES_SAVED_GITHUB_REPOSITORY_NAME, LIVES_SAVED_HOSTED_ZONE_ID,
    LIVES_SAVED_HOSTED_ZONE_NAME, LIVES_SAVED_INSTANCE_TYPE, PUBLIC_SUBNET_IDS, VPC_ID,
    get_all_configurations,
)

GITHUB_OIDC_URL = "https://token.actions.githubusercontent.com"
GITHUB_OIDC_THUMBPRINTS = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
]
COMPOSE_VERSION = "v2.32.4"
APP_DIR = "/opt/lmh-lives-saved"


class LivesSavedStack(Stack):
    """
    lmh-lives-saved-tool (React web + FastAPI api) on one EC2 instance.

        https://<domain> -> Elastic IP -> EC2 (public subnet, ports 80/443 only)
            caddy (TLS via Let's Encrypt) -> web (nginx, SPA, proxies /api)
            -> api (FastAPI) -> Aurora: database lmd_lives_saved_tool

    Deployment: push to main (Dev) / prod (Prod) -> GitHub Action assumes the
    deploy role below through OIDC -> builds and pushes both images to ECR
    -> sends an SSM Run Command to this instance, which runs deploy/deploy.sh
    from the app repo (docker compose pull && up). No SSH, no static keys:
    admin access is SSM Session Manager.

    The instance is cattle: the app, its compose file and .env arrive on every
    deploy; data lives in Aurora. Its own DB login and database are created by
    scripts/create-lives-saved-db.sh (secret <env>/aurora-postgres/lives-saved-tool).
    """

    def __init__(self, scope: Construct, construct_id: str, target_environment: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        mappings = get_all_configurations()
        config = mappings[target_environment]
        env = target_environment.lower()
        name = f"{env}-lmh-lives-saved"
        domain = config[LIVES_SAVED_DOMAIN]
        github_environment = config[LIVES_SAVED_GITHUB_ENVIRONMENT]
        github_repo = (
            f"{mappings[DEPLOYMENT][GITHUB_REPOSITORY_OWNER_NAME]}/"
            f"{mappings[DEPLOYMENT][LIVES_SAVED_GITHUB_REPOSITORY_NAME]}"
        )

        # Attributes, not a lookup: the pipeline's synth step has no lookup role.
        public_subnet_ids = config[PUBLIC_SUBNET_IDS]
        vpc = ec2.Vpc.from_vpc_attributes(
            self, "Vpc",
            vpc_id=config[VPC_ID],
            availability_zones=[f"{self.region}{az}" for az in "abc"][:len(public_subnet_ids)],
            public_subnet_ids=public_subnet_ids,
        )

        # 1. ECR repositories (api, web). Tags move on every deploy; each image
        # is also tagged <branch>-<sha> for rollback.
        repositories = {}
        for component in ("api", "web"):
            repositories[component] = ecr.Repository(
                self, f"{component.capitalize()}Repository",
                repository_name=f"{name}-{component}",
                image_scan_on_push=True,
                image_tag_mutability=ecr.TagMutability.MUTABLE,
                removal_policy=RemovalPolicy.RETAIN,
                lifecycle_rules=[
                    ecr.LifecycleRule(
                        rule_priority=1,
                        description="Expire untagged images after 14 days",
                        tag_status=ecr.TagStatus.UNTAGGED,
                        max_image_age=Duration.days(14),
                    ),
                    ecr.LifecycleRule(
                        rule_priority=2,
                        description="Keep the last 30 commit-tagged images",
                        tag_status=ecr.TagStatus.TAGGED,
                        tag_prefix_list=["main-", "prod-"],
                        max_image_count=30,
                    ),
                ],
            )

        # 2. Container logs (docker awslogs driver, one stream per service).
        log_group = logs.LogGroup(
            self, "LogGroup",
            log_group_name=f"/lmh-lives-saved/{env}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # The app's own DB login (owns only lmd_lives_saved_tool), created out
        # of band; referenced by name so this stack deploys before or after it.
        db_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "DbSecret", f"{env}/aurora-postgres/lives-saved-tool")

        # 3. Instance role: pull its images, read its DB secret, write its
        # logs, and be reachable by SSM (Run Command + Session Manager).
        instance_role = iam.Role(
            self, "InstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
            ],
        )
        for repository in repositories.values():
            repository.grant_pull(instance_role)
        db_secret.grant_read(instance_role)
        log_group.grant_write(instance_role)

        # 4. Network: public subnet, only HTTP (Let's Encrypt challenge and
        # redirect) and HTTPS in. No SSH.
        security_group = ec2.SecurityGroup(
            self, "SecurityGroup",
            vpc=vpc,
            description=f"{name}: HTTP/HTTPS in (Caddy), all out",
            allow_all_outbound=True,
        )
        security_group.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "HTTP (ACME + redirect)")
        security_group.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS")

        # 5. Instance. Docker + compose only; the app arrives with each deploy.
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "set -euxo pipefail",
            "dnf install -y docker jq",
            "systemctl enable --now docker",
            "mkdir -p /usr/local/lib/docker/cli-plugins",
            "curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose "
            f"https://github.com/docker/compose/releases/download/{COMPOSE_VERSION}/docker-compose-linux-x86_64",
            "chmod +x /usr/local/lib/docker/cli-plugins/docker-compose",
            f"mkdir -p {APP_DIR}",
        )

        instance = ec2.Instance(
            self, "Instance",
            instance_name=name,
            instance_type=ec2.InstanceType(config[LIVES_SAVED_INSTANCE_TYPE]),
            machine_image=ec2.MachineImage.generic_linux({self.region: config[LIVES_SAVED_AMI_ID]}),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnets=[vpc.public_subnets[0]]),
            security_group=security_group,
            role=instance_role,
            user_data=user_data,
            require_imdsv2=True,
            propagate_tags_to_volume_on_creation=True,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(
                        30, volume_type=ec2.EbsDeviceVolumeType.GP3, encrypted=True),
                )
            ],
        )

        # Stable public IP: DNS points here, and it survives instance replacement.
        eip = ec2.CfnEIP(self, "ElasticIp", domain="vpc")
        ec2.CfnEIPAssociation(
            self, "ElasticIpAssociation",
            allocation_id=eip.attr_allocation_id,
            instance_id=instance.instance_id,
        )

        if config[LIVES_SAVED_HOSTED_ZONE_ID]:
            zone = route53.HostedZone.from_hosted_zone_attributes(
                self, "HostedZone",
                hosted_zone_id=config[LIVES_SAVED_HOSTED_ZONE_ID],
                zone_name=config[LIVES_SAVED_HOSTED_ZONE_NAME],
            )
            route53.ARecord(
                self, "DnsRecord",
                zone=zone,
                record_name=domain,
                target=route53.RecordTarget.from_ip_addresses(eip.ref),
                ttl=Duration.minutes(5),
            )

        # 6. GitHub deploy role: only the repo's <environment> GitHub
        # environment can assume it; it can push the two images and send
        # commands to this instance only.
        provider_arn = config[GITHUB_OIDC_PROVIDER_ARN]
        if not provider_arn:
            provider_arn = iam.CfnOIDCProvider(
                self, "GitHubOidcProvider",
                url=GITHUB_OIDC_URL,
                client_id_list=["sts.amazonaws.com"],
                thumbprint_list=GITHUB_OIDC_THUMBPRINTS,
            ).attr_arn
        deploy_role = iam.Role(
            self, "GitHubDeployRole",
            role_name=f"{name}-github-deploy",
            description=f"GitHub Actions ({github_repo}, environment {github_environment}) deploy to {name}",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(
                provider_arn,
                conditions={
                    "StringEquals": {
                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                        "token.actions.githubusercontent.com:sub":
                            f"repo:{github_repo}:environment:{github_environment}",
                    }
                },
            ),
        )
        for repository in repositories.values():
            repository.grant_pull_push(deploy_role)
        deploy_role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:SendCommand"],
            resources=[
                f"arn:aws:ec2:{self.region}:{self.account}:instance/{instance.instance_id}",
                f"arn:aws:ssm:{self.region}::document/AWS-RunShellScript",
            ],
        ))
        # Read-only calls with no resource-level scoping.
        deploy_role.add_to_policy(iam.PolicyStatement(
            actions=["ssm:GetCommandInvocation", "ec2:DescribeInstances"],
            resources=["*"],
        ))

        CfnOutput(self, "Url", value=f"https://{domain}")
        CfnOutput(self, "PublicIp", value=eip.ref)
        CfnOutput(self, "InstanceId", value=instance.instance_id)
        CfnOutput(self, "GitHubDeployRoleArn", value=deploy_role.role_arn)
        for component, repository in repositories.items():
            CfnOutput(self, f"{component.capitalize()}RepositoryUri", value=repository.repository_uri)

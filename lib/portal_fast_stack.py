import json

from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    RemovalPolicy,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_s3 as s3,
    aws_dynamodb as dynamodb,
    aws_secretsmanager as secretsmanager,
    aws_apprunner as apprunner_l1,
)
import aws_cdk.aws_apprunner_alpha as apprunner
from constructs import Construct

from .configuration import (
    AURORA_SECRET_ARN, PORTAL_FAST_CORS_ORIGINS, PORTAL_FAST_CREATE_SERVICE,
    PORTAL_FAST_EVENTS_TABLE, PORTAL_FAST_GITHUB_ENVIRONMENT, PORTAL_FAST_IMAGE_TAG,
    PORTAL_FAST_UPLOAD_BUCKET, REDSHIFT_HOST, REDSHIFT_PASSWORD_SECRET_ARN, REDSHIFT_USER,
    SERVICE_GITHUB_REPOSITORY_NAME, DEPLOYMENT, GITHUB_REPOSITORY_OWNER_NAME,
    get_all_configurations,
)

GITHUB_OIDC_URL = "https://token.actions.githubusercontent.com"
# GitHub's published thumbprints. IAM validates GitHub tokens against its own
# trusted CA list, but CloudFormation still accepts/expects a thumbprint list.
GITHUB_OIDC_THUMBPRINTS = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
]
BEDROCK_PROFILE_ID = "us.anthropic.claude-sonnet-4-6"
# Regions the us. cross-region inference profile routes to (checked in Prod
# 2026-10-20 with `aws bedrock get-inference-profile`).
BEDROCK_PROFILE_REGIONS = ["us-east-1", "us-east-2", "us-west-2"]


class PortalFastStack(Stack):
    """
    lmd-portal-services FastAPI service (`modeling` line) on App Runner.

    Runs alongside the legacy API (Prod-apprunner, branch legacy_prod), which
    this stack does not touch. Deployment model:

        push to <image tag branch> -> GitHub Action (OIDC role below) builds
        the Dockerfile -> ECR repo below :<image tag> -> App Runner
        auto-deploys.

    No IAM users or static keys: GitHub uses the OIDC deploy role, the
    running app uses the instance role. Secrets reach the app only through
    App Runner `environment_secrets`.

    Two-pass bootstrap (PORTAL_FAST_CREATE_SERVICE): pass 1 creates
    everything except the service; after the first image is pushed, pass 2
    creates the service.
    """

    def __init__(self, scope: Construct, construct_id: str, target_environment: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.mappings = get_all_configurations()
        config = self.mappings[target_environment]
        env = target_environment.lower()
        name = f"{env}-lmd-portal-fast"
        image_tag = config[PORTAL_FAST_IMAGE_TAG]
        cors_origins = config[PORTAL_FAST_CORS_ORIGINS]
        github_repo = (
            f"{self.mappings[DEPLOYMENT][GITHUB_REPOSITORY_OWNER_NAME]}/"
            f"{self.mappings[DEPLOYMENT][SERVICE_GITHUB_REPOSITORY_NAME]}"
        )

        # 1. ECR repository. The tag is moved on every push, so tags stay
        # mutable; previous images become untagged and are kept 30 days for
        # rollback (re-point the tag at an old digest).
        repository = ecr.Repository(
            self, "PortalFastRepository",
            repository_name=f"{env}-lmd-portal-services",
            image_scan_on_push=True,
            image_tag_mutability=ecr.TagMutability.MUTABLE,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                ecr.LifecycleRule(
                    description="Expire untagged images after 30 days",
                    tag_status=ecr.TagStatus.UNTAGGED,
                    max_image_age=Duration.days(30),
                )
            ],
        )

        # 2. GitHub OIDC provider (one per account; none existed in Prod on
        # 2026-10-20) and the deploy role the GitHub Action assumes. Only the
        # repo's `<environment>` GitHub environment can assume it.
        oidc_provider = iam.CfnOIDCProvider(
            self, "GitHubOidcProvider",
            url=GITHUB_OIDC_URL,
            client_id_list=["sts.amazonaws.com"],
            thumbprint_list=GITHUB_OIDC_THUMBPRINTS,
        )
        deploy_role = iam.Role(
            self, "GitHubDeployRole",
            role_name=f"{name}-github-deploy",
            description=f"GitHub Actions ({github_repo}, environment "
                        f"{config[PORTAL_FAST_GITHUB_ENVIRONMENT]}) push to {repository.repository_name}",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(
                oidc_provider.attr_arn,
                conditions={
                    "StringEquals": {
                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                        "token.actions.githubusercontent.com:sub":
                            f"repo:{github_repo}:environment:{config[PORTAL_FAST_GITHUB_ENVIRONMENT]}",
                    }
                },
            ),
        )
        repository.grant_pull_push(deploy_role)

        # 3. Secrets. Values the stack can't know are created as placeholders
        # and set once by CLI (`aws secretsmanager put-secret-value`); CDK
        # doesn't overwrite them afterwards.
        aurora_secret = secretsmanager.Secret.from_secret_complete_arn(
            self, "AuroraSecret", config[AURORA_SECRET_ARN])
        redshift_secret = secretsmanager.Secret.from_secret_complete_arn(
            self, "RedshiftPasswordSecret", config[REDSHIFT_PASSWORD_SECRET_ARN])

        def generated_secret(construct_id: str, secret_name: str, description: str) -> secretsmanager.Secret:
            return secretsmanager.Secret(
                self, construct_id,
                secret_name=f"{env}/lmd-portal-fast/{secret_name}",
                description=description,
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    password_length=48, exclude_punctuation=True),
            )

        secret_key = generated_secret("SecretKey", "secret-key", "App SECRET_KEY (generated)")
        jwt_secret_key = generated_secret("JwtSecretKey", "jwt-secret-key", "App JWT_SECRET_KEY (generated)")
        service_token = generated_secret(
            "ServiceToken", "service-token",
            "X-Service-Token the portal must send on OKR/portal-config write routes (generated)")
        slack_token = generated_secret(
            "SlackApiToken", "slack-api-token",
            "PLACEHOLDER -- set the Slack bot token with put-secret-value")
        slack_token.apply_removal_policy(RemovalPolicy.RETAIN)

        # 4. OKR reports bucket (browser uploads via presigned PUT, tagged
        # upload-status=pending until the API confirms them).
        reports_bucket = s3.Bucket(
            self, "OkrReportsBucket",
            bucket_name=f"{env}-lmd-portal-okr-reports-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.PUT, s3.HttpMethods.GET],
                    allowed_origins=cors_origins,
                    allowed_headers=["*"],
                    max_age=3000,
                )
            ],
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-unconfirmed-uploads",
                    tag_filters={"upload-status": "pending"},
                    expiration=Duration.days(1),
                ),
                s3.LifecycleRule(
                    id="expire-old-versions",
                    noncurrent_version_expiration=Duration.days(30),
                ),
            ],
        )

        upload_bucket = s3.Bucket.from_bucket_name(
            self, "UploadBucket", config[PORTAL_FAST_UPLOAD_BUCKET])
        events_table = dynamodb.Table.from_table_name(
            self, "PortalEventsTable", config[PORTAL_FAST_EVENTS_TABLE])

        # 5. IAM roles.
        access_role = iam.Role(
            self, "AccessRole",
            assumed_by=iam.ServicePrincipal("build.apprunner.amazonaws.com"),
        )
        repository.grant_pull(access_role)

        # Instance role: exactly the calls the app makes (api/okr_s3.py,
        # api/routers/program_data.py, api/routers/portal_events.py,
        # api/resources/utils.py) plus reading its own secrets.
        instance_role = iam.Role(
            self, "InstanceRole",
            assumed_by=iam.ServicePrincipal("tasks.apprunner.amazonaws.com"),
        )
        instance_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:PutObject", "s3:GetObject", "s3:PutObjectTagging", "s3:DeleteObject"],
            resources=[reports_bucket.arn_for_objects("*")],
        ))
        instance_role.add_to_policy(iam.PolicyStatement(
            actions=["s3:PutObject"],
            resources=[upload_bucket.arn_for_objects("*")],
        ))
        instance_role.add_to_policy(iam.PolicyStatement(
            actions=["dynamodb:Scan", "dynamodb:PutItem", "dynamodb:BatchWriteItem"],
            resources=[events_table.table_arn],
        ))
        instance_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{BEDROCK_PROFILE_ID}",
                *[
                    f"arn:aws:bedrock:{region}::foundation-model/{BEDROCK_PROFILE_ID.split('.', 1)[1]}"
                    for region in BEDROCK_PROFILE_REGIONS
                ],
            ],
        ))
        for secret in (aurora_secret, redshift_secret, secret_key, jwt_secret_key, service_token, slack_token):
            secret.grant_read(instance_role)

        CfnOutput(self, "RepositoryUri", value=repository.repository_uri)
        CfnOutput(self, "GitHubDeployRoleArn", value=deploy_role.role_arn)
        CfnOutput(self, "OkrReportsBucketName", value=reports_bucket.bucket_name)

        if not config[PORTAL_FAST_CREATE_SERVICE]:
            return

        # 6. App Runner service (pass 2). One instance only: the in-process
        # APScheduler must run once (dev-docs in lmd-portal-services,
        # 2026-10-20 notifications job flag); raise only after the scheduler
        # is single-leader.
        # L1: the alpha L2 rejects min_size == max_size, which App Runner allows.
        autoscaling_cfn = apprunner_l1.CfnAutoScalingConfiguration(
            self, "AutoScaling",
            auto_scaling_configuration_name=name,
            min_size=1,
            max_size=1,
            max_concurrency=100,
        )
        observability = apprunner.ObservabilityConfiguration(
            self, "Observability",
            observability_configuration_name=name,
            trace_configuration_vendor=apprunner.TraceConfigurationVendor.AWSXRAY,
        )

        service = apprunner.Service(
            self, "Service",
            service_name=name,
            source=apprunner.Source.from_ecr(
                repository=repository,
                tag_or_digest=image_tag,
                image_configuration=apprunner.ImageConfiguration(
                    port=5005,
                    environment_variables={
                        "ENVIRONMENT": target_environment,
                        "AWS_REGION": self.region,
                        "DB_ENGINE": "postgresql+psycopg",
                        "DB_NAME": "lmd_portal",
                        "REDSHIFT_HOST": config[REDSHIFT_HOST],
                        "REDSHIFT_PORT": "5439",
                        "REDSHIFT_USER": config[REDSHIFT_USER],
                        "REDSHIFT_SSLMODE": "require",
                        "S3_UPLOAD_BUCKET": upload_bucket.bucket_name,
                        "OKR_REPORTS_BUCKET": reports_bucket.bucket_name,
                        "PORTAL_EVENTS_TABLE_NAME": events_table.table_name,
                        "NOTIFICATIONS_TABLE_NAME": "notifications",
                        "CORS_ORIGINS": json.dumps(cors_origins),
                        # Migrations run as a deliberate release step, not at boot.
                        "RUN_MIGRATIONS_ON_STARTUP": "false",
                        "SCHEDULER_ENABLED": "true",
                        # The legacy API sends the Slack digest from the same
                        # table; only one sender while both run.
                        "NOTIFICATIONS_JOB_ENABLED": "false",
                        # One gunicorn worker -> one scheduler.
                        "WEB_CONCURRENCY": "1",
                    },
                    environment_secrets={
                        "DB_HOST": apprunner.Secret.from_secrets_manager(aurora_secret, "host"),
                        "DB_PORT": apprunner.Secret.from_secrets_manager(aurora_secret, "port"),
                        "DB_USERNAME": apprunner.Secret.from_secrets_manager(aurora_secret, "username"),
                        "DB_PASS": apprunner.Secret.from_secrets_manager(aurora_secret, "password"),
                        "REDSHIFT_PASS": apprunner.Secret.from_secrets_manager(redshift_secret),
                        "SECRET_KEY": apprunner.Secret.from_secrets_manager(secret_key),
                        "JWT_SECRET_KEY": apprunner.Secret.from_secrets_manager(jwt_secret_key),
                        "SERVICE_TOKEN": apprunner.Secret.from_secrets_manager(service_token),
                        "SLACK_API_TOKEN": apprunner.Secret.from_secrets_manager(slack_token),
                    },
                ),
            ),
            access_role=access_role,
            instance_role=instance_role,
            cpu=apprunner.Cpu.ONE_VCPU,
            memory=apprunner.Memory.THREE_GB,
            health_check=apprunner.HealthCheck.http(
                path="/api/health",
                interval=Duration.seconds(10),
                timeout=Duration.seconds(5),
                healthy_threshold=1,
                unhealthy_threshold=5,
            ),
            observability_configuration=observability,
            auto_deployments_enabled=True,
        )
        service.node.default_child.add_property_override(
            "AutoScalingConfigurationArn", autoscaling_cfn.attr_auto_scaling_configuration_arn)

        CfnOutput(self, "ServiceUrl", value=f"https://{service.service_url}")

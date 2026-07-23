from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_codebuild as codebuild,
    aws_codepipeline as codepipeline,
    aws_codepipeline_actions as codepipeline_actions,
)
import aws_cdk.aws_apprunner_alpha as apprunner
from constructs import Construct

from .configuration import (
    APPRUNNER_DOCKER_GITHUB_REPOSITORY_NAME, CODESTAR_CONNECTION_ARN, DEPLOYMENT,
    GITHUB_REPOSITORY_OWNER_NAME, get_all_configurations, get_resource_name_prefix
)


class AppRunnerDockerStack(Stack):
    """
    App Runner service deployed from a Docker image, connected to GitHub for
    CI/CD: a CodePipeline pulls source from GitHub, builds a Docker image with
    CodeBuild, and pushes it to ECR. App Runner watches that ECR repository
    (`auto_deployments_enabled=True`) and automatically rolls out a new
    deployment whenever a fresh image lands on the tracked tag -- no explicit
    "deploy" pipeline stage is required.

    This replaces the previous ECS Fargate + ALB ("ECS Express Mode") stack
    with a simpler, fully managed App Runner service that still starts from
    ECR and is connected to GitHub.
    """

    def __init__(self, scope: Construct, construct_id: str, target_environment: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.mappings = get_all_configurations()
        resource_prefix = get_resource_name_prefix()
        # Dev tracks the actively-developed "modeling" branch; Test and Prod
        # track their own environment-named branches (e.g. "test", "prod").
        branch = "modeling" if target_environment.lower() == "dev" else target_environment.lower()
        service_name = f'{target_environment.lower()}-{resource_prefix}-apprunner-docker'

        # 1. ECR repository -- the single source of truth for container images.
        repository = ecr.Repository(
            self, "AppRunnerDockerRepository",
            repository_name=service_name,
            image_scan_on_push=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                ecr.LifecycleRule(
                    description="Expire untagged images after 14 days",
                    tag_status=ecr.TagStatus.UNTAGGED,
                    max_image_age=Duration.days(14),
                ),
                ecr.LifecycleRule(
                    description="Keep only the last 20 tagged images",
                    tag_status=ecr.TagStatus.ANY,
                    max_image_count=20,
                ),
            ],
        )

        # 2. CI/CD: GitHub -> CodeBuild (docker build & push to ECR).
        repo_owner = self.mappings[DEPLOYMENT][GITHUB_REPOSITORY_OWNER_NAME]
        repo_name = self.mappings[DEPLOYMENT][APPRUNNER_DOCKER_GITHUB_REPOSITORY_NAME]

        source_artifact = codepipeline.Artifact("SourceArtifact")

        build_project = codebuild.PipelineProject(
            self, "AppRunnerDockerBuildProject",
            project_name=f'{service_name}-build',
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                privileged=True,  # required to build/push Docker images
            ),
            environment_variables={
                "ECR_REPOSITORY_URI": codebuild.BuildEnvironmentVariable(value=repository.repository_uri),
                "AWS_ACCOUNT_ID": codebuild.BuildEnvironmentVariable(value=self.account),
                "AWS_DEFAULT_REGION": codebuild.BuildEnvironmentVariable(value=self.region),
            },
            build_spec=codebuild.BuildSpec.from_object({
                "version": "0.2",
                "phases": {
                    "pre_build": {
                        "commands": [
                            "echo Logging in to Amazon ECR...",
                            "aws ecr get-login-password --region $AWS_DEFAULT_REGION | "
                            "docker login --username AWS --password-stdin "
                            "$AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com",
                            "COMMIT_HASH=$(echo $CODEBUILD_RESOLVED_SOURCE_VERSION | cut -c 1-7)",
                            "IMAGE_TAG=${COMMIT_HASH:-latest}",
                        ]
                    },
                    "build": {
                        "commands": [
                            "echo Build started on `date`",
                            "echo Building the Docker image...",
                            "docker build -t $ECR_REPOSITORY_URI:latest -t $ECR_REPOSITORY_URI:$IMAGE_TAG .",
                        ]
                    },
                    "post_build": {
                        "commands": [
                            "echo Build completed on `date`",
                            "echo Pushing the Docker images...",
                            "docker push $ECR_REPOSITORY_URI:latest",
                            "docker push $ECR_REPOSITORY_URI:$IMAGE_TAG",
                            # App Runner's auto_deployments_enabled watches the
                            # ":latest" tag and rolls out a new deployment as
                            # soon as this push completes -- no deploy stage needed.
                            "echo Image pushed. App Runner will auto-deploy the new :latest image.",
                        ]
                    },
                },
            }),
        )

        repository.grant_pull_push(build_project)

        pipeline = codepipeline.Pipeline(
            self, "AppRunnerDockerPipeline",
            pipeline_name=f'{service_name}-pipeline',
            cross_account_keys=True,
        )

        pipeline.add_stage(
            stage_name="Source",
            actions=[
                codepipeline_actions.CodeStarConnectionsSourceAction(
                    action_name="GitHub_Source",
                    owner=repo_owner,
                    repo=repo_name,
                    branch=branch,
                    connection_arn=self.mappings[target_environment][CODESTAR_CONNECTION_ARN],
                    output=source_artifact,
                )
            ],
        )

        pipeline.add_stage(
            stage_name="Build",
            actions=[
                codepipeline_actions.CodeBuildAction(
                    action_name="DockerBuildAndPush",
                    project=build_project,
                    input=source_artifact,
                )
            ],
        )

        # 3. IAM roles.
        # AccessRole: assumed by App Runner's build service to pull the
        # private image from ECR when starting a new deployment.
        access_role = iam.Role(
            self, "AppRunnerAccessRole",
            assumed_by=iam.ServicePrincipal("build.apprunner.amazonaws.com"),
        )
        repository.grant_pull(access_role)

        # InstanceRole: assumed by the running application for AWS API calls
        # it makes at runtime (Secrets Manager, DynamoDB tables, S3, etc.).
        instance_role = iam.Role(
            self, "AppRunnerInstanceRole",
            assumed_by=iam.ServicePrincipal("tasks.apprunner.amazonaws.com"),
        )
        instance_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "secretsmanager:ListSecrets",
                    "secretsmanager:GetSecretValue",
                ],
                # NOTE: consider scoping this to specific secret ARNs once the
                # real secrets for this service are known.
                resources=["*"],
            )
        )

        # 4. App Runner service -- deploys the ":latest" image from ECR and
        # automatically redeploys whenever the pipeline pushes a new one.
        service = apprunner.Service(
            self, "AppRunnerDockerService",
            service_name=service_name,
            source=apprunner.Source.from_ecr(
                repository=repository,
                tag_or_digest="latest",
                image_configuration=apprunner.ImageConfiguration(
                    port=5005,
                    environment_variables={
                        "ENVIRONMENT": target_environment,

                        # Flask app configuration
                        "DEBUG": "false",
                        "FLASK_APP": "",
                        "FLASK_ENV": "production",

                        # Database configuration
                        # NOTE: DB_PASS is sensitive -- consider injecting it via
                        # `environment_secrets=` instead of a plaintext value
                        # once a real Secrets Manager secret exists.
                        "DB_ENGINE": "",
                        "DB_HOST": "",
                        "DB_NAME": "",
                        "DB_USERNAME": "",
                        "DB_PASS": "",
                        "DB_PORT": "",

                        # Storage / integrations
                        "S3_UPLOAD_BUCKET": "",
                        "PORTAL_EVENTS_TABLE_NAME": "",
                        # NOTE: SLACK_API_TOKEN is sensitive -- prefer
                        # `environment_secrets=` in production.
                        "SLACK_API_TOKEN": "",

                        "OKR_TABLE_NAME": "",

                        # Redshift configuration
                        # NOTE: REDSHIFT_PASS is sensitive -- prefer
                        # `environment_secrets=` in production.
                        "REDSHIFT_HOST": "",
                        "REDSHIFT_PORT": "",
                        "REDSHIFT_USER": "",
                        "REDSHIFT_PASS": "",
                        "REDSHIFT_DB": "",
                        "REDSHIFT_SSLMODE": "",

                        "NOTIFICATIONS_TABLE_NAME": "",
                        "AWS_ACCESS_KEY_ID": "",
                        "AWS_SECRET_ACCESS_KEY": "",
                    },
                ),
            ),
            access_role=access_role,
            instance_role=instance_role,
            # 1 vCPU / 3 GB memory.
            cpu=apprunner.Cpu.ONE_VCPU,
            memory=apprunner.Memory.THREE_GB,
            health_check=apprunner.HealthCheck.http(
                path="/",
                interval=Duration.seconds(15),
                timeout=Duration.seconds(5),
                healthy_threshold=2,
            ),
            auto_deployments_enabled=True,
        )

        CfnOutput(self, "RepositoryUri", value=repository.repository_uri)
        CfnOutput(self, "ServiceUrl", value=service.service_url)

import aws_cdk as cdk
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_codebuild as codebuild,
    aws_codepipeline as codepipeline,
    aws_codepipeline_actions as codepipeline_actions,
)
from constructs import Construct

from .configuration import (
    DEPLOYMENT, ECS_GITHUB_REPOSITORY_NAME, GITHUB_REPOSITORY_OWNER_NAME, GITHUB_TOKEN,
    get_all_configurations, get_resource_name_prefix
)


class EcsExpressStack(Stack):
    """
    A lightweight, opinionated ECS setup that mirrors Amazon ECS Express Mode's
    fast path to production: a single Fargate service behind an Application
    Load Balancer, sourced from an ECR repository that is kept up to date by a
    CodePipeline connected directly to GitHub. Every push to the target branch
    builds a new container image, pushes it to ECR, and rolls it out to the
    ECS service automatically.
    """

    def __init__(
        self, scope: Construct, construct_id: str, target_environment: str, vpc_id: str, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.mappings = get_all_configurations()
        resource_prefix = get_resource_name_prefix()
        branch = "main" if target_environment.lower() == "dev" else target_environment.lower()
        service_name = f'{target_environment.lower()}-{resource_prefix}-ecs-express'

        # 1. ECR repository -- the single source of truth for container images.
        repository = ecr.Repository(
            self, "EcsExpressRepository",
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

        # 2. Reuse the same VPC as the Aurora Postgres database (looked up by
        # ID, same as AuroraPostgresStack) so the ECS service can reach the
        # database directly without cross-VPC networking.
        vpc = ec2.Vpc.from_lookup(self, "EcsExpressVpc", vpc_id=vpc_id)

        cluster = ecs.Cluster(
            self, "EcsExpressCluster",
            cluster_name=service_name,
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )

        # 3. Fargate service fronted by an Application Load Balancer -- CDK's
        # closest equivalent of the "one command to production" experience
        # that ECS Express Mode provides in the console/CLI.
        #
        # NOTE: the repository is empty on first deploy, so we bootstrap the
        # service with a public placeholder image. The CodePipeline below
        # will replace it with the real application image on its first run.
        fargate_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self, "EcsExpressService",
            service_name=service_name,
            cluster=cluster,
            cpu=256,
            memory_limit_mib=512,
            desired_count=1,
            public_load_balancer=True,
            task_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            assign_public_ip=True,
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_registry("amazon/amazon-ecs-sample"),
                container_name="web",
                container_port=80,
                environment={
                    "ENVIRONMENT": target_environment,

                    # Flask app configuration
                    "DEBUG": "false",
                    "FLASK_APP": "",
                    "FLASK_ENV": "production",

                    # Database configuration
                    # NOTE: DB_PASS is sensitive -- consider injecting it via
                    # `secrets=` (ecs.Secret.from_secrets_manager(...)) instead
                    # of a plaintext environment variable once a real secret exists.
                    "DB_ENGINE": "",
                    "DB_HOST": "",
                    "DB_NAME": "",
                    "DB_USERNAME": "",
                    "DB_PASS": "",
                    "DB_PORT": "3306",

                    # Storage / integrations
                    "S3_UPLOAD_BUCKET": "",
                    "PORTAL_EVENTS_TABLE_NAME": "",
                    # NOTE: SLACK_API_TOKEN is sensitive -- prefer `secrets=`
                    # (ecs.Secret.from_secrets_manager(...)) in production.
                    "SLACK_API_TOKEN": "",

                    "OKR_TABLE_NAME": "",

                    # Redshift configuration
                    # NOTE: REDSHIFT_PASS is sensitive -- prefer `secrets=`
                    # (ecs.Secret.from_secrets_manager(...)) in production.
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
        )

        fargate_service.target_group.configure_health_check(
            path="/",
            healthy_http_codes="200-399",
            healthy_threshold_count=2,
            interval=Duration.seconds(15),
            timeout=Duration.seconds(5),
        )

        # 4. CI/CD: GitHub -> CodeBuild (docker build & push) -> ECS deploy.
        repo_owner = self.mappings[DEPLOYMENT][GITHUB_REPOSITORY_OWNER_NAME]
        repo_name = self.mappings[DEPLOYMENT][ECS_GITHUB_REPOSITORY_NAME]

        source_artifact = codepipeline.Artifact("SourceArtifact")
        build_artifact = codepipeline.Artifact("BuildArtifact")

        build_project = codebuild.PipelineProject(
            self, "EcsExpressBuildProject",
            project_name=f'{service_name}-build',
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                privileged=True,  # required to build/push Docker images
            ),
            environment_variables={
                "ECR_REPOSITORY_URI": codebuild.BuildEnvironmentVariable(value=repository.repository_uri),
                "CONTAINER_NAME": codebuild.BuildEnvironmentVariable(value="web"),
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
                            "printf '[{\"name\":\"%s\",\"imageUri\":\"%s\"}]' "
                            "$CONTAINER_NAME $ECR_REPOSITORY_URI:$IMAGE_TAG > imagedefinitions.json",
                        ]
                    },
                },
                "artifacts": {
                    "files": ["imagedefinitions.json"]
                },
            }),
        )

        repository.grant_pull_push(build_project)

        pipeline = codepipeline.Pipeline(
            self, "EcsExpressPipeline",
            pipeline_name=f'{service_name}-pipeline',
            cross_account_keys=True,
        )

        pipeline.add_stage(
            stage_name="Source",
            actions=[
                codepipeline_actions.GitHubSourceAction(
                    action_name="GitHub_Source",
                    owner=repo_owner,
                    repo=repo_name,
                    branch=branch,
                    oauth_token=cdk.SecretValue.secrets_manager(
                        self.mappings[target_environment][GITHUB_TOKEN]
                    ),
                    output=source_artifact,
                    trigger=codepipeline_actions.GitHubTrigger.WEBHOOK,
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
                    outputs=[build_artifact],
                )
            ],
        )

        pipeline.add_stage(
            stage_name="Deploy",
            actions=[
                codepipeline_actions.EcsDeployAction(
                    action_name="EcsDeploy",
                    service=fargate_service.service,
                    input=build_artifact,
                )
            ],
        )

        CfnOutput(self, "RepositoryUri", value=repository.repository_uri)
        CfnOutput(self, "ServiceUrl", value=f"http://{fargate_service.load_balancer.load_balancer_dns_name}")

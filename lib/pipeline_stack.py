# Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from aws_cdk import Stack, SecretValue
from constructs import Construct

from aws_cdk import pipelines as pipelines
from aws_cdk import aws_iam as iam
from aws_cdk.pipelines import CodePipeline, CodePipelineSource, ShellStep
from aws_cdk.aws_codepipeline_actions import GitHubTrigger


from .configuration import (
    DEPLOYMENT, GITHUB_REPOSITORY_NAME, GITHUB_REPOSITORY_OWNER_NAME, GITHUB_TOKEN,
    get_logical_id_prefix, get_resource_name_prefix, get_all_configurations
)
from .pipeline_deploy_stage import PipelineDeployStage


class PipelineStack(Stack):

    def __init__(
        self, scope: Construct, construct_id: str,
        target_environment: str, target_branch: str, target_aws_env: dict,
        **kwargs
    ) -> None:
        """
        CloudFormation stack to create CDK Pipeline resources (Code Pipeline, Code Build, and ancillary resources).

        @param scope cdk.Construct: Parent of this stack, usually an App or a Stage, but could be any construct.
        @param construct_id str:
            The construct ID of this stack. If stackName is not explicitly defined,
            this id (and any parent IDs) will be used to determine the physical ID of the stack.
        @param target_environment str: The target environment for stacks in the deploy stage
        @param target_branch str: The source branch for polling
        @param target_aws_env dict: The CDK env variable used for stacks in the deploy stage
        """
        super().__init__(scope, construct_id, **kwargs)

        self.mappings = get_all_configurations()
        self.create_environment_pipeline(
            target_environment,
            target_branch,
            target_aws_env
        )

    def create_environment_pipeline(self, target_environment, target_branch, target_aws_env):
        """
        Creates CloudFormation stack to create CDK Pipeline resources such as:
        Code Pipeline, Code Build, and ancillary resources.

        @param target_environment str: The target environment for stacks in the deploy stage
        @param target_branch str: The source branch for polling
        @param target_aws_env dict: The CDK env variable used for stacks in the deploy stage
        """

        logical_id_prefix = get_logical_id_prefix()

        # repository = self.mappings[DEPLOYMENT][GITHUB_REPOSITORY_OWNER_NAME] + \
        # "/" + self.mappings[DEPLOYMENT][GITHUB_REPOSITORY_NAME]

        repository = "Last-Mile-Health/lmd-aws-cdk-pipelines-application-infrastructure"
        resource_name_prefix = get_resource_name_prefix()

        input = CodePipelineSource.git_hub(
            repository,
            target_branch,
            authentication=SecretValue.secrets_manager(self.mappings[DEPLOYMENT][GITHUB_TOKEN]),
            trigger=GitHubTrigger.WEBHOOK
        )

        pipeline = CodePipeline(
            self,
            f'{target_environment}{logical_id_prefix}ApplicationPipeline',
            pipeline_name=f'{target_environment.lower()}-{resource_name_prefix}-infrastructure-pipeline',
            self_mutation=False,
            cross_account_keys=True,
            synth=ShellStep(
                "Synth",
                input=input,
                commands=[
                    "npm install -g aws-cdk",
                    "pip3 install -r requirements.txt",
                    f'export ENV={target_environment} && cdk synth --verbose'
                ],
            ),

        )
        # source_artifact = codepipeline.Artifact()
        # cloud_assembly_artifact = codepipeline.Artifact()
        # logical_id_prefix = get_logical_id_prefix()
        # resource_name_prefix = get_resource_name_prefix()
        # pipeline = pipelines.CdkPipeline(
        #     self,
        #     f'{target_environment}{logical_id_prefix}InfrastructurePipeline',
        #     pipeline_name=f'{target_environment.lower()}-{resource_name_prefix}-infrastructure-pipeline',
        #     cloud_assembly_artifact=cloud_assembly_artifact,
        #     source_action=codepipeline_actions.GitHubSourceAction(
        #         action_name='GitHub',
        #         branch=target_branch,
        #         output=source_artifact,
        #         oauth_token=SecretValue.secrets_manager(
        #             self.mappings[DEPLOYMENT][GITHUB_TOKEN]
        #         ),
        #         trigger=codepipeline_actions.GitHubTrigger.WEBHOOK,
        #         owner=self.mappings[DEPLOYMENT][GITHUB_REPOSITORY_OWNER_NAME],
        #         repo=self.mappings[DEPLOYMENT][GITHUB_REPOSITORY_NAME],
        #     ),
        #     synth_action=pipelines.SimpleSynthAction.standard_npm_synth(
        #         source_artifact=source_artifact,
        #         cloud_assembly_artifact=cloud_assembly_artifact,
        #         install_command='npm install -g aws-cdk && pip3 install -r requirements.txt',
        #         role_policy_statements=[
        #             iam.PolicyStatement(
        #                 sid='InfrastructurePipelineSecretsManagerPolicy',
        #                 effect=iam.Effect.ALLOW,
        #                 actions=[
        #                     'secretsmanager:*',
        #                 ],
        #                 resources=[
        #                     f'arn:aws:secretsmanager:{self.region}:{self.account}:secret:/DataLake/*',
        #                 ],
        #             ),
        #             iam.PolicyStatement(
        #                 sid='InfrastructurePipelineSTSAssumeRolePolicy',
        #                 effect=iam.Effect.ALLOW,
        #                 actions=[
        #                     'sts:AssumeRole',
        #                 ],
        #                 resources=[
        #                     '*',
        #                 ],
        #             ),
        #             iam.PolicyStatement(
        #                 sid='InfrastructurePipelineKmsPolicy',
        #                 effect=iam.Effect.ALLOW,
        #                 actions=[
        #                     'kms:*',
        #                 ],
        #                 resources=[
        #                     '*',
        #                 ],
        #             ),
        #             iam.PolicyStatement(
        #                 sid='InfrastructurePipelineVpcPolicy',
        #                 effect=iam.Effect.ALLOW,
        #                 actions=[
        #                     'vpc:*',
        #                 ],
        #                 resources=[
        #                     '*',
        #                 ],
        #             ),
        #             iam.PolicyStatement(
        #                 sid='InfrastructurePipelineEc2Policy',
        #                 effect=iam.Effect.ALLOW,
        #                 actions=[
        #                     'ec2:*',
        #                 ],
        #                 resources=[
        #                     '*',
        #                 ],
        #             ),
        #         ],
        #         synth_command=f'export ENV={target_environment} && cdk synth --verbose',
        #     ),
        #     cross_account_keys=True,
        # )

        # TODO: Refactor this code.

        pipeline.add_stage(
            PipelineDeployStage(
                self,
                target_environment,
                target_environment=target_environment,
                env=target_aws_env,
            )
        )

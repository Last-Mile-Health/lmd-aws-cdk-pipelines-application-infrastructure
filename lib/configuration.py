# Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import re

# Environments (targeted at accounts)
DEPLOYMENT = 'Deployment'
DEV = 'Dev'
TEST = 'Test'
PROD = 'Prod'

# The following constants are used to map to parameter/secret paths
ENVIRONMENT = 'environment'

# Manual Inputs
GITHUB_REPOSITORY_OWNER_NAME = 'Last-Mile-Health'
GITHUB_REPOSITORY_NAME = 'lmd-aws-cdk-pipelines-application-infrastructure'
ADMIN_EMAIL = 'bmwalwanda@lastmilehealth.org'
AMPLIFY_GITHUB_REPOSITORY_NAME = 'lmd-portal-UI'
SERVICE_GITHUB_REPOSITORY_NAME = 'lmd-portal-services'
AMPLIFY_GITHUB_TOKEN = "token"

ACCOUNT_ID = '829553079673'
REGION = 'us-east-1'
LOGICAL_ID_PREFIX = 'LMDCDKApplication'
RESOURCE_NAME_PREFIX = 'lmd-application'
VPC_CIDR = '10.20.0.0/24'

ENGINE_VERSION = '5.6'
ENGINE_NAME = 'mysql'
MAINTENANCE_WINDOW = 'Sun'
DB_INSTANCE_CLASS = 'db.t2.micro'

# Cognito Inputs
COGNITO_CALLBACK_URLS = 'cognito_callback_urls'
COGNITO_LOGOUT_URLS = 'cognito_logout_urls'

# Secrets Manager Inputs
GITHUB_TOKEN = 'github_token'
DB_USERNAME = 'db_username'
DB_PASSWORD = 'db_password'

# Used in Automated Outputs
VPC_ID = 'vpc_id'
AVAILABILITY_ZONE_1 = 'availability_zone_1'
AVAILABILITY_ZONE_2 = 'availability_zone_2'
AVAILABILITY_ZONE_3 = 'availability_zone_3'
SUBNET_ID_1 = 'subnet_id_1'
SUBNET_ID_2 = 'subnet_id_2'
SUBNET_ID_3 = 'subnet_id_3'
ROUTE_TABLE_1 = 'route_table_1'
ROUTE_TABLE_2 = 'route_table_2'
ROUTE_TABLE_3 = 'route_table_3'
SHARED_SECURITY_GROUP_ID = 'shared_security_group_id'

S3_KMS_KEY = 's3_kms_key'
S3_ACCESS_LOG_BUCKET = 's3_access_log_bucket'
S3_UPLOAD_BUCKET = 's3_upload_bucket'


def get_local_configuration(environment: str) -> dict:
    """
    Provides manually configured variables that are validated for quality and safety.

    @param: environment str: The environment used to retrieve corresponding configuration
    @raises: Exception: Throws an exception if the resource_name_prefix does not conform
    @raises: Exception: Throws an exception if the requested environment does not exist
    @returns: dict:
    """
    local_mapping = {
        DEPLOYMENT: {
            ACCOUNT_ID: '829553079673',
            REGION: 'us-east-1',
            GITHUB_REPOSITORY_OWNER_NAME: 'Last-Mile-Health',
            GITHUB_REPOSITORY_NAME: 'lmd-aws-cdk-pipelines-application-infrastructure',
            AMPLIFY_GITHUB_REPOSITORY_NAME: 'LastMileData2',
            AMPLIFY_GITHUB_REPOSITORY_NAME: 'lmd-portal-UI',
            SERVICE_GITHUB_REPOSITORY_NAME: 'lmd-portal-services',
            LOGICAL_ID_PREFIX: 'LMDCDKApplication',
            RESOURCE_NAME_PREFIX: 'lmd-application',
            COGNITO_CALLBACK_URLS: 'https://main.d1gfzcw5a606s8.amplifyapp.com/',
            COGNITO_LOGOUT_URLS: 'https://main.d1gfzcw5a606s8.amplifyapp.com/auth/sign-in',
            AMPLIFY_GITHUB_TOKEN: "/Amplify/GitHubToken"
        },
        DEV: {
            ACCOUNT_ID: '002190277880',
            REGION: 'us-east-1',
            VPC_CIDR: '10.20.0.0/24',
            AMPLIFY_GITHUB_TOKEN: "/Amplify/GitHubToken"
        },
        TEST: {
            ACCOUNT_ID: '576140831944',
            REGION: 'us-east-1',
            VPC_CIDR: '10.10.0.0/24',
            AMPLIFY_GITHUB_TOKEN: "/Amplify/GitHubToken"
        },
        PROD: {
            ACCOUNT_ID: '301323023124',
            REGION: 'us-east-1',
            VPC_CIDR: '10.0.0.0/24',
            AMPLIFY_GITHUB_TOKEN: "/Amplify/GitHubToken"
        }
    }

    resource_prefix = local_mapping[DEPLOYMENT][RESOURCE_NAME_PREFIX]
    if (
        not re.fullmatch('^[a-z|0-9|-]+', resource_prefix)
        or '-' in resource_prefix[-1:] or '-' in resource_prefix[1]
    ):
        raise Exception('Resource names may only contain lowercase Alphanumeric and hyphens '
                        'and cannot contain leading or trailing hyphens')

    if environment not in local_mapping:
        raise Exception(f'The requested environment: {environment} does not exist in local mappings')

    return local_mapping[environment]


def get_environment_configuration(environment: str) -> dict:
    """
    Provides all configuration values for the given target environment

    @param environment str: The environment used to retrieve corresponding configuration

    @return: dict:
    """
    cloudformation_output_mapping = {
        ENVIRONMENT: environment,
        VPC_ID: f'{environment}VpcId',
        AVAILABILITY_ZONE_1: f'{environment}AvailabilityZone1',
        AVAILABILITY_ZONE_2: f'{environment}AvailabilityZone2',
        AVAILABILITY_ZONE_3: f'{environment}AvailabilityZone3',
        SUBNET_ID_1: f'{environment}SubnetId1',
        SUBNET_ID_2: f'{environment}SubnetId2',
        SUBNET_ID_3: f'{environment}SubnetId3',
        ROUTE_TABLE_1: f'{environment}RouteTable1',
        ROUTE_TABLE_2: f'{environment}RouteTable2',
        ROUTE_TABLE_3: f'{environment}RouteTable3',
        SHARED_SECURITY_GROUP_ID: f'{environment}SharedSecurityGroupId',
        S3_KMS_KEY: f'{environment}S3KmsKeyArn',
        S3_ACCESS_LOG_BUCKET: f'{environment}S3AccessLogBucket',
        S3_UPLOAD_BUCKET: f'{environment}S3UploadBucket',
    }

    return {**cloudformation_output_mapping, **get_local_configuration(environment)}


def get_all_configurations() -> dict:
    """
    Returns a dict mapping of configurations for all environments.
    These keys correspond to static values, CloudFormation outputs, and Secrets Manager (passwords only) records.

    @return: dict:
    """
    return {
        DEPLOYMENT: {
            ENVIRONMENT: DEPLOYMENT,
            GITHUB_TOKEN: '/DataLake/GitHubToken',
            AMPLIFY_GITHUB_TOKEN: "/Amplify/GitHubToken",
            **get_local_configuration(DEPLOYMENT),
        },
        DEV: {
            GITHUB_TOKEN: '/DataLake/GitHubToken',
            AMPLIFY_GITHUB_TOKEN: "/Amplify/GitHubToken",
            ** get_environment_configuration(DEV)
        },
        TEST: {
            GITHUB_TOKEN: '/DataLake/GitHubToken',
            AMPLIFY_GITHUB_TOKEN: "/Amplify/GitHubToken",
            ** get_environment_configuration(TEST)
        },
        PROD: {
            GITHUB_TOKEN: '/DataLake/GitHubToken',
            AMPLIFY_GITHUB_TOKEN: "/Amplify/GitHubToken",
            ** get_environment_configuration(PROD)
        },
    }


def get_logical_id_prefix() -> str:
    """Returns the logical id prefix to apply to all CloudFormation resources

    @return: str:
    """
    return get_local_configuration(DEPLOYMENT)[LOGICAL_ID_PREFIX]


def get_resource_name_prefix() -> str:
    """Returns the resource name prefix to apply to all resources names

    @return: str:
    """
    return get_local_configuration(DEPLOYMENT)[RESOURCE_NAME_PREFIX]

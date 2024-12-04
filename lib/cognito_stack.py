from aws_cdk import (
    Stack,
    SecretValue,
    CfnOutput,
    aws_cognito as cognito,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

from .configuration import (
    DEPLOYMENT,
    COGNITO_CALLBACK_URLS,
    COGNITO_LOGOUT_URLS,
    ADMIN_EMAIL,
    get_all_configurations,
)


class CognitoStack(Stack):
    def __init__(self, scope: Construct, id: str, target_environment: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        user_pool = cognito.UserPool(
            self,
            f'{id}-user-pool',
            user_pool_name=f'{target_environment}-user-pool',
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(
                email=True,
                username=True
            ),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(
                    required=True,
                    mutable=True
                )
            ),
            auto_verify=cognito.AutoVerifiedAttrs(
                email=True
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True
            ),
            email=cognito.UserPoolEmail.with_ses(
                from_email=ADMIN_EMAIL,
                reply_to=ADMIN_EMAIL,
            )
        )

        # Add domain prefix
        domain = user_pool.add_domain(
            'cognito-domain',
            cognito_domain=dict(
                domain_prefix=f'lmd-app-{target_environment.lower()}'
            )
        )

        # get google client secret from secrets manager
        google_client_secret = secretsmanager.Secret.from_secret_name_v2(
            self,
            'GoogleClientSecret',
            'google-oauth-token'
        ).secret_value.unsafe_unwrap()

        # Add Google as Id and map attributes
        google_provider = cognito.UserPoolIdentityProviderGoogle(
            self,
            'GoogleProvider',
            user_pool=user_pool,
            client_id='359915249843-tfq6m4cmskb94kj83lqb036hm5u2fv84.apps.googleusercontent.com',
            client_secret=google_client_secret,
            scopes=[
                'email',
                'openid',
                'phone',
                'profile',
                'aws.cognito.signin.user.admin'
            ],

            attribute_mapping=cognito.AttributeMapping(
                email=cognito.ProviderAttribute.GOOGLE_EMAIL,
            )
        )

        # user pool client
        user_pool_client = user_pool.add_client(
            f'{id}-user-pool-client',
            user_pool_client_name=f'{target_environment}-app-client',
            prevent_user_existence_errors=True,
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.GOOGLE
            ],
            generate_secret=True,
            auth_flows=cognito.AuthFlow(
                user_srp=True,
                custom=True,
                user_password=True,
                admin_user_password=True,
            ),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(
                    authorization_code_grant=True
                ),
                scopes=[
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.PHONE,
                    cognito.OAuthScope.PROFILE,
                    cognito.OAuthScope.COGNITO_ADMIN
                ],
                callback_urls=[
                    'http://localhost:3000/api/auth/callback/cognito',
                    'https://main.d1gfzcw5a606s8.amplifyapp.com/api/auth/callback/cognito'

                ],
                logout_urls=[
                    'http://localhost:3000',
                    'https://main.d1gfzcw5a606s8.amplifyapp.com'
                ]
            )
        )

        # Add dependency to ensure Google provider is created before the client
        user_pool_client.node.add_dependency(google_provider)

        # output values for the frontend app
        CfnOutput(
            self,
            'UserPoolId',
            value=user_pool.user_pool_id,
            description='Cognito User Pool ID'
        )

        CfnOutput(
            self,
            'UserPoolClientId',
            value=user_pool_client.user_pool_client_id,
            description='Cognito User Pool Client ID'
        )

        CfnOutput(
            self,
            'CognitoDomain',
            value=domain.domain_name,
            description='Cognito Domain'
        )

        CfnOutput(
            self,
            'UserPoolArn',
            value=user_pool.user_pool_arn,
            description='Cognito User Pool ARN'
        )

        CfnOutput(
            self,
            'UserPoolClientSecret',
            value=user_pool_client.user_pool_client_secret.unsafe_unwrap(),
            description='Cognito User Pool Client Secret'
        )

from aws_cdk import (
    Stack,
    CfnOutput,
    SecretValue,
    aws_iam as iam,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct


class IamUserStack(Stack):
    def __init__(
        self, scope: Construct, id: str, target_environment: str, **kwargs
    ) -> None:
        super().__init__(scope, id, **kwargs)

        env_lower = target_environment.lower()
        user_name = f"{env_lower}-lmd-portal-user"

        user = iam.User(
            self,
            f"{id}-service-user",
            user_name=user_name,
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AdministratorAccess")
            ],
        )

        access_key = iam.AccessKey(self, f"{id}-access-key", user=user)

        secret = secretsmanager.Secret(
            self,
            f"{id}-credentials-secret",
            secret_name=f"{env_lower}/iam-service-user/credentials",
            secret_object_value={
                "access_key_id": SecretValue.unsafe_plain_text(access_key.access_key_id),
                "secret_access_key": access_key.secret_access_key,
            },
            description=f"Credentials for {user_name}",
        )

        CfnOutput(self, "SecretArn", value=secret.secret_arn)

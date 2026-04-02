from aws_cdk import (
    Stack,
    CfnOutput,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_rds as rds,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct


class AuroraPostgresStack(Stack):
    def __init__(
        self, scope: Construct, id: str, target_environment: str, **kwargs
    ) -> None:
        super().__init__(scope, id, **kwargs)

        vpc = ec2.Vpc(
            self,
            f"{id}-vpc",
            availability_zones=[
                f"{self.region}a",
                f"{self.region}b",
            ],
        )

        security_group = ec2.SecurityGroup(
            self,
            f"{id}-sg",
            vpc=vpc,
            allow_all_outbound=True,
        )
        security_group.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(5432),
            "Allow public PostgreSQL access",
        )

        db_secret = secretsmanager.Secret(
            self,
            f"{id}-db-secret",
            secret_name=f"{target_environment.lower()}/aurora-postgres/credentials",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"username": "dbadmin"}',
                generate_string_key="password",
                exclude_punctuation=True,
            ),
        )

        cluster = rds.DatabaseCluster(
            self,
            f"{id}-aurora-cluster",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_15_4
            ),
            cluster_identifier=f"{target_environment.lower()}-lmd-portal-database",
            credentials=rds.Credentials.from_secret(db_secret),
            writer=rds.ClusterInstance.serverless_v2(f"{id}-writer", publicly_accessible=True),
            readers=[
                rds.ClusterInstance.serverless_v2(
                    f"{id}-reader", scale_with_writer=True, publicly_accessible=True
                )
            ],
            serverless_v2_min_capacity=0.5,
            serverless_v2_max_capacity=4,
            security_groups=[security_group],
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            default_database_name="master",
            removal_policy=RemovalPolicy.SNAPSHOT,
        )

        CfnOutput(self, "ClusterEndpoint", value=cluster.cluster_endpoint.hostname)
        CfnOutput(self, "ClusterPort", value=str(cluster.cluster_endpoint.port))
        CfnOutput(self, "SecretArn", value=db_secret.secret_arn)

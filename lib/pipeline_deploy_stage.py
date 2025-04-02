
from aws_cdk import Stage
from .tagging import tag
from constructs import Construct
from .amplify_stack import AmplifyStack
from .cognito_stack import CognitoStack
from .beanstalk_stack import BeanstalkStack
from .apprunner_stack import AppRunnerStack


class PipelineDeployStage(Stage):
    def __init__(self, scope: Construct, construct_id: str, target_environment: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # Create the ServerlessBackendStack
        '''
        backend_service = ServerlessBackendStack(
            self,
            f"{target_environment}-serverless-backend",
            target_environment,
            **kwargs,
        )
        '''

        # Create the Amplify stack
        amplify_stack = AmplifyStack(
            self,
            f"{target_environment}-amplify",
            target_environment=target_environment,
            **kwargs,
        )

        # Create the Cognito stack
        cognito_stack = CognitoStack(
            self,
            f"{target_environment}-cognito",
            target_environment=target_environment,
            **kwargs,
        )

        # create the beanstalk environment with codedeploy and codepipeline integration
        beanstalk_stack = BeanstalkStack(
            self,
            f"{target_environment}-beanstalk",
            target_environment=target_environment,
            **kwargs,
        )

        app_runner_stack = AppRunnerStack(
            self,
            f"{target_environment}-apprunner",
            target_environment=target_environment,
            **kwargs,
        )

        # Tag the backend_service and amplify_stack with the target_environment
        tag(amplify_stack, target_environment)
        tag(cognito_stack, target_environment)
        tag(beanstalk_stack, target_environment)
        tag(app_runner_stack, target_environment)
